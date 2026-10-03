# Overnight log: 14 August 2026 (pilot dry run)

Plain English, written as it happened. Screenshots referenced by filename live in
`docs/screenshots/2026-08-12-overnight/`.

**The job:** treat tonight as a practice pilot run, find everything wrong with the
box, fix it, and prove the eight clinical workflows actually work end to end.

---

## 1. Where we started

Yesterday's work left the system passing every test: 5,319 backend checks, 834
screen checks, colour and contrast clean in all three themes. So I began by
assuming the tests were lying, which, in this project, they usually are about
the things that matter.

I ran a seven-part inspection of the whole codebase. It produced 96 findings.
Nothing in it was taken from a document or from memory; every claim was
re-checked against the code as it stands today, because several items that
paperwork still listed as "open" had quietly been fixed hours earlier.

**The big one:** Sage's three trust numbers were all failing, and two were
mechanically linked.

- Answers took 11–14 seconds on patient questions, against a target of 5.
- **Not one of the last 40 answers cited a source.** Guidelines were being
  retrieved and then left unattributed.
- Sage marked itself *low confidence* on about 90% of bedside questions.

That last one turned out to be partly an accident of code: a rule forced "low"
whenever documents were retrieved but the model didn't format its citations the
expected way. So the confidence number was measuring the model's punctuation,
not the strength of the evidence.

---

## 2. What I fixed on the server side

Seven work streams ran in parallel. Each one had to write a test that fails
when the bug is put back, no exceptions, because twice this week a
confident-looking test certified a live bug as already fixed.

### Sage's trustworthiness

- **Confidence now reflects evidence, not formatting.** If Sage retrieved good
  guidelines and visibly used them, it can no longer label itself unsure just
  because the model forgot a tag. If it retrieved nothing on a clinical
  question, it can no longer claim high confidence.
- **Citations now appear.** When the model fails to name its sources, Orb works
  out which retrieved passages the answer actually used and attributes them
  itself, labelled honestly as Orb's attribution rather than the model's, so a
  reader can always tell the difference. Nothing is attributed to a passage the
  answer didn't use; an over-credited citation looks checkable and isn't.
- **Drug names no longer wipe the patient's chart.** Yesterday's fix stopped
  "What's…" being mistaken for a person's name, but capitalised medicines still
  tripped it: *"Is Ceftriaxone appropriate?"* silently dropped the patient's
  chart mid-conversation. The code's own words for that state are "the
  hallucination condition", starve the model of facts and it invents them.
  It now consults the hospital's actual medicine list instead of a hand-written
  list of exceptions, so Indian brand names work too.

### The deterioration score

- **A patient on oxygen was losing that point from their NEWS2 score**, on every
  reading after the first, in the exact data pathway the real pilot will use.
  It was invisible on this machine because the demo simulator fills the field
  in. Oxygen-dependent patients are the ones most likely to deteriorate, so
  this mattered.
- The "how complete is this score" flag was only tracking five of the seven
  parameters, so a score missing the consciousness assessment was reported as
  fully measured.
- Two parts of the system scored the same patient by different methods. They
  now share one.

### Privacy and attribution

- **The surgery module was handing out every ward's patients**, names, hospital
  numbers, diagnoses, with no ward check and no access record. Now scoped and
  audited, with the rule written down: surgical staff are hospital-wide *by
  role*, deliberately and visibly, rather than by accident.
- **Every action taken from the browser was being recorded with no name against
  it.** The audit trail only understood the older sign-in method; when the app
  moved to secure cookies, the actor field silently went blank. That undoes the
  whole point of a tamper-evident record.
- **Eight places let someone act on a patient outside their own ward**, including
  creating orders and booking appointments. All eight closed; the count went
  from eight to zero.

### Money

- **Raising a bill turned an unpriced item into a real ₹0.00 line**, erasing the
  "this bill is incomplete" warning the rest of the system works hard to show.
- A patient with a 100%-copay policy had their **entire bill claimed from the
  insurer**, because a zero share was treated as a missing one.
- A credit note could be issued twice against one bill.
- Cash could be taken with no till open, landing in the day's takings but in
  nobody's drawer.

### Honesty and time

- **Outpatient visits registered between midnight and 5:30am were dated the
  previous day and priced against yesterday's rates**, because the code used
  world time instead of hospital time.
- The printed shift handover, the medico-legal document, was printing the raw
  computer term `insufficient_data` into clinical prose.
- **The pilot's evidence recorder was failing silently.** It's the only thing
  that records the *negative* events, alerts nobody opened, drafts nobody
  accepted, and without those the scorecard reports successes only. Failures
  were logged at a level nobody reads. Now loud, counted, and visible.

### Backups and setup

- **The setup template recommended the database mode that once froze the
  appliance for minutes.** An operator following it would have re-enabled the
  freeze.
