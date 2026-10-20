"""The laboratory's test master: catalogue, panels, reference ranges and
critical limits (lis-3).

Behind ORB_LAB (default off). Nothing here runs, and nothing is written, on a
site that has not switched the laboratory on: the starter catalogue is loaded
from services/lab_module.startup only while the switch is on, and every route
that reaches this module answers 404 while it is off.

WHAT SHIPS AND WHAT DOES NOT
  * site_config/lab_catalogue.starter.json ships tests, panels, specimen
    types, sections and rejection reasons. It is loaded ONCE, into an empty
    catalogue; after that the site's pathologist owns the catalogue and a
    later boot never puts back a test, a panel component or a reason the
    pathologist changed.
  * No reference range and no critical limit ships (rulings LIS-R1 and
    LIS-R2, held for Samuel to confirm before go-live). With no approved range
    a result carries no H or L flag and the report says "Reference range not
    configured"; with no approved limit no critical is raised. Not configured
    is never read as zero.
  * Capillary ketones, venous and arterial blood gas ship as bedside tests
    (performed_at 'ward_poct', ruling LIS-R18): they never enter the
    laboratory's work lists, because the DKA and asthma order sets repeat them
    hourly at the bedside. The pathologist can reclassify one per site.
  * The orderable codes are registered in the terminology table so CPOE
    validates them, again only while ORB_LAB is on. With it off the
    terminology table is exactly what it was.

RANGES AND LIMITS ARE NEVER EDITED IN VALUE. Approving a new one closes the
current row for the same sex and age band (effective_to = now) and inserts the
new row, so every released result can name the exact range it was flagged
against. A band that overlaps a different current band for a compatible sex
is refused, so at most one range ever applies to a patient. Values are
integers in thousandths and must be exact at the test's reporting precision.

WHICH RANGE APPLIES (ruling LIS-R7, strictest option, held for Samuel)
  * Age is in days at collection, from patients.date_of_birth and the
    hospital-local collection date. patients.age is never read: it is typed
    once at admission and never moves.
  * Sex is normalised: m or male is male, f or female is female (any case,
    spaces ignored); anything else, including blank, matches only a range for
    any sex.
  * With no date of birth only a range covering every age applies; otherwise
    the result has no range and no flag, and says why.
  * A critical limit follows the same rule, so a limit that does not depend
    on age or sex still applies when the date of birth is missing.

THE COMPARISON WORDS a later increment (lis-11) must use: a value is within
the range when low <= value <= high, L below low, H above high; a value is
critical when it is BELOW critical_low or ABOVE critical_high, compared at the
test's reporting precision (half-up, as the NEWS2 charted-value ruling does).
The display text says "below" and "above" for exactly that reason.
lab_result_service.range_flag (lis-10) implements the range half of this for
result entry.

EVERY WRITE is one BEGIN IMMEDIATE transaction with its audit row inside it,
category clinical_config (fail-closed): an audit that cannot be written rolls
the change back, so the master never changes unaudited. Writes also check the
actor holds lab.master, beside the route's own permission check.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, overload

from database import db_session
from permissions import LAB_MASTER, has_permission

from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.lab.master")

STARTER_CATALOGUE_PATH = (
    Path(__file__).resolve().parents[1] / "site_config" / "lab_catalogue.starter.json"
)

CODE_SYSTEMS = ("LOINC", "LOCAL")
KINDS = ("analyte", "panel")
PERFORMED_AT = ("lab", "ward_poct")
RESULT_TYPES = ("numeric", "text", "coded")
SEXES = ("any", "male", "female")
DELTA_KINDS = ("absolute", "percent")
SHORT_NAME_MAX = 16
MAX_DECIMAL_PLACES = 3
# Thousandths. Far beyond any laboratory value, small enough to stay an exact
# 64-bit integer.
_MILLI_LIMIT = 10**15

_CATALOGUE_TABLES = (
    "lab_tests",
    "lab_panel_components",
    "lab_specimen_types",
    "lab_sections",
    "lab_rejection_reasons",
)

NOT_CONFIGURED_TEXT = "Reference range not configured"
_RANGE_REASON_TEXT = {
    "not_configured": NOT_CONFIGURED_TEXT,
    "date_of_birth_not_recorded": f"{NOT_CONFIGURED_TEXT}: date of birth not recorded",
    "sex_not_recorded": f"{NOT_CONFIGURED_TEXT}: sex not recorded",
    "no_range_for_patient": f"{NOT_CONFIGURED_TEXT} for this patient's age and sex",
    "ambiguous": f"{NOT_CONFIGURED_TEXT}: more than one range applies",
}
_LIMIT_REASON_TEXT = {
    "not_configured": "Critical limit not configured",
    "date_of_birth_not_recorded": "Critical limit not configured: date of birth not recorded",
    "sex_not_recorded": "Critical limit not configured: sex not recorded",
    "no_range_for_patient": "Critical limit not configured for this patient's age and sex",
    "ambiguous": "Critical limit not configured: more than one limit applies",
}


class LabMasterError(ValueError):
    """A refusal a route turns into an HTTP answer: `code` is the machine
    reason, `message` the sentence for the screen, `status` the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ════════════════════════════════════════════════════════════════════════════
#  Small pure helpers
# ════════════════════════════════════════════════════════════════════════════


def test_id_for(code_system: str, code: str) -> str:
    """The catalogue id of a test: '<CODE_SYSTEM>:<code>'."""
    return f"{code_system.strip().upper()}:{code.strip()}"


def normalise_sex(value: Any) -> str | None:
    """'male', 'female' or None. m/male and f/female in any case, surrounding
    spaces ignored; anything else (blank, 'other', 'unknown') is None, which
    matches only a range for any sex."""
    text = str(value or "").strip().lower()
    if text in ("m", "male"):
        return "male"
    if text in ("f", "female"):
        return "female"
    return None


