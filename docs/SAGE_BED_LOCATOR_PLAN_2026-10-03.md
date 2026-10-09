# Sage bed lookup: build plan, 3 October 2026

Status: RULED (R026 (a), D2 to D4, Q1 to Q3, A to F, G, H). Attempts 2 to 6 failed review and were backed out; main has the original substring bed matching plus the shared ward rule. Attempt 7 (attempt 6 plus the fixes below) was built on 4 October, failed review against ruling G and was backed out (see the findings doc, "Attempt 7 review"). Attempt 8 was measured, approved and built on 4 October (D3 kept for comma items), failed review on 6 October and was backed out (findings doc, "Attempt 8 review"). Attempt 9 (confirm another bed while a chart is open) chosen by Samuel on 6 October, built, failed review the same day on the lanes it did not cover (nothing open, a lapsed conversation patient, "not 12" as a reply) and was backed out (findings doc, "Attempt 9 review"). Attempt 10 (confirm every bed) chosen by Samuel on 6 October, built, failed review the same day on two holes in applying the rule (a partial name removing the question; a reply choice surviving a chart switch) and was backed out (findings doc, "Attempt 10 review"). Attempt 11 (attempt 10 with those holes closed) approved by Samuel on 6 October, built, failed review the same night on its exceptions (a full name, the own bed, a remembered reply) and was backed out (findings doc, "Attempt 11 review"). Attempt 12 (no exceptions) approved by Samuel on 6 October, built, failed review the same night on the conversation logic around the rule (a re-ask that lost its offer; a reply choice surviving chart visits Sage never saved) and was backed out (findings doc, "Attempt 12 review"). Attempt 13 ("stop rebuilding": no follow, no bed carried forward, a question listing nobody accepts no bed) approved by Samuel on 9 October and built; passed the bar in review but with S1 (an order settled by "yes" skipped the order gate). Attempt 13b (fix S1) built the same day, reviewed on a frozen copy, kept off main; it failed on R1 (an earlier order drafted on a patient confirmed for a new question) and G1 (a reply naming the patient plus one word skipped the gate) (findings doc, "Attempt 13b review"). Attempt 13c (Samuel, 9 October, with the order bar: only a narration with the order in the model's input, or a draft on an unconfirmed patient, blocks) built the same day and failed review on four order classes, each a draft on an unconfirmed patient (a decline read as a confirmation, a new question inside a multi-patient offer, a two-patient order, go-ahead words with another name) (findings doc, "Attempt 13c review"). Attempt 13d (Samuel: carry only the certain case) built the same day and failed review on two rules applied in one branch only (the "this patient" case skipped the many-patients check; the read branch skipped the decline check and counted a bed plus a name as one) (findings doc, "Attempt 13d review"). Main carries none of attempts 13 to 13d.
Reads with: docs/SAGE_BED_LOCATOR_FINDINGS_2026-10-03.md (the defect and why the first fix was backed out).

## The idea in one paragraph

Today Sage finds a bed by checking whether the squashed bed label appears anywhere inside the squashed question. That over-matches (GM-1 inside GM-103, A1 inside HbA1c), and the over-matching was hiding a second hole: when nothing matches, Sage falls back to the chart that is open on screen. The fix is one shared "bed locator" reader that finds every bed reference in the question, resolves each one as a whole label, and reports which ones it could not resolve. The open-chart gate reads the same locator list, so anything the reader calls a bed reference can never fall through to the open chart. A reference that resolves to nobody ends the turn with NO MATCHING PATIENT (or "outside your department" if it is another department's bed).

## What changes, file by file

Only server/services/sage_service.py, plus one new test file. Backup first to docs/archive/pre-bed-locator-2026-10-03/server/services/sage_service.py. Targeted edits only. No other work in progress touches this file today (checked docs/archive at 20:49).

### 1. One locator reader: `_find_bed_locators(query_lower)`

Pure and DB-free, so it can run inside the name gate. It returns each reference with its text span, its normalised label, and how it was written. What counts as a bed reference:

| Form | Examples | Rule |
|---|---|---|
| Keyword first | bed 12, beds 12 and 14, cot 12, bay 3, bed no. 12, bed num 12, bed number 12, bed #12, bed:12, bed GM-103, bed twelve | Keywords bed(s), cot(s), bay(s). Optional no / no. / num / number / #. Lists joined by `,` `and` `&` `or` `/`. Number words one to twenty. Any label shape after the keyword. |
| Number then keyword | 12 number bed, 12 no bed | Indian English phrasing. |
| "patient in N" | the patient in 12, the lady in 14, pt in GM-103 | patient, pt, man, woman, lady, gentleman, guy, chap, boy, girl, baby, child, one, fellow, then "in", then a label. |
| Ward prefix, joined | GM-103, gm103, CCU-01, RB-106 | Letter run of 2 to 5 letters that begins a bed or ward label on record anywhere in the hospital. With a hyphen: any such prefix. With no separator: not if the prefix is a clinical abbreviation or English word (see stop list). |
| Ward prefix, spaced | gm 103, ccu 01 | Same prefixes, minus the stop list, and only when not preceded by a number (so "2 gm 12 hourly" is a dose, not a bed). |
| Single letter | A1, A-1, B104 | Joined only ("a 1" is ordinary text). Letter must begin a bed label on record. Short clinical codes are excluded unless a keyword is present (decision D4). |

Guards on every form:
- A number followed by a time or unit word is not a bed: "bed 2 days ago", "in 12 hours", "ICU 2 days ago", "12 hrly", "12 mg".
- Stop list for prefix forms without a keyword: English words and ward-round abbreviations that would read as prefixes (or, no, in, at, on, to, is, it, hr, bp, rr, gm when spaced after a number, mg, ml, kg, iu, pt, ph, na, ct, dr, mr, st). A site with a Recovery Room ward "RR-1" must not turn "RR 24, is that high?" into a bed lookup. Those sites still reach their beds with the keyword or the hyphen ("bed RR-24", "RR-24").
- Regex hygiene from the findings note: the keyword prefix is `bed[\s#:.\-]*(?:(?:no\.?|num(?:ber)?)[\s#:.\-]*)?`, no two overlapping `*` groups. A test feeds 50,000 spaces and asserts it returns in under 50 ms.

The prefix list comes from the census refresh that already runs at the start of every Sage turn (`_refresh_census_name_tokens`, hospital-wide on purpose). I extend that one query to read bed and ward labels too, and keep a second module global `_CENSUS_BED_PREFIXES`. No new DB read on the event loop.

### 2. Resolution: whole labels only

Each reference is compared to each bed label as a whole label: lower case, separators removed, never a substring. "gm 101", "GM101" and "GM-101" are the same label; "GM-1011" is not GM-101. Leading zeros: decision D2.

Order inside `_match_patients`:
1. Names, exactly as today (no change to the name matcher).
2. Bed references, resolved against admitted in-scope patients. Discharged patients are never matched by bed: their bed now belongs to someone else.
3. Bed reference text is blanked out of the question, then wards are matched as whole labels in what remains. So "Summarise CCU-01" finds one patient, not all of CCU. A ward label that ends in a letter does not match when digits follow ("ICU" inside "ICU-06"). A digits-only ward label needs the word "ward".
4. Ward and bed written together ("ward 5 bed 12", "GM-1 bed 12", "bed 12 on ward 5") match the intersection. A ward mentioned elsewhere in a sentence that also names a bed is ignored, so "transfer bed 12 to ward GM-2" still finds bed 12.

`_match_patients` keeps its signature and return value (both lanes and the history anchor call it). A new `_locate_patients(...)` returns matches plus the unresolved references, and `_match_patients` becomes a thin wrapper over it.

### 3. The gate and the open-chart fallback

- `_names_a_specific_patient`: the line `\bbed\s*\d+\b` becomes "any bed reference the locator reader finds". Everything else in the gate stays. So the gate now recognises every phrasing the matcher understands, which is the first requirement in the findings note.
- `get_relevant_context`, right after the out-of-department block and before the census branch:
  - For each unresolved reference, check other departments' admitted beds by whole label. Any hit: the existing OUT_OF_SCOPE answer, audited (reason `bed_outside_department`). This now also fires when one bed in "beds 12 and 14" is ours and the other is not.
  - Any reference still unresolved: return the existing NO MATCHING PATIENT grounding at once. No chart, no census, no anchor, no pronoun branch. Same return shape and scope label as today's no-such-patient early exit, so nothing downstream changes.
- Because that exit comes before the pronoun branch, "the potassium of the patient in bed 15" (no bed 15) can no longer be answered from the open chart. That closes the hole the reviewer found under both old and new code.
- The second `\bbed\s*\d+\b` check further down (`names_unambiguously`) reads the same locator list, so there is one definition.

### 4. The conversation-history anchor

`_anchor_patient_from_history` walks back over the clinician's own turns. Today a turn naming nobody is skipped and the walk continues to older turns. A turn whose bed reference resolved to nobody will now STOP the walk, the same way a turn naming two patients already does. Otherwise "Summarise bed 99" (no such bed) followed by "and her lactate?" would answer about whoever was discussed three turns earlier.

### 5. Out-of-department bed lookups

`_out_of_scope_locators` returns a bed only for admitted rows (`CASE WHEN status = 'admitted' THEN bed END`). The projection stays three columns, as its docstring requires. Today a discharged patient in another department who once lay in bed 9 makes "who is in bed 9?" answer "that patient is outside your department". Names still match discharged rows exactly as today.

## What I am deliberately not changing

- The name matcher, the possessive and contraction rules, the clinical vocabulary list.
- The DISAMBIGUATE, BROAD, aggregate and anchor lanes, apart from the early exit above.
- Ward-level questions. "How is ward GM-1 doing?" still lists all of GM-1. (Noted, not fixed: a ward with exactly ONE admitted patient makes any mention of that ward attach that patient's chart. That is today's behaviour and it is outside R026.)
- Whether "bed 103" finds "GM-103". It does not today and will not after this. A bare number must equal the whole label. Changing that is a separate decision.
- ai_governance's PHI classifier has its own bed regex. It decides whether a turn may leave the box, not which chart Sage reads, and its DB match already catches beds on record. Listed as a follow-up, not touched.

## Tests

One new file, tests/test_sage_bed_locator.py. Its own department and bed prefixes, so it cannot collide with other files in the shared session DB, and its patients are discharged at teardown (the isolation rule in test_sage_sticky_anchor.py). Every behaviour test goes through `get_relevant_context` with `patient_id` set to an open chart, so the fallback is always exercised.

I run the file against today's code FIRST and record which tests fail, before any edit.

Wrong patient today, must be fixed (numeric beds 7, 12, 14; bed 7's chart open):
1. "compare beds 12 and 14": both beds, never bed 7.
2. "cot 12 lactate?": bed 12.
3. "bed num 12 lactate?": bed 12.
4. "what is the potassium of the patient in 12?": bed 12.
5. "bed 12 and 14 potassium?": both beds.
6. "Summarise RB-106" (ward RB-1 exists, RB-102 open, no RB-106): NO MATCHING PATIENT, no chart.
7. "the potassium of the patient in bed 15" (no bed 15): NO MATCHING PATIENT, no chart.

Matcher cases from the findings note:
8. Five GM-1 patients: "Summarise bed GM-103" finds GM-103 only; "how is GM-1011" finds nobody; "Summarise CCU-01" finds one; "How is ward GM-1 doing?" finds all of GM-1.
9. Beds 9, 17, 99: "Who is in bed 9917" nobody; "Is a NEWS2 of 9 reassuring?" nobody and the open chart stays; "how is bed 9" bed 9; "bed no. 17 please" bed 17.
10. Bed A1: "what is a normal HbA1c?" and "is a 1 point rise significant" find nobody and keep the open chart; "summarise A1" and "summarise bed A-1" find it.
11. Another department in beds 9 and 17: "who is in bed 9917" is not out of department; "who is in bed 9?" is.

The other direction, the chart must stay open (this is what the first attempt never tested):
12. "potassium after 12 hours", "is vitamin b12 low?", "500 mg 12 hourly", "give 1 gm 8 hourly", "is the potassium 5 or 6?" with an OR ward on record, "RR 24, is that high?" with an RR ward on record, "transferred from ICU 2 days ago", "keep on bed rest", "compare ceftriaxone and cefepime". Each must stay PATIENT_ANCHORED on the open chart.

Also:
13. "ward 5 bed 12" picks the intersection; "transfer bed 12 to ward GM-2" still finds bed 12.
14. History: "Summarise bed 99" then "and her lactate?" with no chart open anchors nobody.
15. Discharged out-of-department patient once in bed 9: "who is in bed 9?" is NO MATCHING, not out of department.
16. 50,000 spaces after "bed" returns in under 50 ms.

Guard suites, each file once, clean env, never the shipping .env: test_sage_patient_phrasing, test_sage_department_scope, test_patient_scope_routes, test_sage_sticky_anchor, test_sage_history_patient_name, then all of tests/test_sage*.py once, plus test_caller_named_patient_scope and test_audit_fixes. test_a_bed_alone_is_enough_to_warn should stop being order-dependent, because "bed 9917" no longer matches a short bed "9" left by another file.

After green: an independent review tries to break it the way the last reviewer did (through `get_relevant_context`, open chart set), and I fix or back out on what it finds. Then I update the findings note, the R026 row and GAP_FIXES, and diff against my backup to confirm only my hunks changed.

## Decisions I need from you

- **D1, ruling R026.** (a) any bed reference that resolves to nobody stops the open-chart fallback and returns NO MATCHING PATIENT; (b) keep today's behaviour. I recommend (a). Nothing gets built without this.
- **D2, leading zeros.** Should "bed 9" find a bed labelled "09", and "CCU 1" find "CCU-01"? I recommend yes. If both "9" and "09" exist, Sage lists both and asks.
- **D3, a list where only some beds exist.** "compare beds 12 and 99" with no bed 99. (a) NO MATCHING PATIENT for the whole turn, the doctor retypes; (b) answer for bed 12 with a line saying bed 99 is not in the census. I recommend (a): it is the literal reading of R026, and nothing is silently dropped.
- **D4, a bed label that is also a clinical code** (B12, T4, V5, L4, A2, D3) written without "bed". (a) Sage asks "did you mean the patient in bed B12?" when that bed is on the doctor's ward, otherwise reads it as clinical text; (b) always read it as clinical text, the keyword is required. I recommend (a). (b) is simpler but turns "summarise B12" into an answer from the open chart, which is the wrong-patient shape this whole fix is about. Neither arises on the seeded naming (GM-101, CCU-01); it matters only for sites with one-letter bed codes.

---

## Attempt 3: revised design (proposed 3 October, 23:45)

Why attempt 2 failed is in the findings note. In one line: it guessed what a bed reference looks like, every gap in that guess fell to the open chart, and the gaps were common speech ("how is 12?") and ordinary label shapes ("MW/12").

### The idea

Find beds from what is actually ON the doctor's ward, then decide how sure we are. When Sage is unsure, it asks. It never falls back to the open chart for a turn that mentions a bed on the ward.

1. **Read the question once.** Unicode dashes and non-breaking hyphens become "-". The question becomes runs of letters and runs of digits with the gaps between them. One linear pass, done once per turn and shared by the gate, the matcher and the history anchor. Digit runs longer than 6 are never a bed (no int() crash).
2. **Find.** Every bed label on the doctor's ward is looked for as a whole sequence of those runs, ignoring case, separators and leading zeros. The shape comes from the stored label, so "MW/12", "GW.12", "Room 205", "CARDIO-12", "12A", "ICU Bed 3" and "HDU-2" all work, and "GM-1" can never match inside "GM-103" nor "A1" inside "HbA1c". Each hit is one of:
   - **Certain**: written after a bed word ("bed 12", "cot 12", "the patient in 12", "12 number bed"), or a lettered code written as a code ("GM-103", "Room 205", "RR-24", "A1"). Sage answers from that chart.
   - **Discarded**: plainly not a bed. Followed by a unit, duration, decimal, ratio, degree, ordinal or "+" ("12 hours", "5.2", "2:1", "30 degrees", "2nd", "4+"); a dose ("2 gm 12"); after a score or lab word ("NEWS2 of 9", "GCS 8", "potassium 5").
   - **Unsure**: everything else, including bare numbers ("how is 12?", "12's potassium?", "compare 12 and 14"), clinical codes ("is vitamin b12 low?" on a ward with bed B12), vitals-style codes ("RR 24" on a recovery ward) and destinations ("transfer her to bed 12"). Sage asks "did you mean the patient in bed 12?" (Q1).
3. **Missing (R026).** A reference written with a bed word, or a code whose letters are one of THIS department's bed or ward prefixes (including discharged patients' labels, so a ward emptied today still counts), that matches nobody on the ward: NO MATCHING PATIENT. A label that exists exactly in another department: the out-of-department answer (Q2 for bare numbers). Ranges ("beds 12-14", "12 to 14"), "bed 1 2" and number words it cannot fully read ("bed hundred and two") get NO MATCHING rather than a guess. Number words up to ninety-nine.
4. **Other departments' prefixes never start a reference** unless that exact label exists there. So "A1c", "D5W", "H2O", "ANC 500" and "COVID-19" stay clinical text in every department.
5. **Wards.** A ward written beside a bed with any connector ("ward 6 bed 12", "bed 12 (ward 6)", "ward-6/bed-12", "bed 12 from ward 6", "6th ward bed 12") narrows the bed. If nobody is in both, it is NO MATCHING, never out-of-department.
6. **Guards the reviewer showed were missing:** a bed word never reaches past the end of a sentence ("out of bed. 2 nurses"), "head of bed 30 degrees", "1:1 nursing", "2 litres O2", "2nd degree", "in 2019", "12 number beds free".
7. **The route.** On a no-match bed turn, the "viewing patient <name>" line is dropped from the prompt (routes/sage_routes.py, `_describe_view`'s caller), so the model has no open-patient name to drift to.
8. **Speed.** Linear. Tested on 512 KB inputs (the request cap) at under one second end to end.
9. A failed census refresh is logged as a warning, not at debug level.

### Tests

All 83 attempt-2 tests, and every reviewer probe as a regression test (W1 to W8, L1 to L5, P1, P2), each driven through `get_relevant_context` with a chart open. Run on today's code first to show what fails, then the guard suites, then a fresh independent reviewer briefed with attempt 2's probes plus a mandate to find new holes.

### Decisions needed

- **Q1, bare numbers.** "how is 12?" or "12's potassium?" on a ward with a bed 12, written without "bed". (a) Sage asks "did you mean the patient in bed 12?". (b) It answers from bed 12, as the old code did. Recommend (a). (b) is right for "how is 12?" but answers from the wrong chart whenever an incidental number slips past the discard guards. The cost of (a) is an occasional extra question on wards with numbered beds.
- **Q2, a bare bed number another department has.** "bed 12" when your ward has no bed 12 but Surgery does. (a) NO MATCHING, because bed numbers repeat across wards. Lettered codes that belong to one ward (CCU-01) still get the out-of-department answer. (b) Out-of-department, as today, which also files a security audit event against the doctor. Recommend (a).
- **Q3, "bay".** (a) A bay is a room of several beds, not a bed, so "bay 2" makes Sage ask if there is a bed 2. (b) Treat "bay" like "bed". Recommend (a).

---

## Attempt 4: what would change (proposed 4 October)

Attempt 3's design stays: find beds from the labels on the ward, ask when unsure, no-match for a bed nobody is in. The fixes for its review, all contained:

1. A unit-like word after a bed word never drops the reference. "bed 12 HR 140" is bed 12. Only a plural duration, degrees, a percent or a decimal after a bed word ("bed 2 days ago", "head of bed 30 degrees") turns it into an UNSURE reference (ask if someone is in that bed, otherwise text).
2. Bed references are read before ward mentions, and "N ward" never claims a number that follows a bed word. "bed 12 ward 6" narrows to ward 6.
3. A destination needs a movement verb (shift, move, transfer, send, put, admit ... to). "ABG from bed 45", "what happened to bed 45", "compare bed 12 to bed 45" are ordinary references.
4. A turn Sage only asked about stops the history walk.
5. Bed-word spellings: nos, nr, n°, nº, "=", "(", ",", newline, ordinals ("bed 45th"), common misspellings, fullwidth digits, thin and zero-width spaces; a number too long to be a bed after a bed word is no-match.
6. "the patient at/on N" is not a certain bed (time, count); it is a bare number, so Sage asks if bed N exists.
7. A list never continues into "65/M", "140/90", "4/5" or "N <letter>".
8. Another department's code counts only written as a code ("ICU-01", "icu01") or after a bed word, never "ICU 1 bed free".
9. Ward mentions windowed by position like the bed hits, so every pass is linear.
10. More "plainly not a bed" families, from the reviewer's ask list: 12-lead, a number right after a drug name or before a route or frequency ("metoprolol 25 bd", "ondansetron 4 iv"), counts ("2 sets", "3 episodes", "2 pints", "1 more dose"), times ("at 5", "on 3 Oct"), more lab and score words.
11. The cache key of the bed-word reader includes the ward vocabulary.

### Decisions needed

- **A. How Sage asks.** (a) The server writes the question itself, without the model, the way the out-of-department answer already works: "Which patient do you mean? Bed 12: <name>, ward 5. If you meant the patient whose chart is open, say so." Applies to every "which patient" turn, including today's name ambiguity. (b) Keep the model and add an explicit instruction to ask. Recommend (a): the reviewer's replay showed the model answering generically with invented citations when it was not told to ask.
- **B. The ask rate on numbered wards.** With item 10 the 61% should fall a long way, but it will not reach zero on a ward numbered 1 to 30. (a) Keep Q1: an unsure number asks. (b) Revisit Q1. Recommend (a): the alternative answers "how is 12?" from the open chart, and the old code put a different bed's chart in front of the model on 28 of the same 54 questions.
- **C. Build attempt 4 now, or pause.** Same process: tests first and shown failing, guard suites, a fresh reviewer with both earlier reviewers' probes.

---

## After attempt 4: where this stands (4 October)

On realistic questions attempt 4 put a wrong chart in front of the model 0 times in 104, against 28 of 54 and about 19 of 50 for the code running today. Every adversarial review still finds phrasings that reach the wrong chart, because any text the reader misses falls to the open chart, and the long tail of ward phrasing does not end.

Attempt 5 would fix every attempt 4 finding (save the which-patient turn; strip the view line from it; ask again on ANY reply that names nobody; never read a bed number after "to / for / of / at" as a value; voice-lane parity; Unicode digits; ordinals before day/week; "with / then / also" lists; a bounded cache; a ward-only question goes to the ward census, not the which-patient list). And it would add one structural backstop, so that the next phrasing nobody thought of is visible instead of silent:

- **E. Name the patient on every answer the clinician did not name.** When Sage answers from the open chart or the conversation's patient on a turn that did not name that patient, the server puts one line first: "About <name>, bed <bed>:". An order drafted on such a turn always asks which patient before it is drafted. A missed bed reference then produces a visibly labelled answer about the open patient, never a silent one, and no order can land on the wrong chart by phrasing.
- **F. Another department's code in a transfer phrase** ("transferred from ICU-03 yesterday", "is HDU-2 free?"): (a) a place in the open patient's story, keep the chart, no denial; (b) "outside your department", as attempt 4 did.

---

## Attempt 6 (4 October): a shipping bar, and attempt 5 plus fixes for its review

**G. The bar (Samuel, 4 October).** Ship when: (1) no wrong-patient path that the old code got right; (2) fewer wrong charts than the old code on realistic questions; (3) every remaining miss is visibly labelled on screen. Each review measures against these, side by side with the old code, not against zero.

**H. Orders (replaces ruling E's order half).** Sage asks before drafting only when the order contains a number that is a bed on the ward ("give lasix to 12"), or when it follows a which-patient question. A pronoun order with a chart open ("give him lasix") drafts as today; the label names the patient and the action card needs approval.

Changes from attempt 5, one per review finding:
1. "head end of bed 30-45 degrees", "foot end of bed", "out of bed", "propped up in bed": a position, not a bed word. A bed number cut short by "-45 degrees" or "-3 days" is unsure (asks), never certain.
2. Ruling F narrowed to what it was for: another department's code with no bed word never draws an out-of-department refusal unless the turn reaches for a patient (the old gate's rule), and transfer phrases keep the chart. "is bed 12 ready for discharge?", "going to bed 12", "she was in bed 12" are ordinary references again.
3. The "About <name>, bed <bed>:" label goes into the finished answer the screen shows and into the saved turn, not only the stream; transferred and discharged patients are labelled too, and the voice fallback chart is labelled.
4. A which-patient question is recognised from the saved assistant turn, not by re-reading the user's message, so census answers never trigger it and the order flow cannot slip past it. After it, any reply that names nobody is asked again (no limit); "this patient" (not negated: "not this patient", "I don't mean this patient") or a bed answers it.
5. No more forced no-match for a second number after a bed ("bed 12 2 vomits", "Bed 12, 65 male"): the bed stands, or Sage asks. Case-presentation words (male, female, old, yrs) end a list; "3x" and "20s" are not beds.
6. The history walk stops at any turn that names a bed or a bare number, and a reply that is only a bed answers the question there too.
7. More list words ("aur", "n", "+", "as well as", "along with", ";").
8. Speed: bed labels indexed by their first two parts; history turns read once per turn; long-question caches hold a few entries and are emptied each turn.

---

## Attempt 7 (4 October): attempt 6 plus fixes for its two blocking classes

Judged against the same bar (ruling G). Changes:
1. Number words are read as a bed only when they ARE the whole reference: "bed twelve?", "bed twenty one, lactate?". "bed one more day", "in bed one month", "the patient in two minds" are ordinary words again, as in the old code. "the patient in" never takes a number word.
2. After a bed word, a number followed by cm, mm, min, minute, hour, inch, am or pm is unsure (Sage asks), never a certain bed ("raise bed 15 cm", "keep her in bed 30 min").
3. Never discarded as a dose or a value: this department's own bed code ("GM-512 iv lasix 40 stat", "GM-512 HR 140" are GM-512), and a bare number that OPENS the message ("12 iv lasix 40 stat", "12 nebs given?", "14 day 5 of meropenem": Sage asks). A spaced code followed by a unit counts only when it is a bed here ("gm 512 iv lasix" yes, "ceftriaxone gm 12 hourly" no). Two numbers joined by and / or ("ceftriaxone 12 and 14?") are asked about.
4. Typed spacing the old code tolerated: "12 a" for bed 12A; "c 4" for bed C-4 when it opens the message (asked).
5. Another department's code is a reach for its patient only when the reach verb is followed by the code and then the end of the clause ("summarise ICU-03"); "show me the ICU-02 notes for her" is not.
6. "bed 12, 45 potassium?": a later list item that is not an age, sex or value is a bed, so a missing one is no match (D3) again.
7. The label is not inserted into a fast-contract "ACT|" answer (it broke the contract on reload; the action card names the patient).
8. The per-turn history cache is per thread.
Known and accepted: voice cannot ask again after its question, because the voice screen sends no conversation history (labelled answers instead).

As built (4 October), with these details settled during the build:
- "The whole reference" for a number word: number words up to the end of the clause, optionally followed by doing / now / today / tonight / overnight / please ("how is bed twelve now?").
- A number opens a line when only spaces or bullets come before it, at the start of the message or of any line. It stays a value there only before an everyday count (sets, episodes, pints, units, more, options...), an ECG lead, a decimal, time, date, percentage or an age or sex. Durations ("14 days of meropenem") and devices ("12 drains removed?") ask.
- A number before a route or frequency word (iv, po, stat, nebs, bd...) is a dose only when a drug or measure word comes before it ("lasix 20 iv"); otherwise Sage asks ("give 12 iv lasix 40").
- A code of ours written with a space before "bed", "nurse" or "nights" is a count in a unit, not a bed ("is a CCU 1 bed free?").
- One-letter codes typed with a space ("c 4", "n 1") ask anywhere, not only at the start, when that bed is on the ward; never for a, i, o or x.
- A bed word, a bed nobody here is in, then a bare second number ("bed 45 2 vomits") is no match; a second number followed by a time or an angle ("bed 30 45 degrees", "bed 12 2 days post-op") makes the bed unsure (asked about).
- "12/14" is a list like "12, 14"; two bare numbers joined by and / or at the end of a clause are asked about, but not a range ("between 88 and 92").
- The history memo keeps the census list it was read against, so an id() reuse can never serve another list's reading.
Tests: tests/test_sage_bed_locator.py, 633 tests (with test_sage_name_match_bounds.py, 652 pass); 472 fail on the code before this work. The attempt 6 reviewer's probe sets re-run: 0 wrong charts in every realistic set (old code 77), no regressions, 3 labelled misses the old code also got wrong ("ICU-02 hr 140", "SX-17 potassium?" with no reach verb; "bed 45 2 vomits", since fixed). Code, patches and probe output: docs/archive/bed-locator-attempt7-2026-10-04/; pre-change copies: docs/archive/pre-bed-locator-a7-2026-10-04/.

## Attempt 8 (4 October, measured, not built): the closed list moves to the certain side

Samuel chose to measure this before any build. Attempts 3 to 7 listed what makes a bed word NOT a bed, so every unlisted word stayed a certain bed and a wrong chart. Attempt 8 lists what makes it CERTAIN; every other shape is unsure, which means Sage asks when that bed is occupied and reads it as text when it is not.

A bed written with a bed word is certain only when:
- a recognised lead-in comes right before the bed word: "how is", "summarise", "update on", "who is in", "the patient in", "of", "for", "about", "compare", "is", an order verb ("give", "start", "hold"...), or a measure or drug word with "in / on / at" ("her potassium in bed 15"); or
- it starts the message, a line or a sentence (or follows "and", "to", "from", "ok", a plain comma...), AND a recognised continuation follows the label: the end of the clause, a list, a measure ("HR 140", "day 3" with its number), a drug, an order verb ("bed 12 start ceftriaxone"), a handover word ("h/o", "k/c/o", "d/c", "post-op", "today", "is", "has"...), or a number;
- or it sits inside a label of OUR ward written whole ("ICU Bed 3" where that is our bed), or beside a ward of ours ("ward 6 bed 12", "GM-1 bed 2").
Anything else is unsure: another unit's name before "bed" ("transferred from ICU bed 12"), a posture ("flat in bed 12 hr"), a pronoun ("her bed 10 metres from the station"), an unknown continuation ("bed 12 noon onwards"), "beds" with a single number ("total no. of beds 30"). Units that double as measures ("hr", "day", "o") continue a reference only with a number after them. One-letter codes of ours ("K4") follow the same rule, and a decimal after one ("K3.2") is a value. A spelled number needs a plain space after the bed word ("pillows in bed: two" is text).

Measured in the scratchpad against every reviewer's question set (911 rows, OLD = the committed code):

| | Attempt 7 | Attempt 8 prototype | Old code |
|---|---|---|---|
| Wrong charts, realistic sets (numbered ward + GM naming) | 2 (unlabelled) | 0 | 91 |
| R or U rows across ALL probe sets, attack sets included | 66 (B1, B2, B3, census, U-same) | 0 | n/a |
| Ask rate, realistic sets | 0 to 20%; orders 10% | the same: 0 to 20%; orders 10% | 25 to 75% (mostly wrong) |
| Labelled misses (L) | 11 | 11 | n/a |
| False no-match or lost chart (F) | 7 | 6 | n/a |

What it costs, phrasings that were a certain bed in attempt 7 and now ask (the bed is still named in the question): "bed 12 drops sats when flat", "bed 12 feet are cold", "she was in bed 12 last night, how is she now?", "bed 12 A potassium?", "bed 12 a bit better today?". What it fixes: all 30 B1 phrasings and the 3 census ones now ask (numbered ward) or keep the open chart labelled (GM ward, where attempt 7 said no match); all 24 B2 phrasings and the 3 B3 ones ask or keep the chart. The L rows are labelled open-chart answers the old code also gave: spelled beds followed by words ("bed twelve potassium?"), another department's code with no reach verb ("SX-17 potassium?"), "bed 45 2 days post op".

Ruled (4 October, keep D3): "Bed 12, 56, k/c/o DM" (age first, no sex) is no match under D3, because 56 reads as a second bed. Options: (a) keep D3 (safe, the clinician retypes); (b) after a singular "bed", a comma item is a bed only with its own bed word ("bed 12, bed 45"), so case presentations work, but "bed 12, 45 potassium?" then answers bed 12 alone.

Build checklist if approved: start from main with the ward-matcher work's services/ward_reference.py (wards through ward_named_in with the text as typed; keep tests/test_ward_reference.py green); attempt 7's code plus the prototype diff (docs/archive/bed-locator-attempt8-measure-2026-10-04/a7_to_a8_prototype.diff); fix the quadratic whitespace patterns the attempt 7 review found (no `\s*-?\s*`); put the label on reloaded order turns (/history shows the stored safe answer); tests for every attempt 7 review row; then the guard suite and a fresh review against ruling G.

### Attempt 8 as built (4 October, night)

Built in a scratch copy on main at e82bd24 (the ward-matcher work's services/ward_reference.py, Samuel's word-code ruling), then ported. Changes from the measured prototype:
- Wards: a ward named on its own is decided by `ward_named_in(label, text as typed)`, the rule the PHI classifier reads, with the text already read as beds or bed codes blanked by equal-length spaces (so "GM-103" never names GM-1 and another department's "ICU-06" never names our ICU). A ward named on its own is answered by the ward census (BROAD), never one patient's chart in place of the open one. Since the shared rule counts a ward anywhere, "should she go to CCU?" gets the CCU census again, as in the old code (attempt 7 kept the open chart).
- The lead-in and continuation checks are read off the runs and sets, not a regex search per bed word (the regex version cost 5 s on 512 KB of "bed 7"; now 1.2 s).
- Whitespace: every locator pattern that ran an optional character between two whitespace runs is possessive (`\s*+`), so long runs of spaces are linear (attempt 7: 15 s for "how is 12" plus 40,000 spaces; now 0.02 s).
- A reloaded order turn keeps the label: the stored safe answer carries it (attempt 7 reloaded "Drafted request for ..." unlabelled).
- Test file: tests/test_sage_bed_locator.py, section 26 holds every blocking row from the attempt 7 review (B1 on a numbered ward asks, on GM naming keeps the chart; B2; B3; recognised shapes stay certain; one-letter codes; whitespace timing; reload label). 716 tests; 514 fail on the code before this work. The ward-matcher work's tests/test_ward_reference.py needs four edits that land with this change (census for a ward on its own; the which-patient turn never reaches the model; a census turn is PHI; retired probe patients renamed so "ward" in a later question cannot match them).

## Attempt 9 (6 October): confirm another bed while a chart is open

Samuel's choice after attempt 8's review: while a chart is open, or the conversation is already about one patient, Sage never switches to ANOTHER bed on its own reading of the words. It asks instead ("Which patient do you mean? - Bed 12: <name>, ward <ward>. Reply "yes" for bed 12, or with another bed or name."), and "yes", "12" or "bed 12" confirms. Eight attempts at reading every phrasing each failed on phrasings no list covered, and every failure was the same event: another patient's chart opened in place of the open one. This removes that event for every phrasing, whatever the reader gets wrong.

Rules:
- Applies to beds read as CERTAIN (bed words, our codes, the patient in N) that are not the referent. The referent is the open chart (patient_id, in department) or, with none, the conversation's patient (the history anchor).
- Not asked: a patient named by NAME, the open patient's own bed ("bed 7 potassium?" with bed 7 open), a reply to the which-patient question itself (that IS the confirmation), and turns with nothing open (no referent: the bed opens its chart as in attempt 8).
- With NOTHING open, a question that speaks of a patient as she, he or they ("her husband in bed 12 has covid, should she be tested?") confirms another bed too: that patient is not the one in the bed. None of the realistic questions that rightly named another bed said she or he (attempt 8's review data), so this costs no extra questions there.
- After the which-patient question, only a reply that is nothing but a bed ("12", "bed 12", "yes bed 12") is the confirmation; "no, she is next to bed 12" is asked again.
- "yes" (and yeah, ok, correct, go ahead, haan...) confirms only a question with exactly ONE candidate; after a question with two it is asked again, as before. The answer after "yes" carries the "About <name>, bed <bed>:" label, since the clinician did not type the name.
- A bed waiting to be confirmed is asked about before the census lane answers ("summary of bed 12" with bed 7 open).
- The reader is attempt 8's (certain-side lists), so with nothing open the same reading decides, and with a chart open it decides only between asking and no match.
Also fixed from attempt 8's review: our codes written with dots or a spaced dash ("G.M.-103", "GM - 103") are read again, as the old code did; labels of three or more runs ("ICU Bed N", 60 beds) are indexed by their first three runs (4 to 7 s at 512 KB before).

Measured from attempt 8's review data before building: on the realistic sets about 90 of 560 answers were a certain other bed, so with a chart open those become one extra reply (about 16 points more asks overall, 40 to 50% on the bed-heavy sets). Every one of the 115 blocking rows from attempt 8's review was a certain other bed, so all of them become questions.

### Attempt 9 as built (6 October)

Built on main at 93d0d38 (the shared ward rule with generic and destination mentions) from attempt 8's code, then ported. tests/test_sage_bed_locator.py: 772 tests, 588 of the 769 before the last three fail on the code before this work; every reviewer-8 blocking row is a test. The ward-matcher session approved the same four edits to tests/test_ward_reference.py. Every reviewer probe set (46 files, attempts 2 to 8, attack sets included) re-run on the build: 0 regressions and 0 unlabelled wrong charts; asks 0 to 4% on everyday sets and 27 to 50% on the bed-heavy sets, as measured beforehand. Guard suite on main: 2,763 passed. Code, patches, probes: docs/archive/bed-locator-attempt9-2026-10-06/; pre-change copies: docs/archive/pre-bed-locator-a9-2026-10-06/.

## Attempt 10 (6 October): every bed is confirmed

Samuel's choice after attempt 9's review, which showed the confirmation rule holding wherever it applied and failing only where it did not. Sage never opens a bed's chart on its own reading of the words, with a chart open or with nothing open.

Rules:
- A bed read as certain is asked about: "Which patient do you mean? - Bed 12: <name>, ward <ward>. Reply "yes" for bed 12, or with another bed or name." The open patient's own bed is the only bed that opens directly.
- A patient named IN FULL is not asked about (one name word is not enough: "she is short of breath" and a patient called Short).
- The reply confirms only when it is "yes" (one candidate) or exactly that bed's stored label ("12", "bed 12", "GM-103 please"), compared with the labels themselves, so "not 12", "except 12" and "nahi 12" are asked again.
- The conversation walk stops at any turn Sage answered with the which-patient question, and reads a reply to the question as the clinician's choice.
- With a chart open and the conversation about someone else: an unnamed follow-up stays with the patient the clinician just chose in answer to the question ("yes" to bed 12, then "add kcl 20 mmol" is bed 12, labelled), and asks when that patient was only named earlier.
- Voice sends no conversation history, so it reaches another bed by name only.
- A question with more than 1,000 bed references (bed words, codes or kept bare numbers) is a paste, not a question: Sage stops reading and asks which patient, with no chart. Reading every one of 87,000 "bed 7" cost about 2.3 s at the 512 KB cap (every attempt since 3 paid it), and under machine load that broke the 3 s budget.
Cost, from attempt 9's review measurements: asks with nothing open rise from about 10% to the chart-open level, about 25% overall on the realistic sets (the old code asked on 57% and opened a wrong chart on a third).

### Attempt 10 as built (6 October)

Built on main at 784f30a from attempt 9's code. tests/test_sage_bed_locator.py: 819 tests; tests/test_ward_reference.py carries the same four edits the ward-matcher session approved; tests/test_sage_department_scope.py::test_own_patient_by_bed_still_resolves now expects the which-patient question naming that patient. Every reviewer probe set (49 files, attempts 2 to 9, attack, nothing-open and route sets included) re-run on the build: 0 regressions and 0 unlabelled wrong charts, with a chart open and with nothing open; asks 0 to 4% on everyday sets and 27 to 50% on bed-heavy sets in both lanes. Guard suite on main: 2,816 passed under heavy machine load. Code, patches, probes: docs/archive/bed-locator-attempt10-2026-10-06/; pre-change copies: docs/archive/pre-bed-locator-a10-2026-10-06/.

## Attempt 11 (6 October): attempt 10 with its holes closed

Same rule as attempt 10 (every bed is confirmed); Samuel approved fixing what the review found:
- Every bed reading is confirmed, certain or unsure, and a name match never removes a patient from the question ("her husband Ravindran in bed 18" opened bed 18 in attempt 10).
- "Named in full" means two or more of the patient's name parts of three letters or more, or a stored name of two or more parts written whole; one typed word never counts ("Murugesan", "Selvaraj" for "Selvaraj K"). A full name does not skip the question when the question speaks of someone as she, he or they ("her husband Selvaraj K in 19").
- Each saved turn records the chart that was open (route: "open_patient_id" in the stored blob, read back into the history rows). A patient chosen in reply to the question is followed only while that same chart stays open; after the clinician opens another chart, or if nothing was open when they chose, Sage asks.
- The open patient's own bed is answered, never asked ("how is 7?" with bed 7 open).
- A repeated question keeps its candidates, so "yes" can still settle it; a reply counts as a bed only for the beds the question offered, and a bed label that was not offered asks the same question again ("2" after a question offering beds 12 and 14).
- Another department's code beside one of our beds is a place unless a reach verb is right before it ("bed 13 was in ICU-03 before" is not refused).
- The 1,000-reference paste cap now applies after the out-of-department check, so a name from another department written in full in a paste is still refused and audited (one word of a name is not, since nothing in the paste was read as a bed).

### Attempt 11 as built (6 October)

Built on main at 1c9ba31 (the ward-matcher work's several-word ward codes) from attempt 10's code. tests/test_sage_bed_locator.py: 862 tests; tests/test_ward_reference.py carries the same four edits, approved again for the ward-matcher work; tests/test_sage_department_scope.py as in attempt 10. Every review probe set (65 files, attempts 2 to 10, attempt 10's included) re-run on the build with attempt 10's invariant recorder: 7,340 retrieval calls, no chart other than the open one opened unlabelled without a full name, a reply to the question or a name word; the only unlabelled ones are name-only questions with no bed, which the old code opens the same way (the name matcher). 0 regressions. The new lost-chart rows are the designed questions (after a chart switch; "her husband X in bed 18 ... start her on ..." asks about bed 18). Code, patches, probes: docs/archive/bed-locator-attempt11-2026-10-06/; pre-change copies: docs/archive/pre-bed-locator-a11-2026-10-06/.

## Attempt 12 (6 October): no exceptions

Samuel's choice after attempt 11's review, in which every blocking finding came through an exception to the confirm-every-bed rule. The rule now has none:
- Every bed reading is asked about, certain or unsure, including the open patient's own bed and a patient written in full with their bed ("how is Ravindran Kuppusamy in bed 18?" asks once; "yes" opens it). A name with no bed is the name matcher's lane, unchanged.
- A chart opens on a bed only through a reply to the which-patient question: "yes" when it offered one patient, or a reply that is exactly the stored label of a bed it offered. When it offered patients and none is still in that bed, no reply opens a chart (attempt 11 fell back to the whole department and opened a newcomer). A reply naming a bed it did not offer starts a new question about that bed.
- A patient chosen in reply is followed for unnamed follow-ups only while every turn since stayed on the chart that was open when they chose (each saved turn records the open chart); a census question on another chart in between ends it.
Cost: one more reply when the clinician types the open patient's own bed or a full name with a bed; none of the realistic question sets contain either.

### Attempt 12 as built (6 October)

Built on main (after d9553ad's vitals filters and 1c9ba31's ward codes) from attempt 11's code with the exceptions removed; `_named_in_full` is gone again, so nothing is added to the name-matcher work's block. tests/test_sage_bed_locator.py: 878 tests; tests/test_ward_reference.py: the same four approved edits; tests/test_sage_department_scope.py as in attempt 10; tests/test_sage_patient_phrasing.py::test_summarise_a_real_patient_by_name_and_bed_finds_the_patient now expects the which-patient question naming that patient. Every review probe set (71 files, attempts 2 to 11) re-run with the invariant recorder: 7,912 retrieval calls, 0 violations; the only unlabelled answers from a chart other than the open one are name-only questions with no bed (12 cases, handed to the name-matcher work in docs/archive/bed-locator-attempt12-2026-10-06/name_only_cases.txt). Build guard run: 2,890 passed. (The probe check above was wrong: the re-run started on an earlier build and the recorder counted any chart opened after a question as a reply; see the findings doc, "Attempt 12 review".) Code, patches, probes: docs/archive/bed-locator-attempt12-2026-10-06/; pre-change copies: docs/archive/pre-bed-locator-a12-2026-10-06/.

## Attempt 13 (9 October): stop rebuilding the conversation

Samuel's choice after attempt 12's review ("Stop rebuilding"). Attempt 12 kept its rule everywhere; both blocking classes came from Sage reconstructing the conversation from saved text. Attempt 13 is attempt 12 plus three changes:
- A which-patient question that lists nobody (a re-ask after the offered patient left their bed, the paste-cap question, the "not this patient" question) accepts no bed in reply. A bed typed in reply is read as a new question, so Sage asks about the patient in that bed now, and "yes" confirms. Attempt 12 read a question listing nobody as "any bed may answer it", and "12" opened the newcomer in bed 12.
- A patient chosen by bed is never carried forward on their own. When a later unnamed turn ("potassium?", "give her lasix 40 iv") would go to a patient chosen by a reply to the question and that patient's chart is not the one open, Sage offers them again ("Which patient do you mean? - Bed 13: ... Reply "yes" for bed 13"), and "yes" settles that turn. This holds with a chart open or not. There is no record of chart visits to keep in step, so turns Sage never saved (refusals, failures, other conversations, voice) cannot matter. The saved open-chart field that attempts 11 and 12 added for that chain is removed.
- Earlier messages carry a patient forward only when the clinician named them by name. A bed in an earlier message is never carried forward. A turn that read one patient by bed offers that patient again, as above. A turn that read several, or an unsure bed, carries nobody. This closes the aggregate-lane gap ("length of stay of cot 12?", then "give her lasix 40 iv").
Unchanged from attempt 12: every bed reading is asked; a chart opens on a bed only through a reply to the question ("yes" when it offered one patient, or the exact label of a bed it offered); the name lane; the aggregate lane itself.
Cost: one extra "yes" on each unnamed follow-up after reaching another bed by reply, unless the clinician opens that patient's chart.
Checks before review: every probe file re-run on the FINAL build (hashes recorded right before the run); the invariant recorder counts a chart as opened by a reply only when the question before it offered that patient.

### Attempt 13 as built (9 October)

Built in a scratch copy from attempt 12 ported onto main (889b4d4's scoring switch, 8325631's voice change), then applied to main on top of an uncommitted parallel scoring-switch edit in sage_service.py (its 12 lines kept; the result differs from the build only by them). Guards on main: 2,920 passed. Then frozen into a scratch copy for review and backed out of main by reverse patch, because a parallel verification step stages the whole tree with `git add -A` (the Ward Record build's bf34f16 had already swept the sage_service.py hunks into HEAD, and cfa61b7 took them back out). If the review passes, it is re-applied onto main as it is then and committed alone. Code: `_candidates_of_question_text` returns [] for a question listing nobody and `_reply_bed_patient(text, offered)` reads only the offered patients; `_conversation_patient` returns (id, "reply" | "bed" | "named") with no open-chart chain; `_offer_from_history` and `_conversation_differs` make both follow-up lanes ask; the route no longer saves `open_patient_id` and history rows no longer carry it. tests/test_sage_bed_locator.py: 904 tests. `_asked` now writes a question listing nobody (attempt 12's "- Bed 12: someone" never parsed as an offer, so those tests passed only through the B-A hole), and `_asked_about` writes the real question. New tests cover B-A through the route (re-occupied bed, chart open or not, an order), B-C (a refusal, a census turn or nothing in between), variant H and D. All fail on attempt 12 or main's code, except the aggregate tests, which pass on main by design. Build guard run: 2,883 passed. Every probe file (79, attempts 2 to 12) re-run on the final build, with hashes recorded first: 8,467 retrieval calls, 0 violations under a stricter checker (a chart opened after a question counts only when that question offered that patient); 133 charts opened on a reply to an offering question (every "yes" labelled), 7 carried by a name, 13 named in the turn (the 6 partial-name ones are the name lane). The only file changed after hashing is a comment in the confirm block; reverting it reproduces the recorded hash. The 3 failing probe files fail on harness names attempt 3 renamed, as in review 12. Realistic sets: 0 wrong charts, single-turn asks unchanged from attempt 12. Code, patches, probes: docs/archive/bed-locator-attempt13-2026-10-09/; pre-change copies: docs/archive/pre-bed-locator-a13-2026-10-09/.

## Attempt 13b (9 October): the reply decides who, the earlier message decides what

Samuel's choice after attempt 13's review: fix S1, review again, then commit. Attempt 13 is unchanged; the fix is one addition.
- When a reply to the which-patient question settles the patient, the reply is only the WHO. A reply settles it when the turn follows the question, a chart answers it, and every word of the reply is a confirmation or filler word ("yes", "ok", "the", "one", "please", "this patient", "the lady"), part of that patient's name, or part of their bed or ward. The WHAT is the clinician's earlier words: the message the question was about, walked back through any re-asks to the message that started the exchange (pure "yes" and bare-bed replies left out).
- On such a turn the order gate runs on every message in that exchange as well as the reply, so "give her lasix 40 iv", offered again, then "yes" is gated as an order: the model's narration never reaches the screen, a supported order becomes a drafted request for approval, and anything else gets the unsupported-action message. Guideline retrieval, the answer-grounding check and the model's user message use the earlier words instead of "yes".
- A reply that carries words of its own (a new question, "how is Ravindran Kuppusamy?") is the clinician's new message: its own text, its own gate, nothing carried from the exchange. Drafting the earlier order on whoever the new message names would put an order on the wrong patient.
- The exchange is read from up to 20 saved messages (the prompt window stays 6), so a re-ask chain does not lose the message that started it.

### Attempt 13b as built (9 October)

Built on a copy of the attempt 13 review copy. sage_service.py: `_reply_is_only_who`, `_question_exchange`, `_exchange_what`; when a reply settles the question, retrieval adds `question_exchange` and `question_what` to the chart dict and searches guidelines with the earlier words. sage_routes.py: reads up to 20 saved messages when the last turn was the question and passes them as `question_history`, but only then, so every other turn makes the same call as before (the first build passed it always, and 34 guard tests whose stubs take four arguments failed); after retrieval, gates every message of the exchange, locks the streamer, and gives the model and the grounding check the earlier words. tests/test_sage_bed_locator.py: 941 tests, 37 new; every new S1 test fails on attempt 13. Guards plus the locator file: 3,898 passed (2 h 7 min at load 13 to 18). All 85 probe files (review 13's included) re-run on the final build with hashes recorded first: 9,182 retrieval calls; the strict checker flags one row, review 13's own model-written question with a NOACT| marker in front, which the code reads as the question as it did in attempt 13 (reported then as not blocking). Review 13's S1 probe now shows the unsupported-action message on every narration it tried for the unnamed follow-up order. One flow in it still shows the narration: "bed 13 give lasix 40 iv", then "yes". The exchange is gated, but the order detector does not see an order in that wording even typed directly ("bed 13 give lasix 40 iv" and "lasix 40 iv bd for bed 12" both read as no order), so the old code shows it too; that detector gap was first reported in attempt 5's review and is outside this fix. Code, patches, probes: docs/archive/bed-locator-attempt13b-2026-10-09/.

## Attempt 13c (9 October): the model only ever sees the settled request

Samuel's choice after attempt 13b's review, with a new order bar: blocking is a model's narration of an order reaching the screen, or an order drafted on a patient the clinician did not confirm for it; a turn that drafts nothing and claims nothing is a lost order, reported but not blocking. Attempt 13c is attempt 13b plus three changes:
- The exchange ends at a reply that brought a different patient into the question. Walking back from the question being answered, Sage steps over an earlier question only when the later question offers nobody it did not (a re-ask keeps or drops candidates; a new question about GM-515 adds one). So "give lasix 40 iv to GM-512", then "how is GM-515?", then "yes" answers "how is GM-515?" and carries no lasix. The walk also ends at a reply that cancels or negates ("cancel that", "no, don't give it": cancel, don't, not, never, no, stop, hold, wait, forget). Ending early only ever loses an order, never moves one.
- The model never sees a request Sage answered with the which-patient question, nor the question itself. Every such pair is left out of the history the model is given. The settled request reaches it only as the user message on the turn a reply settles it. A reply that is a message of its own ("<name>, is she febrile?") goes to the model with its own words, and the earlier order is not in front of it to narrate. This closes G1's cause, whatever words the reply holds.
- A reply that settles the patient may also hold go-ahead words (stat, now, asap, urgent, urgently, immediately, quickly, only, instead, too, also, and, give, start, continue, proceed) and any word of the request it settles ("this patient, the lasix"). Otherwise these replies are lost orders, not false claims.
The request handed to the model and to guideline search is capped at 512 KB, the most the old code ever sent. The gate still reads each message of the exchange on its own.

### Attempt 13c as built (9 October)

Built on a copy of the 13b build. sage_service.py: go-ahead words in `_REPLY_FILLER_WORDS`; `_EXCHANGE_STOP_WORDS`; `_reply_is_only_who(text, patient, extra)` takes the request's own words; `_offered_lines` and the new-patient and stop-word ends in `_question_exchange`; `history_for_model`; `_exchange_what` capped at 512 KB. sage_routes.py: the model is given `history_for_model(history)`. tests/test_sage_bed_locator.py: 970 tests, 29 new, every one failing on 13b. Guards plus the locator file: 3,956 passed. All 91 probe files (attempts 2 to 13b, review 13b's own included) re-run on the final build with hashes recorded first: 9,778 retrieval calls, one flagged row (the known model-written question). Review 13b's R1 flows now give the model only the new question and draft nothing; its G1 replies are gated. Two of its rows still show a draft or a narration, from a stub that declares or narrates the same reply whatever it is given: "no, don't give it" then "GM-512", and a discharged patient's name in reply. In both the order was in neither the model's message nor its history. Code, patches, probes: docs/archive/bed-locator-attempt13c-2026-10-09/.

## Attempt 13d (9 October): an order crosses the question only in the certain case

Samuel's choice after attempt 13c's review ("Carry only the certain case"). Attempt 13c stays (no follow, the exchange ends at a new patient or a cancel, the model never sees an asked-about request). What changes is how a reply that settles the question treats the message the question was about:
- That message is an ORDER (the order detector reads it as one). It crosses the question only when the question offered exactly one patient, the chart answering is that patient, and the reply is exactly "yes" (the yes-only replies) or exactly that patient's bed label. Then the order is gated, given to the model and drafted for approval, as in 13b. A reply that only names the patient or says go-ahead words ("this patient, stat", "<name>, give it", "and <other name>?"), or that holds a cancel or negation word ("<name>, no thanks", "this patient, no lasix"), gets a fixed message written by the server, with no model call: "About <name>, bed <bed>: Nothing has been drafted. To draft an order for this patient, type it again." A reply with words of its own is a new message with its own gate; the order stays hidden from the model.
- That message is NOT an order (a read). A reply made only of who-words carries the read, with any order elsewhere in the exchange left out. So "give lasix 40 iv to Mr <surname>", then "how is GM-533?", then "yes" answers "how is GM-533?" and carries no lasix, and a two-patient order never reaches the model whole.
- The go-ahead and request words 13c added to the who-words are used only to recognise the replies that get the fixed message. They never carry an order.
- Read context after a settled reply is restored: in the history given to the model, a reply that settled a read is shown as the read it settled ("is GM-512 on heparin?", not "yes"). Orders are never put back.
An order a reply does not carry is a lost order (Samuel's bar: reported, not blocking). Retyping completes it: the retyped order is asked about once and "yes" drafts it.
Two additions made while building:
- The question invites exactly "this patient" for the open chart, so an exact "this patient" (or "this patient please", "yes, this patient") is a certain reply too, when the open chart is what answers. "this patient, stat" is not.
- A message that read more than one bed is never handed over for one of them, read or order. The order detector does not read every order ("GM-512 needs lasix 40 iv, and check GM-513 potassium" reads as no order), so such a message would otherwise go to the model whole on one patient's chart. The reply gets a second server notice: "Your message was about more than one patient, so nothing from it was carried over. Type it again for this patient." A question that offered several patients because a NAME matched them all (no bed read) still carries its read.

### Attempt 13d as built (9 October)

Built on a copy of the 13c build. sage_service.py: `_GO_AHEAD_WORDS` split from `_REPLY_FILLER_WORDS`; `ORDER_NOT_CARRIED_NOTICE` and `MANY_NOT_CARRIED_NOTICE`; `_order_reply_is_certain` (one patient offered and exactly "yes" or that bed, or exactly "this patient" for the open chart); `_beds_read`; `_read_what` (reads never carry an order) replaces `_exchange_what`; the settle block returns a NOT_CARRIED notice where the plan says; `history_for_model` shows a settled read as the read. sage_routes.py: the which-patient branch also serves NOT_CARRIED with no model. tests/test_sage_bed_locator.py: 997 tests; every new 13d test fails on 13c, except the guards that a shared-name read and an exact "this patient" still work. Found while building: the test DB has no active furosemide, so a declared order always ends "not_in_helix_database", and the 13c tests that asserted "nothing drafted" passed whatever the code did. `_drafted_for` now reads where the declared order landed, a control test proves the stub can place one, and the order tests use a stub that declares only when the order is in front of it. Also added, from the order-gate work's report: "bed 713 give lasix 40 iv" on a GM ward is refused as outside the department on main (another department's bed "D7" found inside "bed713"); the test fails on main and passes here. Guards plus the locator file: 4,010 passed. All 97 probe files (attempts 2 to 13c, review 13c's own included) re-run on the final build with hashes recorded first: 10,171 retrieval calls, one flagged row (the known model-written question). Review 13c's 24 threads: 23 orders drafted on the right patient, 0 on a wrong one (13c: 2), 4 lost, 0 false blocks, 0 label misses; the 3 narrations with the order in the model's input are the pre-existing class (an order drafted at its own turn stays in history), which the old code shares. Code, patches, probes: docs/archive/bed-locator-attempt13d-2026-10-09/.
