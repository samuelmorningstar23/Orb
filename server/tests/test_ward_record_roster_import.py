"""The roster import under the Orb Ward Record profile (founding ward list
item 3, 21 October 2026).

The roster import is how the ward's patient list reaches the observation
record on day one, and it needs no model. Nothing on the server ever hid it
under the profile: these pin that the field catalogue, a dry run and the run
history answer as they do on an ordinary site while ORB_WARD_RECORD is on, so
the Admin section the screen now lists (AdminPanel WARD_RECORD_ADMIN_TABS)
has a working route behind it. They also pin that the import calls no model
and raises no score on the way in: a planned row is a patients and admissions
row, nothing more.
"""

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from routes.roster_import_routes import router as roster_router
from services import roster_import


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(roster_router)
    return TestClient(app)


@pytest.fixture()
def admin_user():
    uid = f"WRRI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'admin', 'x')",
            (uid, "Ward Record Roster Admin"),
        )
    return uid


@pytest.fixture()
def ward_record(monkeypatch):
    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    monkeypatch.setattr(config, "SCORING_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "MODELS_ENABLED", False, raising=False)
    yield


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, 'admin')}"}


def _upload(mrn: str) -> dict:
    data = (
        f"MRN,Name,Age,Sex,Ward,Bed,Diagnosis\n{mrn},Ward Record Roster,61,F,GM-1,GM-07,Pneumonia\n"
    ).encode()
    return {"file": ("roster.csv", data, "text/csv")}


MAPPING = {
    "mrn": "MRN",
    "name": "Name",
    "age": "Age",
    "sex": "Sex",
    "ward": "Ward",
    "bed": "Bed",
    "diagnosis": "Diagnosis",
}


def test_the_field_catalogue_answers_under_the_profile(client, admin_user, ward_record):
    resp = client.get("/api/admin/roster-import/fields", headers=_auth(admin_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fields"] == roster_import.field_catalogue()
    assert "mrn" in json.dumps(body["fields"])
    assert set(body["required"]) == set(roster_import.REQUIRED_FIELDS)


def test_a_dry_run_plans_the_ward_list_under_the_profile(client, admin_user, ward_record):
    mrn = f"WRRI{uuid.uuid4().hex[:6].upper()}"
    resp = client.post(
        "/api/admin/roster-import/dry-run",
        files=_upload(mrn),
        data={"mapping": json.dumps(MAPPING), "default_department": "General Medicine"},
        headers=_auth(admin_user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["counts"]["create"] == 1
    row = next(r for r in body["rows"] if r["mrn"] == mrn)
    assert row["action"] == "create"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE mrn = ?", (mrn,)).fetchone()[0] == 0


def test_the_run_history_answers_under_the_profile(client, admin_user, ward_record):
    resp = client.get("/api/admin/roster-import/runs", headers=_auth(admin_user))
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json().get("runs", resp.json()), list)


def test_the_import_reaches_no_model_and_no_score(ward_record, monkeypatch):
    """A planned import is patients and admissions rows. No lane asks a model
    and none scores: the profile forces both off, and the importer never
    called them anyway."""
    from services import llm_providers, vigil_service

    def _no_model(*a, **k):
        raise AssertionError("the roster import asked a model")

    def _no_score(*a, **k):
        raise AssertionError("the roster import scored a patient")

    for name in ("generate", "chat", "complete"):
        if hasattr(llm_providers, name):
            monkeypatch.setattr(llm_providers, name, _no_model)
    monkeypatch.setattr(vigil_service, "compute_news2", _no_score)
    mrn = f"WRRI{uuid.uuid4().hex[:6].upper()}"
    content = _upload(mrn)["file"][1]
    plan = roster_import.plan(content, MAPPING, default_department="General Medicine")
    assert plan["counts"]["create"] == 1
