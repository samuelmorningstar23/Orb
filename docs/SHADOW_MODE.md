# Shadow Mode: Operating Manual

*Version 2 · first written 1 August 2026, re-checked and updated 3 September 2026
· For the pilot hospital's physician champion, CIO and ward leads, and for me,
since I operate the appliance.*

This manual describes shadow mode, which is how I introduce Orb to a live ward.
It explains what shadow mode is, what changes for the people working on the ward
(almost nothing, on purpose), what Orb records while it runs, how the scorecard
that comes out of it is calculated, what that scorecard can and cannot prove,
and what has to be true before it starts.

Read sections 1 to 3 if you work on the ward. Read all of it if you are the
champion, the CIO, or the operator.

---

## 1. What shadow mode is

Shadow mode is Orb running on real patient data, in real time, without any
clinician acting on what it produces. Orb watches the same ward the staff are
already working on, forms its own view of who is deteriorating, drafts what it
would have said, writes all of it to a log, and then does nothing. Care proceeds
exactly as it does today, through the existing systems and the existing people.

The point is to answer one question honestly before anyone's judgement is
influenced by the answer:

> When Orb said a patient was deteriorating, was it right, was it early, and
> would acting on it have changed anything?

A demonstration cannot answer that question, and it cannot be answered after
clinicians start using the system, because from the moment a clinician sees an
Orb alert, Orb has changed the very outcome it is being measured against. There
is exactly one window in which the question is answerable, and shadow mode is
that window.

### 1.1 Why I do not simply switch it on

Three reasons, in order of how much they matter.

**Safety.** A monitoring system that has never been measured against real
patients on this ward, with this case mix, these monitors and these staffing
patterns, has an unknown false-alert rate. Alert fatigue is a documented patient
safety hazard in its own right: a system that cries wolf trains staff to ignore
it, including on the occasion it is right. Shadow mode measures the false-alert
rate at zero clinical cost, because nobody is being interrupted.

**Evidence.** You are being asked to trust a system built by one person. "It
works, come and see" is not evidence. A dated scorecard covering several weeks
of your own patients, produced before anyone had an incentive for it to look
good, is evidence. It is also what your CIO's review, the champion's sign-off
and any outside scrutiny will rest on.

**Reversibility.** Shadow mode can be switched off at any moment with no clinical
consequence, because no clinical decision depends on it. That property
disappears the instant clinicians begin relying on the system, and it is worth
keeping for as long as possible.

### 1.2 What shadow mode is not

- It is not a trial run of the user interface. Nobody is asked to use Orb during
  the silent phase. Training happens separately, on the appliance, with
  fictional patients.
- It is not a parallel documentation burden. No one enters anything twice. Orb
  reads; it does not ask.
- It is not a decision-support pilot. Nothing Orb produces during the silent
  phase is shown to a clinician, so nothing it produces can influence care.
- It is not permission to act on Orb. If a member of staff somehow sees an Orb
  output during the silent phase, it carries no clinical authority and must not
  change what they do. Section 9 covers what to do instead.

---

## 2. What changes on the ward

During the silent phase: nothing. Patients are admitted, assessed, treated and
discharged through the hospital's existing systems exactly as before. No member
of ward staff has a new task, a new screen or a new alert.

The only visible changes are physical and administrative:

| Change | Who notices | What it means |
|---|---|---|
| A small computer appears in a locked location on the ward | Ward manager, IT | The Orb appliance. It is not a clinical device and nothing depends on it. |
| Some monitors gain a network or serial connection | Biomedical engineering | Orb reads vital signs from selected high-acuity beds. Read-only: Orb cannot send anything to a monitor or change a setting on one. The adapter has been bench-tested against a simulator, not yet against a real device, so this is done with your biomedical team present. |
| A roster of currently admitted patients is imported | Medical records, IT | So Orb knows who is on the ward. Refreshed as admissions and discharges happen. |
| Nurses may be asked to enter vitals into Orb on selected beds | Ward nurses | Only where monitor integration is not available, and only if agreed in advance. This is the one place shadow mode can create work, and it is negotiated, not assumed. |

Nothing Orb does during shadow mode is visible to a patient, and no patient
receives different care because Orb is running.

### 2.1 What I ask the ward for

One thing, and it is the thing the entire pilot depends on: a record of what
actually happened to the patient.

