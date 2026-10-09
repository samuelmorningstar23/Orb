"""Where an admission is: migration 58, the single location writer, and the
bed master's reconciliation, link, register-complete, rename and retire
guards (adt-3, W3-B, 4 October 2026).

What this file defends:

  1. MIGRATION 58 adds the seven admission columns, four vocabulary triggers
     and two indexes, is idempotent, keeps every existing row, and has NO
     bed_shared_reason column (ruling ADT-S1 (a): no shared beds).
  2. THE BED LOCK. A second active admission on one master bed is refused by
     the database (DBIntegrityError) and by the writer in words (409
     bed_occupied); discharging frees the bed.
  3. VOCABULARIES. A bad discharge_type or presence_status is refused on
     INSERT and on an UPDATE naming the column; an UPDATE of status never
     fires them.
  4. FLAG OFF. An admission written through the admission route with ORB_ADT
     off carries every new column NULL and writes no movement.
  5. TEXT RESOLUTION. 'gm-1' and ' 12 ' link to GM-1/12 and both mirrors take
     the master's spelling; unknown text stays unlinked with bed_id NULL.
  6. RECONCILIATION never links a doubled bed; register-complete is refused
     while the ward has an unlinked admission and stamps ledger_complete_from
     once it has none.
  7. RENAME AND RETIRE. A referenced ward code or bed label is immutable
     (the id references count); a display name is not. An occupied bed cannot
     be retired; a retired bed cannot be chosen.
  8. MIRROR PARITY for every linked active admission, and an unwritable
     patient_movement audit rolls the link back.
  9. BOARDERS and OUT-OF-SERVICE beds need their words; the routes are gated.
"""
import importlib
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from services import bed_master_service as bm
from services.adt import core as adt_core
from services.audit_service import AuditWriteError

client = TestClient(main.app)

M58 = importlib.import_module("migrations_his.m0058_adt_admission_location")
NEW_COLUMNS = ["ward_id", "bed_id", "admitted_at", "presence_status", "presence_since",
               "discharge_type", "discharged_by"]
COVERED = "General Medicine"   # conftest seeds doctors and a nurse in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = None) -> dict:
    uid = f"AL{uuid.uuid4().hex[:8].upper()}"
    dept = department if department is not None else (
        COVERED if role in ("doctor", "nurse", "ward_clerk") else None)
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Location {role}", role, dept, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": dept}


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        _ADMIN = _user("admin")
    return _ADMIN


def _ward(code: str | None = None, department: str = COVERED, labels=("10", "11", "12")) -> dict:
    ward = bm.save_ward({"code": code or f"AL-{_tag()}", "department": department},
                        actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _bed(ward_id: str, label: str) -> dict:
    with db_session() as db:
        return dict(db.execute(
            "SELECT * FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
            (ward_id, label)).fetchone())


def _admit(ward_text: str | None, bed_text: str | None, *, department: str = COVERED,
           case_room: bool = False) -> tuple[str, str]:
    """A patient with one active admission typed at ward/bed text, the way
    every pre-ADT lane writes it. Returns (patient_id, admission_id)."""
    pid = f"PAL{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADM-AL{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status, admission_date) "
            "VALUES (?, 'Location Test', ?, ?, ?, 'admitted', CURRENT_TIMESTAMP)",
            (pid, f"MRN-{pid}", ward_text, bed_text),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status) "
            "VALUES (?, ?, 'D001', ?, ?, ?, 'active')",
            (aid, pid, department, ward_text, bed_text),
        )
        if case_room:
            db.execute(
                "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                (f"CH-{pid}", f"Case: {pid}", pid),
            )
    return pid, aid


