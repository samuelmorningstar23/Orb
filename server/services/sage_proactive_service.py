"""Orb Sage — Proactive surveillance.

Deterministic rules continuously scan each admitted patient's latest data and
surface prioritised INSIGHTS ('Sage noticed X') to the attending clinician. The
rules do the scanning (cheap + reliable); the message is templated from the real
numbers (grounded — it cannot hallucinate a patient or a value). Each insight may
carry a suggested action that one-clicks into the agentic loop.

Everything here is defensive: a patient with missing/None vitals, no risk history,
or malformed data must never raise — surveillance must not take down the app.
"""

import uuid
from datetime import UTC, datetime, timedelta

from database import db_session
from permissions import department_scope, patient_in_scope, scope_patient_ids_sql
from services import facility_service
from services.clinical_time import format_utc, parse_to_utc
from services.news2_text import news2_description
from services.vigil_service import news2_reason_line, score_vitals_row, scoring_enabled
from services.vital_charting import as_charted, charted_text

INSIGHT_COOLDOWN_MIN = 30  # don't re-raise the same dedup_key within this window
AUTO_RESOLVE_AFTER_MIN = (
    10  # auto-resolve an active insight once its condition has been clear this long
)
RECURRENCE_FLAP_GUARD_MIN = (
    3  # after a SYSTEM auto-resolve, how long before the same key may re-raise
)
RISE_REFERENCE_MIN = (
    15  # Rule 1 "rose from" compares against the newest vitals row at least this much older
)
_LEVELS = {"stable": 0, "elevated": 1, "critical": 2}

# Suggested actions an insight may never carry into a one-click draft. A rule
# that fires on vital signs has no basis to name a drug: the old Rule 1 and
# Rule 3 attached "IV ceftriaxone 2g" to every critical and every SIRS-positive
# patient, chosen by nothing (not the allergy list, renal function, the source
# or local resistance), and a doctor confirming the draft charted it for a
# patient with a documented penicillin anaphylaxis. Ruled out 2026-10-02. The
# rules no longer emit these; the set still matters for rows stored before
# then, which the feed hides and the /act route refuses.
NON_ACTIONABLE_ACTION_TYPES = frozenset({"start_medication", "stop_medication"})

# Shared dedup predicate — a same-key insight is suppressed when it is still on
# the clinician's feed, or when a decision about it is recent enough.
#
# The cooldown arm is deliberately restricted to dismissed/acted: those are
# terminal HUMAN decisions, and re-raising minutes after a clinician waved the
# insight away is nagging. auto_resolved is NOT a decision — it means the
# condition cleared and the alert was pulled from the feed, so the clinician is
# now looking at nothing. Holding it under the full 30-min cooldown (anchored to
# the ORIGINAL created_at) meant a patient who went critical, recovered, and
# crashed again raised no new insight for up to ~20 minutes. Recurrences get
# only a short flap-guard instead, anchored to updated_at (the resolution time,
# set by both auto-resolve UPDATEs).
#
# Binds, in order: dedup_key, human-decision cutoff, flap-guard cutoff.
_DEDUP_SUPPRESSED_SQL = """dedup_key = ? AND (
    state = 'active'
    OR (state IN ('dismissed', 'acted') AND created_at > ?)
    OR (state = 'auto_resolved' AND updated_at > ?)
)"""


