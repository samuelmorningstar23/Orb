"""The Insurance desk's two silent duplications.

F097. Saving an existing payer from the desk sent code, name, type, mode and
pre-auth only, and upsert_payer overwrote EVERY other column with its default.
The tariff link (set through the billing masters) went to NULL, so every patient
of that payer was repriced at the default tariff with no warning. A payer
update is now partial, and clearing a tariff link has to be asked for.

F095. "Open claim" had no busy guard and POST /api/payer/claims no key, so a
double click opened two claim files, each with its own CLM number. The same
idempotency key now returns the claim it opened.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services import payer_service as ps
from services.money import rupees

client = TestClient(app)

TAG = "PPI"
ADMIN = f"{TAG}-ADM"


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module", autouse=True)
def _admin():
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Payer desk', 'admin', NULL, ?)",
            (ADMIN, hash_password("password")),
        )


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"employee_id": ADMIN, "password": "password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _tariff_plan() -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO tariff_plans (code, name, plan_type) VALUES (?, ?, 'tpa')",
            (f"{TAG}-PLAN-{_uid()}", "Negotiated TPA rates"),
        )
        return int(cur.lastrowid)


def _linked_payer() -> tuple[str, int]:
    code = f"{TAG}{_uid()}"
    plan = _tariff_plan()
    ps.upsert_payer(code=code, name="Linked TPA", payer_type="tpa",
                    tariff_plan_id=plan, credit_days=45, gstin="29ABCDE1234F1Z5")
    return code, plan


def _payer_row(code: str) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM payers WHERE code = ?", (code,)).fetchone())


# ── F097: a payer update is partial ───────────────────────────────────────────

def test_renaming_a_payer_keeps_its_tariff_link_and_other_columns():
    code, plan = _linked_payer()
    ps.upsert_payer(code=code, name="Linked TPA (renamed)")
    row = _payer_row(code)
    assert row["name"] == "Linked TPA (renamed)"
    assert row["tariff_plan_id"] == plan, "a rename repriced every patient of this payer"
    assert row["credit_days"] == 45
    assert row["gstin"] == "29ABCDE1234F1Z5"


def test_the_desk_form_save_over_http_keeps_the_tariff_link():
    """Exactly what PayerPanel's form sends: code, name, type, mode, pre-auth."""
    code, plan = _linked_payer()
    resp = client.post(
        "/api/payer/payers",
        json={"code": code, "name": "Linked TPA", "payer_type": "tpa",
              "claim_submission_mode": "email", "preauth_required": True},
        headers=_headers(),
    )
    assert resp.status_code == 200, resp.text
    row = _payer_row(code)
    assert row["tariff_plan_id"] == plan
    assert row["claim_submission_mode"] == "email"
    assert row["preauth_required"] == 1
    assert row["credit_days"] == 45


def test_a_null_tariff_link_is_refused_not_taken_as_remove():
    code, plan = _linked_payer()
    with pytest.raises(ps.PayerError) as exc:
        ps.upsert_payer(code=code, name="Linked TPA", tariff_plan_id=None)
    assert "tariff" in str(exc.value).lower()
    assert _payer_row(code)["tariff_plan_id"] == plan


def test_clearing_a_tariff_link_works_when_asked_for():
    code, _ = _linked_payer()
    ps.upsert_payer(code=code, name="Linked TPA", clear_tariff_plan=True)
    assert _payer_row(code)["tariff_plan_id"] is None


def test_a_new_payer_still_gets_the_defaults():
    code = f"{TAG}{_uid()}"
    row = ps.upsert_payer(code=code, name="Fresh TPA")
    assert row["payer_type"] == "tpa"
    assert row["is_active"] is True
    assert row["preauth_required"] is False
    assert row["tariff_plan_id"] is None


# ── F095: opening a claim is idempotent ───────────────────────────────────────

def _patient_with_cover() -> tuple[str, str]:
    payer = f"{TAG}{_uid()}"
    ps.upsert_payer(code=payer, name="Claims TPA", payer_type="tpa")
    pid = f"{TAG}-PT-{_uid()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, 'Claim Patient', ?, 'W1', '1', 'admitted')",
            (pid, f"MRN-{pid}"),
        )
    ps.capture_policy(patient_id=pid, payer_code=payer, policy_number="POL-1",
                      sum_insured_paise=rupees("100000"), created_by=ADMIN)
    return pid, payer


def _claim_count(pid: str) -> int:
    with db_session() as db:
        return db.execute("SELECT COUNT(*) AS c FROM claims WHERE patient_id = ?",
                          (pid,)).fetchone()["c"]


def test_a_double_submitted_claim_opens_one_file():
    pid, _ = _patient_with_cover()
    body = {"patient_id": pid, "claim_type": "cashless",
            "claimed_paise": rupees("5000"), "idempotency_key": f"k-{_uid()}"}
    first = client.post("/api/payer/claims", json=body, headers=_headers())
    second = client.post("/api/payer/claims", json=body, headers=_headers())
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["claim"]["id"] == second.json()["claim"]["id"]
    assert second.json()["claim"]["replayed"] is True
    assert _claim_count(pid) == 1


def test_without_a_key_two_requests_are_two_claims():
    pid, _ = _patient_with_cover()
    ps.create_claim(patient_id=pid, created_by=ADMIN)
    ps.create_claim(patient_id=pid, created_by=ADMIN)
    assert _claim_count(pid) == 2


def test_a_key_reused_for_another_patient_is_refused():
    pid_a, _ = _patient_with_cover()
    pid_b, _ = _patient_with_cover()
    key = f"k-{_uid()}"
    ps.create_claim(patient_id=pid_a, created_by=ADMIN, idempotency_key=key)
    with pytest.raises(ps.ClaimError):
        ps.create_claim(patient_id=pid_b, created_by=ADMIN, idempotency_key=key)
    assert _claim_count(pid_b) == 0
