"""Admitting into a master bed, or awaiting a bed (adt-5, W5-B, 7 October 2026).

What this file defends:

  1. FLAG OFF CHANGES NOTHING. With ORB_ADT off the admission answers 200
     with the same keys, writes the typed ward and bed, no ids, no
     admitted_at, no movement and no patient_movement audit; the new fields
     and the Idempotency-Key header are ignored; a missing bed field is still
     a 422 (with the module on too); the form's options carry no 'master'.
  2. ONE TRANSACTION. With ORB_ADT on the admission and its placement commit
     together: the 'admission' movement and its patient_movement audit, the
     ids and both text mirrors in the master's spelling. Two admissions to
     one bed at once give one 201 and one 409 with nothing left of the loser.
     An unwritable audit leaves nothing behind (503).
  3. REPLAYS. The same Idempotency-Key returns the first admission (200,
     replayed, no second password); a key reused for another patient is 409.
  4. AWAITING A BED, BACK-TIME. 'No bed yet' admits with no bed; a time
     further back than ORB_ADT_BACKTIME_HOURS or in the future is 400.
  5. RULINGS APPLIED STRICTEST: ADT-S1 (a) an occupied bed (held by id or by
     typed text) is refused and an extra bed with its own label is offered;
     ADT-S2 (c) a bed not recorded clean needs a reason, one awaiting a
     terminal clean needs a named doctor, nurse or administrator; an
     out-of-service bed needs a reason; a retired bed is refused; a boarder
     needs the acknowledgement, keeps the treating department and writes one
     clinical_access row and one case-room line.
  6. TEXT LANES. Scribe's quick admission typing 'gm-1' links to GM-1, and a
     refusal there answers in words, not a 500.
  7. POST /api/adt/admissions/{id}/bed: a bed for an admission awaiting one;
     refused when a bed is recorded unless it is a correction (expected bed
     and reason); Idempotency-Key required and replayed; scope and
     permission. Bed text alone resolves on the admission's recorded ward
     (master id, else typed text) and never writes the ward away; with no
     ward recorded it is 422 ward_required. A correction with no bed recorded
     is 409 no_bed_to_correct; one naming a different bed meets ADT-S2 (c)
     in full (reason, or terminal-clean authoriser, recorded on the
     movement); one linking typed text to its own master bed does not.
  8. ROUND 3 REVIEW. (a) A typed place not in bed setup that another admitted
     patient is recorded at is refused (ADT-S1 (a)) on every lane: the form,
     Scribe and the bed route; the module-off warning is no longer dropped
     with nothing in its place. (b) The terminal clean is read from the
     ledger as well as from housekeeping's task: an isolation bed or room, or
     a stay with isolation precautions that left since the last clean, needs
     the authoriser with no task raised; an empty precautions list is no
     precaution; out of service never hides it. (c) The authoriser ACTS: the
     sender when their role allows, or the person named confirming with their
     own signing PIN (wrong PIN 403, audited and counted; locked 429); a name
     alone is 409 terminal_clean_pin_required; the department must fit; the
     PIN is never stored.
"""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import bed_master_service as bm
from services import housekeeping_service as hk
from services import pin_security
from services.admission_service import admit_patient
from services.adt import core as adt_core
from services.audit_service import AuditWriteError

client = TestClient(main.app)
DEPT = "General Medicine"   # conftest seeds D001 and N001 in it

#: The keys the admission answered with before adt-5, and still does with the
#: module off.
FLAG_OFF_KEYS = {"success", "patient_id", "admission_id", "channel_id", "mrn",
                 "patient_login_id", "temporary_password", "date_of_birth_recorded",
                 "bed_warning"}

_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Every patient admitted here is discharged again, so no bed stays held
    and no board in a later module counts them."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'", [(p,) for p in _SEEDED])
    _SEEDED.clear()


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    yield


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = DEPT) -> dict:
    uid = f"AA{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Admit {role} {uid[-4:]}", role, department, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": department, "name": f"Admit {role}"}


def _with_pin(user: dict, pin: str = "246813") -> dict:
    """Give a test account a signing PIN (as POST /api/admin/users/{id}/pin does)."""
    with db_session() as db:
        db.execute("UPDATE users SET pin_hash = ? WHERE id = ?",
                   (pin_security.hash_pin(pin), user["id"]))
    return {**user, "pin": pin}


def _hdr(user: dict, key: str | None = None) -> dict:
    out = {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}
    if key:
        out["Idempotency-Key"] = key
    return out


DOCTOR = {"id": "D001", "role": "doctor", "department": DEPT, "name": "Dr. Test"}
_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin", None)
    return _ADMIN


