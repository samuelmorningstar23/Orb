"""Cross-facility leak sweep (multisite-5b, wave 11, 20 October 2026).

Plants one facility 1 patient with identifiers nothing else in Orb holds (a
name, a hospital number, a diagnosis, a medicine, an allergy, an order and an
operation), adds a second facility with its own department, and then calls
every GET route the application serves, and an enumerated list of POST read
lanes (Sage with the model stubbed to echo the context it was given, the
registration match and identity check, the duplicate sweep, the downtime pack
build, the NEWS2 preview and the NABH evidence export), as five facility 2
accounts: a ward doctor and a ward nurse of facility 2's department, and a
department-less administrator, pharmacist and cashier whose home facility is
facility 2. A planted identifier in an answer, other than one the request
itself carried, is a leak.

  * Route groups the fence lets through while more than one facility is on
    record (signing in, health, facility identity, the operations page,
    /api/pulse, and the routes outside the fence's roots) are STRICT: a leak
    fails the suite.
  * Blind route groups are called with the fence opened (FACILITY_BLIND_SURFACES
    = ()) to show what each would disclose, and are xfail (not strict) until
    their owning sweep clears them; a sweep turns its rows strict by moving the
    group to 'cleared' in tests/facility_sql_sites.
  * With the real lists and two facilities, every blind route and every POST
    lane answers the fence's 503 before its handler runs (strict).

The sweep only finds what it planted; tests/test_facility_sql_registry.py is
the net for statements it cannot reach. The module owns its database
(isolated_module_database), so the second facility never reaches the session
database. Test patients carry invented names (never a word a clinician types).
Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
from auth import create_token
from database import db_session
from services import facility_service as FS
from services.clinical_time import utc_iso_now
from tests import facility_sql_sites as R

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9411                       # an id no other module writes
F2_CODE = f"LS{TAG[:4]}"
F1_DEPT = f"Quorvane Medicine {TAG}"
F2_DEPT = f"Lekswe Medicine {TAG}"

PID = f"LSWP{TAG}"
AID = f"LSWA{TAG}"
OID = f"ORD-LSW{TAG}"
SID = f"SURG-LSW{TAG}"
CHANNEL = f"case-lsw-{TAG.lower()}"
NAME = "Quorvexa Thimbleby"
MRN = f"LSWMRN{TAG}"
DIAGNOSIS = "Vandrelith fever"
DRUG = "Zorquinate"
ALLERGEN = "plimsorbate"
PROCEDURE = "Quelvorian graft"
ORDER_TEXT = "Thrennium panel"
WARD = f"Ward Q{TAG}"

#: What must never reach a facility 2 account.
PLANTED = (NAME, "Quorvexa", "Thimbleby", MRN, PID, AID, DIAGNOSIS, DRUG, ALLERGEN,
           PROCEDURE, ORDER_TEXT)

ROLES = (("doctor", F2_DEPT), ("nurse", F2_DEPT), ("admin", None), ("pharmacist", None),
         ("cashier", None))

client = TestClient(main.app, raise_server_exceptions=False)


# ── the routes ───────────────────────────────────────────────────────────────

def _get_routes() -> list:
    out = []
    for r in main.app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "GET" not in methods or path.startswith("/socket.io"):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.path)


GET_ROUTES = _get_routes()

#: POST lanes that read: (id, path, body). Each sits on a blind group today.
POST_LANES = (
    ("sage-query-by-name", "/api/sage/query",
     {"query": f"What is the latest NEWS2 for {NAME}?"}),
    ("sage-query-by-id", "/api/sage/query",
     {"query": "Summarise this patient", "patient_id": PID}),
    ("registration-match", "/api/registration/match", {"name": NAME, "uhid": MRN}),
    ("registration-identity-check", "/api/registration/identity-check",
     {"patient_id": PID, "name": NAME, "uhid": MRN}),
    ("registration-duplicate-sweep", "/api/registration/duplicates/sweep", {}),
    ("downtime-capture-all", "/api/downtime/capture-all", None),
    ("news2-preview", f"/api/nurse/patients/{PID}/news2-preview",
     {"respiratory_rate": 18, "spo2": 96, "systolic_bp": 120, "heart_rate": 80,
      "temperature": 37.0, "consciousness": "alert", "on_oxygen": False}),
    ("nabh-export", "/api/nabh/export", {"include_patient_identifiers": True}),
    ("nabh-export-markdown", "/api/nabh/export.md", {"include_patient_identifiers": True}),
)


def _surface(path: str) -> str:
    return R.surface_for(path, R.surfaces()) or F.surface_of(path)


def _owner(path: str) -> str:
    row = R.surfaces().get(R.surface_for(path, R.surfaces()) or "")
    return row.owner if row and row.owner else "unclassified"


def _mark(path: str):
    if F.is_fenced(path):
        return pytest.mark.xfail(
            strict=False,
            reason=(f"blind route group {_surface(path)} ({_owner(path)}): fenced while more "
                    "than one facility is on record; called here with the fence opened"))
    return ()


GET_PARAMS = [pytest.param(r, id=f"GET {r.path}", marks=_mark(r.path)) for r in GET_ROUTES]
POST_PARAMS = [pytest.param(path, body, id=f"POST {lane}", marks=_mark(path))
               for lane, path, body in POST_LANES]


# ── the world ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(isolated_module_database):
    from services.helix_db import init_helix_db
    init_helix_db()
    today = date.today().isoformat()
    users = {}
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Lekswe Satellite"))
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                   (F1_DEPT,))
        db.execute("INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                   (F2_DEPT, F2))
        for role, dept in ROLES:
            uid = f"LSW{role[:3].upper()}{TAG}"
            db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                       "VALUES (?, ?, ?, ?, 'x')", (uid, f"Lekswe {role.title()}", role, dept))
            if dept is None:
                db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                           "granted_at, reason) VALUES (?, ?, 'home', 'system', "
                           "CURRENT_TIMESTAMP, 'leak sweep')", (uid, F2))
            users[role] = uid
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
            "admission_date, registered_facility_id) VALUES (?, ?, ?, 61, 'F', ?, 'Q-7', ?, "
            "'admitted', ?, 1)", (PID, NAME, MRN, WARD, DIAGNOSIS, today))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
            "facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'Q-7', 'active', 1, ?)",
            (AID, PID, F1_DEPT, WARD, utc_iso_now()))
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, carried_from) "
            "VALUES (?, ?, 128, 92, 58, 89, 38.9, 27, 'alert', NULL)", (PID, utc_iso_now()))
        cur = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, ?, '5 mg', 'oral', 'OD', 'active', 'system', "
            "'clinician')", (PID, DRUG))
        med_id = cur.lastrowid
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, status, "
            "source, recorded_by) VALUES (?, ?, 'rash', 'mild', 'active', 'clinician', 'system')",
            (PID, ALLERGEN))
        db.execute(
            "INSERT INTO orders (id, patient_id, order_type, description, status, ordered_by) "
            "VALUES (?, ?, 'lab', ?, 'active', 'system')", (OID, PID, ORDER_TEXT))
        db.execute(
            "INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, "
            "scheduled_date, scheduled_time, status) VALUES (?, ?, 'system', ?, ?, ?, '09:00', "
            "'scheduled')", (SID, PID, PROCEDURE, DIAGNOSIS, today))
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (CHANNEL, f"{NAME} case room", PID))
    FS.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    try:
        yield {"users": users, "med_id": str(med_id),
               "headers": {role: {"Authorization": f"Bearer {create_token(uid, role)}"}
                           for role, uid in users.items()}}
    finally:
        FS.invalidate_cache()
        F._audit_last.clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    """The blind list as it will stand once every screen is swept."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def model_echoes_context(monkeypatch):
    """The model lanes stubbed to answer with the context Orb assembled for
    them, so a patient the lane should never have read shows up in the answer
    (and no request waits on a model server): Sage's query stream and the
    surgical risk stream."""
    import routes.sage_routes as sage_routes
    import routes.surgery_routes as surgery_routes

    async def echo(prompt, context="", conversation_history=None, scope="simple", **kwargs):
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model="leak-sweep-echo")
        yield "NOACT|Context I was given: " + " ".join(str(context or "").split())[:8000]

    async def risk_echo(surgery_id, detail=None):
        yield "data: " + json.dumps({"context": detail}, default=str) + "\n\n"

    monkeypatch.setattr(sage_routes, "query_ollama", echo)
    monkeypatch.setattr(sage_routes, "_save_turn", lambda *a, **k: None)
    monkeypatch.setattr(surgery_routes, "generate_risk_assessment", risk_echo)


