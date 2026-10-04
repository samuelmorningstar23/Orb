"""The laboratory Setup screen's server contract (lis-4).

The screen (src/pages/panels/lab/SetupView.tsx) is open to every role holding
lab.read, and shows its controls only to a role holding lab.master. The
server is the authority either way. Pinned here:

  * every catalogue write the screen can make answers 403 for an
    administrator, a technician, a phlebotomist, a doctor and a nurse, and
    changes nothing;
  * every read the screen makes answers 200 for an administrator and a
    technician (read only is still readable);
  * the loud counts arrive with the test list.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_master_service as lm
from services import terminology_service

client = TestClient(main.app)

PATHOLOGIST = {"id": "LSWPATH01", "role": "pathologist", "name": "Pathologist Setup"}


@pytest.fixture(scope="module", autouse=True)
def _starter():
    # Terminology first: the catalogue registers only codes terminology lacks,
    # so loading it into an unseeded table would give a curated LOINC code the
    # catalogue's short name for every later test file.
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()


@pytest.fixture(autouse=True)
def _lab_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)


@pytest.fixture
def own_test():
    code = f"ORB-LSW-{uuid.uuid4().hex[:8].upper()}"
    test = lm.create_test({
        "code_system": "LOCAL", "code": code, "name": f"Setup analyte {code}",
        "short_name": "Setup analyte", "section_id": "biochemistry",
        "specimen_type_code": "serum", "unit": "mmol/L", "decimal_places": 1,
    }, actor=PATHOLOGIST)
    rng = lm.approve_reference_range(test["id"], {"low": "1.0", "high": "2.0",
                                                  "method_note": "fixture"}, actor=PATHOLOGIST)
    lim = lm.approve_critical_limit(test["id"], {"critical_high": "9.0", "agreed_with": "fixture",
                                                 "method_note": "fixture"}, actor=PATHOLOGIST)
    yield {"test": test, "range_id": rng["id"], "limit_id": lim["id"]}
    with db_session() as db:
        db.execute("DELETE FROM lab_reference_ranges WHERE test_id = ?", (test["id"],))
        db.execute("DELETE FROM lab_critical_limits WHERE test_id = ?", (test["id"],))
        db.execute("DELETE FROM lab_tests WHERE id = ?", (test["id"],))


def _hdr(role):
    uid = f"LSW{uuid.uuid4().hex[:6].upper()}"
    dept = "General Medicine" if role in ("doctor", "nurse", "ward_clerk") else None
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _writes(fixture):
    tid = fixture["test"]["id"]
    return [
        ("post", "/api/lab/master/tests", {
            "code_system": "LOCAL", "code": f"ORB-LSW-NEW-{uuid.uuid4().hex[:6]}",
            "name": "Refused", "short_name": "Refused", "section_id": "biochemistry",
            "specimen_type_code": "serum"}),
        ("patch", f"/api/lab/master/tests/{tid}", {"name": "Refused rename"}),
        ("put", "/api/lab/master/panels/LOINC:58410-2/components",
         {"component_test_ids": ["LOINC:718-7"]}),
        ("post", "/api/lab/master/ranges", {"test_id": tid, "low": "1.5", "high": "2.5",
                                            "method_note": "refused"}),
        ("post", f"/api/lab/master/ranges/{fixture['range_id']}/retire", {"reason": "refused"}),
        ("post", "/api/lab/master/critical-limits", {"test_id": tid, "critical_high": "8.0",
                                                     "agreed_with": "x", "method_note": "x"}),
        ("post", f"/api/lab/master/critical-limits/{fixture['limit_id']}/retire",
         {"reason": "refused"}),
        ("put", "/api/lab/master/specimen-types/serum", {"display": "Refused", "active": True}),
        ("put", "/api/lab/master/sections/biochemistry", {"name": "Refused", "sort_order": 1,
                                                          "active": True}),
        ("put", "/api/lab/master/rejection-reasons/haemolysed", {"display": "Refused",
                                                                 "incident_prompt": True,
                                                                 "active": True}),
    ]


def _snapshot(tid):
    with db_session() as db:
        return {
            "test": dict(db.execute("SELECT name, version FROM lab_tests WHERE id = ?",
                                    (tid,)).fetchone()),
            "ranges": db.execute("SELECT COUNT(*) FROM lab_reference_ranges WHERE test_id = ? "
                                 "AND effective_to IS NULL", (tid,)).fetchone()[0],
            "limits": db.execute("SELECT COUNT(*) FROM lab_critical_limits WHERE test_id = ? "
                                 "AND effective_to IS NULL", (tid,)).fetchone()[0],
            "serum": db.execute("SELECT display FROM lab_specimen_types WHERE code = 'serum'"
                                ).fetchone()[0],
            "section": db.execute("SELECT name FROM lab_sections WHERE id = 'biochemistry'"
                                  ).fetchone()[0],
            "reason": dict(db.execute("SELECT display, incident_prompt FROM lab_rejection_reasons "
                                      "WHERE code = 'haemolysed'").fetchone()),
            "cbc": [r[0] for r in db.execute(
                "SELECT component_test_id FROM lab_panel_components WHERE panel_test_id = "
                "'LOINC:58410-2' ORDER BY sort_order").fetchall()],
            "refused_tests": db.execute("SELECT COUNT(*) FROM lab_tests WHERE name = 'Refused'"
                                        ).fetchone()[0],
        }


@pytest.mark.parametrize("role", ["admin", "lab_technician", "phlebotomist", "doctor", "nurse"])
def test_every_setup_write_is_403_for_a_role_without_lab_master(role, own_test):
    tid = own_test["test"]["id"]
    before = _snapshot(tid)
    h = _hdr(role)
    for method, path, body in _writes(own_test):
        resp = getattr(client, method)(path, json=body, headers=h)
        assert resp.status_code == 403, (role, method, path, resp.status_code, resp.text)
    assert _snapshot(tid) == before, "a refused write changed the catalogue"


@pytest.mark.parametrize("role", ["admin", "lab_technician"])
def test_every_setup_read_answers_a_reader(role, own_test):
    h = _hdr(role)
    tid = own_test["test"]["id"]
    for path in ("/api/lab/master/tests?include_inactive=true", "/api/lab/master/ranges",
                 "/api/lab/master/critical-limits",
                 "/api/lab/master/specimen-types?include_inactive=true",
                 "/api/lab/master/sections?include_inactive=true",
                 "/api/lab/master/rejection-reasons?include_inactive=true",
                 f"/api/lab/master/tests/{tid}", "/api/lab/master/panels"):
        resp = client.get(path, headers=h)
        assert resp.status_code == 200, (role, path, resp.text)
    body = client.get("/api/lab/master/tests", headers=h).json()
    assert set(body["gaps"]) == {"numeric_tests", "without_reference_range",
                                 "without_critical_limit"}
    detail = client.get(f"/api/lab/master/tests/{tid}", headers=h).json()
    assert [r["id"] for r in detail["reference_ranges"]] == [own_test["range_id"]]
    assert detail["critical_limits"][0]["agreed_with"] == "fixture"


def test_a_pathologist_write_through_the_same_routes_succeeds(own_test):
    h = _hdr("pathologist")
    tid = own_test["test"]["id"]
    resp = client.post("/api/lab/master/ranges", headers=h, json={
        "test_id": tid, "low": "1.5", "high": "2.50", "method_note": "replaces the fixture band"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["display_text"] == "1.5 to 2.5 mmol/L"
