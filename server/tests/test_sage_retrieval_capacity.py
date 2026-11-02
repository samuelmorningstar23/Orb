"""Retrieval work may shrink; clinical facts and patient boundaries may not."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import services.sage_service as sage
from migrations_his.m0090_sage_retrieval_indexes import apply


@pytest.fixture
def retrieval_db() -> Any:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE patients (
            id TEXT PRIMARY KEY, name TEXT, bed TEXT, ward TEXT,
            status TEXT, registered_facility_id INTEGER
        );
        CREATE TABLE admissions (
            patient_id TEXT, department TEXT, status TEXT,
            created_at TEXT, facility_id INTEGER
        );
        CREATE TABLE opd_visits (
            patient_id TEXT, registered_at TEXT, status TEXT, facility_id INTEGER
        );
        CREATE TABLE appointments (
            patient_id TEXT, created_at TEXT, status TEXT, facility_id INTEGER
        );
        CREATE TABLE vitals (
            id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT,
            voided_at TEXT, heart_rate REAL, systolic_bp REAL,
            diastolic_bp REAL, spo2 REAL, temperature REAL, respiratory_rate REAL
        );
        CREATE TABLE risk_scores (
            id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT
        );
        CREATE INDEX idx_vitals_patient ON vitals(patient_id, timestamp DESC);
        INSERT INTO patients VALUES ('a', 'Alice Example', 'AX-1', 'Alpha', 'admitted', 1);
        INSERT INTO patients VALUES ('b', 'Bob Example', 'BX-1', 'Beta', 'admitted', 1);
        INSERT INTO patients VALUES ('c', 'Carol Example', 'AX-2', 'Alpha', 'admitted', 2);
        INSERT INTO patients VALUES ('d', 'Dora Example', 'AX-3', 'Alpha', 'discharged', 1);
        INSERT INTO admissions VALUES ('a', 'Alpha', 'active', '2026-10-21', 1);
        INSERT INTO admissions VALUES ('b', 'Beta', 'active', '2026-10-21', 1);
        INSERT INTO admissions VALUES ('c', 'Alpha', 'active', '2026-10-21', 2);
        INSERT INTO admissions VALUES ('d', 'Alpha', 'discharged', '2026-10-21', 1);
    """)
    apply(db)
    yield db
    db.close()


def _observation(
    db: Any, patient: str, timestamp: str | None, hr: int, *, void: bool = False
) -> None:
    db.execute(
        "INSERT INTO vitals(patient_id, timestamp, heart_rate, voided_at) VALUES (?, ?, ?, ?)",
        (patient, timestamp, hr, "2026-10-21T12:00:00Z" if void else None),
    )


def _legacy_vitals(db: Any, dept: str | None, facilities: frozenset[int] | None) -> dict[str, dict]:
    """The former production GROUP BY + same-time correction rule, unchanged."""
    sql, params = sage._scope_sql(
        """
        SELECT v.id, v.patient_id, v.heart_rate, v.systolic_bp, v.diastolic_bp,
               v.spo2, v.temperature, v.respiratory_rate, v.timestamp
        FROM vitals v
        INNER JOIN (SELECT patient_id, MAX(timestamp) AS max_ts
                    FROM vitals WHERE voided_at IS NULL GROUP BY patient_id) latest
        ON v.patient_id = latest.patient_id AND v.timestamp = latest.max_ts
        INNER JOIN patients p ON p.id = v.patient_id
        WHERE p.status = 'admitted' AND v.voided_at IS NULL
        """,
        dept,
        facilities=facilities,
    )
    out: dict[str, dict] = {}
    for row in db.execute(sql, params).fetchall():
        prior = out.get(row["patient_id"])
        if prior is None or row["id"] > prior["id"]:
            out[row["patient_id"]] = dict(row)
    return out


@pytest.mark.parametrize("dept", [None, "Alpha", "Beta", "Missing"])
@pytest.mark.parametrize(
    "facilities", [None, frozenset(), frozenset({1}), frozenset({2}), frozenset({1, 2})]
)
def test_latest_probe_is_fact_identical_to_legacy_in_every_scope(
    retrieval_db: Any, dept: str | None, facilities: frozenset[int] | None
) -> None:
    db = retrieval_db
    for patient in ("a", "b", "c", "d"):
        # Stored-text timestamp ordering is existing behaviour, including
        # mixed legacy spellings. This optimisation must not reinterpret it.
        for timestamp, hr in (
            (None, 70),
            ("2026-10-20 23:00:00", 80),
            ("2026-10-21T00:00:00+05:30", 81),
            ("2026-10-21T00:00:00Z", 82),
            ("2026-10-21T00:00:00Z", 83),  # a same-time correction
        ):
            _observation(db, patient, timestamp, hr)
        _observation(db, patient, "2026-10-22T00:00:00Z", 255, void=True)
    actual = {
        row["patient_id"]: dict(row)
        for row in sage._latest_census_vitals(db, dept, facilities=facilities)
    }
    assert actual == _legacy_vitals(db, dept, facilities)
    assert all(row["heart_rate"] == 83 for row in actual.values())