def _ward(department: str = DEPT, labels=("1", "2", "3"), *, tracked: bool = False,
          code: str | None = None) -> dict:
    ward = bm.save_ward({"code": code or f"AD-{_tag()}", "department": department,
                         "cleaning_tracked": tracked}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _ready(bed_id: str) -> None:
    hk.transition_unit(bed_id, "cleaning_in_progress", actor_user_id="AAHK", reason="test")
    hk.transition_unit(bed_id, "vacant_ready", actor_user_id="AAHK", reason="test")


def _payload(**over) -> dict:
    body = {"name": f"Bed Admit {uuid.uuid4().hex[:6]}", "age": 47, "sex": "F",
            "admission_source": "er", "admission_type": "inpatient",
            "department": DEPT, "ward": "", "bed": "",
            "attending_doctor_id": "D001", "chief_complaint": "Fever"}
    body.update(over)
    return body


def _admit(body: dict, *, user: dict = DOCTOR, key: str | None = None):
    resp = client.post("/api/admissions/admit", headers=_hdr(user, key), json=body)
    if resp.status_code in (200, 201):
        _SEEDED.append(resp.json()["patient_id"])
    return resp


def _one(sql: str, params: tuple = ()) -> dict | None:
    with db_session() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _named(name: str) -> int:
    return _one("SELECT COUNT(*) AS n FROM patients WHERE name = ?", (name,))["n"]


def _movements(admission_id: str) -> list[dict]:
    return _all("SELECT * FROM patient_movements WHERE admission_id = ? ORDER BY rowid",
                (admission_id,))


def _audits(action: str, patient_id: str) -> list[dict]:
    return _all("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ?",
                (action, patient_id))


def _detail(resp) -> dict:
    return resp.json()["detail"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. Flag off changes nothing
# ═════════════════════════════════════════════════════════════════════════════

def test_flag_off_the_admission_rows_and_answer_are_unchanged(adt_off):
    ward = f"TW-{_tag()}"
    resp = _admit(_payload(ward=ward, bed="4"))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert set(out) == FLAG_OFF_KEYS
    adm = _one("SELECT * FROM admissions WHERE id = ?", (out["admission_id"],))
    assert (adm["ward"], adm["bed"]) == (ward, "4")
    assert adm["ward_id"] is None and adm["bed_id"] is None and adm["admitted_at"] is None
    pat = _one("SELECT ward, bed FROM patients WHERE id = ?", (out["patient_id"],))
    assert (pat["ward"], pat["bed"]) == (ward, "4")
    assert _movements(out["admission_id"]) == []
    assert _all("SELECT * FROM audit_logs WHERE event_category = 'patient_movement' "
                "AND patient_id = ?", (out["patient_id"],)) == []


def test_flag_off_the_new_fields_and_the_key_are_ignored(adt_off):
    ward = _ward(labels=("1",))
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    body = _payload(ward="Typed ward", bed="9", bed_id=_bed(ward["id"], "1")["id"],
                    ward_id=ward["id"], awaiting_bed=True, admitted_at=long_ago,
                    boarder_acknowledged=True, extra_bed=True)
    resp = _admit(body, key=f"k-{_tag()}")
    assert resp.status_code == 200, resp.text
    adm = _one("SELECT * FROM admissions WHERE id = ?", (resp.json()["admission_id"],))
    assert (adm["ward"], adm["bed"], adm["ward_id"], adm["bed_id"], adm["admitted_at"]) == \
        ("Typed ward", "9", None, None, None)


def test_flag_off_a_held_bed_is_still_admitted_with_the_warning(adt_off):
    ward = f"TW-{_tag()}"
    assert _admit(_payload(ward=ward, bed="2")).status_code == 200
    second = _admit(_payload(ward=ward, bed="2"))
    assert second.status_code == 200 and second.json()["bed_warning"]


@pytest.mark.parametrize("module_on", [False, True])
def test_a_missing_bed_field_is_still_422(monkeypatch, module_on):
    monkeypatch.setattr(config, "ADT_ENABLED", module_on)
    body = _payload()
    body.pop("bed")
    resp = client.post("/api/admissions/admit", headers=_hdr(DOCTOR), json=body)
    assert resp.status_code == 422


def test_flag_off_the_options_carry_no_master(adt_off):
    resp = client.get("/api/admissions/options", headers=_hdr(DOCTOR))
    assert resp.status_code == 200
    assert set(resp.json()) == {"departments", "default_department", "doctors", "wards", "beds"}


# ═════════════════════════════════════════════════════════════════════════════
#  2. One transaction
# ═════════════════════════════════════════════════════════════════════════════

def test_admitting_into_a_master_bed_writes_place_movement_and_audit(adt_on):
    ward = _ward(labels=("7",))
    bed = _bed(ward["id"], "7")
    resp = _admit(_payload(ward=ward["code"].lower(), bed="7", bed_id=bed["id"]))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["linked"] is True and out["location"]["bed"] == "7"
    assert out["temporary_password"] and out["replayed"] is False
    adm = _one("SELECT * FROM admissions WHERE id = ?", (out["admission_id"],))
    assert (adm["ward_id"], adm["bed_id"], adm["ward"], adm["bed"]) == \
        (ward["id"], bed["id"], ward["code"], "7")
    assert adm["admitted_at"] and adm["department"] == DEPT
    pat = _one("SELECT ward, bed FROM patients WHERE id = ?", (out["patient_id"],))
    assert (pat["ward"], pat["bed"]) == (ward["code"], "7")
    moves = _movements(out["admission_id"])
    assert [m["movement_type"] for m in moves] == ["admission"]
    assert moves[0]["to_bed_id"] == bed["id"] and moves[0]["id"] == out["movement_id"]
    assert len(_audits("patient_admitted_to_bed", out["patient_id"])) == 1
    room_lines = _all("SELECT content FROM messages m JOIN channels c ON c.id = m.channel_id "
                      "WHERE c.patient_id = ?", (out["patient_id"],))
    assert any(f"Ward: {ward['code']} | Bed: 7" in r["content"] for r in room_lines)


def test_two_admissions_to_one_bed_at_once_give_one_admission_and_one_refusal(adt_on):
    ward = _ward(labels=("1",))
    bed = _bed(ward["id"], "1")
    names = [f"Race One {_tag()}", f"Race Two {_tag()}"]
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def run(name: str) -> None:
        data = {**_payload(name=name, ward=ward["code"], bed="1", bed_id=bed["id"])}
        barrier.wait()
        try:
            results[name] = admit_patient(data, DOCTOR, idempotency_key=f"race-{name}")
        except Exception as exc:   # noqa: BLE001 - the loser's refusal is the point
            results[name] = exc

    threads = [threading.Thread(target=run, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    wins = [n for n, r in results.items() if isinstance(r, dict)]
    losses = [n for n, r in results.items() if isinstance(r, adt_core.AdtConflict)]
    assert len(wins) == 1 and len(losses) == 1, results
    _SEEDED.append(results[wins[0]]["patient_id"])
    assert results[losses[0]].code == "bed_occupied"
    assert _named(losses[0]) == 0
    assert _one("SELECT COUNT(*) AS n FROM users WHERE name = ?", (losses[0],))["n"] == 0
    held = _all("SELECT id FROM admissions WHERE bed_id = ? AND status = 'active'", (bed["id"],))
    assert len(held) == 1


def test_an_unwritable_movement_audit_leaves_nothing_behind(adt_on, monkeypatch):
    ward = _ward(labels=("1",))
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    name = f"Audit Gone {_tag()}"
    resp = _admit(_payload(name=name, ward=ward["code"], bed="1",
                           bed_id=_bed(ward["id"], "1")["id"]))
    assert resp.status_code == 503 and _detail(resp)["error"] == "audit_unavailable"
    assert _named(name) == 0
    assert _one("SELECT COUNT(*) AS n FROM users WHERE name = ?", (name,))["n"] == 0
    assert _one("SELECT COUNT(*) AS n FROM channels WHERE name = ?", (f"Case: {name}",))["n"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  3. Replays
# ═════════════════════════════════════════════════════════════════════════════

def test_a_replay_with_the_same_key_returns_the_first_admission(adt_on):
    ward = _ward(labels=("1",))
    key = f"adm-{uuid.uuid4().hex}"
    body = _payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"])
    first = _admit(body, key=key)
    assert first.status_code == 201, first.text
    again = client.post("/api/admissions/admit", headers=_hdr(DOCTOR, key), json=body)
    assert again.status_code == 200, again.text
    replay = again.json()
    assert replay["replayed"] is True and replay["temporary_password"] is None
    assert replay["admission_id"] == first.json()["admission_id"]
    assert replay["patient_id"] == first.json()["patient_id"]
    assert replay["location"]["bed_id"] == _bed(ward["id"], "1")["id"]
    assert _named(body["name"]) == 1


def test_a_key_reused_for_another_patient_is_refused_and_writes_nothing(adt_on):
    key = f"adm-{uuid.uuid4().hex}"
    assert _admit(_payload(awaiting_bed=True), key=key).status_code == 201
    other = _payload(awaiting_bed=True)
    resp = _admit(other, key=key)
    assert resp.status_code == 409 and _detail(resp)["error"] == "idempotency_key_reused"
    assert _named(other["name"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
#  4. Awaiting a bed, back-time
# ═════════════════════════════════════════════════════════════════════════════

def test_no_bed_yet_admits_to_the_ward_with_no_bed(adt_on):
    ward = _ward(labels=("1",))
    resp = _admit(_payload(ward=ward["code"], ward_id=ward["id"], awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["awaiting_bed"] is True and out["location"]["bed_id"] is None
    adm = _one("SELECT * FROM admissions WHERE id = ?", (out["admission_id"],))
    assert adm["ward_id"] == ward["id"] and adm["bed_id"] is None and adm["bed"] is None
    move = _movements(out["admission_id"])[0]
    assert move["movement_type"] == "admission" and '"awaiting_bed": true' in move["detail_json"]
    listed = client.get("/api/adt/awaiting-bed", headers=_hdr(_admin())).json()
    assert out["admission_id"] in {e["admission_id"] for e in listed["awaiting"]}


def test_no_ward_and_no_bed_still_writes_the_admission_movement(adt_on):
    resp = _admit(_payload(awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    moves = _movements(out["admission_id"])
    assert [m["movement_type"] for m in moves] == ["admission"]
    assert moves[0]["to_ward"] is None and moves[0]["to_department"] == DEPT
    assert len(_audits("patient_admitted_to_bed", out["patient_id"])) == 1


def test_no_bed_yet_with_a_bed_named_is_refused(adt_on):
    ward = _ward(labels=("1",))
    resp = _admit(_payload(ward=ward["code"], bed="1", awaiting_bed=True))
    assert resp.status_code == 422 and _detail(resp)["error"] == "awaiting_bed_with_bed"


def test_back_time_inside_the_window_is_stored_and_outside_it_is_400(adt_on):
    hours = config.ADT_BACKTIME_HOURS
    inside = (datetime.now(timezone.utc) - timedelta(hours=hours - 1)).replace(microsecond=0)
    resp = _admit(_payload(awaiting_bed=True, admitted_at=inside.isoformat()))
    assert resp.status_code == 201, resp.text
    adm = _one("SELECT admitted_at FROM admissions WHERE id = ?", (resp.json()["admission_id"],))
    assert adt_core.parse_instant(adm["admitted_at"]) == inside
    move = _movements(resp.json()["admission_id"])[0]
    assert adt_core.parse_instant(move["moved_at"]) == inside

    name = f"Too Far Back {_tag()}"
    outside = datetime.now(timezone.utc) - timedelta(hours=hours + 1)
    late = _admit(_payload(name=name, awaiting_bed=True, admitted_at=outside.isoformat()))
    assert late.status_code == 400 and _detail(late)["error"] == "beyond_backtime"
    assert _named(name) == 0
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    ahead = _admit(_payload(awaiting_bed=True, admitted_at=future.isoformat()))
    assert ahead.status_code == 400 and _detail(ahead)["error"] == "future_time"


# ═════════════════════════════════════════════════════════════════════════════
#  5. The rulings, strictest option
# ═════════════════════════════════════════════════════════════════════════════

def test_an_occupied_bed_is_refused_and_an_extra_bed_takes_its_own_label(adt_on):
    ward = _ward(labels=("12",))
    bed = _bed(ward["id"], "12")
    assert _admit(_payload(ward=ward["code"], bed="12", bed_id=bed["id"])).status_code == 201
    name = f"Second In Twelve {_tag()}"
    refused = _admit(_payload(name=name, ward=ward["code"], bed="12", bed_id=bed["id"]))
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_occupied" and _detail(refused)["extra_bed_offered"]
    assert _named(name) == 0

    extra = _admit(_payload(name=name, ward=ward["code"], bed="12", bed_id=bed["id"],
                            extra_bed=True))
    assert extra.status_code == 201, extra.text
    out = extra.json()
    assert out["location"]["extra_bed"] == "12A" and out["location"]["bed"] == "12A"
    unit = _bed(ward["id"], "12A")
    assert unit["is_temporary"] == 1 and unit["unit_code"] == f"{ward['code']}/12A"
    assert out["location"]["bed_id"] == unit["id"]
    events = _all("SELECT * FROM bed_master_events WHERE entity_id = ? AND action = "
                  "'extra_bed_added'", (unit["id"],))
    assert len(events) == 1
    assert '"extra_bed"' in _movements(out["admission_id"])[0]["detail_json"]


def test_an_extra_bed_beside_a_free_bed_is_refused(adt_on):
    ward = _ward(labels=("3",))
    resp = _admit(_payload(ward=ward["code"], bed="3", bed_id=_bed(ward["id"], "3")["id"],
                           extra_bed=True))
    assert resp.status_code == 409 and _detail(resp)["error"] == "extra_bed_not_needed"
    assert _all("SELECT id FROM housekeeping_units WHERE ward_id = ? AND is_temporary = 1",
                (ward["id"],)) == []


def test_a_bed_named_by_another_admissions_typed_text_is_refused(adt_on, monkeypatch):
    ward = _ward(labels=("5",))
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    typed = _admit(_payload(ward=ward["code"], bed="5"))
    assert typed.status_code == 200
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    resp = _admit(_payload(ward=ward["code"], bed="5", bed_id=_bed(ward["id"], "5")["id"]))
    assert resp.status_code == 409 and _detail(resp)["error"] == "bed_occupied"


def test_a_retired_bed_is_refused(adt_on):
    ward = _ward(labels=("1", "2"))
    bed = _bed(ward["id"], "2")
    bm.retire_bed(bed["id"], {"reason": "broken frame"}, actor=_admin())
    resp = _admit(_payload(ward=ward["code"], bed="2", bed_id=bed["id"]))
    assert resp.status_code == 409 and _detail(resp)["error"] == "bed_retired"


def test_an_out_of_service_bed_needs_a_typed_reason_which_is_recorded(adt_on):
    ward = _ward(labels=("1",))
    bed = _bed(ward["id"], "1")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET state = 'out_of_service' WHERE id = ?",
                   (bed["id"],))
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"])
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "out_of_service_reason_required"
    resp = _admit({**body, "out_of_service_reason": "Only bed with suction"})
    assert resp.status_code == 201, resp.text
    assert "Only bed with suction" in _movements(resp.json()["admission_id"])[0]["detail_json"]


def test_a_bed_not_recorded_clean_needs_a_reason_on_a_tracked_ward(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")   # registered beds start vacant_dirty
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"])
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    resp = _admit({**body, "not_clean_reason": "Emergency, bed wiped down"})
    assert resp.status_code == 201, resp.text
    assert "Emergency, bed wiped down" in _movements(resp.json()["admission_id"])[0]["detail_json"]


def test_a_clean_bed_on_a_tracked_ward_needs_nothing(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _ready(bed["id"])
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"]))
    assert resp.status_code == 201, resp.text


def test_a_ward_whose_cleaning_is_not_tracked_raises_nothing(adt_on):
    ward = _ward(labels=("1",), tracked=False)
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"]))
    assert resp.status_code == 201, resp.text


def test_a_bed_awaiting_a_terminal_clean_needs_a_named_authoriser(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    hk.raise_cleaning_request(unit_id=bed["id"], request_type="discharge_turnaround",
                              clean_type="terminal", actor_user_id="AAHK")
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                    not_clean_reason="a reason is not enough here")
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    clerk = _user("ward_clerk")
    by_clerk = _admit({**body, "terminal_clean_authorised_by": clerk["id"],
                       "terminal_clean_reason": "No other isolation bed"})
    assert by_clerk.status_code == 422
    assert _detail(by_clerk)["error"] == "terminal_clean_authoriser_invalid"
    nurse = _with_pin(_user("nurse"))
    ok = _admit({**body, "terminal_clean_authorised_by": nurse["id"],
                 "terminal_clean_authoriser_pin": nurse["pin"],
                 "terminal_clean_reason": "No other isolation bed"})
    assert ok.status_code == 201, ok.text
    detail = _movements(ok.json()["admission_id"])[0]["detail_json"]
    assert '"terminal_clean_override"' in detail
    assert f'"authorised_by": "{nurse["id"]}"' in detail
    assert '"confirmed_by": "signing_pin"' in detail


def test_a_boarder_needs_the_acknowledgement_and_keeps_the_treating_department(adt_on):
    host = f"Host Dept {_tag()}"
    _user("nurse", host)   # covers the host department, so the ward can be set up
    ward = _ward(department=host, labels=("1",))
    body = _payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"])
    refused = _admit(body)
    assert refused.status_code == 409
    detail = _detail(refused)
    assert detail["error"] == "boarder_acknowledgement_required"
    assert "will not be able to open this chart" in detail["message"]
    assert _named(body["name"]) == 0

    resp = _admit({**body, "boarder_acknowledged": True})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["boarder"] is True
    adm = _one("SELECT department FROM admissions WHERE id = ?", (out["admission_id"],))
    assert adm["department"] == DEPT
    assert len(_audits("boarder_placed", out["patient_id"])) == 1
    lines = _all("SELECT content FROM messages m JOIN channels c ON c.id = m.channel_id "
                 "WHERE c.patient_id = ? AND m.content LIKE 'Placed as a boarder%'",
                 (out["patient_id"],))
    assert len(lines) == 1


def test_an_unlisted_ward_is_admitted_unlinked_with_a_warning(adt_on):
    resp = _admit(_payload(ward=f"Nowhere {_tag()}", bed="4"))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["location"]["linked"] is False and out["location_warning"]
    adm = _one("SELECT ward_id, bed_id, bed FROM admissions WHERE id = ?", (out["admission_id"],))
    assert adm["ward_id"] is None and adm["bed_id"] is None and adm["bed"] == "4"


def test_the_options_list_master_beds_without_patient_data(adt_on):
    ward = _ward(labels=("1", "2"), tracked=True)
    _ready(_bed(ward["id"], "1")["id"])
    occupant = _payload(name=f"Occupant {_tag()}", ward=ward["code"], bed="1",
                        bed_id=_bed(ward["id"], "1")["id"])
    assert _admit(occupant).status_code == 201
    _user("ward_clerk")
    resp = client.get("/api/admissions/options", headers=_hdr(DOCTOR))
    assert resp.status_code == 200
    master = resp.json()["master"]
    entry = next(w for w in master["wards"] if w["id"] == ward["id"])
    beds = {b["bed_label"]: b for b in entry["beds"]}
    assert beds["1"]["occupied"] is True and beds["1"]["selectable"] is False
    assert beds["1"]["status_words"] == "Occupied"
    assert beds["2"]["needs"] == "not_clean_reason" and beds["2"]["selectable"] is True
    text = resp.text
    assert occupant["name"] not in text and "patient_id" not in text
    roles = {a["role"] for a in master["authorisers"]}
    assert roles <= set(adt_core.TERMINAL_CLEAN_AUTHORISER_ROLES)
    # Round 3: authorising is an act. The list is of people who can confirm
    # with their own signing PIN (so only those with one), never the caller,
    # who is offered as self_authoriser when their role allows.
    with_pin = _with_pin(_user("nurse"))
    without_pin = _user("nurse")
    again = client.get("/api/admissions/options", headers=_hdr(DOCTOR)).json()["master"]
    listed = {a["id"] for a in again["authorisers"]}
    assert with_pin["id"] in listed and without_pin["id"] not in listed
    assert DOCTOR["id"] not in listed
    assert again["self_authoriser"]["id"] == DOCTOR["id"]
    from services.admission_service import admission_choices
    clerk = _user("ward_clerk")
    assert admission_choices(DEPT, user=clerk)["master"]["self_authoriser"] is None
    assert "pin_hash" not in client.get("/api/admissions/options", headers=_hdr(DOCTOR)).text
    assert master["backtime_hours"] == config.ADT_BACKTIME_HOURS


def test_the_retired_ghost_create_stays_retired_with_the_module_on(adt_on):
    """adt-5's 'legacy create is 404 when on' is superseded by W1-D: the route
    is retired for good (410) whatever the module says."""
    resp = client.post("/api/vigil/patients", headers=_hdr(DOCTOR), json={"name": "Ghost"})
    assert resp.status_code == 410


# ═════════════════════════════════════════════════════════════════════════════
#  6. Text lanes
# ═════════════════════════════════════════════════════════════════════════════

def test_scribe_typing_a_ward_in_lower_case_links_to_the_master(adt_on):
    code = f"SC-{_tag()}"
    ward = _ward(labels=("7",), code=code)
    body = {"name": f"Scribe Typed {_tag()}", "age": 50, "sex": "M", "department": DEPT,
            "ward": f" {code.lower()} ", "bed": "7", "attending_doctor_id": "D001",
            "admission_source": "er", "chief_complaint": "Cough"}
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(DOCTOR), json=body)
    assert resp.status_code == 200, resp.text
    _SEEDED.append(resp.json()["patient_id"])
    adm = _one("SELECT ward, bed, ward_id, bed_id FROM admissions WHERE id = ?",
               (resp.json()["admission_id"],))
    assert (adm["ward"], adm["bed"]) == (code, "7")
    assert adm["ward_id"] == ward["id"] and adm["bed_id"] == _bed(ward["id"], "7")["id"]


def test_scribe_refused_by_the_master_answers_in_words(adt_on):
    ward = _ward(labels=("1",))
    first = _payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"])
    assert _admit(first).status_code == 201
    body = {"name": f"Scribe Second {_tag()}", "age": 50, "sex": "M", "department": DEPT,
            "ward": ward["code"], "bed": "1", "attending_doctor_id": "D001",
            "admission_source": "er", "chief_complaint": "Cough"}
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(DOCTOR), json=body)
    assert resp.status_code == 409 and _detail(resp)["error"] == "bed_occupied"
    assert _named(body["name"]) == 0


def test_scribe_with_an_unwritable_movement_audit_admits_nobody(adt_on, monkeypatch):
    ward = _ward(labels=("1",))
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    body = {"name": f"Scribe No Audit {_tag()}", "age": 50, "sex": "M", "department": DEPT,
            "ward": ward["code"], "bed": "1", "attending_doctor_id": "D001",
            "admission_source": "er", "chief_complaint": "Cough"}
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(DOCTOR), json=body)
    assert resp.status_code == 503 and _detail(resp)["error"] == "audit_unavailable"
    assert _named(body["name"]) == 0
    assert _all("SELECT id FROM admissions WHERE bed_id = ? AND status = 'active'",
                (_bed(ward["id"], "1")["id"],)) == []


# ═════════════════════════════════════════════════════════════════════════════
#  7. POST /api/adt/admissions/{id}/bed
# ═════════════════════════════════════════════════════════════════════════════

def _awaiting(ward: dict | None = None) -> dict:
    extra = {"ward": ward["code"], "ward_id": ward["id"]} if ward else {}
    resp = _admit(_payload(awaiting_bed=True, **extra))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _assign(admission_id: str, body: dict, *, user: dict | None = None, key: str | None = "auto"):
    if key == "auto":
        key = f"bed-{uuid.uuid4().hex}"
    return client.post(f"/api/adt/admissions/{admission_id}/bed",
                       headers=_hdr(user or _admin(), key), json=body)


def test_the_bed_route_is_404_with_the_module_off(adt_off):
    resp = _assign("ADM-NOPE", {"bed": "1"})
    assert resp.status_code == 404 and _detail(resp)["error"] == "module_disabled"


def test_a_bed_is_assigned_to_an_admission_awaiting_one(adt_on):
    ward = _ward(labels=("1", "2"))
    adm = _awaiting(ward)
    bed = _bed(ward["id"], "2")
    resp = _assign(adm["admission_id"], {"bed_id": bed["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["bed_id"] == bed["id"]
    moves = _movements(adm["admission_id"])
    assert [m["movement_type"] for m in moves] == ["admission", "bed_assigned"]
    assert len(_audits("bed_assigned", adm["patient_id"])) == 1


def test_the_bed_route_refuses_when_a_bed_is_recorded_unless_corrected(adt_on):
    ward = _ward(labels=("1", "2"))
    first = _admit(_payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"]))
    out = first.json()
    target = _bed(ward["id"], "2")
    refused = _assign(out["admission_id"], {"bed_id": target["id"]})
    assert refused.status_code == 409 and _detail(refused)["error"] == "bed_already_recorded"
    no_expected = _assign(out["admission_id"], {"bed_id": target["id"], "correction": True,
                                                "reason": "Wrong bed typed at the desk"})
    assert no_expected.status_code == 422
    assert _detail(no_expected)["error"] == "expected_bed_required"
    stale = _assign(out["admission_id"], {"bed_id": target["id"], "correction": True,
                                          "reason": "Wrong bed", "expected_bed_id": "HKU-OTHER"})
    assert stale.status_code == 409 and _detail(stale)["error"] == "state_changed"
    fixed = _assign(out["admission_id"], {"bed_id": target["id"], "correction": True,
                                          "reason": "Wrong bed typed at the desk",
                                          "expected_bed_id": out["location"]["bed_id"]})
    assert fixed.status_code == 200, fixed.text
    moves = _movements(out["admission_id"])
    assert moves[-1]["movement_type"] == "correction"
    assert moves[-1]["reason"].startswith("Bed corrected: Wrong bed typed")


def test_the_bed_route_needs_a_key_and_replays_it(adt_on):
    ward = _ward(labels=("1",))
    adm = _awaiting(ward)
    bed = _bed(ward["id"], "1")
    missing = _assign(adm["admission_id"], {"bed_id": bed["id"]}, key=None)
    assert missing.status_code == 422 and _detail(missing)["error"] == "idempotency_key_required"
    key = f"bed-{uuid.uuid4().hex}"
    first = _assign(adm["admission_id"], {"bed_id": bed["id"]}, key=key)
    assert first.status_code == 200, first.text
    again = _assign(adm["admission_id"], {"bed_id": bed["id"]}, key=key)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert len(_movements(adm["admission_id"])) == 2


def test_the_bed_route_applies_the_cleaning_rule(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    adm = _awaiting(ward)
    bed = _bed(ward["id"], "1")
    refused = _assign(adm["admission_id"], {"bed_id": bed["id"]})
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    ok = _assign(adm["admission_id"], {"bed_id": bed["id"], "not_clean_reason": "Ward full"})
    assert ok.status_code == 200, ok.text


def test_the_bed_route_with_an_unwritable_audit_changes_nothing(adt_on, monkeypatch):
    ward = _ward(labels=("1",))
    adm = _awaiting(ward)
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    resp = _assign(adm["admission_id"], {"bed_id": _bed(ward["id"], "1")["id"]})
    assert resp.status_code == 503 and _detail(resp)["error"] == "audit_unavailable"
    row = _one("SELECT bed_id, bed FROM admissions WHERE id = ?", (adm["admission_id"],))
    assert row["bed_id"] is None and row["bed"] is None
    assert [m["movement_type"] for m in _movements(adm["admission_id"])] == ["admission"]


def test_an_extra_bed_from_the_bed_route_takes_its_own_label(adt_on):
    ward = _ward(labels=("8",))
    bed = _bed(ward["id"], "8")
    assert _admit(_payload(ward=ward["code"], bed="8", bed_id=bed["id"])).status_code == 201
    adm = _awaiting(ward)
    refused = _assign(adm["admission_id"], {"bed_id": bed["id"]})
    assert refused.status_code == 409 and _detail(refused)["error"] == "bed_occupied"
    resp = _assign(adm["admission_id"], {"bed_id": bed["id"], "extra_bed": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["bed"] == "8A"
    assert _bed(ward["id"], "8A")["is_temporary"] == 1


def test_the_bed_route_permission_and_scope(adt_on):
    ward = _ward(labels=("1", "2"))
    adm = _awaiting(ward)
    nurse = _user("nurse")
    assert _assign(adm["admission_id"], {"bed": "1"}, user=nurse).status_code == 403
    other = _user("ward_clerk", f"Other Dept {_tag()}")
    assert _assign(adm["admission_id"], {"bed": "1"}, user=other).status_code == 404
    clerk = _user("ward_clerk")
    resp = _assign(adm["admission_id"], {"ward": ward["code"], "bed": "1"}, user=clerk)
    assert resp.status_code == 200, resp.text
    assert resp.json()["bed_id"] == _bed(ward["id"], "1")["id"]


def test_bed_text_alone_links_to_that_bed_on_the_admissions_master_ward(adt_on):
    """Round 1 review: {"bed": "1"} for a patient awaiting a bed on a master
    ward used to resolve against no ward, writing the ward away and leaving
    the patient in an unlinked bed '1' on no ward."""
    ward = _ward(labels=("1", "2"))
    adm = _awaiting(ward)
    bed = _bed(ward["id"], "1")
    resp = _assign(adm["admission_id"], {"bed": "1"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ward_id"] == ward["id"] and out["bed_id"] == bed["id"]
    assert out["boarder"] is False
    row = _one("SELECT ward, ward_id, bed, bed_id FROM admissions WHERE id = ?",
               (adm["admission_id"],))
    assert row == {"ward": ward["code"], "ward_id": ward["id"], "bed": "1", "bed_id": bed["id"]}
    mirror = _one("SELECT ward, bed FROM patients WHERE id = ?", (adm["patient_id"],))
    assert mirror == {"ward": ward["code"], "bed": "1"}
    move = _movements(adm["admission_id"])[-1]
    assert move["movement_type"] == "bed_assigned"
    assert move["from_ward_id"] == ward["id"] and move["to_ward_id"] == ward["id"]
    assert move["to_bed_id"] == bed["id"]


def test_bed_text_alone_keeps_a_typed_ward_that_is_not_set_up(adt_on):
    typed = f"Nowhere {_tag()}"
    resp = _admit(_payload(ward=typed, awaiting_bed=True))
    assert resp.status_code == 201, resp.text
    adm = resp.json()
    out = _assign(adm["admission_id"], {"bed": "4"})
    assert out.status_code == 200, out.text
    row = _one("SELECT ward, ward_id, bed, bed_id FROM admissions WHERE id = ?",
               (adm["admission_id"],))
    assert row == {"ward": typed, "ward_id": None, "bed": "4", "bed_id": None}
    assert _one("SELECT ward, bed FROM patients WHERE id = ?",
                (adm["patient_id"],)) == {"ward": typed, "bed": "4"}


def test_bed_text_alone_with_no_ward_recorded_is_refused_and_writes_nothing(adt_on):
    adm = _awaiting()
    resp = _assign(adm["admission_id"], {"bed": "1"})
    assert resp.status_code == 422 and _detail(resp)["error"] == "ward_required"
    row = _one("SELECT ward, ward_id, bed, bed_id FROM admissions WHERE id = ?",
               (adm["admission_id"],))
    assert row == {"ward": None, "ward_id": None, "bed": None, "bed_id": None}
    assert [m["movement_type"] for m in _movements(adm["admission_id"])] == ["admission"]
    assert _audits("bed_assigned", adm["patient_id"]) == []


def test_a_correction_with_bed_text_alone_stays_on_the_recorded_ward(adt_on):
    ward = _ward(labels=("1", "2"))
    first = _admit(_payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"]))
    assert first.status_code == 201, first.text
    out = first.json()
    fixed = _assign(out["admission_id"], {"bed": "2", "correction": True,
                                          "reason": "Wrong bed typed at the desk",
                                          "expected_bed_id": out["location"]["bed_id"]})
    assert fixed.status_code == 200, fixed.text
    row = _one("SELECT ward, ward_id, bed, bed_id FROM admissions WHERE id = ?",
               (out["admission_id"],))
    assert row == {"ward": ward["code"], "ward_id": ward["id"], "bed": "2",
                   "bed_id": _bed(ward["id"], "2")["id"]}
    assert _movements(out["admission_id"])[-1]["movement_type"] == "correction"


# ── Round 2 review: a correction is no way round ADT-S2 (c) ─────────────────

def _terminal(bed_id: str) -> None:
    hk.raise_cleaning_request(unit_id=bed_id, request_type="discharge_turnaround",
                              clean_type="terminal", actor_user_id="AAHK")


def test_a_correction_with_no_bed_recorded_is_refused_and_writes_nothing(adt_on):
    """The reviewer's probe: a clerk sending correction for a patient with no
    bed used to put the patient into a bed awaiting its terminal clean with
    no named authoriser and nothing on the movement."""
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _terminal(bed["id"])
    adm = _awaiting(ward)
    clerk = _user("ward_clerk")
    plain = _assign(adm["admission_id"], {"bed_id": bed["id"]}, user=clerk)
    assert plain.status_code == 409
    assert _detail(plain)["error"] == "terminal_clean_authorisation_required"
    corrected = _assign(adm["admission_id"], {"bed_id": bed["id"], "correction": True,
                                              "expected_bed_id": None, "reason": "x"},
                        user=clerk)
    assert corrected.status_code == 409, corrected.text
    assert _detail(corrected)["error"] == "no_bed_to_correct"
    row = _one("SELECT ward, ward_id, bed, bed_id FROM admissions WHERE id = ?",
               (adm["admission_id"],))
    assert row["bed_id"] is None and row["bed"] is None
    assert [m["movement_type"] for m in _movements(adm["admission_id"])] == ["admission"]
    assert _audits("movement_corrected", adm["patient_id"]) == []


def test_a_correction_with_no_bed_recorded_is_refused_on_an_untracked_ward_too(adt_on):
    ward = _ward(labels=("1",), tracked=False)
    adm = _awaiting(ward)
    resp = _assign(adm["admission_id"], {"bed_id": _bed(ward["id"], "1")["id"],
                                         "correction": True, "expected_bed_id": None,
                                         "reason": "Wrong bed typed at the desk"})
    assert resp.status_code == 409 and _detail(resp)["error"] == "no_bed_to_correct"
    assert [m["movement_type"] for m in _movements(adm["admission_id"])] == ["admission"]


def _admitted_in_bed_1(ward: dict) -> dict:
    first = _bed(ward["id"], "1")
    _ready(first["id"])
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=first["id"]))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_correction_into_a_bed_awaiting_a_terminal_clean_needs_the_named_authoriser(adt_on):
    ward = _ward(labels=("1", "2"), tracked=True)
    out = _admitted_in_bed_1(ward)
    target = _bed(ward["id"], "2")
    _terminal(target["id"])
    base = {"bed_id": target["id"], "correction": True, "reason": "Wrong bed typed at the desk",
            "expected_bed_id": out["location"]["bed_id"]}
    clerk = _user("ward_clerk")
    refused = _assign(out["admission_id"], base, user=clerk)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    reason_only = _assign(out["admission_id"],
                          {**base, "not_clean_reason": "a reason is not enough here"}, user=clerk)
    assert reason_only.status_code == 409
    assert _detail(reason_only)["error"] == "terminal_clean_authorisation_required"
    by_clerk = _assign(out["admission_id"],
                       {**base, "terminal_clean_authorised_by": clerk["id"],
                        "terminal_clean_reason": "No other isolation bed"}, user=clerk)
    assert by_clerk.status_code == 422
    assert _detail(by_clerk)["error"] == "terminal_clean_authoriser_invalid"
    row = _one("SELECT bed_id FROM admissions WHERE id = ?", (out["admission_id"],))
    assert row["bed_id"] == out["location"]["bed_id"]
    assert [m["movement_type"] for m in _movements(out["admission_id"])] == ["admission"]
    # Round 3: naming a nurse is not the nurse authorising it.
    nurse = _with_pin(_user("nurse"))
    named_only = _assign(out["admission_id"],
                         {**base, "terminal_clean_authorised_by": nurse["id"],
                          "terminal_clean_reason": "No other isolation bed"}, user=clerk)
    assert named_only.status_code == 409, named_only.text
    assert _detail(named_only)["error"] == "terminal_clean_pin_required"
    assert [m["movement_type"] for m in _movements(out["admission_id"])] == ["admission"]
    ok = _assign(out["admission_id"],
                 {**base, "terminal_clean_authorised_by": nurse["id"],
                  "terminal_clean_authoriser_pin": nurse["pin"],
                  "terminal_clean_reason": "No other isolation bed"}, user=clerk)
    assert ok.status_code == 200, ok.text
    move = _movements(out["admission_id"])[-1]
    assert move["movement_type"] == "correction" and move["to_bed_id"] == target["id"]
    assert '"terminal_clean_override"' in move["detail_json"]
    assert f'"authorised_by": "{nurse["id"]}"' in move["detail_json"]
    assert '"confirmed_by": "signing_pin"' in move["detail_json"]
    assert "No other isolation bed" in move["detail_json"]
    audit = _audits("movement_corrected", out["patient_id"])
    assert len(audit) == 1 and "terminal_clean_override" in (audit[0]["metadata_json"] or "")


def test_a_correction_into_a_bed_not_recorded_clean_needs_the_reason(adt_on):
    ward = _ward(labels=("1", "2"), tracked=True)
    out = _admitted_in_bed_1(ward)
    target = _bed(ward["id"], "2")   # registered beds start vacant_dirty
    base = {"bed_id": target["id"], "correction": True, "reason": "Wrong bed typed at the desk",
            "expected_bed_id": out["location"]["bed_id"]}
    refused = _assign(out["admission_id"], base)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "bed_not_clean_reason_required"
    assert [m["movement_type"] for m in _movements(out["admission_id"])] == ["admission"]
    ok = _assign(out["admission_id"], {**base, "not_clean_reason": "Patient was already in it"})
    assert ok.status_code == 200, ok.text
    move = _movements(out["admission_id"])[-1]
    assert move["movement_type"] == "correction"
    assert "Patient was already in it" in move["detail_json"]
    assert '"bed_status": "needs_cleaning"' in move["detail_json"]


def test_a_correction_into_a_clean_bed_on_a_tracked_ward_needs_nothing_more(adt_on):
    ward = _ward(labels=("1", "2"), tracked=True)
    out = _admitted_in_bed_1(ward)
    target = _bed(ward["id"], "2")
    _ready(target["id"])
    resp = _assign(out["admission_id"], {"bed_id": target["id"], "correction": True,
                                         "reason": "Wrong bed typed at the desk",
                                         "expected_bed_id": out["location"]["bed_id"]})
    assert resp.status_code == 200, resp.text
    assert _movements(out["admission_id"])[-1]["movement_type"] == "correction"


def test_a_correction_linking_typed_text_to_its_own_bed_meets_no_cleaning_rule(adt_on):
    """A patient recorded in GM-x bed 1 by typed text only, corrected to the
    master bed that text names, is not put into a new bed: the cleaning rule
    is not applied, and the recorded-in check says so."""
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    other_ward = _ward(labels=("1",))
    other = _bed(other_ward["id"], "1")
    adm = _awaiting(ward)
    with db_session() as db:
        db.execute("UPDATE admissions SET ward = ?, bed = '1', bed_id = NULL WHERE id = ?",
                   (ward["code"], adm["admission_id"]))
    with db_session() as db:
        assert adt_core._recorded_in_on(db, admission_id=adm["admission_id"], ward=ward,
                                        bed=bed) is True
        assert adt_core._recorded_in_on(db, admission_id=adm["admission_id"], ward=other_ward,
                                        bed=other) is False
    resp = _assign(adm["admission_id"], {"bed_id": bed["id"], "correction": True,
                                         "expected_bed_id": None,
                                         "reason": "Typed at the desk, not linked"})
    assert resp.status_code == 200, resp.text
    move = _movements(adm["admission_id"])[-1]
    assert move["movement_type"] == "correction" and move["to_bed_id"] == bed["id"]
    assert "not_clean_reason" not in (move["detail_json"] or "")


# ═════════════════════════════════════════════════════════════════════════════
#  8. Round 3 review
# ═════════════════════════════════════════════════════════════════════════════

# ── (a) A typed place another admitted patient holds is refused ─────────────

def _scribe(ward: str, bed: str, name: str | None = None):
    body = {"name": name or f"Scribe Typed {_tag()}", "age": 50, "sex": "M", "department": DEPT,
            "ward": ward, "bed": bed, "attending_doctor_id": "D001",
            "admission_source": "er", "chief_complaint": "Cough"}
    resp = client.post("/api/scribe/quick-admit", headers=_hdr(DOCTOR), json=body)
    if resp.status_code == 200:
        _SEEDED.append(resp.json()["patient_id"])
    return resp, body


def test_a_typed_place_not_in_bed_setup_that_is_held_is_refused(adt_on):
    """The reviewer's probe: 'Nowhere X' bed 4 twice used to answer 201 and
    201 with bed_warning None. Refused now, in words, case-blind, with
    nothing of the second patient written."""
    typed = f"Nowhere {_tag()}"
    first = _admit(_payload(ward=typed, bed="4"))
    assert first.status_code == 201, first.text
    assert first.json()["bed_warning"] is None and first.json()["location_warning"]
    name = f"Second Typed {_tag()}"
    second = _admit(_payload(name=name, ward=f"  {typed.lower()} ", bed=" 4"))
    assert second.status_code == 409, second.text
    detail = _detail(second)
    assert detail["error"] == "bed_occupied" and detail["extra_bed_offered"] is False
    assert "not in bed setup" in detail["message"] and "4A" in detail["message"]
    assert _named(name) == 0
    assert _one("SELECT COUNT(*) AS n FROM users WHERE name = ?", (name,))["n"] == 0
    # A label of its own is admitted, unlinked, as before.
    own = _admit(_payload(ward=typed, bed="4A"))
    assert own.status_code == 201, own.text


def test_an_unlisted_bed_on_a_master_ward_that_is_held_is_refused(adt_on):
    ward = _ward(labels=("1",))
    first = _admit(_payload(ward=ward["code"], bed="99"))
    assert first.status_code == 201, first.text
    assert first.json()["location"]["linked"] is False
    second = _admit(_payload(ward=ward["code"], bed="99"))
    assert second.status_code == 409 and _detail(second)["error"] == "bed_occupied"


def test_a_typed_place_held_from_before_the_module_was_on_is_refused(adt_on, monkeypatch):
    typed = f"Old Ward {_tag()}"
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    assert _admit(_payload(ward=typed, bed="2")).status_code == 200
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    resp = _admit(_payload(ward=typed, bed="2"))
    assert resp.status_code == 409 and _detail(resp)["error"] == "bed_occupied"


def test_a_typed_place_held_only_on_the_patients_mirror_is_refused(adt_on):
    """bed_in_use_warning read patients.ward/bed; a record with no admission
    row still holds the place."""
    typed = f"Mirror Ward {_tag()}"
    pid = f"AAP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, status) "
                   "VALUES (?, ?, ?, ?, '7', 'admitted')",
                   (pid, f"Mirror Only {_tag()}", f"MRN-{uuid.uuid4().hex[:8].upper()}", typed))
    _SEEDED.append(pid)
    resp = _admit(_payload(ward=typed, bed="7"))
    assert resp.status_code == 409 and _detail(resp)["error"] == "bed_occupied"


def test_two_scribe_quick_admits_to_one_typed_place_give_one_admission(adt_on):
    typed = f"Scribe Nowhere {_tag()}"
    first, _ = _scribe(typed, "4")
    assert first.status_code == 200, first.text
    second, body = _scribe(typed.upper(), "4")
    assert second.status_code == 409, second.text
    assert _detail(second)["error"] == "bed_occupied"
    assert _named(body["name"]) == 0


def test_the_bed_route_refuses_a_typed_bed_another_admission_holds(adt_on):
    typed = f"Route Nowhere {_tag()}"
    first = _admit(_payload(ward=typed, awaiting_bed=True)).json()
    second = _admit(_payload(ward=typed, awaiting_bed=True)).json()
    ok = _assign(first["admission_id"], {"bed": "4"})
    assert ok.status_code == 200, ok.text
    refused = _assign(second["admission_id"], {"bed": "4"})
    assert refused.status_code == 409, refused.text
    assert _detail(refused)["error"] == "bed_occupied"
    row = _one("SELECT bed, bed_id FROM admissions WHERE id = ?", (second["admission_id"],))
    assert row == {"bed": None, "bed_id": None}
    assert [m["movement_type"] for m in _movements(second["admission_id"])] == ["admission"]


def test_the_bed_route_never_counts_the_admission_as_holding_its_own_typed_bed(adt_on):
    typed = f"Own Nowhere {_tag()}"
    adm = _admit(_payload(ward=typed, awaiting_bed=True)).json()
    assert _assign(adm["admission_id"], {"bed": "4"}).status_code == 200
    fixed = _assign(adm["admission_id"], {"bed": "5", "correction": True,
                                          "expected_bed_id": None, "reason": "Wrong bed typed"})
    assert fixed.status_code == 200, fixed.text


def test_flag_off_a_typed_held_place_still_only_warns(adt_off):
    typed = f"TW-{_tag()}"
    assert _admit(_payload(ward=typed, bed="4")).status_code == 200
    second = _admit(_payload(ward=typed, bed="4"))
    assert second.status_code == 200
    assert second.json()["bed_warning"].startswith("Bed 4 on")


# ── (b) The terminal clean read from the ledger ─────────────────────────────

@pytest.mark.parametrize("stored, carried", [
    (None, False), ("", False), ("[]", False), ('[""]', False), ("null", False),
    ('["Contact"]', True), ("Contact precautions", True), (["Droplet"], True), ([], False),
])
def test_carried_isolation_reads_an_empty_list_as_no_precaution(stored, carried):
    assert adt_core.carried_isolation(stored) is carried


def _needs(ward_id: str, label: str, user: dict = DOCTOR) -> dict:
    master = client.get("/api/admissions/options", headers=_hdr(user)).json()["master"]
    entry = next(w for w in master["wards"] if w["id"] == ward_id)
    return next(b for b in entry["beds"] if b["bed_label"] == label)


def _no_tasks(bed_id: str) -> bool:
    return _all("SELECT id FROM housekeeping_tasks WHERE unit_id = ?", (bed_id,)) == []


def test_an_isolation_room_not_recorded_clean_needs_the_authoriser_with_no_task(adt_on):
    """Probe 1: a bed registered as an isolation room in vacant_dirty, with no
    task raised, needed only a not-clean reason."""
    ward = _ward(labels=(), tracked=True)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"], "is_isolation_room": True},
                     actor=_admin())
    bed = _bed(ward["id"], "1")
    assert _no_tasks(bed["id"])
    picked = _needs(ward["id"], "1")
    assert picked["needs"] == "terminal_clean_authorisation" and picked["terminal_clean"] is True
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"], not_clean_reason="x")
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    assert _named(body["name"]) == 0
    ok = _admit({**body, "terminal_clean_authorised_by": DOCTOR["id"],
                 "terminal_clean_reason": "Only isolation room free"})
    assert ok.status_code == 201, ok.text
    detail = _movements(ok.json()["admission_id"])[0]["detail_json"]
    assert '"confirmed_by": "own_session"' in detail


def test_a_bed_in_an_isolation_room_needs_the_authoriser_too(adt_on):
    ward = _ward(labels=(), tracked=True)
    room = bm.save_room({"ward_id": ward["id"], "code": f"R{_tag()}", "room_type": "isolation"},
                        actor=_admin())
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"], "room_id": room["id"],
                      "is_isolation_room": False}, actor=_admin())
    bed = _bed(ward["id"], "1")
    assert not bed["is_isolation_room"]
    assert _needs(ward["id"], "1")["needs"] == "terminal_clean_authorisation"
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"], not_clean_reason="x"))
    assert resp.status_code == 409
    assert _detail(resp)["error"] == "terminal_clean_authorisation_required"


def _end(out: dict, end_type: str = "lama") -> None:
    resp = client.post(f"/api/adt/admissions/{out['admission_id']}/end",
                       headers=_hdr(DOCTOR, f"end-{uuid.uuid4().hex}"),
                       json={"end_type": end_type, "confirm_mrn": out["mrn"],
                             "reason": "Left against advice"})
    assert resp.status_code == 200, resp.text


def test_a_bed_an_isolation_patient_left_needs_the_authoriser_before_any_task(adt_on):
    """Probe 2: a patient with Contact precautions whose stay ended through
    the end-of-stay route left the bed reading terminal_clean False, and the
    next patient went in with only a not-clean reason."""
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _ready(bed["id"])
    first = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                            isolation_precautions=["Contact"]))
    assert first.status_code == 201, first.text
    _end(first.json())
    assert _no_tasks(bed["id"])
    with db_session() as db:
        ready = adt_core.bed_readiness_on(db, ward=ward, bed=bed)
    assert ready["status"] == "needs_cleaning" and ready["terminal_clean"] is True
    assert _needs(ward["id"], "1")["needs"] == "terminal_clean_authorisation"
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"], not_clean_reason="x")
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    # Recorded clean since: nothing is needed any more. (Housekeeping still
    # reads the bed ready, as nothing told it the patient left, so the clean
    # goes through waiting to be cleaned.)
    hk.transition_unit(bed["id"], "vacant_dirty", actor_user_id="AAHK", reason="test")
    _ready(bed["id"])
    assert _needs(ward["id"], "1")["needs"] is None
    assert _admit({**body, "not_clean_reason": None}).status_code == 201


def test_a_bed_a_patient_with_no_precautions_left_needs_only_a_reason(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _ready(bed["id"])
    first = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"]))   # stored '[]'
    assert first.status_code == 201, first.text
    _end(first.json())
    assert _needs(ward["id"], "1")["needs"] == "not_clean_reason"
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                           not_clean_reason="Ward full, wiped down"))
    assert resp.status_code == 201, resp.text


def test_a_terminal_clean_skipped_once_is_still_owed_after_the_next_patient(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _ready(bed["id"])
    first = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                            isolation_precautions=["Airborne"]))
    _end(first.json())
    second = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                             terminal_clean_authorised_by=DOCTOR["id"],
                             terminal_clean_reason="Emergency"))
    assert second.status_code == 201, second.text
    _end(second.json())
    assert _needs(ward["id"], "1")["needs"] == "terminal_clean_authorisation"


def test_an_out_of_service_bed_never_hides_a_terminal_clean(adt_on):
    ward = _ward(labels=(), tracked=True)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"], "is_isolation_room": True},
                     actor=_admin())
    bed = _bed(ward["id"], "1")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET state = 'out_of_service' WHERE id = ?",
                   (bed["id"],))
    picked = _needs(ward["id"], "1")
    assert picked["needs"] == "terminal_clean_authorisation"
    assert picked["out_of_service"] is True and picked["terminal_clean"] is True
    body = _payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                    out_of_service_reason="Only bed with suction")
    refused = _admit(body)
    assert refused.status_code == 409
    assert _detail(refused)["error"] == "terminal_clean_authorisation_required"
    no_oos = _admit({k: v for k, v in body.items() if k != "out_of_service_reason"})
    assert no_oos.status_code == 409
    assert _detail(no_oos)["error"] == "out_of_service_reason_required"
    ok = _admit({**body, "terminal_clean_authorised_by": DOCTOR["id"],
                 "terminal_clean_reason": "No other isolation room"})
    assert ok.status_code == 201, ok.text
    detail = _movements(ok.json()["admission_id"])[0]["detail_json"]
    assert "Only bed with suction" in detail and '"terminal_clean_override"' in detail


def test_an_out_of_service_bed_with_no_terminal_clean_needs_only_its_reason(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET state = 'out_of_service' WHERE id = ?",
                   (bed["id"],))
    picked = _needs(ward["id"], "1")
    assert picked["needs"] == "out_of_service_reason" and picked["out_of_service"] is True
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=bed["id"],
                           out_of_service_reason="Only bed with suction"))
    assert resp.status_code == 201, resp.text


def test_an_isolation_room_on_an_untracked_ward_raises_nothing(adt_on):
    ward = _ward(labels=(), tracked=False)
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"], "is_isolation_room": True},
                     actor=_admin())
    resp = _admit(_payload(ward=ward["code"], bed="1", bed_id=_bed(ward["id"], "1")["id"]))
    assert resp.status_code == 201, resp.text


# ── (c) The authoriser acts ─────────────────────────────────────────────────

def _terminal_bed(ward: dict | None = None, label: str = "1") -> tuple[dict, dict]:
    ward = ward or _ward(labels=(label,), tracked=True)
    bed = _bed(ward["id"], label)
    _terminal(bed["id"])
    return ward, bed


def _terminal_body(ward: dict, bed: dict, **over) -> dict:
    return _payload(ward=ward["code"], bed=bed["bed_label"], bed_id=bed["id"],
                    terminal_clean_reason="No other isolation bed", **over)


def _pin_refusals(authoriser_id: str) -> list[dict]:
    return _all("SELECT * FROM audit_logs WHERE action_type = "
                "'terminal_clean_authoriser_pin_refused' AND metadata_json LIKE ?",
                (f'%"{authoriser_id}"%',))


def test_naming_someone_else_without_their_pin_is_refused(adt_on):
    """The reviewer's evidence: a clerk named N001 and the record said N001
    authorised it. Only a doctor may admit, and a doctor naming a nurse is
    refused the same way."""
    ward, bed = _terminal_bed()
    nurse = _with_pin(_user("nurse"))
    body = _terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"])
    resp = _admit(body)
    assert resp.status_code == 409, resp.text
    assert _detail(resp)["error"] == "terminal_clean_pin_required"
    assert _named(body["name"]) == 0


def test_the_named_persons_signing_pin_confirms_and_is_never_stored(adt_on):
    ward, bed = _terminal_bed()
    nurse = _with_pin(_user("nurse"), pin="739146")
    resp = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"],
                                 terminal_clean_authoriser_pin=nurse["pin"]))
    assert resp.status_code == 201, resp.text
    move = _movements(resp.json()["admission_id"])[0]
    assert f'"authorised_by": "{nurse["id"]}"' in move["detail_json"]
    assert '"confirmed_by": "signing_pin"' in move["detail_json"]
    assert "739146" not in (move["detail_json"] or "")
    assert _one("SELECT COUNT(*) AS n FROM audit_logs WHERE metadata_json LIKE '%739146%' "
                "OR summary_message LIKE '%739146%'")["n"] == 0
    assert _one("SELECT COUNT(*) AS n FROM admissions WHERE admitting_notes LIKE '%739146%' "
                "OR nursing_notes LIKE '%739146%'")["n"] == 0


