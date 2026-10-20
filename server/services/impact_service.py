"""Orb — Impact / ROI model (the money slide).

Converts Orb's *actually-recorded* clinical activity — Scribe notes authored,
Vigil (NEWS2) high-risk escalations, Sepsis-Six bundles started, pharmacist
verifications of AI-extracted medications, drug-safety reviews, and discharge
reconciliations — into defensible rupee figures and prevented-harm estimates.

Design principles (this is an investor / CFO diligence artifact):

  * Every rupee traces to a COUNTED event in the database multiplied by a
    named, cited assumption. There are no magic numbers — each metric carries
    the human-readable assumption string that produced it.
  * Assumptions live in one module-level ``DEFAULT_ASSUMPTIONS`` dict and are
    fully overridable, so a skeptical CFO can re-run the model with their own
    unit economics.
  * The model degrades gracefully: on an empty database (or a DB missing an
    optional column) every metric returns 0 with a clear "no data yet" flavour
    rather than raising.

The service is pure DB reads (no writes, no side effects).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from database import db_session

from services.stay_times import summary_less_ends_sql

logger = logging.getLogger("orb.impact")

# ── Currency ──────────────────────────────────────────────────────────────────
# Orb's first deployments are Indian private hospitals, so the model is
# denominated in rupees and formatted with Indian digit grouping (lakh/crore:
# ₹12,34,567, not ₹1,234,567). Env-overridable for a site in another market —
# note that changing the CODE does not convert the ASSUMPTION VALUES below,
# which are rupee-denominated; a non-INR site must supply its own assumptions.
CURRENCY_CODE = os.getenv("ORB_CURRENCY_CODE", "INR")
CURRENCY_LOCALE = os.getenv("ORB_CURRENCY_LOCALE", "en-IN")


# ──────────────────────────────────────────────────────────────────────────────
# Tunable assumptions. Each entry is (default_value, unit, citation). The
# citation is what makes a money figure survive diligence — it says exactly
# where the number comes from and that it is conservative.
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_ASSUMPTIONS: dict[str, dict] = {
    "period_days": {
        "value": 30,
        "unit": "days",
        "citation": "Reporting window. Money totals are reported per-month; a "
        "30-day window means totals are already monthly.",
    },
    "doc_minutes_saved_per_note": {
        "value": 7.0,
        "unit": "minutes/note",
        "citation": "Ambient AI-scribe time saving per encounter note. Conservative "
        "vs. published ambient-documentation studies (e.g. The Permanente "
        "Medical Group / Nuance DAX 2024 reporting ~1 hour/clinician/day).",
    },
    "clinician_hourly_cost_inr": {
        "value": 1500.0,
        "unit": "₹/hour",
        "citation": "SITE PLACEHOLDER: Indian private-hospital blended clinician cost. "
        "Derived, not measured: a consultant at ₹25-40 lakh/year fully loaded "
        "over ~2,000 clinical hours is ₹1,250-2,000/hour; blending in residents "
        "and RMOs pulls the average down. Replace with this hospital's payroll.",
    },
    "icu_day_cost_inr": {
        "value": 15000.0,
        "unit": "₹/ICU-day",
        "citation": "SITE PLACEHOLDER: incremental cost of one private-hospital ICU day in "
        "India. Deliberately at the conservative end of the private range. For a "
        "published floor, PM-JAY (Ayushman Bharat) HBP critical-care packages are "
        "several times lower; public-scheme tariffs are not private cost. Replace "
        "with this hospital's own ICU costing.",
    },
    "deterioration_icu_avoidance_rate": {
        "value": 0.15,
        "unit": "fraction",
        "citation": "Fraction of NEWS2 escalations / Sepsis Six bundles "
        "that avert an ICU day. Conservative estimate from rapid-response-team "
        "and Surviving-Sepsis 1-hour-bundle outcome literature.",
    },
    "ade_cost_inr": {
        "value": 40000.0,
        "unit": "₹/ADE",
        "citation": "SITE PLACEHOLDER: incremental cost of one preventable adverse drug "
        "event in an Indian private hospital (extra bed-days plus treatment). "
        "The underlying effect is well established (Bates et al. JAMA 1997 and "
        "the AHRQ literature); the RUPEE figure is our estimate, not a published "
        "Indian cost study. Replace with this hospital's own incident costing.",
    },
    "ade_avoidance_rate_per_alert": {
        "value": 0.03,
        "unit": "fraction",
        "citation": "Fraction of interaction/allergy alerts + pharmacist AI-med verifications "
        "that represent a genuinely averted ADE. Alert-fatigue-adjusted "
        "(~1 in 33 alerts clinically actionable).",
    },
    "ward_bed_day_cost_inr": {
        "value": 5000.0,
        "unit": "₹/bed-day",
        "citation": "SITE PLACEHOLDER: cost (not tariff) of one general-ward bed-day in an "
        "Indian private hospital, used for length-of-stay / throughput savings. "
        "Note this is deliberately BELOW the ₹10,000-15,000 ARPOB band Orb "
        "qualifies hospitals on: ARPOB is revenue per occupied bed-day, and using "
        "revenue here would overstate savings. Replace with this hospital's own "
        "cost-per-bed-day.",
    },
    "los_reduction_days_per_discharge": {
        "value": 0.25,
        "unit": "days/discharge",
        "citation": "Length-of-stay reduction per structured discharge + medication "
        "reconciliation, from faster and more complete discharge documentation. "
        "Deliberately conservative (quarter-day).",
    },
}


def _resolve_assumptions(overrides: dict | None) -> dict:
    """Merge caller overrides onto the defaults, returning a flat {key: value}
    map. Unknown keys are ignored; non-numeric/negative values fall back to the
    default so a bad query param can never produce a negative or nonsense amount."""
    resolved: dict[str, float] = {k: v["value"] for k, v in DEFAULT_ASSUMPTIONS.items()}
    if overrides:
        for key, raw in overrides.items():
            if key not in DEFAULT_ASSUMPTIONS or raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val < 0:
                continue
            # A rate is a fraction of events. 15 (meant as 15%) would multiply
            # every ICU-day figure by a hundred on the CFO's own screen.
            if DEFAULT_ASSUMPTIONS[key]["unit"] == "fraction" and val > 1:
                continue
            resolved[key] = val
    # period_days is an integer count; keep it sane (>= 1).
    try:
        resolved["period_days"] = max(1, int(resolved["period_days"]))
    except (TypeError, ValueError):
        resolved["period_days"] = int(DEFAULT_ASSUMPTIONS["period_days"]["value"])
    return resolved


def _count(db, sql: str, params: tuple = ()) -> int:
    """Run a COUNT/scalar query, returning an int and swallowing any error
    (missing table/column on a partial DB) as 0 so the model never crashes."""
    try:
        row = db.execute(sql, params).fetchone()
        if not row or row[0] is None:
            return 0
        return int(row[0])
    except Exception as e:  # noqa: BLE001 - graceful degradation is the point
        logger.debug("impact count query failed (degrading to 0): %s", e)
        return 0


def _scalar_float(db, sql: str, params: tuple = ()) -> float | None:
    try:
        row = db.execute(sql, params).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])
    except Exception as e:  # noqa: BLE001
        logger.debug("impact scalar query failed (degrading to None): %s", e)
        return None


def _money(x: float) -> float:
    """Round to 2dp, never below zero."""
    return round(max(0.0, x), 2)


_CURRENCY_SYMBOLS = {"INR": "₹"}


def _group_digits(n: int) -> str:
    """Whole-number digit grouping in the configured locale: 12,34,567 for
    en-IN (lakh/crore), 1,234,567 otherwise. Matches what the dashboard's
    Intl formatter prints, so the assumption text and the figure agree."""
    if not CURRENCY_LOCALE.lower().endswith("-in"):
        return f"{n:,}"
    digits = str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return ("-" if n < 0 else "") + digits


def _fmt_money(x: float) -> str:
    """A rupee figure as a person writes it (₹15,000), not 'Rs 15,000'."""
    symbol = _CURRENCY_SYMBOLS.get(CURRENCY_CODE.upper())
    amount = _group_digits(int(round(x)))
    return f"{symbol}{amount}" if symbol else f"{CURRENCY_CODE} {amount}"


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


# The vitals row behind a risk score or a bundle: the patient's latest reading
# at or before that moment. Two seconds of slack because a risk_scores row
# stamped with CURRENT_TIMESTAMP (whole seconds) can sort just before the
# microsecond-stamped reading it was scored from. NULL when the patient has no
# reading at all, which then counts as neither real nor simulated.
_READING_SOURCE_SQL = (
    "(SELECT v.source FROM vitals v "
    " WHERE v.patient_id = {alias}.patient_id "
    "   AND julianday(v.timestamp) <= julianday({alias}.{col}) + 2.0 / 86400.0 "
    " ORDER BY julianday(v.timestamp) DESC, v.id DESC LIMIT 1)"
)


def _count_by_reading_source(
    db, alias: str, col: str, from_where: str, params: tuple
) -> tuple[int, int, int]:
    """(real, simulated, unattributed) rows of ``from_where`` in ONE pass.

    One query rather than three because the source lookup is a correlated
    subquery per row; running it three times tripled the cost on a busy ward.
    Degrades to zeros like ``_count``.
    """
    src = _READING_SOURCE_SQL.format(alias=alias, col=col)
    sql = (
        "SELECT SUM(CASE WHEN src <> 'simulated' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN src = 'simulated' THEN 1 ELSE 0 END), "
        "       SUM(CASE WHEN src IS NULL THEN 1 ELSE 0 END) "
        f"FROM (SELECT {src} AS src {from_where})"
    )
    try:
        row = db.execute(sql, params).fetchone()
    except Exception as e:  # noqa: BLE001 - graceful degradation is the point
        logger.debug("impact source split failed (degrading to 0): %s", e)
        return 0, 0, 0
    if not row:
        return 0, 0, 0
    return tuple(int(x or 0) for x in row[:3])  # type: ignore[return-value]


def compute_impact(assumptions: dict | None = None) -> dict:
    """Compute the ROI / outcomes model over the reporting window.

    Args:
        assumptions: optional {key: value} overrides for any key in
            ``DEFAULT_ASSUMPTIONS``. Anything omitted uses the cited default.

    Returns a structured dict:
        {generated_at, period, metrics:[...], totals:{...}, assumptions:{...},
         has_data}
    """
    a = _resolve_assumptions(assumptions)
    period_days = int(a["period_days"])
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=period_days)).isoformat()

    # We filter on julianday(col) so both 'YYYY-MM-DD HH:MM:SS' (CURRENT_TIMESTAMP)
    # and full ISO-8601 ('...T...+00:00') timestamps compare correctly, and any
    # unparseable/NULL timestamp is simply excluded rather than crashing.
    metrics: list[dict] = []
    total_evidence = 0

    with db_session() as db:
        # ── 1. Documentation time saved (Scribe) ────────────────────────────
        notes = _count(
            db,
            "SELECT COUNT(*) FROM scribe_notes WHERE julianday(created_at) >= julianday(?)",
            (cutoff,),
        )
        minutes_saved = notes * a["doc_minutes_saved_per_note"]
        doc_amount = _money((minutes_saved / 60.0) * a["clinician_hourly_cost_inr"])
        total_evidence += notes
        metrics.append(
            {
                "key": "documentation_time_saved",
                "label": "Documentation time saved",
                "value": round(minutes_saved, 0),
                "unit": "clinician-minutes",
                "amount": doc_amount,
                "evidence_count": notes,
                "assumption": (
                    f"{notes} Scribe {_plural(notes, 'note')} × "
                    f"{a['doc_minutes_saved_per_note']:g} min saved per note × "
                    f"{_fmt_money(a['clinician_hourly_cost_inr'])} per clinician-hour"
                ),
            }
        )

        # ── 2. Deterioration caught early (Vigil + Sepsis-Six) ───────────────
        # Only escalations behind a REAL reading count. The vitals simulator and
        # the demo seed write source='simulated' rows that score critical and
        # auto-open bundles; counting them turned a demo ward into rupee
        # "savings" on the investor screen. A score or bundle with no reading
        # at all cannot be attributed either way and is left out too: this
        # model under-claims rather than invents.
        news2_escalations, news2_simulated, news2_unattributed = _count_by_reading_source(
            db,
            "rs",
            "timestamp",
            "FROM risk_scores rs WHERE rs.level = 'critical' "
            "AND julianday(rs.timestamp) >= julianday(?)",
            (cutoff,),
        )
        bundles_started, bundles_simulated, bundles_unattributed = _count_by_reading_source(
            db,
            "cb",
            "triggered_at",
            "FROM care_bundles cb WHERE julianday(cb.triggered_at) >= julianday(?)",
            (cutoff,),
        )

        escalations = news2_escalations + bundles_started
        simulated_excluded = news2_simulated + bundles_simulated
        unattributed_excluded = news2_unattributed + bundles_unattributed
        icu_days_avoided = escalations * a["deterioration_icu_avoidance_rate"]
        deter_amount = _money(icu_days_avoided * a["icu_day_cost_inr"])
        total_evidence += escalations
        exclusions = []
        if simulated_excluded:
            exclusions.append(
                f"{simulated_excluded} driven by simulated vitals "
                f"{_plural(simulated_excluded, 'is', 'are')} left out"
            )
        if unattributed_excluded:
            exclusions.append(
                f"{unattributed_excluded} with no recorded reading "
                f"{_plural(unattributed_excluded, 'is', 'are')} left out"
            )
        metrics.append(
            {
                "key": "deterioration_caught_early",
                "label": "Deterioration caught early",
                "value": round(icu_days_avoided, 1),
                "unit": "ICU days avoided (est.)",
                "amount": deter_amount,
                "evidence_count": escalations,
                "excluded_simulated": simulated_excluded,
                "excluded_unattributed": unattributed_excluded,
                "assumption": (
                    f"{news2_escalations} NEWS2 critical "
                    f"{_plural(news2_escalations, 'escalation')} + {bundles_started} sepsis "
                    f"{_plural(bundles_started, 'bundle')} × "
                    f"{a['deterioration_icu_avoidance_rate']:g} ICU-day avoidance rate × "
                    f"{_fmt_money(a['icu_day_cost_inr'])} per ICU day"
                    + ("; " + "; ".join(exclusions) if exclusions else "")
                ),
            }
        )

        # ── 3. Medication safety (Helix + Pharmacy) ─────────────────────────
        # AI/agentic-extracted meds that a pharmacist actually verified — a real
        # human-in-the-loop safety intervention on an AI suggestion.
        ai_med_verifications = _count(
            db,
            "SELECT COUNT(*) FROM medications "
            "WHERE source IN ('llm_extracted', 'agentic') AND verified_by IS NOT NULL "
            "AND julianday(COALESCE(verified_at, started_at)) >= julianday(?)",
            (cutoff,),
        )
        # Drug-safety reviews that fired at order/verify time (interaction + allergy gate).
        safety_reviews = _count(
            db,
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE action_type IN ('medication_verify', 'medication_reject') "
            "AND julianday(timestamp) >= julianday(?)",
            (cutoff,),
        )
        med_events = ai_med_verifications + safety_reviews
        ades_avoided = med_events * a["ade_avoidance_rate_per_alert"]
        med_amount = _money(ades_avoided * a["ade_cost_inr"])
        total_evidence += med_events
        metrics.append(
            {
                "key": "medication_safety",
                "label": "Medication safety",
                "value": round(ades_avoided, 1),
                "unit": "ADEs avoided (est.)",
                "amount": med_amount,
                "evidence_count": med_events,
                "assumption": (
                    f"{ai_med_verifications} pharmacist "
                    f"{_plural(ai_med_verifications, 'verification')} of AI-extracted medicines + "
                    f"{safety_reviews} drug-safety {_plural(safety_reviews, 'review')} × "
                    f"{a['ade_avoidance_rate_per_alert']:g} avoidance rate × "
                    f"{_fmt_money(a['ade_cost_inr'])} per adverse drug event"
                ),
            }
        )

        # ── 4. Throughput (LOS + discharge reconciliation) ──────────────────
        # Every discharge event in the period: the summaries, plus the ends
        # the beds and admissions module recorded with no summary (a death, a
        # LAMA, an absconded closure; none exist with ORB_ADT off), which
        # only services/stay_times.py knows.
        discharge_recons = _count(
            db,
            "SELECT COUNT(*) FROM discharge_summaries "
            "WHERE julianday(discharged_at) >= julianday(?)",
            (cutoff,),
        ) + _count(
            db,
            f"SELECT COUNT(*) FROM ({summary_less_ends_sql()}) e "
            "WHERE julianday(e.ended_at) >= julianday(?)",
            (cutoff,),
        )
        # Average length of stay from historical admissions (discharged only).
        avg_los = _scalar_float(
            db,
            "SELECT AVG(length_of_stay_days) FROM admission_history "
            "WHERE discharged = 1 AND length_of_stay_days IS NOT NULL",
        )
        los_days_saved = discharge_recons * a["los_reduction_days_per_discharge"]
        throughput_amount = _money(los_days_saved * a["ward_bed_day_cost_inr"])
        total_evidence += discharge_recons
        metrics.append(
            {
                "key": "throughput",
                "label": "Throughput & length of stay",
                # None, not 0: "0 days average stay" is a measurement nobody made.
                "value": round(avg_los, 2) if avg_los is not None else None,
                "unit": "days average length of stay",
                "amount": throughput_amount,
                "evidence_count": discharge_recons,
                "assumption": (
                    f"{discharge_recons} discharge "
                    f"{_plural(discharge_recons, 'reconciliation')} × "
                    f"{a['los_reduction_days_per_discharge']:g} day shorter stay × "
                    f"{_fmt_money(a['ward_bed_day_cost_inr'])} per bed-day; "
                    + (
                        f"measured average length of stay {avg_los:.2f} days"
                        if avg_los is not None
                        else "average length of stay not yet measured"
                    )
                ),
            }
        )

    # ── Totals ──────────────────────────────────────────────────────────────
    monthly = _money(sum(m["amount"] for m in metrics) * (30.0 / period_days))
    annual = _money(monthly * 12.0)
    # Reported SEPARATELY. These used to be summed into one "events prevented"
    # figure, which added ICU days to adverse drug events: two different units
    # in one number on the screen a CFO is asked to sign off.
    by_key = {m["key"]: m for m in metrics}
    icu_days_total = by_key["deterioration_caught_early"]["value"]
    ades_total = by_key["medication_safety"]["value"]
    simulated_excluded_total = sum(m.get("excluded_simulated", 0) for m in metrics)

    return {
        "generated_at": now.isoformat(),
        # The money unit travels WITH the money. Every amount in this payload is
        # denominated in `currency.code`; the client formats from this rather
        # than hardcoding a locale, which is exactly how the dashboard came to
        # render an India deployment's figures as US dollars.
        "currency": {
            "code": CURRENCY_CODE,
            "locale": CURRENCY_LOCALE,
        },
        "period": {
            "days": period_days,
            "start": cutoff,
            "end": now.isoformat(),
        },
        "metrics": metrics,
        "totals": {
            "monthly_savings": monthly,
            "annual_savings": annual,
            "icu_days_avoided": icu_days_total,
            "ades_avoided": ades_total,
        },
        # How many escalations were left out because simulated vitals drove
        # them, so the screen can say so rather than look smaller unexplained.
        "simulated_excluded": simulated_excluded_total,
        # Echo the assumptions actually used, with unit + citation, so the UI can
        # render editable fields pre-filled with what produced these numbers.
        "assumptions": {
            k: {
                "value": a[k],
                "unit": DEFAULT_ASSUMPTIONS[k]["unit"],
                "citation": DEFAULT_ASSUMPTIONS[k]["citation"],
            }
            for k in DEFAULT_ASSUMPTIONS
        },
        "has_data": total_evidence > 0,
    }