def test_corrected_voided_and_reassigned_records_are_live_on_next_read(
    retrieval_db: Any,
) -> None:
    db = retrieval_db
    _observation(db, "a", "2026-10-21T00:00:00Z", 91)
    _observation(db, "a", "2026-10-21T01:00:00Z", 92)
    assert sage._latest_census_vitals(db, "Alpha")[0]["heart_rate"] == 92
    db.execute("UPDATE vitals SET voided_at = 'corrected' WHERE heart_rate = 92")
    assert sage._latest_census_vitals(db, "Alpha")[0]["heart_rate"] == 91
    db.execute("UPDATE vitals SET heart_rate = 140 WHERE heart_rate = 91")
    assert sage._latest_census_vitals(db, "Alpha")[0]["heart_rate"] == 140
    db.execute("UPDATE admissions SET department = 'Beta' WHERE patient_id = 'a'")
    assert sage._latest_census_vitals(db, "Alpha") == []
    assert sage._latest_census_vitals(db, "Beta")[0]["patient_id"] == "a"
    db.execute("UPDATE admissions SET facility_id = 2 WHERE patient_id = 'a'")
    assert sage._latest_census_vitals(db, "Beta", facilities=frozenset({1})) == []
    assert sage._latest_census_vitals(db, "Beta", facilities=frozenset({2}))[0]["patient_id"] == "a"


def test_all_null_or_all_void_observations_are_not_presented_as_current(
    retrieval_db: Any,
) -> None:
    _observation(retrieval_db, "a", None, 90)
    _observation(retrieval_db, "b", "2026-10-21", 140, void=True)
    assert sage._latest_census_vitals(retrieval_db, None) == []


def test_small_ward_does_not_scan_unrelated_hospital_history(retrieval_db: Any) -> None:
    db = retrieval_db
    _observation(db, "a", "2026-10-21", 90)
    db.executemany(
        "INSERT INTO vitals(patient_id, timestamp, heart_rate) VALUES ('b', ?, 80)",
        [(f"2026-10-20T{i:06d}",) for i in range(20000)],
    )
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0

    db.set_progress_handler(count, 100)
    try:
        legacy = _legacy_vitals(db, "Alpha", frozenset({1}))
        legacy_steps, steps = steps, 0
        actual = {
            row["patient_id"]: dict(row)
            for row in sage._latest_census_vitals(db, "Alpha", facilities=frozenset({1}))
        }
        new_steps = steps
    finally:
        db.set_progress_handler(None, 0)
    assert actual == legacy
    # VM instructions, not elapsed wall time: catches reintroduction of the
    # all-history aggregate without a flaky machine-speed assertion.
    assert legacy_steps > 100
    assert new_steps < legacy_steps // 10, (new_steps, legacy_steps)


def test_indexes_are_idempotent_and_do_not_modify_clinical_rows(
    retrieval_db: Any,
) -> None:
    db = retrieval_db
    _observation(db, "a", "2026-10-21", 97)
    before = [tuple(row) for row in db.execute("SELECT * FROM vitals")]
    apply(db)
    apply(db)
    assert [tuple(row) for row in db.execute("SELECT * FROM vitals")] == before
    indices = {row[1] for row in db.execute("PRAGMA index_list(vitals)")}
    assert "idx_vitals_current_latest" in indices
    partial_plan = " ".join(
        str(tuple(row))
        for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM vitals WHERE patient_id = ? "
            "AND voided_at IS NULL ORDER BY timestamp DESC, id DESC LIMIT 1",
            ("a",),
        )
    )
    assert "idx_vitals_current_latest" in partial_plan
    assert "TEMP B-TREE" not in partial_plan


def test_index_migration_tolerates_missing_or_older_optional_tables() -> None:
    db = sqlite3.connect(":memory:")
    try:
        apply(db)
        db.execute("CREATE TABLE vitals(id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT)")
        apply(db)
        assert list(db.execute("PRAGMA index_list(vitals)")) == []
    finally:
        db.close()


def test_vocabulary_reuse_is_immutable_and_changes_with_every_live_input(
    monkeypatch: Any, retrieval_db: Any
) -> None:
    sage._census_vocabulary.cache_clear()
    for name in (
        "_CENSUS_NAME_TOKENS",
        "_CENSUS_BED_PREFIXES",
        "_CENSUS_BED_KEYS",
        "_CENSUS_WARD_KEYS",
    ):
        monkeypatch.setattr(sage, name, frozenset())
    sage._refresh_census_name_tokens(retrieval_db)
    baseline = sage._CENSUS_NAME_TOKENS
    sage._refresh_census_name_tokens(retrieval_db)
    assert sage._census_vocabulary.cache_info().hits == 1
    retrieval_db.execute(
        "UPDATE patients SET name = 'Renamed Example', bed = 'NEW-44', ward = 'Replacement' WHERE id = 'a'"
    )
    sage._refresh_census_name_tokens(retrieval_db)
    assert "renamed" in sage._CENSUS_NAME_TOKENS
    assert "alice" not in sage._CENSUS_NAME_TOKENS
    assert "alice" in baseline  # earlier callers' immutable result is intact
    assert "new" in sage._CENSUS_BED_PREFIXES
    retrieval_db.execute("UPDATE patients SET status = 'discharged' WHERE id = 'a'")
    sage._refresh_census_name_tokens(retrieval_db)
    assert "renamed" not in sage._CENSUS_NAME_TOKENS
    assert "new" not in sage._CENSUS_BED_PREFIXES


