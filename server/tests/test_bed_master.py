"""The bed master (adt-1, W2-B, 4 October 2026).

What this file defends:

  1. MIGRATION 53 is additive and idempotent. On a fresh database, on one with
     housekeeping rows, and on one whose housekeeping tables were made by the
     lazy path before migration 53, it adds the four master tables and the five
     housekeeping_units columns, leaves every existing row as it was, and a
     second run changes nothing. bed_master_events refuses UPDATE and DELETE.
  2. ONE WARD CODE PER WARD. 'gm-1' beside 'GM-1' is refused (409), by words
     first and by the unique index underneath.
  3. ALL OR NOTHING. Registering 24 beds with a clash at bed 13 leaves zero new
     beds and zero events, whether the clash is found before writing or only
     at the 13th insert.
  4. ONE SPELLING. A bed's unit code is '<ward code>/<label>' and never
     collides across wards; a typed ward spelling never overrides wards.code.
  5. ONE WRITER. With ORB_ADT on, the housekeeping register refuses to create
     or change a bed (409 managed_in_beds_setup) and is unchanged with it off;
     a bed Bed setup registered is refused there whatever the switch says.
  6. THE SWITCH. Every /api/adt route is 404 with ORB_ADT off. With it on, a
     nurse POST to the master is 403 with an audited denial.
"""
import importlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from migrations import HOUSEKEEPING_INDEXES, HOUSEKEEPING_TABLES
from services import bed_master_service as bm
from services import housekeeping_service as hk

client = TestClient(main.app)

M53 = importlib.import_module("migrations_his.m0053_adt_bed_master")
NEW_UNIT_COLUMNS = ["ward_id", "room_id", "bed_class", "is_temporary", "retired_at"]
MASTER_TABLES = {"wards", "ward_rooms", "bed_classes", "bed_master_events"}
COVERED = "General Medicine"   # conftest seeds doctors and a nurse in it


# ── helpers ──────────────────────────────────────────────────────────────────

def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


def _user(role: str, department: str | None = None) -> dict:
    uid = f"BM{uuid.uuid4().hex[:8].upper()}"
    dept = department if department is not None else (
        COVERED if role in ("doctor", "nurse", "ward_clerk") else None)
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, f"Bed master {role}", role, dept, hash_password("password")),
        )
    return {"id": uid, "role": role, "department": dept}


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


ADMIN = None


def _admin() -> dict:
    global ADMIN
    if ADMIN is None:
        ADMIN = _user("admin")
    return ADMIN


def _ward(code: str | None = None, **extra) -> dict:
    data = {"code": code or f"BM-{_tag()}", "department": COVERED, **extra}
    return bm.save_ward(data, actor=_admin())


def _counts(ward_id: str) -> tuple[int, int, int]:
    """(beds on the ward, bed_master_events for them, housekeeping events)."""
    with db_session() as db:
        beds = db.execute("SELECT COUNT(*) FROM housekeeping_units WHERE ward_id = ?",
                          (ward_id,)).fetchone()[0]
        events = db.execute(
            "SELECT COUNT(*) FROM bed_master_events WHERE entity_type = 'bed' "
            "AND json_extract(detail_json, '$.ward_id') = ?", (ward_id,)).fetchone()[0]
        hk_events = db.execute(
            "SELECT COUNT(*) FROM housekeeping_unit_events e JOIN housekeeping_units u "
            "ON u.id = e.unit_id WHERE u.ward_id = ?", (ward_id,)).fetchone()[0]
    return beds, events, hk_events


