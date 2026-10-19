"""Facility identity routes, validation and the create gate (multisite-2, W9-E).

  * With ORB_MULTI_SITE off (every install today) facility 1's identity can be
    read and edited (W162: invoices printed "Not configured" until it could be
    entered), while create, deactivate, the create check and editing any other
    facility answer 404 feature_disabled.
  * With it on, create is refused with its reasons and never succeeds while
    the facility fence holds its sentinel '*'; deactivating facility 1 is
    refused whatever else holds.
  * A bad GSTIN, a state code that is not the GSTIN's, a PAN that is not the
    GSTIN's and a PAN that differs from another facility's are refused, with
    a field-by-field list the screen can show.
  * Every identity edit writes one site_admin audit row carrying the diff; an
    edit that changes nothing writes none.

Facility 1 is put back as it was after every test, and any second facility a
test adds is removed with the cache invalidated (the TEST RULE in
docs/his_design/build/contracts.md), or every later patient route would 503.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence
from auth import create_token, hash_password
from database import db_session
from main import app
from services import facility_identity as FI
from services import facility_service as FS

client = TestClient(app)

GSTIN_MH = "27AAPFU0939F1ZV"          # PAN AAPFU0939F, Maharashtra
GSTIN_KA_SAME_PAN = "29AAPFU0939F1ZR"  # the same PAN, Karnataka
GSTIN_OTHER_PAN = "29AAGCB7383J1Z4"    # another legal entity


def _user(role: str, department=None) -> str:
    uid = f"FR{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password")))
    return uid


def _hdr(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture()
def admin():
    uid = _user("admin")
    return uid, _hdr(uid, "admin")


@pytest.fixture(autouse=True)
def _facilities_restored():
    with db_session() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(facilities)")]
        before = dict(zip(cols, db.execute("SELECT * FROM facilities WHERE id = 1").fetchone()))
        ids_before = {r[0] for r in db.execute("SELECT id FROM facilities")}
    FS.invalidate_cache()
    yield
    with db_session() as db:
        rest = [c for c in cols if c != "id"]
        db.execute(f"UPDATE facilities SET {', '.join(f'{c} = ?' for c in rest)} WHERE id = 1",
                   tuple(before[c] for c in rest))
        for (fid,) in db.execute("SELECT id FROM facilities").fetchall():
            if fid not in ids_before:
                db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
    FS.invalidate_cache()


@pytest.fixture()
def switch_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


@pytest.fixture()
def second_facility():
    """A second facilities row inserted by SQL, carrying another legal entity's
    PAN; removed after (the autouse fixture deletes it too)."""
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO facilities (uuid, code, name, gstin) VALUES (?, ?, 'Second Site', ?)",
            (str(uuid.uuid4()), f"S{uuid.uuid4().hex[:6].upper()}", GSTIN_OTHER_PAN))
        fid = int(cur.lastrowid)
    FS.invalidate_cache()
    yield fid
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
    FS.invalidate_cache()


def _audit_rows(action: str, since_id: int) -> list:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND id > ? ORDER BY id",
            (action, since_id)).fetchall()]


def _max_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


# ── 1. Switch off: facility 1 reads and edits ────────────────────────────────

def test_the_switch_is_off_by_default():
    assert config.feature_enabled("multi_site") is False


def test_facility_one_is_listed_and_read_with_the_switch_off(admin):
    _, hdr = admin
    resp = client.get("/api/facilities", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["multi_site"] is False and body["multi_facility_data"] is False
    main = next(f for f in body["facilities"] if f["id"] == 1)
    assert main["code"] == "MAIN" and main["is_main"] is True and main["is_active"] is True
    assert main["can_edit"] is True
    assert main["invoice_ready"] is False
    assert set(main["missing_for_invoice"]) >= {"Address", "GSTIN"}
    one = client.get("/api/facilities/1", headers=hdr)
    assert one.status_code == 200 and one.json()["id"] == 1
    assert client.get("/api/facilities/987654", headers=hdr).status_code == 404
    # The switch board keeps answering, declared before the id route.
    assert client.get("/api/facilities/meta", headers=hdr).status_code == 200


def test_facility_one_is_edited_with_the_switch_off(admin):
    _, hdr = admin
    resp = client.patch("/api/facilities/1", headers=hdr, json={
        "legal_name": "Sunrise Hospitals Private Limited", "address_line1": "12 MG Road",
        "city": "Pune", "state": "Maharashtra", "pincode": "411 001",
        "gstin": GSTIN_MH.lower(), "hfr_id": "IN2710000123", "nabh_valid_until": "2027-03-31",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gstin"] == GSTIN_MH            # upper-cased
    assert body["state_code"] == "27"           # filled from the GSTIN
    assert body["pincode"] == "411001"
    assert body["invoice_ready"] is True and body["missing_for_invoice"] == []
    assert body["address"] == "12 MG Road, Pune, Maharashtra 411001"
    assert body["updated_by"]
    # Blank clears a field.
    cleared = client.patch("/api/facilities/1", headers=hdr, json={"hfr_id": "  "})
    assert cleared.status_code == 200 and cleared.json()["hfr_id"] is None


def test_only_the_admin_reads_or_edits(admin):
    nurse = _user("nurse", "General Medicine")
    assert client.get("/api/facilities").status_code == 401
    assert client.get("/api/facilities", headers=_hdr(nurse, "nurse")).status_code == 403
    assert client.patch("/api/facilities/1", headers=_hdr(nurse, "nurse"),
                        json={"legal_name": "X"}).status_code == 403
    cashier = _user("cashier")
    assert client.get("/api/facilities/1", headers=_hdr(cashier, "cashier")).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("post", "/api/facilities"),
    ("post", "/api/facilities/1/deactivate"),
    ("get", "/api/facilities/create-check"),
])
def test_growth_answers_404_with_the_switch_off(admin, method, path):
    _, hdr = admin
    kwargs = {"json": {"code": "SAT1", "name": "Satellite"}} if method == "post" else {}
    resp = client.request(method.upper(), path, headers=hdr, **kwargs)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "feature_disabled"
    assert resp.json()["detail"]["feature"] == "multi_site"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 1


def test_another_facility_is_not_edited_with_the_switch_off(admin, second_facility):
    _, hdr = admin
    resp = client.patch(f"/api/facilities/{second_facility}", headers=hdr, json={"city": "Nashik"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"


# ── 2. Switch on: the create gate always refuses while the fence holds '*' ────

def test_create_is_refused_with_its_reasons_and_never_succeeds(admin, switch_on):
    _, hdr = admin
    check = client.get("/api/facilities/create-check", headers=hdr)
    assert check.status_code == 200
    assert check.json()["allowed"] is False
    codes = {r["code"] for r in check.json()["reasons"]}
    assert {"no_group_grant", "screens_not_ready", "audit_index_missing",
            "main_gstin_missing"} <= codes
    assert "switch_off" not in codes
    resp = client.post("/api/facilities", headers=hdr,
                       json={"code": "SAT1", "name": "Satellite", "gstin": GSTIN_KA_SAME_PAN})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "facility_create_refused"
    assert {r["code"] for r in detail["reasons"]} == codes
    assert all(r["message"] for r in detail["reasons"])
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM facilities").fetchone()[0] == 1


def test_the_fence_sentinel_alone_still_refuses(admin, switch_on, monkeypatch):
    """Everything else satisfied: a group-wide admin, facility 1's GSTIN and
    the audit index. The sentinel '*' still refuses."""
    uid, hdr = admin
    assert facility_fence.FACILITY_BLIND_SURFACES == facility_fence.SENTINEL
    with _gate_satisfied(uid):
        reasons = client.get("/api/facilities/create-check", headers=hdr).json()["reasons"]
        assert [r["code"] for r in reasons] == ["screens_not_ready"]
        assert client.post("/api/facilities", headers=hdr,
                           json={"code": "SAT1", "name": "Satellite"}).status_code == 409


def test_facility_one_can_never_be_closed(admin, switch_on):
    _, hdr = admin
    resp = client.post("/api/facilities/1/deactivate", headers=hdr, json={"reason": "Testing"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "main_facility_permanent"


class _gate_satisfied:
    """Every create-gate condition but the fence, undone on exit."""

    def __init__(self, uid: str):
        self.uid = uid

    def __enter__(self):
        now = datetime.now(timezone.utc).isoformat()
        with db_session() as db:
            db.execute("UPDATE facilities SET gstin = ?, state_code = '27' WHERE id = 1",
                       (GSTIN_MH,))
            db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, "
                       "reason) VALUES (?, 'test', ?, 'facility route test')", (self.uid, now))
            db.execute(f"CREATE INDEX IF NOT EXISTS {FI.AUDIT_FACILITY_INDEX} ON audit_logs("
                       "json_extract(metadata_json, '$.facility_id'), timestamp)")
        FS.invalidate_cache()
        return self

    def __exit__(self, *exc):
        with db_session() as db:
            db.execute("DELETE FROM enterprise_access_grants WHERE user_id = ?", (self.uid,))
            db.execute(f"DROP INDEX IF EXISTS {FI.AUDIT_FACILITY_INDEX}")
        FS.invalidate_cache()
        return False


def test_behind_an_open_gate_create_validates_audits_and_closing_waits(admin, switch_on,
                                                                       monkeypatch):
    """What the gate protects, exercised with the fence opened by hand (the
    product cannot do this yet): the new facility gets its audit epoch and an
    audit row, the PAN rule applies, and it cannot be closed while Orb cannot
    tell which outpatient visits and counters are its own."""
    uid, hdr = admin
    monkeypatch.setattr(facility_fence, "FACILITY_BLIND_SURFACES", ())
    since = _max_audit_id()
    with _gate_satisfied(uid):
        bad = client.post("/api/facilities", headers=hdr,
                          json={"code": "sat-1", "name": "", "gstin": GSTIN_OTHER_PAN})
        assert bad.status_code == 400
        assert {e["code"] for e in bad.json()["detail"]["errors"]} >= {
            "bad_code", "name_required", "pan_differs_from_group"}
        made = client.post("/api/facilities", headers=hdr,
                           json={"code": "sat1", "name": "Satellite", "gstin": GSTIN_KA_SAME_PAN})
        assert made.status_code == 200, made.text
        fid = made.json()["id"]
        assert made.json()["code"] == "SAT1" and made.json()["state_code"] == "29"
        assert made.json()["audit_epoch_id"] >= since
        rows = _audit_rows("facility_created", since)
        assert rows and rows[-1]["event_category"] == "site_admin"
        assert rows[-1]["target_id"] == str(fid)
        dup = client.post("/api/facilities", headers=hdr, json={"code": "SAT1", "name": "Again"})
        assert dup.status_code == 400
        assert "code_taken" in {e["code"] for e in dup.json()["detail"]["errors"]}
        closing = client.post(f"/api/facilities/{fid}/deactivate", headers=hdr,
                              json={"reason": "Satellite closed"})
        assert closing.status_code == 409, closing.text
        assert closing.json()["detail"]["error"] == "facility_has_open_work"
        assert "opd_visits_unattributed" in {r["code"] for r in closing.json()["detail"]["reasons"]}
        no_reason = client.post(f"/api/facilities/{fid}/deactivate", headers=hdr, json={})
        assert no_reason.status_code == 400


# ── 3. Validation ────────────────────────────────────────────────────────────

def _errors(resp) -> dict:
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_identity"
    return {e["field"]: e["code"] for e in detail["errors"]}


def test_a_bad_gstin_is_refused(admin):
    _, hdr = admin
    for value in ("27AAPFU0939F1ZX", "27AAPFU0939F1Z", "99AAPFU0939F1ZV", "not a gstin"):
        resp = client.patch("/api/facilities/1", headers=hdr, json={"gstin": value})
        assert _errors(resp) == {"gstin": "bad_gstin"}, value
    with db_session() as db:
        assert db.execute("SELECT gstin FROM facilities WHERE id = 1").fetchone()[0] is None


def test_a_state_code_that_is_not_the_gstins_is_refused(admin):
    _, hdr = admin
    resp = client.patch("/api/facilities/1", headers=hdr,
                        json={"gstin": GSTIN_MH, "state_code": "29"})
    assert _errors(resp) == {"state_code": "state_mismatch"}
    ok = client.patch("/api/facilities/1", headers=hdr, json={"gstin": GSTIN_MH})
    assert ok.status_code == 200
    # Moving the GSTIN to another state leaves the stored code behind: refused.
    resp = client.patch("/api/facilities/1", headers=hdr, json={"gstin": GSTIN_KA_SAME_PAN})
    assert _errors(resp) == {"state_code": "state_mismatch"}
    assert _errors(client.patch("/api/facilities/1", headers=hdr,
                                json={"state_code": "42"})) == {"state_code": "bad_state_code"}


def test_a_pan_that_is_not_the_gstins_is_refused(admin):
    _, hdr = admin
    resp = client.patch("/api/facilities/1", headers=hdr,
                        json={"gstin": GSTIN_MH, "pan": "AAGCB7383J"})
    assert _errors(resp) == {"pan": "pan_mismatch"}
    assert _errors(client.patch("/api/facilities/1", headers=hdr,
                                json={"pan": "ABC"})) == {"pan": "bad_pan"}
    ok = client.patch("/api/facilities/1", headers=hdr, json={"gstin": GSTIN_MH, "pan": "aapfu0939f"})
    assert ok.status_code == 200 and ok.json()["pan"] == "AAPFU0939F"


def test_one_pan_across_every_facility(admin, second_facility):
    _, hdr = admin
    resp = client.patch("/api/facilities/1", headers=hdr, json={"gstin": GSTIN_MH})
    assert _errors(resp) == {"gstin": "pan_differs_from_group"}
    resp = client.patch("/api/facilities/1", headers=hdr, json={"pan": "AAPFU0939F"})
    assert _errors(resp) == {"pan": "pan_differs_from_group"}


def test_the_code_is_fixed_and_unknown_fields_are_refused(admin):
    _, hdr = admin
    assert _errors(client.patch("/api/facilities/1", headers=hdr,
                                json={"code": "OTHER"})) == {"code": "code_fixed"}
    assert client.patch("/api/facilities/1", headers=hdr,
                        json={"is_active": 0}).status_code == 422


def test_other_fields_are_checked(admin):
    _, hdr = admin
    errors = _errors(client.patch("/api/facilities/1", headers=hdr, json={
        "pincode": "012345", "email": "accounts", "phone": "call me",
        "nabh_valid_until": "31/03/2027", "hfr_id": "!!", "legal_name": "x" * 201,
    }))
    assert errors == {"pincode": "bad_pincode", "email": "bad_email", "phone": "bad_phone",
                      "nabh_valid_until": "bad_date", "hfr_id": "bad_registration_number",
                      "legal_name": "too_long"}


def test_the_timezone_must_be_the_computers(admin):
    from zoneinfo import ZoneInfo
    from services.hospital_time import hospital_timezone
    _, hdr = admin
    probe = datetime(datetime.now(timezone.utc).year, 1, 15, 12, tzinfo=timezone.utc)
    here = probe.astimezone(hospital_timezone()).utcoffset()
    candidates = ("Asia/Kolkata", "UTC", "America/New_York", "Europe/London", "Asia/Tokyo")
    other = next(z for z in candidates if probe.astimezone(ZoneInfo(z)).utcoffset() != here)
    assert _errors(client.patch("/api/facilities/1", headers=hdr,
                                json={"timezone": other})) == {"timezone": "bad_timezone"}
    assert _errors(client.patch("/api/facilities/1", headers=hdr,
                                json={"timezone": "Mars/Olympus"})) == {"timezone": "bad_timezone"}
    same = next((z for z in candidates
                 if all(datetime(probe.year, m, 15, 12, tzinfo=timezone.utc).astimezone(
                     ZoneInfo(z)).utcoffset() == datetime(probe.year, m, 15, 12,
                                                          tzinfo=timezone.utc).astimezone(
                     hospital_timezone()).utcoffset() for m in (1, 7))), None)
    if same:
        assert client.patch("/api/facilities/1", headers=hdr,
                            json={"timezone": same}).status_code == 200


def test_gstin_check_character_matches_published_examples():
    assert FI.gstin_check_character("27AAPFU0939F1Z") == "V"
    assert FI.gstin_check_character("29AAGCB7383J1Z") == "4"
    assert FI.gstin_problem(GSTIN_MH) is None


# ── 4. Audit ─────────────────────────────────────────────────────────────────

def test_an_edit_writes_one_site_admin_row_with_the_diff(admin):
    uid, hdr = admin
    since = _max_audit_id()
    resp = client.patch("/api/facilities/1", headers=hdr,
                        json={"legal_name": "Audit Hospitals LLP", "city": "Pune"})
    assert resp.status_code == 200
    rows = _audit_rows("facility_updated", since)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "site_admin" and row["outcome"] == "success"
    assert row["actor_user_id"] == uid and row["target_type"] == "facility"
    assert row["target_id"] == "1"
    meta = json.loads(row["metadata_json"])
    assert meta["facility_id"] == 1 and meta["code"] == "MAIN"
    assert meta["changes"] == {"legal_name": {"from": None, "to": "Audit Hospitals LLP"},
                               "city": {"from": None, "to": "Pune"}}
    # The same values again change nothing and write nothing.
    again = _max_audit_id()
    assert client.patch("/api/facilities/1", headers=hdr,
                        json={"legal_name": "Audit Hospitals LLP"}).status_code == 200
    assert _audit_rows("facility_updated", again) == []


def test_an_edit_reaches_the_cached_labels(admin):
    _, hdr = admin
    assert FS.facility_identity(1)["name"]       # warm the label cache
    client.patch("/api/facilities/1", headers=hdr, json={"name": "Cache Check Hospital"})
    assert FS.facility_identity(1)["name"] == "Cache Check Hospital"
