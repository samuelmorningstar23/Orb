# Sage's order gate reads ward-round wording (9 October 2026)

## What was wrong

`detect_mutation_intent` (server/services/sage_service.py) is Sage's order gate. It runs before any model, and when it fires the turn is buffered and the action-state override shows a drafted request (which needs a clinician's approval) or the unsupported-action message instead of the model's prose. When it does not fire and the model declares no action, the model's own words reach the screen.

It read an order only when the verb came first. Found during the bed locator work (R026, attempt 13b): "give lasix 40 iv to bed 13" was gated, "bed 13 give lasix 40 iv" was not, and neither were "bed 12: lasix 40 iv stat", "lasix 40 iv bd for bed 12" or "inj lasix 40 iv stat". With a model that narrates, the clinician read "Furosemide 40 mg IV has been given and the order is placed." The forbidden-phrase strip removed only the leading "Done."; it leaves every passive narration ("has been given", "the order is placed"), and widening it is not safe either, because passive forms like "has been administered" occur in real Sage answers as legitimate clinical prose (4 of 540 in docs/perf/sage-*.json).

## How it was measured

Every number below is from a run, not an estimate.

- Hand-built set (164 orders, 120 reads), weighted to the hard forms: bed first, drug first, no verb, verb after the drug, shorthand, Hinglish. Tuned against, so not evidence of recall.
- The bed locator reviewers' realistic sets from reviews 3 to 13 (174 medicine orders, 411 ward questions), read out of their probes.
- Four corpora written blind by independent agents that never saw the gate or the corpus: set 1 (150 orders, 250 reads), set 2 (150 orders, 300 reads), set 3 (200 orders, 300 reads) and set 4 (200 orders, 300 reads). Each was scored once on a frozen build (hash checked) before anything was changed in response to it. Sets 3 and 4 were written after the rulings and follow them; sets 1 and 2 were written before.
- A stress set: every verbless order line wrapped in 52 report and question phrasings ("on X", "X given at 8", "X day 3", "X safe in ckd", "X diya kya"), 2,600 rows.

Orders caught, then reads tripped, per blind set. "blind" marks the one run of a build on a set it had never seen; every later number on that set is after tuning against it.

| Gate | Set 1 (150 / 250) | Set 2 (150 / 300) | Set 3 (200 / 300) | Set 4 (200 / 300) |
|---|---|---|---|---|
| Before (main, 9 October) | 35 (23%) / 6 | 26 (17%) / 4 | 42 (21%) / 8 | 54 (27%) / 7 |
| First prototype | 110 (73%) / 8, blind | 79 (53%) / 5 | | |
| Second prototype | 144 (96%) / 8 | 102 (68%) / 5, blind | | |
| First build | 145 / 1 | 132 / 0 | 173 (86%) / 5, blind | |
| Second build | 144 / 1 | 133 / 0 | 183 (92%) / 4 | 164 (82%) / 4, blind |
| Landed build | 144 / 1 | 134 / 0 | 187 (94%) / 4 | 188 (94%) / 2 |

The blind runs are the honest figures: orders caught 73%, 68%, 86% and 82% (old gate on the same sets 23%, 17%, 21%, 27%), reads tripped 3.2%, 1.7%, 1.7% and 1.3% (old gate 2.4%, 1.3%, 2.7%, 2.3%). The landed build's own numbers are after tuning on all four sets.

21 of the old gate's 25 false positives on the blind sets were proposals ending in "?": from 31 July a verb-first message ending in "?" was an order ("stop the heparin?"). In sets 1 and 2, written before the rule changed, 10 of 10 such messages were proposals asking for judgement ("start metoprolol 25?", "hold metformin?"), each of which got the unsupported-action message instead of an answer, and 0 of 300 orders ended in "?".

## Samuel's rulings (9 October 2026)

1. Build the widening, validated on a third blind corpus before it lands.
2. A bare prescription line with no verb ("bed 13 lasix 40 iv bd", "inj dytor 20 iv bd") is an order.
3. A message ending in "?" is a question unless it is polite ("please", "pls", "can you", "could you", "kindly"). "stop the heparin?" is now a question; "can you stop the heparin?" is still an order.

## What was built

