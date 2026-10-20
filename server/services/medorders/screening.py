"""Orb: order-time screening against the medication-order store (medorders-9,
W9-C; rulings MO-R3 and MO-R6, safety rulings held for Samuel's confirmation).

Everything here reads; nothing writes. Three switches, all read at call time
and all OFF by default, because each sits behind ORB_MED_ORDER_LIFECYCLE
(authoritative mode). With the lifecycle off, nothing in this module changes
anything: the interaction screen reads 'active' ward rows exactly as before,
a PRN ward row stays ungivable at the bedside, and no duplicate rule runs.

WHAT THE INTERACTION SCREEN COMPARES A NEW DRUG WITH (MO-R3 (a), screening
half). With the lifecycle on, the order is the authority, not the ward row:

  live orders        status prescribed, verified or held, PRN included. A
                     held drug is still in the patient (it may be resumed), and
                     a signed CPOE or order-set medicine is live the moment it
                     is signed. Drafts are not: nothing has reached the
                     patient, and a draft is screened again when it is signed.
                     Ended orders (stopped, completed, cancelled, rejected) are
                     not.
  unlinked ward rows 'active' or 'prn' rows with no order behind them: rows a
                     legacy writer charted after the boot adoption, and rows
                     whose link names an order that does not exist (a broken
                     link is never a reason to leave a drug out of the screen).
  never              outpatient lines (encounter_type 'op'). The outpatient
                     lane screens them itself
                     (outpatient.outpatient_interactions), and an outpatient
                     prescription never reaches the inpatient chart (OPD-S4).

The order being screened is left out of its own comparison set (it would
otherwise interact with itself: every anticoagulant against its own row):
exclude_med_id leaves out that ward row AND the order it projects, and
exclude_order_id leaves out that order AND its ward row.

The set is exactly what core.screen_fingerprint_in_txn stamps (live orders and
unlinked live ward rows), less outpatient lines, so a step that screened
outside its write transaction and re-reads the stamp inside it sees every
change that could alter this screen.

PRN GIVABLE (MO-R3 (a), bedside half). With the lifecycle on, a ward row
charted 'prn' may be given through the full bedside gate set (the order-state
check, the pharmacist checks, the inpatient guard, the allergy interlock and
the dose guard with its repeat window); emar_service asks prn_givable().
Today a PRN drug shows on the round but every dose is refused, so a PRN dose
is given off the record, past the bedside interlock, and the next dose's
repeat window cannot see it.

THE SAME-INGREDIENT DUPLICATE (MO-R6). At an order-time entry point
(DUPLICATE_SCREENED_ENTRY_POINTS: where a drug becomes live, or a pharmacy
order is raised for a new drug), a new drug that names the same ingredient as
something in the comparison set is a gate finding.

What counts as the same ingredient (same_ingredient). Each name is read in
its ingredient form (_ingredient_form): strengths, units and dosage-form
words left out, every other word kept, short ones included ('Vitamin K 10
mg' reads 'vitamin k', never 'vitamin'). A salt word ('potassium',
'hydrochloride') is left out only when a drug word stays beside it ('Losartan
Potassium' reads 'losartan'); a name that is all salt keeps every word
('Calcium Carbonate', 'Sodium Bicarbonate'), and so does one where only an
-ate, -ide or -ite word would be left ('Sodium Valproate'), so a name never
shrinks to a bare anion that another salt contains. Each name is also resolved
through the hospital's own catalogue by exact name or a whole-word prefix, so
a brand on the chart meets its generic (a bare class or salt word resolves by
exact name only). A longer catalogue name counts only when it adds a
strength, a unit, a form or an anion and nothing else (_same_product):
'Metformin' never takes the generic of a 'Metformin + Glimepiride' tablet,
which would make glimepiride a second metformin, nor 'Dolo' that of 'Dolo
Cold'. The price is a brand charted bare whose catalogue row adds a variant
word ('Augmentin' against 'Augmentin 625 DUO Tablet'): it meets its generic
only when charted with that word ('Augmentin 625 Duo'). Two forms are one
drug when they are equal or hold the same words in another order, or under
the bedside vocabulary (drug_safety_service.names_overlap: 'Paracetamol'
and 'Paracetamol + Codeine', and 'Prednisolone' inside 'Methylprednisolone',
flagged for the prescriber to answer) with two refusals: a substring that
runs into a number ('vitamin b1' inside 'vitamin b12'), and a name made only
of class words and short codes ('vitamin', 'insulin', 'vitamin c'), which
matches only the same name or the same words inside a combination. 'Insulin
Glargine' and 'Insulin Aspart' are two drugs, as are Vitamin K and Vitamin
D3, Sodium Bicarbonate and Calcium Carbonate, Calcium Gluconate and Zinc
Gluconate.

The rule is config.MED_ORDER_DUPLICATE_RULE (ORB_MED_ORDER_DUPLICATE_RULE):
  'block'        the default, the strictest option named (MO-R6 (c)): the
                 gate refuses the order (409 duplicate_order; a
                 non-interactive lane is blocked) and no reason clears it.
  'acknowledge'  the architect's recommendation (MO-R6 (b)): a 428 like a
                 severe alert; it clears with a written reason
                 (validate_duplicate_reason), recorded on the audit chain.

THE OUTPATIENT LANE (ENTRY_OPD) compares a different set, because it
prescribes for a patient who is not admitted (outpatient._require_writable_
visit refuses an admitted one) and nothing closes an inpatient order at
discharge while ruling MO-R7 is held:
  inpatient rows   only while the patient has an active admission. Without
                   one, every live inpatient order is left over from a
                   finished stay, and a medicine the patient went home on
                   could otherwise never be prescribed in clinic again.
  outpatient lines the patient's live lines whose course has not ended. A
                   line on the visit being prescribed on takes the rule
                   above: the prescriber can cancel it on this visit
                   (outpatient.cancel_prescription) and write the new one.
                   Every other line (an earlier visit's, another clinic's,
                   or one whose visit cannot be read) is a finding that
                   clears with a written reason whatever the rule
                   (clears_with_reason, the strictest option that leaves a
                   way through): nobody on this visit can cancel it, so a
                   hard block would refuse every refill of a continuing
                   medicine for good. The lane names its visit
                   (opd_visit_id); a caller that does not is read strictly:
                   a line on any open visit dated today or later takes the
                   rule above.
rule_for_findings turns these into the rule one gate call applies.
Entry points that screen an order that already exists (a pharmacist's
verification, a Helix supply step) or a draft that is screened again at sign
are listed in NOT_DUPLICATE_SCREENED with the reason; every entry point the
server passes to the gate is in one list or the other
(tests/test_medorder_screen_scope.py reads the code to hold it there).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from services.medorders import core

__all__ = [
    "ENTRY_OPD",
    "DUPLICATE_SCREENED_ENTRY_POINTS",
    "NOT_DUPLICATE_SCREENED",
    "DUPLICATE_RULES",
    "DUPLICATE_RULE_BLOCK",
    "DUPLICATE_RULE_ACKNOWLEDGE",
    "DUPLICATE_ERROR",
    "MIN_DUPLICATE_REASON_CHARS",
    "store_screen_on",
    "prn_givable",
    "duplicate_rule",
    "duplicate_rule_for",
    "comparison_rows",
    "outpatient_rows",
    "row_label",
    "find_duplicates",
    "same_ingredient",
    "ingredient_word_set",
    "rule_for_findings",
    "duplicate_check",
    "validate_duplicate_reason",
]

#: The outpatient lane's gate entry point (services/medorders/outpatient.py
#: ENTRY_POINT; pinned equal by tests/test_medorder_screen_scope.py). Its
#: duplicate check compares the patient's unended outpatient lines, and
#: inpatient rows only while the patient is admitted (the module note).
ENTRY_OPD = "opd_prescription"

#: Where the duplicate rule applies: a drug becomes live on the patient, or a
#: pharmacy order is raised for a new drug.
DUPLICATE_SCREENED_ENTRY_POINTS = frozenset(
    {
        "vigil.med_add",  # the chart add: live at once
        "medorder.create.cpoe",  # create-and-sign in one step
        "medorder.sign.cpoe",  # a draft becomes live
        "relay.confirm_agentic",  # a pharmacy order for a new drug
        "sage.agentic_handoff",  # chart row, order and supply (non-interactive)
        "scribe.auto_extraction",  # a drug a signed note starts (non-interactive)
        "sage_assessment",  # a drug Sage proposes (non-interactive)
        "helix.approve",  # a pharmacy order for a new drug
        ENTRY_OPD,  # an outpatient prescription line
    }
)

#: Gate entry points the duplicate rule deliberately does not apply to, and why.
NOT_DUPLICATE_SCREENED: dict[str, str] = {
    "orders.cpoe": (
        "with the lifecycle on a CPOE medicine is a draft that reaches nothing; "
        "the duplicate rule runs when it is signed (medorder.sign.cpoe)"
    ),
    "order_set.preview": "an order set's medicines are drafts, screened again when signed",
    "order_set.apply": "an order set's medicines are drafts, screened again when signed",
    "helix.<step>": (
        "a pharmacist's supply step on an order that already exists; the "
        "prescriber answered the duplicate rule when it was ordered"
    ),
    "seed.pharmacy_queue": "the demonstration seed script, never a clinical order",
}

DUPLICATE_RULE_BLOCK = "block"
DUPLICATE_RULE_ACKNOWLEDGE = "acknowledge"
#: ORB_MED_ORDER_DUPLICATE_RULE's values; the first is the default.
DUPLICATE_RULES = (DUPLICATE_RULE_BLOCK, DUPLICATE_RULE_ACKNOWLEDGE)

#: The error code of the hard block's 409.
DUPLICATE_ERROR = "duplicate_order"

#: The same floor as every other written override (drug_safety_service).
MIN_DUPLICATE_REASON_CHARS = 10


# ── The switches, read at call time ───────────────────────────────────────────


def store_screen_on() -> bool:
    """The interaction screen reads the order store (MO-R3 (a), screening)."""
    return core.lifecycle_on()


def prn_givable() -> bool:
    """A 'prn' ward row may be given at the bedside (MO-R3 (a), bedside)."""
    return core.lifecycle_on()


def duplicate_rule() -> str | None:
    """'block' or 'acknowledge' while the lifecycle is on (MO-R6), else None.
    An unreadable setting is the strictest rule."""
    if not core.lifecycle_on():
        return None
    import config

    rule = getattr(config, "MED_ORDER_DUPLICATE_RULE", DUPLICATE_RULE_BLOCK)
    return rule if rule in DUPLICATE_RULES else DUPLICATE_RULE_BLOCK


def duplicate_rule_for(entry_point: str | None) -> str | None:
    """The duplicate rule for one gate call, or None when it does not apply."""
    if entry_point not in DUPLICATE_SCREENED_ENTRY_POINTS:
        return None
    return duplicate_rule()


# ── The comparison set ────────────────────────────────────────────────────────


def _excluded(conn, exclude_med_id: int | None, exclude_order_id: str | None) -> tuple[set, set]:
    """(order ids, ward row ids) the screened order itself occupies."""
    orders: set = set()
    meds: set = set()
    if exclude_order_id:
        orders.add(str(exclude_order_id))
        row = conn.execute(
            "SELECT legacy_medication_id FROM med_orders WHERE id = ?", (str(exclude_order_id),)
        ).fetchone()
        if row is not None and row["legacy_medication_id"] is not None:
            meds.add(int(row["legacy_medication_id"]))
    if exclude_med_id is not None:
        meds.add(int(exclude_med_id))
        row = conn.execute(
            "SELECT med_order_id FROM medications WHERE id = ?", (int(exclude_med_id),)
        ).fetchone()
        if row is not None and row["med_order_id"]:
            orders.add(str(row["med_order_id"]))
        for r in conn.execute(
            "SELECT id FROM med_orders WHERE legacy_medication_id = ?", (int(exclude_med_id),)
        ).fetchall():
            orders.add(str(r["id"]))
    return orders, meds


def comparison_rows(
    conn, patient_id: str, *, exclude_med_id: int | None = None, exclude_order_id: str | None = None
) -> list[dict]:
    """Every drug the order-time screen compares a new one with (the module
    note): live inpatient orders, PRN and held included, then live ward rows
    with no order behind them, less the screened order's own rows. Each item:
    {drug_name, med_order_id, medication_id, status, is_prn, held, dose, route,
    frequency, setting 'inpatient', kind 'order' or 'chart'}. Reads only."""
    skip_orders, skip_meds = _excluded(conn, exclude_med_id, exclude_order_id)
    marks = ", ".join("?" for _ in core.LIVE_STATUSES)
    out: list[dict] = []
    for r in conn.execute(
        f"SELECT id, drug_name, status, is_prn, projection_status, legacy_medication_id, "
        f"dose, route, frequency FROM med_orders WHERE patient_id = ? "
        f"AND status IN ({marks}) AND COALESCE(encounter_type, 'ip') != 'op' "
        f"ORDER BY created_at, id",
        (patient_id, *core.LIVE_STATUSES),
    ).fetchall():
        if str(r["id"]) in skip_orders:
            continue
        if r["legacy_medication_id"] is not None and int(r["legacy_medication_id"]) in skip_meds:
            continue
        out.append(
            {
                "drug_name": r["drug_name"],
                "med_order_id": r["id"],
                "medication_id": r["legacy_medication_id"],
                "status": r["status"],
                "is_prn": bool(r["is_prn"]) or r["projection_status"] == "prn",
                "held": r["status"] == "held",
                "dose": r["dose"],
                "route": r["route"],
                "frequency": r["frequency"],
                "setting": "inpatient",
                "kind": "order",
            }
        )
    for r in conn.execute(
        "SELECT m.id, m.drug_name, m.status, m.dosage, m.route, m.frequency, m.held_at "
        "FROM medications m LEFT JOIN med_orders o ON o.id = m.med_order_id "
        "WHERE m.patient_id = ? AND m.status IN ('active', 'prn') "
        "AND (m.med_order_id IS NULL OR o.id IS NULL) ORDER BY m.id",
        (patient_id,),
    ).fetchall():
        if int(r["id"]) in skip_meds:
            continue
        out.append(
            {
                "drug_name": r["drug_name"],
                "med_order_id": None,
                "medication_id": r["id"],
                "status": r["status"],
                "is_prn": r["status"] == "prn",
                "held": bool(r["held_at"]),
                "dose": r["dosage"],
                "route": r["route"],
                "frequency": r["frequency"],
                "setting": "inpatient",
                "kind": "chart",
            }
        )
    return out


def _course_ended(end_at: str | None, now: datetime) -> bool:
    """An outpatient course whose end has passed. An unreadable end is live:
    the screen stays stricter, never quieter (outpatient._course_ended)."""
    if not end_at:
        return False
    from services.clinical_time import parse_to_utc

    try:
        return parse_to_utc(end_at) <= now
    except (TypeError, ValueError):
        return False


def _line_visit(payload_json: str | None) -> str | None:
    """The visit an outpatient line was written on, from its 'created' event
    (outpatient.py keeps it there: migration 61 has no column for it), or
    None when it cannot be read."""
    import json

    try:
        payload = json.loads(payload_json) if payload_json else None
    except (TypeError, ValueError):
        return None
    visit = payload.get("opd_visit_id") if isinstance(payload, dict) else None
    return str(visit) if visit else None


def _open_visits_from_today(conn, visit_ids: set) -> set:
    """Of these visits, the ones still open and dated today or later
    (hospital day): the visits a caller that names none may be prescribing
    on. Unreadable means every one of them (the stricter reading)."""
    if not visit_ids:
        return set()
    try:
        from services.hospital_time import hospital_today

        today = hospital_today().isoformat()
        wanted = sorted(visit_ids)
        marks = ", ".join("?" for _ in wanted)
        return {
            str(r["id"])
            for r in conn.execute(
                f"SELECT id, status, visit_date FROM opd_visits WHERE id IN ({marks})",
                wanted,
            ).fetchall()
            if r["status"] not in ("completed", "cancelled")
            and str(r["visit_date"] or "")[:10] >= today
        }
    except Exception:
        return set(visit_ids)


def outpatient_rows(
    conn,
    patient_id: str,
    *,
    exclude_order_id: str | None = None,
    now: datetime | None = None,
    opd_visit_id: str | None = None,
) -> list[dict]:
    """The patient's live outpatient lines whose course has not ended, in the
    comparison_rows shape (setting 'outpatient'), each with the visit it was
    written on (opd_visit_id) and this_visit: True only for a line on the
    visit being prescribed on (the module note: `opd_visit_id` names it; with
    none named, any open visit dated today or later). For the outpatient
    lane's duplicate check only."""
    when = now or datetime.now(UTC)
    marks = ", ".join("?" for _ in core.LIVE_STATUSES)
    lines: list[tuple[Any, str | None]] = []
    for r in conn.execute(
        f"SELECT o.id, o.drug_name, o.status, o.is_prn, o.dose, o.route, o.frequency, o.end_at, "
        f"(SELECT e.payload_json FROM med_order_events e WHERE e.med_order_id = o.id "
        f" AND e.kind = 'created' ORDER BY e.id LIMIT 1) AS created_payload "
        f"FROM med_orders o WHERE o.patient_id = ? AND o.encounter_type = 'op' "
        f"AND o.status IN ({marks}) ORDER BY o.created_at, o.id",
        (patient_id, *core.LIVE_STATUSES),
    ).fetchall():
        if exclude_order_id and str(r["id"]) == str(exclude_order_id):
            continue
        if _course_ended(r["end_at"], when):
            continue
        lines.append((r, _line_visit(r["created_payload"])))
    if opd_visit_id:
        current = {str(opd_visit_id)}
    else:
        current = _open_visits_from_today(conn, {v for _, v in lines if v})
    out: list[dict] = []
    for r, visit in lines:
        out.append(
            {
                "drug_name": r["drug_name"],
                "med_order_id": r["id"],
                "medication_id": None,
                "status": r["status"],
                "is_prn": bool(r["is_prn"]),
                "held": r["status"] == "held",
                "dose": r["dose"],
                "route": r["route"],
                "frequency": r["frequency"],
                "setting": "outpatient",
                "kind": "order",
                "opd_visit_id": visit,
                "this_visit": visit is not None and visit in current,
            }
        )
    return out


