# Sage bed and ward lookup: findings, 3 October 2026

Status: open. Five fixes were built on 3 and 4 October. All five failed independent review for wrong-patient paths and all five were backed out; the back-out was committed (Samuel's instruction, 4 October), which also removed attempt 4's code that an automatic snapshot (5d4f104) had committed while it was under review. Bed matching in main is the original substring scan. R026 is RULED (a) with D2 to D4, Q1 to Q3 and A to F.

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

## Attempt 4 (4 October): attempt 3 plus its fixes, and the server writes the question; failed review, backed out

320 new tests and 2,066 guard tests passed. On realistic traffic it was the best yet. The reviewer's tables, ward numbered 1 to 30, bed 7 open:

| Question set | Attempt 4: asked / kept open chart / WRONG chart | Old code: WRONG chart |
|---|---|---|
| Attempt 3's 54 questions (attempt 4 was tuned on these) | 0 / 49 / 0 | 28 |
| 50 new questions | 6 / 43 / 0 | about 19 |
| Both sets on GM-501 style labels | 0 asks, 0 wrong | n/a |

It fixed nearly everything the two earlier reviewers found. The reviewer still found wrong-patient paths:
- The which-patient turn was never saved (the route returns before the try/finally that saves turns, like the out-of-department answer), so in production the "ask again" rule never saw it: "yes" after "how is 12?" answered from the open chart. The unit tests passed only because they supplied history by hand.
- Replies after the question that name nobody but missed the reply pattern ("potassium?", "neither", "go ahead") went to the open chart; "not this patient" read as "this patient".
- A REGRESSION in order phrasing: a bed number after a drug or lab word and "to / for / of / at" was read as a value, so "give lasix to 12", "hold metformin for 12", "what is the potassium of 12?", "can you look at 12?" went to the open chart (the order drafted on bed 7). The old substring matcher found bed 12 for these.
- "bed sx 17" (another department's letters after a bed word) and "bed 12-3 days post-op" fell to the open chart; Devanagari and Arabic-Indic digits were not read; "out of bed 2nd day post-op" attached bed 2; "compare bed 12 with 45" dropped bed 45.
- "bed 12 65M fever" (no comma) and "bed 12 2 PRBC given" became no-match for a real bed.
- Hyphenated codes of another department in transfer phrasing ("transferred from ICU-03 yesterday", "is HDU-2 free?") were refused as out-of-department, each with a security audit row (a literal reading of Q2; may need a ruling).
- The route appended the internal "ACTIVE VIEW ... viewing patient <name>" line to the server-written question, so the clinician saw it (the same bug class fixed for the out-of-department answer on 3 August).
- The voice lane passes no history, so the ask-again rule cannot run there, and ruling A was not applied to voice.
- The question cache pinned up to 457 MB with eight 512 KB questions.
- Ward-level questions ("how is ward 5 doing?") now got the server's which-patient list instead of an answer, and two patients cannot be compared ("compare beds 12 and 14" asks).

Saved: docs/archive/bed-locator-attempt4-2026-10-04/ (code, tests, reviewer probes and outputs, attempt4_*.patch).

## Attempt 5 (4 October): attempt 4 plus its fixes and rulings E and F; under review

Built on attempt 4. Every attempt 4 finding has a test (412 in tests/test_sage_bed_locator.py; 319 of them fail on the code before this work):
- The which-patient turn is SAVED by the route, so the next turn knows it followed the question. Any reply that names nobody is asked once more; a reply that is only a bed ("12") is that bed; "this patient" is the open chart; after one re-ask the next unnamed turn is answered from the open chart, labelled.
- A bed number right after to / for / of / at / in / on / with is a patient, not a value ("give lasix to 12", "potassium of 12", "look at 12"), except a change ("dropped from 14 to 12"), a scored value with an article ("a GCS of 12") or a time ("review at 3").
- The screen line never reaches the which-patient question; voice speaks the server's question and the patient label, and passes its conversation to retrieval.
- Unicode digits, "bed 12-3 days", "out of bed 2nd day", "compare bed 12 with 45", "bed 12 65M", "bed sx 17", more everyday counts.
- A ward on its own is answered by the ward census; "should she go to CCU?" keeps the chart.
- Ruling E: an answer from a chart the clinician did not name starts with "About <name>, bed <bed>:" (chat and voice), and an order that names no patient asks which patient before anything is drafted.
- Ruling F: a bed or code in a transfer or place phrase keeps the open chart and writes no security event.
- Long questions are tokenized once per turn and the caches are emptied when the turn ends.

Guard suites: 2,414 passed across 49 files (the one order-dependent failure was a test patient named "Patient RBAC Test" matching the word "patient"; that case was rephrased). The name-matcher work's 512 KB timing tests pass with it (1.48 s against a 3 s budget).

It was backed out once when work stopped before review, then re-applied unchanged for the review.

### Attempt 5 review (4 October): failed, backed out

On everyday questions it was the best attempt: 0 wrong charts in 134 questions (the old code: 28 of 54, 24 of 50, 12 of 30), asking 0 to 3% of the time. Every attempt 4 finding the reviewer re-ran was fixed. It still failed:
- Wrong chart with NO label: "head end of bed 30-45 degrees", "bed 30-45° up", "raise the foot end of bed 15 cm" read as certain beds 30 and 15 (the "bed 12-3 days" rule dropped "-45" and left "30" certain).
- Ruling F's place rule swallowed ordinary references: "is bed 12 ready for discharge?", "I'm going to bed 12 now, potassium?", "she was in bed 12 last night, how is she now?" were answered from the open chart (labelled); "is GM-545 ready for discharge?" got the open chart instead of no match.
- Ruling E did not hold in practice: the label was a separate streamed event, but the screen replaces the streamed text with the structured answer when it arrives (src/pages/panels/SagePanel.tsx, src/components/sage/SageMessage.tsx), and the saved turn and provenance lacked it; the reviewer read this from the UI code, not a browser. And an order could still be drafted on the open chart: after "give him lasix 40 iv" got the order question, "yes" or "go ahead" drafted it on bed 7, because the ask-again rule re-reads the previous USER message and an unnamed order names nobody. "give him lasix", then "12", then "yes", then "yes" drafted it on bed 7. detect_mutation_intent misses 21 of 30 order shorthands ("inj lasix 40 iv stat", "transfuse 2 units prbc"). "not for this patient" counted as "this patient".
- False no-match for real beds from closed word lists ("bed 12 2 vomits", "Bed 12, 65 male, fever"), false out-of-department refusals on transfer phrasing the place list missed ("back from ICU-03", "refer her to ICU-03?"), the history anchor binding an older patient after a turn the reader could not resolve, spurious which-patient questions after a ward census answer, and a quadratic-in-beds case (4.5 s on 480 KB of "gm 512" on a 60-bed ward against a 3 s budget).
- Ruling E asked on 70% of 20 realistic ward-round orders typed with a chart open.

The pattern across five reviews: every closed list (bed words, units, counts, place verbs, order shorthands, reply phrasings) leaves a tail, and each tail is a wrong-patient path or a false refusal. The probes are in docs/archive/bed-locator-attempt5-2026-10-04/probes/.