def _parse_date_of_birth(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def age_days_at(date_of_birth: Any, at: Any) -> int | None:
    """Age in whole days on the hospital-local calendar day of `at` (the
    collection instant), from a date of birth. None when the date of birth is
    missing, unreadable or after `at`: an age Orb cannot state is not recorded.
    """
    dob = _parse_date_of_birth(date_of_birth)
    if dob is None or at is None:
        return None
    from services.hospital_time import to_hospital

    local = to_hospital(at)
    if local is None:
        return None
    days = (local.date() - dob).days
    return days if days >= 0 else None


def to_milli(value: Any, decimal_places: int, *, field: str) -> int | None:
    """A value typed by a person as thousandths, refused unless it is exact at
    the test's reporting precision. None and blank stay None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise LabMasterError("invalid_value", f"{field} must be a number.")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise LabMasterError("invalid_value", f"{field} must be a number.") from None
    if not number.is_finite():
        raise LabMasterError("invalid_value", f"{field} must be a number.")
    scaled = number * (Decimal(10) ** int(decimal_places))
    if scaled != scaled.to_integral_value():
        raise LabMasterError(
            "precision",
            f"{field} has more decimal places than this test reports ({decimal_places}).",
        )
    milli = number * 1000
    if abs(milli) >= _MILLI_LIMIT:
        raise LabMasterError("invalid_value", f"{field} is out of range.")
    return int(milli)


def format_milli(milli: int | None, decimal_places: int) -> str:
    """Thousandths at the test's reporting precision, trailing zeros kept."""
    if milli is None:
        return ""
    value = Decimal(int(milli)) / Decimal(1000)
    return f"{value:.{int(decimal_places)}f}"


def _with_unit(text: str, unit: str | None) -> str:
    unit = (unit or "").strip()
    return f"{text} {unit}" if unit else text


def range_display(low: int | None, high: int | None, decimal_places: int, unit: str | None) -> str:
    """'3.5 to 5.1 mmol/L', '200 mg/dL or below', '40 U/L or above'."""
    lo, hi = format_milli(low, decimal_places), format_milli(high, decimal_places)
    if low is not None and high is not None:
        return _with_unit(f"{lo} to {hi}", unit)
    if high is not None:
        return f"{_with_unit(hi, unit)} or below"
    return f"{_with_unit(lo, unit)} or above"


def limit_display(low: int | None, high: int | None, decimal_places: int, unit: str | None) -> str:
    """'Critical below 2.5 or above 6.5 mmol/L', 'Critical above 6.5 mmol/L'."""
    lo, hi = format_milli(low, decimal_places), format_milli(high, decimal_places)
    if low is not None and high is not None:
        return f"Critical below {lo} or above {_with_unit(hi, unit)}"
    if high is not None:
        return f"Critical above {_with_unit(hi, unit)}"
    return f"Critical below {_with_unit(lo, unit)}"


def _sex_compatible(a: str, b: str) -> bool:
    return a == b or "any" in (a, b)


def _bands_overlap(a_min: int, a_max: int | None, b_min: int, b_max: int | None) -> bool:
    a_end = float("inf") if a_max is None else a_max
    b_end = float("inf") if b_max is None else b_max
    return a_min < b_end and b_min < a_end


@dataclass(frozen=True)
class RangeChoice:
    """The answer to "which range (or limit) applies to this patient now"."""

    row: dict | None
    reason: str | None  # None when a row applies
    display_text: str


def select_applicable(
    rows: Iterable[dict], *, sex: str | None, age_days: int | None, limits: bool = False
) -> RangeChoice:
    """Pick the one current row that applies, or say why none does.

    `rows` are the CURRENT rows for one test. `sex` is already normalised.
    With `age_days` None (no date of birth) only a row covering every age
    (age_min_days 0, no upper age) can apply. With `sex` None only a row for
    any sex can apply. More than one match is refused as ambiguous rather than
    guessed.
    """
    texts = _LIMIT_REASON_TEXT if limits else _RANGE_REASON_TEXT
    rows = [dict(r) for r in rows]
    if not rows:
        return RangeChoice(None, "not_configured", texts["not_configured"])

    def sex_ok(r):
        return r["sex"] == "any" or (sex is not None and r["sex"] == sex)

    def covers_every_age(r):
        return int(r["age_min_days"] or 0) == 0 and r["age_max_days"] is None

    def age_ok(r):
        if age_days is None:
            return covers_every_age(r)
        lo = int(r["age_min_days"] or 0)
        hi = r["age_max_days"]
        return lo <= age_days and (hi is None or age_days < int(hi))

    matches = [r for r in rows if sex_ok(r) and age_ok(r)]
    if len(matches) == 1:
        return RangeChoice(matches[0], None, matches[0]["display_text"])
    if len(matches) > 1:
        return RangeChoice(None, "ambiguous", texts["ambiguous"])
    if age_days is None and any(sex_ok(r) for r in rows):
        return RangeChoice(None, "date_of_birth_not_recorded", texts["date_of_birth_not_recorded"])
    if sex is None and any(r["sex"] != "any" and (age_days is None or age_ok(r)) for r in rows):
        return RangeChoice(None, "sex_not_recorded", texts["sex_not_recorded"])
    return RangeChoice(None, "no_range_for_patient", texts["no_range_for_patient"])


# ════════════════════════════════════════════════════════════════════════════
#  Introspection
# ════════════════════════════════════════════════════════════════════════════


def tables_present(conn, names: Iterable[str] = _CATALOGUE_TABLES) -> bool:
    names = tuple(names)
    placeholders = ", ".join("?" for _ in names)
    found = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        names,
    ).fetchone()[0]
    return int(found) == len(names)


# ════════════════════════════════════════════════════════════════════════════
#  The starter catalogue
# ════════════════════════════════════════════════════════════════════════════


