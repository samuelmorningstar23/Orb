# Sage bed and ward lookup: findings, 3 October 2026

Status: open. Twelve fixes were built between 3 and 6 October. All twelve failed independent review and were backed out; main has the original substring bed matching (plus the shared ward rule, services/ward_reference.py). R026 is RULED (a) with D2 to D4, Q1 to Q3, A to F, G (the shipping bar) and H (orders), and on 6 October Samuel ruled that every bed is confirmed before Sage opens its chart. See "Attempt 11 review": every failure since attempt 10 came through an EXCEPTION to that rule; attempt 12 removed the exceptions and failed on the conversation logic around the rule ("Attempt 12 review").

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

## Attempt 9 review (6 October): confirm another bed while a chart is open; did not ship

Attempt 9 (plan, "Attempt 9") kept attempt 8's reader and added Samuel's structural rule: while a chart or the conversation's patient is open, a bed that is not that patient is asked about, and "yes" or "12" confirms. With a chart open it held completely: every earlier probe set (46 files) gave 0 regressions and 0 unlabelled wrong charts, and the realistic sets gave 0 wrong charts in 668 numbered-ward rows against the old code's 221. Asks: 25% overall with a chart open and 10% with nothing open, against the old code's 57%. "yes" opened the right chart, labelled, live and on reload, and an order after "yes" was drafted on the confirmed patient.

It failed items 1 and 3 on the lanes the rule did not cover:
- B-A, nothing open: attempt 8's reader decided, and the shapes that broke it came back written with nouns instead of she or he ("the husband in bed 12 has covid, should the wife be tested?", "ICU referral: bed 12, 60M, septic shock, can we accept?", "transferred from ICU (bed 12) yesterday", "same plan as for bed 12 for the new admission?"). About 26 phrasings where the old code asked opened bed 12 unlabelled, orders included.
- B-B, the conversation's patient lapsed: the history anchor stops at any earlier turn holding a plain number ("lasix 40 iv bd, continue?"), so the rule had no referent and fell to B-A.
- B-C, a negated reply: after the question, "not 12", "except 12", "nahi 12" were read as the bare-bed confirmation (the reply pattern allowed any word before the number) and opened bed 12.
- B-D (rarer): a bed whose patient also matched by one name word ("Elvira Short" in "she is short of breath, her husband in bed 12 had flu") was never confirmed.
Also: after "yes" confirmed bed 12 with bed 7 open, an unnamed follow-up order ("add kcl 20 mmol") went back to bed 7, labelled; the history walk could anchor on a bed that had only been asked about; the bare-bed reply pattern was quadratic on whitespace (8.8 s for 40,000 spaces); voice, which sends no history, can never reach another bed by number while a chart is open.

Backed out by reverse patch on 6 October; the four files are byte-identical to HEAD (93d0d38). Code, tests, patches and both probe sets: docs/archive/bed-locator-attempt9-2026-10-06/ (probes_review9/ is the reviewer's).

### What attempt 9 showed

The structural rule works where it applies: with a chart open no phrasing produced a wrong chart, across nine reviewers' worth of probes. Every remaining failure is a place where Sage still trusted its own reading of a bed without asking: nothing open, a lapsed conversation patient, a reply read as a confirmation, a name word. Extending the same rule to every bed reading (a bed always needs confirming unless it is the open patient's own, or the reply is exactly that bed's label) closes all four by construction; the cost is the nothing-open ask rate rising to the chart-open level (about 25% overall on the realistic sets, still under the old code's 57%).

## Attempt 10 review (6 October): every bed is confirmed; did not ship

