"""The formulary loader never rewrites the clinical identity of a drug in use
(grn-5, consistent with every option of ruling GRN-RS1).

scripts/load_rxnorm_formulary.py used to upsert generic_name, form and
strength over existing rows with no audit. generic_name is what the allergy
and interaction screens read, so a bulk file could turn a charted, stocked
ceftriaxone into something else under the same id. Now:
  * a row with a Helix order (prescribed or substituted) or a stock lot keeps
    its generic name, form and strength, and is listed in the report;
  * a row in use whose identity the file repeats still takes a new brand name;
  * a row not in use is updated as before, and a new row is added;
  * every run writes one 'security' audit row with its counts.
"""
from __future__ import annotations

import json
import uuid

import pytest

from database import db_session
from scripts.load_rxnorm_formulary import load_file, load_file_report
from services.helix_db import init_helix_db


@pytest.fixture(scope="module", autouse=True)
def _init():
    init_helix_db()


def _drug(med_id, generic="ceftriaxone", form="Vial", strength="1g", name=None):
    with db_session() as db:
        db.execute("INSERT INTO helix_medication_master (id, name, generic_name, form, strength) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (med_id, name or f"Loader {med_id}", generic, form, strength))


def _order(med_id, *, substitute=False):
    pid = f"PATFL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, f"Loader Case {pid[-4:]}", f"MRN-{pid}"))
        other = f"RX-FLOTHER{uuid.uuid4().hex[:5]}"
        if substitute:
            _drug(other, generic="other")
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, "
                   "substitute_medication_id, status) VALUES (?, ?, ?, ?, 'pending')",
                   (f"ORDFL{uuid.uuid4().hex[:8]}", pid, other if substitute else med_id,
                    med_id if substitute else None))


def _lot(med_id):
    with db_session() as db:
        db.execute("INSERT INTO helix_inventory_lots (id, medication_id, lot_number, quantity, "
                   "expiration_date) VALUES (?, ?, 'B1', 0, '2030-01-01')",
                   (f"LOTFL{uuid.uuid4().hex[:8]}", med_id))


def _row(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT name, generic_name, form, strength FROM "
                               "helix_medication_master WHERE id = ?", (med_id,)).fetchone())


def _csv(tmp_path, rows):
    path = tmp_path / f"formulary-{uuid.uuid4().hex[:6]}.csv"
    lines = ["rxcui,name,generic_name,form,strength"] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_drug_with_an_order_or_a_lot_keeps_its_identity(tmp_path):
    tag = uuid.uuid4().hex[:6].upper()
    ordered, stocked, subbed, free = (f"RX-FL{tag}{n}" for n in ("A", "B", "C", "D"))
    for med in (ordered, stocked, subbed, free):
        _drug(med)
    _order(ordered)
    _lot(stocked)
    _order(subbed, substitute=True)

    path = _csv(tmp_path, [
        (ordered, "Renamed A", "amoxicillin", "Capsule", "500mg"),
        (stocked, "Renamed B", "ceftriaxone", "Vial", "2g"),
        (subbed, "Renamed C", "ceftriaxone", "Tablet", "1g"),
        (free, "Renamed D", "cefixime", "Tablet", "200mg"),
    ])
    report = load_file_report(path)
    assert report["read"] == 4 and report["upserted"] == 1
    assert sorted(r["id"] for r in report["refused"]) == sorted([ordered, stocked, subbed])
    refused = next(r for r in report["refused"] if r["id"] == ordered)
    assert refused["current"] == {"generic_name": "ceftriaxone", "form": "Vial",
                                  "strength": "1g"}
    assert refused["incoming"]["generic_name"] == "amoxicillin"

    for med in (ordered, stocked, subbed):
        row = _row(med)
        assert (row["generic_name"], row["form"], row["strength"]) == ("ceftriaxone", "Vial",
                                                                       "1g")
        assert row["name"] == f"Loader {med}", "a refused row is left exactly as it was"
    assert _row(free) == {"name": "Renamed D", "generic_name": "cefixime", "form": "Tablet",
                          "strength": "200mg"}


def test_a_drug_in_use_still_takes_a_new_brand_name_and_new_rows_are_added(tmp_path):
    tag = uuid.uuid4().hex[:6].upper()
    in_use, new = f"RX-FL{tag}E", f"RX-FL{tag}N"
    _drug(in_use)
    _lot(in_use)
    path = _csv(tmp_path, [(in_use, "Monocef 1g Vial", "ceftriaxone", "Vial", "1g"),
                           (new, "Brand new", "linezolid", "Tablet", "600mg")])
    assert load_file(path) == (2, 2)
    assert _row(in_use)["name"] == "Monocef 1g Vial"
    assert _row(new)["generic_name"] == "linezolid"


def test_every_run_is_audited_with_its_counts(tmp_path):
    tag = uuid.uuid4().hex[:6].upper()
    med = f"RX-FL{tag}F"
    _drug(med)
    _order(med)
    path = _csv(tmp_path, [(med, "X", "other", "Vial", "1g")])
    load_file_report(path)
    with db_session() as db:
        row = db.execute("SELECT * FROM audit_logs WHERE action_type = "
                         "'pharmacy_formulary_loaded' AND target_id = ?",
                         (path.name,)).fetchone()
    assert row is not None and row["event_category"] == "security"
    meta = json.loads(row["metadata_json"])
    assert meta == {"read": 1, "upserted": 0, "refused_ids": [med]}
