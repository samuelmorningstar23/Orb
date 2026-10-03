"""MEASURE the two tiers against the real model. Manual — not collected by pytest.

    server/.venv/bin/python tests/manual/assessment_tier_latency.py [--runs 3]

WHY A HARNESS AND NOT A TEST. The numbers that decide whether this feature is
usable are wall-clock numbers on THIS appliance with THIS model resident, and a
unit test with a fake provider can only ever prove ordering. The verdict that
forced the tier split ("useless") came from a measurement like this one, so the
replacement has to be defended the same way.

WHAT IT MEASURES, per run, warm (model already resident):
  * TIER 1   — the deterministic driver/trend block, end to end, no model.
  * TIER 2   — the alert lane: prompt build → first token → FIRST COMPLETE STEP
               → done. `first_step_ms` is the honest "first words" figure; a
               token nobody can read is not a first word.
  * TIER 3   — the four-section draft, as the control the verdict was passed on.

It builds the REAL prompts through the service's own builders, so what is timed
is what ships. It talks to Ollama directly rather than through llm_queue: the
queue's contribution is scheduling, which is measured separately by the pilot's
own instrumentation, and mixing it in here would hide the model's own numbers.

IT DOES NOT WRITE TO THE DATABASE and it does not go near the running server's
DB — point ORB_DB_PATH at a scratch file, or let it default to one.
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.request

# A SCRATCH DB AND THE DEV PROFILE, both set before config is imported.
# Nothing here reads or writes a real chart — the observation series is built in
# an in-memory SQLite — but `config` refuses to import under the prod profile
# without a JWT secret, and pointing ORB_DB_PATH at a scratch file is the same
# isolation the test harness uses. Never run this with ORB_DEPLOYMENT_PROFILE
# unset against the dev database: booting the dev DB under the prod profile
# deactivates every seeded login.
os.environ.setdefault(
    "ORB_DB_PATH",
    os.path.join(tempfile.gettempdir(), "orb_tier_latency.db"))
os.environ.setdefault("ORB_DEPLOYMENT_PROFILE", "dev")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from config import LLM_MODEL, OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE  # noqa: E402
from services import sage_assessment_service as sas  # noqa: E402

#: A peri-arrest chart of the size the ward actually produces. Written out
#: rather than read from a fixture so this harness has no DB dependency at all
#: and the prompt is identical from run to run (Ollama's cache reuse is
#: prefix-anchored — a prompt that differs by one character is a different
#: prefix and buys nothing).
CHART = """═══ CHANNEL A: PATIENT FACTS ═══
DEMOGRAPHICS: 71-year-old female, Bed A-1, Ward AS-1, Community acquired pneumonia, day 3 of admission
VITALS (latest): HR 138, BP 78/44, RR 32, SpO2 86% on 4L O2, Temp 39.4°C, ACVPU voice
TRENDS: deteriorating (HR +18, SBP -22, SpO2 -6 over 6h)
MEDICATIONS: Amoxicillin 1 g TDS PO, Paracetamol 1 g QDS PO, Enoxaparin 40 mg OD SC
RECENT LABS: WCC 21.4 (H) | CRP 288 (H) | Lactate 4.1 (H) | Creatinine 168 (H) | Platelets 88 (L)
SCRIBE NOTE: Increasingly breathless overnight, productive cough, new confusion this morning per nursing staff. Not tolerating oral intake.
IMAGING: CXR — dense right lower zone consolidation, new small parapneumonic effusion.
MISSING DATA: no blood cultures on file, no urine output charted since 06:00"""

GUIDELINES = """(DOC: LOC-SEPSIS-001) Sepsis Six — local policy
Administer broad-spectrum antibiotics within one hour of recognising sepsis. Take blood cultures before antibiotics where this does not delay them. Measure serum lactate; a lactate above 4 mmol/L indicates septic shock and mandates immediate senior review. Give a 500 mL crystalloid fluid challenge over 15 minutes in hypotension, and monitor urine output hourly.
(DOC: EXT-RESP-004) Acute oxygen therapy in the acutely unwell adult
Target saturations of 94-98% unless the patient is at risk of hypercapnic respiratory failure, in which case target 88-92%. Escalate the delivery device rather than the flow rate alone when targets are not met."""


def _prep():
    """A `prep` dict of the shape `_open_assessment` returns, with a real Tier 1.

    Tier 1 is computed by the SHIPPING function over the observation series
    below, so the block that goes into the prompt — and the timing recorded for
    it — is the production one, not a hand-written stand-in.
    """
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT,
                    timestamp TEXT, heart_rate REAL, systolic_bp REAL, diastolic_bp REAL,
                    spo2 REAL, temperature REAL, respiratory_rate REAL,
                    consciousness TEXT, on_oxygen INTEGER)""")
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, on_scale_2 INTEGER)")
    conn.execute("INSERT INTO patients VALUES ('P-LAT', 0)")
    series = [(120, 100, 92, 38.9, 26), (126, 92, 90, 39.1, 28),
              (132, 84, 88, 39.4, 30), (138, 78, 86, 39.4, 32)]
    for i, (hr, sbp, spo2, temp, rr) in enumerate(series):
        conn.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, on_oxygen) "
            "VALUES ('P-LAT', ?, ?, ?, 44, ?, ?, ?, 'voice', 1)",
            (f"2026-08-14T0{i}:00:00+00:00", hr, sbp, spo2, temp, rr))

    t0 = time.monotonic()
    tier1 = sas._compute_tier1(conn, "P-LAT")
    tier1_ms = (time.monotonic() - t0) * 1000
    conn.close()
    return {
        "patient_id": "P-LAT", "channel_id": None, "message_id": None,
        "lane": "manual", "depth": sas.DEPTH_BRIEF,
        "news2": tier1.get("news2"), "news2_level": "critical",
        "news2_drivers": ["RR 32", "SpO2 86%", "SBP 78"],
        "news2_line": None, "tier1": tier1, "chart_block": CHART,
        "trend_lines": [f"2026-08-14T0{i}:00:00: HR {hr} · BP {sbp} · RR {rr} · "
                        f"SpO2 {spo2}% · Temp {temp}°C · ACVPU voice on O2"
                        for i, (hr, sbp, spo2, temp, rr) in enumerate(series)],
        "facts": {}, "conversation_id": "assess-latency",
    }, tier1_ms