def _admitted(conn, patient_id: str) -> bool:
    """An active admission (outpatient._active_admission's reading)."""
    return (
        conn.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
            (patient_id,),
        ).fetchone()
        is not None
    )


def row_label(row: Mapping) -> str | None:
    """What a comparison row is, in words, when that matters to the reader of
    an alert: on hold, or as needed. None for an ordinary live drug."""
    if row.get("held"):
        return "on hold"
    if row.get("is_prn"):
        return "as needed"
    return None


# ── The same-ingredient duplicate ─────────────────────────────────────────────


def _norm(name: Any) -> str:
    return " ".join(str(name or "").lower().split())


def _catalogue(conn) -> list[tuple[str, str]]:
    """(name, generic) for every catalogue row, lower case. Every row, not only
    the orderable ones: a safety screen reads them all (W5-C)."""
    try:
        rows = conn.execute("SELECT name, generic_name FROM helix_medication_master").fetchall()
    except Exception:
        # The catalogue only adds names; without it the names are compared
        # as charted, which is what the screen did before.
        return []
    return [(_norm(r["name"]), _norm(r["generic_name"])) for r in rows if _norm(r["name"])]


#: What splits a combination into its parts ('Paracetamol + Codeine',
#: 'Amoxicillin/Clavulanate', 'Calcium with Vitamin D3').
_PART_SPLIT_RE = re.compile(r"[+/,;&]|\b(?:and|with|plus)\b")
#: Words and numbers; a hyphen splits ('co-amoxiclav' reads 'co amoxiclav' on
#: both sides, so it still meets itself).
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
#: A word glued to its strength ('paracetamol500mg'): the letters are the
#: word. Three letters or fewer is a code ('b12', 'd3', 'k1') and stays whole.
_GLUED_RE = re.compile(r"^([a-z]{4,})\d[a-z0-9]*$")
#: A strength: a number, with a unit glued on or not ('500', '500mg', '60k').
_STRENGTH_RE = re.compile(r"^\d+(?:mg|g|gm|gms|kg|mcg|ug|ml|l|iu|u|mu|k|mmol|meq|lakh|lac|x)?$")
_UNIT_WORDS = frozenset(
    {
        "mg",
        "g",
        "gm",
        "gms",
        "gram",
        "grams",
        "kg",
        "mcg",
        "ug",
        "microgram",
        "micrograms",
        "milligram",
        "milligrams",
        "ml",
        "l",
        "litre",
        "litres",
        "liter",
        "liters",
        "iu",
        "u",
        "mu",
        "unit",
        "units",
        "mmol",
        "meq",
        "lakh",
        "lac",
        "million",
        "x",
        "w",
        "v",
        "percent",
    }
)
#: Dosage-form and release words: the same medicine in another form is the
#: same ingredient.
_FORM_WORDS = frozenset(
    {
        "tablet",
        "tablets",
        "tab",
        "tabs",
        "capsule",
        "capsules",
        "cap",
        "caps",
        "injection",
        "injections",
        "inj",
        "infusion",
        "syrup",
        "suspension",
        "susp",
        "solution",
        "soln",
        "oral",
        "cream",
        "ointment",
        "drops",
        "drop",
        "vial",
        "vials",
        "ampoule",
        "ampoules",
        "amp",
        "inhaler",
        "extended",
        "release",
        "sr",
        "er",
        "xr",
        "xl",
        "cr",
        "mr",
        "dt",
        "sachet",
        "sachets",
        "powder",
        "granules",
        "gel",
        "spray",
        "lotion",
        "elixir",
        "emulsion",
        "nebules",
        "respules",
        "lozenge",
        "lozenges",
        "iv",
        "im",
        "sc",
    }
)
#: Cations that are a counter-ion beside a drug ('Losartan Potassium') and the
#: medicine itself on their own ('Potassium Chloride').
_CATION_WORDS = frozenset(
    {
        "sodium",
        "disodium",
        "potassium",
        "dipotassium",
        "calcium",
        "magnesium",
    }
)
#: Anions, acids' salts and hydrates that are never the medicine beside a drug.
_ANION_WORDS = frozenset(
    {
        "hydrochloride",
        "dihydrochloride",
        "hcl",
        "hydrobromide",
        "chloride",
        "bromide",
        "iodide",
        "fluoride",
        "sulphate",
        "sulfate",
        "bisulphate",
        "bisulfate",
        "acetate",
        "phosphate",
        "citrate",
        "maleate",
        "succinate",
        "tartrate",
        "bitartrate",
        "besylate",
        "besilate",
        "mesylate",
        "mesilate",
        "tosylate",
        "fumarate",
        "carbonate",
        "bicarbonate",
        "gluconate",
        "lactate",
        "hydroxide",
        "oxide",
        "trisilicate",
        "glycerophosphate",
        "nitrate",
        "stearate",
        "palmitate",
        "propionate",
        "dipropionate",
        "valerate",
        "furoate",
        "hyclate",
        "estolate",
        "ethylsuccinate",
        "pamoate",
        "embonate",
        "orotate",
        "glycinate",
        "bisglycinate",
        "aspartate",
        "monohydrate",
        "dihydrate",
        "trihydrate",
        "hydrate",
        "anhydrous",
    }
)
_SALT_WORDS = _CATION_WORDS | _ANION_WORDS
#: Endings of a word that may be an anion the list above does not know.
_ANION_ENDINGS = ("ate", "ide", "ite")
#: Words that name a class of medicines, never one ingredient on their own.
_CLASS_WORDS = frozenset(
    {
        "vitamin",
        "vitamins",
        "insulin",
        "insulins",
        "vaccine",
        "vaccines",
        "supplement",
        "supplements",
        "antacid",
        "antacids",
        "electrolyte",
        "electrolytes",
        "mineral",
        "minerals",
    }
)


