"""Sage Ambient — RETRACTION end-to-end harness (manual / live-model).

NOT a pytest test: it needs a running Orb server on :8000 and a resident
medgemma:4b. Posts a realistic ward thread through the REAL Relay API
(POST /api/relay/channels/{id}/messages) and reads back the REAL clinician
surface (GET /api/relay/channels/{id}/sage_memory).

Question under test: after a CRITICAL potassium of 6.8 is later retracted
(haemolysed sample, repeat 4.1), does the ambient room summary
  (i)  mention the retraction, and
  (ii) stop asserting 6.8 as a current value?

Isolation: each run gets a FRESH synthetic patient + case room (additive
inserts only — nothing existing is modified or deleted), so run N never
inherits run N-1's memory.

Usage:  .venv/bin/python tests/manual/retraction_harness.py [--runs 5]
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SERVER_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SERVER_DIR))

# orb.db is SQLCipher-encrypted; only the codec-aware helper applies PRAGMA key.
from database import _open_raw_connection, _row_factory_for  # noqa: E402

BASE = "http://127.0.0.1:8000"
DOC = ("DOC001", "password")   # Dr. Meera Sharma
NUR = ("NUR001", "password")   # Priya Reddy

# ── The ward thread ────────────────────────────────────────────────────
# 12 messages. #2 carries the CRITICAL value, #7 retracts it. The other 10
# are clinically detailed so the retraction is not trivially the only content.
# role: "doc" | "nur"
THREAD = [
    ("nur", "Handover from night shift: 58F, day 3 of community-acquired pneumonia "
            "with AKI. Overnight temp 38.4, HR 104, BP 112/68, RR 24, SpO2 92% on "
            "2L nasal prongs. Urine output only 320ml over the last 12 hours."),

    # ── CRITICAL VALUE ──
    ("doc", "Morning bloods are back and they are bad. Creatinine up to 2.4 from 1.6 "
            "yesterday, urea 92. Potassium 6.8 — that is critical, severe "
            "hyperkalaemia. Get an urgent ECG and hold the ramipril now."),

    ("nur", "ECG done at 07:52 — no peaked T waves, QRS narrow at 88ms, rate 101 "
            "sinus. Sending the trace to you now."),
    ("doc", "Given the potassium of 6.8 I want 10 units of soluble insulin in 50ml of "
            "50% dextrose, plus 10ml calcium gluconate IV over 5 minutes. Repeat a "
            "venous gas in 30 minutes."),
    ("nur", "Blood gas at 08:40 — pH 7.34, bicarbonate 19, lactate 2.1, base excess "
            "-5. Chest sounds coarse at the right base with bronchial breathing."),
    ("doc", "Chest X-ray shows dense right lower lobe consolidation, no effusion, no "
            "pneumothorax. Continue IV co-amoxiclav 1.2g three times daily, this is "
            "day 3 of a 7 day course."),

    # ── RETRACTION ──
    ("doc", "The lab just rang about the 07:00 sample — it was haemolysed, so ignore "
            "that potassium. The repeat is 4.1, completely normal. There is no "
            "hyperkalaemia. Please stop the insulin-dextrose."),

    ("nur", "Insulin-dextrose infusion stopped at 09:15. Capillary glucose 6.2, I will "
            "recheck hourly for the next four hours in case of rebound hypoglycaemia."),
    ("doc", "Renal team have reviewed. The AKI is pre-renal from sepsis and poor oral "
            "intake, not obstructive — ultrasound showed no hydronephrosis. Push "
            "fluids: 1 litre of Hartmann's over 4 hours then reassess."),
    ("nur", "Urine output has picked up, 180ml in the last two hours. She is more "
            "alert, taking sips of water and managed half a cup of tea."),
    ("doc", "Repeat creatinine and a full set of electrolytes this evening. If the "
            "creatinine is trending down and she stays afebrile overnight we can step "
            "her down to the general ward tomorrow."),
    ("nur", "Family updated at the bedside. Daughter asked about discharge planning and "
            "whether she will need home oxygen — I said the team would review tomorrow."),
]
CRITICAL_IDX = 1     # 0-based index of the message carrying K 6.8
RETRACTION_IDX = 6   # 0-based index of the retraction


# ── plumbing ───────────────────────────────────────────────────────────
def login(emp_id, pw):
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"employee_id": emp_id, "password": pw}, timeout=20)
    r.raise_for_status()
    return r.json()["token"]


def db_query(sql, params=()):
    conn = _open_raw_connection()
    conn.row_factory = _row_factory_for(conn)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def db_exec(sql, params=()):
    conn = _open_raw_connection()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def seed_alarm(cid, pid):
    """Pre-chart the CRITICAL potassium as a live risk flag + headline concern.

    Used by --seed-alarm, for when the ambient tier cannot be made resident (one
    GPU, another agent holding the 27B). It stands in for "the classifier
    correctly flagged the alarm", which is the EASY half and was never the
    defect — the alarming message always matched Gate 5 and always reached the
    model. What it leaves fully real is the half that failed: the retraction
    travelling through the live Relay route and BackgroundTask into memory.

    Shaped exactly as `_merge_risk` + `_update_memory` would write it.
    """
    now = datetime.now(timezone.utc).isoformat()
    flags = [{
        "detail": "Severe hyperkalaemia — potassium 6.8, requires urgent treatment",
        "rationale": "Potassium 6.8 with creatinine 2.4 in AKI",
        "confidence": 0.9,
        "created_at": now,
    }]
    db_exec(
        "INSERT OR REPLACE INTO sage_room_memory (room_id, patient_id, "
        "patient_summary, discussion_summary, current_concern, likely_next_steps, "
        "unresolved_questions, active_intents, risk_flags, participants, "
        "message_count, llm_call_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, pid, "", "Potassium 6.8 reported as critical; urgent ECG requested.",
         "Severe hyperkalaemia (K 6.8) in AKI", "[]", "[]", "[]",
         json.dumps(flags), "[]", 1, 1, now, now),
    )


def make_room(tag):
    """Additive-only: create a fresh synthetic patient + case room."""
    pid = f"PAT-RETR-{tag}"
    cid = f"chan_retr_{tag}"
    now = datetime.now(timezone.utc).isoformat()
    db_exec(
        "INSERT OR REPLACE INTO patients (id, name, mrn, bed, ward, admission_date, "
        "diagnosis, status, age, sex, attending_doctor_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, f"Retraction Testcase {tag}", f"MRN-RETR-{tag}", f"B-{tag}", "Ward 4",
         now, "Community-acquired pneumonia with acute kidney injury", "admitted",
         58, "F", "DOC001", now),
    )
    db_exec(
        "INSERT OR REPLACE INTO channels (id, name, type, patient_id, created_at) "
        "VALUES (?,?,?,?,?)",
        (cid, f"Case: Retraction Testcase {tag}", "case_room", pid, now),
    )
    # Guarantee a virgin ambient state for this room.
    db_exec("DELETE FROM sage_room_memory WHERE room_id = ?", (cid,))
    db_exec("DELETE FROM sage_observations WHERE room_id = ?", (cid,))
    return pid, cid


def post(cid, content, token):
    r = requests.post(f"{BASE}/api/relay/channels/{cid}/messages",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"content": content, "message_type": "text"}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def wait_processed(cid, msg_id, timeout=120):
    """Block until the ambient BackgroundTask has finished this message.

    `_update_memory` stamps last_processed_msg_id on every path that is not
    gate-rejected, so this is the true completion signal.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = db_query(
            "SELECT last_processed_msg_id, llm_call_count FROM sage_room_memory "
            "WHERE room_id = ?", (cid,))
        if rows and (rows[0]["last_processed_msg_id"] or 0) >= msg_id:
            return True, round(time.time() - t0, 1), rows[0]["llm_call_count"]
        time.sleep(0.4)
    return False, round(time.time() - t0, 1), None


