"""Orb — the patient chart timeline, and the "since you last looked" watermark.

WHAT THIS IS FOR. Orb already stores everything that happens to a patient, but
until now it stored it in fourteen places and showed it in fourteen places: notes
in one modal, imaging in another, medications in a third, orders on a different
tab, administrations inside the eMAR, bundles on the ward round. A doctor coming
back to a chart after a night off had no way to ask the one question they
actually ask — *what happened to this patient since I last looked?* — except by
opening every one of those surfaces and reading the timestamps.

``build_chart_timeline`` answers it. It is a READ-ONLY projection over tables
that already exist; it creates no new clinical record and it is not a source of
truth for anything. If it disagreed with the eMAR, the eMAR is right.

The one piece of new state is ``patient_review_marks`` (migration 17) — a
per-clinician bookmark saying "I have read this chart up to here". See that
migration's docstring for why it is per-user, why it holds no PHI, and why a
missing row means *no badge* rather than *everything is new*.

── THE TIMESTAMP TRAP, WRITTEN DOWN ─────────────────────────────────────────
The columns this file reads are a mix of two dialects, in the SAME column in
some tables: SQLite's ``CURRENT_TIMESTAMP`` default writes ``"2026-08-08
01:15:00"`` (bare, UTC, space separator) while application code writes
``"2026-08-08T01:15:00.123456+00:00"``. Comparing those two as strings is
wrong twice over — the space sorts before the ``T``, and ``'Z'`` sorts after
``'.'`` so a whole-second stamp compares as LATER than a sub-second one in the
same second.

So nothing in this file compares raw timestamp strings. Two normalisations, and
they must agree:

* In Python, ``_normalize_ts`` parses either dialect into an aware UTC
  ``datetime``; every sort and every ``is_new`` decision uses the datetime.
* In SQL, ``_TS_NORM`` wraps the column in ``strftime`` — which parses both
  dialects and converts a tz-suffixed value to UTC — producing one fixed-width
  string, so ``>`` means what it says. Used for the counts, which have to be
  computed by the database because they must be exact over rows we did not
  fetch.

A timestamp neither can parse is dropped from the timeline rather than sorted
to an arbitrary position. That is the honest failure: a missing row is visible
(the chart has a hole someone will notice), a misplaced one is not.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from database import db_session

from services import lab_observations
from services.sepsis_bundle_service import bundle_label, trigger_reason_text

# One fixed-width, UTC-normalised rendering of any of the timestamp dialects
# above, for comparisons done inside SQLite. `%f` is SS.SSS, so every value is
# the same width and lexical order is chronological order. Returns NULL for a
# value SQLite cannot parse — which drops the row from a count rather than
# miscounting it.
_TS_NORM = "strftime('%Y-%m-%dT%H:%M:%f', {col})"


def _norm(col: str) -> str:
    return _TS_NORM.format(col=col)


# ═════════════════════════════════════════════════════════════════════════════
#  Timestamp normalisation
# ═════════════════════════════════════════════════════════════════════════════


def _normalize_ts(raw: Any) -> datetime | None:
    """Parse either timestamp dialect into an aware UTC datetime, or None.

    None means "unparseable" and the caller drops the row. It never means
    "epoch" — silently substituting a sentinel date is how an untimed event ends
    up at the top or bottom of a clinical timeline pretending to be real.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # "YYYY-MM-DD HH:MM:SS" → "YYYY-MM-DDTHH:MM:SS" (only the first space; a
    # trailing " +05:30" offset stays intact for fromisoformat).
    if "T" not in text and " " in text:
        head, _, tail = text.partition(" ")
        text = f"{head}T{tail}"
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Bare strings from SQLite are UTC by definition (CURRENT_TIMESTAMP is
        # documented as UTC). Reading them as local would shift every charted
        # time by the appliance's offset — the exact defect src/lib/datetime.ts
        # was written to stop on the browser side.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    """Emit one unambiguous wire format: ISO-8601 UTC with a Z suffix.

    The frontend's parseUTC accepts this untouched, so the browser never has to
    guess which dialect a given row came from.
    """
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _clip(text: Any, length: int = 220) -> str:
    """Trim free clinical text for a timeline row.

    The timeline is an index, not a viewer: the full note is one click away in
    the surface that owns it. Clipping here also bounds the response — a chart
    with 40 dictated notes must not ship 200 KB of prose to draw a rail.
    """
    if text is None:
        return ""
    s = " ".join(str(text).split())
    return s if len(s) <= length else s[: length - 1].rstrip() + "…"


# ═════════════════════════════════════════════════════════════════════════════
#  The sources
# ═════════════════════════════════════════════════════════════════════════════
#
# ONE declaration per clinical stream, used to generate BOTH the detail query
# and the counting query. They are generated rather than written twice because
# the two drifting apart is the failure that matters: a stream present in the
# detail list but missing from the count shows "0 new" above six new rows, and a
# stream counted but not listed shows "3 new" above nothing.


@dataclass(frozen=True)
class _Source:
    kind: str
    # Table + joins. Must expose `patient_id` (or alias one) and the ts column.
    from_sql: str
    patient_col: str
    ts_col: str
    id_col: str
    # Extra columns the renderer needs, as "expr AS name" fragments.
    select_cols: tuple[str, ...] = ()
    where_sql: str = ""
    render: Callable[[dict], dict] = field(default=lambda r: {})
    # Streams that can never be "new" relative to a review (the admission that
    # precedes every review of that admission) still appear on the rail; they
    # are simply not worth counting. Nothing currently sets this — it exists so
    # a future stream can opt out explicitly rather than by being forgotten.
    countable: bool = True
    # The detail query's ORDER BY. Blank means newest first by the normalised
    # ts_col. A stream with a shared reader that owns its ordering (lab
    # results: services/lab_observations.py) passes that reader's.
    order_sql: str = ""
    # A config feature id (config.feature_enabled) the stream belongs to.
    # Blank means always on. A stream whose feature is off is left out of the
    # rail AND the count, exactly as if its table did not exist, so a site
    # that never switches the feature on sees the timeline it always had.
    feature: str = ""

    @property
    def base_table(self) -> str:
        """The first table in `from_sql` — the one whose absence removes the
        stream. Joined tables (users, orders, medications) are core and are not
        checked; a DB without `users` is not a DB Orb can serve at all."""
        return self.from_sql.split()[0].strip('"')

    @property
    def live_where_sql(self) -> str:
        """`where_sql` as it applies right now. A subclass narrows it on a
        switch (the medication orders under the Orb Ward Record)."""
        return self.where_sql


def _r_admission(r: dict) -> dict:
    bits = [b for b in (r.get("ward"), r.get("bed") and f"Bed {r['bed']}") if b]
    detail = " · ".join(bits)
    if r.get("diagnosis"):
        detail = f"{detail} · {r['diagnosis']}" if detail else str(r["diagnosis"])
    return {"title": "Admitted", "detail": detail, "severity": "info"}


def _r_note(r: dict) -> dict:
    doc_type = (r.get("doc_type") or "note").upper()
    return {
        "title": r.get("doc_title") or f"{doc_type} note",
        "detail": _clip(r.get("content")),
        "severity": "info",
        "badge": doc_type,
    }


def _r_scribe(r: dict) -> dict:
    return {
        "title": f"Scribe {(r.get('note_format') or 'raw').upper()} note",
        "detail": _clip(r.get("structured_note") or r.get("raw_transcript")),
        "severity": "info",
        "badge": "SCRIBE",
    }


def _r_nursing_note(r: dict) -> dict:
    return {
        "title": f"Nursing note ({r.get('shift_label') or 'shift not recorded'})",
        "detail": _clip(r.get("content")),
        "severity": "info",
        "badge": "NURSING",
    }


def _r_imaging(r: dict) -> dict:
    return {
        "title": f"Imaging analysed: {(r.get('image_type') or 'image').upper()}",
        "detail": _clip(r.get("analysis")),
        "severity": "info",
        "badge": "LENS",
    }


def _r_alert(r: dict) -> dict:
    drivers = r.get("drivers")
    parsed: list = []
    if drivers:
        try:
            loaded = json.loads(drivers)
            if isinstance(loaded, list):
                parsed = [str(d) for d in loaded]
        except (ValueError, TypeError):
            parsed = [str(drivers)]
    return {
        # The stored `level` is the NEWS2 clinical band. The 0-1 score is a
        # normalised index and is NOT a probability of deterioration — Wave A
        # found it being rendered as a percentage. It is not shown here at all.
        "title": "Critical deterioration alert",
        "detail": ", ".join(parsed),
        "severity": "critical",
        "badge": "VIGIL",
    }


@dataclass(frozen=True)
class _ScoredSource(_Source):
    """A stream that IS a score: the stored critical NEWS2 band. While scoring
    is switched off (ORB_SCORING_ENABLED, the Orb Ward Record) it names no
    table, so available_sources leaves it out of the rail and the count the
    same way it leaves out a stream whose table does not exist."""

    @property
    def base_table(self) -> str:
        from services.vigil_service import scoring_enabled

        return super().base_table if scoring_enabled() else ""


@dataclass(frozen=True)
class _DrugChartSource(_Source):
    """A stream that IS the drug chart: medicines charted and doses given. On
    an Orb Ward Record computer the drug chart stays on paper
    (services/drug_chart), so it names no table and is left out of the rail
    and the count, as a stream whose table does not exist is."""

    @property
    def base_table(self) -> str:
        from services.drug_chart import drug_chart_enabled

        return super().base_table if drug_chart_enabled() else ""


@dataclass(frozen=True)
class _NursingNoteSource(_Source):
    """The nurses' shift notes (services/nursing_note_service.py). On an Orb
    Ward Record computer nursing notes stay on paper (SALES_PLAN section 3), so
    the stream names no table and is left out of the rail and the count, as a
    stream whose table does not exist is."""

    @property
    def base_table(self) -> str:
        from services.nursing_note_service import nursing_notes_enabled

        return super().base_table if nursing_notes_enabled() else ""


#: The order rows that are not medication orders, for _OrderSource.
_NOT_A_MEDICATION_ORDER = "o.order_type != 'medication'"


@dataclass(frozen=True)
class _OrderSource(_Source):
    """An order stream. On an Orb Ward Record computer a medication order is
    part of the drug chart, so its rows (and its status changes) are left out
    of the rail and the count; every other order stays."""

    @property
    def live_where_sql(self) -> str:
        from services.drug_chart import drug_chart_enabled

        if drug_chart_enabled():
            return self.where_sql
        if self.where_sql:
            return f"({self.where_sql}) AND {_NOT_A_MEDICATION_ORDER}"
        return _NOT_A_MEDICATION_ORDER


def _r_medication(r: dict) -> dict:
    dose = " ".join(str(x) for x in (r.get("dosage"), r.get("route"), r.get("frequency")) if x)
    status = (r.get("med_status") or "active").lower()
    verb = "Discontinued" if status == "discontinued" else "Prescribed"
    detail = dose
    if (r.get("med_source") or "clinician") in ("llm_extracted", "agentic") and not r.get(
        "verified_by"
    ):
        # Never let an AI-extracted, pharmacist-unverified medication read on a
        # timeline exactly like a charted one. Same rule as the discharge
        # reconciliation in discharge_service.
        detail = f"{detail} · UNVERIFIED (AI-extracted, awaiting pharmacist)".strip(" ·")
    return {
        "title": f"{verb} {r.get('drug_name') or 'medication'}",
        "detail": detail,
        "severity": "warn" if status == "discontinued" else "info",
        "badge": "MED",
    }


def _r_administration(r: dict) -> dict:
    given = (r.get("admin_status") or "") == "given"
    dose = " ".join(str(x) for x in (r.get("dose_given"), r.get("route_given")) if x)
    if given:
        return {
            "title": f"Gave {r.get('drug_name') or 'medication'}",
            "detail": dose,
            "severity": "ok",
            "badge": "eMAR",
        }
    reason = r.get("not_given_reason") or "no reason recorded"
    return {
        "title": f"NOT given: {r.get('drug_name') or 'medication'}",
        "detail": f"Reason: {reason}",
        "severity": "warn",
        "badge": "eMAR",
    }


def _r_order(r: dict) -> dict:
    priority = (r.get("priority") or "routine").lower()
    what = r.get("description") or r.get("order_type") or "order"
    # The status the order was born in comes from its creation event, which is
    # not listed separately (see the order_event source): one row per order.
    created = (r.get("created_status") or "").lower()
    return {
        "title": f"Order placed ({created}): {what}" if created else f"Order placed: {what}",
        "detail": " · ".join(
            x for x in ((r.get("order_type") or "").upper(), priority.upper()) if x
        ),
        "severity": "warn" if priority in ("stat", "urgent") else "info",
        "badge": "ORDER",
    }


def _r_order_event(r: dict) -> dict:
    new_status = (r.get("new_status") or "").lower()
    prior = (r.get("prior_status") or "").lower()
    detail = r.get("description") or ""
    if r.get("reason"):
        detail = f"{detail} · {r['reason']}" if detail else str(r["reason"])
    # No prior status is the creation event. It used to print an em dash where
    # the prior status goes ("Order <dash> → draft"), standing in for a status
    # that never existed.
    title = (
        f"Order {prior} → {new_status}"
        if prior
        else f"Order placed ({new_status})"
        if new_status
        else "Order placed"
    )
    return {
        "title": title,
        "detail": _clip(detail),
        "severity": "warn" if new_status == "cancelled" else "info",
        "badge": "ORDER",
    }


def _r_observation(r: dict) -> dict:
    # The lab's own text when it sent any, else the number: the one display
    # rule every lab reader shares (lab_observations.display_value). A row
    # with neither (an HL7 OBX with an empty OBX-5: a pending result or a
    # comment-only line) prints no value at all, never the word None, which
    # on a culture would read like a result.
    value = lab_observations.display_value(r)
    if value is None:
        value = ""
    unit = r.get("unit") or ""
    flag = (r.get("abnormal_flag") or "").strip().upper()
    detail = f"{value} {unit}".strip()
    if r.get("reference_range"):
        detail = f"{detail} (ref {r['reference_range']})"
    if flag:
        detail = f"{detail} · {flag}"
    # H/L/A/HH/LL are the HL7 abnormal flags the ingestion parser writes. The
    # critical ones (HH/LL/AA) are the ones that must not read as routine.
    severity = "info"
    if flag in ("HH", "LL", "AA", "CRITICAL", "PANIC"):
        severity = "critical"
    elif flag in ("H", "L", "A", "ABNORMAL"):
        severity = "warn"
    return {
        "title": r.get("observation_name") or "Result",
        "detail": detail,
        "severity": severity,
        "badge": (r.get("panel_name") or "RESULT")[:18].upper(),
    }


def _r_bundle(r: dict) -> dict:
    # Scoring switched off (ORB_SCORING_ENABLED, the Orb Ward Record): a
    # bundle Orb opened on a score keeps its line, without the stored NEWS2
    # criterion that opened it. A reason a clinician typed is kept.
    from services.vigil_service import scoring_enabled

    # The automatic starter always signs as 'vigil_auto' (sepsis_bundle_service).
    auto = r.get("triggered_by") == "vigil_auto"
    reason = "" if auto and not scoring_enabled() else trigger_reason_text(r.get("trigger_reason"))
    return {
        "title": f"{bundle_label(r.get('bundle_type'))} bundle started",
        "detail": reason or "",
        "severity": "critical",
        "badge": "BUNDLE",
    }


def _r_problem(r: dict) -> dict:
    code = r.get("icd10_code") or r.get("snomed_code")
    return {
        "title": f"Problem added: {r.get('problem') or 'problem'}",
        "detail": f"Code {code}" if code else "",
        "severity": "info",
        "badge": "PROBLEM",
    }


def _r_allergy(r: dict) -> dict:
    sev = (r.get("allergy_severity") or "unknown").lower()
    bits = [b for b in (r.get("reaction"), f"severity {sev}") if b]
    return {
        "title": f"Allergy recorded: {r.get('substance') or 'substance'}",
        "detail": " · ".join(bits),
        "severity": "critical" if sev == "severe" else "warn",
        "badge": "ALLERGY",
    }


def _r_discharge(r: dict) -> dict:
    return {
        "title": "Discharged",
        "detail": _clip(r.get("summary"), 160),
        "severity": "ok",
        "badge": "DISCHARGE",
    }


_OPD_VISIT_STATUS_WORDS = {
    "waiting": "waiting",
    "in_consultation": "in consultation",
    "completed": "seen",
}


def _r_opd_visit(r: dict) -> dict:
    """An outpatient visit (opd-10). Which clinic, which doctor, and how it
    stands; the desk's free-text complaint is not repeated here."""
    bits = [
        b
        for b in (
            r.get("doctor_name"),
            _OPD_VISIT_STATUS_WORDS.get((r.get("visit_status") or "").lower()),
        )
        if b
    ]
    return {
        "title": f"Outpatient visit: {r.get('department') or 'clinic'}",
        "detail": " · ".join(bits),
        "severity": "info",
        "badge": "OPD",
    }


def _r_opd_consultation(r: dict) -> dict:
    """A signed outpatient consultation note (opd-10): its diagnoses and plan.
    The vitals typed into it are not shown on the rail, and nothing about
    them is scored or banded."""
    diagnoses: list[str] = []
    raw = r.get("diagnoses_json")
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                diagnoses = [
                    str(d.get("text")) for d in loaded if isinstance(d, dict) and d.get("text")
                ]
        except (ValueError, TypeError):
            diagnoses = []
    detail = "; ".join(diagnoses)
    if r.get("plan"):
        detail = f"{detail} · Plan: {r['plan']}" if detail else f"Plan: {r['plan']}"
    return {
        "title": f"Outpatient note signed: {r.get('department') or 'clinic'}",
        "detail": _clip(detail),
        "severity": "info",
        "badge": "OPD",
    }


SOURCES: tuple[_Source, ...] = (
    _Source(
        kind="admission",
        from_sql="patients p",
        patient_col="p.id",
        ts_col="p.admission_date",
        id_col="p.id",
        select_cols=(
            "p.ward AS ward",
            "p.bed AS bed",
            "p.diagnosis AS diagnosis",
            "NULL AS actor_name",
        ),
        render=_r_admission,
    ),
    _Source(
        kind="note",
        from_sql="clinical_documents cd LEFT JOIN users u ON u.id = cd.author_id",
        patient_col="cd.patient_id",
        ts_col="cd.created_at",
        id_col="cd.id",
        select_cols=(
            "cd.doc_type AS doc_type",
            "cd.title AS doc_title",
            "cd.content AS content",
            "u.name AS actor_name",
        ),
        render=_r_note,
    ),
    _Source(
        kind="scribe",
        from_sql="scribe_notes sn LEFT JOIN users u ON u.id = sn.author_id",
        patient_col="sn.patient_id",
        ts_col="sn.created_at",
        id_col="sn.id",
        select_cols=(
            "sn.note_format AS note_format",
            "sn.structured_note AS structured_note",
            "sn.raw_transcript AS raw_transcript",
            "u.name AS actor_name",
        ),
        render=_r_scribe,
    ),
    _Source(
        kind="imaging",
        from_sql="lens_analyses la LEFT JOIN users u ON u.id = la.author_id",
        patient_col="la.patient_id",
        ts_col="la.created_at",
        id_col="la.id",
        select_cols=(
            "la.image_type AS image_type",
            "la.analysis AS analysis",
            "u.name AS actor_name",
        ),
        render=_r_imaging,
    ),
    _ScoredSource(
        kind="alert",
        from_sql="risk_scores rs",
        patient_col="rs.patient_id",
        ts_col="rs.timestamp",
        id_col="rs.id",
        select_cols=("rs.drivers AS drivers", "NULL AS actor_name"),
        where_sql="rs.level = 'critical'",
        render=_r_alert,
    ),
    _DrugChartSource(
        kind="medication",
        from_sql="medications m LEFT JOIN users u ON u.id = m.prescribed_by",
        patient_col="m.patient_id",
        ts_col="m.started_at",
        id_col="m.id",
        select_cols=(
            "m.drug_name AS drug_name",
            "m.dosage AS dosage",
            "m.route AS route",
            "m.frequency AS frequency",
            "m.status AS med_status",
            "m.source AS med_source",
            "m.verified_by AS verified_by",
            "u.name AS actor_name",
        ),
        render=_r_medication,
    ),
    _DrugChartSource(
        kind="administration",
        from_sql=(
            "medication_administrations ma "
            "LEFT JOIN medications m ON m.id = ma.medication_id "
            "LEFT JOIN users u ON u.id = ma.administered_by"
        ),
        patient_col="ma.patient_id",
        ts_col="ma.administered_at",
        id_col="ma.id",
        select_cols=(
            "ma.status AS admin_status",
            "ma.dose_given AS dose_given",
            "ma.route_given AS route_given",
            "ma.not_given_reason AS not_given_reason",
            "m.drug_name AS drug_name",
            "u.name AS actor_name",
        ),
        render=_r_administration,
    ),
    _OrderSource(
        kind="order",
        from_sql="orders o LEFT JOIN users u ON u.id = o.ordered_by",
        patient_col="o.patient_id",
        ts_col="o.created_at",
        id_col="o.id",
        select_cols=(
            "o.order_type AS order_type",
            "o.description AS description",
            "o.priority AS priority",
            "u.name AS actor_name",
            "(SELECT oe0.new_status FROM order_events oe0 "
            "WHERE oe0.order_id = o.id AND oe0.prior_status IS NULL "
            "ORDER BY oe0.id LIMIT 1) AS created_status",
        ),
        render=_r_order,
    ),
    _OrderSource(
        kind="order_event",
        from_sql=(
            "order_events oe JOIN orders o ON o.id = oe.order_id "
            "LEFT JOIN users u ON u.id = oe.actor_id"
        ),
        patient_col="o.patient_id",
        ts_col="oe.created_at",
        id_col="oe.id",
        select_cols=(
            "oe.prior_status AS prior_status",
            "oe.new_status AS new_status",
            "oe.reason AS reason",
            "o.description AS description",
            "u.name AS actor_name",
        ),
        # The creation event is the orders row above, said again at the same
        # instant. Listing both put every order on the chart, and in the
        # round's "since" count, twice. Status changes are what this adds.
        where_sql="oe.prior_status IS NOT NULL",
        render=_r_order_event,
    ),
    _Source(
        kind="observation",
        # Lab results come through the one reader every lab lane shares
        # (services/lab_observations.py): a voided or superseded result is not
        # on the rail or in the "new" count, and equal instants go to the
        # later write. Simulated results stay, as on the chart's lab card.
        from_sql=lab_observations.from_sql("ob"),
        patient_col="ob.patient_id",
        ts_col="COALESCE(ob.observed_at, ob.recorded_at, ob.created_at)",
        id_col="ob.id",
        select_cols=(
            "ob.observation_name AS observation_name",
            "ob.value_text AS value_text",
            "ob.value_numeric AS value_numeric",
            "ob.unit AS unit",
            "ob.reference_range AS reference_range",
            "ob.abnormal_flag AS abnormal_flag",
            "ob.panel_name AS panel_name",
            "NULL AS actor_name",
        ),
        where_sql=lab_observations.live_observations_sql("ob"),
        order_sql=lab_observations.order_by_sql("ob"),
        render=_r_observation,
    ),
    _Source(
        kind="bundle",
        from_sql="care_bundles cb",
        patient_col="cb.patient_id",
        ts_col="cb.triggered_at",
        id_col="cb.id",
        select_cols=(
            "cb.bundle_type AS bundle_type",
            "cb.trigger_reason AS trigger_reason",
            "cb.triggered_by AS triggered_by",
            "NULL AS actor_name",
        ),
        render=_r_bundle,
    ),
    _Source(
        kind="problem",
        from_sql="problem_list pl LEFT JOIN users u ON u.id = pl.recorded_by",
        patient_col="pl.patient_id",
        ts_col="pl.recorded_at",
        id_col="pl.id",
        select_cols=(
            "pl.problem AS problem",
            "pl.icd10_code AS icd10_code",
            "pl.snomed_code AS snomed_code",
            "u.name AS actor_name",
        ),
        render=_r_problem,
    ),
    _Source(
        kind="allergy",
        from_sql="patient_allergies pa LEFT JOIN users u ON u.id = pa.recorded_by",
        patient_col="pa.patient_id",
        ts_col="pa.recorded_at",
        id_col="pa.id",
        select_cols=(
            "pa.substance AS substance",
            "pa.reaction AS reaction",
            "pa.severity AS allergy_severity",
            "u.name AS actor_name",
        ),
        render=_r_allergy,
    ),
    _Source(
        kind="discharge",
        from_sql="discharge_summaries ds LEFT JOIN users u ON u.id = ds.author_id",
        patient_col="ds.patient_id",
        ts_col="ds.discharged_at",
        id_col="ds.id",
        select_cols=("ds.summary AS summary", "u.name AS actor_name"),
        render=_r_discharge,
    ),
    # ── Outpatients (opd-10), only while the 'opd' feature is on ──
    # The visit: registered at the desk, cancelled visits left out.
    _Source(
        kind="opd_visit",
        from_sql="opd_visits ov LEFT JOIN users u ON u.id = ov.registered_by",
        patient_col="ov.patient_id",
        ts_col="ov.registered_at",
        id_col="ov.id",
        select_cols=(
            "ov.department AS department",
            "ov.doctor_name AS doctor_name",
            "ov.status AS visit_status",
            "u.name AS actor_name",
        ),
        where_sql="ov.status != 'cancelled'",
        render=_r_opd_visit,
        feature="opd",
    ),
    # The signed consultation note: diagnoses and plan, never its vitals.
    # A draft is not a record yet and is not on the rail.
    _Source(
        kind="opd_consultation",
        from_sql="opd_consultations oc LEFT JOIN users u ON u.id = oc.signed_by",
        patient_col="oc.patient_id",
        ts_col="oc.signed_at",
        id_col="oc.id",
        select_cols=(
            "oc.department AS department",
            "oc.diagnoses_json AS diagnoses_json",
            "oc.plan AS plan",
            "u.name AS actor_name",
        ),
        where_sql="oc.status = 'signed'",
        render=_r_opd_consultation,
        feature="opd",
    ),
    # The nurses' shift notes (migration 88), the per-shift nursing record.
    _NursingNoteSource(
        kind="nursing_note",
        from_sql="nursing_notes nn LEFT JOIN users u ON u.id = nn.author_id",
        patient_col="nn.patient_id",
        ts_col="nn.written_at",
        id_col="nn.id",
        select_cols=(
            "nn.shift_label AS shift_label",
            "nn.content AS content",
            "u.name AS actor_name",
        ),
        render=_r_nursing_note,
    ),
)

KINDS: tuple[str, ...] = tuple(s.kind for s in SOURCES)


# ═════════════════════════════════════════════════════════════════════════════
#  Queries
# ═════════════════════════════════════════════════════════════════════════════


def _detail_sql(src: _Source, limit: int) -> str:
    cols = ", ".join(src.select_cols) if src.select_cols else "NULL AS actor_name"
    where = f"{src.patient_col} = ?"
    if src.live_where_sql:
        where = f"{where} AND {src.live_where_sql}"
    order = src.order_sql or f"{_norm(src.ts_col)} DESC"
    return (
        f"SELECT {src.id_col} AS row_id, {src.ts_col} AS ts, {cols} "
        f"FROM {src.from_sql} WHERE {where} "
        f"ORDER BY {order} LIMIT {int(limit)}"
    )


def available_sources(db, sources: tuple[_Source, ...] | None = None) -> tuple[_Source, ...]:
    """The declared streams whose base table actually exists in THIS database.

    One ``sqlite_master`` read per call, and the result is then used for BOTH
    the detail queries and the count. That is the point: on a minimal or
    partially-migrated deployment the rail and the badge must agree about which
    streams exist, or the badge counts rows the rail can never show.
    """
    names = {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    return tuple(
        s
        for s in (sources if sources is not None else SOURCES)
        if s.base_table in names and _feature_on(s.feature)
    )


def _feature_on(feature: str) -> bool:
    """A stream with no feature is always on; otherwise ask config at call
    time (the outpatient streams follow the 'opd' feature)."""
    if not feature:
        return True
    import config

    return config.feature_enabled(feature)


def _count_union_sql(patient_filter: str, sources: tuple[_Source, ...] | None = None) -> str:
    """UNION ALL of (patient_id, normalised_ts) across every countable stream.

    `patient_filter` is spliced in as a WHERE fragment — it is built by this
    module from a bound-parameter expression, never from request text.
    """
    parts = []
    for src in sources if sources is not None else SOURCES:
        if not src.countable:
            continue
        where = patient_filter.format(pcol=src.patient_col)
        if src.live_where_sql:
            where = f"({where}) AND {src.live_where_sql}"
        parts.append(
            f"SELECT {src.patient_col} AS pid, {_norm(src.ts_col)} AS nts "
            f"FROM {src.from_sql} WHERE {where}"
        )
    return " UNION ALL ".join(parts)


def get_review_mark(db, user_id: str, patient_id: str) -> str | None:
    """This clinician's watermark for this patient, or None if never reviewed."""
    row = db.execute(
        "SELECT last_reviewed_at FROM patient_review_marks WHERE user_id = ? AND patient_id = ?",
        (user_id, patient_id),
    ).fetchone()
    return row["last_reviewed_at"] if row else None


def count_new_since(
    db, patient_id: str, since_iso: str | None, sources: tuple[_Source, ...] | None = None
) -> int:
    """How many chart events landed after `since_iso`.

    Counted by the DATABASE over every row, not by counting the truncated list
    the UI receives — "6 new" above a list capped at 150 must still be true.
    """
    if not since_iso:
        return 0
    union = _count_union_sql("{pcol} = ?", sources)
    if not union:
        return 0
    params = tuple([patient_id] * union.count("?"))
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM ({union}) WHERE nts IS NOT NULL AND nts > ?",
        params + (_sql_norm_value(since_iso),),
    ).fetchone()
    return int(row["n"] or 0)


def _sql_norm_value(iso: str) -> str:
    """Render a watermark the same way `_TS_NORM` renders a column.

    Done in Python rather than by nesting another strftime() around a bound
    parameter so that an unparseable watermark raises here — visibly — instead
    of quietly becoming NULL and making every comparison false, which would
    report "0 new" forever.
    """
    dt = _normalize_ts(iso)
    if dt is None:
        raise ValueError(f"unparseable review watermark: {iso!r}")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def build_chart_timeline(
    patient_id: str,
    user_id: str | None = None,
    limit: int = 150,
) -> dict:
    """Everything that happened to this patient, newest first.

    NEWEST FIRST is deliberate. The question this screen exists to answer is
    "what has happened since I last looked", and the answer is at the top of a
    reverse-chronological list. A clinician reading the whole admission forward
    scrolls to the bottom, which is the rarer trip.

    Returns a dict with the events, this user's watermark, and the exact count
    of events after it. Contains NO scoping of its own — the caller is
    responsible for having established that this user may read this patient
    (routes do it with require_patient_access, which also writes the PHI-read
    audit row).
    """
    limit = max(1, min(int(limit or 150), 500))
    # Fetch a little more per stream than we will show, so that a chart whose
    # recent activity is dominated by one stream (a lab panel dumping 40 rows)
    # cannot push every other stream off the rail.
    per_source = min(limit, 200)

    events: list[dict] = []
    with db_session() as db:
        # Resolve the streams ONCE and use the same list for the rows and the
        # count, so the rail and the badge can never disagree about which
        # streams this database actually has.
        sources = available_sources(db)
        usable: list[_Source] = []
        for src in sources:
            try:
                rows = db.execute(_detail_sql(src, per_source), (patient_id,)).fetchall()
            except Exception:  # noqa: BLE001 — the table exists but a column on
                # it does not (a half-applied migration). Lose ONE stream, not
                # the chart; and drop it from the count too, on the next line.
                continue
            usable.append(src)
            for raw in rows:
                row = dict(raw)
                dt = _normalize_ts(row.get("ts"))
                if dt is None:
                    continue
                rendered = src.render(row)
                events.append(
                    {
                        # Stable across refetches: the UI keys on it and the "new"
                        # divider is drawn between two of them.
                        "id": f"{src.kind}:{row.get('row_id')}",
                        "kind": src.kind,
                        "timestamp": _iso(dt),
                        "_dt": dt,
                        "title": rendered.get("title") or src.kind,
                        "detail": rendered.get("detail") or "",
                        "severity": rendered.get("severity") or "info",
                        "badge": rendered.get("badge") or src.kind.upper(),
                        "actor": row.get("actor_name") or None,
                    }
                )

        mark = get_review_mark(db, user_id, patient_id) if user_id else None
        new_count = count_new_since(db, patient_id, mark, tuple(usable)) if mark else 0

    events.sort(key=lambda e: e["_dt"], reverse=True)
    total = len(events)
    events = events[:limit]

    mark_dt = _normalize_ts(mark) if mark else None
    for e in events:
        e["is_new"] = bool(mark_dt and e["_dt"] > mark_dt)
        del e["_dt"]

    return {
        "patient_id": patient_id,
        "events": events,
        "count": len(events),
        # True when the rail is showing less than the chart holds, so the UI can
        # say so rather than implying the admission started 150 events ago.
        "truncated": total > len(events),
        "last_reviewed_at": mark,
        "new_count": new_count,
    }


def read_review_state(user_id: str | None, patient_id: str) -> dict:
    """This clinician's watermark for this chart, and how much has landed since.

    Split out from build_chart_timeline so a caller that already has the events
    — the Patient Story route — can add the "since you last looked" layer
    without assembling the whole rail a second time. Read-only: it never moves
    the mark, because a mark that advanced merely because a screen rendered
    would answer "has this been drawn" rather than "has this been read".
    """
    if not user_id:
        return {"last_reviewed_at": None, "new_count": 0}
    with db_session() as db:
        mark = get_review_mark(db, user_id, patient_id)
        if not mark:
            return {"last_reviewed_at": None, "new_count": 0}
        return {
            "last_reviewed_at": mark,
            "new_count": count_new_since(db, patient_id, mark, available_sources(db)),
        }


def mark_reviewed(user_id: str, patient_id: str, as_of: str | None = None) -> dict:
    """Move this clinician's watermark on this patient forward.

    NEVER BACKWARDS. A stale tab replaying an old "reviewed" (the offline queue
    in client.ts does exactly this) must not un-review a chart the clinician has
    since read — that would resurrect a "12 new" badge over events they have
    already acted on, which is worse than no badge at all. So the write keeps
    the LATER of the stored and incoming values.

    Returns both the previous and the new watermark: the caller writes the
    previous one into the audit row, which is what makes "reviewed up to X"
    reconstructable afterwards.
    """
    if not user_id or not patient_id:
        raise ValueError("mark_reviewed requires both a user and a patient")
    now = _normalize_ts(as_of) if as_of else datetime.now(UTC)
    if now is None:
        raise ValueError(f"unparseable as_of: {as_of!r}")
    stamp = _iso(now)

    with db_session() as db:
        previous = get_review_mark(db, user_id, patient_id)
        prev_dt = _normalize_ts(previous) if previous else None
        if prev_dt is not None and prev_dt >= now:
            # Already reviewed at or after this instant — the row stands.
            return {"previous": previous, "last_reviewed_at": previous, "advanced": False}
        db.execute(
            """INSERT INTO patient_review_marks (user_id, patient_id, last_reviewed_at, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id, patient_id)
               DO UPDATE SET last_reviewed_at = excluded.last_reviewed_at,
                             updated_at = CURRENT_TIMESTAMP""",
            (user_id, patient_id, stamp),
        )
    return {"previous": previous, "last_reviewed_at": stamp, "advanced": True}


def unseen_counts_for_scope(user_id: str, dept: str | None) -> dict[str, int]:
    """{patient_id: new-event count} for every ADMITTED patient in `dept`.

    ONE query for the whole board, not one per patient — the ward board renders
    30 rows and this must not turn into 30 round trips against an encrypted
    SQLite file. Patients this clinician has never reviewed are absent from the
    result entirely (see migration 17 on why "never reviewed" is not "all new").

    `dept` follows the permissions.department_scope convention: None means a
    genuinely unscoped governance account, and the department predicate lives in
    the QUERY rather than in a post-filter so it cannot be bypassed.
    """
    if not user_id:
        return {}

    # Scope is derived from the caller's own review marks joined to the admitted
    # census, so the answer can only ever mention patients this account has
    # already opened AND that are inside its department.
    dept_clause = "AND a.department = ?" if dept else ""
    scope_sql = (
        "SELECT prm.patient_id AS pid, prm.last_reviewed_at AS mark "
        "FROM patient_review_marks prm "
        "JOIN patients p ON p.id = prm.patient_id AND p.status = 'admitted' "
        "JOIN admissions a ON a.patient_id = p.id AND a.status = 'active' "
        f"WHERE prm.user_id = ? {dept_clause}"
    )

    with db_session() as db:
        try:
            marks = db.execute(scope_sql, (user_id, dept) if dept else (user_id,)).fetchall()
        except Exception:  # noqa: BLE001 — table absent on a pre-migration DB.
            return {}
        if not marks:
            return {}

        pids = [m["pid"] for m in marks]
        placeholders = ",".join("?" for _ in pids)
        union = _count_union_sql(f"{{pcol}} IN ({placeholders})", available_sources(db))
        if not union:
            return {}
        # Every stream carries its own copy of the id list, in stream order.
        stream_count = union.count("UNION ALL") + 1
        params = pids * stream_count

        rows = db.execute(
            f"SELECT pid, nts FROM ({union}) WHERE nts IS NOT NULL", params
        ).fetchall()

    thresholds: dict[str, str] = {}
    for m in marks:
        try:
            thresholds[m["pid"]] = _sql_norm_value(m["mark"])
        except ValueError:
            continue

    counts: dict[str, int] = {}
    for r in rows:
        threshold = thresholds.get(r["pid"])
        if threshold and r["nts"] > threshold:
            counts[r["pid"]] = counts.get(r["pid"], 0) + 1
    return counts
