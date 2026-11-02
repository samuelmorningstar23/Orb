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


_MEDICATION_CHANGE = (
    r"\b(?:(?:medications?|meds|drugs?|prescriptions?)[\s\-‐‑]+"
    r"(?:changes?|adjustments?|modifications?|updates?)"
    r"|(?:changes?|adjustments?|modifications?|updates?)\s+(?:to\s+)?"
    r"(?:the\s+)?(?:medications?|meds|drugs?|prescriptions?))\b"
)
_TASKS = r"\b(?:(?:outstanding|unresolved|pending|open)\s+(?:tasks?|work|actions?)|tasks?|work items?)\b"


def _clauses(text: str) -> list[str]:
    # A heading followed by its status remains one observation. No unrelated
    # sentence can donate a missing-data marker to a different requested field.
    text = re.sub(r":\s*\n\s*", ": ", text)
    return re.split(r"[.!?;\n]+", text)


def _documented_gap(text: str, term: str) -> bool:
    """Missing source information, not a claim that no changes/tasks exist."""
    missing_source = (
        r"\b(?:unknown|undocumented|unavailable|missing)\b"
        r"|\b(?:not|none|no|without)\b.{0,100}\b"
        r"(?:documented|recorded|available|provided|supplied|listed|"
        r"information|data|details|records?|documentation)\b"
    )
    return any(re.search(term, clause) and re.search(missing_source, clause)
               for clause in _clauses(text))


def _unsupported_task_clearance(text: str) -> bool:
    for clause in _clauses(text):
        if not re.search(_TASKS, clause):
            continue
        clearance = re.search(
            r"\bno\s+(?:(?:outstanding|unresolved|pending|open)\s+)?tasks?\b"
            r"|\btasks?\b.{0,30}\b(?:all completed|all resolved|none remain|nothing remains)\b",
            clause,
        )
        inference_negated = re.search(
            r"\b(?:does not|doesn.t|cannot|can.t)\s+"
            r"(?:establish|mean|indicate|confirm|prove|infer|imply)\b", clause,
        )
        if clearance and not inference_negated and not _documented_gap(clause, _TASKS):
            return True
    return False


def patient_summary_checks(answer: str, expected: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"[*_`#]", "", answer).casefold()
    checks = {
        "patient_identity_preserved": str(expected["name"]).casefold() in text,
        "documented_diagnosis_present": str(expected["diagnosis"]).casefold() in text,
        "missing_observations_explicit": _gap(text, r"\b(?:vitals?|observations?)\b"),
        "missing_active_medications_explicit": _gap(text, r"\b(?:medications?|meds|drugs?)\b"),
        "missing_medication_change_history_explicit": _documented_gap(text, _MEDICATION_CHANGE),
        "missing_labs_explicit": _gap(text, r"\b(?:labs?|laborator\w*|blood results?)\b"),
        "missing_unresolved_tasks_explicit": _documented_gap(text, _TASKS),
        "no_unsupported_task_clearance": not _unsupported_task_clearance(text),
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
