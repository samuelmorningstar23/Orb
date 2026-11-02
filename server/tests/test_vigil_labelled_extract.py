"""Synthetic labelled extracts only: no training or credentialed data reads."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import pytest
import torch
from scripts import vigil_labelled_extract as E, train_vigil_cfc as T
from models.vigil_cfc import PRODUCTION_FEATURE_ORDER

START = datetime(2026, 10, 11, tzinfo=timezone.utc)
def stamp(hours): return (START + timedelta(hours=hours)).isoformat()
def window(subject="subject-a", encounter=None, hr=80, length=3, target=None):
    return {"schema_version": 1, "outcome": "mortality", "subject_id": subject, "encounter_id": encounter or f"stay-{subject}", "encounter_started_at": stamp(0), "prediction_at": stamp(5), "age": 50, "age_available_at": stamp(0), "observations": [{"observed_at": stamp(i), "available_at": stamp(i + .1), "heart_rate": hr} for i in range(length)], "labels": dict(zip(("1h", "3h", "6h", "12h", "24h"), target or [0, 0, 0, 0, 1])), "labels_complete": True, "data_source": "synthetic-only-v1", "label_definition": "authored-endpoint-v1"}
def extract(tmp_path, rows):
    path = tmp_path / "synthetic-labelled.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path

def test_production_feature_defaults_map_avpu_age_and_elapsed_parity(monkeypatch):
    from services import vigil_service as V
    row = window(length=4)
    row["observations"][1].update(systolic_bp=135, diastolic_bp=75, consciousness="verbal", temperature=None)
    row["observations"][2].update(observed_at=stamp(2.5), available_at=stamp(2.6), consciousness="confusion")
    row["observations"][3].update(observed_at=stamp(4), available_at=stamp(4.1), heart_rate=None)
    parsed = E.parse_window(row, "mortality")
    observations = [{**{key: value for key, value in obs.items() if key not in {"observed_at", "available_at"}}, "timestamp": obs["observed_at"]} for obs in row["observations"]]
    class Cursor:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows
        def fetchone(self): return self.rows[0]
    class FakeDb:
        def execute(self, sql, args): return Cursor(list(reversed(observations)) if "FROM vitals" in sql else [{"age": 50}])
    @contextmanager
    def synthetic_db(): yield FakeDb()
    monkeypatch.setattr(V, "db_session", synthetic_db)
    monkeypatch.setattr(V, "current_stay_start_on", lambda *args: None)
    monkeypatch.setattr(V, "this_stay_rows", lambda rows, start: rows)
    monkeypatch.setattr(V, "_cfc_device", "cpu")
    actual, dt = V._assemble_feature_sequence("synthetic", PRODUCTION_FEATURE_ORDER, [0]*9, [1]*9)
    assert torch.equal(actual[0], parsed.features) and torch.equal(dt[0], parsed.dt)
    assert parsed.dt[:, 0].tolist() == [0, 1, 1.5, 1.5]
    assert parsed.features[1, PRODUCTION_FEATURE_ORDER.index("map")] == 95
    assert parsed.features[3, 0] == 80

@pytest.mark.parametrize("change", [
    lambda r: r.update(schema_version=True), lambda r: r.update(outcome="deterioration"), lambda r: r.update(labels_complete=False),
    lambda r: r["labels"].update({"1h": True}), lambda r: r["labels"].update({"1h": 1, "3h": 0}), lambda r: r["labels"].pop("24h"),
    lambda r: r.update(unapproved_outcome_column=1), lambda r: r.update(age_available_at=stamp(6)), lambda r: r.update(age=float("inf")),
    lambda r: r["observations"][0].update(heart_rate=True), lambda r: r["observations"][0].update(heart_rate=float("nan")),
    lambda r: r["observations"][0].update(map=80), lambda r: r["observations"][0].update(consciousness="unsupported"), lambda r: r["observations"][0].update(consciousness=False),
    lambda r: r["observations"][0].update(observed_at=stamp(-1)), lambda r: r["observations"][0].update(available_at=stamp(6)),
    lambda r: r["observations"][0].update(available_at=stamp(-1)), lambda r: r["observations"][1].update(observed_at=stamp(0)),
    lambda r: r["observations"][2].update(observed_at=stamp(6), available_at=stamp(6)),
    lambda r: r["observations"][0].update(observed_at="2026-10-11T00:00:00"), lambda r: r.update(observations=r["observations"][:2]),
])
def test_reject_schema_censoring_future_or_cross_stay_features(change):
    row = window(); change(row)
    with pytest.raises(E.ExtractError): E.parse_window(row, "mortality")

def test_patient_split_groups_encounters_and_is_repeatable(tmp_path):
    rows = [window(subject, f"stay-{i}-{stay}", target=[0]*(4-i)+[1]*(i+1)) for i, subject in enumerate(("subject-a", "subject-b", "subject-c")) for stay in range(2)]
    path = extract(tmp_path, rows)
    data, again = E.load_extract(path, "mortality"), E.load_extract(path, "mortality")
    assert {tuple(s[2].tolist()) for s in data.train}.isdisjoint({tuple(s[2].tolist()) for s in data.validation})
    assert len(data.train) == 4 and len(data.validation) == 2
    assert data.metadata["split_unit"] == "subject" and data.metadata["clinical_validation"] == "not_assessed"
    assert all(torch.equal(a[0], b[0]) for a, b in zip(data.train, again.train))
    assert "subject-a" not in json.dumps(data.metadata) and str(path) not in json.dumps(data.metadata)

def test_normalization_is_training_only(tmp_path):
    data = E.load_extract(extract(tmp_path, [window("subject-a", hr=1000), window("subject-b", hr=100)]), "mortality")
    assert data.feature_mean[0] == 100 and data.feature_std[0] == pytest.approx(.001)
    assert torch.all(data.train[0][0][:, 0] == 0) and torch.all(data.validation[0][0][:, 0] > 100000)

@pytest.mark.parametrize("rows", [[window()], [window(), window()], [window("subject-a", "same-stay"), window("subject-b", "same-stay")], [window("subject-a"), {**window("subject-b"), "label_definition": "different-endpoint"}]])
def test_reject_insufficient_subjects_duplicates_and_mixed_definitions(tmp_path, rows):
    with pytest.raises(E.ExtractError): E.load_extract(extract(tmp_path, rows), "mortality")

def test_equal_length_batches_cover_all_windows_without_padding():
    parsed = [E.parse_window(window(length=length), "mortality") for length in (3, 4, 3, 5, 4, 3)]
    samples = [(w.features, w.dt, w.labels) for w in parsed]
    sampler = E.EqualLengthBatchSampler(samples, 2, shuffle=True)
    batches = list(sampler)
    assert sorted(i for batch in batches for i in batch) == list(range(6)) and len(batches) == len(sampler) == 4
    for indices in batches:
        chosen = [samples[i] for i in indices]
        feats, dt, labels = E.collate_equal_length(chosen)
        assert feats.shape[1] == len(chosen[0][0]) and dt.shape[:2] == feats.shape[:2] and labels.shape[1] == 5
    with pytest.raises(E.ExtractError, match="cannot be padded"): E.collate_equal_length([samples[0], samples[1]])

def test_errors_omit_paths_and_payload(tmp_path):
    path = tmp_path / "private-source-identity.jsonl"
    with pytest.raises(E.ExtractError) as exc: E.load_extract(path, "mortality")
    assert str(path) not in str(exc.value)
    path.write_text('{"unexpected":"SENSITIVE_SYNTHETIC_MARKER"}\n')
    with pytest.raises(E.ExtractError) as exc: E.load_extract(path, "mortality")
    assert "SENSITIVE_SYNTHETIC_MARKER" not in str(exc.value) and str(path) not in str(exc.value)
    path.write_text('invalid SENSITIVE_SYNTHETIC_MARKER\n')
    with pytest.raises(E.ExtractError, match="Line 1: invalid JSON"): E.load_extract(path, "mortality")

def test_cli_validation_has_no_training_or_writes(tmp_path, monkeypatch, capsys):
    path = extract(tmp_path, [window("subject-a"), window("subject-b")])
    def forbidden(*args, **kwargs): pytest.fail("validation attempted training or checkpoint writing")
    monkeypatch.setattr(T, "train", forbidden); monkeypatch.setattr(torch, "save", forbidden)
    before = set(tmp_path.iterdir())
    T.main(["--outcome", "mortality", "--labelled-extract", str(path), "--validate-only"])
    assert set(tmp_path.iterdir()) == before and "subject-separated" in capsys.readouterr().out

def test_nonsepsis_requires_explicit_data_and_new_research_output(tmp_path):
    with pytest.raises(E.ExtractError, match="Supply --labelled-extract"): T.load_dataset_for("mortality")
    with pytest.raises(E.ExtractError, match="explicit --output"): T.train("mortality")
    with pytest.raises(E.ExtractError, match="runtime models"): T.train("mortality", output=T.SERVER_DIR / "models" / "never-write.pt")
    existing = tmp_path / "existing.pt"; existing.write_bytes(b"synthetic sentinel")
    with pytest.raises(E.ExtractError, match="already exists"): T.train("mortality", output=existing)
    assert existing.read_bytes() == b"synthetic sentinel"


def test_reject_duplicate_json_fields_and_oversized_line_without_echo(tmp_path):
    path = tmp_path / "synthetic-rejected.jsonl"
    path.write_text('{"private-field":1,"private-field":2}\n')
    with pytest.raises(E.ExtractError, match="duplicate fields") as exc:
        E.load_extract(path, "mortality")
    assert "private-field" not in str(exc.value)
    path.write_text('x' * 65537 + '\n')
    with pytest.raises(E.ExtractError, match="exceeds 64 KiB"):
        E.load_extract(path, "mortality")
