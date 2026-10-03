"""Phase 2: model governance registry — honest provenance/validation surface."""
import ast
import os
from unittest import mock

import migrations
import services.model_governance_service as mgs
from database import db_session
from services.model_governance_service import list_models, seed_default_models

# The models Orb ships. Asserted as an EXACT set everywhere below, never as a
# subset: `<=` is what let a retired model sit in this registry for a month
# (see test_a_retired_model_is_not_served_again and migration 38).
#
# Derived from the resolved config since 2026-10-03: the Sage rows follow
# LLM_MODEL / THINKING_FAST_MODEL / AMBIENT_LLM_MODEL, so the 4B appears exactly
# when it serves turns. The four below are always present.
ALWAYS = {"vigil_news2", "vigil_cfc", "forecast_lstm", "sage_llm",
          "sage_embedding", "scribe_asr"}
SHIPPED = {m["model_key"] for m in mgs._default_models()}
# Migration 38's frozen key set: what _default_models() shipped when it was written.
M38_KNOWN = {"vigil_news2", "vigil_cfc", "forecast_lstm", "sage_llm"}


def _insert_retired_row(key="sage_llm_hosted"):
    """Put a model in the registry that `_default_models()` does not ship."""
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO model_registry
                   (model_key, version, task, training_data, validated,
                    validation_metrics, calibration_stats, is_active,
                    checkpoint_present, notes)
               VALUES (?, 'unconfigured', 'Hosted PHI-free lane', 'Hosted provider',
                       0, '{}', NULL, 0, 0, 'retired surface')""",
            (key,),
        )


def _keys():
    return {m["model_key"] for m in list_models()}


def test_registry_seeds_and_lists():
    seed_default_models()
    assert _keys() == SHIPPED
    assert ALWAYS <= SHIPPED


def _keys_with(**config_values):
    with mock.patch.multiple("config", **config_values):
        return {m["model_key"]: m for m in mgs._default_models()}


def test_the_fast_model_is_listed_when_it_serves_turns():
    """Model Governance said the 27B was the only lane while the 4B answered
    every thinking-off turn."""
    rows = _keys_with(LLM_MODEL="medgemma:27b", THINKING_FAST_MODEL="medgemma:4b",
                      THINKING_FAST_MODE_ENABLED=True, AMBIENT_LLM_MODEL="medgemma:27b")
    assert rows["sage_llm"]["version"] == "medgemma:27b"
    assert rows["sage_llm_fast"]["version"] == "medgemma:4b"
    assert "sage_llm_ambient" not in rows
    assert "only inference lane" not in rows["sage_llm"]["notes"].lower()


def test_no_fast_row_when_fast_mode_falls_back_to_the_careful_model():
    rows = _keys_with(LLM_MODEL="medgemma:27b", THINKING_FAST_MODEL="medgemma:27b",
                      THINKING_FAST_MODE_ENABLED=True, AMBIENT_LLM_MODEL="medgemma:27b")
    assert "sage_llm_fast" not in rows
    rows = _keys_with(LLM_MODEL="medgemma:27b", THINKING_FAST_MODEL="medgemma:4b",
                      THINKING_FAST_MODE_ENABLED=False, AMBIENT_LLM_MODEL="medgemma:27b")
    assert "sage_llm_fast" not in rows


def test_a_distinct_ambient_model_gets_its_own_row():
    rows = _keys_with(LLM_MODEL="medgemma:27b", THINKING_FAST_MODEL="medgemma:4b",
                      THINKING_FAST_MODE_ENABLED=True, AMBIENT_LLM_MODEL="qwen:7b")
    assert rows["sage_llm_ambient"]["version"] == "qwen:7b"
    # Shared with the fast model: one row, both jobs listed on it.
    rows = _keys_with(LLM_MODEL="medgemma:27b", THINKING_FAST_MODEL="medgemma:4b",
                      THINKING_FAST_MODE_ENABLED=True, AMBIENT_LLM_MODEL="medgemma:4b")
    assert "sage_llm_ambient" not in rows
    assert any("Relay" in line for line in rows["sage_llm_fast"]["impacts"])


def test_training_data_follows_the_checkpoint_and_says_no_demo():
    with mock.patch.object(mgs, "_checkpoint_exists", return_value=False):
        rows = {m["model_key"]: m for m in mgs._default_models()}
    assert rows["forecast_lstm"]["training_data"].startswith("Not trained")
    assert rows["vigil_cfc"]["training_data"].startswith("Not trained")
    with mock.patch.object(mgs, "_checkpoint_exists", return_value=True):
        rows = {m["model_key"]: m for m in mgs._default_models()}
    assert "synthetic" in rows["forecast_lstm"]["training_data"].lower()
    for m in rows.values():
        assert "demo" not in m["training_data"].lower()


def test_listed_rows_carry_a_display_name_and_what_they_serve():
    seed_default_models()
    for m in list_models():
        assert m["display_name"] and m["display_name"] != m["model_key"]
        assert isinstance(m["impacts"], list)


def test_untrained_models_are_not_marked_validated():
    seed_default_models()
    by_key = {m["model_key"]: m for m in list_models()}
    # The neural models are NOT validated on real clinical data — the honesty layer
    # must never claim otherwise.
    assert by_key["forecast_lstm"]["validated"] == 0
    assert by_key["vigil_cfc"]["validated"] == 0
    # NEWS2 is a published validated standard.
    assert by_key["vigil_news2"]["validated"] == 1


def test_checkpoint_presence_is_boolean_and_consistent():
    seed_default_models()
    by_key = {m["model_key"]: m for m in list_models()}
    # If the CfC has no checkpoint on disk, it must be inactive (fails closed).
    cfc = by_key["vigil_cfc"]
    if not cfc["checkpoint_present"]:
        assert cfc["is_active"] == 0


def test_a_retired_model_is_not_served_again():
    """Seeding must DROP a model Orb no longer ships, not merely ignore it.

    The regression this pins: `sage_llm_hosted` was removed from
    `_default_models()` on 2026-08-09 and was still being served a month later,
    because the DELETE that retires a row lives in `seed_default_models()` and
    nothing on a populated database called it. It reached the Model Governance
    screen AND `compliance_service._model_posture`, which reports
    `total = len(models)` straight into the compliance posture export.
    """
    _insert_retired_row()
    assert "sage_llm_hosted" in _keys(), "fixture did not take"
    seed_default_models()
    keys = _keys()
    assert "sage_llm_hosted" not in keys
    # Exact, not a subset. A subset assertion is what hid this originally.
    assert keys == SHIPPED


def test_migration_38_retires_unknown_models_on_an_existing_database():
    """Migration 38 is what repairs boxes that are ALREADY stale.

    The lifespan reconcile in main.py stops the next retirement drifting, but it
    only helps a box that reboots. This migration is what makes an upgrade repair
    the estate, which matters because the compliance export is generated on demand
    from whatever the registry holds at that moment.
    """
    _insert_retired_row()
    with db_session() as db:
        migrations._m38_retire_unknown_models(db)
        remaining = {
            r[0] for r in db.execute("SELECT model_key FROM model_registry").fetchall()
        }
    assert "sage_llm_hosted" not in remaining
    # The frozen set: rows added after migration 38 was written come back on
    # the next seed, which main.py's lifespan runs straight after init_db().
    assert remaining == SHIPPED & M38_KNOWN
    seed_default_models()
    assert _keys() == SHIPPED


def test_reading_the_registry_never_writes_to_it():
    """PINS A DESIGN DECISION, and will fail if someone "fixes" the bug the
    tempting way.

    `list_models()` must NOT reconcile on a populated registry. It backs
    `GET /admin/models/provenance`, which any authenticated clinician hits inline
    on an AI output, and SQLite is single-writer, so reconciling there would take
    a write lock per client for zero needed writes. It would also be an unreliable
    repair: a box nobody opened would stay wrong, making the moment of repair
    depend on traffic rather than deployment.

    So a stale row surviving a READ is correct behaviour. Migration 38 and the
    main.py lifespan hook are what remove it. If this test fails, someone has moved
    reconciliation onto the read path; fix it at the boot path instead.
    """
    _insert_retired_row()
    try:
        assert "sage_llm_hosted" in _keys()
        assert "sage_llm_hosted" in _keys(), "a second read must not repair it either"
    finally:
        seed_default_models()  # leave the registry clean for other tests
    assert _keys() == SHIPPED


# ── The retired VERSION, which the key-only reconcile could not see ──────────
# Everything above is about a retired model KEY. A key Orb still ships can also
# strand a row, because the upsert conflicts on (model_key, version) while the
# reconcile only knew about keys. This is the likelier of the two failures: it
# needs no retirement at all, only a checkpoint appearing, which is the intended
# lifecycle of both untrained models.


def _rows():
    with db_session() as db:
        return [(r["model_key"], r["version"])
                for r in db.execute(
                    "SELECT model_key, version FROM model_registry").fetchall()]


def test_a_version_change_moves_the_row_instead_of_adding_one():
    """THE REGRESSION. `_default_models()` reads two versions off the FILESYSTEM:
    forecast_lstm is "untrained" or "synthetic-v1" depending on whether a
    checkpoint is present, and vigil_cfc moves with its checkpoint the same way.
    So the day anyone trains the forecast model, a key-only reconcile leaves a
    permanent phantom `forecast_lstm/untrained` beside the real row."""
    seed_default_models()
    before = _rows()
    assert ("forecast_lstm", "untrained") in before, \
        "this box is expected to have no forecast checkpoint; the fixture has moved"

    with mock.patch.object(mgs, "_checkpoint_exists", return_value=True):
        seed_default_models()
    after = _rows()
    try:
        assert ("forecast_lstm", "synthetic-v1") in after, "the new version must be recorded"
        assert ("forecast_lstm", "untrained") not in after, (
            "the superseded version survived, so the registry now claims Orb ships "
            "two forecast models and the compliance export over-counts by one"
        )
        assert len(after) == len(before), "one surface, one row"
    finally:
        seed_default_models()
    assert _rows() == before, "and it round-trips when the checkpoint goes away"


def test_the_compliance_export_counts_what_orb_actually_ships():
    """WHY IT MATTERS, rather than a row count. `_model_posture()` feeds
    `compliance_report()`, which is the posture a CIO reads. A stranded row
    inflates `total` and `unvalidated` there, in writing, on an appliance whose
    pitch is that it does not over-claim."""
    from services.compliance_service import _model_posture

    seed_default_models()
    baseline = _model_posture()
    # ASSERT THE PRECONDITION, or this test passes on a registry an earlier
    # failure left polluted: it would take the inflated count as its baseline and
    # then agree with itself. Caught exactly that way during prove-can-fail.
    assert baseline["total"] == len(SHIPPED), (
        f"the registry was not clean at entry ({baseline['total']} models, "
        f"expected {len(SHIPPED)}), so this test cannot measure anything"
    )
    assert baseline["total"] == baseline["validated"] + baseline["unvalidated"]

    with mock.patch.object(mgs, "_checkpoint_exists", return_value=True):
        seed_default_models()
        posture = _model_posture()
    try:
        assert posture["total"] == baseline["total"], (
            f"training a model changed the MODEL COUNT in the compliance export "
            f"({baseline['total']} -> {posture['total']}); it should change a "
            f"version, not add a surface"
        )
        assert posture["unvalidated"] == baseline["unvalidated"]
    finally:
        seed_default_models()


def test_retiring_a_whole_key_still_works():
    """The reconcile got NARROWER in one sense (it now compares pairs), so the
    original case has to be re-pinned: a retired key cannot appear in any pair,
    so it is still dropped."""
    seed_default_models()
    _insert_retired_row()
    assert "sage_llm_hosted" in _keys()
    seed_default_models()
    assert _keys() == SHIPPED


def _main_lifespan_ast():
    """main.py's lifespan, out of the parse tree — same technique as
    tests/test_sage_provenance.py, for the same reason: "reconciled at startup"
    is a claim a comment can lie about while nothing calls anything."""
    main_path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    with open(main_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py has no `lifespan` coroutine")


def test_the_registry_is_reconciled_from_lifespan():
    """seed_default_models() must have a CALLER at boot.

    This is the half of the fix that migration 38 cannot provide. The migration
    repairs databases that are stale TODAY and then never runs again; only a
    per-boot reconcile stops the NEXT retired model drifting the same way. Without
    it the whole defect returns the first time a model leaves `_default_models()`,
    and every test in this file would still pass, because they all seed explicitly.
    """
    lifespan = _main_lifespan_ast()
    called = [
        node for node in ast.walk(lifespan)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "seed_default_models"
    ]
    assert called, (
        "seed_default_models() is never called from main.py's lifespan — nothing "
        "reconciles the model registry at boot, so the next retired model will be "
        "served indefinitely on every existing database (see migration 38)")