# ── filling a request ────────────────────────────────────────────────────────

SEARCH_PARAMS = {"q", "query", "search", "term", "text", "name"}
DAY_PARAMS = {"day", "date", "visit_date", "on", "scheduled_date"}


def _path_values(world) -> dict:
    return {"patient_id": PID, "pid": PID, "admission_id": AID, "order_id": OID,
            "surgery_id": SID, "med_id": world["med_id"], "medication_id": world["med_id"],
            "channel_id": CHANNEL, "mrn": MRN, "uhid": MRN, "ward": WARD, "facility_id": "1",
            "filename": "nothing.png", "user_id": "D001"}


def _query_value(name: str, required: bool):
    if name in ("patient_id", "pid"):
        return PID
    if name in ("uhid", "mrn"):
        return MRN
    if name in SEARCH_PARAMS:
        return MRN
    if name in ("ward",):
        return WARD
    if name in DAY_PARAMS:
        return date.today().isoformat()
    return "1" if required else None


def _required(field) -> bool:
    req = getattr(field, "required", None)
    if isinstance(req, bool):
        return req
    info = getattr(field, "field_info", None)
    return bool(info is not None and info.is_required())


def _url(route, world) -> str:
    values = _path_values(world)
    path = route.path

    def fill(m):
        name = m.group(1).split(":")[0]
        return str(values.get(name, "1"))

    path = re.sub(r"\{([^}]+)\}", fill, path)
    query = {}
    dependant = getattr(route, "dependant", None)
    for field in (getattr(dependant, "query_params", None) or []):
        name = getattr(field, "alias", None) or field.name
        value = _query_value(name, _required(field))
        if value is not None:
            query[name] = value
    if query:
        from urllib.parse import urlencode
        path += "?" + urlencode(query)
    return path


