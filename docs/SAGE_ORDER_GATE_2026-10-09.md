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

7 of the 1,150 blind reads still trip, 5 of which the old gate tripped too: "stop lasix? uo only 200" (the "?" is mid-message, outside ruling 3); "stop ceftriaxone or not" and "give or hold the morning amlodipine, bp 104/70" (a question with no "?" that opens with an order verb); "start the round from bed 1" and "give the family an update about the transfusion" (pattern 4 has read any message opening with "start" or "give" as an order since before this work). The 2 new ones: "OHA: glimepiride 2 mg + metformin 1 g bd", a bare prescription line and so an order under ruling 2; and "plan: switch to oral antibiotics tomorrow", where the blind authors disagree (set 2 labelled "Plan: start inj. Ceftriaxone 2 gram IV once daily ..." an order).

## Found on the way, not changed

`sage_service._match_out_of_scope` matches an out-of-department bed by `norm_bed in norm_query` with spaces stripped, so a short bed code elsewhere is "named" by any query that contains its characters in a row: in the suite, "bed 713 give lasix 40 iv" got "That patient is outside your department" while "713 give lasix 40 iv" did not (another department's bed "D7" inside "bed713"). Reported to the bed locator session, which confirmed it is in R026's scope: its build matches another department's bed only by the exact label key, and it now carries these two queries as a regression test. Main still has the substring match; test_order_gate_wording.py stubs retrieval in its end-to-end test so it does not depend on it.

## How to re-run

`cd server && env -i PATH=/usr/bin:/bin HOME=$HOME ORB_DEPLOYMENT_PROFILE=dev ORB_JWT_SECRET=suite-only-secret-0123456789abcdef0123456789 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_order_gate_wording.py tests/test_sage_guardrails.py`

## 19 October: an informational give is not an order

Adopted from the 12 October pass at Samuel's request: "give" followed by an overview or a summary ("Give a concise overview of my department's admitted patients", "bed 12: give a brief summary of her meds") no longer counts as an order. Before, pattern 4 read any message opening with "give" as one, which also forced the department question into the 512-token simple scope. An order later in the same message is still read ("give a summary and give heparin 5000 units"), and so is every ward shape after it ("give a summary, then lasix 40 iv stat for bed 12"). The four blind sets are unchanged by it, row for row. The plain English cases are in tests/test_sage_scope_budget.py and the ward-wording ones in test_an_informational_give_beside_ward_wording in tests/test_order_gate_wording.py.
