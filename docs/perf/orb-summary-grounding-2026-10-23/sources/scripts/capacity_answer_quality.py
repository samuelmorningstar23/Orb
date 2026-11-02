"""Source-contract checks for owned synthetic chart summaries.

An answer mentioning the right missing-data words is not evidence of grounding.
The fixture captures authorized source evidence independently of the response;
this checker validates the model's complete fact selection and re-renders that
trusted evidence. No arbitrary model prose, even plausible prose, can pass.
This engineering contract does not award clinical validation.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any


@lru_cache(maxsize=1)
def _grounding_module() -> ModuleType:
    """Load only the pure source-contract module, never app/DB configuration."""
    path = Path(__file__).resolve().parents[1] / "server" / "services" / "summary_grounding.py"
    name = "_orb_capacity_source_grounding"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Owned fixture grounding verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their module during construction.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def patient_summary_checks(answer: str, expected: dict[str, Any],
                           grounding: object = None, *, proof_count: int = 1,
                           patient_id: str | None = None) -> dict[str, Any]:
    """Verify exact extractive output against independently captured evidence.

    The response's fingerprint/claims are never treated as the expected source.
    The expected bundle must originate in the disposable fixture before the AI
    call. Selection order can vary; source text, gaps, scope and provenance
    cannot. A malformed, omitted, duplicated or foreign fact fails closed.
    """
    module = _grounding_module()
    checks = {
        "trusted_source_evidence_present": False,
        "patient_scope_matches": False,
        "single_grounding_proof": type(proof_count) is int and proof_count == 1,
        "grounding_proof_present": isinstance(grounding, dict),
        "contract_version_matches": False,
        "source_fingerprint_matches": False,
        "all_source_facts_selected_once": False,
        "source_references_match": False,
        "extractive_only_declared": False,
        "clinical_validation_not_claimed": False,
        "grounding_metadata_matches": False,
        "exact_source_rendering": False,
        "unknowns_and_scope_preserved": False,
    }
    try:
        bundle = module.bundle_from_dict(expected.get("grounding_bundle"))
        checks["trusted_source_evidence_present"] = True
        checks["patient_scope_matches"] = patient_id is None or bundle.patient_id == patient_id
    except (ValueError, TypeError):
        bundle = None
    if bundle is not None and isinstance(grounding, dict):
        checks["contract_version_matches"] = grounding.get("contract") == module.CONTRACT_VERSION
        checks["extractive_only_declared"] = grounding.get("extractive_only") is True
        checks["clinical_validation_not_claimed"] = grounding.get("clinical_validation") is False
        try:
            selection = module.validate_selection(
                _canonical({"fact_ids": grounding.get("selected_fact_ids")}), bundle)
            checks["all_source_facts_selected_once"] = True
            expected_proof = module.grounding_metadata(bundle, selection)
            checks["source_fingerprint_matches"] = (
                grounding.get("source_fingerprint") == expected_proof["source_fingerprint"])
            checks["source_references_match"] = (
                _canonical(grounding.get("source_references")) == _canonical(expected_proof["source_references"]))
            # Exact JSON comparison also rejects unexpected keys and bool/int
            # substitutions, not just the handful of fields checked above.
            checks["grounding_metadata_matches"] = _canonical(grounding) == _canonical(expected_proof)
            rendered = module.render_sage(bundle, selection)
            allowed = {rendered}
            label = expected.get("answer_label")
            if isinstance(label, str) and label:
                # Sage's existing implicit-patient safety label is captured
                # from the scoped fixture DB too, never from model text.
                allowed.add(label + "\n\n" + rendered)
            checks["exact_source_rendering"] = isinstance(answer, str) and answer in allowed
            # Unknowns are rendered by the server from the trusted source
            # contract. Substring searches cannot prove their meaning/scope.
            checks["unknowns_and_scope_preserved"] = checks["exact_source_rendering"]
        except (ValueError, TypeError):
            pass
    return {"passed": all(checks.values()), "checks": checks,
            "scope": "Exact source-facts-v1 rendering/provenance regression; not clinical validation"}


def apply_workflow_checks(rows: list[dict[str, Any]], plan: list[dict[str, Any]],
                          expectations: dict[str, Any]) -> dict[str, Any]:
    """Reject completed patient replies lacking a verified source contract.

    Original transport results/timings stay recorded. Partial or failed replies
    cannot pass even when their content and metadata happen to be correct.
    """
    by_id = {item["request_id"]: item for item in plan}
    attempted = completed = passed = 0
    for row in rows:
        if row["lane"] != "patient":
            continue
        attempted += 1
        row["transport_success"] = bool(row["success"])
        item = by_id[row["request_id"]]
        expected = expectations.get(item["patient_id"])
        if not isinstance(expected, dict) or not isinstance(expected.get("grounding_bundle"), dict):
            raise ValueError("Owned fixture summary grounding expectations are absent")
        if not row["success"]:
            row["workflow_quality"] = {"passed": False, "reason": "transport_incomplete_or_failed"}
            continue
        completed += 1
        check = patient_summary_checks(row.get("synthetic_answer", ""), expected,
            row.get("summary_grounding"), proof_count=row.get("summary_grounding_proof_count", 0),
            patient_id=item["patient_id"])
        row["workflow_quality"] = check
        if check["passed"]:
            passed += 1
        else:
            row["success"], row["error"] = False, "summary_quality_failed"
            row["completed_within_target"] = row["first_readable_within_target"] = False
    return {"required": True, "contract": "source-facts-v1", "patient_requests": attempted,
            "transport_completed": completed, "task_checks_passed": passed,
            "all_patient_requests_passed": attempted > 0 and passed == attempted,
            "clinical_accuracy_qualified": False}
