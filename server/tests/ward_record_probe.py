"""Boot Orb as an Orb Ward Record computer and call every GET route as each role.

Run as a script in a FRESH interpreter by tests/test_ward_record_profile_boot.py
(never collected by pytest itself: no test_ prefix). The caller sets the
switches under test in the environment (ORB_WARD_RECORD=true, or
ORB_MODELS_ENABLED=false alone); this script sets everything else a throwaway
boot needs, the way tests/conftest.py does, so it never touches orb.db.

What it does:
  1. A fresh temporary database (init_db), with one user per role that exists
     on a Ward Record computer (admin, doctor, nurse, ward_clerk, pharmacist),
     two admitted patients on one ward, each with a critical set of vitals, a
     stored risk_scores row carrying a marker driver, and the ward's
     observation interval.
  2. Every route a model could be reached through is made to fail and recorded
     (services/ollama_gateway post and stream, the two provider seams, any
     httpx or urllib request to the Ollama address, Whisper, the knowledge
     worker pool), BEFORE the app is imported.
  3. The real app boots through its lifespan (TestClient as a context
     manager), the background lanes the profile must keep score free are run
     once by hand (the simulator tick, the Sage insights sweep, the ward brief,
     the downtime snapshot and the escalation sweep), and then every GET route
     under /api is called as each role, with path parameters filled from the
     seeded rows.
  4. Prints one JSON object: every 500, every call a scoring entry point
     refused (vigil_service.SWITCHED_OFF_CALLS, so a caller that swallowed the
     exception is still caught), every response that carried the stored band's
     marker, and every model request attempted.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

_tmp = tempfile.NamedTemporaryFile(prefix="orb_test_wrprobe_", suffix=".db", delete=False)
_tmp.close()
os.environ["ORB_DB_PATH"] = _tmp.name
os.environ.setdefault("ORB_DEPLOYMENT_PROFILE", "dev")
os.environ["ORB_KNOWLEDGE_INPROCESS"] = "1"
os.environ["ORB_WATCHDOG_DIR"] = tempfile.mkdtemp(prefix="orb_test_wrprobe_wd_")
os.environ["ORB_FHIR_EXPORT_DIR"] = tempfile.mkdtemp(prefix="orb_test_wrprobe_fhir_")
os.environ["ORB_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="orb_test_wrprobe_up_")
# The Ward Record ward clerk needs the beds module.
os.environ.setdefault("ORB_ADT", "1")
# The simulator lane runs too, so its tick is checked under the profile.
os.environ.setdefault("ORB_INGESTION_MODE", "hybrid")

MARKER = "WRPROBE STORED BAND MARKER"
WARD = "WRPROBE WARD"
DEPT = "General Medicine"
ROLES = ("admin", "doctor", "nurse", "ward_clerk", "pharmacist")

MODEL_CALLS: list[str] = []

#: Routes that hand over EVERYTHING stored about a patient, by law, and so
#: still carry a band stored before the profile was switched on: the data
#: principal's export (DPDP Act right of access). Hiding stored rows there
#: would make the export incomplete. Nothing is scored to produce it.
EXPORTS_OF_STORED_DATA = {
    "/api/admin/compliance/data-principal/{patient_id}/export",
}


def _model_call(where: str):
    MODEL_CALLS.append(where + "\n" + "".join(traceback.format_stack(limit=10)[:-1]))
    raise RuntimeError(f"ward record probe: a model was called ({where})")


def _install_model_tripwires() -> None:
    """Fail and record every way a request can reach a model."""
    import httpx
    import urllib.request

    ollama = os.environ.get("ORB_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    ollama_hosts = {ollama, ollama.replace("localhost", "127.0.0.1")}

    def _is_ollama(url) -> bool:
        text = str(url)
        return any(text.startswith(h) for h in ollama_hosts) or ":11434" in text

    real_async_send = httpx.AsyncClient.send
    real_sync_send = httpx.Client.send

    async def _async_send(self, request, *a, **k):
        if _is_ollama(request.url):
            _model_call(f"httpx async {request.method} {request.url}")
        return await real_async_send(self, request, *a, **k)

    def _sync_send(self, request, *a, **k):
        if _is_ollama(request.url):
            _model_call(f"httpx {request.method} {request.url}")
        return real_sync_send(self, request, *a, **k)

    httpx.AsyncClient.send = _async_send
    httpx.Client.send = _sync_send

    real_urlopen = urllib.request.urlopen

    def _urlopen(url, *a, **k):
        target = getattr(url, "full_url", url)
        if _is_ollama(target):
            _model_call(f"urllib {target}")
        return real_urlopen(url, *a, **k)

    urllib.request.urlopen = _urlopen


def _install_gateway_tripwires() -> None:
    """After config imports: the gateway, the provider seams, Whisper and the
    knowledge workers. Each records the call; the gateway's own refusal (when
    the models switch is off) runs first, so a refused call is not a call."""
    from services import ollama_gateway
    real_post, real_stream = ollama_gateway.post, ollama_gateway.stream

    async def _post(*a, **k):
        _refuse = getattr(ollama_gateway, "refuse_when_models_off", None)
        if _refuse is not None:
            _refuse(k.get("purpose", "post"))
        _model_call(f"ollama_gateway.post {k.get('purpose')}")
        return await real_post(*a, **k)

    def _stream(*a, **k):
        _refuse = getattr(ollama_gateway, "refuse_when_models_off", None)
        if _refuse is not None:
            _refuse(k.get("purpose", "stream"))
        _model_call(f"ollama_gateway.stream {k.get('purpose')}")
        return real_stream(*a, **k)

    ollama_gateway.post = _post
    ollama_gateway.stream = _stream

    try:
        from services import knowledge_worker as kw

        def _pool(*a, **k):
            _model_call("knowledge_worker._ensure_pool")

        kw._ensure_pool = _pool
    except Exception:  # noqa: BLE001
        pass


def _seed() -> dict:
    from auth import hash_password
    from database import db_session, init_db
    from services.clinical_time import format_utc

    init_db()
    users = {}
    pw = hash_password("password")
    with db_session() as db:
        for role in ROLES:
            uid = f"WRP-{role.upper()}"
            db.execute(
                "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (uid, f"Probe {role}", role, DEPT, pw),
            )
            users[role] = uid
        now = datetime.now(timezone.utc)
        pids = []
        for n in (1, 2):
            pid = f"WRP-P{n}"
            pids.append(pid)
            db.execute(
                """INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex,
                       admission_date, attending_doctor_id)
                   VALUES (?, ?, ?, ?, ?, 'Pneumonia', 'admitted', 60, 'F', ?, ?)""",
                (pid, f"Probe Patient {n}", f"WRP-MRN-{n}", WARD, f"B-{n}",
                 format_utc(now - timedelta(hours=10)), users["doctor"]),
            )
            db.execute(
                """INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed,
                       attending_doctor_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (f"WRP-ADM-{n}", pid, users["doctor"], DEPT, WARD, f"B-{n}",
                 users["doctor"], format_utc(now - timedelta(hours=10))),
            )
            for minutes in (300, 30):
                db.execute(
                    """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                           diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                           on_oxygen, source)
                       VALUES (?, ?, 135, 85, 50, 88, 39.5, 30, 'alert', 0, 'nurse_manual')""",
                    (pid, format_utc(now - timedelta(minutes=minutes))),
                )
            # A band stored before the profile was switched on. No screen may
            # show it while scoring is off.
            db.execute(
                "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                "VALUES (?, ?, 0.9, 'critical', ?)",
                (pid, format_utc(now - timedelta(minutes=30)), json.dumps([MARKER])),
            )
        db.execute(
            "INSERT INTO ward_observation_settings (ward, interval_minutes, grace_minutes, set_by, set_at) "
            "VALUES (?, 240, 30, ?, ?)",
            (WARD, users["admin"], format_utc(now - timedelta(days=1))),
        )
    return {"users": users, "patients": pids}