SOURCES = [{"document_id": "LOC-SEPSIS-001", "trust_tier": 1},
           {"document_id": "EXT-RESP-004", "trust_tier": 3}]


def _run(system: str, user: str, num_predict: int, *, step_terminated: bool):
    """One real generation. Returns (ttft_s, first_step_s, total_s, counters).

    `first_step_s` is the moment a COMPLETE line existed — the first thing a
    clinician could read and act on — which is the figure the fast lane is
    designed around. On the four-section tier there is no such moment before the
    end, and that is the point.
    """
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": True,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.2, "num_predict": num_predict,
                    "num_ctx": sas._num_ctx(), "repeat_penalty": 1.1},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    ttft = first_step = None
    buf, out, counters = "", [], {}
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            if not line.strip():
                continue
            data = json.loads(line)
            chunk = (data.get("message") or {}).get("content")
            if chunk:
                if ttft is None:
                    ttft = time.monotonic() - t0
                out.append(chunk)
                if step_terminated and first_step is None:
                    # THE REAL PARSER, so this measures what a clinician can
                    # actually read. The 27B reliably echoes the "STEP | WHY |
                    # ACTION | TARGET" header line first; production rejects it,
                    # and a harness that counted it would flatter the number by
                    # the ~8 tokens the header costs.
                    buf += chunk
                    while "\n" in buf and first_step is None:
                        line, buf = buf.split("\n", 1)
                        if sas._parse_step_line(line, 0, lambda t: t, None):
                            first_step = time.monotonic() - t0
            if data.get("done"):
                counters = {
                    "prompt_tokens": data.get("prompt_eval_count"),
                    "prefill_s": (data.get("prompt_eval_duration") or 0) / 1e9,
                    "output_tokens": data.get("eval_count"),
                    "decode_s": (data.get("eval_duration") or 0) / 1e9,
                }
                break
    return ttft, first_step, time.monotonic() - t0, counters, "".join(out)