def read_surface(cid, token):
    """THE REAL READ PATH the clinician UI uses."""
    r = requests.get(f"{BASE}/api/relay/channels/{cid}/sage_memory",
                     headers={"Authorization": f"Bearer {token}"}, timeout=20)
    r.raise_for_status()
    return r.json()


# ── scoring ────────────────────────────────────────────────────────────
_RETRACT_VOCAB = re.compile(
    r"retract|haemolys|hemolys|spurious|superseded?|corrected|amended|"
    r"disregard|no longer|not valid|repeat (?:is|was|of|potassium|k)|"
    r"invalid|withdrawn|false(?:ly)? elevated|artefact|artifact",
    re.I)
_HAS_41 = re.compile(r"\b4\.1\b")
_HAS_68 = re.compile(r"\b6\.8\b")
_HYPERK = re.compile(r"hyperkal", re.I)


def _blob(mem):
    """Everything the sage_memory surface shows a clinician, as one string."""
    parts = [
        mem.get("discussion_summary") or "",
        mem.get("current_concern") or "",
        json.dumps(mem.get("risk_flags") or []),
        json.dumps(mem.get("active_intents") or []),
        json.dumps(mem.get("unresolved_questions") or []),
        json.dumps(mem.get("likely_next_steps") or []),
    ]
    return "\n".join(parts)


