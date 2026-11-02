"""Offline adapter for already-labelled, pseudonymous Vigil prediction windows.

It does not download data or define clinical outcomes. The extract owner supplies
complete cumulative labels and a versioned definition identifier. Only information
available at prediction time becomes a model feature. Errors never echo rows/paths.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re

import torch
from torch.utils.data import Sampler
from models.vigil_cfc import HORIZONS_HOURS, PRODUCTION_FEATURE_ORDER

MAX_OBSERVATIONS = 24  # same cap as vigil_service._assemble_feature_sequence
MAX_WINDOWS = 250000
VITAL_DEFAULTS = {"heart_rate": 80.0, "spo2": 97.0, "temperature": 36.8,
                  "systolic_bp": 120.0, "diastolic_bp": 70.0, "respiratory_rate": 18.0}
AVPU = {"alert": 0.0, "a": 0.0, "verbal": 1.0, "v": 1.0, "pain": 2.0, "p": 2.0,
        "confusion": 3.0, "c": 3.0, "new_confusion": 3.0, "unresponsive": 3.0, "u": 3.0}
ROW_KEYS = {"schema_version", "outcome", "subject_id", "encounter_id", "encounter_started_at",
            "prediction_at", "age", "age_available_at", "observations", "labels", "labels_complete",
            "data_source", "label_definition"}
OBS_KEYS = {"observed_at", "available_at", "consciousness", *VITAL_DEFAULTS}


class ExtractError(ValueError):
    """Safe diagnostic: field names and line numbers, no submitted values."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExtractError("JSON objects must not contain duplicate fields")
        result[key] = value
    return result


def _object(value, allowed, required, field):
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise ExtractError(f"{field} has missing or unsupported fields")
    return value


def _identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ExtractError(f"{field} must be a bounded opaque identifier")
    return value