def test_a_wrong_pin_is_refused_audited_and_counted(adt_on):
    ward, bed = _terminal_bed()
    doctor = _user("doctor")
    nurse = _with_pin(_user("nurse"))
    body = _terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"],
                          terminal_clean_authoriser_pin="000000")
    resp = _admit(body, user=doctor)
    assert resp.status_code == 403, resp.text
    assert _detail(resp)["error"] == "terminal_clean_pin_wrong"
    assert _named(body["name"]) == 0
    refusals = _pin_refusals(nurse["id"])
    assert len(refusals) == 1
    assert refusals[0]["event_category"] == "security" and refusals[0]["outcome"] == "failure"
    assert refusals[0]["actor_user_id"] == doctor["id"]
    assert len(pin_security.PIN_ATTEMPTS.get(nurse["id"], [])) == 1


def test_five_wrong_pins_lock_the_persons_pin_entry(adt_on):
    ward, bed = _terminal_bed()
    nurse = _with_pin(_user("nurse"))
    for _ in range(pin_security.MAX_PIN_ATTEMPTS):
        wrong = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"],
                                      terminal_clean_authoriser_pin="000000"))
        assert wrong.status_code == 403
    locked = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"],
                                   terminal_clean_authoriser_pin=nurse["pin"]))
    assert locked.status_code == 429, locked.text
    assert _detail(locked)["error"] == "terminal_clean_authoriser_locked"
    assert len(_pin_refusals(nurse["id"])) == pin_security.MAX_PIN_ATTEMPTS + 1


