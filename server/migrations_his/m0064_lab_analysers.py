"""Migration 64: the laboratory's analyser registry (lis-23).

Two tables, both empty until a site switches ORB_LAB on and registers an
analyser. Nothing here receives a result: the intake that reads them is
lis-24 (HTTP) and lis-25 (MLLP), built later and each behind its own switch.

1. lab_instruments: one row per analyser, or per piece of middleware that
   speaks for several, that may send results to Orb.
     id                     the instrument id the analyser sends in the
                            X-Instrument-Id header: capitals, digits, '-' and
                            '_', chosen by the site (services/
                            lab_instrument_service.normalise_instrument_id)
     name, make, model, serial, section_id
                            what it is and which laboratory section runs it
     protocol               'hl7v2' or 'astm' (ASTM is accepted by the schema
                            for lis-26 and refused by the service until then)
     transport              'http', 'mllp_listen', 'tcp_client', 'tcp_listen'
                            (the two tcp_ ones are ASTM's, also refused until
                            lis-27)
     host, port             where the analyser sits on the hospital network
     assumed_timezone       the IANA zone its clock reads when a timestamp
                            carries no offset; NULL means the hospital's own
     biomedical_asset_id    its row in the biomedical register
                            (biomedical_assets.id), so the screen can show
                            its calibration status
     credential_hash        'sha256:' and the hex digest of the analyser's
                            key. The key itself is shown ONCE, when it is
                            issued, and never stored: a key that can be read
                            back is a key anyone with the database can use.
     credential_rotated_at, credential_rotated_by
                            when and by whom the current key was issued
     enabled                0 or 1. A new analyser starts switched off, and
                            the database refuses an enabled row with no key.
     status_reason          why it was last switched on or off
     last_message_at        written by the intake (lis-24), never by a person

2. lab_instrument_test_map: which Orb test each analyser code is, keyed on
   (instrument_id, instrument_code) as the design names it.
     factor_milli           the multiplier from the analyser's number to the
                            test's unit, in thousandths (1000 = unchanged)
     instrument_unit        the unit the analyser sends (OBX-6), so the intake
                            can refuse a result whose unit differs
     note                   the pathologist's reason, for example the manual
                            and revision the code was checked against
     approved_by/at         the pathologist who made the current mapping
     active, retired_*      a mapping is retired with a reason, never deleted,
                            so a result entered from it can always be traced
   Two codes on one analyser can never both feed the same test (a unique
   index over the active rows), and two codes that differ only in capitals
   can never both exist on one analyser (a second unique index, NOCASE): the
   intake looks a code up exactly as it arrives, so either would be a result
   that lands on two tests or on the wrong one.

Who changes them, and the audit: the registry and its keys need lab.interface
(the administrator); the code map needs lab.master (the pathologist). Every
change is audited as clinical_config inside its own transaction, which is
fail-closed: an audit row that cannot be written rolls the change back.

The rules every migration here follows: one statement per conn.execute
(never executescript, which commits the runner's transaction), IF NOT EXISTS
so a second run changes nothing, no FOREIGN KEY, no REAL column, no
CURRENT_TIMESTAMP default (the writer stamps every instant in the canonical
UTC spelling from services/clinical_time), and an index is created only when
its table has every column it names, so an unusual table of the same name
cannot stop boot.
"""
from migrations import Migration

INSTRUMENTS_DDL = """CREATE TABLE IF NOT EXISTS lab_instruments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    make TEXT,
    model TEXT,
    serial TEXT,
    section_id TEXT,
    site_id TEXT,
    protocol TEXT NOT NULL DEFAULT 'hl7v2' CHECK (protocol IN ('hl7v2', 'astm')),
    transport TEXT NOT NULL DEFAULT 'http' CHECK (transport IN (
        'http', 'mllp_listen', 'tcp_client', 'tcp_listen')),
    host TEXT,
    port INTEGER CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
    assumed_timezone TEXT,
    biomedical_asset_id INTEGER,
    credential_hash TEXT,
    credential_rotated_at TEXT,
    credential_rotated_by TEXT,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    status_reason TEXT,
    last_message_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    CHECK (enabled = 0 OR credential_hash IS NOT NULL),
    CHECK (credential_hash IS NULL OR (credential_rotated_at IS NOT NULL
                                       AND credential_rotated_by IS NOT NULL))
)"""

TEST_MAP_DDL = """CREATE TABLE IF NOT EXISTS lab_instrument_test_map (
    instrument_id TEXT NOT NULL,
    instrument_code TEXT NOT NULL CHECK (length(instrument_code) BETWEEN 1 AND 64
                                         AND instrument_code = trim(instrument_code)),
    test_id TEXT NOT NULL,
    factor_milli INTEGER NOT NULL DEFAULT 1000 CHECK (factor_milli > 0),
    instrument_unit TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    note TEXT NOT NULL CHECK (trim(note) != ''),
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    retired_by TEXT,
    retired_at TEXT,
    retire_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (instrument_id, instrument_code),
    CHECK (active = 1 OR (retired_by IS NOT NULL AND retired_at IS NOT NULL
                          AND trim(COALESCE(retire_reason, '')) != '')),
    CHECK (active = 0 OR (retired_by IS NULL AND retired_at IS NULL
                          AND retire_reason IS NULL))
)"""

TABLE_NAMES = ("lab_instruments", "lab_instrument_test_map")

# (index name, table, column expressions, the bare columns they need,
#  WHERE clause or "", unique)
_INDEXES = (
    ("idx_lab_instruments_section", "lab_instruments", ("section_id",),
     ("section_id",), "", False),
    ("idx_lab_instrument_test_map_test", "lab_instrument_test_map", ("test_id",),
     ("test_id",), "", False),
    # One live code per test on one analyser.
    ("ux_lab_instrument_test_map_live_test", "lab_instrument_test_map",
     ("instrument_id", "test_id"), ("instrument_id", "test_id", "active"), "active = 1", True),
    # 'HB' and 'Hb' are never two mappings on one analyser.
    ("ux_lab_instrument_test_map_code_nocase", "lab_instrument_test_map",
     ("instrument_id", "instrument_code COLLATE NOCASE"),
     ("instrument_id", "instrument_code"), "", True),
)


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply(conn) -> None:
    conn.execute(INSTRUMENTS_DDL)
    conn.execute(TEST_MAP_DDL)
    for name, table, expressions, needs, where, unique in _INDEXES:
        if not set(needs) <= _columns(conn, table):
            continue
        kind = "UNIQUE INDEX" if unique else "INDEX"
        clause = f" WHERE {where}" if where else ""
        conn.execute(
            f"CREATE {kind} IF NOT EXISTS {name} ON {table}({', '.join(expressions)}){clause}"
        )


MIGRATION = Migration(64, "lab_analysers", apply)