- server/services/order_wording.py (new): the ward's drug names and shorthand, the prescription-line reader, an order after a reading, Hinglish, the leading patient reference, and spelling repairs ("pls" is "please", "inj.lasix40mg" is "inj lasix 40mg", "givelasix" is "give lasix"). Pure and DB-free, like the gate; it imports nothing from sage_service.
- server/services/sage_service.py: patterns 7 to 10 (ward drug names after an order verb; the round's other verbs such as transfuse, nebulise, bolus, slow the drip; an order after a condition, "if grbs > 300 give actrapid 8 units"; a hold said as a negative, "no lasix today", "don't give the metformin tonight"); a colon starts a clause ("Plan: start ..."); ruling 3 in `_is_question_form`; `detect_mutation_intent` runs the old patterns, then the same patterns after a leading patient reference ("bed 13 give lasix 40 iv"), then the line readers.
- The prescription line is a closed vocabulary: a message is a line only when every word in it is prescription vocabulary, so one report word ("on", "given", "running", "day 3", "since", "diya") stops it. 0 of the 2,600 stress rows trip.
- After set 3: a report or question word anywhere ends a prescription line, even where it would have passed as an unknown brand ("neb given at 10 pm, sats now 95"); a word before a drug passes as part of its name only after inj or tab (set 3's "contine NS at 75 ml/hr" had passed as one); an order followed by its reason ("inj morphine 4 mg iv stat, chest pain", "aldactone 25 od, add it"); "before" and "after" may follow a complete order; am and pm, per-minute rates, "keep", "skip", Hinglish read clause by clause and its obligation forms ("sdp abhi chadhana hai"); "repeat" orders a drug only with a dose or route ("repeat potassium at 6 pm" is a blood test).
- Fuzzy spelling was deliberately not built: "gave" is one letter from "give" and "sat" (saturation) one from "stat", so a typo-tolerant gate reads reports as orders. Typos are the largest group of known misses.
- After set 4: a polite order is read without its "can you please" and its "?" (ruling 3 said it is an order, but the line reader took "can" and "could" for a question: 6 of set 4's 36 misses); a reason after an order may hold "is" ("inj lorazepam 2 mg iv now, bed 11 is seizing") but not a word of the drug's own state ("taken", "day 3"); medication-list words ("meds", "regular", "taken") make a line a read; "ICU bed 2" as a patient reference; "put up"; formulation suffixes ("pantop dsr", "ondem md", "magnex forte"); "then" after a complete order; class nouns ("hold all antihypertensives") and a few common brands (Flagyl, Metrogyl, Telma, Avil).
- Cost: 67 µs per ordinary message against 7 µs before. Patterns 7 to 10, the spelling repairs and the line readers run only on messages up to 600 characters, so a pasted 512 KB note costs what it did (84, 198 and 24 ms before; 86, 199 and 24 ms after, on three 512 KB shapes).
- The voice lane uses the same gate (`_mutation_gate`), so a spoken "bed 13 lasix 40 iv stat" now gets the fixed message too.
- Tests: server/tests/test_order_gate_wording.py over server/tests/order_gate_corpus.py, including an end-to-end test where the stub model says "Done. Furosemide 40 mg IV has been given and the order is placed." after "bed 13 give lasix 40 iv" and the clinician must see only the safe message (fails on the old gate, passes now). The known misses on the blind sets are listed in the corpus from a run, so a row the gate used to catch cannot quietly stop being caught. In server/tests/test_sage_guardrails.py "stop the heparin?" moved to the questions (ruling 3), and the formulary test uses two brands the built-in list does not have.

## What it still misses

47 of the 700 blind orders, listed in the corpus: typos ("strat", "metfromin", "transufse", "sat" for stat, "inrease"), number words ("bed seven transfuse two units"), brands it does not know with no dose beside them, and indirect wordings ("the 2 pm piptaz can go now", "pcm only if temp above 100", "fluid challenge 250 ml rl", "nbm so iv pan instead of oral", "replace ns with rl at the same rate"). "we'll add spiro 25 od" and "continue lasix" are deliberately not orders.

7 of the 1,150 blind reads still trip, 5 of which the old gate tripped too: "stop lasix? uo only 200" (the "?" is mid-message, outside ruling 3); "stop ceftriaxone or not" and "give or hold the morning amlodipine, bp 104/70" (a question with no "?" that opens with an order verb); "start the round from bed 1" and "give the family an update about the transfusion" (pattern 4 has read any message opening with "start" or "give" as an order since before this work). The 2 new ones: "OHA: glimepiride 2 mg + metformin 1 g bd", a bare prescription line and so an order under ruling 2; and "plan: switch to oral antibiotics tomorrow", where the blind authors disagree (set 2 labelled "Plan: start inj. Ceftriaxone 2 gram IV once daily ..." an order). Decided on 20 October (below): it is an order.

## Found on the way, not changed

`sage_service._match_out_of_scope` matches an out-of-department bed by `norm_bed in norm_query` with spaces stripped, so a short bed code elsewhere is "named" by any query that contains its characters in a row: in the suite, "bed 713 give lasix 40 iv" got "That patient is outside your department" while "713 give lasix 40 iv" did not (another department's bed "D7" inside "bed713"). Reported to the bed locator session, which confirmed it is in R026's scope: its build matches another department's bed only by the exact label key, and it now carries these two queries as a regression test. Main still has the substring match; test_order_gate_wording.py stubs retrieval in its end-to-end test so it does not depend on it.

## How to re-run

`cd server && env -i PATH=/usr/bin:/bin HOME=$HOME ORB_DEPLOYMENT_PROFILE=dev ORB_JWT_SECRET=suite-only-secret-0123456789abcdef0123456789 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_order_gate_wording.py tests/test_sage_guardrails.py`

## 19 October: an informational give is not an order

Adopted from the 12 October pass at Samuel's request: "give" followed by an overview or a summary ("Give a concise overview of my department's admitted patients", "bed 12: give a brief summary of her meds") no longer counts as an order. Before, pattern 4 read any message opening with "give" as one, which also forced the department question into the 512-token simple scope. An order later in the same message is still read ("give a summary and give heparin 5000 units"), and so is every ward shape after it ("give a summary, then lasix 40 iv stat for bed 12"). The four blind sets are unchanged by it, row for row. The plain English cases are in tests/test_sage_scope_budget.py and the ward-wording ones in test_an_informational_give_beside_ward_wording in tests/test_order_gate_wording.py.

## 20 October: a plan line is read as the order under it

The open "Plan:" question was decided: a "Plan:", "Rx:" or "Orders:" line that names a medicine action is an order, exactly as the same line would be without the header; a plan line with no medicine action ("Plan: review in the evening", "plan: repeat cbc and rft", "rx: as before") and a plan asked as a question ("Plan: start inj ceftriaxone 2 g iv od?", ruling 3) stay reads. Why: on a ward the plan section is the day's order list, and the costs are lopsided. Gated, Sage says truthfully that no chart change was made, or drafts a request that needs approval; ungated, the model can answer that the drug was started, which is the failure this gate exists for. Only a header followed by a colon or a dash counts, so "plan to start insulin if sugars stay high" and "the plan is ..." stay prose.

Before, a plan line with a verb was already gated through the colon clause head ("Plan: start inj ceftriaxone ...") but a verbless one was not ("Plan: inj ceftriaxone 2 g iv od"), against ruling 2. order_wording.PLAN_HEADER now sets the header aside for the line readers, as the patient reference already was. The four blind sets are unchanged row for row and no reviewer or hand-built read trips; the cases are in test_a_plan_line_is_read_like_the_order_under_it in tests/test_order_gate_wording.py.

## 20 October: a "we'll add" line is read like a plan line

Ruled by Samuel: a first-person statement of what the team will do to a medicine ("we'll add spiro 25 od", "we will stop the heparin before surgery", "we're going to switch to oral antibiotics tomorrow", "we shall start inj ceftriaxone 1 g iv od") is an order, for the same reason as a plan line. The singular ("I'll start ceftriaxone 1 g iv od") is included, as it carries the same risk. Advice ("we should stop the heparin soon", still pinned as a read), a hypothetical ("we would add spiro if k is low"), someone else's plan ("they'll start ceftriaxone in the icu"), the past ("we've started ceftriaxone"), a question ("we'll add spiro?") and a line with no medicine action ("we'll review in the evening", "we'll start the round from bed 1") stay reads. order_wording.INTENT_LEAD sets the lead aside; what follows must open with an order verb and name a therapy (pattern 11), or be a line the line readers accept. The bare verbs of patterns 3 and 4 are deliberately not used there.

Measured: set 2's "we'll add spiro 25 od" is now caught. Six reads in sets 3 and 4 now trip, all first-person future plans ("we will wean norad in the morning", "we will give 1 unit prbc after dialysis", "we'll start insulin if sugars stay above 250"); those sets were written to the earlier rule that "we will" is a plan, which this ruling reverses, so they are recorded as the ruled behaviour. No reviewer or hand-built read trips. The cases are in test_a_we_will_line_is_read_like_a_plan_line in tests/test_order_gate_wording.py.

## 20 October: a "continue lasix" line is read like a plan line

Ruled by Samuel: continuing a medicine is an order, for the same reason as a plan line ("continue lasix 40 bd", "continue inj ceftriaxone 1 g iv bd", "cont iv fluids at 100 ml/hr", "metformin: continue", "bed 12 continue lasix", "we'll continue lasix"). The medicines as a group count ("continue home meds", "continue the antibiotics"), because a model can answer that they were continued. The medicine must come straight after the verb, past a few fillers (the, same, all, current, home, iv, inj, tab), so "continue same", "continue monitoring" and "continue to monitor urine output after lasix" stay reads, as do a question ("continue lasix?", ruling 3) and the past ("continued on lasix", "lasix 40 iv bd continued"). This reverses the original proposal's "continue lasix is not an order". In order_wording, "continue" and "cont" moved from the report words to the order verbs ("continued" stays a report word), and new pattern 12 reads the verb-led form.

Measured: set 2's author, who was never given a rule about "continue", labelled "continue home meds except ramipril" an order, and it is now caught. Sixteen reads in sets 3 and 4 now trip, every one a "continue X" line those sets filed as a read under the earlier rule this ruling reverses ("continue inj pan 40 iv od", "cont same antibiotics", "continue lasix 40 bd for now"); they are recorded as the ruled behaviour. No reviewer question trips (each "continue" question there ends in "?" or opens with a question word) and no order is newly missed. The cases are in test_a_continue_line_is_read_like_a_plan_line in tests/test_order_gate_wording.py.

## 20 October: blind set 5 on the landed gate, and two fixes

A fifth corpus, written blind by an independent agent to the rulings above (plan lines, "we will" lines and "continue" lines are orders; a trailing "?" asks unless polite), was scored once on the gate as landed at db6460f, with its hashes checked: 175 of 200 orders caught (88%), 4 of 300 reads tripped (1.3%). The gate of 9 October, on the same set: 47 of 200 (24%), 9 of 300.

The four reads were two bugs, both against the day's rulings, and both are fixed:
- A conditional whose verb follows a hypothetical or someone else's plan was read as an order: "if k drops below 3.5 we would start kcl", "if map drops again we would add vasopressin", "if she spikes again they will add vanco". Pattern 9 stopped only at a subject directly before the verb; it now also stops after would, should, could, might or may, and after another person's future (they will, he'll, you will). "if k drops below 3.5 we will start kcl" is still an order.
- "continue vitals 2 hourly" was read as an order with "vitals" taken for a brand nobody listed, because a frequency passes for a dosing signature. order_wording.MONITORING lists what a ward watches (vitals, obs, grbs, i/o, feeds, dressings and so on), and one of those words now ends a prescription line.

After the fixes: set 5 trips 0 of 300 reads with the same 175 orders; sets 1 to 4, the reviewer sets and the hand-built sets are unchanged row for row. Set 5's 25 misses are listed in the corpus: typos ("insuin", "wil", "contnue"), drug classes the gate does not list ("beta blocker", "statin", "ATT", "antiepileptics", "oral hypoglycemics") and wordings it does not read yet ("plan for tonight:", "no metformin from today", "x3d", Hindi time words such as "subah").

## 20 October: the drug classes and wordings set 5 missed

At Samuel's request, the drug classes and wordings among set 5's 25 misses were added, each as a family rather than as the one sentence, so set 5 is no longer held out.
- Drug classes and short names: beta blockers, ACE inhibitors, calcium channel blockers, ARBs, oral hypoglycaemics, PPIs, H2 blockers, statin, antiepileptics and anticonvulsants, antipsychotics, antidepressants, benzodiazepines, opioids, antiemetics, antifungals, antivirals, bronchodilators, inhalers, vasopressors and inotropes, ATT and AKT (anti-TB), HAART, genta, ampi, amika, metro, polymyxin B, and ivf for the verb-led patterns.
- Words that belong to a name or a dose: "tranexamic acid", "valproate sodium", "25 lakh units", "x 5 doses", "x3d", "alternate days", Hindi times of day ("subah", "raat") and "khane se pehle" (before food).
- Wordings: a reason starting before, after or then may follow any dosed order ("ffp 2 units before the cvc insertion"); a conditional order may open with after or before ("after the culture is sent start meropenem 1 g q8h"); "no metformin from today"; "plan for tonight:"; "will start inj meropenem from now" with the subject dropped; "put a neb"; and a polite request after a vocative ("sister can you put a neb duolin for bed 2?").

Measured: set 5 now catches 195 of 200 orders, and the 5 left are typos ("amlodipin", "sulbactum", "wil", "contnue", "dosses"), unread on purpose. Its reads still trip 0 of 300. Sets 1 to 4 catch the same orders. One read in set 3 now trips, "will add kcl if k drops further", the "we will" ruling with the subject dropped, recorded with that set's other ruled trips. A slip on the way made "pan 40 dose" (a lookup) read as dosing, and the tests caught it: only "doses", as in "x 5 doses", counts as a duration. No reviewer or hand-built read trips. The cases are in test_the_classes_and_wordings_set_5_missed in tests/test_order_gate_wording.py.