def _drug_word(word: str) -> bool:
    """A word that can be the medicine beside a salt: not a short code, not a
    class word, not a word that may itself be an anion."""
    return len(word) >= 4 and word not in _CLASS_WORDS and not word.endswith(_ANION_ENDINGS)


def _part_form(words: list[str]) -> list[str]:
    """One part of a name, its salt words left out only when a drug word
    stays beside them; otherwise every word (the salt is the medicine)."""
    core_words = [w for w in words if w not in _SALT_WORDS]
    if any(_drug_word(w) for w in core_words):
        return core_words
    return words


def _ingredient_form(drug_name: str) -> str:
    """The name with its strength, units and dosage-form words left out, and
    its salt words when a drug word stays beside them (_part_form); every
    other word kept, in order. 'Paracetamol 500 mg' and 'Paracetamol 1 g'
    both read 'paracetamol'; 'Losartan Potassium 50 mg' reads 'losartan';
    'Insulin Glargine 100 units/ml' reads 'insulin glargine'; 'Vitamin K 10
    mg' reads 'vitamin k'; 'Sodium Bicarbonate 8.4% w/v' reads 'sodium
    bicarbonate'. Empty when nothing is left."""
    out: list[str] = []
    for piece in _PART_SPLIT_RE.split((drug_name or "").lower()):
        words: list[str] = []
        for token in _TOKEN_RE.findall(piece):
            glued = _GLUED_RE.match(token)
            if glued:
                token = glued.group(1)
            if _STRENGTH_RE.match(token) or token in _UNIT_WORDS or token in _FORM_WORDS:
                continue
            words.append(token)
        for word in _part_form(words):
            if word not in out:
                out.append(word)
    return " ".join(out)


