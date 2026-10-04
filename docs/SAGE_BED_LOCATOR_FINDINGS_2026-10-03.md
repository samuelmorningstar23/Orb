# Sage bed and ward lookup: findings, 3 October 2026

Status: open. Three fixes were built on 3 and 4 October. All three failed independent review for wrong-patient paths and all three were backed out. server/services/sage_service.py and server/routes/sage_routes.py are byte-identical to before (SHA-256 af7be48c... and 38aeb70a...). R026 is RULED (a) with D2 to D4 and Q1 to Q3; see "Attempt 2" and "Attempt 3" at the end.

## The defect today

Sage decides which patient a question is about partly by bed and ward labels. `_match_patients` and `_match_out_of_scope` squash the question and each label down to letters and digits and test for a substring (`norm_bed in norm_query`, `norm_ward in norm_query`).

1. **Bed lookup fails on Orb's own bed naming.** Ward "GM-1" ("gm1") is a substring of bed "GM-101" ("gm101"). "Summarise bed GM-103" matches all five GM-1 patients, and Sage asks the doctor to pick one. Measured on the dev database copy.
2. **A short label matches characters that are there by accident.** Bed "A1" matches "HbA1c". Bed "12" matches "potassium after 12 hours". Bed "B12" matches "is vitamin b12 low?". When exactly one patient matches this way, Sage attaches that patient's chart to a question that was not about them. That is a wrong-patient path, though it needs short bed labels to happen.
3. **Another department's short bed answers for a bed that does not exist.** With an out-of-department patient in bed "9", "who is in bed 9917" gets "that patient is outside your department". This is also why tests/test_sage_patient_phrasing.py::test_a_bed_alone_is_enough_to_warn fails in some full-suite runs: another test leaves a patient in a short bed.

## Why the first fix was backed out

The first fix matched labels as whole tokens and required the word "bed" before a label that is only digits. It fixed all three cases above. But matching less means more questions end with no match, and **no match does not stop Sage**. When `_match_patients` returns nothing and `_names_a_specific_patient(...)` is False, the sticky anchor attaches the open chart, or the patient from the conversation history. The old over-matching usually ended in DISAMBIGUATE (ask the doctor), which is safe. The new matching turned some of those into "answer from the chart that happens to be open".

Measured by the reviewer through `get_relevant_context`, with numeric beds 7, 12 and 14 and the chart for bed 7 open:

| Question | Old code | First fix | Wanted |
|---|---|---|---|
| compare beds 12 and 14 | DISAMBIGUATE | bed 7's chart | beds 12 and 14 |
| cot 12 lactate? | bed 12 | bed 7's chart | bed 12 |
| bed num 12 lactate? | bed 12 | bed 7's chart | bed 12 |
| what is the potassium of the patient in 12? | bed 12 | bed 7's chart | bed 12 |
| bed 12 and 14 potassium? | DISAMBIGUATE | bed 12 only | both beds |
| Summarise RB-106 (no such bed, ward RB-1 exists, RB-102 open) | DISAMBIGUATE | RB-102's chart | NO MATCHING PATIENT |

The pronoun branch has the same hole under both old and new code: "the potassium of the patient in bed 15" (no such bed) answers from the open chart.

## What a correct fix needs

1. **The gate first, then the matcher.** `_names_a_specific_patient` must recognise every locator phrasing the matcher understands, plus bed-shaped tokens built from the letter prefixes of beds on record (gm, ccu and so on). Do not use a generic letter-plus-digit rule, or "SpO2" and "HbA1c" would lose the open chart. An unresolved locator must suppress the anchor, run the out-of-department check, and return NO MATCHING PATIENT.
2. **A wider locator grammar.** Keywords `bed`, `beds`, `cot`, `bay`; `no`, `no.`, `num`, `number`, `#`; number lists ("beds 12 and 14", "12, 14"); "N number bed"; "the patient in N".
3. **Whole-token matching** for labels, with separators inside a label interchangeable when the letter run has two or more letters (GM101 typed as "gm 101"), and only an optional hyphen after a single letter ("a 1" is ordinary text).
4. **Beds before wards.** Remove a matched bed's text before ward matching, so "CCU-01" names one patient. A ward label ending in a letter must not match when digits follow (ICU in "ICU-06" when ICU-06 is not in scope).
5. **Short letter-plus-digit labels** (3 characters or fewer, like B12, D3, T4, V5) need the keyword, or a check against `_is_clinical_vocabulary`.
6. **Ward and bed together** ("ward 5 bed 12") should match the intersection.
7. **Regex hygiene.** The keyword prefix must not use two overlapping `*` groups (quadratic backtracking on long whitespace). Use `bed[\s#:.\-]*(?:(?:no\.?|num(?:ber)?)[\s#:.\-]*)?`.
8. **Leading zeros:** decide whether "bed 9" finds label "09".

## Tests the fix must carry

Every case in the table above, driven through `get_relevant_context` with a `patient_id` set (so the open-chart path is exercised), and the matcher-level cases from the first attempt:

- "Summarise bed GM-103" with five GM-1 patients finds only GM-103; "how is GM-1011" finds nobody; "Summarise CCU-01" finds one patient, not all of CCU; "How is ward GM-1 doing?" still finds all of GM-1.
- With beds "9", "17", "99": "Who is in bed 9917" finds nobody; "Is a NEWS2 of 9 reassuring?" finds nobody; "how is bed 9" finds bed 9; "bed no. 17 please" finds bed 17.
- With bed "A1": "what is a normal HbA1c?" and "is a 1 point rise significant" find nobody; "summarise A1" and "summarise bed A-1" find it.
- With another department's patients in beds "9" and "17": "who is in bed 9917" is not an out-of-department hit; "who is in bed 9?" is.