def _fmt(label, values):
    if not values:
        return f"  {label:<22} —"
    return (f"  {label:<22} median {statistics.median(values):6.2f}s   "
            f"min {min(values):6.2f}s   max {max(values):6.2f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--skip-full", action="store_true",
                    help="skip the Tier 3 control (each run costs ~45 s)")
    args = ap.parse_args()

    prep, tier1_ms = _prep()
    brief_user = sas._build_prompt_brief(prep, GUIDELINES, SOURCES)
    full_user = sas._build_prompt(prep, GUIDELINES, SOURCES)

    print(f"model={LLM_MODEL}  num_ctx={sas._num_ctx()}  "
          f"cap={sas._max_advice_tokens()} tok  max_steps={sas._max_steps()}")
    print(f"TIER 1 (deterministic, no model): {tier1_ms:.1f} ms  "
          f"NEWS2 {prep['tier1']['news2']}, {len(prep['tier1']['parameters'])} drivers")
    print(f"prompt chars: brief {len(brief_user):,} vs full {len(full_user):,}\n")

    rows = {"brief": {"ttft": [], "first": [], "total": [], "model": []},
            "full": {"ttft": [], "first": [], "total": [], "model": []}}
    for i in range(args.runs):
        ttft, first, total, c, text = _run(sas._SYSTEM_BRIEF, brief_user,
                                           sas._max_advice_tokens(), step_terminated=True)
        rows["brief"]["ttft"].append(ttft)
        rows["brief"]["total"].append(total)
        rows["brief"]["model"].append(c["prefill_s"] + c["decode_s"])
        if first:
            rows["brief"]["first"].append(first)
        # THE THREE PARTS OF A WALL-CLOCK NUMBER, separated on purpose. `queue`
        # is time this appliance spent serving somebody ELSE before it started
        # on us; prefill and decode are the model's own work on this prompt.
        # Reporting only the total would blame the design for the neighbour's
        # load, and reporting only the model's counters would hide the wait a
        # clinician actually experiences.
        queue = (ttft or 0) - c["prefill_s"]
        print(f"[brief {i+1}] ttft={ttft:.2f}s first_step={first and round(first, 2)}s "
              f"total={total:.2f}s  queue≈{queue:.2f}s "
              f"prompt={c['prompt_tokens']}tok/{c['prefill_s']:.2f}s "
              f"({(c['prompt_tokens'] or 0)/(c['prefill_s'] or 1):.0f} tok/s) "
              f"out={c['output_tokens']}tok/{c['decode_s']:.2f}s "
              f"({(c['output_tokens'] or 0)/(c['decode_s'] or 1):.1f} tok/s)")
        print("        " + text.strip().replace("\n", "\n        ")[:400])

        if not args.skip_full:
            ttft, _f, total, c, _t = _run(sas._SYSTEM, full_user, 900,
                                          step_terminated=False)
            rows["full"]["ttft"].append(ttft)
            rows["full"]["total"].append(total)
            rows["full"]["model"].append(c["prefill_s"] + c["decode_s"])
            print(f"[full  {i+1}] ttft={ttft:.2f}s total={total:.2f}s  "
                  f"prompt={c['prompt_tokens']}tok/{c['prefill_s']:.2f}s "
                  f"out={c['output_tokens']}tok/{c['decode_s']:.2f}s")

    print("\nTIER 2 (alert lane)")
    print(_fmt("first token", rows["brief"]["ttft"]))
    print(_fmt("first COMPLETE step", rows["brief"]["first"]))
    print(_fmt("complete", rows["brief"]["total"]))
    print(_fmt("model time only", rows["brief"]["model"]))
    if rows["full"]["total"]:
        print("\nTIER 3 (handover control)")
        print(_fmt("first token", rows["full"]["ttft"]))
        print(_fmt("complete", rows["full"]["total"]))
        print(_fmt("model time only", rows["full"]["model"]))


if __name__ == "__main__":
    main()