# The sidebar (src/components/relay/SageAmbientSidebar.tsx:92) only renders a
# risk flag at confidence >= 0.80, so only those can mislead a clinician.
RISK_RENDER_THRESHOLD = 0.80


def score(mem):
    """Mechanical signals only. The final verdict is a human read of the text.

    Judged on the three fields the ambient sidebar actually paints
    (SageAmbientSidebar.tsx:106-120): current_concern, discussion_summary and
    risk_flags above the render threshold.
    """
    if not mem:
        return {"mentions_retraction": False, "asserts_retracted_as_current": True,
                "why": ["no room memory row at all"]}

    blob = _blob(mem)
    concern = mem.get("current_concern") or ""
    disc = mem.get("discussion_summary") or ""

    # (i) does the surface say the value was withdrawn, and name the replacement?
    mentions = bool(_RETRACT_VOCAB.search(blob)) and bool(_HAS_41.search(blob))

    bad = []
    soft = []

    # current_concern is the headline "what is wrong with this patient right
    # now" line. A retracted value here is an unqualified live assertion.
    if (_HAS_68.search(concern) or _HYPERK.search(concern)) \
            and not _RETRACT_VOCAB.search(concern):
        bad.append(f"current_concern asserts it as current: {concern!r}")

    # A rendered risk-flag card says "Risk Detected / Active".
    for rf in (mem.get("risk_flags") or []):
        conf = rf.get("confidence") or 0
        txt = json.dumps(rf)
        names_it = _HAS_68.search(txt) or _HYPERK.search(txt)
        if not names_it:
            continue
        if _RETRACT_VOCAB.search(txt):
            continue
        if conf >= RISK_RENDER_THRESHOLD:
            bad.append(f"RENDERED risk_flag (conf={conf}) asserts it: {txt}")
        else:
            soft.append(f"stored-but-unrendered risk_flag (conf={conf}) asserts it: {txt}")

    # discussion_summary is an append-only narrative, so an early mention of 6.8
    # is history, not an assertion — PROVIDED the correction also appears. If
    # the narrative names the value and never withdraws it anywhere, that is the
    # dangerous case the fix exists to prevent.
    if _HAS_68.search(disc) or _HYPERK.search(disc):
        if not _RETRACT_VOCAB.search(disc):
            bad.append("discussion_summary names the retracted value and never "
                       "withdraws it anywhere in the narrative")
        else:
            soft.append("discussion_summary carries the old value AND the "
                        "correction (append-only narrative)")

    return {"mentions_retraction": mentions,
            "asserts_retracted_as_current": bool(bad),
            "why": bad,
            "notes": soft}