def ingredient_word_set(drug_name: str) -> frozenset[str]:
    """The words of the name's ingredient form (_ingredient_form). The Sage
    lane's exact binding asks whether a name it found only as part of a
    product's name names every one of these (ai_lanes.bind_sage_product):
    'Vitamin K 10mg Injection' gives {'vitamin', 'k'}, so 'Vitamin' does not
    name it."""
    return frozenset(_ingredient_form(drug_name).split())


def _bare(form: str) -> bool:
    """A form made only of class and salt words ('insulin', 'calcium',
    'potassium chloride'): it may resolve through the catalogue by exact
    name only, never a prefix."""
    words = form.split()
    return not words or all(w in _CLASS_WORDS or w in _SALT_WORDS for w in words)


#: What marks a second ingredient in a product's name. A slash is not one:
#: it also writes a strength ('500mg/1mg', '100 units/ml'), and a word after
#: it still shows in the ingredient form.
_COMBINATION_MARK_RE = re.compile(r"[+,;&]|\b(?:and|with|plus)\b")


def _same_product(stem: str, longer: str) -> bool:
    """Does `longer`, a catalogue name that starts with the whole words of
    the charted name `stem`, name the same product? Only when the words it
    adds are a strength, a unit, a dosage-form or release word, or an anion
    beside a drug word, so both read the same ingredient form ('Dolo' and
    'Dolo 650mg Tablet', 'Metoprolol' and 'Metoprolol Succinate 50mg Oral
    Tablet'). Never when it adds another word or a mark of a second
    ingredient: 'Metformin + Glimepiride 500mg/1mg Tablet' is a combination
    that starts with metformin, 'Dolo Cold Tablet' and 'Combiflam Plus' are
    other products under the brand, and a variant word ('Duo', 'Forte') adds
    a second drug on some brands. Nor when it adds a cation word, which after
    a brand may be a second medicine ('Calcium' after a vitamin D3 brand).
    Their generics would join every ingredient of that other product to the
    charted name, and the duplicate rule would then refuse a different
    medicine as the same one."""
    tail = longer[len(stem) :]
    if _COMBINATION_MARK_RE.search(tail):
        return False
    if any(word in _CATION_WORDS for word in _TOKEN_RE.findall(tail)):
        return False
    return _ingredient_form(longer) == _ingredient_form(stem)