Orb can see that it raised a concern about bed 12 at 03:14. It cannot see, on
its own, that bed 12 was reviewed by the registrar at 05:40, escalated to
critical care at 06:20, and transferred at 07:05. Without that, "Orb flagged
this three hours early" is an unverifiable claim, and the single most important
number in the pilot does not exist. Section 5.1 explains this in full; section 6
lists it as a hard prerequisite. The charge nurse's end-of-shift screen for
recording it exists (built 10 August 2026). What counts as an escalation event is
the champion's decision, and it must be decided before the silent phase starts,
because it cannot be reconstructed afterwards.

---

## 3. The three phases

Shadow mode is a sequence with a gate between each step, not a single state.
Access widens only when the preceding phase has produced evidence that it
should.

### Phase A: Silent (the shadow proper)

Orb ingests, monitors, drafts and logs. No clinician has access to clinical
output. The operator and the champion can see the scorecard; nobody sees a live
alert.

- Duration: at minimum one full clean week, and in practice as long as it takes
  to satisfy the exit bars below.
- Exit bars, all of which must hold:
  - zero data loss: every expected vitals reading, roster change and event is
    accounted for;
  - backups verified on schedule, with at least one restore actually performed,
    not merely documented;
  - no severity-1 incident in the window;
  - the scorecard is populating: the metrics in section 5 have real
    denominators, not zeros;
  - the false-alert rate is one the champion is willing to expose staff to. That
    is a clinical judgement and it belongs to the champion, not to me.

### Phase B: Champion-only

The physician champion, and nobody else, gets live access to Orb's clinical
output. They are explicitly not asked to act on it; they are asked to judge it,
case by case, while it is fresh.

This phase exists because the scorecard measures agreement in aggregate, and
aggregate agreement hides the failure modes that matter. A single clinician
reading real alerts on real patients they know will find, in a week, things no
metric surfaces: the alert that was technically correct and clinically useless,
the phrasing that would be misread at 3 a.m., the patient cohort where the model
is confidently wrong.

- Exit bars: the champion's explicit sign-off, plus every Phase A bar still
  holding.

### Phase C: Phased clinician access

Access widens by role and by ward, on a schedule the champion sets. Vigil
alerting typically comes before Sage drafting, and both come before anything
that proposes a change to a chart.

Throughout Phase C the shadow log keeps recording and the scorecard keeps
accumulating, but from this point it measures something different, and I want
to be plain about that: once clinicians can see Orb, the scorecard stops being a
measurement of Orb and becomes a measurement of Orb plus staff. Detection lead
time in particular loses its meaning, because the event Orb was predicting may
not happen precisely because Orb predicted it. The Phase A numbers are the
clean ones. I preserve them, dated, before Phase B opens.

### Reverting

Any phase can be reverted to the one before it, immediately, at the champion's
or my discretion, without a meeting. Reverting is a normal operational action
and is not treated as a failure. I record the reason in the operating log.

---

## 4. What Orb records

Shadow mode's value is entirely a function of what gets written down, so this
section is precise about it, including where the record was thin and what I
built to fix that.

### 4.1 What Orb records well

These exist, carry an actor and a timestamp, and are trustworthy:

| Record | What it captures |
|---|---|
| Vitals and observations | Every reading ingested from a monitor or entered by a nurse, with its source |
| Vigil risk scoring | NEWS2, recomputed as data arrives, with the per-parameter drivers |
| Sage conversations | Every question asked and answer produced, with the guideline sources retrieved and the sources the model actually cited |
| eMAR administrations | Which drug, to which patient, by whom, at what time, and any override with the reason and the role that gave it |
| Orders | Placement and every state transition |
| Note signing | Who signed which note, and when |
| Sage action proposals | Proposed chart changes and their outcome, including the ones a clinician declined, which is the most valuable class of row in the entire dataset |
| Audit trail | A tamper-evident, hash-chained log of changes; 91,743 rows recomputed intact on 3 September 2026 on the development appliance |

### 4.2 What was missing on 31 July, and what I built since

An audit of the codebase on 31 July 2026 found the following gaps. I list them
with their status on 3 September, because a manual that overstates its
instrument is worse than no manual.

