"""The imperative corpus: which Sage turns get a gate, and which get none.

WHAT THIS FILE IS. A clinician can type an instruction into Sage. Two
independent, deterministic mechanisms decide what happens to it before any model
sees it, and this corpus is the table of what each one does:

  * `sage_service.detect_mutation_intent` — the CHART-MUTATION gate. True means
    the turn is buffered end-to-end (`mutation_locked`), nothing streams, and
    the action-state override rewrites the answer.
  * `thinking_mode.classify_floor` — the SAFETY FLOOR. Non-empty means the turn
    goes to the careful 27B even when the clinician asked for fast.

WHY IT EXISTS. `docs/perf/FAST_LANE_MARKER_2026-09-07.md` section 7b recorded
six mutation-shaped imperatives that tripped NEITHER, and left the decision
open. The decision is now made and written up there in full; this corpus is what
it was made against, and it is deliberately readable as a record rather than as a
list of assertions. `tests/test_action_lane_gates.py` asserts on every row and
`dev_tools/check_action_lane_gates.py` prints the same table by hand.

THE SHORT VERSION OF THE RULING, so a row's expectation is not a mystery: the
six stay OUTSIDE the mutation gate (they do not change a chart, and the regex
that would reach them also swallows "how do I book a CT?"), and they are now
INSIDE the safety floor as rule 4, `work_request_imperative`. What made the
decision was a measurement, not an argument. Asked to book, cancel, refer,
request, schedule or record, with no patient in scope: `medgemma:4b` promised
the work and streamed it on 3 of 48 turns ("I will cancel the morning bloods",
"I can schedule physiotherapy for tomorrow"), and `medgemma:27b` on 2 of 30
("I will record a blood pressure of 130/80 mmHg in the patient's chart"). The
careful model is better, not clean, which is why the ruling is BOTH a floor rule
and a forbidden-phrase entry rather than either one.

HOW TO READ A ROW. `(query, mutation, floor_rules)` — the two gates' verdicts on
the BARE query, i.e. no `patient_id`, no chart in the assembled context and no
`QueryClassification`. That is the weakest position any turn can be in, and it is
the position section 1 was found in: in a ward round the chart is open,
`patient_id` is passed to `classify_floor` from the route's
`effective_patient_id`, and rule 1 (`patient_specific`) fires on everything, so
the exposure was always a chart-free Sage rather than a round.

WHEN YOU ADD A ROW. Put it in the section that describes what you expect, run
the dev tool, and paste the verdict you actually get. A row whose expectation was
guessed rather than run is worse than no row: it makes the suite agree with the
guess. The NEGATIVE sections are the important half — a gate is trivially made
sensitive, and the work is in not firing on a question.
"""

# ── 1. THE SIX. Work requests: floored by rule 4, never a mutation ───────────
# Section 7b's list, verbatim, and until 2026-09-07 it tripped nothing at all.
# These are imperatives that ask the HOSPITAL for work (booking, cancelling,
# referring, requesting, scheduling) rather than asking Orb to change a chart.
# No verb here is in `_ADD_VERBS`/`_STOP_VERBS` with a medication object, none
# carries patient deixis, and none names a drug, a dose or an Orb-computed
# score, which is why rules 1 to 3 all stayed quiet and the turn ran on the 4B.
#
# THE RULING (2026-09-07, written up in that section). They stay outside the
# MUTATION gate on purpose: widening `MUTATION_PATTERNS` to reach them would
# assert they change a chart, which is false, and the widening that reaches
# them also swallows the questions in section 4. Two things were added instead,
# and both are measured rather than argued:
#
#   * `thinking_mode` floor rule 4, `work_request_imperative`, which routes an
#     imperative asking for hospital work to the careful model without claiming
#     it is a mutation. It improves the odds: 3 of 48 fast-tier replies promised
#     the work and streamed it, against 2 of 30 careful-tier ones.
#   * The future-tense entries in `routes/sage_routes.FORBIDDEN_PHRASE_PATTERNS`,
#     which delete the promise itself on whichever model makes it, and which are
#     what actually closes it: after them, 0 of 78 across both tiers.
#
# So `mutation` is still False for every row here, and that is the decision.
# The floor column is what changed.
WORK_REQUESTS = [
    ("book an urgent CT head", False, ("work_request_imperative",)),
    ("cancel the morning bloods", False, ("work_request_imperative",)),
    ("put in a referral to cardiology", False, ("work_request_imperative",)),
    ("raise a request for an echo", False, ("work_request_imperative",)),
    ("schedule physiotherapy for tomorrow", False, ("work_request_imperative",)),
    ("record a blood pressure of 130/80", False, ("work_request_imperative",)),
]

# Same six, one step further from a gate: politeness and request markers do not
# change the verdict either way, so they are here to stop a future widening from
# being tested only against the bare imperative.
WORK_REQUESTS_POLITE = [
    ("please book an urgent CT head", False, ("work_request_imperative",)),
    ("can you book an urgent CT head", False, ("work_request_imperative",)),
    ("go ahead and cancel the morning bloods", False, ("work_request_imperative",)),
    ("refer to cardiology", False, ("work_request_imperative",)),
    ("request an echo", False, ("work_request_imperative",)),
    ("arrange an echo", False, ("work_request_imperative",)),
    ("book a bed on the ward", False, ("work_request_imperative",)),
    ("chase the CT report", False, ("work_request_imperative",)),
    ("call the on-call registrar", False, ("work_request_imperative",)),
]

