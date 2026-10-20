"""Orb WEDGE benchmark — what the loop actually costs, end to end.

Orb sells a loop: Vigil detects deterioration, Sage reasons over it, Relay puts a
DRAFTED ACTION in front of the right clinician. `sage_bench.py` measures Sage
answering a question a clinician typed. THIS measures the other thing — the loop
that starts with a set of vitals and ends with a clinician having something to
act on — because those are different products with different numbers, and the
wedge number is the one a champion and an investor are shown.

WHAT IS REPORTED, per run:

    vitals_to_alert_ms      vitals-in → the alert row exists and has been
                            dispatched (the nurse's POST returns)
    alert_read_ms           the registrar's GET of the case room
    alert_visible_ms        vitals-in → a clinician can SEE the alert
                            (= vitals_to_alert_ms + alert_read_ms). THIS IS THE
                            NUMBER THAT MUST NOT MOVE.
    assessment_ttft_ms      first token out of the careful model (server-timed)
    assessment_total_ms     the generator's own end-to-end, from its t_start
    alert_to_ready_ms       alert-visible → the assessment row reaches a
                            terminal state. What the clinician actually waits.
    total_to_actionable_ms  vitals-in → ready assessment fetched with its cards
                            (= what "Orb had a drafted plan in front of me N
                            seconds after the obs" means)

…plus the outcome of every run (`ready` / `unavailable:<reason>`), which is what
makes the SHED AND FAILURE RATE a measurement rather than an assumption.

HOW IT RUNS. In process, against the real models, with NO SERVER. The alert is
charted through the real nurse route and read back through the real Relay route
over an in-process ASGI transport (starlette's TestClient), so the numbers
include Orb's own routing, auth, permission checks and encrypted-at-rest SQLite,
and EXCLUDE uvicorn's socket handling and any network. That exclusion is the
only difference from the deployed appliance and it is small; it is stated here
rather than buried, because a benchmark that quietly measures a different code
path is worse than no benchmark.

THE DATABASE. By default this copies the dev database to a throwaway file and
runs against the COPY, so the real corpus (and therefore real guideline
retrieval cost) is in the measurement while nothing is written back. `--db
fresh` builds an empty schema instead, which is faster to start and measures a
retrieval that finds nothing — use it only for a smoke run, and note that the
retrieval component is then understated.

    NEVER run this with ORB_DEPLOYMENT_PROFILE=prod against the dev database:
    booting the dev DB under the prod profile deactivates every seeded login.
    The script refuses to start in that combination.

HONESTY RULES BUILT INTO THE OUTPUT:
  * median AND full range, never a single figure. n is always printed.
  * every failed or shed run is counted and named; the timing summaries say how
    many runs they are over.
  * whether the model was WARM is recorded from Ollama itself, not assumed.
  * the hardware is recorded.

USAGE (no server needed):

    .venv/bin/python scripts/wedge_bench.py --runs 8 --label "post-assessment"
    .venv/bin/python scripts/wedge_bench.py --runs 4 --storm 6      # contention
    .venv/bin/python scripts/wedge_bench.py --runs 2 --db fresh     # smoke

Writes docs/perf/wedge-<UTC timestamp>.json and a dated Markdown report
alongside it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent


# NEWS2 19 — the dry run's peri-arrest picture: RR 32 (3), SpO2 86 (3), on O2
# (2), Temp 39.4 (2), SBP 78 (3), HR 138 (3), ACVPU voice (3). Deliberately the
# same clinical picture the wedge tests use, so a number here and an assertion
# there are about the same event.
class _BenchVitals(TypedDict):
    heart_rate: float
    systolic_bp: float
    diastolic_bp: float
    respiratory_rate: float
    temperature: float
    spo2: float
    consciousness: str
    on_oxygen: bool


PERI_ARREST: _BenchVitals = {
    "heart_rate": 138,
    "systolic_bp": 78,
    "diastolic_bp": 44,
    "respiratory_rate": 32,
    "temperature": 39.4,
    "spo2": 86,
    "consciousness": "voice",
    "on_oxygen": True,
}

DEPARTMENT = "Wedge Bench Medicine"
BENCH_TAG = "WEDGEBENCH"


# ═════════════════════════════════════════════════════════════════════════════
#  Environment — must be settled BEFORE anything imports config/database
# ═════════════════════════════════════════════════════════════════════════════
def load_dotenv(path: Path) -> list[str]:
    """Load the repo `.env` exactly as run-orb.sh sources it.

    Needed because the dev database is ENCRYPTED AT REST: without
    ORB_DB_ENCRYPTION_KEY the copy cannot be opened at all. Values already
    present in the environment win, so an operator can override one setting
    without editing the file. Returns the key NAMES loaded, never the values —
    this script prints its own configuration and must not print a secret.
    """
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


#: Throwaway directory this run created, so it can be removed on the way out.
#: A copy of the dev database is 430 MB; leaving one behind per invocation fills
#: a laptop quietly, which is exactly the shape of the 390-file / 339 MB FHIR
#: export leak the test harness had to be fixed for.
_SCRATCH: Path | None = None


def prepare_database(mode: str) -> tuple[Path, str]:
    """Point ORB_DB_PATH at something safe to write to. Returns (path, note)."""
    global _SCRATCH
    if mode not in ("copy", "fresh") and mode:
        path = Path(mode).expanduser().resolve()
        os.environ["ORB_DB_PATH"] = str(path)
        return path, f"caller-supplied database at {path}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="orb_wedge_bench_"))
    _SCRATCH = tmp_dir
    target = tmp_dir / "orb.db"
    if mode == "fresh":
        os.environ["ORB_DB_PATH"] = str(target)
        return target, (
            "EMPTY schema, guideline retrieval finds no corpus, so the "
            "retrieval component of assessment latency is UNDERSTATED"
        )

    source = SERVER_DIR / "orb.db"
    if not source.exists():
        raise SystemExit(f"no dev database at {source}; use --db fresh")
    shutil.copy2(source, target)
    # The WAL and shared-memory files carry committed pages that have not been
    # checkpointed back into the main file yet. Copying the main file alone
    # would silently benchmark against an older database.
    for suffix in ("-wal", "-shm"):
        side = Path(str(source) + suffix)
        if side.exists():
            shutil.copy2(side, str(target) + suffix)
    os.environ["ORB_DB_PATH"] = str(target)
    size_mb = target.stat().st_size / (1024 * 1024)
    return target, (
        f"throwaway COPY of the dev database ({size_mb:.0f} MB), real "
        f"corpus, real retrieval cost, nothing written back"
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Machine + model facts, recorded rather than assumed
# ═════════════════════════════════════════════════════════════════════════════
def hardware() -> dict:
    info = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpu": platform.processor() or None,
        "chip": None,
        "memory_gb": None,
    }
    try:
        out = subprocess.run(
            ["system_profiler", "SPHardwareDataType"], capture_output=True, text=True, timeout=20
        ).stdout
        for line in out.splitlines():
            if "Chip:" in line:
                info["chip"] = line.split(":", 1)[1].strip()
            elif "Memory:" in line:
                info["memory_gb"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return info


def ollama_resident() -> list[dict]:
    """What Ollama says is loaded RIGHT NOW. `ollama ps`, parsed — the model
    being warm is the single biggest lever on these numbers, so it is read from
    the runtime rather than asserted in a report footnote."""
    try:
        import httpx

        base = os.getenv("ORB_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        r = httpx.get(f"{base}/api/ps", timeout=5, trust_env=False)
        r.raise_for_status()
        return [
            {
                "model": m.get("name") or m.get("model"),
                "size_vram": m.get("size_vram"),
                "expires_at": m.get("expires_at"),
            }
            for m in (r.json().get("models") or [])
        ]
    except Exception as e:  # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {e}"}]


# ═════════════════════════════════════════════════════════════════════════════
#  The world under test
# ═════════════════════════════════════════════════════════════════════════════
def make_app():
    """Nurse router + Relay router. NOT `main.app`: its lifespan starts the
    vitals simulator, the shadow sweeper, the escalation sweep and the model
    warmups, and every one of those would land inside the numbers."""
    from fastapi import FastAPI
    from routes.nurse_routes import router as nurse_router
    from routes.relay_routes import router as relay_router

    app = FastAPI()
    app.include_router(nurse_router)
    app.include_router(relay_router)
    return app


def auth(uid: str, role: str) -> dict:
    from auth import create_token

    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def seed_staff() -> tuple[str, dict]:
    from database import db_session

    nurse = f"{BENCH_TAG}N{uuid.uuid4().hex[:8].upper()}"
    doctor = f"{BENCH_TAG}D{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Bench Nurse', 'nurse', 'x', ?)",
            (nurse, DEPARTMENT),
        )
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Bench Registrar', 'doctor', 'x', ?)",
            (doctor, DEPARTMENT),
        )
    return nurse, {"id": doctor, "role": "doctor"}


def seed_patient() -> tuple[str, str]:
    """An admitted, deteriorating patient with a case room. Returns (id, room)."""
    from database import db_session

    pid = f"{BENCH_TAG}P{uuid.uuid4().hex[:10].upper()}"
    room = f"wedge-bench-room-{pid}"
    now = datetime.now(UTC)
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES (?, 'Bench Admitter', 'doctor', 'x')",
            (f"{BENCH_TAG}ADM",),
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'B-1', 'WB-1', 71, 'F', 'Community acquired pneumonia',
                       'admitted', ?)""",
            (pid, f"Bench {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now.isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'WB-1', 'B-1', 'active', ?)",
            (pid, DEPARTMENT, f"{BENCH_TAG}ADM"),
        )
        # A worsening series, oldest first, so the prompt carries a real trend
        # rather than four copies of one snapshot — the trend is part of what the
        # model has to read, and therefore part of what it costs.
        for i in range(4):
            drift = 3 - i
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate,
                                       consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pid,
                    (now - timedelta(minutes=5 * (drift + 1))).isoformat(),
                    PERI_ARREST["heart_rate"] - 4 * drift,
                    PERI_ARREST["systolic_bp"] + 6 * drift,
                    PERI_ARREST["diastolic_bp"],
                    PERI_ARREST["spo2"] + 2 * drift,
                    PERI_ARREST["temperature"],
                    PERI_ARREST["respiratory_rate"] - 2 * drift,
                    PERI_ARREST["consciousness"],
                    1,
                ),
            )
        db.execute(
            "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
            "VALUES (?, ?, 'case_room', ?)",
            (room, f"Bench Case Room {pid}", pid),
        )
    return pid, room


