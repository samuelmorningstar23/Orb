"""Equipment evidence validates dates and preserves corrections without certification."""
import uuid
from datetime import date

import pytest
from database import db_session
from services import radiation_safety_service as svc
from services.offline_workflows import WorkflowError


@pytest.fixture
def equipment():
    tag = "RS-" + uuid.uuid4().hex
    actor = {"id": tag, "role": "admin"}
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,password_hash) VALUES (?,'Equipment test','admin','disabled')", (tag,))
        aid = db.execute("INSERT INTO biomedical_assets(asset_tag,name,category,is_active) VALUES (?,'Test scanner','imaging',1)", (tag,)).lastrowid
    values = {"asset_id": aid, "record_kind": "licence", "reference": tag, "authority": "Recorded issuer",
              "valid_from": "2026-01-01", "valid_to": "2026-10-11", "scope_description": "Source document scope"}
    return actor, values


def test_expiry_is_date_evidence_and_inclusive(equipment, monkeypatch):
    actor, values = equipment
    monkeypatch.setattr(svc, "hospital_today", lambda: date(2026, 10, 11))
    row = svc.create(actor=actor, values=values)
    assert row["date_status"] == "within_recorded_dates"
    monkeypatch.setattr(svc, "hospital_today", lambda: date(2026, 10, 12))
    listed = svc.listing(actor=actor, asset_id=values["asset_id"])
    assert listed["records"][0]["date_status"] == "recorded_expiry_passed"
    with db_session() as db:
        assert db.execute("SELECT status FROM biomedical_assets WHERE id=?", (values["asset_id"],)).fetchone()[0] == "in_service"


def test_bad_dates_missing_officer_and_duplicate_document_refused(equipment):
    actor, values = equipment
    for changes in ({"valid_from": "2026-02-30"}, {"valid_to": "2025-12-01"}, {"record_kind": "officer"}):
        with pytest.raises(WorkflowError):
            svc.create(actor=actor, values={**values, **changes})
    svc.create(actor=actor, values=values)
    with pytest.raises(WorkflowError) as duplicate:
        svc.create(actor=actor, values=values)
    assert duplicate.value.status == 409


def test_withdrawal_preserves_history_and_rejects_stale_version(equipment):
    actor, values = equipment
    row = svc.create(actor=actor, values=values)
    result = svc.withdraw(row["id"], actor=actor, version=1, reason="Source withdrawn by issuer")
    assert result["version"] == 2 and result["state"] == "withdrawn"
    with pytest.raises(WorkflowError):
        svc.withdraw(row["id"], actor=actor, version=1, reason="Stale repeat")
    assert len(svc.history(row["id"], actor=actor)["events"]) == 2


def test_audit_failure_rolls_back_document_and_stored_role_is_authoritative(equipment, monkeypatch):
    actor, values = equipment
    with pytest.raises(WorkflowError) as denied:
        svc.create(actor={"id": "N001", "role": "admin"}, values=values)
    assert denied.value.status == 403
    def fail(*args, **kwargs):
        raise RuntimeError("Audit unavailable")
    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(RuntimeError):
        svc.create(actor=actor, values=values)
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM radiation_safety_records WHERE reference=?", (values["reference"],)).fetchone()[0] == 0


@pytest.fixture
def scoped_equipment(equipment):
    actor, values = equipment
    department = "RS-DEPT-" + uuid.uuid4().hex
    scoped_actor = {"id": "RS-SCOPED-" + uuid.uuid4().hex, "role": "admin"}
    foreign_tag = "RS-FOREIGN-" + uuid.uuid4().hex
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,department,password_hash) VALUES (?,'Scoped equipment test','admin',?,'disabled')", (scoped_actor["id"], department))
        db.execute("UPDATE biomedical_assets SET department=? WHERE id=?", (department, values["asset_id"]))
        foreign = db.execute("INSERT INTO biomedical_assets(asset_tag,name,category,department,is_active) VALUES (?,'Foreign scanner','imaging','RS-OTHER',1)", (foreign_tag,)).lastrowid
    own = svc.create(actor=actor, values=values)
    other = svc.create(actor=actor, values={**values, "asset_id": foreign, "reference": foreign_tag, "valid_from": "2000-01-01", "valid_to": "2000-01-02"})
    return scoped_actor, own, other, department


