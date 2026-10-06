# Sage bed and ward lookup: findings, 3 October 2026

Status: open. Eight fixes were built between 3 and 6 October. All eight failed independent review and were backed out; main has the original substring bed matching (plus the shared ward rule, services/ward_reference.py). R026 is RULED (a) with D2 to D4, Q1 to Q3, A to F, G (the shipping bar) and H (orders). See "Attempt 8 review" for the closest result so far.

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

## Attempt 6 review (4 October): judged against ruling G, did not ship

Ruling G (Samuel): ship when (1) no wrong-patient path the old code got right, (2) fewer wrong charts than the old code on realistic questions, (3) every remaining miss visibly labelled. Ruling H: ask before an order only when it holds a bed number or follows the which-patient question.

Attempt 6 met (2) by a wide margin and most of (3): across all the realistic sets (184 questions and 40 orders on a numbered ward and on Orb's GM naming) it put 7 wrong charts in front of the model, all labelled; the old code put 77, none labelled. The label now reaches the finished answer and the saved turn; the server-written question is saved and re-asked; speed is within budget (worst 1.57 s on 512 KB).

It failed (1) and (3) on two classes:
- Number words after a bed word or "patient in" became certain beds, unlabelled, where the old code answered correctly from the open chart: "can she stay in bed one more day?" opened bed 1, "is the patient in two minds about surgery?" bed 2, "the patient in four point restraints" bed 4 (17 phrasings). With an order in the same turn, the order was drafted on bed 1. Also a bed word plus a number and a singular or unlisted unit ("raise bed 15 cm", "keep her in bed 30 min") read as certain where the old code asked.
- A leading bed number or code followed by a route, unit, count or sex word was thrown away as a value and the open chart answered (labelled): "12 iv lasix 40 stat" and "GM-512 iv lasix 40 stat" drafted the order on the open patient where the old code asked (ruling H broken); "GM-512 HR 140", "GM-512 day 3 of tazo" and 16 numeric shorthands likewise.
Also: false out-of-department refusals from a too-broad reach list ("show me the ICU-02 notes for her"); "bed 12, 45 potassium?" dropped bed 45; the label was lost on reload for fast-contract ACT| answers; voice cannot run the ask-again rule because the voice client sends no conversation history.

Saved with the reviewer's probes in docs/archive/bed-locator-attempt6-2026-10-04/.

## Attempt 7 review (4 October): judged against ruling G, did not ship

Attempt 7 was attempt 6 plus fixes for both of its blocking classes (docs/SAGE_BED_LOCATOR_PLAN_2026-10-03.md, "Attempt 7"). 633 locator tests (472 fail on the old code) and 2,635 guard tests passed. The one timing failure in the guard run was machine load: the same test passed alone in 1.3 s.

The reviewer confirmed every attempt 6 finding fixed. On realistic traffic it is far ahead of the old code: across 234 questions and orders on a numbered ward it put 2 wrong charts in front of the model (both unlabelled, both from B1 below); the old code put 91. On Orb's GM naming: 0 and 0.

It failed items 1 and 3 of the bar on three classes. In each, the new code attached a different patient's chart with no label where the old code asked. The first two were already in attempt 6; review 6 did not probe them.
- B1. A bed in ANOTHER unit, written with a bed word, read as a certain bed on the doctor's own ward: "transferred from ICU bed 12, still on noradrenaline?", "transferred from ward 6 bed 12 yesterday", "came from ICU bed 12 this morning", "ex-MICU bed 14", "bed 12 in ICU is septic, can we take her?", and census talk ("ICU beds 12, any free tonight?"). 30 phrasings where the old code asked. With an order in the turn ("transferred from ICU bed 12, start lasix 40 iv") the order was drafted on our bed 12's patient. The place and destination rules only fire when "from" or "to" sits right before the bed word; a unit name in between defeats them. On Orb's GM naming the same phrasings are no match: safe, but a false no-match.
- B2. A bed word, a number and a unit word not on attempt 7's list: "keep her flat in bed 12 hr after the angio", "flat in bed 24 hr post LP?", "raise the bed 15 cms", "bed 12 noon onwards nil by mouth?", "bed 10 o clock sedation?". 17 phrasings where the old code asked; orders drafted on that bed. "hr" was deliberately left off the list because "bed 12 HR 140" is bed 12.
- B3 (new in attempt 7, rare). A number word ending the clause after a separator: "pillows in bed: two" opened bed 2.
Also: whitespace runs are quadratic (15 s for "how is 12" plus 40,000 spaces; attempt 7 made it about 5 times worse with `\s*-?\s*` patterns); "Bed 12, 56, k/c/o DM" (age first, no sex) is no match where the old code asked; a spelled missing bed followed by more words ("bed forty five potassium?") gets the labelled open chart, not no match; a reloaded order turn shows no label (/history shows the stored safe answer).

Backed out by reverse patch the same evening; the three files are byte-identical to the committed code. Code, tests, patches, the author's and the reviewer's probes: docs/archive/bed-locator-attempt7-2026-10-04/ (probes_review7/ is the reviewer's; run.sh runs them).

### Why seven attempts failed the same way

Every attempt from 3 on put its closed lists on the NOT-A-BED side: units, counts, positions, place verbs and route words that make a number "not a bed" or "unsure". Anything missing from those lists stays a CERTAIN bed, so each word a reviewer finds that is not on a list ("hr", "cms", "noon", "ICU" before "bed") is a wrong chart, and the old code, which asks about nearly every digit, "got it right". Adding words shortens the tail but never closes it.

The way out is to move the closed list to the CERTAIN side: a bed reference is certain only in shapes Sage recognises (the bed phrase is the subject of the question, "bed 12?", "how is bed 12", "bed 12 potassium?", "bed 12 HR 140", a list of beds), and every other shape is unsure, which means Sage asks when that bed is occupied and treats it as text when it is not. Then each missing word lands on "ask", which the bar counts as right wherever the old code asked, and never on a wrong chart. The cost is more questions on unusual phrasings, which has to be measured on the same realistic sets before it is built.

## Attempt 8 review (6 October): the closed list on the certain side; did not ship

Attempt 8 moved the closed lists to the CERTAIN side (plan, "Attempt 8"), built on the ward-matcher work's shared ward rule. 716 locator tests (514 fail on the old code) and 2,800 guard tests passed; every attempt 7 blocking row asks or keeps the chart; whitespace runs are linear (0.02 s for 40,000 spaces); reloaded order turns keep the label. On realistic traffic: 3 wrong charts in 342 numbered-ward questions and orders against the old code's 103, and 0 on Orb's GM naming.

It failed items 1 and 3 of the bar: about 71 attack phrasings on a numbered ward where the old code asked and attempt 8 attached another patient's chart unlabelled, and every order tried in those shapes (11 of 11) was drafted on that patient. The certain-side lists still hold words with other meanings:
- B4: a strong lead-in used in a sense that does not name the patient: "she was given the insulin meant for bed 12", "blood for bed 12 was hung on her by mistake", "the visitor of bed 12 gave her food", "she had bed 12 last admission", "hold bed 12 for her", "ICU has bed 12 free, can we shift her?".
- B5: a joining word plus a recognised continuation: "she was in contact with bed 12, TB positive", "did she catch the flu from bed 12?", "bed 12 was shifted to ICU last night, how is she doing there?", "Came from ICU. Bed 12, on norad, wean now?".
- B6: a relative or a measure plus in / on: "her husband in bed 12 has covid, should she be tested?", "her sugar in bed 12 was 300 before transfer".
- B7: another unit's name before a bracket, colon or comma: "transferred from ICU (bed 12) yesterday", "ICU referral: bed 12, 60M, septic shock, can we accept?".
- B8: our own ward written beside a PAST bed: "she was in ward 6 bed 12 till yesterday, old cultures?".
- B9: "the patient in N" never checked what followed: "will review the patient in 20, what should I check first?", "the patient in 12 noon slot for echo".
Also: "G.M.-103", "GM - 103" (dots or spaced dashes) answered from the open chart, labelled, where the old code found GM-103; a 60-bed ward storing "ICU Bed N", repeated to 512 KB, took 4 to 7 s (also in attempt 7). Attempt 7's code gives the same wrong chart on the B4 to B6 rows tested; reviews 6 and 7 never probed them.

The reviewer's pointer, checked against its own data: 91 of the 115 blocking rows contain she, her, he, him, his or their (the clinician is talking about the OPEN patient and the bed is someone in relation to her), and none of the 90 correct certain other-bed answers across the realistic sets contains one. The other 24: B7, B9, one-letter codes ("K4 is low, replace?", old code the same), dotted GM codes, "swapped from bed 12".

Backed out by reverse patch on 6 October; the four files are byte-identical to HEAD (d2531f8). Code, tests, patches and both probe sets: docs/archive/bed-locator-attempt8-2026-10-04/ (probes_review8/ is the reviewer's).