def _admission(aid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM admissions WHERE id = ?", (aid,)).fetchone())


def _patient(pid: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone())


def _movements(aid: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM patient_movements WHERE admission_id = ? ORDER BY rowid", (aid,))]


def _audits(action: str, patient_id: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ?",
            (action, patient_id))]


def _locate(aid: str, **kw):
    kw.setdefault("movement_type", "location_linked")
    kw.setdefault("reason", "test placement")
    with db_session(immediate=True) as db:
        return adt_core.locate_admission_on(db, admission_id=aid, actor=_admin(), **kw)


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    yield


# ═════════════════════════════════════════════════════════════════════════════
#  1. Migration 58
# ═════════════════════════════════════════════════════════════════════════════

def test_migration_58_is_ledgered_with_its_columns_triggers_and_indexes():
    with db_session() as db:
        ledger = {r[0] for r in db.execute("SELECT version FROM schema_migrations")}
        cols = [r[1] for r in db.execute("PRAGMA table_info(admissions)")]
        objects = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name = 'admissions'")}
    assert 58 in ledger
    assert set(NEW_COLUMNS) <= set(cols)
    assert M58.TRIGGER_NAMES <= objects
    assert M58.INDEX_NAMES <= objects


def test_there_is_no_shared_bed_column_and_the_lock_has_no_exemption():
    """ADT-S1 (a): an occupied bed is refused; there is no shared-bed reason."""
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(admissions)")}
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_admissions_active_bed'"
                         ).fetchone()[0]
    assert "bed_shared_reason" not in cols
    assert "shared" not in sql.lower()
    assert "UNIQUE" in sql.upper() and "status = 'active'" in sql


