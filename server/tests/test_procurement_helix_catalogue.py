"""A medicine cannot be added to the stores item master by leaving the flag off.

THE FINDING (F103). `inventory_items.is_pharmacy` carries CHECK (is_pharmacy = 0)
and upsert_inventory_item refused only when the caller passed is_pharmacy=True.
The panel never sends that flag, so "Amoxicillin 500 mg" went straight into the
stores master while the screen told the storekeeper the database refuses it.
Two stock records for the same drug is exactly what the boundary exists to stop.

The refusal now compares the item with the Helix catalogue: the code against a
medication id, the name against catalogue and generic names, and a generic or
product stem followed by a strength. A stem with no strength after it is not a
drug ("Insulin syringe 1 ml" stays a stores item).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import procurement_service as svc

client = TestClient(app)

TAG = "PHX"
# Catalogue rows this module owns, removed again at the end.
_MEDS = (
    (f"{TAG}-AMOX-500", "Amoxicillin 500mg Oral Capsule", "Amoxicillin"),
    (f"{TAG}-INS", "Insulin Aspart 100 units/mL Injection", "Insulin"),
    (f"{TAG}-AUG", "Augmentin 625 DUO Tablet", "Amoxicillin/Clavulanate"),
    (f"{TAG}-TZL", "Testozolam 10mg Tablet", "Testozolam"),
)


def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


@pytest.fixture(scope="module", autouse=True)
def catalogue():
    # The Helix tables are created at boot; make sure this DB has them.
    from services.helix_db import init_helix_db
    init_helix_db()
    with db_session() as db:
        for mid, name, generic in _MEDS:
            db.execute(
                "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name) "
                "VALUES (?, ?, ?)", (mid, name, generic),
            )
    yield
    with db_session() as db:
        db.execute("DELETE FROM helix_medication_master WHERE id LIKE ?", (f"{TAG}-%",))
        db.execute("DELETE FROM inventory_items WHERE code LIKE ?", (f"{TAG}%",))


def _item(name: str, code: str | None = None) -> dict:
    return svc.upsert_inventory_item(
        code=code or f"{TAG}I{_uid()}", name=name, category="consumable",
    )


@pytest.mark.parametrize("name", [
    "Amoxicillin 500 mg",          # the finding, with the storekeeper's spacing
    "amoxicillin 500mg oral capsule",
    "Amoxicillin",                 # the generic on its own
    "Testozolam 5 mg",             # a strength the formulary does not stock
    "Augmentin 375 mg",            # brand stem plus a different strength
    "Insulin aspart",              # product stem on its own
])
def test_a_medicine_is_refused_without_the_flag(name):
    with pytest.raises(svc.PharmacyItemRefused) as exc:
        _item(name)
    message = str(exc.value)
    assert "Helix" in message and "pharmacy" in message
    assert name in message, "the refusal must say which entry it refused"


def test_a_helix_medication_id_as_the_code_is_refused():
    with pytest.raises(svc.PharmacyItemRefused):
        _item("Capsules, box", code=f"{TAG}-AMOX-500")


@pytest.mark.parametrize("name", [
    "Insulin syringe 1 ml",        # a device that starts with a drug's name
    "Sterile glove size 7",
    "Amoxi test strip",            # shares letters, not a word
])
def test_stores_items_are_not_caught_by_the_net(name):
    item = _item(name)
    assert item["name"] == name


def test_the_refusal_reaches_the_screen_with_its_sentence():
    uid = f"{TAG}U{_uid()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Stores admin', 'admin', 'x')",
            (uid,),
        )
    response = client.post(
        "/api/procurement/items",
        headers={"Authorization": f"Bearer {create_token(uid, 'admin')}"},
        json={"code": f"{TAG}H{_uid()}", "name": "Amoxicillin 500 mg"},
    )
    # 422 is this route's existing status for the refusal (see test_procurement).
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "pharmacy_item_refused"
    assert "Amoxicillin 500mg Oral Capsule" in detail["message"]
    with db_session() as db:
        assert db.execute(
            "SELECT 1 FROM inventory_items WHERE name = 'Amoxicillin 500 mg'"
        ).fetchone() is None


def test_an_unreadable_catalogue_refuses_rather_than_waving_through(monkeypatch):
    from services import helix_service

    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(helix_service, "get_medication_catalog", broken)
    with pytest.raises(svc.ProcurementError) as exc:
        _item("Sterile glove size 8")
    assert exc.value.code == "pharmacy_check_unavailable"