def _names(drug_name: str, catalogue: list[tuple[str, str]]) -> list[str]:
    """The name's ingredient form (_ingredient_form; the name as written when
    nothing is left), and the ingredient form of the generic of every
    catalogue row it names by exact name or by a whole-word prefix either way
    ('Dolo 650' and 'Dolo 650 Tablet'). A catalogue name longer than the
    charted one counts only when it names the same product (_same_product):
    'Metformin' never takes the generic of 'Metformin + Glimepiride 500mg/1mg
    Tablet', nor 'Dolo' that of 'Dolo Cold Tablet'. A charted name longer
    than the catalogue's takes that product's generic, which a product named
    after it carries. Never by a first-word prefix alone, which would make
    'Insulin Glargine' resolve to every insulin, and never a prefix from or to
    a bare class or salt word ('Vitamin', 'Calcium'). The name as charted is
    not compared: its salt words would join 'Calcium' to 'Atorvastatin
    Calcium'."""
    want = _norm(drug_name)
    form = _ingredient_form(drug_name) or want
    out = [form] if form else []
    if not want:
        return out
    exact_only = _bare(form)
    for name, generic in catalogue:
        if not generic:
            continue
        hit = name == want or generic == want
        if not hit and not exact_only:
            hit = (name.startswith(want + " ") and _same_product(want, name)) or (
                want.startswith(name + " ") and not _bare(_ingredient_form(name))
            )
        if hit:
            resolved = _ingredient_form(generic) or generic
            if resolved not in out:
                out.append(resolved)
    return out