def _stamp(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise ExtractError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ExtractError(f"{field} must be a timezone-aware ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExtractError(f"{field} must include a timezone")
    return parsed


def _number(value, field):
    if type(value) not in (int, float):
        raise ExtractError(f"{field} must be a finite number or omitted")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ExtractError(f"{field} must be a finite number") from None
    if not math.isfinite(result):
        raise ExtractError(f"{field} must be finite")
    return result


@dataclass
class Window:
    subject: str
    encounter: str
    prediction: datetime
    features: torch.Tensor
    dt: torch.Tensor
    labels: torch.Tensor
    source: str
    definition: str


def parse_window(value, outcome):
    row = _object(value, ROW_KEYS, ROW_KEYS - {"age", "age_available_at"}, "window")
    if type(row["schema_version"]) is not int or row["schema_version"] != 1 or row["outcome"] != outcome:
        raise ExtractError("schema_version or outcome does not match this run")
    subject = _identifier(row["subject_id"], "subject_id")
    encounter = _identifier(row["encounter_id"], "encounter_id")
    source = _identifier(row["data_source"], "data_source")
    definition = _identifier(row["label_definition"], "label_definition")
    start = _stamp(row["encounter_started_at"], "encounter_started_at")
    prediction = _stamp(row["prediction_at"], "prediction_at")
    if prediction < start:
        raise ExtractError("prediction_at precedes the encounter")
    if row["labels_complete"] is not True:
        raise ExtractError("Every horizon must have an ascertained label; censored labels are not negatives")
    labels = _object(row["labels"], {f"{h}h" for h in HORIZONS_HOURS}, {f"{h}h" for h in HORIZONS_HOURS}, "labels")
    target = [labels[f"{h}h"] for h in HORIZONS_HOURS]
    if any(type(label) is not int or label not in (0, 1) for label in target) or target != sorted(target):
        raise ExtractError("labels must be cumulative binary integers in horizon order")
    age = 62.0
    if row.get("age") is not None:
        age = _number(row["age"], "age")
        if "age_available_at" not in row or _stamp(row["age_available_at"], "age_available_at") > prediction:
            raise ExtractError("age must be available by prediction_at")
    elif row.get("age_available_at") is not None:
        raise ExtractError("age_available_at requires an age value")
    observations = row["observations"]
    if not isinstance(observations, list) or not 3 <= len(observations) <= MAX_OBSERVATIONS:
        raise ExtractError("observations must contain 3 to 24 chronological entries")
    features, elapsed, previous = [], [], None
    for item in observations:
        obs = _object(item, OBS_KEYS, {"observed_at", "available_at"}, "observation")
        observed = _stamp(obs["observed_at"], "observed_at")
        available = _stamp(obs["available_at"], "available_at")
        if observed < start or observed > prediction or available < observed or available > prediction:
            raise ExtractError("Observation must belong to this encounter and be available by prediction_at")
        if previous is not None and observed <= previous:
            raise ExtractError("Observation timestamps must be strictly increasing")
        raw = {name: default if obs.get(name) is None else _number(obs[name], name) for name, default in VITAL_DEFAULTS.items()}
        # Production uses a per-observation default, not future/backward fill or
        # a measured MAP channel. Derive MAP from the same imputed SBP/DBP.
        raw["map"] = raw["diastolic_bp"] + (raw["systolic_bp"] - raw["diastolic_bp"]) / 3.0
        cons = obs.get("consciousness")
        if cons is not None and not isinstance(cons, str):
            raise ExtractError("consciousness must use the production AVPU vocabulary")
        cons = cons or "alert"
        if not isinstance(cons, str) or cons.lower() not in AVPU:
            raise ExtractError("consciousness must use the production AVPU vocabulary")
        raw["avpu"], raw["age"] = AVPU[cons.lower()], age
        features.append([raw[name] for name in PRODUCTION_FEATURE_ORDER])
        elapsed.append([0.0 if previous is None else (observed - previous).total_seconds() / 3600.0])
        previous = observed
    feat = torch.tensor(features, dtype=torch.float32)
    dt = torch.tensor(elapsed, dtype=torch.float32)
    if not torch.isfinite(feat).all() or not torch.isfinite(dt).all():
        raise ExtractError("Feature or interval is outside the model's finite numeric representation")
    return Window(subject, encounter, prediction, feat, dt, torch.tensor(target, dtype=torch.float32), source, definition)


@dataclass
class LabelledDataset:
    train: list
    validation: list
    feature_mean: torch.Tensor
    feature_std: torch.Tensor
    metadata: dict


def load_extract(path, outcome, *, train_fraction=0.85, seed=42):
    if outcome not in {"deterioration", "mortality"}:
        raise ExtractError("This adapter accepts deterioration or mortality labels")
    if not 0 < train_fraction < 1:
        raise ExtractError("Training fraction must be between zero and one")
    windows, seen, encounter_owners = [], set(), {}
    digest = hashlib.sha256()
    try:
        stream = Path(path).open("rb")
    except (OSError, TypeError, ValueError):
        raise ExtractError("Could not read the labelled extract; supply an accessible local JSONL file") from None
    try:
        with stream:
            line_number = 0
            while True:
                line = stream.readline(65538)
                if not line:
                    break
                line_number += 1
                if len(line) > 65536:
                    raise ExtractError(f"Line {line_number}: window exceeds 64 KiB")
                digest.update(line)
                try:
                    window = parse_window(json.loads(line, object_pairs_hook=_unique_object), outcome)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise ExtractError(f"Line {line_number}: invalid JSON") from None
                except ExtractError as exc:
                    raise ExtractError(f"Line {line_number}: {exc}") from None
                identity = (window.subject, window.encounter, window.prediction)
                if identity in seen:
                    raise ExtractError(f"Line {line_number}: duplicate prediction window")
                seen.add(identity)
                if encounter_owners.setdefault(window.encounter, window.subject) != window.subject:
                    raise ExtractError(f"Line {line_number}: encounter assigned to multiple subjects")
                if windows and (window.source, window.definition) != (windows[0].source, windows[0].definition):
                    raise ExtractError(f"Line {line_number}: mixed source or label definitions")
                windows.append(window)
                if len(windows) > MAX_WINDOWS:
                    raise ExtractError("Extract is too large; prepare a bounded training extract")
    except OSError:
        raise ExtractError("Could not finish reading the labelled extract") from None
    subjects = sorted({window.subject for window in windows})
    if len(subjects) < 2:
        raise ExtractError("At least two distinct subjects are needed for a patient-separated validation split")
    random.Random(seed).shuffle(subjects)
    split = max(1, min(len(subjects) - 1, int(len(subjects) * train_fraction)))
    training_subjects = set(subjects[:split])
    training = [window for window in windows if window.subject in training_subjects]
    validation = [window for window in windows if window.subject not in training_subjects]
    corpus = torch.cat([window.features for window in training])
    mean, std = corpus.mean(0), corpus.std(0, unbiased=False).clamp(min=1e-3)
    if not torch.isfinite(mean).all() or not torch.isfinite(std).all():
        raise ExtractError("Training normalization exceeds finite numeric limits")
    def normalized(rows):
        samples = [((window.features - mean) / std, window.dt, window.labels) for window in rows]
        if any(not torch.isfinite(sample[0]).all() for sample in samples):
            raise ExtractError("Normalized features exceed finite numeric limits")
        return samples
    return LabelledDataset(normalized(training), normalized(validation), mean, std,
                           {"data_source": windows[0].source, "label_definition": windows[0].definition,
                            "extract_schema_version": 1, "extract_sha256": digest.hexdigest(), "split_unit": "subject",
                            "train_subjects": split, "validation_subjects": len(subjects) - split,
                            "train_windows": len(training), "validation_windows": len(validation), "split_seed": seed,
                            "feature_assembly": "production-v1-derived-map-per-row-defaults", "clinical_validation": "not_assessed"})


class EqualLengthBatchSampler(Sampler):
    """Padding changes CfC's final state. Batch equal-length windows instead."""
    def __init__(self, samples, batch_size=64, *, shuffle=False, seed=42):
        self.groups = defaultdict(list)
        for index, (features, _, _) in enumerate(samples):
            self.groups[len(features)].append(index)
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size, self.shuffle, self.seed, self.epoch = batch_size, shuffle, seed, 0

    def __iter__(self):
        rng, batches = random.Random(self.seed + self.epoch), []
        self.epoch += 1
        for indices in self.groups.values():
            indices = list(indices)
            if self.shuffle:
                rng.shuffle(indices)
            batches.extend(indices[i:i+self.batch_size] for i in range(0, len(indices), self.batch_size))
        if self.shuffle:
            rng.shuffle(batches)
        yield from batches

    def __len__(self):
        return sum(math.ceil(len(indices) / self.batch_size) for indices in self.groups.values())


def collate_equal_length(batch):
    features, dt, labels = zip(*batch)
    if len({len(item) for item in features}) != 1:
        raise ExtractError("Unequal sequences cannot be padded into a CfC final-state batch")
    return torch.stack(features), torch.stack(dt), torch.stack(labels)