Attempt 10 (plan, "Attempt 10") made the confirmation universal: Sage never opens a bed's chart on its own reading, with a chart open or not. The rule held everywhere it was applied. Across all 50 earlier probe files the reviewer recorded 5,013 retrieval calls and 93 charts other than the open one: 57 labelled, 30 typed replies to the which-patient question, 3 full names and 3 partial names with no bed; none broke the rule. Realistic sets (384 rows per lane): 2 wrong charts against the old code's 118 on a numbered ward, 0 against 6 on GM naming; asks 26% against the old code's 59%; every other-bed question reached in one reply. Performance within budget everywhere (worst 1.54 s at 512 KB).

It failed on two holes in applying the rule:
- B1: a bed read as unsure plus ONE word of a patient's name. The unsure bed was put on the question, then the partial name match removed it, so the patient was matched by name alone and never confirmed ("her husband Ravindran in bed 18 has covid, should she be tested?" opened bed 18 unlabelled; with "start lasix 40 iv for her" the order was drafted on bed 18). Separately, the full-name test drops name parts of two letters or fewer, so "Selvaraj K" or a one-word name counted as named in full from one typed word.
- B2: following the patient chosen in reply survived a chart switch. Sage's panel keeps one conversation while the clinician moves between charts, so after "yes" to bed 13 and opening bed 9's chart, "give her lasix 40 iv" was drafted on bed 13, labelled, where the old code used the chart now open.
Also reported: 8 of 12 phrasings asked about the OPEN patient's own bed ("how is 7?" with bed 7 open); a re-asked question lost its candidate, so "yes" could not settle it; a number reply was read as any bed in the department (an option number "2", or a bed re-occupied since); our bed plus another department's code as a place ("bed 13 was in ICU-03 before") was refused as out of department, with a security audit row; the 1,000-reference cap skipped the out-of-department name refusal.

Backed out by reverse patch on 6 October; the five files are byte-identical to HEAD (784f30a). Code, tests, patches and both probe sets: docs/archive/bed-locator-attempt10-2026-10-06/ (probes_review10/ is the reviewer's, with an invariant recorder in plug/).

## Attempt 11 review (6 October): attempt 10 with its holes closed; did not ship

The rule held again wherever it applied: the invariant recorder over 56 earlier probe files saw 7,019 retrieval calls with 0 violations; the realistic sets (434 rows per lane) gave 0 wrong charts against the old code's 128 on a numbered ward, asks 26 to 36% against the old code's 59 to 74%, and every other-bed question was settled in one reply. Both attempt 10 holes were closed as specified.

It failed items 1 and 3 on four classes, each through an exception to the rule or through the follow-on logic:
- F1, the full-name exception: a full name with that patient's bed opened the chart even when the clinician was talking about someone else in nouns, not pronouns ("the husband Ravindran Kuppusamy in bed 18 has covid, should the wife be tested?", "start this patient on the same antibiotics as Ravindran Kuppusamy in bed 18", Hinglish "uske pati ..."). The she/he/they guard missed "this patient", "pt", "the wife", "uske". Orders were drafted on the named patient, unlabelled.
- F2, the own-bed exception (new in attempt 11): the open patient's own bed was answered directly even when Sage was only unsure of it ("ER bed 12 referral: 60M, septic shock, can we accept?" with bed 12 open answered from bed 12's chart, unlabelled, and orders were drafted there). Attempt 10 asked.
- B3, follow-on: a choice made in reply survived a visit to another chart when the turns in between did not ask (a census or aggregate question, an unknown name), so back on the first chart "give her lasix 40 iv" was drafted on the bed chosen earlier, labelled.
- B4, follow-on: when every patient a question offered had since left their bed, a reply fell back to any bed in the department, and "12" opened the newcomer in bed 12, unlabelled.
Also reported: a bare other bed in reply ("21" to a question about bed 20) repeated the question; after a chart switch every turn asked until "this patient"; another department's code beside our bed in a comparison ("compare bed 13 with ICU-03") was refused and audited.