| Gap found on 31 July | Status on 3 September 2026 |
|---|---|
| There was no alerts table; a Vigil alert was a message row containing prose, with no acknowledgement or response record | Built: a single append-only shadow event log, one identifier per episode, appended never edited (10 August) |
| No separate record of when an alert fired versus when it was written | Built: fire time and first-view capture are recorded; the scorecard shows its own instrument lag (p50 2 ms, p95 1.1 s on the development appliance) |
| No record that a human saw an alert | Built, as above; a read receipt is now a named clinician on a named alert |
| The audit trail was actor-blind for browser sessions | Fixed 14 August: browser-originated actions carry the actor |
| Correlation identifiers did not correlate | Fixed: one episode identifier threads the whole chain |
| No ground-truth clinical outcome record of any kind | Half built: the charge nurse's end-of-shift outcome screen exists (10 August). The clinical definition of an escalation event is still the champion's decision, and without it the headline metric cannot be computed |
| Alerts raised from manually entered vitals were invisible to the recorder | Found in the 14 August dry run, fixed the same day |

Two things remain open and both are decisions, not code: the champion's
definition of an escalation event (section 5.1) and the adjudication process
for false alerts (section 5.2).

### 4.3 The episode

The unit of analysis is the episode, not the alert. One episode is the whole
chain for one patient concern:

```
vitals arrive → risk crosses a threshold → alert is created → delivered to
someone → seen (or not seen) → Sage drafts (or does not) → a clinician acts
(or declines, or does nothing) → a clinical outcome occurs (or does not)
```

Every step is a row, appended, never edited. A correction is a new row, not a
rewrite, so the log can be audited and cannot be quietly tidied up.

The negatives are recorded as deliberately as the positives. An alert nobody
opened, a draft a clinician declined, a proposed action that was rejected: these
are the rows that make the scorecard honest, and they are the easiest ones to
lose by accident. A system that only logs its successes will report a
spectacular success rate.

Rows written by the built-in simulator are labelled synthetic on the scorecard,
are the only rows the database allows to be deleted, and are never counted as
clinical evidence.

---

## 5. The scorecard

Four metrics. Each is defined here operationally, together with what it cannot
show, because every one of them can be read to flatter Orb if the caveat is
dropped.

### 5.1 Detection lead time

**The number.** The interval between Orb first flagging a patient and the
clinical event occurring: escalation of care, a rapid-response call, a
critical-care transfer, or death.

**Why it is the one that matters.** It is the only metric that speaks to Orb's
actual claim. Everything else measures whether Orb is tolerable; this measures
whether it is useful.

**What it requires.** Both ends of the interval. Orb's flag time is recorded
now (section 4.2). The clinical event needs a definition and a person who
records it. Deciding how the clinical event gets captured, who records it, from
what source, and what counts as "escalation" is the physician champion's
decision, and it is a prerequisite, not a refinement. If the pilot starts
without it, the pilot cannot produce its own headline number.

**What it cannot show.** That acting on the flag would have helped. A flag three
hours before a transfer is evidence Orb saw something; it is not evidence that
intervening at that point would have changed the outcome. I will not let the
number be reported as though it were.

### 5.2 False-alert rate

**The number.** The proportion of alerts that did not correspond to a real
deterioration.

**The denominator is the whole argument.** Per alert, per patient-day and per
occupied bed-day are three different numbers with three different meanings, and
the one that flatters a monitoring system is per alert. The scorecard states the
denominator every time the number appears and reports per occupied bed-day as
the primary figure, because that is what determines how often a nurse is
interrupted.

**Somebody has to adjudicate.** "Real deterioration" is a clinical judgement. The
champion, or a delegate they name, reviews a defined sample, not a convenience
sample, and their adjudication is recorded with their identity and the date.
Unadjudicated alerts are reported as unadjudicated, never as correct.

**What it cannot show.** Alert fatigue. A tolerable rate on paper can still be
intolerable at 3 a.m. on a short-staffed night. That is what Phase B is for.

### 5.3 Agreement

**The number.** How often Sage's draft matched what the clinician actually did.

**What it cannot show, and this is the big one.** Agreement is not correctness.
If Sage agrees with the clinician 90% of the time, that is equally consistent
with "Sage is good" and with "Sage is a fluent restatement of the obvious."
Disagreements are the informative rows, and they need reading individually
rather than summarising. A rising agreement percentage during Phase C may simply
mean clinicians have started deferring to the system, which is a risk, not a
result.

**When a medicine counts as the clinician's action.** The clinician's side of
an agreement is the clinical.action row an order writes. With the
medication-order lifecycle switched off (the default), a medicine ordered under
Orders or by an order set writes that row when the order is created. With the
lifecycle on (ORB_MED_ORDER_LIFECYCLE), the order is a draft until a prescriber
signs it, and the row is written at the signature instead, once, with the
signer as the actor. A draft signed hours later is therefore matched against
the alert hours later, or falls outside the correlation window and is not
counted at all. Tests, nursing orders and the rest are unchanged. Any pilot
that runs the lifecycle must say so beside its agreement figure and must not
compare those figures with a period that ran with it off.