def _dedup_cutoffs() -> tuple:
    """(human-decision cutoff, auto-resolve flap-guard cutoff) for _DEDUP_SUPPRESSED_SQL."""
    now = datetime.now(UTC)
    return (
        (now - timedelta(minutes=INSIGHT_COOLDOWN_MIN)).isoformat(),
        (now - timedelta(minutes=RECURRENCE_FLAP_GUARD_MIN)).isoformat(),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _level_rank(level) -> int:
    return _LEVELS.get((level or "").lower(), 0)


def _num(v):
    """Coerce to float or None — tolerates strings, None, junk."""
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _hours_since(ts) -> float:
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 3600.0
    except Exception:
        return 0.0


def _refresh_insight(db, dedup_key, detail, title=None, severity=None, observed_at=None) -> None:
    """Rewrite the detail of the ACTIVE insight for this key with this sweep's
    numbers. The dedup guard used to stop a refire at the door, so a card raised
    at SpO2 94 still read 94 an hour later beside a chart reading 88.

    `observed_at` is when the reading the detail describes was taken (the
    vitals row's own timestamp, for a copied set the time on the sheet); the
    UI prints it. updated_at is the sweep's clock and only orders and ages the
    feed: printing it put "At 01:51 AM" on a set the sheet timed 22:50.

    A `title` and `severity` are rewritten too when given. Rule 1 stands a card
    down ("was critical", severity info), and a refire for that key must put
    "is critical" and its red dot back."""
    sets, args = ["detail = ?", "updated_at = ?"], [(detail or "")[:600], _now()]
    if observed_at is not None:
        sets.append("observed_at = ?")
        args.append(observed_at)
    if title is not None:
        sets.append("title = ?")
        args.append(title[:200])
    if severity is not None:
        sets.append("severity = ?")
        args.append(severity)
    db.execute(
        f"UPDATE sage_insights SET {', '.join(sets)} WHERE dedup_key = ? AND state = 'active'",
        (*args, dedup_key),
    )


def _location(ward, bed) -> str:
    """Where the patient is, for an insight title. A bed code that already
    carries its ward ('GM-104' on ward 'GM-1') stands alone; the old
    f"{ward}-{bed}" printed 'GM-1-GM-104'."""
    ward = str(ward or "").strip()
    bed = str(bed or "").strip()
    if bed and (not ward or bed.upper().startswith(ward.split("-")[0].upper())):
        return bed
    if ward and bed:
        return f"{ward}, bed {bed}"
    return ward or "location not recorded"


def _insert_insight(
    db,
    patient_id,
    itype,
    severity,
    title,
    detail,
    target_user,
    dedup_key,
    action_type=None,
    action_target=None,
    observed_at=None,
):
    """Atomic dedup-and-insert. A single INSERT ... WHERE NOT EXISTS so concurrent
    sweeps cannot both create the same insight — the previous check-then-insert raced
    (50 parallel evaluations produced duplicate active insights). SQLite serializes
    writes, so the embedded NOT EXISTS sees prior committed inserts."""
    # FEEDBACK LOOP: skip patterns clinicians consistently reject (never for
    # critical severity — see sage_feedback_service safety note). This makes Sage
    # quieter about low-value nudges over time without hiding anything the
    # clinician is already looking at.
    try:
        from services.sage_feedback_service import is_pattern_suppressed

        if is_pattern_suppressed(itype, severity, db=db):
            # Not raised again, but a card already on the feed still gets this
            # sweep's numbers, and the time they were taken.
            _refresh_insight(db, dedup_key, detail, title, severity, observed_at)
            return None
    except Exception:
        pass  # suppression is an optimisation; never block a real insight on it
    iid = str(uuid.uuid4())
    cutoff, flap_cutoff = _dedup_cutoffs()
    cur = db.execute(
        f"""INSERT INTO sage_insights
           (id, patient_id, insight_type, severity, title, detail,
            suggested_action_type, suggested_action_target, target_user_id, dedup_key,
            state, created_at, updated_at, observed_at)
           SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM sage_insights WHERE {_DEDUP_SUPPRESSED_SQL}
           )""",
        (
            iid,
            patient_id,
            itype,
            severity,
            title[:200],
            (detail or "")[:600],
            action_type,
            action_target,
            target_user,
            dedup_key,
            _now(),
            _now(),
            observed_at,
            dedup_key,
            cutoff,
            flap_cutoff,
        ),
    )
    if cur.rowcount > 0:
        return iid
    # Suppressed. If that is because the same insight is still on the feed, it
    # gets this sweep's numbers rather than staying frozen at raise time.
    _refresh_insight(db, dedup_key, detail, title, severity, observed_at)
    return None


def _read_moment(patient_id):
    """Everything the rules read for one patient, in ONE session that only
    SELECTs: (patient, latest vitals rows, the Rule 1 reference row, active
    meds), or None when the patient is not admitted.

    Rows come back as dicts and the session is closed before anything is scored,
    because the CfC overlay in `score_vitals_row` reads the vitals history on its
    own connection, and the sweep's write session must not be open around it.

    Vitals are newest by OBSERVATION time, id only as the exact-tie breaker: the
    order the Vigil board reads (vigil_service.scored_admitted_patients). The
    reference row is the newest one observed at least RISE_REFERENCE_MIN before
    the latest; text order is time order because every writer stores one UTC
    spelling (services/clinical_time.py).
    """
    with db_session() as db:
        pat = db.execute(
            "SELECT id, name, ward, bed, diagnosis, attending_doctor_id, status, on_scale_2 "
            "FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
        if not pat or (pat["status"] or "") != "admitted":
            return None
        vits = [
            dict(v)
            for v in db.execute(
                "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
                "ORDER BY timestamp DESC, id DESC LIMIT 4",
                (patient_id,),
            ).fetchall()
        ]
        before = None
        if vits:
            try:
                cutoff = format_utc(
                    parse_to_utc(vits[0]["timestamp"]) - timedelta(minutes=RISE_REFERENCE_MIN)
                )
            except (TypeError, ValueError):
                cutoff = None  # unparseable time: no reference, so no rise
            if cutoff:
                row = db.execute(
                    "SELECT * FROM vitals WHERE patient_id = ? AND timestamp <= ? "
                    "AND voided_at IS NULL ORDER BY timestamp DESC, id DESC LIMIT 1",
                    (patient_id, cutoff),
                ).fetchone()
                before = dict(row) if row else None
        meds = [
            dict(m)
            for m in db.execute(
                "SELECT drug_name, started_at FROM medications WHERE patient_id = ? AND status = 'active'",
                (patient_id,),
            ).fetchall()
        ]
    return dict(pat), vits, before, meds


def _label(now, table) -> str:
    """'NEWS2 9 (critical) · a single parameter scores 3', the one server label
    (news2_text.news2_description). `now` is the board's score (overlay on),
    `table` the same row on the RCP chart alone (overlay off). When the CfC
    overlay moved the band the chart's own band is printed and the overlay
    named: news2_description prints a level as given, and an overlaid level
    beside the chart's aggregate reads as a NEWS2 the chart does not give."""
    text = news2_description(table)
    if now["level"] != table["level"]:
        text += f", raised to {now['level']} by the CfC overlay"
    return text


def _reasons(now, table) -> str:
    """The Relay reason line for this moment (vigil_service.news2_reason_line),
    led by the overlay's own "CfC: ..." driver when there is one: the line is
    built from the NEWS2 breakdown, which never carries it. A wording failure
    leaves the line out (as ruling G2 does for the alert lanes) and never costs
    the clinician the card; the label in front of it already states the band."""
    cfc = [str(d) for d in (now.get("drivers") or []) if str(d).startswith("CfC:")]
    try:
        cfc.append(news2_reason_line(table))
    except Exception as e:
        print(f"  [SageProactive] reason line failed, card raised without it: {e}")
    return "; ".join(cfc)


def _detail(*parts) -> str:
    """Sentences, skipping an empty one: 'A. B. C.'"""
    return ". ".join(p for p in parts if p) + "."


# A Rule 1 card that has been stood down. Rule 1 raises only critical and
# warning cards, so 'info' on a deterioration key means exactly that.
STOOD_DOWN_SEVERITY = "info"

# The value at which each vital scores 3 on the RCP chart, on either SpO2 scale,
# and the worst ACVPU. Used only to ask what an UNMEASURED parameter could add.
_WORST_VALUES = {
    "heart_rate": 140.0,
    "systolic_bp": 80.0,
    "respiratory_rate": 30.0,
    "temperature": 34.0,
    "spo2": 80.0,
    "consciousness": "unresponsive",
}


def _worst_case_level(row, on_scale_2) -> str:
    """The band this vitals row would score if every UNMEASURED parameter were
    at its worst: each missing vital at a value scoring 3, ACVPU unresponsive,
    and an unrecorded O2 tried both ways (on Scale 2, oxygen changes how SpO2
    itself scores). Measured values stay as charted. Scored by the real scorer,
    overlay off, so no hand-kept points table can drift from the chart."""
    filled = dict(row)
    for col, worst in _WORST_VALUES.items():
        value = filled.get(col)
        if value is None or (isinstance(value, str) and not value.strip()):
            filled[col] = worst
    o2 = filled.get("on_oxygen")
    levels = []
    for option in [o2] if o2 is not None else [True, False]:
        filled["on_oxygen"] = option
        scored = score_vitals_row(filled, on_scale_2=on_scale_2, overlay=False)
        assert scored is not None
        levels.append(scored["level"])
    return max(levels, key=_level_rank)


def _stood_down_id(db, dedup_key):
    """The id of this key's active card if it is stood down, else None."""
    row = db.execute(
        "SELECT id FROM sage_insights WHERE dedup_key = ? AND state = 'active' AND severity = ?",
        (dedup_key, STOOD_DOWN_SEVERITY),
    ).fetchone()
    return row["id"] if row else None


def _stand_down(
    db, dedup_key, now, label, worst, band, was_title, no_longer, observed_at=None
) -> bool:
    """The latest observation is below `band`, the band of this Rule 1 card.
    Returns True when the card must stay on the feed (the caller keeps its key
    valid). `worst` is `_worst_case_level` of the latest row (None when the set
    is complete).

    A COMPLETE set stands the card down at once (ruled 2026-10-03): retitled
    ("was critical"), severity info (a blue dot, out of the active-alert count),
    the detail says what the latest observation scored, and the key is NOT kept
    valid, so the AUTO_RESOLVE_AFTER_MIN clock runs from this rewrite. Only the
    first such sweep rewrites it (the severity check): restamping it every
    sweep would keep it alive for ever. If the patient goes back up, the raise
    path refreshes the same card and puts its title and severity back.

    An INCOMPLETE set is not recovery when the unmeasured parameters could still
    put the patient in the card's band (ruled 2026-10-03): an unmeasured
    parameter scores 0, so the aggregate is a floor. The card keeps its title,
    says what was not measured, and stays until a set settles it or a clinician
    dismisses it. When even the unmeasured parameters at their worst could not
    reach the band (a monitor feed with no ACVPU or O2 at NEWS2 1 can reach 6,
    never 7), the set does show the patient came down, and it stands the card
    down like a complete one, saying what was not measured. Narrowed 2026-10-03
    ("fix and build everything"): before, a monitor-only patient's card could
    never clear.
    """
    row = db.execute(
        "SELECT severity FROM sage_insights WHERE dedup_key = ? AND state = 'active'",
        (dedup_key,),
    ).fetchone()
    if row is None or row["severity"] == STOOD_DOWN_SEVERITY:
        return False
    missing = ", ".join(now.get("missing_parameters") or []) or "not recorded"
    if now.get("complete") is False:
        if worst is None or _level_rank(worst) >= _level_rank(band):
            _refresh_insight(
                db,
                dedup_key,
                f"Not confirmed recovered: {label}; not measured: {missing}.",
                observed_at=observed_at,
            )
            return True
        detail = (
            f"{no_longer}: latest observation {label}; not measured: {missing}, "
            f"which could not bring it to {band}."
        )
    else:
        detail = f"{no_longer}: latest observation {label}."
    _refresh_insight(db, dedup_key, detail, was_title, STOOD_DOWN_SEVERITY, observed_at)
    return False


class _RuleOneOff(Exception):
    """Rule 1 (the NEWS2 band) does not run while scoring is switched off."""


def evaluate_patient(patient_id, raised_again=None) -> list:
    """Run all surveillance rules for one patient. Returns ids of NEW insights created.
    Never raises.

    `raised_again`, when a list, also collects the ids of stood-down Rule 1
    cards this sweep put back up ("was critical" to "is critical"). They are the
    same card, so not new, but the feed must hear about them now rather than on
    its next poll."""
    if not patient_id:
        return []
    new_ids = []
    valid_keys: set = set()
    try:
        moment = _read_moment(patient_id)
        if moment is None:
            return []
        pat, vits, before, meds = moment
        target = pat["attending_doctor_id"]
        loc = _location(pat["ward"], pat["bed"])
        name = pat["name"] or "Patient"
        # When the latest reading was taken: the vitals row's own timestamp
        # (for a copied set, the time on the paper sheet). Every rule below
        # describes this row, so every card it raises or rewrites carries it.
        read_at = vits[0]["timestamp"] if vits else None

        # Scored with NO session open (see _read_moment). A scoring failure (a
        # CfC checkpoint that will not run) must not cost the patient Rules 2-4,
        # nor stand down a card on the feed: Rule 1 sits this sweep out.
        now = table = before_score = worst = None
        scored = True
        # Scoring switched off (the Orb Ward Record): Rule 1 never runs. Its
        # cards are not kept valid either, so any left from before go stale and
        # auto-resolve, and get_active_insights hides them meanwhile.
        rule_1 = scoring_enabled()
        try:
            if not rule_1:
                raise _RuleOneOff()
            on_scale_2 = bool(pat["on_scale_2"])
            if vits:
                now = score_vitals_row(vits[0], patient_id=patient_id, on_scale_2=on_scale_2)
                table = score_vitals_row(vits[0], on_scale_2=on_scale_2, overlay=False)
                assert now is not None
                if now.get("complete") is False:
                    worst = _worst_case_level(vits[0], on_scale_2)
            if before is not None:
                # Overlay off: it scores the history as it stands NOW, so it
                # cannot say what the band was 15 minutes ago.
                before_score = score_vitals_row(before, on_scale_2=on_scale_2, overlay=False)
        except _RuleOneOff:
            scored = False
        except Exception as e:
            scored = False
            print(f"  [SageProactive] scoring {patient_id} failed: {e}")

        with db_session() as db:
            # ── Rule 1: deterioration (critical now, or risen in 15 minutes) ──
            # Banded from the SAME moment as the Vigil board and the ward brief
            # (ruled 2026-10-03): the latest vitals row, through score_vitals_row,
            # overlay on, on the patient's own SpO2 scale. It used to read
            # risk_scores, which the simulator and the Scribe lane never write,
            # so the feed said "Rajesh Iyer (GM-102) is critical" over a board
            # row reading stable, and on a seeded ward its critical set was the
            # exact inverse of the board's. No vitals, no Rule 1 card: the board
            # shows that patient as not recorded.
            #
            # "Rose from" compares against the newest row at least
            # RISE_REFERENCE_MIN older, not the previous row: the simulator
            # writes every 5 s and this sweep runs every 15 s, so a
            # previous-row compare saw about one rise in three.
            crit_key = f"{patient_id}:deterioration:critical"
            elev_key = f"{patient_id}:deterioration:elevated"
            if not rule_1:
                pass
            elif not scored:
                valid_keys.update((crit_key, elev_key))
            elif now is not None:
                lvl_now = now["level"]
                label = _label(now, table)
                if lvl_now in ("critical", "elevated"):
                    valid_keys.add(f"{patient_id}:deterioration:{lvl_now}")
                # A stood-down card about to go back up, so it can be pushed.
                flipping = _stood_down_id(db, f"{patient_id}:deterioration:{lvl_now}")
                if lvl_now == "critical":
                    iid = _insert_insight(
                        db,
                        patient_id,
                        "deterioration",
                        "critical",
                        f"{name} ({loc}) is critical",
                        _detail(label, _reasons(now, table), "Recommend immediate senior review"),
                        target,
                        crit_key,
                        observed_at=read_at,
                    )
                    if iid:
                        new_ids.append(iid)
                elif (
                    lvl_now == "elevated"
                    and before_score is not None
                    and _level_rank(lvl_now) > _level_rank(before_score["level"])
                ):
                    iid = _insert_insight(
                        db,
                        patient_id,
                        "deterioration",
                        "warning",
                        f"{name} ({loc}) is deteriorating",
                        _detail(
                            f"Rose from {news2_description(before_score, scale=False)} to {label}",
                            _reasons(now, table),
                            "Consider review",
                        ),
                        target,
                        elev_key,
                        observed_at=read_at,
                    )
                    if iid:
                        new_ids.append(iid)
                elif lvl_now == "elevated":
                    # Still elevated, no new rise: the card raised on the rise
                    # stays, with today's numbers rather than the ones it was
                    # raised on.
                    _refresh_insight(
                        db,
                        elev_key,
                        _detail(label, _reasons(now, table), "Consider review"),
                        f"{name} ({loc}) is deteriorating",
                        "warning",
                        read_at,
                    )
                if flipping and raised_again is not None:
                    raised_again.append(flipping)

                # Below a card's band: stand it down, unless an incomplete set
                # leaves it open (see _stand_down).
                if lvl_now != "critical" and _stand_down(
                    db,
                    crit_key,
                    now,
                    label,
                    worst,
                    "critical",
                    f"{name} ({loc}) was critical",
                    "No longer critical",
                    read_at,
                ):
                    valid_keys.add(crit_key)
                if lvl_now == "stable" and _stand_down(
                    db,
                    elev_key,
                    now,
                    label,
                    worst,
                    "elevated",
                    f"{name} ({loc}) was deteriorating",
                    "No longer elevated",
                    read_at,
                ):
                    valid_keys.add(elev_key)

            # ── Rule 2: trajectory (SpO2 falling / RR rising over last 3 readings) ──
            if len(vits) >= 3:
                spo2 = [_num(vits[i]["spo2"]) for i in range(3)]
                rr = [_num(vits[i]["respiratory_rate"]) for i in range(3)]
                # index 0 = latest, 2 = oldest of the window
                if (
                    all(x is not None for x in spo2)
                    and spo2[0] <= spo2[1] <= spo2[2]
                    and (spo2[2] - spo2[0]) >= 3
                ):
                    valid_keys.add(f"{patient_id}:trajectory:spo2")
                    iid = _insert_insight(
                        db,
                        patient_id,
                        "trajectory",
                        "warning",
                        f"{name} ({loc}): SpO2 falling",
                        f"SpO2 trending down {charted_text(spo2[2], unit='%')} to "
                        f"{charted_text(spo2[0], unit='%')} over the last 3 readings.",
                        target,
                        f"{patient_id}:trajectory:spo2",
                        observed_at=read_at,
                    )
                    if iid:
                        new_ids.append(iid)
                if (
                    all(x is not None for x in rr)
                    and rr[0] >= rr[1] >= rr[2]
                    and (rr[0] - rr[2]) >= 4
                ):
                    valid_keys.add(f"{patient_id}:trajectory:rr")
                    iid = _insert_insight(
                        db,
                        patient_id,
                        "trajectory",
                        "warning",
                        f"{name} ({loc}): respiratory rate rising",
                        f"RR climbing {charted_text(rr[2])} to {charted_text(rr[0])} over the last 3 readings.",
                        target,
                        f"{patient_id}:trajectory:rr",
                        observed_at=read_at,
                    )
                    if iid:
                        new_ids.append(iid)

            # ── Rule 3: sepsis pattern (SIRS: any 2 of 4 criteria) ──
            # SIRS is MET by any 2 of: temp > 38.0 or < 36.0, HR > 90, RR > 20
            # (or PaCO2 < 32), white cell count > 12 or < 4 (or > 10% bands);
            # the limbs are STRICT. Orb holds no structured white cell count or
            # blood gas, so only the three vital-sign limbs can be checked: 2 of
            # them is SIRS whatever the count would show, and the detail says the
            # count was not checked. Ruled 2026-10-03; until then the rule
            # demanded all three vitals and never fired on tachycardia plus
            # tachypnoea without a fever. Limbs are tested on the value AS
            # CHARTED (vital_charting), the number NEWS2 scores and the detail
            # prints, so "Temp 38.0C, HR 90, RR 20" can never read as SIRS.
            if vits:
                t = as_charted(_num(vits[0]["temperature"]), 1)
                hr = as_charted(_num(vits[0]["heart_rate"]))
                rrate = as_charted(_num(vits[0]["respiratory_rate"]))
                met = []
                if t is not None and t > 38.0:
                    met.append("temp >38.0")
                elif t is not None and t < 36.0:
                    met.append("temp <36.0")
                if hr is not None and hr > 90:
                    met.append("HR >90")
                if rrate is not None and rrate > 20:
                    met.append("RR >20")
                if len(met) >= 2:
                    valid_keys.add(f"{patient_id}:sepsis")
                    iid = _insert_insight(
                        db,
                        patient_id,
                        "sepsis",
                        "warning",
                        f"{name} ({loc}): possible sepsis",
                        f"Temp {charted_text(t, 1, unit='C')}, HR {charted_text(hr)}, RR {charted_text(rrate)}: "
                        f"meets SIRS, {len(met)} of 4 criteria ({', '.join(met)}; white cell count not "
                        "recorded in Orb). Consider sepsis screen + cultures.",
                        target,
                        f"{patient_id}:sepsis",
                        observed_at=read_at,
                    )
                    if iid:
                        new_ids.append(iid)

            # ── Rule 4: antibiotic stewardship (broad-spectrum > 72h) ──
            for m in meds:
                drug = (m["drug_name"] or "").lower()
                if any(
                    k in drug
                    for k in (
                        "ceftriaxone",
                        "meropenem",
                        "piperacillin",
                        "tazobactam",
                        "vancomycin",
                        "monocef",
                    )
                ):
                    if m["started_at"] and _hours_since(m["started_at"]) >= 72:
                        valid_keys.add(f"{patient_id}:stewardship:{drug[:12]}")
                        iid = _insert_insight(
                            db,
                            patient_id,
                            "stewardship",
                            "info",
                            f"{name} ({loc}): antibiotic review due",
                            f"On {m['drug_name']} for >72h. Review for de-escalation / check culture results.",
                            target,
                            f"{patient_id}:stewardship:{drug[:12]}",
                        )
                        if iid:
                            new_ids.append(iid)

            # ── Auto-resolution ──
            # Touch updated_at on every active insight whose condition still
            # holds this sweep; anything untouched for AUTO_RESOLVE_AFTER_MIN
            # is no longer ward truth and resolves itself. Manual dismiss/act
            # states are untouched (state = 'active' guard). Without this, the
            # ward-brief "active" count grows monotonically: insights were born
            # 'active' and only a human click could ever retire them.
            resolved_at = _now()
            if valid_keys:
                marks = ",".join("?" * len(valid_keys))
                db.execute(
                    f"UPDATE sage_insights SET updated_at = ? "
                    f"WHERE patient_id = ? AND state = 'active' AND dedup_key IN ({marks})",
                    (resolved_at, patient_id, *valid_keys),
                )
            stale = (datetime.now(UTC) - timedelta(minutes=AUTO_RESOLVE_AFTER_MIN)).isoformat()
            db.execute(
                "UPDATE sage_insights SET state = 'auto_resolved', updated_at = ? "
                "WHERE patient_id = ? AND state = 'active' AND updated_at < ?",
                (resolved_at, patient_id, stale),
            )
    except Exception as e:
        print(f"  [SageProactive] evaluate_patient({patient_id}) error: {e}")
    return new_ids


def evaluate_all_admitted(raised_again=None, user=None) -> list:
    """Sweep every admitted patient. Returns ids of all new insights created.
    `raised_again` as in evaluate_patient. A manual caller supplies their
    authenticated user; the scheduled system sweep keeps hospital-wide scope.
    """
    ids = []
    try:
        with db_session() as db:
            scope_sql, scope_params = scope_patient_ids_sql(
                department_scope(user),
                facilities=(
                    facility_service.facility_scope_for(user, db=db) if user is not None else None
                ),
            )
            rows = db.execute(
                f"SELECT id FROM patients WHERE status = 'admitted' AND id IN ({scope_sql})",
                scope_params,
            ).fetchall()
            # Discharged patients are never swept again — without this their
            # insights stay 'active' forever (observed leak: 31 patients with
            # active insights vs 30 admitted).
            manual_scope = f" AND patient_id IN ({scope_sql})" if user is not None else ""
            db.execute(
                f"""UPDATE sage_insights SET state = 'auto_resolved', updated_at = ?
                   WHERE state = 'active' AND patient_id NOT IN
                         (SELECT id FROM patients WHERE status = 'admitted'){manual_scope}""",
                (_now(), *(scope_params if user is not None else ())),
            )
        for r in rows:
            ids.extend(evaluate_patient(r["id"], raised_again))
    except Exception as e:
        print(f"  [SageProactive] sweep error: {e}")
    return ids


def get_active_insights(user_id, role) -> list:
    """Active insights for this clinician (their patients), newest + most severe first.
    Admins see every recipient within their normal patient scope. The stored
    user row supplies role and department; the role argument is kept for callers.
    """
    sev_order = "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END"
    with db_session() as db:
        user = db.execute(
            "SELECT id, role, department FROM users WHERE id = ? AND COALESCE(is_active, 1) = 1",
            (user_id,),
        ).fetchone()
        if user is None:
            return []
        user = dict(user)
        # Recipient assignment never grants access to a chart. Derive scope
        # from the stored clinician, including an insight targeted at them.
        role = user["role"]
        scope_sql, scope_params = scope_patient_ids_sql(
            department_scope(user),
            facilities=facility_service.facility_scope_for(user, db=db),
        )
        if role == "admin":
            rows = db.execute(
                f"""SELECT i.*, p.name as patient_name FROM sage_insights i
                    LEFT JOIN patients p ON p.id = i.patient_id
                    WHERE i.state = 'active' AND i.patient_id IN ({scope_sql})
                    ORDER BY {sev_order}, i.created_at DESC LIMIT 50""",
                scope_params,
            ).fetchall()
        else:
            rows = db.execute(
                f"""SELECT i.*, p.name as patient_name FROM sage_insights i
                    LEFT JOIN patients p ON p.id = i.patient_id
                    WHERE i.state = 'active' AND (i.target_user_id = ? OR i.target_user_id IS NULL)
                    AND i.patient_id IN ({scope_sql})
                    ORDER BY {sev_order}, i.created_at DESC LIMIT 50""",
                (user_id, *scope_params),
            ).fetchall()
        out = [dict(r) for r in rows]
    # Scoring switched off: a deterioration card is a band, so none is shown.
    if not scoring_enabled():
        out = [i for i in out if i.get("insight_type") != "deterioration"]
    # A row stored before 2026-10-02 may still name a drug. Hide it here rather
    # than rewrite it: the stored row is the record of what Sage suggested.
    for i in out:
        if i.get("suggested_action_type") in NON_ACTIONABLE_ACTION_TYPES:
            i["suggested_action_type"] = None
            i["suggested_action_target"] = None
    return out


def transition_insight(insight_id, new_state, user_id, role) -> dict:
    """Dismiss / act an insight inside the stored caller's chart scope.
    Recipient assignment is an additional guard (admins: any recipient).
    Returns the updated row or {} without disclosing an inaccessible insight.
    """
    if new_state not in ("dismissed", "acted"):
        raise ValueError("invalid state")
    with db_session() as db:
        row = db.execute("SELECT * FROM sage_insights WHERE id = ?", (insight_id,)).fetchone()
        if not row:
            return {}
        user = db.execute(
            "SELECT id, role, department FROM users WHERE id = ? AND COALESCE(is_active, 1) = 1",
            (user_id,),
        ).fetchone()
        if user is None:
            return {}
        user = dict(user)
        role = user["role"]
        if not patient_in_scope(
            db,
            row["patient_id"],
            department_scope(user),
            facilities=facility_service.facility_scope_for(user, db=db),
        ):
            return {}
        if role != "admin" and row["target_user_id"] not in (None, user_id):
            raise PermissionError("not your insight")
        if row["state"] != "active":
            return dict(row)  # idempotent — already handled
        db.execute(
            "UPDATE sage_insights SET state = ?, updated_at = ? WHERE id = ?",
            (new_state, _now(), insight_id),
        )
        # FEEDBACK LOOP: record the clinician's judgment keyed by pattern so the
        # engine can learn which insight patterns are worth surfacing. Same
        # transaction as the transition, so the outcome and the state can't
        # diverge. Best-effort — never fails the transition.
        try:
            from services.sage_feedback_service import pattern_of, record_feedback

            record_feedback(
                pattern_of(row["insight_type"], row["severity"]),
                new_state,
                insight_id=insight_id,
                patient_id=row["patient_id"],
                actor_user_id=user_id,
                db=db,
            )
        except Exception:
            pass
        return dict(
            db.execute("SELECT * FROM sage_insights WHERE id = ?", (insight_id,)).fetchone()
        )