def engine_news2() -> int:
    from services.vigil_service import compute_news2

    return compute_news2(
        PERI_ARREST["heart_rate"],
        PERI_ARREST["systolic_bp"],
        PERI_ARREST["respiratory_rate"],
        PERI_ARREST["temperature"],
        PERI_ARREST["spo2"],
        PERI_ARREST["consciousness"],
        PERI_ARREST["on_oxygen"],
    )["raw_news2"]


# ═════════════════════════════════════════════════════════════════════════════
#  One run of the loop
# ═════════════════════════════════════════════════════════════════════════════
def chart_and_read(client, nurse: str, doctor: dict, patient: str, room: str) -> dict:
    """Vitals in → alert visible. Everything the CLINICIAN WAITS FOR.

    The assessment is deliberately not awaited here: the whole design claim is
    that it is generated after the alert is already on screen, so measuring it
    inside this function would measure a product Orb does not ship.
    """
    rec: dict = {"patient_id": patient, "error": None}

    t0 = time.perf_counter()
    posted = client.post(
        f"/api/nurse/patients/{patient}/observations",
        json=dict(PERI_ARREST),
        headers=auth(nurse, "nurse"),
        timeout=120,
    )
    t_alert = time.perf_counter()
    if posted.status_code != 200:
        rec["error"] = f"observation POST {posted.status_code}: {posted.text[:200]}"
        return rec
    rec["news2_engine"] = posted.json().get("news2", {}).get("raw_news2")

    read = client.get(
        f"/api/relay/channels/{room}/messages", headers=auth(doctor["id"], "doctor"), timeout=120
    )
    t_read = time.perf_counter()
    if read.status_code != 200:
        rec["error"] = f"messages GET {read.status_code}"
        return rec
    alerts = [m for m in read.json() if m.get("message_type") == "alert"]
    if not alerts:
        rec["error"] = "no alert reached the case room"
        return rec

    rec["t0"] = t0
    # Wall-clock twin of t0. The perf counter measures what THIS thread observed,
    # and in `--storm` mode the harness polls the batch in order — so run 3's
    # poll cannot start until run 1's has returned, and a poll-derived figure
    # would report run 3 as slower than it was. The row's own `updated_at` is
    # when the draft actually resolved, and it needs a wall-clock origin to be
    # subtracted from.
    rec["t0_wall"] = datetime.now(UTC)
    rec["alert_message_id"] = alerts[-1]["id"]
    rec["vitals_to_alert_ms"] = round((t_alert - t0) * 1000, 1)
    rec["alert_read_ms"] = round((t_read - t_alert) * 1000, 1)
    rec["alert_visible_ms"] = round((t_read - t0) * 1000, 1)
    return rec