def _legacy_admissions_db(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.execute("""CREATE TABLE admissions (
        id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, admitted_by TEXT NOT NULL,
        department TEXT, ward TEXT, bed TEXT,
        status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discharged', 'transferred')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, ward, bed) "
                 "VALUES ('A1', 'P1', 'U1', 'GM-1', '12')")
    conn.execute("INSERT INTO admissions (id, patient_id, admitted_by, ward, bed) "
                 "VALUES ('A2', 'P2', 'U1', 'GM-1', '12')")
    conn.commit()
    return conn


def test_migration_58_is_additive_and_idempotent_on_a_legacy_table(tmp_path):
    conn = _legacy_admissions_db(tmp_path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM admissions ORDER BY id")]
    M58.apply(conn)
    conn.commit()
    schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(admissions)")]
    assert cols[-7:] == NEW_COLUMNS
    after = [tuple(r)[:len(before[0])] for r in conn.execute("SELECT * FROM admissions ORDER BY id")]
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM admissions WHERE ward_id IS NULL AND bed_id IS NULL "
                        "AND discharge_type IS NULL AND presence_status IS NULL").fetchone()[0] == 2
    M58.apply(conn)
    conn.commit()
    assert sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master")) == schema
    conn.close()


def test_migration_58_leaves_a_database_without_admissions_alone(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    M58.apply(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  2 and 3. The bed lock and the vocabularies, at the database
# ═════════════════════════════════════════════════════════════════════════════

def test_a_second_active_admission_on_one_bed_is_refused_by_the_database():
    ward = _ward()
    bed = _bed(ward["id"], "10")
    _, first = _admit(ward["code"], "10")
    _, second = _admit(ward["code"], "11")
    with db_session() as db:
        db.execute("UPDATE admissions SET bed_id = ? WHERE id = ?", (bed["id"], first))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE admissions SET bed_id = ? WHERE id = ?", (bed["id"], second))
    # Discharging the first frees the bed.
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (first,))
        db.execute("UPDATE admissions SET bed_id = ? WHERE id = ?", (bed["id"], second))
    assert _admission(second)["bed_id"] == bed["id"]


def test_the_writer_refuses_an_occupied_bed_in_words_with_409(adt_on):
    ward = _ward()
    bed = _bed(ward["id"], "10")
    pid1, first = _admit(ward["code"], "10")
    _, second = _admit(None, None)
    _locate(first, ward_id=ward["id"], bed_id=bed["id"])
    with pytest.raises(adt_core.AdtConflict) as exc:
        _locate(second, ward_id=ward["id"], bed_id=bed["id"], movement_type="bed_assigned")
    assert exc.value.http_status == 409 and exc.value.code == "bed_occupied"
    assert "already holds a patient" in str(exc.value)
    assert _admission(second)["bed_id"] is None
    assert _movements(second) == []
    # After the first patient leaves, the bed can be assigned.
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (first,))
    placed = _locate(second, ward_id=ward["id"], bed_id=bed["id"], movement_type="bed_assigned")
    assert placed["bed_id"] == bed["id"]


@pytest.mark.parametrize("statement", [
    "INSERT INTO admissions (id, patient_id, admitted_by, discharge_type) VALUES ('{a}', '{p}', 'D001', 'dead')",
    "INSERT INTO admissions (id, patient_id, admitted_by, presence_status) VALUES ('{a}', '{p}', 'D001', 'away')",
    "UPDATE admissions SET discharge_type = 'gone home' WHERE id = '{x}'",
    "UPDATE admissions SET presence_status = 'out' WHERE id = '{x}'",
])
def test_the_vocabulary_triggers_refuse_bad_values(statement):
    pid, existing = _admit(None, None)
    sql = statement.format(a=f"ADM-BAD{_tag()}", p=pid, x=existing)
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(sql)


def test_good_values_pass_and_status_updates_never_fire_the_triggers(tmp_path):
    with db_session() as db:
        pid = _admit(None, None)[0]
        aid = f"ADM-OK{_tag()}"
        # An ended stay beside the patient's active one: since migration 65
        # (opd-17b, W6-B) a patient holds one active admission only.
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, presence_status, "
                   "discharge_type, status) VALUES (?, ?, 'D001', 'on_leave', NULL, 'discharged')",
                   (aid, pid))
        db.execute("UPDATE admissions SET discharge_type = 'death', presence_status = 'absconded' "
                   "WHERE id = ?", (aid,))
    # A legacy row holding a value outside the vocabulary (written before the
    # triggers existed) can still have its status flipped: only an UPDATE that
    # names the guarded column fires its trigger.
    conn = _legacy_admissions_db(tmp_path)
    conn.execute("ALTER TABLE admissions ADD COLUMN discharge_type TEXT")
    conn.execute("UPDATE admissions SET discharge_type = 'legacy word' WHERE id = 'A1'")
    conn.commit()
    M58.apply(conn)
    conn.execute("UPDATE admissions SET status = 'discharged' WHERE id = 'A1'")
    with pytest.raises(DBIntegrityError):
        conn.execute("UPDATE admissions SET discharge_type = 'other word' WHERE id = 'A1'")
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  4. Flag off: the admission route writes what it always wrote
# ═════════════════════════════════════════════════════════════════════════════

def test_with_adt_off_an_admission_carries_no_location_ids_and_no_movement(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    doctor = _user("doctor")
    body = {
        "name": "Flag Off Admit", "age": 52, "sex": "F", "department": COVERED,
        "ward": f"OFF-{_tag()}", "bed": "4", "attending_doctor_id": doctor["id"],
        "chief_complaint": "fever", "initial_diagnosis": "fever",
        "admission_source": "Emergency", "admission_type": "Inpatient",
    }
    resp = client.post("/api/admissions/admit", json=body, headers=_hdr(doctor))
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json().get("patient_id") or resp.json().get("patient", {}).get("id")
    with db_session() as db:
        row = dict(db.execute("SELECT * FROM admissions WHERE patient_id = ?", (pid,)).fetchone())
        moves = db.execute("SELECT COUNT(*) FROM patient_movements WHERE patient_id = ?",
                           (pid,)).fetchone()[0]
        audits = db.execute("SELECT COUNT(*) FROM audit_logs WHERE patient_id = ? AND "
                            "event_category = 'patient_movement'", (pid,)).fetchone()[0]
    assert {c: row[c] for c in NEW_COLUMNS} == {c: None for c in NEW_COLUMNS}
    assert row["ward"] == body["ward"] and row["bed"] == "4" and row["status"] == "active"
    assert moves == 0 and audits == 0


# ═════════════════════════════════════════════════════════════════════════════
#  5. Text resolution and the mirrors
# ═════════════════════════════════════════════════════════════════════════════

def test_typed_text_links_to_the_master_and_both_mirrors_take_its_spelling(adt_on):
    code = f"GM-{_tag()}"
    ward = _ward(code)
    pid, aid = _admit(code.lower(), " 12 ")
    report = bm.reconciliation_report(actor=_admin(), ward_id=ward["id"])
    assert aid in {e["admission_id"] for e in report["matched"]}
    out = bm.link_admissions({"ward_id": ward["id"], "admission_ids": [aid]}, actor=_admin())
    assert [x["admission_id"] for x in out["linked"]] == [aid]
    adm, pat = _admission(aid), _patient(pid)
    assert adm["ward_id"] == ward["id"] and adm["bed_id"] == _bed(ward["id"], "12")["id"]
    assert (adm["ward"], adm["bed"]) == (code, "12")
    assert (pat["ward"], pat["bed"]) == (code, "12")
    [move] = _movements(aid)
    assert move["movement_type"] == "location_linked"
    assert (move["from_ward"], move["from_bed"]) == (code.lower(), " 12 ")
    assert (move["to_ward_id"], move["to_bed_id"]) == (adm["ward_id"], adm["bed_id"])
    assert move["recorded_at"] and move["recorded_at"].endswith("+00:00")
    [audit] = _audits("admission_location_linked", pid)
    assert audit["event_category"] == "patient_movement" and audit["target_id"] == aid


def test_unknown_text_stays_unlinked_with_bed_id_null(adt_on):
    ward = _ward()
    pid, unknown_ward = _admit(f"NOPE-{_tag()}", "3")
    _, unknown_bed = _admit(ward["code"], "99")
    report = bm.reconciliation_report(actor=_admin())
    reasons = {e["admission_id"]: e["reason"] for e in report["unlinked"]}
    assert reasons[unknown_ward] == "ward_not_in_setup"
    assert reasons[unknown_bed] == "bed_not_in_setup"
    out = bm.link_admissions({"admission_ids": [unknown_ward, unknown_bed]}, actor=_admin())
    assert out["linked"] == []
    assert {s["code"] for s in out["skipped"]} == {"not_matched"}
    assert _admission(unknown_ward)["bed_id"] is None
    assert _admission(unknown_bed)["bed_id"] is None
    # The writer itself keeps unknown text as typed, unlinked.
    placed = _locate(unknown_ward, ward_text="  somewhere   else ", bed_text="7",
                     movement_type="correction")
    assert placed["ward_id"] is None and placed["bed_id"] is None
    assert (placed["ward"], placed["bed"]) == ("somewhere else", "7")


def test_current_location_reports_linked_unlinked_and_awaiting(adt_on):
    ward = _ward()
    pid, aid = _admit(ward["code"], "10")
    loc = adt_core.current_location(pid)
    assert loc["unlinked_bed"] and not loc["linked"] and not loc["awaiting_bed"]
    _locate(aid, ward_id=ward["id"], bed_id=_bed(ward["id"], "10")["id"])
    loc = adt_core.current_location(pid)
    assert loc["linked"] and loc["ward_code"] == ward["code"] and loc["bed_label"] == "10"
    assert loc["on_ward"] and not loc["boarder"]
    waiting, _ = _admit(ward["code"], None)
    assert adt_core.current_location(waiting)["awaiting_bed"]
    assert adt_core.current_location("NO-SUCH-PATIENT") is None


def test_an_expected_bed_that_moved_is_state_changed(adt_on):
    ward = _ward()
    _, aid = _admit(None, None)
    with pytest.raises(adt_core.AdtConflict) as exc:
        _locate(aid, ward_id=ward["id"], bed_id=_bed(ward["id"], "10")["id"],
                expected_bed_id="HKU-SOMETHING-ELSE", movement_type="bed_assigned")
    assert exc.value.code == "state_changed"


def test_record_movement_refuses_an_unknown_type_and_a_missing_reason():
    pid, aid = _admit(None, None)
    with db_session() as db:
        with pytest.raises(adt_core.AdtError):
            adt_core.record_movement_on(db, patient_id=pid, admission_id=aid,
                                        movement_type="teleport", reason="x")
        with pytest.raises(adt_core.AdtError):
            adt_core.record_movement_on(db, patient_id=pid, admission_id=aid,
                                        movement_type="bed_change", reason="   ")


# ═════════════════════════════════════════════════════════════════════════════
#  6. Reconciliation, doubled beds and register complete
# ═════════════════════════════════════════════════════════════════════════════

def test_reconciliation_never_links_a_doubled_bed(adt_on):
    ward = _ward()
    _, a1 = _admit(ward["code"], "11")
    _, a2 = _admit(ward["code"].lower(), "11")
    report = bm.reconciliation_report(actor=_admin(), ward_id=ward["id"])
    [doubled] = [d for d in report["doubled"] if d["bed_label"] == "11"]
    assert {e["admission_id"] for e in doubled["admissions"]} == {a1, a2}
    assert not {a1, a2} & {e["admission_id"] for e in report["matched"]}
    out = bm.link_admissions({"ward_id": ward["id"]}, actor=_admin())
    assert not {a1, a2} & {x["admission_id"] for x in out["linked"]}
    out = bm.link_admissions({"admission_ids": [a1, a2]}, actor=_admin())
    assert out["linked"] == [] and {s["code"] for s in out["skipped"]} == {"doubled_bed"}
    assert _admission(a1)["bed_id"] is None and _admission(a2)["bed_id"] is None


def test_a_bed_linked_to_one_admission_and_typed_by_another_is_doubled(adt_on):
    ward = _ward()
    _, linked = _admit(ward["code"], "10")
    _locate(linked, ward_id=ward["id"], bed_id=_bed(ward["id"], "10")["id"])
    _, typed = _admit(ward["code"], "10")
    report = bm.reconciliation_report(actor=_admin(), ward_id=ward["id"])
    assert any(d["bed_label"] == "10" for d in report["doubled"])
    assert typed not in {e["admission_id"] for e in report["matched"]}


def test_register_complete_is_refused_with_an_unlinked_admission_and_stamps_the_ledger(adt_on):
    ward = _ward()
    _, linkable = _admit(ward["code"], "10")
    _, stray = _admit(ward["code"], "77")
    _, waiting = _admit(ward["code"], None)
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.mark_register_complete(ward["id"], actor=_admin())
    assert exc.value.code == "register_incomplete"
    assert set(exc.value.detail["unlinked"]) == {stray}
    bm.link_admissions({"ward_id": ward["id"]}, actor=_admin())
    with pytest.raises(bm.BedMasterConflict):
        bm.mark_register_complete(ward["id"], actor=_admin())   # the stray still blocks
    _locate(stray, ward_id=ward["id"], bed_id=_bed(ward["id"], "11")["id"],
            movement_type="correction")
    done = bm.mark_register_complete(ward["id"], actor=_admin())
    assert done["register_complete"] is True
    assert done["register_complete_by"] == _admin()["id"] and done["ledger_complete_from"]
    again = bm.mark_register_complete(ward["id"], actor=_admin())
    assert again["ledger_complete_from"] == done["ledger_complete_from"]
    with db_session() as db:
        events = db.execute("SELECT COUNT(*) FROM bed_master_events WHERE entity_id = ? "
                            "AND action = 'register_completed'", (ward["id"],)).fetchone()[0]
    assert events == 1
    assert _admission(linkable)["bed_id"] is not None and _admission(waiting)["bed_id"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  7. Rename and retire guards
# ═════════════════════════════════════════════════════════════════════════════

def _end_deliberately_desynced(aid: str) -> None:
    """The two rename tests desync a mirror on purpose to prove the id
    reference alone holds; end that admission so the parity check below
    (which walks every linked active admission) is not reading their fixture."""
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (aid,))


def test_a_ward_code_referenced_by_a_linked_admission_cannot_change_but_its_name_can(adt_on):
    ward = _ward(labels=("10",))
    _, aid = _admit(None, None)
    _locate(aid, ward_id=ward["id"], bed_id=_bed(ward["id"], "10")["id"],
            movement_type="bed_assigned")
    # Even with the text mirrors pointing elsewhere, the id reference holds.
    with db_session() as db:
        db.execute("UPDATE admissions SET ward = 'ELSEWHERE', bed = 'Z' WHERE id = ?", (aid,))
        db.execute("UPDATE patient_movements SET to_ward = 'ELSEWHERE', to_bed = 'Z' "
                   "WHERE admission_id = ?", (aid,))
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.save_ward({"code": f"{ward['code']}X"}, actor=_admin(), ward_id=ward["id"])
    assert exc.value.code == "code_in_use"
    assert exc.value.detail["references"]["admissions"] >= 1
    renamed = bm.save_ward({"name": "Gentle Medicine"}, actor=_admin(), ward_id=ward["id"])
    assert renamed["name"] == "Gentle Medicine" and renamed["code"] == ward["code"]
    _end_deliberately_desynced(aid)


def test_a_bed_label_referenced_by_bed_id_cannot_change(adt_on):
    ward = _ward(labels=("10", "11"))
    bed = _bed(ward["id"], "11")
    _, aid = _admit(None, None)
    _locate(aid, ward_id=ward["id"], bed_id=bed["id"], movement_type="bed_assigned")
    with db_session() as db:
        db.execute("UPDATE admissions SET bed = 'Z' WHERE id = ?", (aid,))
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.update_bed(bed["id"], {"bed_label": "11A"}, actor=_admin())
    assert exc.value.code == "label_in_use"
    # An unreferenced bed can still be relabelled.
    assert bm.update_bed(_bed(ward["id"], "10")["id"], {"bed_label": "10B"},
                         actor=_admin())["bed_label"] == "10B"
    _end_deliberately_desynced(aid)


def test_an_occupied_bed_cannot_be_retired_and_a_retired_bed_cannot_be_chosen(adt_on):
    ward = _ward(labels=("10", "11", "12"))
    by_id, by_text, free = (_bed(ward["id"], x) for x in ("10", "11", "12"))
    _, linked = _admit(None, None)
    _locate(linked, ward_id=ward["id"], bed_id=by_id["id"], movement_type="bed_assigned")
    _admit(ward["code"], "11")
    for bed in (by_id, by_text):
        with pytest.raises(bm.BedMasterConflict) as exc:
            bm.retire_bed(bed["id"], {"reason": "broken"}, actor=_admin())
        assert exc.value.code == "bed_occupied"
    retired = bm.retire_bed(free["id"], {"reason": "broken rail"}, actor=_admin())
    assert retired["retired_at"] and retired["is_active"] is False
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.retire_bed(free["id"], {}, actor=_admin())
    assert exc.value.code == "bed_retired"
    _, other = _admit(None, None)
    with pytest.raises(adt_core.AdtConflict) as exc:
        _locate(other, ward_id=ward["id"], bed_id=free["id"], movement_type="bed_assigned")
    assert exc.value.code == "bed_retired"
    with db_session() as db:
        event = db.execute("SELECT reason FROM bed_master_events WHERE entity_id = ? AND "
                           "action = 'retired'", (free["id"],)).fetchone()
    assert event["reason"] == "broken rail"


# ═════════════════════════════════════════════════════════════════════════════
#  8. Mirror parity and the fail-closed audit
# ═════════════════════════════════════════════════════════════════════════════

def test_mirror_parity_holds_for_every_linked_active_admission(adt_on):
    ward = _ward()
    for label in ("10", "11"):
        _admit(ward["code"].lower(), f" {label}")
    bm.link_admissions({"ward_id": ward["id"]}, actor=_admin())
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            """SELECT a.id, a.patient_id, a.ward, a.bed, w.code, u.bed_label, p.ward AS p_ward,
                      p.bed AS p_bed,
                      (SELECT a2.id FROM admissions a2 WHERE a2.patient_id = a.patient_id
                         AND a2.status = 'active' ORDER BY a2.created_at DESC, a2.rowid DESC
                         LIMIT 1) AS current_id
                 FROM admissions a
                 JOIN wards w ON w.id = a.ward_id
                 JOIN housekeeping_units u ON u.id = a.bed_id
                 JOIN patients p ON p.id = a.patient_id
                WHERE a.status = 'active' AND a.bed_id IS NOT NULL""")]
    assert rows, "no linked admissions to check"
    for r in rows:
        assert (r["ward"], r["bed"]) == (r["code"], r["bed_label"]), r
        if r["current_id"] == r["id"]:
            assert (r["p_ward"], r["p_bed"]) == (r["code"], r["bed_label"]), r


def test_an_unwritable_patient_movement_audit_rolls_the_link_back(adt_on, monkeypatch):
    ward = _ward()
    pid, aid = _admit(ward["code"], "10")
    real = adt_core.log_audit_event

    def broken(*args, **kwargs):
        if kwargs.get("event_category") == "patient_movement":
            raise AuditWriteError("audit table unwritable")
        return real(*args, **kwargs)

    monkeypatch.setattr(adt_core, "log_audit_event", broken)
    with pytest.raises(AuditWriteError):
        bm.link_admissions({"admission_ids": [aid]}, actor=_admin())
    adm, pat = _admission(aid), _patient(pid)
    assert adm["bed_id"] is None and adm["ward_id"] is None
    assert (adm["ward"], adm["bed"]) == (ward["code"], "10") and pat["bed"] == "10"
    assert _movements(aid) == []


# ═════════════════════════════════════════════════════════════════════════════
#  9. Boarders, out-of-service beds and the routes
# ═════════════════════════════════════════════════════════════════════════════

def test_a_boarder_needs_the_acknowledgement_and_then_writes_audit_and_case_room_line(adt_on):
    host_dept = f"Ortho {_tag()}"
    _user("nurse", department=host_dept)   # cover, so the ward can be set up there
    ward = _ward(department=host_dept, labels=("1",))
    pid, aid = _admit(ward["code"], "1", case_room=True)
    out = bm.link_admissions({"admission_ids": [aid]}, actor=_admin())
    [skip] = out["skipped"]
    assert skip["code"] == "boarder_acknowledgement_required"
    assert f"Nurses on {ward['code']} ({host_dept}) will not be able to open this chart" in skip["message"]
    assert f"The {COVERED} team keeps it." in skip["message"]
    assert _admission(aid)["bed_id"] is None
    out = bm.link_admissions({"admission_ids": [aid], "boarder_acknowledged": True}, actor=_admin())
    assert out["linked"][0]["boarder"] is True
    adm = _admission(aid)
    assert adm["department"] == COVERED and adm["ward_id"] == ward["id"]
    [audit] = _audits("boarder_placed", pid)
    assert audit["event_category"] == "clinical_access"
    with db_session() as db:
        lines = db.execute("SELECT content FROM messages WHERE channel_id = ? AND sender_id = 'system'",
                           (f"CH-{pid}",)).fetchall()
    assert any("boarder" in r["content"] for r in lines)


def test_an_out_of_service_bed_needs_a_typed_reason(adt_on):
    ward = _ward(labels=("5",))
    bed = _bed(ward["id"], "5")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET state = 'out_of_service' WHERE id = ?", (bed["id"],))
    _, aid = _admit(None, None)
    with pytest.raises(adt_core.AdtConflict) as exc:
        _locate(aid, ward_id=ward["id"], bed_id=bed["id"], movement_type="bed_assigned")
    assert exc.value.code == "out_of_service_reason_required"
    placed = _locate(aid, ward_id=ward["id"], bed_id=bed["id"], movement_type="bed_assigned",
                     out_of_service_reason="only bed with oxygen")
    assert placed["movement"]["detail"]["out_of_service_reason"] == "only bed with oxygen"


def test_the_new_master_routes_are_gated_and_permissioned(monkeypatch):
    ward = _ward(labels=("1", "2"))
    admin, nurse = _admin(), _user("nurse")
    bed = _bed(ward["id"], "2")
    routes = [
        ("get", "/api/adt/master/reconciliation", None),
        ("post", "/api/adt/master/reconciliation/link", {"ward_id": ward["id"]}),
        ("post", f"/api/adt/master/beds/{bed['id']}/retire", {"reason": "x"}),
        ("post", f"/api/adt/master/wards/{ward['id']}/register-complete", {}),
    ]
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    for method, path, body in routes:
        resp = getattr(client, method)(path, headers=_hdr(admin), **({"json": body} if body is not None else {}))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled"
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    for method, path, body in routes:
        resp = getattr(client, method)(path, headers=_hdr(nurse), **({"json": body} if body is not None else {}))
        assert resp.status_code == 403, (path, resp.text)
    for method, path, body in routes:
        resp = getattr(client, method)(path, headers=_hdr(admin), **({"json": body} if body is not None else {}))
        assert resp.status_code == 200, (path, resp.text)


def test_reading_the_reconciliation_writes_a_phi_access_audit_naming_the_patients(adt_on):
    ward = _ward()
    pid, _ = _admit(ward["code"], "10")
    admin = _admin()
    resp = client.get(f"/api/adt/master/reconciliation?ward_id={ward['id']}", headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"]["matched"] == 1
    entry = body["matched"][0]
    assert "name" not in entry and "mrn" not in entry
    with db_session() as db:
        row = db.execute(
            "SELECT event_category, metadata_json FROM audit_logs WHERE action_type = "
            "'bed_reconciliation_viewed' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (ward["id"],)).fetchone()
    assert row["event_category"] == "phi_access" and pid in row["metadata_json"]


def test_a_retire_refusal_is_a_409_in_words_through_the_route(adt_on):
    ward = _ward(labels=("1",))
    _admit(ward["code"], "1")
    resp = client.post(f"/api/adt/master/beds/{_bed(ward['id'], '1')['id']}/retire",
                       json={"reason": "x"}, headers=_hdr(_admin()))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "bed_occupied"
    assert "Move the patient first" in resp.json()["detail"]["message"]


def test_patient_movement_is_emitted_by_a_real_call_site():
    from tests.test_audit_fail_closed_categories import emitted_categories
    assert any(site.startswith("services/adt/core.py")
               for site in emitted_categories().get("patient_movement", []))