# ── 2. THE DETERMINISTIC GATE FIRES ──────────────────────────────────────────
# The contrast that makes section 7b a real hole rather than a naming quibble:
# these are the same grammatical shape and they DO lock. What separates them is
# vocabulary — an `_ADD_VERBS` head, a medication object, or chart housekeeping.
GATED_BY_MUTATION = [
    ("order a chest X-ray", True, ()),
    ("add a note that the family was updated", True, ()),
    ("start the sepsis bundle", True, ("orb_computed_score",)),
    ("stop the heparin for this patient", True,
     ("patient_specific", "dosing_or_medication")),
    ("add paracetamol 500mg for this patient", True,
     ("patient_specific", "dosing_or_medication")),
    ("discontinue the metoprolol", True, ("dosing_or_medication",)),
    ("update the chart", True, ()),
    ("place an order", True, ()),
]

# ── 3. THE SAFETY FLOOR FIRES ON TWO RULES ───────────────────────────────────
# The six with a patient anywhere in the sentence. This is the ward-round case,
# and it is why the ungated window was always narrower than "six phrasings":
# rule 1 catches deixis, and in the running product it also catches the open
# chart via `patient_id` and the `PATIENT:` marker in the assembled context, so
# these were routed to the careful model even before rule 4 existed. Kept as a
# section because rule 1 is the one that survives if rule 4 is ever narrowed.
_P_W = ("patient_specific", "work_request_imperative")
GATED_BY_FLOOR = [
    ("book an urgent CT head for this patient", False, _P_W),
    ("cancel his morning bloods", False, _P_W),
    ("refer him to cardiology", False, _P_W),
    ("schedule physiotherapy for tomorrow for this patient", False, _P_W),
    ("record his blood pressure as 130/80", False, _P_W),
    ("discharge him today", False, _P_W),
    ("admit him to ICU", False, _P_W),
    ("consent him for surgery", False, _P_W),
    ("book an mri for the patient in bed 4", False, _P_W),
]

# ── 4. QUESTIONS THAT MUST NOT BE READ AS ORDERS ─────────────────────────────
# THE BOUND ON ANY FUTURE WIDENING. `sage_service` warns above
# `MUTATION_PATTERNS` that over-triggering turns a plain question into a slow
# non-answer; this is the list that warning is about. Every one of these shares
# a verb with section 1, and every one must stay `mutation=False`.
#
# A floor hit on a question is NOT a failure — it costs the clinician seconds
# and buys them the careful model — so the floor column here records what
# happens rather than demanding silence.
QUESTIONS_MUST_NOT_LOCK = [
    ("when should you stop heparin?", False, ("dosing_or_medication",)),
    ("when do you book a CT head in head injury?", False, ()),
    ("how do I book a CT?", False, ()),
    ("what is the referral pathway to cardiology?", False, ()),
    ("should I request an echo here?", False, ()),
    ("when should physiotherapy be scheduled after a stroke?", False, ()),
    ("what blood pressure counts as hypertensive urgency?", False, ()),
    ("who cancels the morning bloods if the patient is fasting?", False, ()),
    ("does a CT head need to be booked before the MRI", False, ()),
    ("what is hypoxia", False, ()),
    ("can you explain the NEWS2 escalation criteria", False,
     ("orb_computed_score",)),
    ("why did this escalate?", False, ("orb_computed_score",)),
    ("tell me about the sepsis bundle", False, ("orb_computed_score",)),
]

# ── 5. STATEMENTS THAT ARE NOT ORDERS ────────────────────────────────────────
# THE SHARPER HALF OF THE BOUND, and the reason section 4 alone is not enough.
# `detect_mutation_intent` vetoes anything `_is_question_form` recognises BEFORE
# it looks at a single pattern, so every row in section 4 is protected by the
# question lead ("when", "how", "who", a trailing "?") no matter how wide the
# regex gets. A widening test built only from those rows cannot fail, which
# makes it worse than no test.
#
# These have no interrogative lead and no question mark. They are the gerund and
# the declarative: a clinician reading a policy back, thinking aloud, or
# dictating. Nothing here is an instruction, and the vocabulary widening that
# would reach section 1 reaches all of them. Verified 2026-09-07 that
# `_is_question_form` is False for every row, so the veto is not doing the work
# and the patterns are.
STATEMENTS_MUST_NOT_LOCK = [
    ("physiotherapy is usually scheduled within 24 hours of a stroke", False, ()),
    ("booking a CT head goes through the radiology desk", False, ()),
    ("referral to cardiology is the usual next step here", False, ()),
    ("cancelling bloods needs the phlebotomy desk told before 07:00", False, ()),
    ("the morning bloods are normally cancelled if the patient is fasting",
     False, ()),
    ("recording a manual blood pressure is more reliable in AF", False, ()),
    ("a request for an echo usually takes two days", False, ()),
    ("scheduling physio early improves mobility", False, ()),
    ("not sure whether to cancel the morning bloods", False, ()),
]

#: Every row, for a caller that wants the whole table in one pass.
ALL_ROWS = (
    [("the six (7b)", *r) for r in WORK_REQUESTS]
    + [("work request", *r) for r in WORK_REQUESTS_POLITE]
    + [("mutation gate", *r) for r in GATED_BY_MUTATION]
    + [("safety floor", *r) for r in GATED_BY_FLOOR]
    + [("question", *r) for r in QUESTIONS_MUST_NOT_LOCK]
    + [("statement", *r) for r in STATEMENTS_MUST_NOT_LOCK]
)