def await_assessment(patient: str, timeout: float) -> dict | None:
    """Poll for the draft to reach a TERMINAL state.

    A poll, not a wait on the task: the alert path is fire-and-forget by design,
    and anything that could block on the generator here would be measuring an
    implementation Orb deliberately does not have.
    """
    from database import db_session

    from services import sage_assessment_service as sas

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db_session() as db:
            row = db.execute(
                "SELECT * FROM sage_assessments WHERE patient_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (patient,),
            ).fetchone()
        if row is not None:
            state, reason = sas._derive_state(dict(row))
            if state != sas.STATE_GENERATING:
                out = dict(row)
                out["_derived_state"] = state
                out["_derived_reason"] = reason
                return out
        time.sleep(0.05)
    return None


def finish_run(client, doctor: dict, room: str, rec: dict, timeout: float) -> dict:
    """Alert-visible → the clinician has a drafted plan they could act on."""
    from database import db_session

    patient = rec["patient_id"]
    row = await_assessment(patient, timeout)
    t_ready = time.perf_counter()
    if row is None:
        rec["outcome"] = "never_resolved"
        rec["error"] = rec["error"] or f"no assessment resolved within {timeout}s"
        return rec

    state = row["_derived_state"]
    rec["outcome"] = state if state == "ready" else f"unavailable:{row['_derived_reason']}"
    rec["assessment_ttft_ms"] = row["ttft_ms"]
    rec["assessment_total_ms"] = row["total_ms"]
    rec["model"] = row["model"]
    rec["citation_basis"] = row["citation_basis"]
    rec["confidence"] = row["confidence"]
    rec["news2_stored"] = row["news2"]
    rec["news2_matches_engine"] = row["news2"] == rec.get("news2_engine")
    rec["alert_to_observed_ms"] = round((t_ready - rec["t0"]) * 1000 - rec["alert_visible_ms"], 1)
    # THE HEADLINE FIGURE, taken from the row rather than from the poll — see
    # the note on `t0_wall`. Falls back to the poll when the timestamp cannot be
    # parsed, and says which it used rather than silently mixing the two.
    rec["alert_to_ready_ms"] = rec["alert_to_observed_ms"]
    rec["ready_ms_source"] = "poll"
    try:
        resolved = datetime.fromisoformat(row["updated_at"])
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        elapsed = (resolved - rec["t0_wall"]).total_seconds() * 1000 - rec["alert_visible_ms"]
        if elapsed > 0:
            rec["alert_to_ready_ms"] = round(elapsed, 1)
            rec["ready_ms_source"] = "row.updated_at"
    except Exception:
        pass

    if state != "ready":
        rec["cards"] = 0
        rec["total_to_actionable_ms"] = None
        return rec

    fetched = client.get(
        f"/api/relay/channels/{room}/sage_assessments",
        params={"message_id": rec["alert_message_id"]},
        headers=auth(doctor["id"], "doctor"),
        timeout=120,
    )
    t_fetched = time.perf_counter()
    rec["assessment_fetch_ms"] = round((t_fetched - t_ready) * 1000, 1)
    # Composed from the three measured stages rather than from one wall-clock
    # span, so it inherits the row-derived resolution time and is not distorted
    # by the harness polling a storm batch in order.
    rec["total_to_actionable_ms"] = round(
        rec["alert_visible_ms"] + rec["alert_to_ready_ms"] + rec["assessment_fetch_ms"], 1
    )
    payload = fetched.json()[0] if fetched.status_code == 200 and fetched.json() else {}
    rec["sections_present"] = sum(
        bool(payload.get(k))
        for k in ("whats_happening", "considerations", "citation_basis", "watch_items")
    )

    # WHY THERE ARE OR ARE NOT CARDS. A latency figure for a draft with nothing
    # to approve would describe a different product from the one being sold, so
    # the count is recorded with the reason beside it rather than on its own.
    considerations = payload.get("considerations") or []
    rec["considerations_n"] = len(considerations)
    rec["cardable_steps"] = sum(1 for c in considerations if c.get("action_type"))
    rec["advice_only_reasons"] = [
        c["advice_only_reason"] for c in considerations if c.get("advice_only_reason")
    ]
    rec["step_texts"] = [(c.get("step") or "")[:140] for c in considerations]
    rec["watch_items_n"] = len(payload.get("watch_items") or [])
    rec["evidence_n"] = len(payload.get("evidence") or [])
    rec["summary_chars"] = len(payload.get("whats_happening") or "")

    with db_session() as db:
        rec["cards"] = db.execute(
            "SELECT COUNT(*) AS n FROM sage_actions WHERE patient_id = ?", (patient,)
        ).fetchone()["n"]
        # Guideline retrieval, from the instrument rather than from a second
        # clock: it is the component of the generator's total that is NOT model
        # time, and under contention it is the part that serialises first.
        row = db.execute(
            "SELECT payload_json FROM shadow_events_current WHERE patient_id = ? "
            "AND kind = 'sage.drafted' ORDER BY id DESC LIMIT 1",
            (patient,),
        ).fetchone()
    try:
        rec["retrieval_ms"] = (json.loads(row["payload_json"]) or {}).get("retrieval_ms")
    except Exception:
        rec["retrieval_ms"] = None
    return rec


