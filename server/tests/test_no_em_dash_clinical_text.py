"""No em dash in clinical text the app shows (F147, standing rule 2026-09-03).

Drug-interaction mechanisms are shown in the order-time and bedside alerts.
Builtin rows used to be seeded only into an EMPTY table, so a corrected
mechanism never reached a database seeded earlier; ensure_seeded now re-syncs
the curated set once per process (main.py runs it at boot), which is what puts
corrected text into existing databases. The chart timeline's order creation
event used to print an em dash where the prior status goes.
"""
from database import db_session
from services import interaction_db
from services.patient_timeline_service import _r_order_event

EM_DASH = "\u2014"


def test_no_builtin_interaction_mechanism_has_an_em_dash():
    bad = [(a, b) for a, b, _, _, mech in interaction_db._BUILTIN if EM_DASH in mech]
    assert not bad, bad


def test_boot_resync_corrects_an_old_builtin_mechanism(monkeypatch):
    interaction_db.seed_builtin_interactions()
    with db_session() as db:
        row = db.execute("SELECT id FROM drug_interactions WHERE source = 'builtin' "
                         "ORDER BY id LIMIT 1").fetchone()
        db.execute("UPDATE drug_interactions SET mechanism = ? WHERE id = ?",
                   (f"Old text {EM_DASH} as first seeded", row["id"]))
    # A fresh process: the first ensure_seeded re-syncs the curated set.
    monkeypatch.setattr(interaction_db, "_synced_this_process", False)
    interaction_db.ensure_seeded()
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM drug_interactions WHERE source = 'builtin' "
                       "AND instr(mechanism, ?) > 0", (EM_DASH,)).fetchone()[0]
    assert n == 0


def test_order_creation_event_reads_in_words():
    created = _r_order_event({"prior_status": None, "new_status": "draft",
                              "description": "Serum lactate"})
    assert created["title"] == "Order placed (draft)"
    assert EM_DASH not in created["title"]
    moved = _r_order_event({"prior_status": "draft", "new_status": "signed"})
    assert moved["title"] == "Order draft → signed"
