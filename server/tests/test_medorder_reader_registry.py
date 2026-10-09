"""Every module that reads FROM medications, and what a hold does to it
(medorders-6b, W7-E; design section 7, "HELD STATE IN EVERY READER").

A held medication order keeps its ward row 'active' or 'prn' (so the
interaction screen and the regimen still see the drug) and sets held_at and
hold_reason. So a reader that lists drugs as being given, and has never heard
of held_at, would quietly present a held drug as current. This registry is the
list of readers and the reason each is safe; the test fails on any module that
reads FROM medications and is not in it (a new reader must be classified before
it lands), and on an entry that no longer reads the table.

What counts as a read: a SQL string literal (an f-string's literal part
included) naming FROM medications or JOIN medications, in any module under
server/ except the tests and the data folders (datasets, eval, corpus, models,
uploads, docs, downtime_exports), which hold no server code. Docstrings and
bare string statements are not reads.

Kinds:
  dose          drives a dose or a due time. The bedside decides per row: the
                eMAR's link check refuses 'given' on a held order (W5-E); the
                nurse board, the paper MAR and the voice agent are medorders-6a
                (W6-D), which no wave has built yet, and no route can hold an
                order until it is (only the store's own writer can).
  labelled      shows or hands on a drug list: a held drug says 'on hold'
                (services/medorders/held.py). Each of these reads held_at.
  kept          a screening set: a held drug is still in the patient, so it
                stays in the set.
  raw           hands the rows to a screen as they are (SELECT *), held_at
                included.
  count         a count or a summary, unchanged by a hold and stated so.
  verification  the pharmacist's queue: a held order still needs its check,
                and the store's legacy mirror keeps the hold
                (core.legacy_decision_on_held_in_txn).
  history       administration history: doses already given.
  lookup        one row by id or key, for a scope check or an idempotent
                replay; no drug list.
  store         the medication-order store and its lanes
                (services/medorders/), which read the marker themselves.
  tool          a development or benchmark script the server never runs.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".venv", "tests", "__pycache__", "node_modules",
              # Data folders: tens of thousands of files and no server code.
              "datasets", "eval", "corpus", "models", "uploads", "docs", "downtime_exports"}
READ_SQL = re.compile(r"\b(FROM|JOIN)\s+medications\b", re.IGNORECASE)

#: Every module under server/services/medorders/ is the store itself.
STORE_PREFIX = "services/medorders/"

KINDS = ("dose", "labelled", "kept", "raw", "count", "verification", "history", "lookup",
         "store", "tool")

REGISTRY: dict[str, tuple[str, str]] = {
    # Drives a dose or a due time.
    "services/emar_service.py": (
        "dose", "record_administration; the linked order's state is checked through "
                "services/medorders/emar_check.py and a held order refuses 'given' (W5-E). "
                "The administration-history joins read doses already given."),
    "services/nurse_shift_service.py": (
        "dose", "the round's due and overdue doses; held rows go to an on-hold list in "
                "medorders-6a (W6-D, not built yet)"),
    "services/downtime_service.py": (
        "dose", "the paper MAR; an 'ON HOLD: do not give' section in medorders-6a (W6-D, "
                "not built yet)"),
    "services/voice_agent_service.py": (
        "dose", "the voice agent's drug list; labelled and never read out as due in "
                "medorders-6a (W6-D, not built yet)"),
    # Shows or hands on a drug list: labelled.
    "services/ipass_service.py": ("labelled", "I-PASS action list and the printed sheet"),
    "services/discharge_service.py": ("labelled", "discharge medication reconciliation"),
    "routes/bridge_routes.py": ("labelled", "the patient portal: 'On hold', nothing more"),
    "services/fhir_export_service.py": ("labelled", "FHIR MedicationRequest 'on-hold'"),
    "services/sage_service.py": ("labelled", "the Sage patient context"),
    "services/admission_service.py": (
        "labelled", "the readmission's list of earlier-stay medicines it stopped, for "
                    "re-prescribing (opd-17c); a held one is marked was_on_hold"),
    "services/sage_ambient_service.py": ("labelled", "the ambient Relay context"),
    # Screening sets: kept.
    "services/drug_safety_service.py": (
        "kept", "the order-time interaction screen (status 'active'; a held row stays active)"),
    "services/patient_specific_screen.py": ("kept", "the patient-specific screen"),
    "services/scribe_service.py": ("kept", "the Scribe transcription vocabulary"),
    # Raw rows to the chart.
    "routes/vigil_routes.py": (
        "raw", "the chart's SELECT * carries held_at and hold_reason to the screen; the other "
               "reads are idempotency look-ups by key"),
    # Counts and summaries.
    "services/forecast_service.py": ("count", "the count of active drugs in the forecast features"),
    "services/impact_service.py": ("count", "the count of pharmacist-verified AI drugs"),
    "services/coding_service.py": ("count", "drug names as coding evidence"),
    "services/sage_proactive_service.py": (
        "count", "drugs started before a change in vitals; a held drug was still started then"),
    # The pharmacist's queue.
    "services/pharmacy_verification_service.py": (
        "verification", "the Verify Queue; a held order still needs its check"),
    # Administration history.
    "services/patient_timeline_service.py": ("history", "doses on the patient timeline"),
    # One row by id or key.
    "routes/emar_routes.py": ("lookup", "the ward row's patient, for the scope check"),
    # Tools the server never runs.
    "scripts/benchmark_wer.py": ("tool", "deletes its own benchmark rows"),
    "scripts/populate_helix_from_medgemma.py": ("tool", "wipes and repopulates a demo database"),
    "scripts/seed_relay_chats.py": ("tool", "seeds demo Relay chats"),
}


def _expression_strings(tree: ast.AST) -> set[int]:
    """ids of string constants that are statements on their own: docstrings
    and strings used as comments. Neither is a read."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}


