"""Narrow task-correctness regressions for the disposable empty-chart fixture.

This never calls a model or replaces an answer. It keeps transport completion
separate from a useful summary and does not award clinical validation. Expected
facts come from the owned fixture; no real patient text belongs in this module.
"""
from __future__ import annotations

import re
from typing import Any


def _gap(text: str, term: str) -> bool:
    missing = r"\b(?:no|not|missing|absent|unavailable|unknown|undocumented|without)\b"
    return any(re.search(term, sentence) and re.search(missing, sentence)
               for sentence in re.split(r"[.!?;\n]+", text))


def patient_summary_checks(answer: str, expected: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"[*_`#]", "", answer).casefold()
    checks = {
        "patient_identity_preserved": str(expected["name"]).casefold() in text,
        "documented_diagnosis_present": str(expected["diagnosis"]).casefold() in text,
        "missing_observations_explicit": _gap(text, r"\b(?:vitals?|observations?)\b"),
        "missing_active_medications_explicit": _gap(text, r"\b(?:medications?|meds|drugs?)\b"),
        "missing_labs_explicit": _gap(text, r"\b(?:labs?|laborator\w*|blood results?)\b"),
        "no_chart_update_refusal": not re.search(
            r"understood your request to update|direct modifications|no chart change has been made|"
            r"(?:cannot|can.t|not able to|not supported).{0,65}(?:modify|update the chart|add medications)", text),
        "no_action_declaration": not re.search(r"(?:^|\n)\s*act\s*\||<action(?:\s|>)", text),
        "no_other_fixture_patient": all(name.casefold() == str(expected["name"]).casefold()
            for name in re.findall(r"Xqz[A-Z]{2} Vyx[A-Z]", answer)),
        "no_unsupported_stability_assertion": not re.search(
            r"\b(?:patient is|patient remains|patient appears|clinically|currently|condition is)\s+(?:clinically\s+)?stable\b", text),
    }
    return {"passed": all(checks.values()), "checks": {k: bool(v) for k, v in checks.items()},
            "scope": "Empty-chart summary task regression; not clinical validation"}


def apply_workflow_checks(rows: list[dict[str, Any]], plan: list[dict[str, Any]],
                          expectations: dict[str, Any]) -> dict[str, Any]:
    """Reject a completed patient reply that fails the known summary contract.

    Original transport result and timings stay recorded. Incomplete responses
    cannot pass by containing a few correct words before timing out.
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
        if not isinstance(expected, dict):
            raise ValueError("Owned fixture summary expectations are absent")
        if not row["success"]:
            row["workflow_quality"] = {"passed": False, "reason": "transport_incomplete_or_failed"}
            continue
        completed += 1
        check = patient_summary_checks(row.get("synthetic_answer", ""), expected)
        row["workflow_quality"] = check
        if check["passed"]:
            passed += 1
        else:
            row["success"], row["error"] = False, "summary_quality_failed"
            row["completed_within_target"] = row["first_readable_within_target"] = False
    return {"required": True, "patient_requests": attempted, "transport_completed": completed,
            "task_checks_passed": passed, "all_patient_requests_passed": attempted > 0 and passed == attempted,
            "clinical_accuracy_qualified": False}
