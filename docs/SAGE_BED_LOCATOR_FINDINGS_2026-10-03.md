# Sage bed and ward lookup: findings, 3 October 2026

Status: open. Two fixes were built on 3 October. Both failed independent review for wrong-patient paths and both were backed out the same evening. The code in server/services/sage_service.py is byte-identical to before (SHA-256 af7be48c...). R026 is now RULED (a); see the section "Attempt 2" at the end and the revised design in docs/SAGE_BED_LOCATOR_PLAN_2026-10-03.md.

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