def sql_reads(source: str) -> list[tuple[int, str]]:
    """(line, SQL text) for every string literal in `source` that reads FROM
    or JOINs medications."""
    tree = ast.parse(source)
    skip = _expression_strings(tree)
    return sorted(
        (node.lineno, node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in skip and READ_SQL.search(node.value))


def scan(root: Path) -> dict[str, list[tuple[int, str]]]:
    """Every module under `root` (tests, virtualenvs and hidden folders aside)
    that reads FROM medications, keyed by its path relative to `root`."""
    found: dict[str, list[tuple[int, str]]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_PARTS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            reads = sql_reads(path.read_text(errors="ignore"))
            if reads:
                found[path.relative_to(root).as_posix()] = reads
    return found


_SCANNED: dict[str, list[tuple[int, str]]] = {}


def _server_readers() -> dict[str, list[tuple[int, str]]]:
    """scan(SERVER_DIR), once per run."""
    if not _SCANNED:
        _SCANNED.update(scan(SERVER_DIR))
    return _SCANNED


def unclassified(found, registry=REGISTRY) -> list[str]:
    return sorted(m for m in found if m not in registry and not m.startswith(STORE_PREFIX))


def stale(found, registry=REGISTRY) -> list[str]:
    return sorted(m for m in registry if m not in found)


# ── The registry against the code ─────────────────────────────────────────────

def test_every_reader_of_medications_is_classified():
    found = _server_readers()
    missing = unclassified(found)
    assert not missing, (
        "These modules read FROM medications but are not in the reader registry. A held "
        "medication order keeps its row 'active' with held_at set: say in REGISTRY what a "
        "hold does to each (tests/test_medorder_reader_registry.py): "
        + ", ".join(f"{m} (line {found[m][0][0]})" for m in missing))


def test_no_registry_entry_is_stale():
    assert not stale(_server_readers()), "these entries no longer read medications"


def test_every_kind_is_one_of_the_kinds():
    assert {kind for kind, _ in REGISTRY.values()} <= set(KINDS)
    assert all(note.strip() for _, note in REGISTRY.values())


def test_every_labelled_reader_reads_the_hold_marker():
    found = _server_readers()
    for module, (kind, _) in REGISTRY.items():
        if kind != "labelled":
            continue
        for line, sql in found[module]:
            assert "held_at" in sql and "hold_reason" in sql, (module, line)


def test_the_store_package_reads_the_marker_itself():
    """The prefix rule covers services/medorders/: it is the module that sets
    and checks the marker (core.projection_for, emar_check)."""
    found = _server_readers()
    assert any(m.startswith(STORE_PREFIX) for m in found)
    core_src = (SERVER_DIR / "services" / "medorders" / "core.py").read_text()
    assert "held_at" in core_src


# ── The scanner itself ────────────────────────────────────────────────────────

def test_the_scanner_finds_a_new_reader_and_the_check_names_it(tmp_path):
    pkg = tmp_path / "services"
    pkg.mkdir()
    (pkg / "new_reader.py").write_text(
        '"""Mentions SELECT * FROM medications in a docstring only."""\n'
        "def drugs(db, pid):\n"
        "    return db.execute(\n"
        "        'SELECT drug_name FROM medications '\n"
        "        'WHERE patient_id = ?', (pid,)).fetchall()\n")
    (pkg / "fstring_reader.py").write_text(
        "def rows(db, marks):\n"
        "    return db.execute(f'SELECT m.id FROM orders o JOIN medications m ON 1 "
        "WHERE o.id IN ({marks})').fetchall()\n")
    (pkg / "docstring_only.py").write_text(
        '"""Reads nothing: SELECT * FROM medications is only an example here."""\n'
        "def f():\n"
        "    'SELECT * FROM medications, as a comment string'\n"
        "    return 1\n")
    (pkg / "other_table.py").write_text("SQL = 'SELECT * FROM medications_archive'\n")
    found = scan(tmp_path)
    assert set(found) == {"services/new_reader.py", "services/fstring_reader.py"}
    assert found["services/new_reader.py"][0][0] == 4
    assert unclassified(found, {}) == ["services/fstring_reader.py", "services/new_reader.py"]
    assert unclassified(found, {"services/new_reader.py": ("count", "x"),
                                "services/fstring_reader.py": ("kept", "y")}) == []
    assert stale(found, {"services/gone.py": ("count", "z")}) == ["services/gone.py"]


def test_the_store_prefix_never_hides_a_reader_elsewhere(tmp_path):
    (tmp_path / "services" / "medorders").mkdir(parents=True)
    (tmp_path / "services" / "medorders" / "lane.py").write_text(
        "SQL = 'SELECT id FROM medications WHERE id = ?'\n")
    (tmp_path / "services" / "medorders_like.py").write_text(
        "SQL = 'SELECT id FROM medications WHERE id = ?'\n")
    found = scan(tmp_path)
    assert unclassified(found, {}) == ["services/medorders_like.py"]