def _leaks(text: str, sent: str) -> list:
    low, sent_low = text.lower(), sent.lower()
    return [p for p in PLANTED if p.lower() in low and p.lower() not in sent_low]


def _sweep(method: str, url: str, body, world) -> list:
    found = []
    sent = url + (json.dumps(body) if body is not None else "")
    for role, headers in world["headers"].items():
        if method == "GET":
            resp = client.get(url, headers=headers)
        else:
            resp = client.post(url, headers=headers, json=body)
        hits = _leaks(resp.content.decode("utf-8", "ignore"), sent)
        if hits:
            found.append(f"{role} ({resp.status_code}): {', '.join(hits)}")
    return found


# ── 1. The sweep ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("route", GET_PARAMS)
def test_no_facility_two_account_reads_the_facility_one_patient(route, world, fence_open,
                                                                model_echoes_context):
    url = _url(route, world)
    found = _sweep("GET", url, None, world)
    assert found == [], f"GET {url} disclosed facility 1's patient to: " + "; ".join(found)


@pytest.mark.parametrize("path, body", POST_PARAMS)
def test_no_post_read_lane_gives_facility_two_the_facility_one_patient(
        path, body, world, fence_open, model_echoes_context):
    found = _sweep("POST", path, body, world)
    assert found == [], f"POST {path} disclosed facility 1's patient to: " + "; ".join(found)


# ── 2. Meanwhile the fence holds every blind route shut ──────────────────────

def test_the_fence_answers_every_blind_route_before_its_handler(world):
    headers = world["headers"]["admin"]
    calls = [("GET", _url(r, world), None) for r in GET_ROUTES if F.is_fenced(r.path)]
    calls += [("POST", path, body) for _, path, body in POST_LANES]
    assert len(calls) > 300
    wrong = []
    for method, url, body in calls:
        resp = (client.get(url, headers=headers) if method == "GET"
                else client.post(url, headers=headers, json=body))
        if resp.status_code != 503 or resp.json().get("error") != "facility_fence":
            wrong.append(f"{method} {url}: {resp.status_code}")
        elif _leaks(resp.text, url):
            wrong.append(f"{method} {url}: the 503 itself named the patient")
    assert wrong == []


def test_the_sweep_world_is_what_it_says(world, fence_open):
    """The planted patient is really at facility 1 and every account at 2, so a
    clean answer means the scope held, not that the plant failed."""
    import permissions as P
    with db_session() as db:
        fid = db.execute(f"SELECT {P.FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?",
                         (PID,)).fetchone()[0]
        assert fid == 1
        for role, uid in world["users"].items():
            user = dict(db.execute("SELECT id, role, department FROM users WHERE id = ?",
                                   (uid,)).fetchone())
            assert FS.facility_scope_for(user, db=db) == frozenset({F2}), role
    # A facility 1 doctor does see the planted patient's chart: the plant works.
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, department, password_hash) "
                   "VALUES (?, 'Quorvane Doctor', 'doctor', ?, 'x')", (f"LSWF1D{TAG}", F1_DEPT))
    resp = client.get(f"/api/vigil/patients/{PID}",
                      headers={"Authorization": f"Bearer {create_token(f'LSWF1D{TAG}', 'doctor')}"})
    assert resp.status_code == 200, resp.text
    assert DIAGNOSIS in resp.text
    # ...and a facility 2 doctor is refused it (multisite-5a), with the fence open.
    resp = client.get(f"/api/vigil/patients/{PID}", headers=world["headers"]["doctor"])
    assert resp.status_code == 404
