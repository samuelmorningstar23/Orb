"""The mounted application, switch board and session gate agree for local modules."""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app

SURFACES = [
    ("feedback", "FEEDBACK_ENABLED", "/api/feedback"),
    ("mortuary", "MORTUARY_ENABLED", "/api/mortuary"),
    ("cssd", "CSSD_ENABLED", "/api/cssd"),
    ("reporting", "REPORTING_ENABLED", "/api/reporting/catalog"),
    ("finance-tools", "FINANCE_TOOLS_ENABLED", "/api/finance-tools/meta"),
    ("radiology", "RADIOLOGY_ENABLED", "/api/radiology/meta"),
    ("care-plans", "CARE_PLANS_ENABLED", "/api/care-plans/meta"),
    ("emergency", "EMERGENCY_ENABLED", "/api/emergency/meta"),
    ("perioperative", "PERIOPERATIVE_ENABLED", "/api/perioperative/meta"),
    ("dialysis", "DIALYSIS_ENABLED", "/api/dialysis/meta"),
    ("record-governance", "RECORD_GOVERNANCE_ENABLED", "/api/record-governance/hold"),
    ("radiation-safety", "RADIATION_SAFETY_ENABLED", "/api/radiation-safety"),
    ("infection-surveillance", "INFECTION_SURVEILLANCE_ENABLED", "/api/infection-surveillance"),
]


@pytest.fixture(scope="module")
def admin_headers():
    uid = "LOCAL-INTEGRATION-" + uuid.uuid4().hex
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,password_hash,is_active) VALUES (?,'Local integration','admin','disabled',1)", (uid,))
    return {"Authorization": "Bearer " + create_token(uid, "admin")}


@pytest.mark.parametrize("module,constant,path", SURFACES)
def test_module_is_mounted_and_default_off(module, constant, path, admin_headers, monkeypatch):
    monkeypatch.setattr(config, constant, False)
    client = TestClient(app)
    disabled = client.get(path, headers=admin_headers)
    assert disabled.status_code == 404, disabled.text
    assert disabled.json()["detail"]["error"] == "module_disabled"
    assert disabled.json()["detail"].get("module", module) == module
    assert config.module_states()[module] == "off"
    assert client.get(path).status_code == 401
    monkeypatch.setattr(config, constant, True)
    assert config.module_states()[module] == "on"
    assert client.get(path).status_code == 401
    enabled = client.get(path, headers=admin_headers)
    assert enabled.status_code == 200, enabled.text