def read_starter_catalogue(path: Path | None = None) -> dict:
    with open(path or STARTER_CATALOGUE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_catalogue(data: dict) -> list[str]:
    """Every problem with a catalogue file, as sentences. Empty means loadable.

    Refuses any reference range or critical limit in the file: none ships
    (LIS-R1, LIS-R2), and a file that carried one would be a default nobody
    approved.
    """
    problems: list[str] = []
    for forbidden in ("reference_ranges", "critical_limits", "ranges", "limits"):
        if forbidden in data:
            problems.append(
                f"the catalogue must not ship {forbidden}: a pathologist approves each one"
            )
    sections = {s.get("id") for s in data.get("sections", [])}
    specimens = {s.get("code") for s in data.get("specimen_types", [])}
    if None in sections or "" in sections:
        problems.append("a section has no id")
    if None in specimens or "" in specimens:
        problems.append("a specimen type has no code")
    for reason in data.get("rejection_reasons", []):
        if not reason.get("code") or not reason.get("display"):
            problems.append(f"rejection reason {reason!r} needs a code and a display")
    seen: dict[tuple, dict] = {}
    for t in data.get("tests", []):
        key = (str(t.get("code_system", "")).upper(), str(t.get("code", "")).strip())
        label = f"{key[0]} {key[1]}"
        if key[0] not in CODE_SYSTEMS or not key[1]:
            problems.append(f"{label}: code system must be LOINC or LOCAL and the code set")
        if key in seen:
            problems.append(f"{label}: listed twice")
        seen[key] = t
        for field in ("name", "short_name"):
            if not str(t.get(field) or "").strip():
                problems.append(f"{label}: {field} is missing")
        if len(str(t.get("short_name") or "")) > SHORT_NAME_MAX:
            problems.append(f"{label}: short name is longer than {SHORT_NAME_MAX} characters")
        if t.get("kind", "analyte") not in KINDS:
            problems.append(f"{label}: kind must be analyte or panel")
        if t.get("performed_at", "lab") not in PERFORMED_AT:
            problems.append(f"{label}: performed_at must be lab or ward_poct")
        if t.get("result_type", "numeric") not in RESULT_TYPES:
            problems.append(f"{label}: result_type must be numeric, text or coded")
        dp = t.get("decimal_places", 0)
        if not isinstance(dp, int) or isinstance(dp, bool) or not 0 <= dp <= MAX_DECIMAL_PLACES:
            problems.append(f"{label}: decimal_places must be 0 to {MAX_DECIMAL_PLACES}")
        if t.get("section_id") and t["section_id"] not in sections:
            problems.append(f"{label}: unknown section {t['section_id']}")
        if t.get("performed_at", "lab") == "lab" and not t.get("section_id"):
            problems.append(f"{label}: a laboratory test needs a section")
        if not t.get("specimen_type_code") or t["specimen_type_code"] not in specimens:
            problems.append(f"{label}: unknown specimen type {t.get('specimen_type_code')}")
        for forbidden in ("reference_range", "critical_limit", "low", "high"):
            if forbidden in t:
                problems.append(f"{label}: must not ship a {forbidden}")
    for panel in data.get("panels", []):
        system = str(panel.get("code_system", "")).upper()
        pkey = (system, str(panel.get("code", "")).strip())
        p = seen.get(pkey)
        if p is None or p.get("kind") != "panel":
            problems.append(f"panel {pkey[0]} {pkey[1]} is not a test of kind panel")
            continue
        comps = panel.get("components") or []
        if not comps:
            problems.append(f"panel {pkey[1]} has no components")
        if len(set(comps)) != len(comps):
            problems.append(f"panel {pkey[1]} lists a component twice")
        for code in comps:
            c = seen.get((system, str(code).strip()))
            if c is None:
                problems.append(f"panel {pkey[1]}: component {code} is not in the catalogue")
            elif c.get("kind", "analyte") != "analyte":
                problems.append(f"panel {pkey[1]}: component {code} is not an analyte")
    for (system, code), t in seen.items():
        if t.get("kind") == "panel" and not any(
            (str(p.get("code_system", "")).upper(), str(p.get("code", "")).strip())
            == (system, code)
            for p in data.get("panels", [])
        ):
            problems.append(f"panel {code} lists no components")
    return problems


def load_starter_catalogue(*, path: Path | None = None) -> dict:
    """Load the starter catalogue into an EMPTY catalogue and register every
    active orderable code in terminology. Called only while ORB_LAB is on
    (services/lab_module.startup). Idempotent: once lab_tests holds any row
    the site owns the catalogue and nothing is inserted again; terminology
    registration adds only codes that are missing.

    One transaction, audited as clinical_config when it changes anything.
    """
    data = read_starter_catalogue(path)
    problems = validate_catalogue(data)
    if problems:
        raise LabMasterError("catalogue_invalid", "; ".join(problems))
    now = utc_iso_now()
    with db_session(immediate=True) as conn:
        if not tables_present(conn):
            return {"loaded": False, "reason": "tables_absent", "terminology_added": 0}
        existing = conn.execute("SELECT COUNT(*) FROM lab_tests").fetchone()[0]
        inserted = {
            "sections": 0,
            "specimen_types": 0,
            "rejection_reasons": 0,
            "tests": 0,
            "panel_components": 0,
        }
        if not existing:
            for s in data.get("sections", []):
                conn.execute(
                    "INSERT OR IGNORE INTO lab_sections (id, name, site_id, active, sort_order) "
                    "VALUES (?, ?, NULL, 1, ?)",
                    (s["id"], s["name"], int(s.get("sort_order") or 0)),
                )
                inserted["sections"] += 1
            for s in data.get("specimen_types", []):
                conn.execute(
                    """INSERT OR IGNORE INTO lab_specimen_types
                       (code, display, container, container_colour_name, min_volume_ul,
                        stability_minutes, handling_note, active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        s["code"],
                        s["display"],
                        s.get("container"),
                        s.get("container_colour_name"),
                        s.get("min_volume_ul"),
                        s.get("stability_minutes"),
                        s.get("handling_note"),
                    ),
                )
                inserted["specimen_types"] += 1
            for r in data.get("rejection_reasons", []):
                conn.execute(
                    "INSERT OR IGNORE INTO lab_rejection_reasons (code, display, incident_prompt, active) "
                    "VALUES (?, ?, ?, 1)",
                    (r["code"], r["display"], 1 if r.get("incident_prompt") else 0),
                )
                inserted["rejection_reasons"] += 1
            for t in data.get("tests", []):
                system = t["code_system"].upper()
                conn.execute(
                    """INSERT OR IGNORE INTO lab_tests
                       (id, code, code_system, name, short_name, kind, performed_at, section_id,
                        specimen_type_code, result_type, unit, decimal_places, coded_values_json,
                        orderable, active, version, created_at, updated_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, 1, ?, 'system', ?)""",
                    (
                        test_id_for(system, t["code"]),
                        t["code"].strip(),
                        system,
                        t["name"],
                        t["short_name"],
                        t.get("kind", "analyte"),
                        t.get("performed_at", "lab"),
                        t.get("section_id"),
                        t["specimen_type_code"],
                        t.get("result_type", "numeric"),
                        t.get("unit"),
                        int(t.get("decimal_places", 0)),
                        1 if t.get("orderable", True) else 0,
                        now,
                        now,
                    ),
                )
                inserted["tests"] += 1
            for panel in data.get("panels", []):
                system = panel["code_system"].upper()
                pid = test_id_for(system, panel["code"])
                for order, code in enumerate(panel["components"], start=1):
                    conn.execute(
                        "INSERT OR IGNORE INTO lab_panel_components "
                        "(panel_test_id, component_test_id, sort_order) VALUES (?, ?, ?)",
                        (pid, test_id_for(system, code), order * 10),
                    )
                    inserted["panel_components"] += 1
        displays = {
            test_id_for(t["code_system"], t["code"]): t.get("terminology_display")
            for t in data.get("tests", [])
        }
        added = _register_catalogue_terminology(conn, displays)
        changed = (not existing) or added
        if changed:
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="clinical_config",
                action_type="lab_catalogue_loaded"
                if not existing
                else "lab_terminology_registered",
                outcome="success",
                db=conn,
                summary_message=(
                    "Starter laboratory catalogue loaded (no reference ranges or critical limits)"
                    if not existing
                    else "Laboratory codes registered for order validation"
                ),
                actor_user_id="system",
                actor_role="system",
                target_type="lab_catalogue",
                target_id="starter",
                metadata={
                    "inserted": inserted if not existing else {},
                    "terminology_added": added,
                    "catalogue_version": data.get("version"),
                },
            )
    return {
        "loaded": not existing,
        "inserted": inserted if not existing else {},
        "terminology_added": added,
    }


def _register_catalogue_terminology(conn, displays: dict | None = None) -> int:
    """Register every active orderable test code in terminology (missing codes
    only). On the caller's transaction."""
    from services.terminology_service import register_codes

    displays = displays or {}
    rows = conn.execute(
        "SELECT id, code_system, code, name FROM lab_tests WHERE active = 1 AND orderable = 1 "
        "ORDER BY id"
    ).fetchall()
    return register_codes(
        conn, [(r["code_system"], r["code"], displays.get(r["id"]) or r["name"]) for r in rows]
    )


# ════════════════════════════════════════════════════════════════════════════
#  Reads (take an open connection, never commit)
# ════════════════════════════════════════════════════════════════════════════

_TEST_COLUMNS = (
    "id, code, code_system, name, short_name, kind, performed_at, section_id, "
    "specimen_type_code, result_type, unit, decimal_places, coded_values_json, orderable, "
    "tat_routine_min, tat_urgent_min, tat_stat_min, delta_kind, delta_threshold_milli, "
    "delta_window_hours, is_send_out, active, version, created_at, updated_by, updated_at"
)


def _test_row(r) -> dict:
    d = dict(r)
    for flag in ("orderable", "is_send_out", "active"):
        d[flag] = bool(d.get(flag))
    return d


def _components(conn, panel_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT c.component_test_id AS test_id, t.code, t.code_system, t.name,
                  t.short_name, t.unit, t.decimal_places, t.active, c.sort_order
           FROM lab_panel_components c
           LEFT JOIN lab_tests t ON t.id = c.component_test_id
           WHERE c.panel_test_id = ?
           ORDER BY c.sort_order, c.component_test_id""",
            (panel_id,),
        ).fetchall()
    ]


def search_catalogue(conn, q: str = "", *, limit: int = 25) -> list[dict]:
    """Active orderable tests matching `q` (name, short name or code), with the
    specimen and container to collect and, for a panel, its components. For
    the order screen's typeahead. No patient data."""
    if not tables_present(conn):
        return []
    limit = max(1, min(int(limit or 25), 100))
    term = (q or "").strip()
    params: list[Any] = []
    where = "t.active = 1 AND t.orderable = 1"
    if term:
        like = f"%{term.lower()}%"
        where += " AND (LOWER(t.name) LIKE ? OR LOWER(t.short_name) LIKE ? OR LOWER(t.code) LIKE ?)"
        params += [like, like, like]
    rows = conn.execute(
        f"""SELECT t.id, t.code, t.code_system, t.name, t.short_name, t.kind,
                   t.performed_at, t.unit, t.result_type, t.section_id,
                   s.name AS section_name, t.specimen_type_code,
                   st.display AS specimen_display, st.container,
                   st.container_colour_name
            FROM lab_tests t
            LEFT JOIN lab_sections s ON s.id = t.section_id
            LEFT JOIN lab_specimen_types st ON st.code = t.specimen_type_code
            WHERE {where}
            ORDER BY (LOWER(t.code) = ?) DESC, (LOWER(t.short_name) = ?) DESC,
                     t.name COLLATE NOCASE, t.id
            LIMIT ?""",
        (*params, term.lower(), term.lower(), limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["kind"] == "panel":
            d["components"] = [
                {"test_id": c["test_id"], "name": c["name"], "short_name": c["short_name"]}
                for c in _components(conn, d["id"])
            ]
        out.append(d)
    return out


def list_tests(conn, *, include_inactive: bool = False, kind: str | None = None) -> list[dict]:
    if not tables_present(conn):
        return []
    clauses, params = [], []
    if not include_inactive:
        clauses.append("active = 1")
    if kind:
        if kind not in KINDS:
            raise LabMasterError("invalid_kind", "Kind must be analyte or panel.")
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [
        _test_row(r)
        for r in conn.execute(
            f"SELECT {_TEST_COLUMNS} FROM lab_tests {where} ORDER BY name COLLATE NOCASE, id",
            params,
        ).fetchall()
    ]


def get_test(conn, test_id: str) -> dict | None:
    if not tables_present(conn):
        return None
    r = conn.execute(f"SELECT {_TEST_COLUMNS} FROM lab_tests WHERE id = ?", (test_id,)).fetchone()
    if r is None:
        return None
    d = _test_row(r)
    if d["kind"] == "panel":
        d["components"] = _components(conn, test_id)
    d["reference_ranges"] = list_reference_ranges(conn, test_id=test_id)
    d["critical_limits"] = list_critical_limits(conn, test_id=test_id)
    return d


def list_panels(conn) -> list[dict]:
    panels = list_tests(conn, include_inactive=True, kind="panel")
    for p in panels:
        p["components"] = _components(conn, p["id"])
    return panels


def list_specimen_types(conn, *, include_inactive: bool = False) -> list[dict]:
    if not tables_present(conn):
        return []
    where = "" if include_inactive else "WHERE active = 1"
    return [
        dict(r) | {"active": bool(r["active"])}
        for r in conn.execute(
            f"SELECT * FROM lab_specimen_types {where} ORDER BY display COLLATE NOCASE"
        ).fetchall()
    ]


def list_sections(conn, *, include_inactive: bool = False) -> list[dict]:
    if not tables_present(conn):
        return []
    where = "" if include_inactive else "WHERE active = 1"
    return [
        dict(r) | {"active": bool(r["active"])}
        for r in conn.execute(
            f"SELECT * FROM lab_sections {where} ORDER BY sort_order, name COLLATE NOCASE"
        ).fetchall()
    ]


def list_rejection_reasons(conn, *, include_inactive: bool = False) -> list[dict]:
    if not tables_present(conn):
        return []
    where = "" if include_inactive else "WHERE active = 1"
    return [
        dict(r) | {"active": bool(r["active"]), "incident_prompt": bool(r["incident_prompt"])}
        for r in conn.execute(
            f"SELECT * FROM lab_rejection_reasons {where} ORDER BY display COLLATE NOCASE"
        ).fetchall()
    ]


def _current_sql(alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    return (
        f"julianday({a}effective_from) <= julianday(?) AND "
        f"({a}effective_to IS NULL OR julianday({a}effective_to) > julianday(?))"
    )


def _list_versioned(
    conn, table: str, *, test_id: str | None, include_retired: bool, at: str | None = None
) -> list[dict]:
    if not tables_present(conn, (table,)):
        return []
    clauses, params = [], []
    if test_id:
        clauses.append("test_id = ?")
        params.append(test_id)
    if not include_retired:
        at = at or utc_iso_now()
        clauses.append(_current_sql())
        params += [at, at]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM {table} {where} "
            "ORDER BY test_id, sex, age_min_days, julianday(effective_from) DESC, id DESC",
            params,
        ).fetchall()
    ]


def list_reference_ranges(
    conn, *, test_id: str | None = None, include_retired: bool = False
) -> list[dict]:
    return _list_versioned(
        conn, "lab_reference_ranges", test_id=test_id, include_retired=include_retired
    )


def list_critical_limits(
    conn, *, test_id: str | None = None, include_retired: bool = False
) -> list[dict]:
    return _list_versioned(
        conn, "lab_critical_limits", test_id=test_id, include_retired=include_retired
    )


def configuration_gaps(conn) -> dict:
    """The loud counts for the setup screen: active numeric laboratory
    analytes with no current approved reference range, and with no current
    approved critical limit. Not configured is never zero."""
    if not tables_present(conn, ("lab_tests", "lab_reference_ranges", "lab_critical_limits")):
        return {"numeric_tests": 0, "without_reference_range": 0, "without_critical_limit": 0}
    now = utc_iso_now()
    base = (
        "FROM lab_tests t WHERE t.active = 1 AND t.kind = 'analyte' "
        "AND t.result_type = 'numeric' AND t.performed_at = 'lab'"
    )
    total = conn.execute(f"SELECT COUNT(*) {base}").fetchone()[0]
    no_range = conn.execute(
        f"SELECT COUNT(*) {base} AND NOT EXISTS (SELECT 1 FROM lab_reference_ranges r "
        f"WHERE r.test_id = t.id AND {_current_sql('r')})",
        (now, now),
    ).fetchone()[0]
    no_limit = conn.execute(
        f"SELECT COUNT(*) {base} AND NOT EXISTS (SELECT 1 FROM lab_critical_limits c "
        f"WHERE c.test_id = t.id AND {_current_sql('c')})",
        (now, now),
    ).fetchone()[0]
    return {
        "numeric_tests": int(total),
        "without_reference_range": int(no_range),
        "without_critical_limit": int(no_limit),
    }


def applicable_range(
    conn,
    test_id: str,
    *,
    sex: Any,
    date_of_birth: Any,
    at: str | None = None,
    version_at: str | None = None,
) -> RangeChoice:
    """The approved reference range for a patient of this sex and date of
    birth at `at` (the collection instant; now when not given).

    `version_at` picks WHICH approved version is read (the range in force at
    that instant); it defaults to `at`. Result entry (lis-10) passes the
    collection instant as `at`, so the patient's age is the age when the
    sample was taken, and now as `version_at`, so a value is flagged against
    the range the laboratory has approved when the value is entered."""
    at = at or utc_iso_now()
    rows = _list_versioned(
        conn, "lab_reference_ranges", test_id=test_id, include_retired=False, at=version_at or at
    )
    return select_applicable(rows, sex=normalise_sex(sex), age_days=age_days_at(date_of_birth, at))


def applicable_critical_limit(
    conn, test_id: str, *, sex: Any, date_of_birth: Any, at: str | None = None
) -> RangeChoice:
    """The approved critical limit, by the same rule as the reference range."""
    at = at or utc_iso_now()
    rows = _list_versioned(
        conn, "lab_critical_limits", test_id=test_id, include_retired=False, at=at
    )
    return select_applicable(
        rows, sex=normalise_sex(sex), age_days=age_days_at(date_of_birth, at), limits=True
    )


def patient_demographics(conn, patient_id: str) -> dict:
    """The two facts a range depends on, read from patients.date_of_birth and
    patients.sex. The static patients.age column is deliberately not read."""
    r = conn.execute(
        "SELECT date_of_birth, sex FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()
    if r is None:
        return {"date_of_birth": None, "sex": None}
    return {"date_of_birth": r["date_of_birth"], "sex": r["sex"]}


def applicable_range_for_patient(
    conn, test_id: str, patient_id: str, *, at: str | None = None, version_at: str | None = None
) -> RangeChoice:
    demo = patient_demographics(conn, patient_id)
    return applicable_range(
        conn,
        test_id,
        sex=demo["sex"],
        date_of_birth=demo["date_of_birth"],
        at=at,
        version_at=version_at,
    )


def coded_values(test: dict) -> list[str]:
    """A coded test's list of allowed values, from coded_values_json. An
    unreadable or missing list is empty, which refuses every value (a coded
    result outside the list is never stored)."""
    raw = (test or {}).get("coded_values_json")
    if not raw:
        return []
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


# ════════════════════════════════════════════════════════════════════════════
#  Writes (each one BEGIN IMMEDIATE transaction with its audit row inside)
# ════════════════════════════════════════════════════════════════════════════


def _require_master(actor: dict) -> None:
    if not actor or not actor.get("id") or not has_permission(actor.get("role", ""), LAB_MASTER):
        raise LabMasterError(
            "permission_denied", "Only a pathologist can change the laboratory catalogue.", 403
        )


def _audit(
    conn,
    actor: dict,
    action: str,
    *,
    target_type: str,
    target_id: Any,
    summary: str,
    metadata: dict | None = None,
) -> None:
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_config",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type=target_type,
        target_id=str(target_id),
        metadata=metadata or None,
    )


def _require_tables(conn) -> None:
    if not tables_present(
        conn, _CATALOGUE_TABLES + ("lab_reference_ranges", "lab_critical_limits")
    ):
        raise LabMasterError("not_ready", "The laboratory tables are not installed yet.", 409)


@overload
def _clean_text(value: Any, field: str, *, required: Literal[True], max_len: int = 200) -> str: ...


@overload
def _clean_text(
    value: Any, field: str, *, required: Literal[False] = False, max_len: int = 200
) -> str | None: ...


@overload
def _clean_text(
    value: Any, field: str, *, required: bool = False, max_len: int = 200
) -> str | None: ...


def _clean_text(
    value: Any, field: str, *, required: bool = False, max_len: int = 200
) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        if required:
            raise LabMasterError("missing_field", f"{field} is required.")
        return None
    if len(text) > max_len:
        raise LabMasterError("too_long", f"{field} is longer than {max_len} characters.")
    return text


def _positive_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise LabMasterError("invalid_value", f"{field} must be a whole number.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise LabMasterError("invalid_value", f"{field} must be a whole number.") from None
    if number <= 0 or str(number) != str(value).strip().lstrip("+"):
        raise LabMasterError("invalid_value", f"{field} must be a whole number above zero.")
    return number


def _check_reference(conn, table: str, key: str, value: str | None, label: str) -> None:
    if value is None:
        return
    if (
        conn.execute(f"SELECT 1 FROM {table} WHERE {key} = ? AND active = 1", (value,)).fetchone()
        is None
    ):
        raise LabMasterError("unknown_reference", f"Unknown {label}: {value}.")


_EDITABLE_TEST_FIELDS = (
    "name",
    "short_name",
    "performed_at",
    "section_id",
    "specimen_type_code",
    "unit",
    "decimal_places",
    "coded_values_json",
    "orderable",
    "tat_routine_min",
    "tat_urgent_min",
    "tat_stat_min",
    "delta_kind",
    "delta_threshold_milli",
    "delta_window_hours",
    "active",
    "result_type",
)


def _validated_test_fields(conn, data: dict, *, current: dict | None) -> dict:
    """The fields of a create or update, checked. `current` is None on create."""
    out: dict[str, Any] = {}
    keys = set(data)
    if current is None:
        keys |= {"name", "short_name", "specimen_type_code"}
    if "name" in keys:
        out["name"] = _clean_text(data.get("name"), "Name", required=True)
    if "short_name" in keys:
        out["short_name"] = _clean_text(
            data.get("short_name"), "Short name", required=True, max_len=SHORT_NAME_MAX
        )
    if "performed_at" in keys:
        if data.get("performed_at") not in PERFORMED_AT:
            raise LabMasterError("invalid_value", "Performed at must be lab or ward_poct.")
        out["performed_at"] = data["performed_at"]
    if "result_type" in keys:
        if data.get("result_type") not in RESULT_TYPES:
            raise LabMasterError("invalid_value", "Result type must be numeric, text or coded.")
        out["result_type"] = data["result_type"]
    if "section_id" in keys:
        out["section_id"] = _clean_text(data.get("section_id"), "Section")
        _check_reference(conn, "lab_sections", "id", out["section_id"], "section")
    if "specimen_type_code" in keys:
        out["specimen_type_code"] = _clean_text(
            data.get("specimen_type_code"), "Specimen type", required=True
        )
        _check_reference(
            conn, "lab_specimen_types", "code", out["specimen_type_code"], "specimen type"
        )
    if "unit" in keys:
        out["unit"] = _clean_text(data.get("unit"), "Unit", max_len=40) or ""
    if "decimal_places" in keys:
        dp = data.get("decimal_places")
        if not isinstance(dp, int) or isinstance(dp, bool) or not 0 <= dp <= MAX_DECIMAL_PLACES:
            raise LabMasterError(
                "invalid_value",
                f"Decimal places must be a whole number from 0 to {MAX_DECIMAL_PLACES}.",
            )
        out["decimal_places"] = dp
    if "coded_values_json" in keys:
        raw = data.get("coded_values_json")
        if raw in (None, ""):
            out["coded_values_json"] = None
        else:
            try:
                values = (
                    raw
                    if isinstance(raw, list)
                    else json.loads(raw)
                    if isinstance(raw, str)
                    else None
                )
            except ValueError:
                values = None
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v.strip() for v in values
            ):
                raise LabMasterError("invalid_value", "Coded values must be a list of words.")
            out["coded_values_json"] = json.dumps([v.strip() for v in values])
    for flag in ("orderable", "active"):
        if flag in keys:
            if not isinstance(data.get(flag), bool):
                raise LabMasterError("invalid_value", f"{flag} must be true or false.")
            out[flag] = 1 if data[flag] else 0
    for field in ("tat_routine_min", "tat_urgent_min", "tat_stat_min", "delta_window_hours"):
        if field in keys:
            out[field] = _positive_int(data.get(field), field.replace("_", " "))
    if "delta_kind" in keys:
        kind = data.get("delta_kind")
        if kind not in (None, "", *DELTA_KINDS):
            raise LabMasterError("invalid_value", "Delta kind must be absolute or percent.")
        out["delta_kind"] = kind or None
    if "delta_threshold_milli" in keys:
        out["delta_threshold_milli"] = _positive_int(
            data.get("delta_threshold_milli"), "delta threshold"
        )
    merged = dict(current or {})
    merged.update(out)
    if merged.get("performed_at", "lab") == "lab" and not merged.get("section_id"):
        raise LabMasterError("missing_field", "A laboratory test needs a section.")
    if bool(merged.get("delta_kind")) != bool(merged.get("delta_threshold_milli")):
        raise LabMasterError(
            "invalid_value", "A delta check needs both its kind and its threshold, or neither."
        )
    if merged.get("result_type") == "coded" and not merged.get("coded_values_json"):
        raise LabMasterError("missing_field", "A coded test needs its list of coded values.")
    return out


def create_test(data: dict, *, actor: dict) -> dict:
    """Add a test (analyte or panel) to the catalogue. Its code is its
    identity and never changes; an orderable code is registered in
    terminology in the same transaction."""
    _require_master(actor)
    system = str(data.get("code_system") or "").strip().upper()
    code = _clean_text(data.get("code"), "Code", required=True, max_len=64)
    if system not in CODE_SYSTEMS:
        raise LabMasterError("invalid_value", "Code system must be LOINC or LOCAL.")
    kind = data.get("kind", "analyte")
    if kind not in KINDS:
        raise LabMasterError("invalid_value", "Kind must be analyte or panel.")
    now = utc_iso_now()
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        tid = test_id_for(system, code)
        if conn.execute(
            "SELECT 1 FROM lab_tests WHERE id = ? OR (code_system = ? AND code = ?)",
            (tid, system, code),
        ).fetchone():
            raise LabMasterError("duplicate", f"{system} {code} is already in the catalogue.", 409)
        fields = _validated_test_fields(
            conn, {k: v for k, v in data.items() if k in _EDITABLE_TEST_FIELDS}, current=None
        )
        if kind == "panel":
            fields["result_type"] = "text"
            fields.pop("decimal_places", None)
        row = {
            "id": tid,
            "code": code,
            "code_system": system,
            "kind": kind,
            "performed_at": "lab",
            "result_type": "numeric",
            "decimal_places": 0,
            "orderable": 1,
            "active": 1,
            "version": 1,
            "created_at": now,
            "updated_by": actor["id"],
            "updated_at": now,
        }
        row.update(fields)
        cols = ", ".join(row)
        conn.execute(
            f"INSERT INTO lab_tests ({cols}) VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        _register_catalogue_terminology(conn)
        _audit(
            conn,
            actor,
            "lab_test_created",
            target_type="lab_test",
            target_id=tid,
            summary=f"Laboratory test added: {row['name']}",
            metadata={"after": row},
        )
        _written_row = get_test(conn, tid)
        assert _written_row is not None, "Expected the row written in this transaction"
        return _written_row


def update_test(test_id: str, changes: dict, *, actor: dict) -> dict:
    """Change a test. Code, code system and kind never change. A change of
    unit, or of decimal places that a current range or limit could not be
    stated in, is refused until those are retired: a range is a number in a
    unit at a precision."""
    _require_master(actor)
    unknown = set(changes) - set(_EDITABLE_TEST_FIELDS)
    if unknown:
        raise LabMasterError(
            "not_editable", f"These cannot be changed: {', '.join(sorted(unknown))}."
        )
    if not changes:
        raise LabMasterError("missing_field", "Nothing to change.")
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = conn.execute(
            f"SELECT {_TEST_COLUMNS} FROM lab_tests WHERE id = ?", (test_id,)
        ).fetchone()
        if before is None:
            raise LabMasterError("not_found", "No such laboratory test.", 404)
        before = dict(before)
        fields = _validated_test_fields(conn, changes, current=before)
        if before["kind"] == "panel" and ("result_type" in fields or "decimal_places" in fields):
            raise LabMasterError("not_editable", "A panel has no result type or decimal places.")
        now = utc_iso_now()
        ranges = _list_versioned(
            conn, "lab_reference_ranges", test_id=test_id, include_retired=False
        ) + _list_versioned(conn, "lab_critical_limits", test_id=test_id, include_retired=False)
        if ranges and "unit" in fields and (fields["unit"] or "") != (before["unit"] or ""):
            raise LabMasterError(
                "retire_ranges_first",
                "Retire this test's current ranges and limits before changing its unit.",
                409,
            )
        if ranges and "result_type" in fields and fields["result_type"] != "numeric":
            raise LabMasterError(
                "retire_ranges_first",
                "Retire this test's current ranges and limits before it stops being numeric.",
                409,
            )
        if "decimal_places" in fields and ranges:
            step = 10 ** (MAX_DECIMAL_PLACES - fields["decimal_places"])
            for r in ranges:
                for key in ("low_milli", "high_milli", "critical_low_milli", "critical_high_milli"):
                    v = r.get(key)
                    if v is not None and int(v) % step:
                        raise LabMasterError(
                            "retire_ranges_first",
                            "A current range or limit has more decimal places than the new "
                            "precision. Retire it first.",
                            409,
                        )
        fields["version"] = int(before["version"]) + 1
        fields["updated_by"] = actor["id"]
        fields["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE lab_tests SET {assignments} WHERE id = ?", (*fields.values(), test_id)
        )
        _register_catalogue_terminology(conn)
        after = dict(
            conn.execute(
                f"SELECT {_TEST_COLUMNS} FROM lab_tests WHERE id = ?", (test_id,)
            ).fetchone()
        )
        _audit(
            conn,
            actor,
            "lab_test_changed",
            target_type="lab_test",
            target_id=test_id,
            summary=f"Laboratory test changed: {after['name']} (version {after['version']})",
            metadata={
                "before": {k: before.get(k) for k in fields if k in before},
                "after": {k: after.get(k) for k in fields},
            },
        )
        _written_row = get_test(conn, test_id)
        assert _written_row is not None, "Expected the row written in this transaction"
        return _written_row


def set_panel_components(panel_id: str, component_ids: list, *, actor: dict) -> dict:
    """Replace a panel's components (each an existing analyte), in order."""
    _require_master(actor)
    if not isinstance(component_ids, list) or not component_ids:
        raise LabMasterError("missing_field", "A panel needs at least one component.")
    ids = [str(c).strip() for c in component_ids]
    if len(set(ids)) != len(ids):
        raise LabMasterError("invalid_value", "A component is listed twice.")
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        panel = conn.execute(
            "SELECT id, name, kind, version FROM lab_tests WHERE id = ?", (panel_id,)
        ).fetchone()
        if panel is None:
            raise LabMasterError("not_found", "No such laboratory test.", 404)
        if panel["kind"] != "panel":
            raise LabMasterError("not_a_panel", "Only a panel has components.")
        for cid in ids:
            c = conn.execute("SELECT kind FROM lab_tests WHERE id = ?", (cid,)).fetchone()
            if c is None:
                raise LabMasterError("unknown_reference", f"Unknown test: {cid}.")
            if c["kind"] != "analyte" or cid == panel_id:
                raise LabMasterError("invalid_value", f"{cid} is not an analyte.")
        before = [
            r["component_test_id"]
            for r in conn.execute(
                "SELECT component_test_id FROM lab_panel_components WHERE panel_test_id = ? "
                "ORDER BY sort_order, component_test_id",
                (panel_id,),
            ).fetchall()
        ]
        conn.execute("DELETE FROM lab_panel_components WHERE panel_test_id = ?", (panel_id,))
        for order, cid in enumerate(ids, start=1):
            conn.execute(
                "INSERT INTO lab_panel_components (panel_test_id, component_test_id, "
                "sort_order) VALUES (?, ?, ?)",
                (panel_id, cid, order * 10),
            )
        now = utc_iso_now()
        conn.execute(
            "UPDATE lab_tests SET version = version + 1, updated_by = ?, updated_at = ? "
            "WHERE id = ?",
            (actor["id"], now, panel_id),
        )
        _audit(
            conn,
            actor,
            "lab_panel_changed",
            target_type="lab_test",
            target_id=panel_id,
            summary=f"Panel components changed: {panel['name']}",
            metadata={"before": before, "after": ids},
        )
        _written_row = get_test(conn, panel_id)
        assert _written_row is not None, "Expected the row written in this transaction"
        return _written_row


def _band(data: dict) -> tuple[str, int, int | None]:
    sex = str(data.get("sex") or "any").strip().lower()
    if sex not in SEXES:
        raise LabMasterError("invalid_value", "Sex must be any, male or female.")
    raw_min = data.get("age_min_days", 0)
    raw_max = data.get("age_max_days")
    try:
        age_min = 0 if raw_min in (None, "") else int(raw_min)
        age_max = None if raw_max in (None, "") else int(raw_max)
    except (TypeError, ValueError):
        raise LabMasterError("invalid_value", "Ages must be whole numbers of days.") from None
    if isinstance(raw_min, bool) or isinstance(raw_max, bool) or age_min < 0:
        raise LabMasterError("invalid_value", "Ages must be whole numbers of days.")
    if age_max is not None and age_max <= age_min:
        raise LabMasterError("invalid_value", "The upper age must be above the lower age.")
    return sex, age_min, age_max


def _numeric_test(conn, test_id: str) -> dict:
    t = conn.execute(
        "SELECT id, name, kind, result_type, unit, decimal_places, active "
        "FROM lab_tests WHERE id = ?",
        (test_id,),
    ).fetchone()
    if t is None:
        raise LabMasterError("not_found", "No such laboratory test.", 404)
    if t["kind"] != "analyte" or t["result_type"] != "numeric":
        raise LabMasterError("not_numeric", "Only a numeric analyte has a range or a limit.")
    if not t["active"]:
        raise LabMasterError("inactive", "This test is switched off in the catalogue.")
    return dict(t)


def _approve_versioned(table: str, test_id: str, data: dict, *, actor: dict, limits: bool) -> dict:
    _require_master(actor)
    sex, age_min, age_max = _band(data)
    method_note = _clean_text(data.get("method_note"), "Approval note", required=True, max_len=500)
    agreed_with = (
        _clean_text(data.get("agreed_with"), "Agreed with", required=True, max_len=500)
        if limits
        else None
    )
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        test = _numeric_test(conn, test_id)
        dp = int(test["decimal_places"])
        lo_key, hi_key = ("critical_low", "critical_high") if limits else ("low", "high")
        low = to_milli(data.get(lo_key), dp, field="The lower value")
        high = to_milli(data.get(hi_key), dp, field="The upper value")
        if low is None and high is None:
            raise LabMasterError("missing_field", "Give a lower value, an upper value, or both.")
        if low is not None and high is not None and (high < low or (limits and high == low)):
            raise LabMasterError("invalid_value", "The upper value must be above the lower value.")
        now = utc_iso_now()
        current = _list_versioned(conn, table, test_id=test_id, include_retired=False, at=now)
        same_band = [
            r
            for r in current
            if r["sex"] == sex
            and int(r["age_min_days"]) == age_min
            and r["age_max_days"] == age_max
        ]
        for r in current:
            if r in same_band:
                continue
            if _sex_compatible(r["sex"], sex) and _bands_overlap(
                int(r["age_min_days"]), r["age_max_days"], age_min, age_max
            ):
                what = "limit" if limits else "range"
                raise LabMasterError(
                    "overlap",
                    f"This overlaps a current {what} for {r['sex']} patients aged "
                    f"{r['age_min_days']} to {r['age_max_days'] or 'any'} days. Retire that one "
                    "first, or approve the same band to replace it.",
                    409,
                )
        for r in same_band:
            conn.execute(f"UPDATE {table} SET effective_to = ? WHERE id = ?", (now, r["id"]))
        display = (limit_display if limits else range_display)(low, high, dp, test["unit"])
        if limits:
            cur = conn.execute(
                """INSERT INTO lab_critical_limits
                   (test_id, sex, age_min_days, age_max_days, critical_low_milli,
                    critical_high_milli, display_text, effective_from, effective_to,
                    approved_by, approved_at, agreed_with, method_note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (
                    test_id,
                    sex,
                    age_min,
                    age_max,
                    low,
                    high,
                    display,
                    now,
                    actor["id"],
                    now,
                    agreed_with,
                    method_note,
                ),
            )
        else:
            cur = conn.execute(
                """INSERT INTO lab_reference_ranges
                   (test_id, sex, age_min_days, age_max_days, low_milli, high_milli,
                    display_text, effective_from, effective_to, approved_by, approved_at,
                    method_note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)""",
                (
                    test_id,
                    sex,
                    age_min,
                    age_max,
                    low,
                    high,
                    display,
                    now,
                    actor["id"],
                    now,
                    method_note,
                ),
            )
        new_id = cur.lastrowid
        row = dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (new_id,)).fetchone())
        _audit(
            conn,
            actor,
            "lab_critical_limit_approved" if limits else "lab_range_approved",
            target_type="lab_critical_limit" if limits else "lab_reference_range",
            target_id=new_id,
            summary=f"{'Critical limit' if limits else 'Reference range'} approved for "
            f"{test['name']}: {display}",
            metadata={
                "test_id": test_id,
                "replaced": [r["id"] for r in same_band],
                "after": {k: row[k] for k in row if k not in ("method_note",)},
            },
        )
        return row


def approve_reference_range(test_id: str, data: dict, *, actor: dict) -> dict:
    """Approve a reference range for one sex and age band of a numeric test.
    It replaces the current range for the same band and takes effect now."""
    return _approve_versioned("lab_reference_ranges", test_id, data, actor=actor, limits=False)


def approve_critical_limit(test_id: str, data: dict, *, actor: dict) -> dict:
    """Approve a critical limit, naming the clinicians it was agreed with
    (ruling LIS-R1)."""
    return _approve_versioned("lab_critical_limits", test_id, data, actor=actor, limits=True)


def _retire_versioned(table: str, row_id: int, reason: str, *, actor: dict, limits: bool) -> dict:
    _require_master(actor)
    reason = _clean_text(reason, "Reason", required=True, max_len=500)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        now = utc_iso_now()
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise LabMasterError("not_found", "No such range or limit.", 404)
        if (
            row["effective_to"] is not None
            and conn.execute(
                "SELECT julianday(?) <= julianday(?)", (row["effective_to"], now)
            ).fetchone()[0]
        ):
            raise LabMasterError("already_retired", "This one is already retired.", 409)
        conn.execute(f"UPDATE {table} SET effective_to = ? WHERE id = ?", (now, row_id))
        _audit(
            conn,
            actor,
            "lab_critical_limit_retired" if limits else "lab_range_retired",
            target_type="lab_critical_limit" if limits else "lab_reference_range",
            target_id=row_id,
            summary=f"{'Critical limit' if limits else 'Reference range'} retired: "
            f"{row['display_text']}",
            metadata={"test_id": row["test_id"], "reason": reason},
        )
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def retire_reference_range(range_id: int, reason: str, *, actor: dict) -> dict:
    return _retire_versioned("lab_reference_ranges", range_id, reason, actor=actor, limits=False)


def retire_critical_limit(limit_id: int, reason: str, *, actor: dict) -> dict:
    return _retire_versioned("lab_critical_limits", limit_id, reason, actor=actor, limits=True)


def upsert_specimen_type(code: str, data: dict, *, actor: dict) -> dict:
    _require_master(actor)
    code = _clean_text(code, "Code", required=True, max_len=64)
    row = {
        "display": _clean_text(data.get("display"), "Name", required=True),
        "container": _clean_text(data.get("container"), "Container"),
        "container_colour_name": _clean_text(data.get("container_colour_name"), "Container colour"),
        "min_volume_ul": _positive_int(data.get("min_volume_ul"), "minimum volume"),
        "stability_minutes": _positive_int(data.get("stability_minutes"), "stability"),
        "handling_note": _clean_text(data.get("handling_note"), "Handling note", max_len=500),
        "active": 0 if data.get("active") is False else 1,
    }
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = conn.execute("SELECT * FROM lab_specimen_types WHERE code = ?", (code,)).fetchone()
        if (
            before is not None
            and row["active"] == 0
            and conn.execute(
                "SELECT 1 FROM lab_tests WHERE specimen_type_code = ? AND active = 1", (code,)
            ).fetchone()
        ):
            raise LabMasterError("in_use", "An active test still uses this specimen type.", 409)
        conn.execute(
            """INSERT INTO lab_specimen_types (code, display, container, container_colour_name,
                   min_volume_ul, stability_minutes, handling_note, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET display = excluded.display,
                   container = excluded.container,
                   container_colour_name = excluded.container_colour_name,
                   min_volume_ul = excluded.min_volume_ul,
                   stability_minutes = excluded.stability_minutes,
                   handling_note = excluded.handling_note, active = excluded.active""",
            (code, *row.values()),
        )
        _audit(
            conn,
            actor,
            "lab_specimen_type_changed",
            target_type="lab_specimen_type",
            target_id=code,
            summary=f"Specimen type saved: {row['display']}",
            metadata={"before": dict(before) if before else None, "after": row},
        )
        return dict(
            conn.execute("SELECT * FROM lab_specimen_types WHERE code = ?", (code,)).fetchone()
        )


def upsert_section(section_id: str, data: dict, *, actor: dict) -> dict:
    _require_master(actor)
    section_id = _clean_text(section_id, "Section id", required=True, max_len=64)
    name = _clean_text(data.get("name"), "Name", required=True)
    sort_order = data.get("sort_order", 0)
    if not isinstance(sort_order, int) or isinstance(sort_order, bool):
        raise LabMasterError("invalid_value", "Sort order must be a whole number.")
    active = 0 if data.get("active") is False else 1
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = conn.execute("SELECT * FROM lab_sections WHERE id = ?", (section_id,)).fetchone()
        if (
            active == 0
            and conn.execute(
                "SELECT 1 FROM lab_tests WHERE section_id = ? AND active = 1", (section_id,)
            ).fetchone()
        ):
            raise LabMasterError("in_use", "An active test still belongs to this section.", 409)
        conn.execute(
            """INSERT INTO lab_sections (id, name, site_id, active, sort_order)
               VALUES (?, ?, NULL, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, active = excluded.active,
                   sort_order = excluded.sort_order""",
            (section_id, name, active, sort_order),
        )
        _audit(
            conn,
            actor,
            "lab_section_changed",
            target_type="lab_section",
            target_id=section_id,
            summary=f"Laboratory section saved: {name}",
            metadata={
                "before": dict(before) if before else None,
                "after": {"name": name, "active": active, "sort_order": sort_order},
            },
        )
        return dict(
            conn.execute("SELECT * FROM lab_sections WHERE id = ?", (section_id,)).fetchone()
        )


def upsert_rejection_reason(code: str, data: dict, *, actor: dict) -> dict:
    _require_master(actor)
    code = _clean_text(code, "Code", required=True, max_len=64)
    display = _clean_text(data.get("display"), "Name", required=True)
    incident_prompt = 1 if data.get("incident_prompt") else 0
    active = 0 if data.get("active") is False else 1
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = conn.execute(
            "SELECT * FROM lab_rejection_reasons WHERE code = ?", (code,)
        ).fetchone()
        conn.execute(
            """INSERT INTO lab_rejection_reasons (code, display, incident_prompt, active)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET display = excluded.display,
                   incident_prompt = excluded.incident_prompt, active = excluded.active""",
            (code, display, incident_prompt, active),
        )
        _audit(
            conn,
            actor,
            "lab_rejection_reason_changed",
            target_type="lab_rejection_reason",
            target_id=code,
            summary=f"Rejection reason saved: {display}",
            metadata={
                "before": dict(before) if before else None,
                "after": {"display": display, "incident_prompt": incident_prompt, "active": active},
            },
        )
        return dict(
            conn.execute("SELECT * FROM lab_rejection_reasons WHERE code = ?", (code,)).fetchone()
        )