def _path_values(seed: dict, role: str) -> dict:
    pid = seed["patients"][0]
    return {
        "patient_id": pid, "pid": pid, "ward": WARD, "ward_name": WARD,
        "department": DEPT, "dept": DEPT, "user_id": seed["users"][role],
        "admission_id": "WRP-ADM-1", "mrn": "WRP-MRN-1",
    }


def _fill(path: str, values: dict, int_params: set[str]) -> str:
    import re

    def _sub(m):
        name = m.group(1).split(":")[0]
        if name in values:
            return str(values[name])
        return "1" if name in int_params else "WRPROBE-NONE"

    return re.sub(r"\{([^}]+)\}", _sub, path)


def _streams(route) -> bool:
    """Routes that answer with an open-ended event stream are not called: a
    stream with nothing to stream never ends. Each is a model route, which the
    models switch already turns off; listed in the output."""
    import inspect
    try:
        src = inspect.getsource(route.endpoint)
    except (OSError, TypeError):
        return False
    return "text/event-stream" in src or "EventSourceResponse" in src


def main() -> None:
    _install_model_tripwires()
    import config  # noqa: F401  (the profile is forced here)
    _install_gateway_tripwires()
    seed = _seed()

    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from auth import create_token
    from main import app
    from services import vigil_service

    out = {"profile": {"ward_record": config.WARD_RECORD, "scoring": config.SCORING_ENABLED,
                       "models": config.MODELS_ENABLED},
           "server_errors": [], "scoring_calls": [], "marker_leaks": [],
           "model_calls": [], "lane_errors": [], "skipped_streams": [], "calls": 0}

    with TestClient(app, raise_server_exceptions=False) as client:
        # The background lanes, run once by hand rather than waited for. Each
        # must store what it stores and score nothing.
        def _lanes():
            from services import (bot_service, downtime_service, escalation_service,
                                  sage_proactive_service)
            from services.sage_narrator_service import ward_narrator
            yield "simulator tick", lambda: bot_service._generate_vitals(
                datetime.now(timezone.utc), 1)
            yield "Sage insights sweep", sage_proactive_service.evaluate_all_admitted
            yield "escalation sweep", escalation_service.sweep
            yield "downtime snapshots", lambda: downtime_service.capture_all_admitted("auto")
            yield "downtime ward packs", lambda: downtime_service.build_ward_print_packs("auto")
            yield "ward brief snapshot", ward_narrator._build_ward_snapshot
            yield "ward brief", lambda: ward_narrator.get_brief(None)

        try:
            lanes = list(_lanes())
        except Exception as e:  # noqa: BLE001
            lanes = []
            out["lane_errors"].append(f"lane import: {type(e).__name__}: {e}")
        for label, fn in lanes:
            before = len(vigil_service.SWITCHED_OFF_CALLS)
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            except Exception as e:  # noqa: BLE001
                out["lane_errors"].append(f"{label}: {type(e).__name__}: {e}")
            for call in vigil_service.SWITCHED_OFF_CALLS[before:]:
                out["scoring_calls"].append(f"lane {label}: {call}")

        routes = [r for r in app.routes
                  if isinstance(r, APIRoute) and "GET" in r.methods and r.path.startswith("/api")]
        for role in ROLES:
            token = create_token(seed["users"][role], role)
            headers = {"Authorization": f"Bearer {token}", "Host": "localhost"}
            values = _path_values(seed, role)
            for route in routes:
                if _streams(route):
                    if role == ROLES[0]:
                        out["skipped_streams"].append(route.path)
                    continue
                int_params = {p.name for p in route.dependant.path_params
                              if getattr(p, "type_", None) is int
                              or "int" in str(getattr(p.field_info, "annotation", ""))}
                url = _fill(route.path, values, int_params)
                before = len(vigil_service.SWITCHED_OFF_CALLS)
                t0 = time.monotonic()
                try:
                    resp = client.get(url, headers=headers)
                except Exception as e:  # noqa: BLE001
                    out["server_errors"].append(f"{role} GET {url}: raised {type(e).__name__}: {e}")
                    continue
                out["calls"] += 1
                if resp.status_code >= 500:
                    out["server_errors"].append(
                        f"{role} GET {url}: {resp.status_code} {resp.text[:300]}")
                if MARKER in resp.text and route.path not in EXPORTS_OF_STORED_DATA:
                    out["marker_leaks"].append(f"{role} GET {url}")
                for call in vigil_service.SWITCHED_OFF_CALLS[before:]:
                    out["scoring_calls"].append(f"{role} GET {url}: {call}")
                if time.monotonic() - t0 > 20:
                    out["lane_errors"].append(f"slow: {role} GET {url}")
        # Anything a background loop scored while the probe ran.
        attributed = sum(1 for c in out["scoring_calls"])
        if len(vigil_service.SWITCHED_OFF_CALLS) > attributed:
            for call in vigil_service.SWITCHED_OFF_CALLS[attributed:]:
                out["scoring_calls"].append(f"background: {call}")

    out["model_calls"] = MODEL_CALLS
    print("WRPROBE-RESULT " + json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            os.unlink(os.environ["ORB_DB_PATH"])
        except OSError:
            pass