def test_the_sender_authorises_from_their_own_session_when_their_role_allows(adt_on):
    ward, bed = _terminal_bed()
    doctor = _user("doctor")   # no PIN set, none needed for themself
    resp = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=doctor["id"]),
                  user=doctor)
    assert resp.status_code == 201, resp.text
    detail = _movements(resp.json()["admission_id"])[0]["detail_json"]
    assert '"confirmed_by": "own_session"' in detail
    assert f'"authorised_by": "{doctor["id"]}"' in detail


def test_an_authoriser_with_no_pin_cannot_confirm_for_someone_else(adt_on):
    ward, bed = _terminal_bed()
    nurse = _user("nurse")
    resp = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=nurse["id"],
                                 terminal_clean_authoriser_pin="123456"))
    assert resp.status_code == 422, resp.text
    assert _detail(resp)["error"] == "terminal_clean_authoriser_no_pin"


def test_an_authoriser_from_another_department_is_refused(adt_on):
    ward, bed = _terminal_bed()
    outsider = _with_pin(_user("nurse", f"Other Dept {_tag()}"))
    body = _terminal_body(ward, bed, terminal_clean_authorised_by=outsider["id"],
                          terminal_clean_authoriser_pin=outsider["pin"])
    resp = _admit(body)
    assert resp.status_code == 422, resp.text
    assert _detail(resp)["error"] == "terminal_clean_authoriser_out_of_department"
    assert _named(body["name"]) == 0
    hospital_wide = _with_pin(_user("admin", None))
    ok = _admit(_terminal_body(ward, bed, terminal_clean_authorised_by=hospital_wide["id"],
                               terminal_clean_authoriser_pin=hospital_wide["pin"]))
    assert ok.status_code == 201, ok.text


