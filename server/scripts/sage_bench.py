"""Sage latency + grounding benchmark.

Drives the real SSE endpoint (/api/sage/query) against a running Orb instance
and records, per question:

    stage_retrieving_ms  — time to the 'retrieving' stage event
    retrieval_ms         — server-reported retrieval cost
    ttft_ms              — first RAW model token (server-timed)
    first_words_ms       — first VISIBLE text reaching the client (what the
                           clinician actually experiences — the roadmap's
                           "first words ≤5s warm" bar)
    total_ms             — stream complete
    tokens, answer text, sources, confidence, downgraded

Two suites:
  --suite general  20 questions of escalating complexity (definition → recall →
                   application → multi-step reasoning → guideline synthesis)
  --suite patient  20 patient-scoped queries whose answers are checkable
                   against the DB; emits ground truth alongside each answer so
                   a judge pass can classify hallucination.

Usage (server already running):
    .venv/bin/python scripts/sage_bench.py --suite general --out docs/perf/sage-general.json
    .venv/bin/python scripts/sage_bench.py --suite patient --out docs/perf/sage-patient.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SERVER_DIR = Path(__file__).resolve().parent.parent

# ── 20 general questions, strictly escalating complexity ──
GENERAL_QUESTIONS = [
    # Tier 1 — definition / recall (1-5)
    "What does NEWS2 stand for?",
    "What is the normal adult respiratory rate?",
    "What are the CURB-65 criteria?",
    "Define septic shock.",
    "What is the qSOFA score?",
    # Tier 2 — protocol recall (6-10)
    "What is in the sepsis 1-hour bundle?",
    "What is the door-to-balloon time target for STEMI?",
    "What are the Berlin criteria for ARDS?",
    "What is the first-line vasopressor in septic shock?",
    "What are the diagnostic criteria for DKA?",
    # Tier 3 — applied dosing / calculation (11-15)
    "How do you dose IV alteplase for acute ischemic stroke?",
    "How is potassium replaced in severe hypokalemia?",
    "What is the insulin infusion rate for DKA and when do you add dextrose?",
    "What antibiotics would you use empirically for sepsis of unknown origin here?",
    "How do you calculate tidal volume for lung-protective ventilation in a 70 kg male?",
    # Tier 4 — multi-step clinical reasoning (16-18)
    "A patient with COPD has SpO2 88% on room air. How does that change NEWS2 scoring and what do you do?",
    "When would you add hydrocortisone in septic shock, and what is the evidence threshold?",
    "A patient on warfarin needs urgent surgery with INR 3.5: how do you reverse it?",
    # Tier 5 — synthesis across guidelines + local policy (19-20)
    "Compare our local empiric sepsis antibiotic policy against the Surviving Sepsis Campaign recommendation and explain any difference.",
    "For a septic patient with AKI and a penicillin allergy, walk through antibiotic selection, fluid strategy, and the monitoring plan.",
]

# ── 20 patient-scoped query TEMPLATES. {name}/{pid} filled from the live DB. ──
# 'field' names the ground-truth key the judge should check against.
PATIENT_TEMPLATES = [
    ("What is {name}'s current diagnosis?", "diagnosis"),
    ("What bed is {name} in?", "bed"),
    ("How old is {name}?", "age"),
    ("What is {name}'s latest heart rate?", "latest_hr"),
    ("What is {name}'s latest blood pressure?", "latest_bp"),
    ("What is {name}'s latest SpO2?", "latest_spo2"),
    ("What is {name}'s current NEWS2 score?", "news2"),
    ("What is {name}'s risk level right now?", "risk_level"),
    ("What medications is {name} on?", "medications"),
    ("Does {name} have any allergies documented?", "allergies"),
    ("Summarize {name}'s vitals trend over the last few hours.", "vitals_trend"),
    ("Is {name} deteriorating or improving?", "risk_level"),
    ("What is {name}'s temperature and is it abnormal?", "latest_temp"),
    ("What is {name}'s respiratory rate?", "latest_rr"),
    ("Which ward is {name} admitted to?", "ward"),
    ("Has {name} had any critical alerts?", "risk_level"),
    (
        "Given {name}'s diagnosis and current vitals, what should I watch for overnight?",
        "diagnosis",
    ),
    (
        "What antibiotics, if any, is {name} receiving, and are they appropriate for the diagnosis?",
        "medications",
    ),
    ("Based on {name}'s NEWS2 and trajectory, does this patient need escalation now?", "news2"),
    ("Give me a handover summary for {name}.", "diagnosis"),
]


def fetch_patient_ground_truth(client: httpx.Client, patient_id: str) -> dict:
    """Pull the checkable facts straight from the API (same source the model
    is given) so hallucination judging compares like with like."""
    r = client.get(f"/api/vigil/patients/{patient_id}", timeout=60)
    r.raise_for_status()
    d = r.json()
    p = d.get("patient", {})
    vitals = d.get("vitals", [])
    latest = vitals[0] if vitals else {}
    meds = [
        f"{m.get('drug_name')} {m.get('dosage') or ''}".strip()
        for m in d.get("medications", [])
        if m.get("status") == "active"
    ]
    risk = d.get("risk", {}) or {}
    return {
        "patient_id": patient_id,
        "name": p.get("name"),
        "age": p.get("age"),
        "sex": p.get("sex"),
        "bed": p.get("bed"),
        "ward": p.get("ward"),
        "diagnosis": p.get("diagnosis"),
        "latest_hr": latest.get("heart_rate"),
        "latest_bp": (
            f"{latest.get('systolic_bp')}/{latest.get('diastolic_bp')}"
            if latest.get("systolic_bp")
            else None
        ),
        "latest_spo2": latest.get("spo2"),
        "latest_temp": latest.get("temperature"),
        "latest_rr": latest.get("respiratory_rate"),
        "news2": risk.get("raw_news2", risk.get("score")),
        "risk_level": risk.get("level"),
        "medications": meds,
        "allergies": p.get("allergies"),
        "vitals_trend": d.get("trend"),
    }


def ask(client: httpx.Client, question: str, patient_id: str | None) -> dict:
    """One SSE turn. Returns per-stage timings + the assembled answer."""
    body = {"query": question}
    if patient_id:
        body["patient_id"] = patient_id

    t0 = time.perf_counter()
    rec: dict[str, Any] = {
        "question": question,
        "patient_id": patient_id,
        "stage_retrieving_ms": None,
        "retrieval_ms": None,
        "ttft_ms": None,
        "first_words_ms": None,
        "total_ms": None,
        "answer": "",
        "sources": [],
        "confidence": None,
        "downgraded": None,
        "error": None,
    }
    try:
        with client.stream("POST", "/api/sage/query", json=body, timeout=600) as r:
            if r.status_code != 200:
                rec["error"] = f"HTTP {r.status_code}"
                return rec
            for line in r.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                now = (time.perf_counter() - t0) * 1000
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if evt.get("stage") == "retrieving" and rec["stage_retrieving_ms"] is None:
                    rec["stage_retrieving_ms"] = round(now, 1)
                if evt.get("stage") == "generating":
                    rec["retrieval_ms"] = evt.get("retrieval_ms")
                if "content" in evt:
                    if rec["first_words_ms"] is None:
                        rec["first_words_ms"] = round(now, 1)
                    rec["answer"] += evt["content"]
                if evt.get("error"):
                    rec["error"] = evt["error"]
                if evt.get("done"):
                    rec["total_ms"] = round(now, 1)
                    t = evt.get("timing") or {}
                    rec["ttft_ms"] = t.get("ttft_ms", rec["ttft_ms"])
                    if evt.get("answer") and not rec["answer"]:
                        rec["answer"] = evt["answer"]
                    rec["sources"] = evt.get("sources") or evt.get("cited_sources") or []
                    rec["confidence"] = evt.get("confidence")
                    rec["downgraded"] = (evt.get("route") or {}).get("downgraded")
                    break
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
    if rec["total_ms"] is None:
        rec["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return rec


def summarize(records: list[dict]) -> dict:
    def stat(key):
        vals = [r[key] for r in records if r.get(key) is not None]
        if not vals:
            return None
        vals.sort()
        return {
            "n": len(vals),
            "min": round(vals[0], 1),
            "p50": round(statistics.median(vals), 1),
            "p95": round(vals[min(len(vals) - 1, int(len(vals) * 0.95))], 1),
            "max": round(vals[-1], 1),
            "mean": round(statistics.fmean(vals), 1),
        }

    return {
        "count": len(records),
        "errors": sum(1 for r in records if r.get("error")),
        "first_words_ms": stat("first_words_ms"),
        "ttft_ms": stat("ttft_ms"),
        "retrieval_ms": stat("retrieval_ms"),
        "total_ms": stat("total_ms"),
        "under_5s_first_words": sum(
            1
            for r in records
            if r.get("first_words_ms") is not None and r["first_words_ms"] <= 5000
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["general", "patient"], required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="DOC001")
    ap.add_argument("--password", default="password")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=0, help="only first N questions")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.base, timeout=60, trust_env=False)
    r = client.post("/api/auth/login", json={"employee_id": args.user, "password": args.password})
    r.raise_for_status()
    tok = r.json().get("access_token") or r.json().get("token")
    client.headers["Authorization"] = f"Bearer {tok}"

    records = []
    if args.suite == "general":
        qs = GENERAL_QUESTIONS[: args.limit] if args.limit else GENERAL_QUESTIONS
        for i, q in enumerate(qs, 1):
            print(f"[{i}/{len(qs)}] {q[:70]}…", flush=True)
            rec = ask(client, q, None)
            rec["tier"] = (i - 1) // 5 + 1
            records.append(rec)
            print(
                f"      first_words={rec['first_words_ms']}ms total={rec['total_ms']}ms "
                f"err={rec['error']}",
                flush=True,
            )
    else:
        pts = client.get("/api/vigil/patients?limit=100").json()
        assert pts, "no patients on the board"
        templates = PATIENT_TEMPLATES[: args.limit] if args.limit else PATIENT_TEMPLATES
        truth_cache: dict[str, dict] = {}
        for i, (tpl, field) in enumerate(templates, 1):
            p = pts[i % len(pts)]
            pid = p["id"]
            if pid not in truth_cache:
                truth_cache[pid] = fetch_patient_ground_truth(client, pid)
            truth = truth_cache[pid]
            q = tpl.format(name=truth["name"], pid=pid)
            print(f"[{i}/{len(templates)}] {q[:70]}…", flush=True)
            rec = ask(client, q, pid)
            rec["ground_truth"] = truth
            rec["checked_field"] = field
            records.append(rec)
            print(
                f"      first_words={rec['first_words_ms']}ms total={rec['total_ms']}ms "
                f"err={rec['error']}",
                flush=True,
            )

    out = {
        "suite": args.suite,
        "label": args.label,
        "run_at": datetime.now(UTC).isoformat(),
        "summary": summarize(records),
        "records": records,
    }
    path = (
        Path(args.out)
        if args.out
        else (
            SERVER_DIR.parent
            / "docs"
            / "perf"
            / f"sage-{args.suite}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.json"
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"\n→ {path}")
    print(json.dumps(out["summary"], indent=2))


if __name__ == "__main__":
    main()
