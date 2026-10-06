# Sage bed lookup: build plan, 3 October 2026

Status: RULED (R026 (a), D2 to D4, Q1 to Q3, A to F, G, H). Attempts 2 to 6 failed review and were backed out; main has the original substring bed matching plus the shared ward rule. Attempt 7 (attempt 6 plus the fixes below) was built on 4 October, failed review against ruling G and was backed out (see the findings doc, "Attempt 7 review"). Attempt 8 was measured, approved and built on 4 October (D3 kept for comma items), failed review on 6 October and was backed out (findings doc, "Attempt 8 review"). Attempt 9 (confirm another bed while a chart is open) chosen by Samuel on 6 October, built, failed review the same day on the lanes it did not cover (nothing open, a lapsed conversation patient, "not 12" as a reply) and was backed out (findings doc, "Attempt 9 review"). Next step: Samuel's call.
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
