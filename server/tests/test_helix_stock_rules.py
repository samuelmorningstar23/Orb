"""What counts as stock, and what may be suggested when there is none.

Two defects in one place (helix_service):

  * Dispense picked the earliest-expiring lot with no date filter, so an
    expired lot was always the FIRST one dispensed, and a lot with no expiry
    (NULL sorts first in SQLite) went ahead of everything. Pre-flight counted
    expired units as available.
  * The stockout lookup fell back to a hard-coded class map that offered
    warfarin for heparin and sodium chloride for potassium, and matched
    "equivalents" on a generic name the seed had mislabelled on purpose.
"""
import uuid
from datetime import date, timedelta

import pytest

from database import db_session
from services import helix_service
from services.helix_db import init_helix_db

_PAST = (date.today() - timedelta(days=30)).isoformat()
_SOON = (date.today() + timedelta(days=60)).isoformat()
_LATER = (date.today() + timedelta(days=400)).isoformat()


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


def _med(generic, strength="10mg", form="Tablet", name=None):
    mid = f"HXSTK{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, name or f"Stockrule {generic} {strength} {mid[-4:]}", generic, form, strength),
        )
    return mid


def _lot(med_id, qty, expiry):
    lot_id = f"LOTSTK{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, expiration_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (lot_id, med_id, lot_id, qty, expiry),
        )
    return lot_id


def _qty(lot_id):
    with db_session() as db:
        return db.execute("SELECT quantity FROM helix_inventory_lots WHERE id = ?", (lot_id,)).fetchone()[0]


def _dispense(med_id):
    pid = f"PATSTK{uuid.uuid4().hex[:6].upper()}"
    oid = f"ORDSTK{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            (pid, f"Stock Case {pid[-4:]}", f"MRN-{pid}"),
        )
        db.execute(
            "INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, dosage, route, frequency, status) "
            "VALUES (?, ?, ?, '1', 'PO', 'OD', 'scheduled')",
            (oid, pid, med_id),
        )
    with db_session() as db:
        movement = helix_service._atomic_dispense(db, oid, med_id, "tester")
        lot = db.execute("SELECT lot_id FROM helix_stock_movements WHERE id = ?", (movement,)).fetchone()
        db.commit()
    return lot["lot_id"]


# ── expired stock ─────────────────────────────────────────────────────────────
def test_an_expired_lot_with_stock_is_never_dispensed():
    med = _med("stockrulemol-a")
    expired = _lot(med, 50, _PAST)
    good = _lot(med, 5, _LATER)
    assert _dispense(med) == good
    assert _qty(expired) == 50
    assert _qty(good) == 4


def test_a_lot_with_no_expiry_is_never_dispensed():
    med = _med("stockrulemol-b")
    undated = _lot(med, 50, None)
    good = _lot(med, 5, _LATER)
    assert _dispense(med) == good
    assert _qty(undated) == 50


def test_in_date_lots_still_go_earliest_expiry_first():
    med = _med("stockrulemol-c")
    later = _lot(med, 5, _LATER)
    sooner = _lot(med, 5, _SOON)
    assert _dispense(med) == sooner
    assert _qty(later) == 5


def test_only_expired_stock_refuses_the_dispense_and_says_why():
    med = _med("stockrulemol-d")
    expired = _lot(med, 12, _PAST)
    with pytest.raises(ValueError, match="expired or have no expiry"):
        _dispense(med)
    assert _qty(expired) == 12


def test_pre_flight_counts_in_date_stock_only():
    med = _med("stockrulemol-e")
    _lot(med, 40, _PAST)
    _lot(med, 7, None)
    assert helix_service.pre_flight_inventory_check(med) == {
        "medication_id": med, "in_stock": False, "total_quantity": 0,
    }
    _lot(med, 3, _LATER)
    check = helix_service.pre_flight_inventory_check(med)
    assert check["in_stock"] is True and check["total_quantity"] == 3


# ── what a stockout may suggest ───────────────────────────────────────────────
def test_heparin_stockout_never_suggests_warfarin():
    heparin = _med("Heparin", "5000 units/mL", "Vial", name="Heparin Stockrule 5000 units/mL")
    warfarin = _med("Warfarin", "5mg", "Tablet", name="Warfarin Stockrule 5mg Tablet")
    _lot(warfarin, 100, _LATER)
    assert helix_service.pre_flight_alternative_suggestion(heparin) is None


def test_potassium_stockout_never_suggests_sodium_chloride():
    potassium = _med("Potassium", "20mEq", "Ampoule", name="Potassium Chloride Stockrule 20mEq")
    saline = _med("Sodium Chloride", "0.9%", "Bag", name="Sodium Chloride Stockrule 0.9%")
    _lot(saline, 100, _LATER)
    assert helix_service.pre_flight_alternative_suggestion(potassium) is None


def test_same_molecule_at_another_strength_is_not_an_equivalent():
    mol = "stockrulemol-f"
    wanted = _med(mol, "500mg", "Capsule")
    other = _med(mol, "250mg", "Capsule")
    _lot(other, 100, _LATER)
    assert helix_service.pre_flight_alternative_suggestion(wanted) is None


def test_an_exact_equivalent_with_in_date_stock_is_suggested():
    mol = "stockrulemol-g"
    wanted = _med(mol, "500 mg", "Capsule")
    expired_brand = _med(mol.upper(), "500mg", "capsule")
    _lot(expired_brand, 100, _PAST)
    assert helix_service.pre_flight_alternative_suggestion(wanted) is None, \
        "an equivalent with only expired stock is not in stock"
    good_brand = _med(mol, "500mg", "CAPSULE")
    _lot(good_brand, 9, _LATER)
    alt = helix_service.pre_flight_alternative_suggestion(wanted)
    assert alt is not None and alt["id"] == good_brand
    assert alt["stock_status"] == {"in_stock": True, "total_quantity": 9}


def test_inventory_marks_each_lot_with_the_dispense_rule():
    med = _med("stockrulemol-h")
    expired, undated, good = _lot(med, 4, _PAST), _lot(med, 4, None), _lot(med, 4, _LATER)
    lots = {l["id"]: l for l in helix_service.get_inventory() if l["medication_id"] == med}
    assert lots[expired]["in_date"] == 0
    assert lots[undated]["in_date"] == 0
    assert lots[good]["in_date"] == 1


def test_expiry_cutoff_is_the_hospital_date_from_site_config(monkeypatch):
    """At 01:00 in Kolkata the UTC date is still yesterday. The cutoff must be
    the hospital's date, or a lot that expired yesterday stays dispensable."""
    from types import SimpleNamespace
    from services import site_config_service

    monkeypatch.setattr(site_config_service, "current_site_config",
                        lambda: SimpleNamespace(timezone="Pacific/Kiritimati"))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    assert helix_service._expiry_cutoff() == datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()

    monkeypatch.setattr(site_config_service, "current_site_config",
                        lambda: SimpleNamespace(timezone=None))
    assert helix_service._expiry_cutoff() == date.today().isoformat()