def _weak(words: list[str]) -> bool:
    """Only class words and short codes ('vitamin k', 'vitamin b12', 'ors'):
    too little to share a substring or a word with another name safely."""
    return all(w in _CLASS_WORDS or len(w) <= 3 for w in words)


def _run_in(words: list[str], other: list[str]) -> bool:
    """`words`, whole and in order, inside `other` ('vitamin d3' inside
    'calcium vitamin d3'; never 'vitamin b1' inside 'vitamin b12')."""
    size = len(words)
    return any(other[i : i + size] == words for i in range(len(other) - size + 1))


def _strong_overlap(a: str, b: str) -> bool:
    """drug_safety_service.names_overlap, less a substring match that runs
    into a number on either side."""
    from services.drug_safety_service import names_overlap

    if not names_overlap(a, b):
        return False
    if a in b.split() or b in a.split():
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    at = long_.find(short)
    while at != -1:
        end = at + len(short)
        before = long_[at - 1] if at > 0 else ""
        after = long_[end] if end < len(long_) else ""
        if not before.isdigit() and not after.isdigit():
            return True
        at = long_.find(short, at + 1)
    return False


def _overlap(a: str, b: str) -> bool:
    """Do two ingredient forms name the same medicine (the module note)?"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    if sorted(wa) == sorted(wb):
        return True
    for mine, other in ((wa, wb), (wb, wa)):
        # A class word names no one medicine: 'insulin' meets only 'insulin'.
        if all(w in _CLASS_WORDS for w in mine):
            continue
        if _weak(mine) and _run_in(mine, other):
            return True
    if _weak(wa) or _weak(wb):
        return False
    return _strong_overlap(a, b)


def _same_ingredient(names_a: Iterable[str], names_b: Iterable[str]) -> bool:
    names_b = list(names_b)
    return any(_overlap(x, y) for x in names_a for y in names_b)


def same_ingredient(drug_a: str, drug_b: str, catalogue: Iterable[tuple[str, str]] = ()) -> bool:
    """Do two drug names name the same ingredient (the module note)? The
    duplicate rule's own test; `catalogue` is (name, generic) pairs in lower
    case, as _catalogue reads them."""
    rows = [(_norm(n), _norm(g)) for n, g in catalogue]
    return _same_ingredient(_names(drug_a, rows), _names(drug_b, rows))


def _status_words(row: Mapping) -> str:
    if row.get("setting") == "outpatient":
        words = (
            "an outpatient prescription on this visit"
            if row.get("this_visit")
            else "an outpatient prescription from another visit"
        )
    elif row.get("kind") == "chart":
        words = "on the chart"
    else:
        words = {
            "prescribed": "prescribed",
            "verified": "checked by a pharmacist",
            "held": "on hold",
        }.get((row.get("status") or ""), row.get("status") or "live")
    if row.get("is_prn") and not row.get("held"):
        words += ", as needed"
    return words


def _describe(row: Mapping) -> str:
    detail = ", ".join(
        str(v).strip()
        for v in (row.get("dose"), row.get("route"), row.get("frequency"))
        if v and str(v).strip()
    )
    return f"{row['drug_name']}" + (f" {detail}" if detail else "") + f" ({_status_words(row)})"


def find_duplicates(
    conn,
    patient_id: str,
    drug_name: str,
    *,
    exclude_med_id: int | None = None,
    exclude_order_id: str | None = None,
    include_outpatient: bool = False,
    opd_visit_id: str | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """The live drugs that name the same ingredient as `drug_name`, each as a
    finding {kind 'duplicate', drug_name, med_order_id, medication_id, status,
    setting, record ('order' or 'chart'), dose, route, frequency, description,
    message, clears_with_reason}. `include_outpatient` is the outpatient
    lane's set (the module note): inpatient rows only while the patient is
    admitted, plus the unended outpatient lines, a line on another visit than
    `opd_visit_id` clearing with a written reason. Reads only; raises on a
    read that fails (the gate then treats the whole screen as unavailable)."""
    if include_outpatient:
        rows = (
            comparison_rows(
                conn, patient_id, exclude_med_id=exclude_med_id, exclude_order_id=exclude_order_id
            )
            if _admitted(conn, patient_id)
            else []
        )
        rows += outpatient_rows(
            conn, patient_id, exclude_order_id=exclude_order_id, now=now, opd_visit_id=opd_visit_id
        )
    else:
        rows = comparison_rows(
            conn, patient_id, exclude_med_id=exclude_med_id, exclude_order_id=exclude_order_id
        )
    if not rows:
        return []
    catalogue = _catalogue(conn)
    mine = _names(drug_name, catalogue)
    found: list[dict] = []
    for row in rows:
        if not _same_ingredient(mine, _names(row["drug_name"], catalogue)):
            continue
        described = _describe(row)
        found.append(
            {
                "kind": "duplicate",
                "drug_name": row["drug_name"],
                "med_order_id": row["med_order_id"],
                "medication_id": row["medication_id"],
                "status": row["status"],
                "setting": row["setting"],
                "record": row["kind"],
                "dose": row["dose"],
                "route": row["route"],
                "frequency": row["frequency"],
                "description": described,
                "message": f"Already ordered: {described}.",
                # Only an outpatient line on another visit (the module note).
                "clears_with_reason": (
                    row["setting"] == "outpatient" and not row.get("this_visit")
                ),
            }
        )
    return found


def rule_for_findings(findings: Iterable[Mapping], rule: str | None) -> str | None:
    """The rule one gate call applies to its findings: the configured rule,
    except that findings which ALL clear with a written reason (outpatient
    lines from another visit) are 'acknowledge' under 'block' too. One
    finding the prescriber could cancel on this visit keeps the block."""
    findings = list(findings)
    if (
        rule == DUPLICATE_RULE_BLOCK
        and findings
        and all(f.get("clears_with_reason") for f in findings)
    ):
        return DUPLICATE_RULE_ACKNOWLEDGE
    return rule


def duplicate_check(findings: Iterable[Mapping], drug_name: str, *, rule: str) -> dict:
    """The gate's duplicate verdict, ridden on safety['duplicate_check'] the
    way the dose guard's rides on safety['dose_check']."""
    findings = [dict(f) for f in findings]
    listed = "; ".join(f.get("description") or str(f.get("drug_name")) for f in findings)
    if (
        rule == DUPLICATE_RULE_ACKNOWLEDGE
        and findings
        and all(f.get("clears_with_reason") for f in findings)
    ):
        message = (
            f"{drug_name} is already prescribed for this patient: {listed}. "
            "Give a reason to prescribe it again, for example a refill of a "
            "continuing medicine."
        )
        decision = "acknowledge"
    elif rule == DUPLICATE_RULE_ACKNOWLEDGE:
        message = (
            f"{drug_name} is already ordered for this patient: {listed}. "
            "Give a reason to order the same medicine again."
        )
        decision = "acknowledge"
    else:
        message = (
            f"{drug_name} is already ordered for this patient: {listed}. "
            "A second order for the same medicine is not allowed."
        )
        decision = "block"
    return {"rule": rule, "decision": decision, "findings": findings, "message": message}


_REASON_EXAMPLE = "for example 'IV while nil by mouth, oral doses not being given'"


def validate_duplicate_reason(reason: str | None) -> str:
    """Clean the reason for a second order of the same medicine, or raise
    ValueError in words the prescriber reads. The same floor as the dose
    override: 'ok' never clears it."""
    from services.drug_safety_service import JUNK_OVERRIDE_REASONS

    cleaned = re.sub(r"\s+", " ", (reason or "").strip())
    if not cleaned:
        raise ValueError("Say why this patient needs a second order for the same medicine.")
    if cleaned.lower() in JUNK_OVERRIDE_REASONS or len(set(cleaned.replace(" ", ""))) <= 1:
        raise ValueError("Give a clinical reason for the second order, " + _REASON_EXAMPLE + ".")
    if len(cleaned) < MIN_DUPLICATE_REASON_CHARS:
        raise ValueError(
            f"The reason must be at least {MIN_DUPLICATE_REASON_CHARS} characters and say why "
            "a second order for the same medicine is right, " + _REASON_EXAMPLE + "."
        )
    return cleaned
