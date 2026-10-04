"""One writer for drug stock (grn-4, docs/his_design/grn.json section D).

services/pharmacy_stock_service.py is the only server code that may INSERT
INTO, UPDATE or DELETE FROM helix_inventory_lots or helix_stock_movements.
A second writer is how a lot's quantity drifts from its ledger, and a second
ledger for one drug is the failure the procurement header warns about.

This walks every Python file under server/ (tests and the virtualenv apart),
reads every string literal and f-string in it with the ast module, and fails on
a write statement naming either table outside the allow-list:

  services/pharmacy_stock_service.py       the writer itself
  migrations.py                            migration 35's legacy reason rewrite
  migrations_his/m0054_pharmacy_store_schema.py   the ledger backfill
  services/helix_db.py                     DDL only (CREATE is not a write)
  scripts/seed_helix.py                    DELETE only (the reseed wipe)
  scripts/populate_helix_from_medgemma.py  DELETE only (its own wipe)

A table named through a variable (an f-string over a list of tables, as the
reseed sweep does) is not seen, and that is deliberate: those are the generic
wipes, not stock writes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent

_WRITE = re.compile(
    r"\b(?P<verb>INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
    r"\s+[\"'`\[]?(?P<table>helix_inventory_lots|helix_stock_movements)\b",
    re.IGNORECASE,
)

# path relative to server/ -> the verbs it may use ('*' for all)
ALLOWED: dict[str, set[str]] = {
    "services/pharmacy_stock_service.py": {"*"},
    "migrations.py": {"*"},
    "migrations_his/m0054_pharmacy_store_schema.py": {"*"},
    "services/helix_db.py": set(),
    "scripts/seed_helix.py": {"DELETE"},
    "scripts/populate_helix_from_medgemma.py": {"DELETE"},
}

_SKIP_DIRS = {".venv", "venv", "__pycache__", "tests", "node_modules", ".pytest_cache"}


def _strings(tree: ast.AST):
    """Every string literal, and every f-string with its holes as '{}'. The
    literal pieces inside an f-string are read once, as part of it."""
    inside_fstrings = {id(value) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                       for value in node.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in inside_fstrings:
                continue
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append("{}")
            yield node.lineno, "".join(parts)


def _verb(raw: str) -> str:
    word = raw.split()[0].upper()
    return "INSERT" if word in ("INSERT", "REPLACE") else word


def stock_writes(source: str) -> list[tuple[int, str, str]]:
    """(line, verb, table) for each write to a drug stock table in `source`."""
    found = []
    for lineno, text in _strings(ast.parse(source)):
        for match in _WRITE.finditer(text):
            found.append((lineno, _verb(match.group("verb")), match.group("table").lower()))
    return sorted(found)


def violations(root: Path = SERVER) -> list[str]:
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        key = rel.as_posix()
        allowed = ALLOWED.get(key)
        for lineno, verb, table in stock_writes(path.read_text(encoding="utf-8")):
            if allowed is not None and ("*" in allowed or verb in allowed):
                continue
            out.append(f"{key}:{lineno} {verb} {table}")
    return out


def test_only_the_stock_service_writes_drug_stock():
    found = violations()
    assert not found, (
        "drug stock is written outside services/pharmacy_stock_service.py. Call "
        "post_movement / open_or_merge_lot / set_lot_status instead:\n  " + "\n  ".join(found))


def test_the_guard_sees_every_write_shape():
    """The guard itself: each write shape is caught, reads and DDL are not."""
    source = '''
lot = "x"
a = "UPDATE helix_inventory_lots SET quantity = ? WHERE id = ?"
b = """INSERT INTO helix_stock_movements (id) VALUES (?)"""
c = "insert or ignore into helix_inventory_lots (id) values (?)"
d = f"DELETE FROM helix_stock_movements WHERE lot_id = {lot}"
e = ("UPDATE "
     "helix_stock_movements SET seq = 1")
f = 'REPLACE INTO "helix_inventory_lots" (id) VALUES (1)'
g = "SELECT * FROM helix_inventory_lots"
h = "CREATE TABLE IF NOT EXISTS helix_stock_movements (id TEXT)"
i = "UPDATE helix_inventory_lots_archive SET x = 1"
'''
    assert stock_writes(source) == [
        (3, "UPDATE", "helix_inventory_lots"),
        (4, "INSERT", "helix_stock_movements"),
        (5, "INSERT", "helix_inventory_lots"),
        (6, "DELETE", "helix_stock_movements"),
        (7, "UPDATE", "helix_stock_movements"),
        (9, "INSERT", "helix_inventory_lots"),
    ]


def test_a_new_writer_anywhere_is_reported(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "rogue.py").write_text(
        'SQL = "UPDATE helix_inventory_lots SET quantity = 0"\n', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "seed_helix.py").write_text(
        'A = "DELETE FROM helix_inventory_lots"\n'
        'B = "INSERT INTO helix_inventory_lots (id) VALUES (1)"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        'A = "UPDATE helix_inventory_lots SET quantity = 9"\n', encoding="utf-8")
    assert violations(tmp_path) == [
        "scripts/seed_helix.py:2 INSERT helix_inventory_lots",
        "services/rogue.py:1 UPDATE helix_inventory_lots",
    ]


def test_helix_service_no_longer_writes_stock_itself():
    """The dispense used to UPDATE the lot and INSERT the movement inline."""
    source = (SERVER / "services" / "helix_service.py").read_text(encoding="utf-8")
    assert stock_writes(source) == []
    assert "pharmacy_stock_service" in source