Backed out on 6 October. A parallel commit (d9553ad, vitals voided_at filters) had touched sage_service.py meanwhile; the working copy was proven to be exactly attempt 11 plus that commit before HEAD's content was restored, and the other four files were reverse-patched; all five are byte-identical to HEAD. Code, tests, patches and both probe sets: docs/archive/bed-locator-attempt11-2026-10-06/ (probes_review11/ holds the review's probes).

### What attempts 10 and 11 showed

The rule itself has not been broken by any probe in two reviews. Every blocking finding came through an exception or a shortcut that let Sage act on its own reading: one name word, a full name, the open patient's own bed, a reply remembered across chart switches, a reply matched against the whole department. Removing the exceptions (every bed reading is asked, the open patient's own bed and full names included; a chart opens only on a reply to the question about the chart the clinician is on) leaves no path for Sage's reading to open a chart. The cost is one more reply on questions that name the open patient's own bed or a patient's full name with their bed, which none of the realistic sets contain.

## Attempt 12 review (6 October): no exceptions; did not ship

Attempt 12 (plan, "Attempt 12") removed every exception: each bed reading is asked, the open patient's own bed and a full name with a bed included, and a chart opens on a bed only through a reply to the which-patient question. On realistic traffic it is the best attempt yet: 0 wrong charts in 1,936 rows (484 per lane) against the old code's 317; asks 29% on a numbered ward (old 60%) and 24% on GM naming (old 1%); every other-bed question settled in one reply; the open patient's own bed costs one reply on 5 of the reviewer's 50 rows per chart-open lane. Performance within budget everywhere (worst 1.54 s at 512 KB), no crashes.

It failed the bar on two classes, both in the conversation logic around the rule rather than the rule:
- B-A, the re-ask loses the offer (U; R on a numbered ward): bed 7 open, "how is bed 12?" offers bed 12's patient; that patient moves and a newcomer is admitted to bed 12; "yes" gets a re-ask that lists nobody; "12" then opens the newcomer's chart, unlabelled, and "give her lasix 40 iv" drafts the order there. The old code asked. The re-ask was written from an empty offer list, a question listing nobody reads back as "any bed may answer", and the reply was matched against the whole department. The paste-cap question and the "not this patient" question had the same gap. Reproduced with "bed 12" as the reply, two re-occupied beds, a discharge, nothing open, an order ("give lasix 40 iv to bed 16", "go ahead", "16"), and GM naming (U only there; the old code also opens the newcomer).
- B-C, the follow survives chart visits Sage never saved (R, labelled): bed 7 open, "how is bed 13?", "yes"; then on bed 9's chart a turn that saves no assistant row (a safety refusal such as "what is the antidote for paracetamol overdose?", an out-of-department refusal, a retrieval failure, a turn in another conversation, voice, an empty model output); back on bed 7, "give her furosemide iv stat" was drafted on bed 13, labelled, where the old code used bed 7. A saved census turn on bed 9 correctly ended the follow. Variant: with no chart open now, the follow ran whatever chart had been open when the choice was made.
Also reported, labelled or non-blocking: an aggregate question about a bed ("length of stay of cot 12?") is answered before the locator runs, and with nothing open the history walk then anchors on that bed, so "give her lasix 40 iv" drafts on bed 12, labelled ("apart from GM-512" then an order drafts on the excluded GM-512); the choice drops out of the 6-message history window after three turns, silently, as in the old code; after a chart switch every unnamed turn asks, general questions included, until "this patient"; "no, this patient" is read as negated; voice cannot complete a question because the panel sends no history; a duplicate bed label across two wards re-asks until the ward is given; "compare bed 13 with ICU-03" is still refused and audited.

My build note for attempt 12 overstated its own check: the probe re-run started on an earlier build (its md5 file does not match the shipped files), and the invariant recorder tagged every chart opened after a which-patient question as a reply, so 19 rows of B-A (about 10 flows, after a question that offered nobody) counted as clean. The attempt 11 reviewer's own probe (test_rv11_route.py::test_reoccupied_yes) shows B-A under attempt 12.