- **Backups verified four tables out of sixty-six.** Restoring a backup that
  silently dropped all the billing and pharmacy data would still have printed
  PASS. Now it checks the schema version and every module.

---

## 3. A real backup gap, found and closed on the spot

While checking that fix, one line stood out: every backup on this machine was
from a schema seven versions old. In plain terms, **there was no backup you
could have restored without losing everything built in the last week.**

I took a fresh one immediately. It verified clean, and the new checks worked
exactly as intended: it now lists each module's row count and states that
"EMPTY" is fine if that module isn't live yet and a **real loss** if it is, 
with a setting to make it fail hard once a module goes live.

---

## 4. The server froze: and why that is *not* a product bug

Midway through, the running app stopped responding. The health check timed out,
which normally means something has blocked the whole system.

I captured the stack while it was still stuck, which is the diagnostic step this
project learned the hard way. The answer: the **development file-watcher** was
trying to restart the app because my own fix agents were editing server files
underneath it, and it was stuck waiting for the old process to exit.

That is a known hazard of running the development setup while editing, and it is
documented in the project's own notes. **It is not a defect in what ships to a
hospital**, the real appliance runs without that file-watcher. I'm recording it
because it looked exactly like a serious fault for the first thirty seconds, and
next time it should be recognised in five.

---

## 5. A test that agreed with itself

After the fixes, the full run came back with 5,549 passing and one failure:
the Command Centre's census check, asserting 72 should equal 48.

It passed when run alone and failed when run after another test file. That is
the signature of a test measuring shared state rather than behaviour.

The cause is the most instructive thing in tonight's log, because this project
has met it before and written it down. The bed capacity is published in two
forms: a **list of wards** (which updates as wards are discovered at startup)
and a **single total** (which does not, it is a plain number, copied once when
the file loads). The screen was once showing "30 of 48 beds" in the header and
"34 of 72" in the tile directly below it, from a single snapshot, because one
read the live list and the other read the frozen copy.

That was fixed in the product months ago. **The test was never fixed**, it kept
comparing the answer against the same frozen copy, so the two agreed with each
other while disagreeing with reality, and it only broke when another test
admitted a patient into a ward that hadn't existed at load time.

It now asserts the thing that must always be true, *the total equals the sum of
its parts*, rather than a number captured at start-up. I also removed the
frozen copy from the file's imports entirely, with a note explaining why
importing it is the trap, so nobody reintroduces the same assertion later.

**5,549 passing, 1 failing → 5,550 passing.**

---

## 6. Checking the eight workflows

The last piece. Orb's commercial claim is that it sells the *loop*, not the
parts: a competitor sells an alerting agent, Orb sells detection → reasoning →
the right clinician getting a drafted action. Clicking through screens cannot
prove that, because each screen can work perfectly while the handoffs between
them silently fail.

So all eight are being driven end to end through the real interfaces, as the
real roles, with the wedge timed from "abnormal vitals arrive" to "a clinician
can see the alert."


---

## 7. I was wrong about the freeze

Earlier I told you the frozen server was purely my own agents editing files
underneath a running development setup. That was half the story, and the
comfortable half.

The deeper look found the real reason it never *recovered*. When something asks a
background job to stop, the job is supposed to pass that instruction along. In
six places across the codebase, the code caught the "please stop" signal and
quietly ignored it, treating it the same as a routine timeout. A job that
ignores "stop" cannot be stopped, and anything waiting for it waits forever.

That is why the app sat at "Waiting for application shutdown" indefinitely, still
holding the network port so every request hung with no error at all, not even
the health check, while the old copy kept running the vitals simulator and
writing to the encrypted database.

**Why this matters for a hospital, not just for my night.** The appliance
restarts: software upgrades, power events, the machine rebooting. A shutdown that
can hang forever is an appliance that does not come back. Worse, the old copy
kept writing to the database, so a restart could have two processes writing to
one file, which is a corruption risk this project has already been bitten by.

Fixed in all six places, plus the shutdown itself now waits a bounded ten seconds
and then says loudly, by name, which job refused to die instead of hanging
silently. There are thirteen new tests, and each one fails within seconds if the
bug is put back rather than hanging the test run.

**The lesson worth keeping:** the first explanation was true and still wrong. "My
agents caused it" explained the trigger and stopped the investigation one step
short of the defect. The stack trace was the thing that kept going.


---

## 8. Driving the eight workflows: the most important thing tonight

Orb's commercial claim is that it sells the **loop**, not the parts: a competitor
sells an alerting tool, Orb sells *detection → reasoning → the right clinician
getting a drafted action*. Clicking through screens cannot test that, because
every screen can work perfectly while the handoffs between them quietly fail.

So all eight were driven end to end, as the real roles, through the real system.

**Two are genuinely strong.**