def test_the_bed_route_takes_the_signing_pin_the_same_way(adt_on):
    ward = _ward(labels=("1",), tracked=True)
    bed = _bed(ward["id"], "1")
    _terminal(bed["id"])
    adm = _awaiting(ward)
    clerk = _user("ward_clerk")
    nurse = _with_pin(_user("nurse"))
    base = {"bed_id": bed["id"], "terminal_clean_authorised_by": nurse["id"],
            "terminal_clean_reason": "No other isolation bed"}
    named = _assign(adm["admission_id"], base, user=clerk)
    assert named.status_code == 409 and _detail(named)["error"] == "terminal_clean_pin_required"
    wrong = _assign(adm["admission_id"], {**base, "terminal_clean_authoriser_pin": "000000"},
                    user=clerk)
    assert wrong.status_code == 403 and _detail(wrong)["error"] == "terminal_clean_pin_wrong"
    refusal = _pin_refusals(nurse["id"])
    assert len(refusal) == 1 and refusal[0]["patient_id"] == adm["patient_id"]
    ok = _assign(adm["admission_id"], {**base, "terminal_clean_authoriser_pin": nurse["pin"]},
                 user=clerk)
    assert ok.status_code == 200, ok.text
    move = _movements(adm["admission_id"])[-1]
    assert '"confirmed_by": "signing_pin"' in move["detail_json"]


def test_flag_off_the_pin_field_is_ignored(adt_off):
    resp = _admit(_payload(ward=f"TW-{_tag()}", bed="1", terminal_clean_authorised_by="N001",
                           terminal_clean_authoriser_pin="000000"))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == FLAG_OFF_KEYS