Run with the existing guards: tests/test_sage_patient_phrasing.py, test_sage_department_scope.py, test_patient_scope_routes.py, test_sage_sticky_anchor.py, test_sage_history_patient_name.py.

## Attempt 2 (3 October, evening): built to the ruling, failed review, backed out

Samuel ruled R026 (a) with D2 (leading zeros ignored), D3 (one missing bed in a list = no match for the whole turn) and D4 (a clinical-code bed label without "bed" makes Sage ask). Attempt 2 built one shared "bed reference" reader for the matcher, the open-chart gate and the history anchor, with an early NO MATCHING exit. 83 new tests passed, as did the Sage guard suites (1,674). An independent reviewer then broke it through `get_relevant_context` with a chart open:

- Wrong patient, regressions against the old code. Bare numbers and phrasings the reader did not know fell to the open chart: "how is 12?", "12's potassium?", "what about 12?", "compare 12 and 14", "room 12", "cubicle 12", "pt 12", "12 wala patient", "bed-12" typed with an en dash or non-breaking hyphen. Stored label shapes it could not express: "MW/12", "GW.12", "CARDIO-12", "Room 204", "12A". A bed label equal to another department's ward label ("HDU-2"). The old substring matcher found all of these.
- Wrong bed: "bed twenty one" read as bed 20, "beds 1-2" as bed 12; "beds 12 to 14" silently dropped bed 14.
- Chart lost or false out-of-department denials (each one writes a security audit event against the clinician): "is the patient in 2nd degree heart block?", "patient in 2019", "1:1 nursing", "2:1 block"; "A1c", "D5W", "H2O", "ANC 500", "COVID-19" whenever any ward in the hospital used that letter or prefix; "ward 6 bed 12" denied because another ward has a bed 12.
- Performance: 57.7 s end to end on a 50,000-character input (old code 0.56 s), and a 4,400-digit number crashed the turn and every later follow-up in that conversation.

The lesson: a closed grammar of "what a bed reference looks like" always has holes, and the open-chart fallback turns every hole into a wrong-patient answer. Attempt 3 must find beds from the labels actually on the ward (any shape), and when it is unsure it must ask, never fall back. Attempt 2's code, tests, the reviewer's probes and outputs: docs/archive/bed-locator-attempt2-2026-10-03/ (attempt2.patch is the exact diff).

## Attempt 3 (4 October): beds found from the labels on the ward, failed review, backed out

Design (docs/SAGE_BED_LOCATOR_PLAN_2026-10-03.md, "Attempt 3"): every bed label on the doctor's ward is looked for as a whole label in whatever shape the hospital stores it; each hit is certain, plainly not a bed, or unsure, and unsure means Sage asks (rulings Q1, D4). 208 new tests and 1,954 guard tests passed. It fixed most of attempt 2's findings: bare numbers ask instead of using the open chart, every stored shape (MW/12, GW.12, Room 204, ICU Bed 3, 12A, HDU-2) is found, unicode dashes, spoken numbers, ranges, A1c / D5W / ANC 500 / COVID-19 keep the chart, no int crash, the 57 s case runs in 0.55 s.

The fresh reviewer still found wrong-patient paths:
- "bed 12 HR 140", "bed 12 day 3 of ceftriaxone", "bed 12 h/o CKD", "bed 12 d/c today?", "bed 12 dose of vanc?" answered from the OPEN chart (and "bed 45 HR 140" with no bed 45 too). A guard meant for "bed 2 days ago" dropped the bed reference whenever a unit-like word followed. This is ordinary handover phrasing.
- "bed 12 ward 6" (no connector) read "12 ward" as ward 12 and answered from ward 5's bed 12.
- "ABG from bed 45", "what happened to bed 45 overnight?", "compare bed 12 to bed 45" were treated as destinations and fell to the open chart (or dropped bed 45).
- In the history anchor, a turn Sage had only ASKED about ("12 lead ECG done?") bound the thread to bed 12.
- Bed-word spellings the reader missed: "bed nos 45", "bed nr 45", "bed n° 45", "bed (45)", "bed = 45", "bed 45th", "fourty", fullwidth digits, thin and zero-width spaces, 7+ digit numbers.
- "the patient at 4" (a time) and "the patient on 2 pressors" (a count) read as certain beds.

And three further problems:
- The ask is not enforced. The chat route has NO handling for DISAMBIGUATE: the candidate list goes to the model under the general-question prompt ("no patient chart attached") with no instruction to ask. In a replay the 27B answered generically with invented citations. This also applies to today's name disambiguation (two patients named Nair).
- "Bed 12, 65/M, k/c/o DM ..." (the standard Indian case-presentation opening) became no-match: the list reader continued into "65/M".
- False out-of-department denials (each one a security audit row): "is there an ICU 1 bed free?", "she was in ICU 2 nights ago".
- Ward and bed pairs repeated were quadratic (4.2 s at 50 KB in the locator alone).

The cost of ruling Q1, measured by the reviewer on 54 realistic questions with numbers in them, on a ward numbered 1 to 30 with bed 7 open: the new code asked "did you mean bed N?" on 33 (61%), kept the open chart on 19, and attached a wrong chart on 0. The old code attached a DIFFERENT bed's chart on 28 of the 54. On Orb's own naming (GM-501 to GM-530) the new code asked on 0%. The asks came from: 12-lead ECG, lab words not on the list, doses written without units ("metoprolol 25 bd"), counts ("2 sets of cultures"), times ("review at 5") and an age.

Saved: docs/archive/bed-locator-attempt3-2026-10-04/ (code, tests, the reviewer's probes and outputs, attempt3_*.patch).