# ── run ────────────────────────────────────────────────────────────────
def run_once(tag, tok_doc, tok_nur, pace, seed=False):
    pid, cid = make_room(tag)
    if seed:
        seed_alarm(cid, pid)
    print(f"\n{'='*74}\nRUN {tag}   room={cid}  patient={pid}"
          f"{'  [alarm pre-seeded]' if seed else ''}\n{'='*74}")
    timings = []
    prev_llm = 0
    llm_ran = {}          # thread index -> did this message actually reach the model
    for i, (role, content) in enumerate(THREAD):
        tok = tok_doc if role == "doc" else tok_nur
        mid = post(cid, content, tok)
        ok, secs, llm = wait_processed(cid, mid)
        llm = llm or 0
        classified = llm > prev_llm
        llm_ran[i] = classified
        prev_llm = llm
        marker = ("  <<< CRITICAL" if i == CRITICAL_IDX else
                  "  <<< RETRACTION" if i == RETRACTION_IDX else "")
        print(f"  [{i:2}] {role} msg={mid} processed={ok} {secs:5.1f}s "
              f"llm_total={llm} classified={classified}{marker}")
        timings.append(secs)
        if not ok:
            print(f"       !! ambient never processed message {mid}")
        time.sleep(pace)

    # A run where the retraction never reached the model measures NOTHING about
    # the model or the prompt — it measures GPU residency. Reporting it as a
    # model failure would be a lie, so it is called out separately.
    retraction_classified = llm_ran.get(RETRACTION_IDX, False)
    if not retraction_classified and not seed:
        print("\n  !! INVALID RUN: the retraction message never reached the "
              "classifier (ambient tier not resident, or LLM slot timed out). "
              "This says nothing about the prompt fix.")
    elif seed:
        print(f"\n  [seeded mode] classifier reached on retraction: "
              f"{retraction_classified} — scoring the ROUTE + MEMORY layer only.")

    mem = read_surface(cid, tok_doc)
    obs = db_query("SELECT id, source_message_id, observation_type, confidence, content "
                   "FROM sage_observations WHERE room_id = ? ORDER BY id", (cid,))
    sc = score(mem)

    print(f"\n  --- SAGE_MEMORY SURFACE (GET /api/relay/channels/{cid}/sage_memory) ---")
    if mem:
        print(f"  current_concern:\n    {mem.get('current_concern')!r}")
        print(f"  discussion_summary:\n    {mem.get('discussion_summary')!r}")
        print(f"  risk_flags:\n    {json.dumps(mem.get('risk_flags'), indent=2)}")
        print(f"  active_intents:\n    {json.dumps(mem.get('active_intents'))}")
    else:
        print("  <NO MEMORY ROW>")

    print(f"\n  --- OBSERVATIONS ({len(obs)}) ---")
    for o in obs:
        print(f"  obs#{o['id']} msg={o['source_message_id']} "
              f"type={o['observation_type']} conf={o['confidence']}")
        print(f"      {o['content'][:500]}")

    print(f"\n  SCORE: mentions_retraction={sc['mentions_retraction']}  "
          f"asserts_retracted_as_current={sc['asserts_retracted_as_current']}")
    for w in sc.get("why") or []:
        print(f"     FAIL - {w}")
    for w in sc.get("notes") or []:
        print(f"     note - {w}")

    passed = sc["mentions_retraction"] and not sc["asserts_retracted_as_current"]
    verdict = "PASS" if passed else (
        "FAIL" if (retraction_classified or seed) else "INVALID")
    print(f"  RUN {tag} -> {verdict}")
    return {"tag": tag, "room": cid, "passed": passed, "verdict": verdict,
            "retraction_classified": retraction_classified, "score": sc,
            "memory": mem, "observations": obs,
            "mean_latency": sum(timings) / len(timings)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--pace", type=float, default=1.0,
                    help="seconds between messages, on top of processing wait")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--seed-alarm", action="store_true",
                    help="pre-chart the critical potassium instead of relying on "
                         "the classifier (use when the ambient tier is not resident)")
    ap.add_argument("--out", default="/tmp/retraction_results.json")
    a = ap.parse_args()

    tok_doc = login(*DOC)
    tok_nur = login(*NUR)
    print(f"auth ok. runs={a.runs} pace={a.pace}s")

    results = []
    for n in range(1, a.runs + 1):
        tag = f"{a.prefix}{n:02d}"
        try:
            results.append(run_once(tag, tok_doc, tok_nur, a.pace, a.seed_alarm))
        except Exception as e:
            print(f"RUN {tag} EXPLODED: {type(e).__name__}: {e}")
            results.append({"tag": tag, "passed": False, "error": repr(e)})

    n_pass = sum(1 for r in results if r.get("passed"))
    n_invalid = sum(1 for r in results if r.get("verdict") == "INVALID")
    print(f"\n{'='*74}\nPASS RATE: {n_pass}/{len(results)}"
          f"{f'   ({n_invalid} INVALID — model never called)' if n_invalid else ''}"
          f"\n{'='*74}")
    for r in results:
        s = r.get("score", {})
        print(f"  {r['tag']}: {r.get('verdict', 'ERROR')}  "
              f"mentions={s.get('mentions_retraction')} "
              f"asserts_current={s.get('asserts_retracted_as_current')}")
    Path(a.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"\nfull results -> {a.out}")


if __name__ == "__main__":
    main()