def test_simultaneous_vocabulary_users_cannot_mutate_shared_results() -> None:
    rows = (
        ("Synthetic Alice", "GM-12", "General Medicine"),
        ("Synthetic Bob", "C-1", "Cardiology"),
    )
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _n: sage._census_vocabulary(rows), range(200)))
    assert all(result == results[0] for result in results)
    assert all(isinstance(value, frozenset) for result in results for value in result)
    assert sage._census_vocabulary.cache_info().currsize == 1


@pytest.mark.parametrize(
    "label",
    [
        None,
        "",
        0,
        21,
        "GM-0103",
        "gm 103",
        "Bed GM-0103",
        "ICU Bed 3",
        "General Medicine",
        "GW.12",
        "MW/12",
        "GM—103",
        "B1234567",
        "A" * 300 + " 12",
    ],
)
def test_cached_label_keys_preserve_the_complete_legacy_parser(label: object) -> None:
    def legacy_ward(value: object) -> tuple | None:
        if not value:
            return None
        tokens = sage._tokenize(str(value).lower().translate(sage._LOCATOR_FOLD))
        if not tokens or any(token.value is None for token in tokens):
            return None
        return tuple(token.value for token in tokens)

    def legacy_bed(value: object) -> tuple | None:
        if not value:
            return None
        text = sage._LABEL_BED_WORD_RE.sub(
            "", str(value).lower().translate(sage._LOCATOR_FOLD), count=1
        )
        chunks = legacy_ward(text)
        return chunks if chunks and any(isinstance(chunk, int) for chunk in chunks) else None

    assert sage._ward_chunks(label) == legacy_ward(label)
    assert sage._label_chunks(label) == legacy_bed(label)


def test_label_cache_bounds_memory_and_does_not_pin_long_values() -> None:
    sage._ward_chunks_cached.cache_clear()
    sage._label_chunks_cached.cache_clear()
    for number in range(2200):
        sage._label_chunks(f"NEW-{number}")
    assert sage._ward_chunks_cached.cache_info().currsize <= 2048
    assert sage._label_chunks_cached.cache_info().currsize == 2048
    before = sage._label_chunks_cached.cache_info()
    assert sage._label_chunks("W" * 300 + "12") is not None
    assert sage._label_chunks_cached.cache_info() == before


@pytest.mark.parametrize(
    "name",
    [
        "",
        "s",
        "om",
        "mr rama",
        "mrs anand",
        "dr jean-paul o'neill",
        "Prof SUSAN SMITH",
        "søren ångström",
        "Name123 Example",
        "x" * 300,
    ],
)
def test_name_preprocessing_keeps_legacy_honorific_case_punctuation_and_length_rules(
    name: str,
) -> None:
    parts = tuple(
        part
        for part in re.split(r"[^a-zA-Z]", name)
        if part and part not in ("mr", "mrs", "ms", "dr", "prof")
    )
    signature = sage._name_signature(name)
    assert signature.parts == parts
    assert signature.part_letters == sum(len(part) for part in parts)
    assert signature.clean_name == " ".join(parts)
    assert sage._name_letters(name) == len(re.sub(r"[^a-z]", "", name.lower()))
    assert dict(sage._name_character_counts(name)) == dict(Counter(name))


def test_name_preprocessing_caches_are_bounded_and_values_are_immutable() -> None:
    sage._name_signature_cached.cache_clear()
    sage._name_character_counts_cached.cache_clear()
    for number in range(2200):
        sage._name_signature(f"synthetic example {number}")
        sage._name_character_counts(f"synthetic example {number}")
    assert sage._name_signature_cached.cache_info().currsize == 2048
    assert sage._name_character_counts_cached.cache_info().currsize == 2048
    before_signature = sage._name_signature_cached.cache_info()
    before_counts = sage._name_character_counts_cached.cache_info()
    long_name = "x" * 300
    sage._name_signature(long_name)
    sage._name_character_counts(long_name)
    assert sage._name_signature_cached.cache_info() == before_signature
    assert sage._name_character_counts_cached.cache_info() == before_counts
    counts = sage._name_character_counts("synthetic")
    with pytest.raises(TypeError):
        counts["s"] = 100  # type: ignore[index]
    assert counts["s"] == 1
