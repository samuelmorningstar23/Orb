"""The prep-step list and the WHO checklist no longer share a column.

surgeries.surgical_checklist had two writers that disagreed about its shape.
The AI prep package (sage_handoff_service._persist_package and
surgery_escalation_service._finalize_surgery) writes a LIST of prep steps; the
WHO Surgical Safety Checklist (services/who_checklist.set_item) wrote an OBJECT,
{template, state}, carrying the prep list under 'legacy'. GET /api/surgery/{id}
returned whichever was stored, so the Surgical Suite crashed (`.map` on an
object) as soon as anyone ticked a WHO item. The other way round was worse: a
prep package landing after a tick (an emergency case can be signed in while its
package is still generating) overwrote the OR's safety record with a bare list.

Migration 76 gives the WHO state its own column, surgeries.who_checklist, and
moves every existing WHO payload into it.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app

client = TestClient(app)

PREP = ["Confirm consent", "Cross-match 2 units", "Nil by mouth from midnight"]


@pytest.fixture()
def doctor():
    uid = f"SCDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (uid, f"Doctor {uid}"),
        )
    return uid


@pytest.fixture()
def patient():
    pid = f"PATSC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis) "
            "VALUES (?, ?, ?, 'Ward A', 'A-4', 'Perforated viscus')",
            (pid, "Checklist Patient", f"MRN-{uuid.uuid4().hex[:8]}"),
        )
    yield pid
    with db_session() as db:
        db.execute(
            "DELETE FROM surgery_team_assignments WHERE surgery_id IN "
            "(SELECT id FROM surgeries WHERE patient_id = ?)", (pid,),
        )
        db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, 'doctor')}"}


def _mk_surgery(patient_id, surgeon_id, surgical_checklist=PREP):
    sid = f"SURG{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis,
                                      scheduled_date, scheduled_time, status, surgical_checklist)
               VALUES (?, ?, ?, 'Laparotomy', 'Perforation', '2026-08-14', '09:00',
                       'emergency_ready', ?)""",
            (sid, patient_id, surgeon_id,
             json.dumps(surgical_checklist) if surgical_checklist is not None else None),
        )
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, 'Surgeon', 'confirmed')", (sid, surgeon_id),
        )
    return sid


def _tick(sid, doctor, item_id="si_identity"):
    r = client.post(f"/api/surgery/{sid}/checklist", json={"item_id": item_id, "checked": True},
                    headers=_auth(doctor))
    assert r.status_code == 200, r.text
    return r.json()


def _detail(sid, doctor):
    r = client.get(f"/api/surgery/{sid}", headers=_auth(doctor))
    assert r.status_code == 200, r.text
    return r.json()


def test_ticking_a_who_item_leaves_the_prep_steps_a_list(doctor, patient):
    """The crash: after one tick the detail route handed the panel an object."""
    sid = _mk_surgery(patient, doctor)

    state = _tick(sid, doctor)

    assert state["state"]["si_identity"]["checked"] is True
    assert _detail(sid, doctor)["surgical_checklist"] == PREP


def _finalize_escalation(sid):
    from services.surgery_escalation_service import _finalize_surgery
    _finalize_surgery(sid, {"surgical_checklist": ["Regenerated step"]}, "generated")


def _persist_transfer_package(sid):
    from services.sage_handoff_service import _persist_package
    _persist_package(sid, "Moderate", [], None, ["Regenerated step"], [], [],
                     {"ai_package_status": "generated"})


@pytest.mark.parametrize("write_package", [_finalize_escalation, _persist_transfer_package],
                         ids=["escalation_package", "transfer_package"])
def test_a_prep_package_landing_after_a_tick_keeps_the_who_record(doctor, patient, write_package):
    """The data loss: a package written after a tick used to wipe the WHO state."""
    sid = _mk_surgery(patient, doctor)
    _tick(sid, doctor)

    write_package(sid)

    r = client.get(f"/api/surgery/{sid}/checklist", headers=_auth(doctor))
    assert r.status_code == 200, r.text
    assert r.json()["state"].get("si_identity", {}).get("checked") is True, (
        "the prep package overwrote the WHO checklist, the OR's safety record"
    )
    assert _detail(sid, doctor)["surgical_checklist"] == ["Regenerated step"]


def test_migration_moves_a_who_payload_out_of_the_prep_column(doctor, patient):
    """A row written by the old code: WHO state in surgical_checklist, the prep
    list under 'legacy'. Migration 76 splits it, and running it again moves
    nothing."""
    from migrations_his.m0076_surgery_who_checklist_column import apply

    state = {"si_identity": {"checked": True, "by": doctor, "at": "2026-10-04T08:00:00+00:00"}}
    old_payload = {"template": "who-ssc-2009", "state": state, "legacy": PREP}
    with_legacy = _mk_surgery(patient, doctor, surgical_checklist=old_payload)
    without_legacy = _mk_surgery(patient, doctor,
                                 surgical_checklist={"template": "who-ssc-2009", "state": state})
    untouched = _mk_surgery(patient, doctor)

    with db_session() as db:
        apply(db)
        apply(db)
        rows = {r["id"]: dict(r) for r in db.execute(
            "SELECT id, surgical_checklist, who_checklist FROM surgeries WHERE id IN (?, ?, ?)",
            (with_legacy, without_legacy, untouched)).fetchall()}

    assert json.loads(rows[with_legacy]["surgical_checklist"]) == PREP
    assert json.loads(rows[with_legacy]["who_checklist"]) == {"template": "who-ssc-2009", "state": state}
    assert rows[without_legacy]["surgical_checklist"] is None
    assert json.loads(rows[without_legacy]["who_checklist"])["state"] == state
    assert json.loads(rows[untouched]["surgical_checklist"]) == PREP
    assert rows[untouched]["who_checklist"] is None


def test_the_detail_route_never_hands_the_panel_a_non_list(doctor, patient):
    """Belt and braces for the crash: whatever a future writer stores, the prep
    checklist the panel maps over is a list."""
    sid = _mk_surgery(patient, doctor, surgical_checklist={"unexpected": "shape"})

    assert _detail(sid, doctor)["surgical_checklist"] == []


def test_a_row_left_in_the_old_shape_is_read_and_split_on_the_next_tick(doctor, patient):
    """A WHO payload still in the prep column (written in the moment between
    the migration and the new code, or brought back by an old backup) still
    counts, and the next tick moves it out and puts the prep list back."""
    first = {"si_identity": {"checked": True, "by": doctor, "at": "2026-10-04T08:00:00+00:00"}}
    sid = _mk_surgery(patient, doctor,
                      surgical_checklist={"template": "who-ssc-2009", "state": first, "legacy": PREP})

    r = client.get(f"/api/surgery/{sid}/checklist", headers=_auth(doctor))
    assert r.status_code == 200, r.text
    assert r.json()["state"]["si_identity"]["checked"] is True

    state = _tick(sid, doctor, "si_site_marked")

    assert set(state["state"]) == {"si_identity", "si_site_marked"}
    assert _detail(sid, doctor)["surgical_checklist"] == PREP
    with db_session() as db:
        row = db.execute("SELECT who_checklist FROM surgeries WHERE id = ?", (sid,)).fetchone()
    assert set(json.loads(row["who_checklist"])["state"]) == {"si_identity", "si_site_marked"}
