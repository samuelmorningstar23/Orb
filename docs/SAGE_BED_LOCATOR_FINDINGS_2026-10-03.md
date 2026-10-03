# Sage bed and ward lookup: findings, 3 October 2026

Status: open. A fix was built on 3 October, failed independent review for a wrong-patient path, and was backed out the same hour. The code in server/services/sage_service.py is exactly as it was before. This note is the starting point for the next attempt. Ruling R026 in docs/RULINGS_NEEDED_2026-10.md.

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