Backed out by reverse patch on 6 October; the six files are byte-identical to HEAD (2b04fd8) and tests/test_sage_bed_locator.py is removed. Code, tests, patches and both probe sets: docs/archive/bed-locator-attempt12-2026-10-06/ (probes_review12/ is the reviewer's).

### What attempt 12 showed

Apart from B-A's replies to a question that offered nobody, no chart opened on Sage's own reading of a bed across 7,982 recorded retrieval calls. Both blocking classes come from Sage reconstructing the conversation by re-reading text: the offer list parsed back out of the question's wording (an empty list reads as "anyone"), and the chart history rebuilt from saved rows (an unsaved turn is invisible). The next step is to stop reconstructing: store what each turn decided (the patients a question offered, the chart an answer used, the chart that was open) and have the reply and follow logic read only that; where the record is missing or does not cover the turn, ask.

## Attempt 13 review (9 October): passed the bar, with one safety finding outside it

Attempt 13 (plan, "Attempt 13") is attempt 12 plus: a question listing nobody accepts no bed; a patient chosen by bed is offered again on every unnamed follow-up, never followed; a bed in an earlier message is never carried forward. It was reviewed on a frozen copy (hashes matched at start and end), kept off main because a parallel verification step stages the whole tree.

Against ruling G the reviewer found no regression and no unlabelled miss. Single questions (534 rows per lane, 19 realistic sets): 0 wrong charts in every lane against the old code's 149 and 151 (numbered ward) and 19 and 19 (GM); asks 29% numbered (old 62%) and 25% GM (old 1%); every other-bed question settled in one reply. Multi-turn threads (20 threads, 86 steps, through the route): 2 wrong charts, both labelled, against the old code's 31 unlabelled; 1 order on the wrong patient, labelled, against 17; 0 loops; 0 labels missing live or on reload. The accepted cost: 67 replies against 26, about 40 of them re-offers on unnamed follow-ups after a reply choice. The strict invariant check over 9,170 recorded retrieval calls found no violation (every chart other than the open one was named in the turn, carried by an earlier name, or opened by a reply to a question that offered that exact patient). The author's re-run reproduced exactly. Performance within budget.

S1, outside the bar and a safety regression against the old code: an order settled by a reply skips the order gate. The gate (`_mutation_gate(query)` in routes/sage_routes.py) and the action-state override read only the turn's own text, and on a reply turn that text is "yes", "this patient" or "13". Chart 7 open, "how is bed 13?", "yes", "give her lasix 40 iv" (offered again), "yes": with a model reply such as "Done. Furosemide 40 mg IV has been given and the order is placed." the clinician sees that text, labelled with bed 13, with nothing drafted and no action card; reload is the same. Five such wordings reached the screen. The old code always gated the order's own text, so this reply never reached the screen. Guideline retrieval and the model's user message are also the word "yes". The path has existed since attempt 9 on the first "yes", but reviews 9 to 12 stubbed an ACT| reply there and never saw it; attempt 13 makes it the path for every unnamed follow-up order after a reply.

Also reported, not blocking: with nothing open, after one reply choice a general question ("what is the max dose of furosemide in AKI?") can only be answered with that patient's chart ("yes"), by naming someone, or by a new chat ("no", "general", "not about any patient" repeat the question); the choice never ages out, since each "yes" is a new reply choice; after "how many have fever apart from cot 12?" the next pronoun turn offers the excluded bed 12 patient; a bed label containing ": " or a name with a newline can never be confirmed; shadow rows for a turn answered from bed 13 via "yes" while chart 7 is open name bed 7 (pre-existing for names); the question is recognised by its text, so an assistant message reproducing it counts as an offer; the sidebar's loadConversation shows an old exchange without switching the conversation id (pre-existing, read from code).

Code, patches and both probe sets: docs/archive/bed-locator-attempt13-2026-10-09/ (probes_review13/ is the reviewer's). Main does not carry attempt 13; the next step is Samuel's ruling on S1.

## Attempt 13b review (9 October): S1 fixed, two new order findings; did not ship

Attempt 13b (plan, "Attempt 13b") added one rule to attempt 13: when a reply made only of who-words settles the which-patient question, the order gate runs on every message of the exchange, and guideline search, the grounding check and the model get the clinician's earlier words. Reviewed on a frozen copy (hashes matched at start and end); main never carried it.

S1 is fixed: "yes", "12", a name and "this patient" are gated in both contracts and in the 4B marker lane, drafts land on the confirmed patient, labels hold live and on reload. Single questions and review 13's threads unchanged (0 wrong charts against the old code's 149 and 19 per lane). The strict invariant check over 9,182 calls reproduced exactly.

It failed on two classes, both in how far the exchange reaches:
- R1, an order drafted on a patient it was never ordered for: the walk went back through a reply that was itself a new question about another bed. "give lasix 40 iv to GM-512", the question about GM-512, "how is GM-515?", the question about GM-515, "yes": the model was given "give lasix 40 iv to GM-512\nhow is GM-515?" with GM-515's chart, and a model that declares orders drafted furosemide on GM-515's patient (labelled, needs approval). Also with KCl and ceftriaxone, on a numbered ward, after "cancel that", and on the follow-up path. The old code drafted on GM-512 at its own turn.
- G1, a reply that names the patient and adds one word skipped the gate: "this patient, stat", "this patient asap", "<first name>, stat", "<first name> please start it", "<full name> - give it". The reply was not made only of who-words, so it kept its own text and its own gate, the detector saw no order in it, and the model, which still had the order in its history, narrated it with nothing drafted. The old code gated the order at its own turn.
Also reported, not blocking: a read question inside an order exchange ("potassium?" then "yes") is answered with the order card; "cancel that" inside the exchange does not cancel; a chain of 10 re-asks pushes the order out of the 20 saved messages; joining a long exchange hands the model a string up to about 4.7 MB; provenance records the question as "yes"; the shadow rows for a settled order are filed under the open chart; a reply naming a discharged patient goes to the model ungated.

Code, patches and both probe sets: docs/archive/bed-locator-attempt13b-2026-10-09/ (probes_review13b/ is the reviewer's).

### What attempts 13 and 13b showed

The chart rule now holds everywhere: no reviewer found a wrong chart or an unlabelled miss in two reviews. What is left is the ORDER a reply settles. Both 13b findings come from the model still seeing an order the clinician never confirmed. In R1 the walk handed it to the model along with a new question. In G1 the order stayed in the model's history while the reply was answered as its own message. An order Sage asked "which patient?" about should reach the model only as the settled request, and only when the replies in between were about that same request.

## Attempt 13c review (9 October): did not ship, on four order classes

Attempt 13c (plan, "Attempt 13c") ended the exchange at a reply that brought a new patient into the question or cancelled, hid every request Sage asked "which patient?" about from the model, and let a settling reply hold go-ahead words and the request's own words. The new order bar (Samuel, 9 October): blocking is a narration shown with the order in the model's input, or an order drafted on a patient the clinician did not confirm for it.

Items 1 to 3 pass again: 0 wrong charts in 534 rows per lane (old code 149 and 19), no label missing live or on reload, the strict invariant check over 9,778 calls reproduced exactly. 13b's R1 and G1 are fixed, and no asked-about order reaches the model's history on any lane. In 24 threads with a stub that acts only on its input, 13c drafted 27 orders on the right patient and 2 on the wrong one; 13b drafted 4 on the wrong one, the old code 32.

It failed item 4(b) on four classes, each an order drafted on a patient nobody confirmed it for (labelled, needs approval):
- G1, a reply that turns the order down is read as a confirmation: "<first name>, no thanks", "this patient, no lasix", "<surname> - no", "<first name> no kcl". "no", "thanks" and "you" are filler, the stop words were checked only on earlier messages, never on the settling reply, and 13c's request words and go-ahead words let "lasix", "kcl", "give" and "stat" through. 20 of 26 such rows drafted, against 7 in 13b; the old code never drafts after a decline.
- G2, a new question about one of several offered patients steps back into the order: "give lasix 40 iv to Mr <surname>" with two patients of that surname, then "how is GM-533?", then "yes" drafts the lasix on GM-533, because the later question offers a subset of the earlier one. Same with one bed label on two wards, and in a realistic thread where the clinician ignores the order question.
- G3, one message ordering for two patients is handed to the model whole once a reply picks one: "give lasix 40 iv to GM-532 and kcl 20 mmol iv to GM-533", then "GM-533".
- G4, go-ahead words plus another patient's name move the order: "and <full name of GM-515>?" after a question about GM-512. A bare other name already did in 13b.
Also reported, not blocking: context is lost after a settled read ("is GM-512 on heparin?", "yes", "should she be?" gives the model "should she be?" with history ["yes"]); lost orders ("this patient, push it", a discharged name, a chain past 20 messages); provenance still records the reply as the question, and shadow rows sit under the open chart.

Code, patches and both probe sets: docs/archive/bed-locator-attempt13c-2026-10-09/ (probes_review13c/ is the reviewer's). Main carries none of attempts 13 to 13c.

### What attempts 13b and 13c showed

The chart side is settled: three reviews in a row found no wrong chart and no unlabelled miss. The order side has now failed twice the way attempts 3 to 8 failed on beds. Each rule for which replies may carry an order across the question (who-words, go-ahead words, stop words, which earlier questions to step over) is a list of what is NOT a new request, and every review finds a reply the list did not foresee. Attempt 8's lesson applies: list the certain side. An order should cross the question only in the one shape that cannot mean anything else. That shape is a question that offered exactly one patient, the order typed immediately before it, and a reply that is exactly "yes" or exactly that patient's bed. Every other reply to an order question should confirm the patient and ask for the order again, never draft it.

## Attempt 13d review (9 October): did not ship, on two order findings from rules applied in one branch only

Attempt 13d (plan, "Attempt 13d") let an order cross the which-patient question only in the certain case (one patient offered and exactly "yes" or that bed, or exactly "this patient" for the open chart), answered every other reply to an order question with a server notice, refused to hand over a message that read more than one bed, and restored read context in the model's history.

Items 1 to 3 pass for the fourth review running: 0 wrong charts in 534 rows per lane (old code 149 and 19), every miss labelled, the strict invariant check over 10,171 calls reproduced exactly. Review 13c's threads stayed clean (0 orders on a wrong patient), and in the reviewer's own 30 threads with a stub that acts only on its input, 13d placed 31 orders on the right patient and 0 on a wrong one (the old code 30 wrong or patient-less), with 0 unlabelled answers and every lost order completed by retyping, no loops.

It failed item 4 on two findings, both rules 13d applied in one branch only:
- G-A: the "this patient" certain case carried an order message that named other patients to the open chart. "give lasix 40 iv to GM-612 and kcl 20 mmol iv to GM-613", the question offering both, "this patient": furosemide drafted on the open GM-607. Also "give lasix 40 iv to GM-612 and kcl 20 mmol iv to this patient", where the lasix meant for GM-612 landed on GM-607 (the old code drafted it on GM-612). The many-beds rule ran only for reads.
- G-B: orders the detector misses ("lasix 40 iv for GM-612", "GM-612 needs lasix 40 iv", "push lasix 40 iv to GM-612") take the read branch, where "this patient, no" was carried (the decline check ran only for orders) and moved the order to the open chart, and a bed plus a NAME ("GM-612 needs lasix 40 iv, and check <GM-613 patient's name>'s potassium") counted as one bed, so the message went whole to GM-613.
Also reported: G-C, the history restore put a message the many-patients notice had refused back into the model's history, and a later turn narrated or drafted its order (detector-dependent); an undetected order then a bare other name drafted on that patient where a detected order got the notice; "this patient" binds to the chart open when the reply is typed; voice passes a NOT_CARRIED notice to the model instead of speaking it; "yes" after nine 512 KB re-asks took 2.0 to 2.8 s, close to the 3 s budget; provenance still records the reply as the question.

Code, patches and both probe sets: docs/archive/bed-locator-attempt13d-2026-10-09/ (probes_review13d/ is the reviewer's). Main carries none of attempts 13 to 13d.

## Attempt 13e review (9 October): did not ship; every failure came through an exception inside the one rule

Attempt 13e (plan, "Attempt 13e") applied one carry rule to reads and orders alike: a reply carries a message only when everyone it reads is the patient the reply chose, with three exceptions. One ambiguous reference could include that patient (one bed label on two wards, or one name word all of them share). An exact "this patient" could disown readings Sage was unsure of. And a list of negation phrases kept "this patient" off messages that ruled it out. It was built on main with 324e754's wider order detector and reviewed against main as it is, the old bed matcher with the new detector.

Items 1 to 3 pass for the fifth review running: 0 wrong charts in 574 rows per lane (old code 157 and 20), every miss labelled, every other-bed question settled in one reply, the strict invariant check over 10,812 calls reproduced exactly. In the reviewer's 30 threads with a stub that acts on its input: 30 orders on the right patient, 2 on a wrong one, 0 narrations with the order in the model's input, 0 unlabelled; the old code 11 right, 29 wrong, 34 narrations, 31 unlabelled. Review 13d's 30 threads: 31 right, 0 wrong.

It failed item 4(b) on four classes. Each is one of the three exceptions, beaten:
- G1, one shared name word: "give lasix 40 iv to Rajesh Kumar and kcl 20 mmol iv to Suresh Kumar", then "this patient" on Suresh's chart, drafted Rajesh's lasix on Suresh. The rule checked that all matched patients share a word in the message, not that they were read from that one word. Same with shared first names, and with an order the detector misses.
- G2, unsure readings disowned by "this patient": "give lasix 40 iv to 12 and kcl 20 mmol iv to 13", "12 would benefit from lasix 40 iv and 13 could do with kcl 20 mmol iv", and "bed 12 stat ... bed 13 stat" (a trailing "stat" makes a bed unsure) all went to the open chart.
- G3, the negation list: "give lasix 40 iv to bed 12 instead of this patient", then "this patient", drafted on the patient the message excluded; also "rather than", "unlike", "excluding", "leave this patient alone".
- G4, one bed label on two wards written as two references: "bed 12 in DWA ... and bed 12 in DWB ...", and the KCl for DWB's bed 12 landed on DWA's.
None of these is a regression against the old code, which drafts these orders with no patient at their own turn and then narrates or drafts on the wrong patient at the reply. The bar's item 4 is absolute, though.
Also reported: "yes" after nine 512 KB re-asks took up to 2.69 s, because the order detector reads every message before the three-message cap; "<name>, thanks" after an undetected order drafts on that patient where a detected order gets the notice; voice gives its own model the raw history.

Code, patches and both probe sets: docs/archive/bed-locator-attempt13e-2026-10-09/ (probes_review13e/ is the reviewer's). Main carries none of attempts 13 to 13e.

### What attempt 13e showed

The rule held; its exceptions did not. Each exception (one ambiguous reference, unsure readings disowned by "this patient", a list of negation phrases) was there to keep an ordinary read working, and each was a list that a reviewer beat. Attempt 8's lesson again: the certain side only. A reply carries a message only when everything the message read is the chosen patient by itself, or it read nobody. Then a shared name, a misread number or a two-ward bed costs the clinician a retype. They can no longer cost an order on the wrong patient.