### 5.4 Time saved

**The number.** A proxy, and it is labelled as one everywhere it appears.

Orb can measure intervals it controls end to end: how long a Sage answer took
against how long the same lookup takes manually, measured once under observation
rather than assumed. It cannot measure clinical time saved, because that would
require a controlled comparison the pilot is not running.

Only clinician-requested Sage answers enter this lookup proxy. Machine-initiated
Vigil alert drafts are excluded from its answer count and latency distribution;
their measured timings remain in the shadow log.

I report it as a bounded, sourced estimate with its assumptions visible, or I do
not report it. It is the metric most likely to be quoted back in a fundraising
context and the one with the weakest foundation, and those two facts together
are exactly why it needs the tightest discipline.

### 5.5 Reading the scorecard honestly

Three rules, enforced in the interface itself:

1. Every number carries its denominator and its date range. A percentage
   without a denominator is not a finding.
2. Insufficient data is displayed as insufficient data, never as zero. A
   scorecard that reports "0% false alerts" on its first morning, when it has
   seen four alerts, is actively misleading.
3. The export states what has been removed. The exportable scorecard is
   de-identified and is the artefact that may be shared with the CIO, counsel or
   an investor. The identified data never leaves the appliance.

An export is a snapshot of a moving table. Orb closes out stale episodes on a
background sweep every few minutes, so two exports taken seconds apart can
legitimately differ, and so can their integrity digests. Nothing is torn or
inconsistent; each export is internally coherent. If anyone ever reconciles two
exports against each other and finds they disagree, that is why. Quote one dated
export, not "the numbers". For the same reason, an event stamped in the same
millisecond an export is taken falls into the next export, not that one.

---

## 6. Before shadow mode can start

A checklist. Every line is a hard prerequisite; none is a nice-to-have. Items
marked "done" were completed on the development appliance and have to be
repeated on yours.

**Clinical and governance**

- [ ] Physician champion named, in writing.
- [ ] Clinical outcome capture agreed: what counts as an escalation event, who
      records it, from which source. Without this, section 5.1 does not exist.
- [ ] False-alert adjudication process agreed: who, what sample, how often.
- [ ] Shadow-first phasing recorded in the MOU, including that Orb output has no
      clinical authority during Phase A.
- [ ] Counsel engaged; data-protection posture and decision-support
      classification settled.
- [ ] Ward staff briefed: what the appliance is, what it is not, and what to do
      if they see an Orb output (section 9).

**Technical**

- [x] Shadow event log built, verified, and shown to capture the negatives
      (done, 10 August 2026).
- [x] Alert fire-time and first-view capture landed (done).
- [x] Episode correlation working end to end on real ingestion (done; the
      manual-entry lane was fixed on 14 August).
- [x] Browser-originated actions carry an actor in the audit trail (done, 14
      August).
- [ ] Roster import running against the hospital's real export.
- [ ] Vitals flowing from every bed in scope, monitor or manual; the monitor
      adapter tested against the ward's real devices.
- [ ] Backups scheduled and running on the appliance, and one restore actually
      performed with hospital IT.
- [ ] Key escrow ceremony complete: envelopes sealed, safe and off-site.
- [x] Soak test passed at the pilot's bed count (done on the development
      machine: 11 August, 60 beds, 15 minutes, zero errors; to be repeated on
      the appliance).
- [x] Health probe under alert-storm conditions passed (a standing regression
      test; re-run at every phase gate).

**Operational**

- [ ] Operator contactable, with an agreed response expectation.
- [ ] Daily on-site operator check agreed, with a named person. The appliance
      has no internet connection, so nothing pages anyone off site (the
      outbound heartbeat was removed on 4 October 2026); the check reads
      `./scripts/orb.sh status`, the Trust Center and the backup log.
- [ ] Downtime print packs generated and the drill rehearsed.
- [ ] Runbook on the ward, covering every failure mode with a page.

---

## 7. Operating rhythm

**Daily (me, on site, about 10 minutes).** Status page green
(`./scripts/orb.sh status`)? Ingestion gaps overnight? Backup ran and
verified? Any alert storm or error spike? Scorecard still populating?