def cleanup(patients: list[str]) -> None:
    """The bench DB is a throwaway copy, but `--db <path>` may not be."""
    if not patients:
        return
    from database import db_session

    marks = ",".join("?" * len(patients))
    for sql in (
        f"DELETE FROM sage_assessment_actions WHERE assessment_id IN (SELECT id FROM sage_assessments WHERE patient_id IN ({marks}))",
        f"DELETE FROM sage_assessment_events WHERE assessment_id IN (SELECT id FROM sage_assessments WHERE patient_id IN ({marks}))",
        f"DELETE FROM sage_assessments WHERE patient_id IN ({marks})",
        f"DELETE FROM sage_action_events WHERE action_id IN (SELECT id FROM sage_actions WHERE patient_id IN ({marks}))",
        f"DELETE FROM sage_actions WHERE patient_id IN ({marks})",
        f"DELETE FROM messages WHERE channel_id IN (SELECT id FROM channels WHERE patient_id IN ({marks}))",
        f"DELETE FROM channels WHERE patient_id IN ({marks})",
        f"DELETE FROM observations WHERE patient_id IN ({marks})",
        f"DELETE FROM vitals WHERE patient_id IN ({marks})",
        f"DELETE FROM risk_scores WHERE patient_id IN ({marks})",
        f"DELETE FROM admissions WHERE patient_id IN ({marks})",
    ):
        try:
            with db_session() as db:
                db.execute(sql, patients)
        except Exception:
            pass
    try:
        with db_session() as db:
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", patients)
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  Summary — median and RANGE, never a single flattering figure
# ═════════════════════════════════════════════════════════════════════════════
TIMING_KEYS = (
    "vitals_to_alert_ms",
    "alert_read_ms",
    "alert_visible_ms",
    "retrieval_ms",
    "assessment_ttft_ms",
    "assessment_total_ms",
    "alert_to_ready_ms",
    "total_to_actionable_ms",
)