def test_scoped_register_and_picker_apply_department_before_pagination(scoped_equipment):
    actor, own, other, _ = scoped_equipment
    listed = svc.listing(actor=actor, limit=1)
    assert [row["id"] for row in listed["records"]] == [own["id"]]
    assert listed["total"] == 1
    assert [asset["id"] for asset in listed["assets"]] == [own["asset_id"]]
    assert other["asset_id"] not in [asset["id"] for asset in listed["assets"]]


def test_scoped_admin_cannot_read_or_withdraw_foreign_record(scoped_equipment):
    actor, _, foreign, _ = scoped_equipment
    for operation in (lambda: svc.history(foreign["id"], actor=actor), lambda: svc.withdraw(foreign["id"], actor=actor, version=1, reason="Unauthorized attempt")):
        with pytest.raises(WorkflowError) as denied:
            operation()
        assert denied.value.status == 404
    with db_session() as db:
        row = db.execute("SELECT state,version FROM radiation_safety_records WHERE id=?", (foreign["id"],)).fetchone()
        assert tuple(row) == ("recorded", 1)
        assert db.execute("SELECT COUNT(*) FROM radiation_safety_events WHERE record_id=?", (foreign["id"],)).fetchone()[0] == 1


def test_record_scope_uses_frozen_department_and_picker_uses_current_asset(scoped_equipment):
    actor, own, _, _ = scoped_equipment
    with db_session() as db:
        db.execute("UPDATE biomedical_assets SET department='RS-MOVED' WHERE id=?", (own["asset_id"],))
    assert svc.history(own["id"], actor=actor)["record"]["id"] == own["id"]
    assert svc.listing(actor=actor)["assets"] == []


def test_asset_search_and_pagination_reach_later_equipment(scoped_equipment):
    actor, own, _, department = scoped_equipment
    prefix = "RS-PAGE-" + uuid.uuid4().hex
    with db_session() as db:
        for index in range(4):
            db.execute("INSERT INTO biomedical_assets(asset_tag,name,category,department,is_active) VALUES (?,?, 'imaging',?,1)", (f"{prefix}-{index}", f"Synthetic paged scanner {index}", department))
    first = svc.listing(actor=actor, asset_query=prefix, asset_limit=2)
    assert len(first["assets"]) == 2 and first["asset_total"] == 4 and first["asset_next_offset"] == 2
    second = svc.listing(actor=actor, asset_query=prefix, asset_offset=first["asset_next_offset"], asset_limit=2)
    assert len(second["assets"]) == 2 and second["asset_next_offset"] is None
    assert {a["id"] for a in first["assets"]}.isdisjoint(a["id"] for a in second["assets"])
    exact = svc.listing(actor=actor, asset_query=f"{prefix}-3")
    assert [a["name"] for a in exact["assets"]] == ["Synthetic paged scanner 3"]
    assert svc.listing(actor=actor, asset_query="%' OR 1=1 --")["assets"] == []


def test_withdrawal_audit_failure_rolls_back_state_and_event(scoped_equipment, monkeypatch):
    actor, own, _, _ = scoped_equipment
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")
    monkeypatch.setattr(svc, "audit", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit"):
        svc.withdraw(own["id"], actor=actor, version=1, reason="Synthetic correction")
    with db_session() as db:
        assert db.execute("SELECT state FROM radiation_safety_records WHERE id=?", (own["id"],)).fetchone()[0] == "recorded"
        assert db.execute("SELECT COUNT(*) FROM radiation_safety_events WHERE record_id=?", (own["id"],)).fetchone()[0] == 1