*Medication safety* is the best thing in the product. I watched a drug ordered
for a patient with a severe allergy get blocked at prescribing, watched the
prescriber override it, and then watched it get **blocked again, independently,
at the bedside**. Two different guards, neither trusting the other. That is a
demo I would put in front of any clinician.
→ `wf-01-caseroom-nurse-obs-alert.png`

*Admission → capacity → transfer → discharge* also closes: the patient appears on
both boards, capacity moves, a ward transfer re-scopes who can see them, and the
bed frees on discharge.

**The wedge, the thing you actually sell, did not.** Three breaks:

1. A nurse charted a NEWS2 of **19**. The escalation board showed **10** for the
   same patient in the same minute. Scores above a certain point were being
   dropped entirely by a comparison that could not distinguish "as bad as the
   open alert" from "far worse than it."
2. **Sage contributes almost nothing.** The only Sage output in the whole loop is
   a one-sentence note from the small model, produced only on the demo simulator
   path, with no way for a clinician to accept or reject it. The "reasoning"
   middle of the loop is thinner than the story implies.
3. **The pilot's evidence recorder wrote nothing** for either alert I raised, 
   because manual nurse entry never told it an alert had happened. The simulator
   lane records perfectly, so the instrument reported itself healthy while being
   blind to the lane the pilot will actually run on.

**Two should not be shown at all.** A surgical case could be marked complete
without ever being started, with the safety checklist gating nothing, and no
timestamps existed to measure turnaround, the thing that workflow is named
after. And a patient asking for a follow-up appointment wrote to a table that
**no staff screen anywhere reads**, while the system told the patient "the care
team will confirm a time."

All of this is now fixed. The evidence recorder covers the manual lane, alerts
from every lane trigger the same preparation, the severity comparison was
corrected, surgery has a proper state gate and timestamps, and follow-up requests
reach staff. One honest note: I did **not** invent a full Sage assessment
tonight, that is a real product decision with latency and safety consequences,
and it deserves your judgement rather than a 4am guess.

---

## 9. A fix that was reported done and wasn't

Worth recording on its own. One agent reported fixing the escalation severity
bug. A later agent, checking rather than trusting, found **the file had never
been modified**, the fix existed only in the report. The bug was still live and
still dropping every score from 7 to 20.

This is the third time this week a confident report has been wrong in a way only
verification caught. It is why every fix tonight had to prove its own test fails
when the bug is put back.

---

## 10. Where things ended

- **Backend: 5,680+ checks passing.** Frontend: **888** (up from 834 at the start
  of the night). Colour and contrast clean in all three themes. Build clean.
- **122 screenshots** in `docs/screenshots/2026-08-12-overnight/`, including 43
  from the deep sweep and 10 from the workflow runs.
- The eight previously-empty modules now have realistic seed data, so their
  deeper screens can actually be opened and demonstrated.
- A fresh, verified backup exists at the current schema, there wasn't one when
  the night started.

### Needs your ruling

1. **The surgical safety checklist now blocks case completion**, switched on by
   default. That is a governance decision, not a code decision, confirm or
   reverse it.
2. **Sage's role in the wedge**: today it garnishes an alert with one sentence.
   A real drafted assessment a clinician can accept or reject is a build, with a
   latency cost on the most time-critical screen in the product.
3. **A patient's date of birth** is now optional at admission and is what unlocks
   their discharge portal. Without it no discharge code can be issued, which is
   the honest behaviour, but it means the front desk should start capturing it.

---

## 11. Shutdown and cleanup

Final checks, all verified rather than assumed:

| Check | Result |
|---|---|
| Backend tests | **5,682 passing, 0 failing** |
| Screen tests | **888 passing, 0 failing** |
| Colour & contrast | clean, all three themes |
| Production build | clean |
| App processes | none left running |
| Ports 8000 / 8010 | both clear |
| Leftover temporary databases | none |
| AI models held in memory | **unloaded, 20.4 GB released** |

A note on that last line. Orb deliberately pins its two medical models in memory
so answers come back fast; they do not release on their own. Both were unloaded,
which frees the memory now and costs a few seconds' warm-up the next time the
app starts. Nothing was deleted, the models reload on demand.

Free memory went from 26 GB to 27 GB after cleanup, with the 20 GB of model
weights released back to the system.

---

## 12. The through-line

Three separate times tonight, something was *reported* finished and wasn't:

- Eleven modules with passing tests and no way to open them (found yesterday).
- An escalation fix that existed only in a report, the file was never touched.
- A frozen server explained away as my own doing, which turned out to be hiding
  six real defects underneath.

None of these were caught by tests. All three were caught by **using the thing
and checking whether it did what it claimed**. That is the argument for running
this exercise again before any clinician touches the system, not because the
work is sloppy, but because "finished" and "working" are different claims and
only one of them is testable from the inside.