def stat(records: list[dict], key: str) -> dict | None:
    vals = sorted(float(r[key]) for r in records if isinstance(r.get(key), (int, float)))
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": round(vals[0], 1),
        "p50": round(statistics.median(vals), 1),
        "p95": round(vals[min(len(vals) - 1, int(round((len(vals) - 1) * 0.95)))], 1),
        "max": round(vals[-1], 1),
        "mean": round(statistics.fmean(vals), 1),
    }


def summarize(records: list[dict]) -> dict:
    ready = [r for r in records if r.get("outcome") == "ready"]
    unavailable = [r for r in records if str(r.get("outcome", "")).startswith("unavailable")]
    errored = [r for r in records if r.get("error")]
    reasons: dict[str, int] = {}
    for r in unavailable:
        reasons[r["outcome"].split(":", 1)[1]] = reasons.get(r["outcome"].split(":", 1)[1], 0) + 1

    n = len(records) or 1
    return {
        "runs": len(records),
        "ready": len(ready),
        "unavailable": len(unavailable),
        "errors": len(errored),
        # THE SHED/FAILURE RATE, split, because "the GPU was busy" and "the model
        # broke" are different operational facts with different fixes.
        "shed_rate": round(sum(reasons.get(k, 0) for k in ("shed", "queue_timeout")) / n, 3),
        "failure_rate": round(
            sum(v for k, v in reasons.items() if k not in ("shed", "queue_timeout")) / n, 3
        ),
        "unavailable_reasons": reasons,
        "news2_agreed": sum(1 for r in records if r.get("news2_matches_engine")),
        "news2_checked": sum(1 for r in records if r.get("news2_stored") is not None),
        # ── THE WEDGE'S OWN CLAIM, COUNTED ───────────────────────────────────
        # Orb sells "Relay puts a DRAFTED ACTION in front of the clinician". A
        # ready assessment with zero cards is a well-written note, not a drafted
        # action: there is nothing to approve, nothing to decline, and
        # `agreement` — the metric that would show whether clinicians trust Sage
        # — cannot move. So the card count is reported as a RATE beside the
        # latency, not buried in the per-run table.
        "cards_total": sum(int(r.get("cards") or 0) for r in ready),
        "runs_with_a_card": sum(1 for r in ready if int(r.get("cards") or 0) > 0),
        "actionable_rate": (
            round(sum(1 for r in ready if int(r.get("cards") or 0) > 0) / len(ready), 3)
            if ready
            else None
        ),
        "considerations_p50": (
            statistics.median(
                [r["considerations_n"] for r in ready if r.get("considerations_n") is not None]
            )
            if any(r.get("considerations_n") is not None for r in ready)
            else None
        ),
        "cardable_steps_total": sum(int(r.get("cardable_steps") or 0) for r in ready),
        "timings": {k: stat(records, k) for k in TIMING_KEYS},
        "timings_note": (
            "Each timing summary reports its own n. `alert_visible_ms` is over "
            "EVERY run that raised an alert; the assessment timings are over the "
            "runs that produced one, which is the only population they can "
            "honestly describe."
        ),
    }