def _old_housekeeping_db(path: Path) -> sqlite3.Connection:
    """A database with the housekeeping tables in the migration 22 shape, which
    is also exactly what the lazy path (ensure_housekeeping_schema) created
    before migration 53, holding two units and their events."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for statement in HOUSEKEEPING_TABLES:
        conn.execute(statement)
    for statement in HOUSEKEEPING_INDEXES:
        conn.execute(statement)
    conn.execute("INSERT INTO housekeeping_units (id, unit_code, ward, bed_label, unit_type, state) "
                 "VALUES ('HKU1', 'GM-1/03', 'GM-1', '03', 'bed', 'vacant_ready')")
    conn.execute("INSERT INTO housekeeping_units (id, unit_code, ward, unit_type, state) "
                 "VALUES ('HKU2', 'THEATRE-1', 'OT', 'theatre', 'vacant_dirty')")
    conn.execute("INSERT INTO housekeeping_unit_events (unit_id, to_state, occurred_at) "
                 "VALUES ('HKU1', 'vacant_ready', '2026-10-01T00:00:00+00:00')")
    conn.commit()
    return conn


def _schema(conn) -> list:
    return [tuple(r) for r in conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  1. Migration 53
# ═════════════════════════════════════════════════════════════════════════════

def test_migration_53_is_ledgered_on_the_session_db_with_its_tables_and_columns():
    with db_session() as db:
        ledger = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = [r[1] for r in db.execute("PRAGMA table_info(housekeeping_units)").fetchall()]
        ward_cols = {r[1] for r in db.execute("PRAGMA table_info(wards)").fetchall()}
    assert 53 in ledger
    assert MASTER_TABLES <= tables
    assert cols[-5:] == NEW_UNIT_COLUMNS
    assert {"facility_code", "code", "department", "cleaning_tracked", "register_complete",
            "register_complete_at", "register_complete_by", "ledger_complete_from"} <= ward_cols


def test_migration_53_keeps_existing_units_and_is_idempotent(tmp_path):
    conn = _old_housekeeping_db(tmp_path / "old.db")
    before = [tuple(r) for r in conn.execute("SELECT * FROM housekeeping_units ORDER BY id")]
    events_before = conn.execute("SELECT COUNT(*) FROM housekeeping_unit_events").fetchone()[0]

    M53.apply(conn)
    conn.commit()
    first = _schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(housekeeping_units)")]
    assert cols[-5:] == NEW_UNIT_COLUMNS
    after = [tuple(r)[:len(before[0])] for r in conn.execute("SELECT * FROM housekeeping_units ORDER BY id")]
    assert after == before, "an existing unit row changed"
    assert conn.execute("SELECT COUNT(*) FROM housekeeping_unit_events").fetchone()[0] == events_before
    assert conn.execute(
        "SELECT COUNT(*) FROM housekeeping_units WHERE ward_id IS NULL AND room_id IS NULL "
        "AND bed_class IS NULL AND is_temporary = 0 AND retired_at IS NULL").fetchone()[0] == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert MASTER_TABLES <= tables

    M53.apply(conn)
    conn.commit()
    assert _schema(conn) == first, "a second run changed the schema"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_migration_53_on_a_database_without_housekeeping_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    M53.apply(conn)
    conn.commit()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert MASTER_TABLES <= tables
    assert "housekeeping_units" not in tables
    conn.close()


def test_the_runner_applies_53_to_a_database_the_lazy_path_made(tmp_path, monkeypatch):
    """Housekeeping tables made lazily before migration 53, then the runner
    with nothing pending but 53: ledgered, columns added, rows kept."""
    import migrations

    conn = _old_housekeeping_db(tmp_path / "lazy.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", [M53.MIGRATION])
    assert conn.in_transaction is False
    assert migrations.run_pending_migrations(conn) == [53]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(housekeeping_units)")]
    assert cols[-5:] == NEW_UNIT_COLUMNS
    assert [r[0] for r in conn.execute("SELECT unit_code FROM housekeeping_units ORDER BY id")] == [
        "GM-1/03", "THEATRE-1"]
    assert migrations.run_pending_migrations(conn) == []
    conn.close()


def test_the_lazy_path_adds_the_columns_through_the_migrations_own_function(monkeypatch):
    """ensure_housekeeping_schema runs the same guarded ALTER the migration
    does, so the lazy path can never recreate the old shape."""
    calls = []
    real = hk.add_housekeeping_unit_columns
    monkeypatch.setattr(hk, "add_housekeeping_unit_columns",
                        lambda conn: (calls.append(conn), real(conn)))
    hk.ensure_housekeeping_schema(force=True)
    assert len(calls) == 1
    assert real is M53.add_housekeeping_unit_columns


def test_the_lazy_alter_brings_an_old_table_up_to_the_migrated_shape(tmp_path):
    lazy = _old_housekeeping_db(tmp_path / "lazy.db")
    M53.add_housekeeping_unit_columns(lazy)
    lazy.commit()
    migrated = _old_housekeeping_db(tmp_path / "migrated.db")
    M53.apply(migrated)
    migrated.commit()

    def shape(c):
        return [tuple(r) for r in c.execute("PRAGMA table_info(housekeeping_units)")]

    def idx(c):
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='housekeeping_units'")}
    assert shape(lazy) == shape(migrated)
    assert idx(lazy) == idx(migrated)
    lazy.close()
    migrated.close()


def test_bed_master_events_refuses_update_and_delete():
    ward = _ward()
    with db_session() as db:
        event_id = db.execute("SELECT id FROM bed_master_events WHERE entity_id = ?",
                              (ward["id"],)).fetchone()[0]
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("UPDATE bed_master_events SET action = 'edited' WHERE id = ?", (event_id,))
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("DELETE FROM bed_master_events WHERE id = ?", (event_id,))
    with db_session() as db:
        assert db.execute("SELECT action FROM bed_master_events WHERE id = ?",
                          (event_id,)).fetchone()[0] == "created"


def test_the_restore_manifest_lists_the_four_master_tables():
    import sys
    lib = str(Path(__file__).resolve().parents[2] / "scripts" / "lib")
    if lib not in sys.path:   # the same import tests/test_warm_standby.py uses
        sys.path.insert(0, lib)
    import verify_restore as vr
    assert set(vr.MODULE_TABLES["bed master"]) == MASTER_TABLES


# ═════════════════════════════════════════════════════════════════════════════
#  2. Wards
# ═════════════════════════════════════════════════════════════════════════════

def test_a_ward_code_in_another_capitalisation_is_refused():
    code = f"GM-{_tag()}"
    first = _ward(code)
    assert first["code"] == code and first["facility_code"] == "MAIN"
    assert first["has_cover"] is True and first["cover_note"] is None
    with pytest.raises(bm.BedMasterConflict) as exc:
        _ward(f"  {code.lower()} ")
    assert exc.value.http_status == 409 and exc.value.code == "duplicate_code"
    assert code in str(exc.value)


def test_the_unique_index_refuses_a_case_variant_even_past_the_service():
    code = f"IX-{_tag()}"
    _ward(code)
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute("INSERT INTO wards (id, code, department, created_at) VALUES (?, ?, ?, ?)",
                       (f"WRD-{_tag()}", code.lower(), COVERED, "2026-10-04T00:00:00+00:00"))


def test_a_department_nobody_covers_or_declares_is_refused():
    with pytest.raises(bm.BedMasterError) as exc:
        bm.save_ward({"code": f"NX-{_tag()}", "department": "Astrology"}, actor=_admin())
    assert exc.value.code == "unknown_department" and exc.value.http_status == 422


def test_the_department_takes_the_covered_spelling():
    ward = bm.save_ward({"code": f"SP-{_tag()}", "department": "  general medicine "},
                        actor=_admin())
    assert ward["department"] == COVERED


def test_a_declared_department_nobody_covers_is_saved_and_flagged(monkeypatch):
    from services import site_config_service as scs
    cfg = scs.SiteConfig(present=True, departments=[{"name": "Orthopaedics", "wards": []}])
    monkeypatch.setattr(scs, "current_site_config", lambda: cfg)
    ward = bm.save_ward({"code": f"OR-{_tag()}", "department": "orthopaedics"}, actor=_admin())
    assert ward["department"] == "Orthopaedics"
    assert ward["has_cover"] is False
    assert ward["cover_note"] == "Admissions refused until a doctor or nurse covers Orthopaedics."


def test_a_ward_code_is_immutable_once_a_bed_refers_to_it():
    ward = _ward()
    renamed = bm.save_ward({"code": ward["code"] + "X"}, actor=_admin(), ward_id=ward["id"])
    assert renamed["code"] == ward["code"] + "X"
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.save_ward({"code": ward["code"] + "Y"}, actor=_admin(), ward_id=ward["id"])
    assert exc.value.code == "code_in_use"
    # The display name is still free to change.
    assert bm.save_ward({"name": "East wing"}, actor=_admin(), ward_id=ward["id"])["name"] == "East wing"


def test_a_ward_code_is_immutable_once_a_patient_record_carries_it():
    code = f"PT-{_tag()}"
    ward = _ward(code)
    pid = f"BMP{_tag()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'discharged', ?, '1')",
                   (pid, "Bed Master Fixture", f"MRN-{pid}", code.lower()))
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.save_ward({"code": code + "Z"}, actor=_admin(), ward_id=ward["id"])
    assert exc.value.code == "code_in_use" and exc.value.detail["references"]["patients"] == 1


def test_a_ward_with_beds_cannot_be_switched_off():
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.save_ward({"is_active": False}, actor=_admin(), ward_id=ward["id"])
    assert exc.value.code == "ward_in_use"


def test_every_ward_write_leaves_an_event_and_a_facilities_audit_line():
    ward = _ward()
    bm.save_ward({"name": "Renamed"}, actor=_admin(), ward_id=ward["id"])
    with db_session() as db:
        actions = [r[0] for r in db.execute(
            "SELECT action FROM bed_master_events WHERE entity_type = 'ward' AND entity_id = ? "
            "ORDER BY id", (ward["id"],))]
        audits = [tuple(r) for r in db.execute(
            "SELECT event_category, action_type, target_type FROM audit_logs WHERE target_id = ? "
            "ORDER BY id", (ward["id"],))]
    assert actions == ["created", "updated"]
    assert audits == [("facilities", "bed_master_ward_created", "ward"),
                      ("facilities", "bed_master_ward_updated", "ward")]


# ═════════════════════════════════════════════════════════════════════════════
#  3. Beds: one session, one spelling
# ═════════════════════════════════════════════════════════════════════════════

def test_a_range_of_24_beds_is_registered_dirty_with_ward_coded_unit_codes():
    ward = _ward()
    out = bm.register_beds({"ward_id": ward["id"], "range": {"start": 1, "end": 24, "pad": 2}},
                           actor=_admin())
    assert out["count"] == 24 and not out["adopted"]
    codes = [b["unit_code"] for b in out["registered"]]
    assert codes[0] == f"{ward['code']}/01" and codes[-1] == f"{ward['code']}/24"
    assert {b["state"] for b in out["registered"]} == {"vacant_dirty"}
    assert {b["ward"] for b in out["registered"]} == {ward["code"]}
    assert _counts(ward["id"]) == (24, 24, 24)


def test_a_clash_at_bed_13_saves_nothing_when_found_before_writing():
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["13"]}, actor=_admin())
    before = _counts(ward["id"])
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": ward["id"], "range": {"start": 1, "end": 24}}, actor=_admin())
    assert exc.value.code == "labels_taken" and exc.value.detail["labels"] == ["13"]
    assert _counts(ward["id"]) == before == (1, 1, 1)


def test_a_label_repeated_in_one_request_saves_nothing():
    ward = _ward()
    labels = [str(n) for n in range(1, 25)]
    labels[12] = "12"
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": ward["id"], "labels": labels}, actor=_admin())
    assert exc.value.code == "labels_repeated"
    assert _counts(ward["id"]) == (0, 0, 0)


def test_a_clash_found_only_at_the_13th_insert_rolls_back_the_first_12(monkeypatch):
    ward = _ward()
    real = hk.register_unit_on
    calls = {"n": 0}

    def flaky(db, **kwargs):
        calls["n"] += 1
        if calls["n"] == 13:
            raise hk.UnitAlreadyRegistered("bed 13 collided at insert time")
        return real(db, **kwargs)

    monkeypatch.setattr(hk, "register_unit_on", flaky)
    with pytest.raises(bm.BedMasterConflict):
        bm.register_beds({"ward_id": ward["id"], "range": {"start": 1, "end": 24}}, actor=_admin())
    assert calls["n"] == 13
    assert _counts(ward["id"]) == (0, 0, 0)
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action_type = "
                          "'bed_master_beds_registered' AND target_id = ?",
                          (ward["id"],)).fetchone()[0] == 0


def test_unit_codes_never_collide_across_wards():
    a, b = _ward(), _ward()
    ra = bm.register_beds({"ward_id": a["id"], "labels": ["1"]}, actor=_admin())["registered"][0]
    rb = bm.register_beds({"ward_id": b["id"], "labels": ["1"]}, actor=_admin())["registered"][0]
    assert ra["unit_code"] == f"{a['code']}/1" and rb["unit_code"] == f"{b['code']}/1"
    with pytest.raises(bm.BedMasterError) as exc:
        bm.save_ward({"code": "A/B", "department": COVERED}, actor=_admin())
    assert exc.value.code == "slash_not_allowed"
    with pytest.raises(bm.BedMasterError) as exc:
        bm.register_beds({"ward_id": a["id"], "labels": ["2/3"]}, actor=_admin())
    assert exc.value.code == "slash_not_allowed"


def test_a_bed_label_is_unique_in_its_ward_without_regard_to_capitals():
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["12a"]}, actor=_admin())
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": ward["id"], "labels": ["12A"]}, actor=_admin())
    assert exc.value.code == "labels_taken"
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            hk.register_unit_on(db, ward_id=ward["id"], bed_label=" 12A", create_only=True)


def test_a_typed_ward_spelling_never_overrides_the_ward_code():
    ward = _ward()
    with db_session() as db:
        unit = hk.register_unit_on(db, ward="  some typed ward ", ward_id=ward["id"],
                                   bed_label="7", create_only=True)
    assert unit["ward"] == ward["code"]
    assert unit["unit_code"] == f"{ward['code']}/7"
    assert unit["ward_id"] == ward["id"]
    with pytest.raises(hk.HousekeepingError):
        with db_session() as db:
            hk.register_unit_on(db, unit_code="ANYTHING-8", ward_id=ward["id"], bed_label="8")


def test_the_housekeeping_wrapper_will_not_redescribe_a_master_bed():
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())["registered"][0]
    with pytest.raises(hk.UnitManagedByBedSetup):
        hk.register_unit(unit_code=bed["unit_code"], ward="Somewhere else", bed_label="1")
    with db_session() as db:
        assert db.execute("SELECT ward FROM housekeeping_units WHERE id = ?",
                          (bed["id"],)).fetchone()[0] == ward["code"]


def test_a_bed_label_is_immutable_once_its_cleaning_record_refers_to_it():
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["5"]}, actor=_admin())["registered"][0]
    relabelled = bm.update_bed(bed["id"], {"bed_label": "5B"}, actor=_admin())
    assert relabelled["bed_label"] == "5B" and relabelled["unit_code"] == f"{ward['code']}/5B"
    hk.transition_unit(bed["id"], "cleaning_in_progress", actor_user_id=_admin()["id"])
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.update_bed(bed["id"], {"bed_label": "5C"}, actor=_admin())
    assert exc.value.code == "label_in_use"


def test_an_isolation_room_makes_its_beds_isolation_units_and_follows_changes():
    ward = _ward()
    room = bm.save_room({"ward_id": ward["id"], "code": "Side 1", "room_type": "isolation"},
                        actor=_admin())
    assert room["is_isolation"] is True
    beds = bm.register_beds({"ward_id": ward["id"], "room_id": room["id"], "labels": ["S1", "S2"]},
                            actor=_admin())["registered"]
    assert {b["is_isolation_room"] for b in beds} == {True}
    assert {b["room"] for b in beds} == {"Side 1"}
    bm.save_room({"is_isolation": False}, actor=_admin(), room_id=room["id"])
    with db_session() as db:
        flags = {r[0] for r in db.execute(
            "SELECT is_isolation_room FROM housekeeping_units WHERE room_id = ?", (room["id"],))}
    assert flags == {0}
    with pytest.raises(bm.BedMasterConflict):
        bm.save_room({"code": "Side 9"}, actor=_admin(), room_id=room["id"])
    with pytest.raises(bm.BedMasterConflict):
        bm.save_room({"ward_id": ward["id"], "code": "side 1"}, actor=_admin())


def _room_bed_flags(room_id: str) -> set:
    with db_session() as db:
        return {r[0] for r in db.execute(
            "SELECT is_isolation_room FROM housekeeping_units WHERE room_id = ?", (room_id,))}


def _last_event_detail(entity_id: str) -> dict:
    with db_session() as db:
        raw = db.execute(
            "SELECT detail_json FROM bed_master_events WHERE entity_id = ? AND action = 'updated' "
            "ORDER BY id DESC LIMIT 1", (entity_id,)).fetchone()[0]
    return json.loads(raw)


def test_a_bed_moved_to_another_room_takes_that_rooms_isolation_flag():
    """Round 1 review: a bed moved into an isolation room kept the bay's flag,
    so housekeeping would send a standard clean after discharge."""
    ward = _ward()
    side = bm.save_room({"ward_id": ward["id"], "code": "Side", "room_type": "isolation"},
                        actor=_admin())
    bay = bm.save_room({"ward_id": ward["id"], "code": "Bay A"}, actor=_admin())
    bed = bm.register_beds({"ward_id": ward["id"], "room_id": bay["id"], "labels": ["1"]},
                           actor=_admin())["registered"][0]
    assert bed["is_isolation_room"] is False

    into_side = bm.update_bed(bed["id"], {"room_id": side["id"]}, actor=_admin())
    assert into_side["room"] == "Side" and into_side["is_isolation_room"] is True
    assert _last_event_detail(bed["id"])["isolation_follows_room"] is True

    back_to_bay = bm.update_bed(bed["id"], {"room_id": bay["id"]}, actor=_admin())
    assert back_to_bay["room"] == "Bay A" and back_to_bay["is_isolation_room"] is False

    bm.update_bed(bed["id"], {"room_id": side["id"]}, actor=_admin())
    no_room = bm.update_bed(bed["id"], {"room_id": None}, actor=_admin())
    assert no_room["room_id"] is None and no_room["room"] is None
    assert no_room["is_isolation_room"] is False


def test_an_isolation_flag_sent_with_the_move_wins_and_the_same_room_changes_nothing():
    ward = _ward()
    side = bm.save_room({"ward_id": ward["id"], "code": "Side", "room_type": "isolation"},
                        actor=_admin())
    bay = bm.save_room({"ward_id": ward["id"], "code": "Bay A"}, actor=_admin())
    bed = bm.register_beds({"ward_id": ward["id"], "room_id": bay["id"], "labels": ["1"],
                            "is_isolation_room": True}, actor=_admin())["registered"][0]
    assert bed["is_isolation_room"] is True
    # The room it is already in, sent again with an edit: the flag set by
    # hand is not re-derived.
    same = bm.update_bed(bed["id"], {"room_id": bay["id"], "notes": "By the window"},
                         actor=_admin())
    assert same["is_isolation_room"] is True and same["notes"] == "By the window"
    # A flag sent with the move is what is saved.
    bm.update_bed(bed["id"], {"room_id": side["id"]}, actor=_admin())
    kept = bm.update_bed(bed["id"], {"room_id": bay["id"], "is_isolation_room": True},
                         actor=_admin())
    assert kept["room"] == "Bay A" and kept["is_isolation_room"] is True
    assert "isolation_follows_room" not in _last_event_detail(bed["id"])


def test_a_room_retyped_to_or_from_isolation_carries_the_flag_to_its_beds():
    """Round 1 review: a room PATCH to room type 'isolation' left the room's
    isolation flag and its beds' flags off."""
    ward = _ward()
    room = bm.save_room({"ward_id": ward["id"], "code": "Bay B"}, actor=_admin())
    assert room["is_isolation"] is False
    beds = bm.register_beds({"ward_id": ward["id"], "room_id": room["id"], "labels": ["1", "2"]},
                            actor=_admin())["registered"]

    retyped = bm.save_room({"room_type": "isolation"}, actor=_admin(), room_id=room["id"])
    assert retyped["room_type"] == "isolation" and retyped["is_isolation"] is True
    assert _room_bed_flags(room["id"]) == {1}
    detail = _last_event_detail(room["id"])
    assert detail["isolation_follows_room_type"] is True
    assert sorted(detail["beds_isolation_changed"]) == sorted(b["id"] for b in beds)

    back = bm.save_room({"room_type": "bay"}, actor=_admin(), room_id=room["id"])
    assert back["room_type"] == "bay" and back["is_isolation"] is False
    assert _room_bed_flags(room["id"]) == {0}

    # A flag sent with the new type is what is saved.
    explicit = bm.save_room({"room_type": "private", "is_isolation": True}, actor=_admin(),
                            room_id=room["id"])
    assert explicit["room_type"] == "private" and explicit["is_isolation"] is True
    assert _room_bed_flags(room["id"]) == {1}
    # Between two types that are not 'isolation', the flag the bed manager
    # chose stays, and so do the beds.
    semi = bm.save_room({"room_type": "semi_private"}, actor=_admin(), room_id=room["id"])
    assert semi["room_type"] == "semi_private" and semi["is_isolation"] is True
    assert _room_bed_flags(room["id"]) == {1}