**Weekly (me and the champion, about 30 minutes).** Walk the week's alerts.
Adjudicate the sample. Read the disagreements individually. Note anything that
would have been misread on a night shift. Snapshot the scorecard, dated, into
the pilot record; snapshot it even in a bad week, especially in a bad week.

**At every phase gate.** Re-run the readiness checklist for the phase being
entered, re-run the soak and the storm probe, and record the champion's decision
with its date and reasoning.

---

## 8. Privacy and governance

Shadow mode does not change Orb's privacy position, and it is worth restating
because shadow mode is when the largest volume of real patient data first
arrives:

- Everything runs on the appliance. No patient data is transmitted to any
  outside service, including for AI inference. The models run locally.
- The database is encrypted at rest. Backups are encrypted, and the encryption
  keys are escrowed physically, not held by me.
- The audit trail is tamper-evident. Entries are hash-chained; altering history
  breaks the chain visibly.
- There is no outbound health signal. The heartbeat was removed on 4 October
  2026, when Orb was ruled strictly offline (no internet connection of any
  kind); the appliance is monitored on site.
- Access is on-site only. There is no inbound remote access to the appliance.
- The scorecard export is de-identified, and states what was removed. The
  identified dataset stays in the building.

The shadow log contains PHI. It is subject to the same retention, access control
and erasure obligations as the rest of the clinical record.

Erasure does not currently reach it, and the reason is this table's own
integrity guarantee. Shadow rows are append-only at the database layer for
everything except the simulator lane; that immutability is what makes the
scorecard export evidentiary rather than a report. The admin erasure routine
derives its delete plan from the schema, so it tries to delete from this table
and is refused by that guard; the whole erasure transaction then rolls back and
no patient data is erased anywhere. Until I reconcile the two, treat a
data-subject erasure request as something Orb cannot yet honour, and say so.

---

## 9. If something goes wrong

**A staff member sees an Orb clinical output during Phase A.** Treat it as
having no clinical authority; it has not been validated on this ward, which is
the entire reason shadow mode exists. Do not let it change management. Tell me,
so the leak can be closed; the leak is a defect and I treat it as one.

**Orb disagrees with the clinical team about a patient, and Orb looks right.**
During Phase A this is a scorecard row, not an alert. Nobody is asked to act on
it. This will feel wrong at least once, and I would rather say so now: the
discipline is what makes the eventual number mean anything, and a single
influenced case cannot be un-influenced.

**The appliance fails.** Nothing clinical depends on it during Phase A, so there
is no clinical urgency. Restore per the runbook. Record the gap; an ingestion
gap must be visible in the scorecard's denominators, not silently absorbed.

**The scorecard looks bad.** Report it. A shadow pilot that measures an
unfavourable result and says so is doing precisely what it was built to do; one
that only ever produces good numbers was not an instrument. An honest bad
scorecard is more useful to the hospital, and more credible to an investor, than
a flattering one that cannot be defended under scrutiny.

---

## 10. What shadow mode cannot prove

Stated plainly, so nobody has to discover it in a due-diligence conversation.

- It is one site, two wards. It establishes that Orb works here. It does not
  establish generalisability.
- It has no control group. Every claim is a before/after or an agreement
  measure, not a controlled comparison. Nothing here will support a causal claim
  about patient outcomes.
- It cannot show that acting on Orb would have helped. Only a phase where
  clinicians act can show that, and by then the clean measurement is gone.
- Statistical power is limited. Serious deterioration events on two wards over
  a few weeks are a small number. Expect wide confidence intervals and resist
  reporting a point estimate as though it were precise.
- The absence of harm is not proof of safety. No adverse event in a few weeks of
  silent operation is reassuring; it is not a safety case.

What shadow mode does produce is narrow and real: evidence, from this hospital's
own patients, that Orb sees what it claims to see, at an interruption rate the
ward can live with, without losing data or falling over. That is a sound basis
for letting clinicians look at it. It is not a claim that Orb improves outcomes,
and I will not present it as one.

---

## Related documents

| Document | What it covers |
|---|---|
| [PILOT_ROADMAP.md](PILOT_ROADMAP.md) | The 13-week sequence this fits into, and current status |
| [OUTCOME_CAPTURE.md](OUTCOME_CAPTURE.md) | The champion's decision on what counts as an escalation event |
| [USER_MANUAL.md](USER_MANUAL.md) | How every module works for the people using it |
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Backup, verification and restore procedure |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Installing and configuring the appliance |
| `perf/` | Latency and soak measurements referenced by the readiness gates |
