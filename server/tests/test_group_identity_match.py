"""Group identity match at registration (multisite-17, W12-E, wave 12).

A satellite desk sees only its own facility's patients, so a patient treated
at another facility of the group would be registered twice and their allergy
list split (ruling MS-R11 (a): one group record per person). POST
/api/frontdesk/group-match finds them across the whole group by four things
the patient states, answers who they are and nothing clinical, is audited
phi_access with the desk's reason and rate-limited, and gives a ticket with
which the desk registers their visit here; the visit then brings the record
into this facility's scope (ruling MS-R5 (a)).

Pinned here, on a database this module owns, with a second facility, the
multi-site switch on and the fence opened:

  * the gates: the switch off is 404 feature_disabled, one facility on record
    is 409 single_facility, an account with a department is 403 desk_only (and
    audited), a missing fact or reason is 422 naming the fields;
  * an F2 desk finds an F1 patient by name, age or date of birth, sex and
    phone, and the answer carries id, name, age, sex, group record number and
    registered facility code and a ticket, never a clinical or stated value;
  * one fact wrong is no match; every search, a no-match one included, writes
    one phi_access row with the reason and the ids it showed;
  * the rate limit holds (429, an authorization row) and counts from the audit
    trail;
  * with the ticket the F2 desk registers the visit, a clinical_access row
    records the chart moving into F2's scope, and F2's department-less staff
    can then open the chart; without the ticket, with another account's, with
    a forged or an expired one it is 404; a matched patient admitted at F1 now
    is 409;
  * without a match the desk still registers a patient of its own facility.

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
import main
from auth import create_token
from database import db_session
from services import facility_service as FS
from services import frontdesk_service as FDS

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9871                       # an id no other module writes
F2_CODE = f"GM{TAG[:4]}"
F1_DEPT = f"Haldrick Medicine {TAG}"
F2_DEPT = f"Sorrowby Medicine {TAG}"

NAME = "Zerelda Quonset"
PHONE = "98450 12345"

client = TestClient(main.app, raise_server_exceptions=False)


def _hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n].upper()


def _user(db, role: str, *, department=None, home=None) -> str:
    uid = f"GM{role[:3].upper()}{_hex(6)}"
    db.execute("INSERT INTO users (id, name, role, department, password_hash) "
               "VALUES (?, ?, ?, ?, 'x')", (uid, f"Ottilie Brannagh {uid[-4:]}", role, department))
    if home is not None:
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'group match')", (uid, home))
    return uid


def _patient(db, name: str, *, age=47, dob=None, sex="F", phone=PHONE, facility=1,
             department=F1_DEPT, active=False) -> str:
    pid = f"GMP{_hex(8)}"
    db.execute("INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, diagnosis, "
               "ward, bed, status, registered_facility_id) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, 'Thessaly fever', 'Ward G', '9', ?, ?)",
               (pid, name, f"UHID-{pid}", age, dob, sex, phone,
                "admitted" if active else "discharged", facility))
    if department:
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, facility_id, created_at) VALUES (?, ?, 'system', ?, 'Ward G', '9', "
                   "?, ?, ?)", (f"GMA{_hex(8)}", pid, department,
                                "active" if active else "discharged", facility,
                                "2026-08-01T08:00:00+00:00"))
    return pid


def _headers(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _search(world, who="desk2", **overrides):
    body = {"name": NAME, "age_years": 47, "sex": "F", "phone": PHONE,
            "reason": "Patient says she was treated at the main hospital"}
    body.update(overrides)
    body = {k: v for k, v in body.items() if v is not ...}
    return client.post("/api/frontdesk/group-match", json=body, headers=world["h"][who])


def _audit(action: str, actor: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute("SELECT event_category, outcome, patient_id, metadata_json "
                          "FROM audit_logs WHERE action_type = ? AND actor_user_id = ? "
                          "ORDER BY id", (action, actor)).fetchall()
    return [{**dict(r), "metadata": json.loads(r["metadata_json"] or "{}")} for r in rows]


@pytest.fixture(scope="module")
def world(isolated_module_database):
    FDS.ensure_frontdesk_schema(force=True)
    w: dict = {}
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Sorrowby Satellite"))
        for name, fid in ((F1_DEPT, 1), (F2_DEPT, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        w["doc1"] = _user(db, "doctor", department=F1_DEPT)
        w["doc2"] = _user(db, "doctor", department=F2_DEPT)
        w["desk2"] = _user(db, "admin", home=F2)
        w["desk2b"] = _user(db, "admin", home=F2)
        w["limited"] = _user(db, "admin", home=F2)
        w["pharm2"] = _user(db, "pharmacist", home=F2)
        # The F1 patient the satellite desk is talking to.
        w["pid"] = _patient(db, NAME)
        # Same phone and name, other sex: never a match.
        w["twin"] = _patient(db, NAME, sex="M")
        # An F1 inpatient right now, findable but not registrable at F2.
        w["inpatient"] = _patient(db, "Pellamy Ardwynne", phone="98450 67890", active=True)
        # A patient registered at F2 who has never had an encounter.
        w["local"] = _patient(db, "Isolde Varnaway", phone="98450 24680", facility=F2,
                              department=None)
        # An F1 patient for the OPD-mode registration (registration feature on).
        w["opd_pid"] = _patient(db, "Wenzeline Brackmore", phone="98450 11223")
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True
    w["h"] = {k: _headers(w[k], role) for k, role in (
        ("desk2", "admin"), ("desk2b", "admin"), ("limited", "admin"), ("doc2", "doctor"),
        ("pharm2", "pharmacist"))}
    try:
        yield w
    finally:
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def switched_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


# ── 1. The gates ─────────────────────────────────────────────────────────────

def test_the_switch_off_is_404_feature_disabled(world, monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    resp = _search(world)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"
    assert _audit(FDS.GROUP_MATCH_ACTION, world["desk2"]) == []


def test_one_facility_on_record_is_409_single_facility(world, monkeypatch):
    monkeypatch.setattr(FS, "multi_facility_data", lambda *a, **k: False)
    resp = _search(world)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "single_facility"


def test_an_account_with_a_department_is_403_and_audited(world):
    resp = _search(world, who="doc2")
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "desk_only"
    rows = _audit("permission_denied", world["doc2"])
    assert rows and rows[-1]["metadata"]["lane"] == "group_identity_match"
    assert _audit(FDS.GROUP_MATCH_ACTION, world["doc2"]) == []


@pytest.mark.parametrize("overrides, fields", [
    ({"phone": "12345"}, ["phone"]),
    ({"phone": "1234567890"}, ["phone"]),                 # an obvious placeholder
    ({"sex": "unknown"}, ["sex"]),
    ({"name": "Z"}, ["name"]),
    ({"age_years": None}, ["age_or_date_of_birth"]),
    ({"age_years": 400}, ["age_years"]),
    ({"date_of_birth": "not a date"}, ["date_of_birth"]),
])
def test_every_fact_is_required(world, overrides, fields):
    resp = _search(world, **overrides)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "group_match_incomplete"
    assert set(fields) <= set(detail["fields"])


def test_a_reason_is_required(world):
    resp = _search(world, reason="because")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "reason_required"


def test_an_extra_field_is_refused(world):
    resp = _search(world, uhid="UHID-anything")
    assert resp.status_code == 422


# ── 2. The match ─────────────────────────────────────────────────────────────

def test_an_f2_desk_finds_an_f1_patient_and_sees_no_clinical_field(world):
    resp = _search(world)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["id"] for m in body["matches"]] == [world["pid"]]
    match = body["matches"][0]
    assert set(match) == {"id", "name", "age", "sex", "group_mrn", "registered_facility",
                          "ticket"}
    assert match["name"] == NAME and match["age"] == 47 and match["sex"] == "F"
    assert match["group_mrn"] == f"UHID-{world['pid']}"
    assert match["registered_facility"] == {"id": 1, "code": "MAIN"}
    for clinical in ("Thessaly fever", "Ward G", PHONE, "98450"):
        assert clinical not in resp.text
    assert FDS.group_match_ticket_valid(match["ticket"], user_id=world["desk2"],
                                        patient_id=world["pid"])


def test_a_date_of_birth_matches_a_stated_age_within_two_years(world):
    resp = _search(world, age_years=None, date_of_birth="1978-01-15")
    assert resp.status_code == 200, resp.text
    assert world["pid"] in [m["id"] for m in resp.json()["matches"]]


@pytest.mark.parametrize("overrides", [
    {"phone": "98450 54321"},
    {"name": "Zerelda Pemberline"},
    {"age_years": 30},
    {"sex": "O"},
])
def test_one_fact_wrong_is_no_match(world, overrides):
    resp = _search(world, **overrides)
    assert resp.status_code == 200, resp.text
    assert world["pid"] not in [m["id"] for m in resp.json()["matches"]]


def test_fewer_names_than_the_record_still_match_but_never_a_different_one(world):
    resp = _search(world, name="Zerelda")
    assert world["pid"] in [m["id"] for m in resp.json()["matches"]]


def test_every_search_writes_a_phi_access_row_with_the_reason(world):
    before = len(_audit(FDS.GROUP_MATCH_ACTION, world["desk2b"]))
    _search(world, who="desk2b", reason="Treated at the main hospital last year")
    _search(world, who="desk2b", phone="98450 99999", reason="Second try with the other phone")
    rows = _audit(FDS.GROUP_MATCH_ACTION, world["desk2b"])
    assert len(rows) == before + 2
    first, second = rows[-2], rows[-1]
    assert first["event_category"] == "phi_access" and first["outcome"] == "success"
    assert first["metadata"]["reason"] == "Treated at the main hospital last year"
    assert first["metadata"]["matched_patient_ids"] == [world["pid"]]
    assert first["metadata"]["criteria"] == ["name", "age_years", "sex", "phone"]
    assert second["metadata"]["matched_patient_ids"] == []
    assert second["metadata"]["reason"] == "Second try with the other phone"
    # The stated phone is never written to the trail.
    assert "98450" not in json.dumps(first["metadata"])


def test_nothing_is_answered_when_the_search_cannot_be_recorded(world, monkeypatch):
    from services import audit_service

    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == FDS.GROUP_MATCH_ACTION:
            raise audit_service.AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    resp = _search(world, who="desk2b")
    assert resp.status_code == 503
    assert NAME not in resp.text


# ── 3. The rate limit ────────────────────────────────────────────────────────

def test_the_rate_limit_holds_and_counts_from_the_audit_trail(world, monkeypatch):
    monkeypatch.setattr(FDS, "GROUP_MATCH_LIMIT", 3)
    for _ in range(3):
        assert _search(world, who="limited").status_code == 200
    resp = _search(world, who="limited")
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["error"] == "group_match_rate_limited"
    assert detail["retry_after_seconds"] == 3600
    assert resp.headers.get("retry-after") == "3600"
    assert NAME not in resp.text
    assert len(_audit(FDS.GROUP_MATCH_ACTION, world["limited"])) == 3
    limited = _audit(FDS.GROUP_MATCH_LIMITED_ACTION, world["limited"])
    assert limited and limited[-1]["event_category"] == "authorization"
    # Another account is not limited by this one's searches.
    assert _search(world, who="desk2b").status_code == 200


# ── 4. The ticket and the visit ──────────────────────────────────────────────

def _visit_body(world, ticket=None, pid=None):
    body = {"patient_id": pid or world["pid"], "department": F2_DEPT}
    if ticket is not None:
        body["group_match_ticket"] = ticket
    return body


def test_the_ticket_is_bound_to_the_account_the_patient_and_the_time():
    now = time.time()
    t = FDS.group_match_ticket("U1", "P1", now=now)
    assert FDS.group_match_ticket_valid(t, user_id="U1", patient_id="P1", now=now)
    assert not FDS.group_match_ticket_valid(t, user_id="U2", patient_id="P1", now=now)
    assert not FDS.group_match_ticket_valid(t, user_id="U1", patient_id="P2", now=now)
    later = now + FDS.GROUP_MATCH_TICKET_SECONDS + 1
    assert not FDS.group_match_ticket_valid(t, user_id="U1", patient_id="P1", now=later)
    expires, mac = t.split(".")
    stretched = f"{int(expires) + 86400}.{mac}"
    assert not FDS.group_match_ticket_valid(stretched, user_id="U1", patient_id="P1", now=now)
    assert not FDS.group_match_ticket_valid("garbage", user_id="U1", patient_id="P1", now=now)
    assert not FDS.group_match_ticket_valid(None, user_id="U1", patient_id="P1", now=now)


def test_without_a_ticket_the_f1_patient_is_a_404(world):
    resp = client.post("/api/frontdesk/opd/visits", json=_visit_body(world),
                       headers=world["h"]["desk2"])
    assert resp.status_code == 404
    rows = [r for r in _audit("patient_scope_denied", world["desk2"])
            if r["patient_id"] == world["pid"]]
    assert rows and rows[-1]["metadata"]["scope_helper"] == "frontdesk_registration"


@pytest.mark.parametrize("which", ["other_account", "forged", "expired"])
def test_a_ticket_that_is_not_the_callers_own_is_a_404(world, which):
    if which == "other_account":
        ticket = FDS.group_match_ticket(world["desk2b"], world["pid"])
    elif which == "forged":
        ticket = f"{int(time.time()) + 600}.{'0' * 64}"
    else:
        ticket = FDS.group_match_ticket(world["desk2"], world["pid"],
                                        now=time.time() - FDS.GROUP_MATCH_TICKET_SECONDS - 60)
    resp = client.post("/api/frontdesk/opd/visits", json=_visit_body(world, ticket),
                       headers=world["h"]["desk2"])
    assert resp.status_code == 404


def test_a_matched_patient_admitted_at_f1_now_is_409(world):
    resp = _search(world, name="Pellamy Ardwynne", phone="98450 67890", sex="F", age_years=47)
    match = resp.json()["matches"][0]
    assert match["id"] == world["inpatient"]
    resp = client.post("/api/frontdesk/opd/visits",
                       json=_visit_body(world, match["ticket"], world["inpatient"]),
                       headers=world["h"]["desk2"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "cross_facility_move_refused"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?",
                          (world["inpatient"],)).fetchone()[0] == 0


def test_with_the_ticket_the_visit_brings_the_chart_into_f2s_scope(world):
    path = f"/api/frontdesk/patients/{world['pid']}/movements"
    # Before: the F2 department-less staff cannot open the F1 chart.
    assert client.get(path, headers=world["h"]["pharm2"]).status_code in (403, 404)
    assert client.get(path, headers=world["h"]["desk2"]).status_code == 404
    ticket = _search(world).json()["matches"][0]["ticket"]
    resp = client.post("/api/frontdesk/opd/visits", json=_visit_body(world, ticket),
                       headers=world["h"]["desk2"])
    assert resp.status_code == 200, resp.text
    visit = resp.json()
    assert FDS.opd_visit_facility_id(visit["id"]) == F2
    rows = _audit(FDS.GROUP_MATCH_VISIT_ACTION, world["desk2"])
    assert rows and rows[-1]["event_category"] == "clinical_access"
    assert rows[-1]["patient_id"] == world["pid"]
    assert rows[-1]["metadata"]["from_facility_id"] == 1
    assert rows[-1]["metadata"]["to_facility_id"] == F2
    # After (MS-R5 (a)): the latest encounter is at F2, so F2 opens the chart.
    import permissions as P
    with db_session() as db:
        assert P.facility_denial(db, world["pid"], frozenset({F2})) == (False, F2)
    assert client.get(path, headers=world["h"]["desk2"]).status_code == 200


def test_in_opd_mode_the_match_audit_lands_in_the_visits_own_transaction(world, monkeypatch):
    """With the registration and OPD features on, the visit carries the
    identity check (what the patient stated at the search) and its audit rows
    commit with it; the group match row is one of them."""
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    resp = _search(world, name="Wenzeline Brackmore", phone="98450 11223")
    assert resp.status_code == 200, resp.text
    ticket = resp.json()["matches"][0]["ticket"]
    body = _visit_body(world, ticket, world["opd_pid"])
    body["identity_check"] = {"name": "Wenzeline Brackmore", "age_years": 47,
                              "phone_last4": "1223"}
    resp = client.post("/api/frontdesk/opd/visits", json=body, headers=world["h"]["desk2"])
    assert resp.status_code == 200, resp.text
    rows = [r for r in _audit(FDS.GROUP_MATCH_VISIT_ACTION, world["desk2"])
            if r["patient_id"] == world["opd_pid"]]
    assert len(rows) == 1
    assert FDS.opd_visit_facility_id(resp.json()["id"]) == F2


def test_in_opd_mode_a_failing_match_audit_rolls_the_visit_back(world, monkeypatch):
    from services import audit_service

    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    with db_session() as db:
        pid = _patient(db, "Aldreth Pomfrey", phone="98450 44556")
    resp = _search(world, name="Aldreth Pomfrey", phone="98450 44556")
    ticket = resp.json()["matches"][0]["ticket"]
    real = audit_service.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == FDS.GROUP_MATCH_VISIT_ACTION:
            raise audit_service.AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit_service, "log_audit_event", failing)
    body = _visit_body(world, ticket, pid)
    body["identity_check"] = {"name": "Aldreth Pomfrey", "age_years": 47, "phone_last4": "4556"}
    resp = client.post("/api/frontdesk/opd/visits", json=body, headers=world["h"]["desk2"])
    assert resp.status_code == 503
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0


def test_without_a_match_the_desk_still_registers_its_own_patient(world):
    # A search that finds nobody...
    resp = _search(world, name="Isolde Varnaway", phone="98450 13579")
    assert resp.status_code == 200 and resp.json()["matches"] == []
    # ...does not stand in the way: a patient of the desk's own facility is
    # registered with no ticket at all.
    resp = client.post("/api/frontdesk/opd/visits", json=_visit_body(world, pid=world["local"]),
                       headers=world["h"]["desk2"])
    assert resp.status_code == 200, resp.text
    # An ordinary registration: no group match row for this patient.
    assert all(r["patient_id"] != world["local"]
               for r in _audit(FDS.GROUP_MATCH_VISIT_ACTION, world["desk2"]))