def fmt(s: dict | None, unit: str = "ms") -> str:
    if not s:
        return "not measured"
    if unit == "s":
        return (
            f"{s['p50'] / 1000:.1f} s median (range {s['min'] / 1000:.1f}–"
            f"{s['max'] / 1000:.1f} s, n={s['n']})"
        )
    return f"{s['p50']:.0f} ms median (range {s['min']:.0f}–{s['max']:.0f} ms, n={s['n']})"


def write_report(out: dict, path: Path) -> Path:
    s = out["summary"]
    t = s["timings"]
    md = path.with_suffix(".md")
    warm = out["environment"]["ollama_resident"]
    warm_line = (
        ", ".join(f"{m.get('model')} resident" for m in warm if m.get("model"))
        or "NO model resident"
    )
    lines = [
        f"# Orb wedge benchmark: {out['run_at'][:10]}",
        "",
        f"*{out['label'] or 'unlabelled run'}: {s['runs']} run(s), generated {out['run_at']}*",
        "",
        "## What this measures",
        "",
        "Vitals arrive → Vigil scores and fires → the alert is persisted and is",
        "READABLE by the right clinician → Sage drafts the assessment → its",
        "proposals become approvable cards. In process, through the real nurse and",
        "Relay routes, against the real models. No server; no network.",
        "",
        "## Headline",
        "",
        "| Stage | Figure |",
        "| --- | --- |",
        f"| Vitals in → alert visible to the clinician | **{fmt(t['alert_visible_ms'])}** |",
        f"| …of which: charting + Vigil + Relay write | {fmt(t['vitals_to_alert_ms'])} |",
        f"| …of which: the registrar's read | {fmt(t['alert_read_ms'])} |",
        f"| Alert visible → assessment resolved | **{fmt(t['alert_to_ready_ms'], 's')}** |",
        f"| Vitals in → a drafted plan in hand | **{fmt(t['total_to_actionable_ms'], 's')}** |",
        f"| …of which: guideline retrieval | {fmt(t['retrieval_ms'], 's')} |",
        f"| Model time to first token | {fmt(t['assessment_ttft_ms'], 's')} |",
        f"| Generator end-to-end (its own clock) | {fmt(t['assessment_total_ms'], 's')} |",
        "",
        "## Outcomes",
        "",
        f"- Assessments **ready**: {s['ready']} / {s['runs']}",
        f"- **Unavailable**: {s['unavailable']}: {s['unavailable_reasons'] or 'none'}",
        f"- Shed rate (GPU unavailable in the window): **{s['shed_rate']:.0%}**",
        f"- Failure rate (everything else): **{s['failure_rate']:.0%}**",
        f"- Harness errors: {s['errors']}",
        f"- NEWS2 on the draft equalled the deterministic engine: "
        f"**{s['news2_agreed']} / {s['news2_checked']}**",
        "",
        "## Is there anything to approve?",
        "",
        "The wedge's claim is that Relay puts a *drafted action* in front of a",
        "clinician. A ready assessment with no card is a well-written note: there is",
        "nothing to approve, nothing to decline, and `agreement` cannot move.",
        "",
        f"- Ready assessments carrying **at least one approvable card**: "
        f"**{s['runs_with_a_card']} / {s['ready']}**"
        + (f" (**{s['actionable_rate']:.0%}**)" if s["actionable_rate"] is not None else ""),
        f"- Total cards proposed: {s['cards_total']}",
        f"- Considerations written per assessment (median): {s['considerations_p50']}",
        f"- …of which the model marked as something Orb could draft as an order: "
        f"{s['cardable_steps_total']} across all ready runs",
        "",
        "## Conditions",
        "",
        f"- Hardware: {out['environment']['hardware'].get('chip') or out['environment']['hardware'].get('cpu')}, "
        f"{out['environment']['hardware'].get('memory_gb')}, {out['environment']['hardware']['platform']}",
        f"- Models resident at start: {warm_line}",
        f"- Careful tier configured: `{out['environment']['llm_model']}`; "
        f"ambient tier `{out['environment']['ambient_model']}`",
        f"- Database: {out['environment']['database_note']}",
        f"- Warmup run before the timed runs: {'yes' if out['environment']['warmed'] else 'NO'}",
        f"- Concurrency: {out['environment']['storm'] or 1} alert(s) in flight per batch",
        "",
        "## How to read it",
        "",
        "- **The alert number is the product.** It is measured on every run and it",
        "  does not depend on the model: the alert path pays one `create_task` for",
        "  the draft and nothing else. If this figure ever moves with model load,",
        "  something is awaiting generation on the alert path.",
        "- **The assessment number is a wait a clinician can see.** It is tens of",
        "  seconds on a 27B. That is the honest cost of a careful model, and it is",
        "  why the alert is never allowed to wait for it.",
        "- Medians with the full range are given because the range is the finding:",
        "  a first-token time that varies 4x across runs on an idle machine will",
        "  vary more on a ward.",
        "- Excluded from these numbers: uvicorn socket handling and network time.",
        "  Included: auth, permissions, encrypted-at-rest SQLite, guideline",
        "  retrieval, the drug-safety gate and the shadow instrument.",
        "",
        "## Per-run detail",
        "",
        "| # | alert visible (ms) | assessment (s) | total (s) | outcome | cards | NEWS2 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(out["records"], 1):

        def _s(key: str, r: dict = r) -> str:
            v = r.get(key)
            return f"{v / 1000:.1f}" if isinstance(v, (int, float)) else "—"

        lines.append(
            f"| {i} | {r.get('alert_visible_ms', '—')} | {_s('alert_to_ready_ms')} | "
            f"{_s('total_to_actionable_ms')} | {r.get('outcome') or r.get('error')} | "
            f"{r.get('cards', '—')} | {r.get('news2_stored', '—')} |"
        )
    md.write_text("\n".join(lines) + "\n")
    return md


# ═════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=6, help="timed runs (default 6)")
    ap.add_argument(
        "--storm",
        type=int,
        default=0,
        help="alerts fired back-to-back per batch, to measure shed under "
        "contention (default 0 = one at a time)",
    )
    ap.add_argument(
        "--db",
        default="copy",
        choices=None,
        help="'copy' (default, throwaway copy of the dev DB), 'fresh', or a path",
    )
    ap.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait for one assessment to resolve"
    )
    ap.add_argument(
        "--no-warmup",
        action="store_true",
        help="skip the untimed warmup run (its cost lands in run 1)",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="leave the bench patients and their drafts in the bench DB "
        "so the generated text can be read afterwards",
    )
    ap.add_argument("--label", default="")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    loaded = load_dotenv(REPO_ROOT / ".env")
    if os.environ.get("ORB_DEPLOYMENT_PROFILE", "dev").strip().lower() == "prod":
        # Booting the dev database under the prod profile deactivates every
        # seeded login; a benchmark is never worth that.
        print(
            "REFUSING: ORB_DEPLOYMENT_PROFILE=prod. Run the bench under the dev profile.",
            file=sys.stderr,
        )
        return 2
    os.environ.setdefault("ORB_SHADOW_ENABLED", "true")
    os.environ.setdefault("ORB_SAGE_ASSESSMENT_ENABLED", "true")
    # Watchdog and FHIR-export paths would otherwise litter the checkout.
    os.environ.setdefault("ORB_WATCHDOG_DIR", tempfile.mkdtemp(prefix="orb_bench_watchdog_"))
    os.environ.setdefault("ORB_FHIR_EXPORT_DIR", tempfile.mkdtemp(prefix="orb_bench_fhir_"))

    db_path, db_note = prepare_database(args.db)
    sys.path.insert(0, str(SERVER_DIR))

    print(f"[wedge_bench] .env keys loaded: {len(loaded)}")
    print(f"[wedge_bench] database: {db_note}")

    from config import AMBIENT_LLM_MODEL, LLM_MODEL
    from database import init_db

    init_db()  # brings a copied database up to the current migration set

    from fastapi.testclient import TestClient

    resident = ollama_resident()
    print(f"[wedge_bench] ollama resident: {resident}")

    records: list[dict] = []
    patients: list[str] = []
    warmed = False
    try:
        with TestClient(make_app()) as client:
            nurse, doctor = seed_staff()
            engine = engine_news2()
            print(f"[wedge_bench] engine NEWS2 for the bench observation: {engine}")

            batch = max(1, args.storm)
            if not args.no_warmup:
                print("[wedge_bench] warmup (untimed)…", flush=True)
                pid, room = seed_patient()
                patients.append(pid)
                rec = chart_and_read(client, nurse, doctor, pid, room)
                if not rec.get("error"):
                    finish_run(client, doctor, room, rec, args.timeout)
                warmed = True
                print(
                    f"[wedge_bench] warmup outcome: {rec.get('outcome') or rec.get('error')}",
                    flush=True,
                )

            done = 0
            while done < args.runs:
                size = min(batch, args.runs - done)
                pending = []
                for _ in range(size):
                    pid, room = seed_patient()
                    patients.append(pid)
                    rec = chart_and_read(client, nurse, doctor, pid, room)
                    rec["news2_engine"] = rec.get("news2_engine", engine)
                    pending.append((rec, room))
                for rec, room in pending:
                    if rec.get("error"):
                        records.append(rec)
                    else:
                        records.append(finish_run(client, doctor, room, rec, args.timeout))
                    done += 1
                    r = records[-1]
                    print(
                        f"[{done}/{args.runs}] alert_visible={r.get('alert_visible_ms')}ms "
                        f"assessment={r.get('alert_to_ready_ms')}ms "
                        f"outcome={r.get('outcome') or r.get('error')} "
                        f"cards={r.get('cards')}",
                        flush=True,
                    )
    finally:
        if not args.keep:
            try:
                cleanup(patients)
            except Exception:
                pass

    for rec in records:
        rec.pop("t0", None)
        rec["t0_wall"] = rec["t0_wall"].isoformat() if rec.get("t0_wall") else None

    out = {
        "benchmark": "wedge",
        "label": args.label,
        "run_at": datetime.now(UTC).isoformat(),
        "environment": {
            "hardware": hardware(),
            "ollama_resident": resident,
            "llm_model": LLM_MODEL,
            "ambient_model": AMBIENT_LLM_MODEL,
            "database_note": db_note,
            "database_path": str(db_path),
            "warmed": warmed,
            "storm": args.storm,
            "server": "none, in-process ASGI transport",
        },
        "summary": summarize(records),
        "records": records,
    }

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = Path(args.out) if args.out else (REPO_ROOT / "docs" / "perf" / f"wedge-{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    md = write_report(out, path)

    print(f"\n→ {path}\n→ {md}")
    print(json.dumps(out["summary"], indent=2))

    # The copy is 430 MB. Removed unless --keep, which is the flag that exists
    # precisely so the generated drafts can be read afterwards.
    if _SCRATCH is not None and not args.keep:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
    elif _SCRATCH is not None:
        print(
            f"[wedge_bench] --keep: bench database left at {_SCRATCH} "
            f"({_SCRATCH.stat().st_size if _SCRATCH.is_file() else '430 MB'}) — "
            f"delete it when you are done"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