def _discharged_from(ward_code: str, bed_label: str) -> int:
    """A patient discharged from this ward and bed, with the discharge record
    the housekeeping sweep reads and no isolation precautions on the admission,
    so only the bed's own flag can make the clean terminal. Returns the
    discharge summary id."""
    author = _admin()["id"]
    pid = f"BMP{uuid.uuid4().hex[:8].upper()}"
    when = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, ?, 61, 'M', 'Bed master fixture', 'discharged', ?)""",
            (pid, f"BM Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             bed_label, ward_code, when),
        )
        db.execute(
            """INSERT INTO admissions (id, patient_id, admitted_by, department, ward,
                                       bed, isolation_precautions, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, 'discharged', ?)""",
            (f"BMA{uuid.uuid4().hex[:8].upper()}", pid, author, COVERED, ward_code,
             bed_label, when),
        )
        db.execute(
            """INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at)
               VALUES (?, ?, 'Bed master fixture summary', ?)""",
            (pid, author, when),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_a_bed_moved_into_a_retyped_isolation_room_by_the_routes_gets_a_terminal_clean(monkeypatch):
    """The effect the flag exists for, through the PATCH routes: a bed in a
    room retyped as isolation, then moved there, gets a terminal clean after
    discharge although the admission carried no isolation precautions."""
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    admin = _hdr(_user("admin"))
    ward = _ward()
    bay = client.post("/api/adt/master/rooms", headers=admin,
                      json={"ward_id": ward["id"], "code": "Bay A"}).json()
    side = client.post("/api/adt/master/rooms", headers=admin,
                       json={"ward_id": ward["id"], "code": "Side", "room_type": "private"}).json()
    assert side["is_isolation"] is False
    bed = client.post("/api/adt/master/beds", headers=admin,
                      json={"ward_id": ward["id"], "labels": ["1"], "room_id": bay["id"]}
                      ).json()["registered"][0]

    retyped = client.patch(f"/api/adt/master/rooms/{side['id']}", headers=admin,
                           json={"room_type": "isolation"})
    assert retyped.status_code == 200, retyped.text
    assert retyped.json()["is_isolation"] is True
    moved = client.patch(f"/api/adt/master/beds/{bed['id']}", headers=admin,
                         json={"room_id": side["id"]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["room"] == "Side" and moved.json()["is_isolation_room"] is True

    ds_id = _discharged_from(ward["code"], "1")
    hk.sync_discharge_turnarounds(lookback_hours=24)
    with db_session() as db:
        tasks = [tuple(r) for r in db.execute(
            "SELECT unit_id, clean_type FROM housekeeping_tasks WHERE source_type = 'discharge' "
            "AND source_ref = ?", (str(ds_id),)).fetchall()]
    assert tasks == [(bed["id"], "terminal")]


def test_bed_classes_are_unique_and_a_ward_default_reaches_its_beds():
    code = f"PVT{_tag()}"
    bc = bm.save_bed_class({"code": code, "name": "Private room", "bed_day_service_code": "ROOM-PVT"},
                           actor=_admin())
    assert bc["is_active"] is True and bc["counts_in_census"] is True
    with pytest.raises(bm.BedMasterConflict):
        bm.save_bed_class({"code": code.lower(), "name": "Again"}, actor=_admin())
    ward = _ward(default_bed_class=code.lower())
    assert ward["default_bed_class"] == code
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())["registered"][0]
    assert bed["bed_class"] == code
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.save_bed_class({"is_active": False}, actor=_admin(), code=code)
    assert exc.value.code == "bed_class_in_use"
    listed = [c for c in bm.list_master()["bed_classes"] if c["code"] == code][0]
    assert listed["bed_day_charge_listed"] is False


def test_beds_already_on_the_housekeeping_register_are_refused_unless_adopted():
    ward = _ward()
    legacy = hk.register_unit(unit_code=f"LEGACY-{_tag()}", ward=ward["code"].lower(),
                              bed_label="3", unit_type="bed")
    hk.transition_unit(legacy["id"], "cleaning_in_progress")
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": ward["id"], "labels": ["3", "4"]}, actor=_admin())
    assert exc.value.code == "already_in_housekeeping"
    assert _counts(ward["id"])[0] == 0

    out = bm.register_beds({"ward_id": ward["id"], "labels": ["3", "4"], "adopt_existing": True},
                           actor=_admin())
    assert [b["id"] for b in out["adopted"]] == [legacy["id"]]
    adopted = out["adopted"][0]
    assert adopted["state"] == "cleaning_in_progress", "adoption must keep the cleaning state"
    assert adopted["unit_code"] == f"{ward['code']}/3" and adopted["ward"] == ward["code"]
    assert len(out["registered"]) == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM housekeeping_unit_events WHERE unit_id = ?",
                          (legacy["id"],)).fetchone()[0] == 2


def test_a_unit_code_held_by_something_else_is_refused_even_with_adopt():
    ward = _ward()
    hk.register_unit(unit_code=f"{ward['code']}/9", ward="Somewhere else", unit_type="room")
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": ward["id"], "labels": ["9"], "adopt_existing": True},
                         actor=_admin())
    assert exc.value.code == "unit_codes_taken"
    assert _counts(ward["id"])[0] == 0


def _bed_row(bed_id: str) -> tuple:
    with db_session() as db:
        return tuple(db.execute("SELECT bed_label, unit_code FROM housekeeping_units WHERE id = ?",
                                (bed_id,)).fetchone())


def _bed_updates(bed_id: str) -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM bed_master_events WHERE entity_type = 'bed' "
                          "AND entity_id = ? AND action = 'updated'", (bed_id,)).fetchone()[0]


def test_a_rename_onto_a_bed_already_on_the_housekeeping_register_is_refused():
    """The padded-label trap: '01' registered beside a typed legacy bed '1',
    then renamed to '1', would leave two housekeeping rows for one bed and the
    discharge turnaround could land on the stale one. Refused like
    register_beds refuses it, case-blind, switched-off legacy beds included,
    and nothing about the master bed changes."""
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["01"]}, actor=_admin())["registered"][0]
    before = _bed_row(bed["id"])
    legacy = hk.register_unit(unit_code=f"LEG-{_tag()}", ward=ward["code"].lower(),
                              bed_label="1", unit_type="bed")
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.update_bed(bed["id"], {"bed_label": "1"}, actor=_admin())
    assert exc.value.code == "already_in_housekeeping"
    assert exc.value.detail["unit_codes"] == [legacy["unit_code"]]

    letter = hk.register_unit(unit_code=f"LEG-{_tag()}", ward=ward["code"],
                              bed_label="2a", unit_type="bed")
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET is_active = 0 WHERE id = ?", (letter["id"],))
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.update_bed(bed["id"], {"bed_label": "2A"}, actor=_admin())
    assert exc.value.code == "already_in_housekeeping"

    assert _bed_row(bed["id"]) == before
    assert _bed_updates(bed["id"]) == 0
    with db_session() as db:
        rows = db.execute(
            "SELECT COUNT(*) FROM housekeeping_units WHERE unit_type = 'bed' "
            "AND LOWER(TRIM(ward)) = LOWER(?) AND LOWER(TRIM(bed_label)) = '1'",
            (ward["code"],)).fetchone()[0]
    assert rows == 1, "one physical bed, one housekeeping row"

    # A free label still renames, and the legacy beds are untouched.
    renamed = bm.update_bed(bed["id"], {"bed_label": "3"}, actor=_admin())
    assert renamed["bed_label"] == "3" and renamed["unit_code"] == f"{ward['code']}/3"
    with db_session() as db:
        assert tuple(db.execute("SELECT ward_id, bed_label FROM housekeeping_units WHERE id = ?",
                                (legacy["id"],)).fetchone()) == (None, "1")


def test_a_rename_onto_a_unit_code_held_by_something_else_is_refused():
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["8"]}, actor=_admin())["registered"][0]
    hk.register_unit(unit_code=f"{ward['code']}/9", ward="Somewhere else", unit_type="room")
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.update_bed(bed["id"], {"bed_label": "9"}, actor=_admin())
    assert exc.value.code == "unit_codes_taken"
    assert _bed_row(bed["id"]) == ("8", f"{ward['code']}/8")


def test_the_bed_patch_route_refuses_a_rename_onto_a_housekeeping_bed(monkeypatch):
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["01"]}, actor=_admin())["registered"][0]
    hk.register_unit(unit_code=f"LEG-{_tag()}", ward=ward["code"], bed_label="1", unit_type="bed")
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    resp = client.patch(f"/api/adt/master/beds/{bed['id']}", headers=_hdr(_user("admin")),
                        json={"bed_label": "1"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "already_in_housekeeping"
    assert _bed_row(bed["id"]) == ("01", f"{ward['code']}/01")


def test_site_configuration_wards_import_once(monkeypatch):
    from services import site_config_service as scs
    a, b, c = f"SC-{_tag()}", f"SC-{_tag()}", f"SC-{_tag()}"
    _ward(c)   # already set up, in General Medicine
    cfg = scs.SiteConfig(present=True, departments=[
        {"name": COVERED, "wards": [a, b]},
        {"name": "Orthopaedics", "wards": [c]},
    ])
    monkeypatch.setattr(scs, "current_site_config", lambda: cfg)
    first = bm.import_site_config_wards(actor=_admin())
    assert sorted(w["code"] for w in first["created"]) == sorted([a, b])
    assert [x["code"] for x in first["conflicts"]] == [c]
    second = bm.import_site_config_wards(actor=_admin())
    assert second["created"] == []
    assert sorted(w["code"] for w in second["existing"]) == sorted([a, b])


def test_the_master_listing_carries_no_patient_data():
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())
    out = bm.list_master(ward_id=ward["id"])
    assert [w["id"] for w in out["wards"]] == [ward["id"]]
    assert out["wards"][0]["beds"] == 1
    text = repr(out).lower()
    for word in ("patient_id", "mrn", "diagnosis"):
        assert word not in text


def test_beds_are_listed_in_bed_number_order():
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["10", "2", "1", "1A"]}, actor=_admin())
    labels = [b["bed_label"] for b in bm.list_master(ward_id=ward["id"])["beds"]]
    assert labels == ["1", "1A", "2", "10"]


def test_a_range_with_a_prefix_and_the_registration_cap():
    ward = _ward()
    out = bm.register_beds({"ward_id": ward["id"], "range": {"prefix": "B", "start": 8, "end": 10,
                                                             "pad": 2}}, actor=_admin())
    assert [b["bed_label"] for b in out["registered"]] == ["B08", "B09", "B10"]
    with pytest.raises(bm.BedMasterError) as exc:
        bm.register_beds({"ward_id": ward["id"], "range": {"start": 1,
                          "end": bm.MAX_BEDS_PER_REGISTRATION + 1}}, actor=_admin())
    assert exc.value.code == "too_many_beds"
    assert _counts(ward["id"])[0] == 3


def test_beds_refuse_a_room_on_another_ward_and_a_switched_off_ward():
    a, b = _ward(), _ward()
    room_b = bm.save_room({"ward_id": b["id"], "code": "Bay 1"}, actor=_admin())
    with pytest.raises(bm.BedMasterError) as exc:
        bm.register_beds({"ward_id": a["id"], "room_id": room_b["id"], "labels": ["1"]},
                         actor=_admin())
    assert exc.value.code == "room_not_on_ward"
    bm.save_ward({"is_active": False}, actor=_admin(), ward_id=a["id"])
    with pytest.raises(bm.BedMasterConflict) as exc:
        bm.register_beds({"ward_id": a["id"], "labels": ["1"]}, actor=_admin())
    assert exc.value.code == "ward_inactive"
    with pytest.raises(bm.BedMasterNotFound):
        bm.register_beds({"ward_id": "WRD-NOPE", "labels": ["1"]}, actor=_admin())
    assert _counts(a["id"])[0] == 0


def test_a_bed_class_code_never_changes():
    code = f"GEN{_tag()}"
    bm.save_bed_class({"code": code, "name": "General"}, actor=_admin())
    with pytest.raises(bm.BedMasterError) as exc:
        bm.save_bed_class({"code": code + "X"}, actor=_admin(), code=code)
    assert exc.value.code == "code_fixed"
    changed = bm.save_bed_class({"name": "General ward", "is_critical_care": True}, actor=_admin(),
                                code=code)
    assert changed["name"] == "General ward" and changed["is_critical_care"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  4. Routes: the switch, the permissions, the housekeeping refusal
# ═════════════════════════════════════════════════════════════════════════════

def _adt_routes():
    for route in main.app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/adt"):
            for method in route.methods:
                yield method, route.path


def test_every_adt_route_is_404_while_the_module_is_off():
    admin = _hdr(_admin())
    routes = list(_adt_routes())
    assert any(p.startswith("/api/adt/master") for _, p in routes)
    for method, path in routes:
        url = path.replace("{ward_id}", "W").replace("{room_id}", "R").replace(
            "{bed_id}", "B").replace("{code}", "C").replace("{patient_id}", "P")
        resp = client.request(method, url, headers=admin, json={})
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
        assert resp.json()["detail"]["error"] == "module_disabled", (method, path)


def test_with_the_module_on_the_master_answers_by_permission(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    admin, doctor = _user("admin"), _user("doctor")
    clerk, nurse = _user("ward_clerk"), _user("nurse")
    assert client.get("/api/adt/master", headers=_hdr(admin)).status_code == 200
    assert client.get("/api/adt/master", headers=_hdr(doctor)).status_code == 200
    assert client.get("/api/adt/master", headers=_hdr(clerk)).status_code == 200
    assert client.get("/api/adt/master", headers=_hdr(nurse)).status_code == 403
    body = {"code": f"RT-{_tag()}", "department": COVERED}
    for who in (doctor, clerk):
        assert client.post("/api/adt/master/wards", headers=_hdr(who), json=body).status_code == 403
    made = client.post("/api/adt/master/wards", headers=_hdr(admin), json=body)
    assert made.status_code == 201, made.text
    again = client.post("/api/adt/master/wards", headers=_hdr(admin),
                        json={**body, "code": body["code"].lower()})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "duplicate_code"
    beds = client.post("/api/adt/master/beds", headers=_hdr(admin),
                       json={"ward_id": made.json()["id"], "range": {"start": 1, "end": 24}})
    assert beds.status_code == 201 and beds.json()["count"] == 24


def test_a_nurse_post_to_the_master_is_403_with_an_audited_denial(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    nurse = _user("nurse")
    resp = client.post("/api/adt/master/wards", headers=_hdr(nurse),
                       json={"code": f"NU-{_tag()}", "department": COVERED})
    assert resp.status_code == 403
    assert resp.json()["detail"]["missing"] == ["bed.master"]
    with db_session() as db:
        row = db.execute(
            "SELECT event_category, outcome, request_path FROM audit_logs "
            "WHERE actor_user_id = ? AND action_type = 'permission_denied'", (nurse["id"],)).fetchone()
        wards = db.execute("SELECT COUNT(*) FROM wards WHERE created_by = ?",
                           (nurse["id"],)).fetchone()[0]
    assert tuple(row) == ("authorization", "denied", "/api/adt/master/wards")
    assert wards == 0


def _unit_body(**extra):
    return {"unit_code": f"HKB-{_tag()}", "ward": f"HK Bed Ward {_tag()}", "bed_label": "1",
            "unit_type": "bed", "create_only": True, **extra}


def test_housekeeping_bed_registration_is_unchanged_with_the_module_off():
    nurse = _user("nurse")
    resp = client.post("/api/housekeeping/units", headers=_hdr(nurse), json=_unit_body())
    assert resp.status_code == 200, resp.text
    assert resp.json()["unit_type"] == "bed" and resp.json()["ward_id"] is None


def test_housekeeping_refuses_bed_creates_and_edits_with_the_module_on(monkeypatch):
    nurse = _user("nurse")
    existing = client.post("/api/housekeeping/units", headers=_hdr(nurse), json=_unit_body()).json()
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    create = client.post("/api/housekeeping/units", headers=_hdr(nurse), json=_unit_body())
    assert create.status_code == 409
    assert create.json()["detail"]["error"] == "managed_in_beds_setup"
    # Re-describing an existing bed as a room is an edit of a bed: refused too.
    edit = client.post("/api/housekeeping/units", headers=_hdr(nurse), json={
        "unit_code": existing["unit_code"], "ward": existing["ward"], "unit_type": "room"})
    assert edit.status_code == 409
    with db_session() as db:
        assert db.execute("SELECT unit_type FROM housekeeping_units WHERE id = ?",
                          (existing["id"],)).fetchone()[0] == "bed"
    # Rooms, bays and theatres are still registered here.
    room = client.post("/api/housekeeping/units", headers=_hdr(nurse),
                       json=_unit_body(unit_type="room", bed_label=None))
    assert room.status_code == 200, room.text


def test_a_master_bed_is_refused_by_housekeeping_even_with_the_module_off():
    ward = _ward()
    bed = bm.register_beds({"ward_id": ward["id"], "labels": ["1"]}, actor=_admin())["registered"][0]
    resp = client.post("/api/housekeeping/units", headers=_hdr(_user("nurse")), json={
        "unit_code": bed["unit_code"], "ward": "Typed elsewhere", "unit_type": "bed"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "managed_in_beds_setup"


def test_the_patch_routes_change_only_what_was_sent(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    admin = _hdr(_user("admin"))
    ward = client.post("/api/adt/master/wards", headers=admin,
                       json={"code": f"PA-{_tag()}", "department": COVERED, "ward_type": "icu"}).json()
    patched = client.patch(f"/api/adt/master/wards/{ward['id']}", headers=admin,
                           json={"name": "Intensive care", "reason": "Sign on the door"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Intensive care" and patched.json()["ward_type"] == "icu"
    room = client.post("/api/adt/master/rooms", headers=admin,
                       json={"ward_id": ward["id"], "code": "Cubicle 1", "room_type": "icu_cubicle"})
    assert room.status_code == 201, room.text
    beds = client.post("/api/adt/master/beds", headers=admin,
                       json={"ward_id": ward["id"], "labels": ["1"], "room_id": room.json()["id"]})
    bed = beds.json()["registered"][0]
    moved = client.patch(f"/api/adt/master/beds/{bed['id']}", headers=admin, json={"room_id": None})
    assert moved.status_code == 200 and moved.json()["room_id"] is None
    assert moved.json()["bed_label"] == "1"
    code = f"ICU{_tag()}"
    assert client.post("/api/adt/master/bed-classes", headers=admin,
                       json={"code": code, "name": "Intensive care bed"}).status_code == 201
    renamed = client.patch(f"/api/adt/master/bed-classes/{code}", headers=admin,
                           json={"is_critical_care": True})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Intensive care bed"
    missing = client.patch("/api/adt/master/wards/WRD-NOPE", headers=admin, json={"name": "x"})
    assert missing.status_code == 404 and missing.json()["detail"]["error"] == "ward_not_found"
    with db_session() as db:
        reason = db.execute("SELECT reason FROM bed_master_events WHERE entity_id = ? "
                            "AND action = 'updated'", (ward["id"],)).fetchone()[0]
    assert reason == "Sign on the door"
