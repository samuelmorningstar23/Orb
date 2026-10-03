# Clinical Outcome Capture: a decision for the physician champion

*Version 2 · first written 1 August 2026, updated 3 September 2026 · Written to
be read in ten minutes and signed at the end. Companion to
[SHADOW_MODE.md](SHADOW_MODE.md) section 5.1.*

---

## The decision, in one paragraph

Orb can record that it flagged bed 12 at 03:14. It cannot record that bed 12
went to intensive care at 06:20, because nothing in the system knows that
happened. Somebody has to write it down. This document asks you to decide who
writes it down, what counts as an event worth writing down, and when. It is
five decisions and a signature. It is not a request for a budget, a new system
or extra documentation: the recording takes about twenty seconds per shift, and
the screen for it is already built (section 5). What it needs is your
definition.

If this is not decided before shadow mode starts, the pilot's headline number,
detection lead time, does not exist. Not "is weak". Does not exist. And it
cannot be reconstructed afterwards, for the reasons in section 2.

---

## 1. What is being measured, and why it needs you

The pilot's central question is:

> When Orb said a patient was deteriorating, was it right, was it early, and by
> how much?

That is an interval between two moments. Orb owns the first one: it records, to
the second, when its risk score crossed a threshold and when it raised a
concern. It does not own the second one. The second moment is a clinical fact
that lives in your team's heads, in the notes, in the ICU's admission log and in
the handover, and Orb only learns it if somebody tells it.

Detection lead time = (time the clinical event happened) minus (time Orb first
flagged the patient).

Without the first term the number is undefined. Every other pilot metric
(false-alert rate, agreement, time saved) measures whether Orb is tolerable.
This is the only one that measures whether it is useful, and it is the one the
CIO, your board and anyone reviewing this pilot will look at first.

---

## 2. Why this cannot be defaulted, automated, or done later

Three separate reasons, each sufficient on its own.

**Orb's data model cannot represent these events on its own.** A patient's
status field in Orb is constrained to admitted, discharged or transferred.
There is no value for death. There is no value for a critical-care transfer as
distinct from any other transfer, none for a rapid-response call, none for an
escalation of care. Adding a field is easy; I have added it. Deciding what
belongs in it is a clinical judgement, and it is yours.

**No chart-derived signal is trustworthy for this.** I could try to infer an ICU
transfer from a ward change, or a deterioration from a burst of new orders.
Every such inference would be a guess, and it would be a guess that
systematically flatters Orb: the inference fires on the chart activity that
follows recognition, which is exactly the moment Orb is being timed against. A
metric whose ground truth is inferred from the same data stream as the
prediction is not a measurement. It is a mirror.

**Retrospective reconstruction is biased late, and the bias runs one way.** If
somebody reviews the notes next month and writes down when the patient
deteriorated, they will find the moment it was documented, not the moment it
began. Documentation lags recognition, and recognition lags deterioration. Both
lags push the recorded event time later, and a later event time makes Orb's
lead look longer. Every hour of reconstruction error is an hour of credit Orb
did not earn.

That is why the recording form makes the source mandatory and reports the
kinds separately:

| Source | What it means | How it is treated |
|---|---|---|
| witnessed | Somebody who was there recorded it, at or near the time | The conservative source. The headline number should rest on this one. |
| handover | Recorded at shift change from the outgoing team's account | Acceptable. Slightly later than witnessed. |
| chart_review | Reconstructed from notes afterwards | Recorded and counted, but reported separately, because it is systematically late and therefore inflates lead time. |

You cannot fix a late-biased number by collecting more of it. You fix it by
capturing at the time, which means deciding now.

---

## 3. The proposed event list

Five events. This list is already implemented in the recording form; changing
it is a one-line change, and I would rather change it than have you record
something you do not believe in.

| # | Event | Proposed definition | Counts | Does not count |
|---|---|---|---|---|
| 1 | ICU / critical-care transfer | Patient physically moves from the pilot ward to an intensive care or high-dependency bed, for clinical reasons | Unplanned transfer for deterioration; transfer for intensive monitoring after a deterioration event | Planned post-operative ICU admission booked before the deterioration; transfer for bed management with no clinical change |
| 2 | Rapid-response / medical-emergency call | Any call that summons a response team to the bedside outside the normal ward team | The hospital's own equivalent of a MET or rapid-response activation, however it is named locally | A routine review requested from the covering registrar during working hours |
| 3 | Code blue / cardiac arrest | Cardiopulmonary arrest with a resuscitation attempt | Arrest on the ward; arrest during transfer out of the ward | Arrest occurring after the patient has left the pilot ward's care |
| 4 | Unplanned escalation of care | A significant, unplanned increase in the intensity of treatment, short of an ICU transfer | Starting vasopressors, non-invasive or invasive ventilation, an urgent unplanned surgical decision, an urgent senior review called for deterioration | A routine medication change; a scheduled review |
| 5 | Death | Death on the pilot ward | Any in-ward death, expected or not | Death after discharge or after transfer off the ward |

### Three questions only you can answer

1. **Is this list right for your wards?** Anything missing that your team would
   consider a deterioration event? Anything on it that will be recorded so
   rarely it is not worth the field?
2. **Where does "unplanned escalation of care" start?** It is the widest
   definition and the one most open to interpretation. Your judgement on it
   determines how many events the pilot sees, which determines whether the
   headline number has enough data to be reported at all. Orb will not print a
   detection-lead-time figure until it has at least five matched events; a
   definition drawn too tightly could leave the pilot below that floor.
3. **Do expected deaths count?** A patient on a palliative pathway who dies as
   anticipated is a death, but scoring Orb against it measures nothing about
   early detection. My suggestion is to record it and mark it, so the number can
   be reported both ways. Your call.

---

## 4. Who records it, and when

### The proposal

| Who | What they record | When |
|---|---|---|
| The nurse in charge of the shift (charge nurse) | Any of the five events that occurred on their shift, for any patient on the pilot wards | At the time if possible; at the latest, before they hand over |
| The same person | The once-per-shift confirmation that no deterioration events occurred (section 6) | At handover, every shift, without exception |
| You, or a delegate you name | Adjudication of a sample of Orb's alerts (section 7) | Weekly, about thirty minutes |

### Why the charge nurse and not the doctor

Three reasons. The charge nurse is present for the whole shift and knows what
happened on every bed, where a doctor covers several wards. The charge nurse is
already doing a structured end-of-shift handover, so the recording attaches to
an existing habit rather than creating a new one. And the events being recorded
are facts about what happened, not clinical judgements requiring a physician:
"the patient went to ICU at 06:20" is not a diagnosis.

If you would rather it were the ward doctor, or the shift's senior nurse, or
split between them, say so on the decision sheet. The important property is one
named person per shift, not which role they hold. Two people who both think the
other did it is the failure mode this is designed against.

### What I am not asking for

- No duplicate documentation. This is not a second chart entry. The event is
  already documented in the hospital's own record; this is a timestamp and a
  category so a metric can be computed.
- No clinical judgement about Orb. The person recording is not asked whether
  Orb was right, whether it was early, or whether they saw an Orb alert. They
  are asked what happened to the patient. Judging Orb is section 7, and it is
  deliberately a different person at a different time.
- No change to care. During the silent phase of shadow mode nobody acts on Orb
  at all. Recording an outcome does not change what happens to the patient; it
  happens after.

---

## 5. The form

Four fields and a button, finishable in under twenty seconds, because a form
that takes two minutes gets skipped on the shift where it matters most.

**What exists today, precisely.** Both halves are built. The server accepts
exactly the five events in section 3, the three sources in section 2, and the
per-shift confirmation in section 6, and writes them into the pilot's event log
where the scorecard reads them. The screen itself has lived on the nurse's home
page since 10 August 2026 and is open to the charge-nurse role, so a nurse does
not need an administrator to record an event. One early defect is worth
mentioning because it is the kind that flatters a pilot: re-submitting "no
events this shift" used to append a second confirmation row every time; that
was fixed on 10 August and the database now refuses a duplicate. What is not
built is your definition, and the form cannot be used honestly without it.

```
┌─ Record a deterioration event ──────────────────────────────┐
│                                                              │
│  Patient           [ bed 12 · Ananya Iyer          ▾ ]      │
│                                                              │
│  What happened     ( ) ICU / critical-care transfer          │
│                    ( ) Rapid-response call                   │
│                    ( ) Code blue / cardiac arrest            │
│                    ( ) Unplanned escalation of care          │
│                    ( ) Death                                 │
│                                                              │
│  When              [ 06:20 ]  today ▾    ← defaults to now    │
│                                                              │
│  How do you know   (•) I was there / told at the time        │
│                    ( ) From handover                         │
│                    ( ) From the notes, afterwards            │
│                                                              │
│                                     [ Cancel ]  [ Record ]   │
└──────────────────────────────────────────────────────────────┘
```

**Why each field is there, and why there are no others.**

- **Patient**, picked from the ward's live list, so it cannot be mistyped.
- **What happened**, a closed list of five. Free text would make the metric
  uncountable; five radio buttons take a second.
- **When**, defaults to now, which is correct when it is recorded at the time.
  Editable, because "the transfer was at 06:20 and I am recording it at 07:40"
  is the normal case, and the true time is the one the metric needs.
- **How do you know**, the source, from section 2. This is the field people will
  wonder about. It is mandatory because it is the only defence against a lead
  time that quietly inflates itself.

There is deliberately no free-text box, no severity, no "was Orb right" field
and no reason field. Every one of those would add seconds, and none of them is
needed to compute the number.

**When it fails, it says so.** If the recording cannot be written, the form
returns an explicit failure and tells the person it was not recorded. It never
tells somebody their entry is saved when it is not. That asymmetry is
deliberate: elsewhere in Orb a failed background write is swallowed so that a
clinical action can never fail because of an instrument, but here a human has
just typed a fact and must not be deceived about it.

---

## 6. The once-per-shift confirmation: the part everyone forgets

This is the most important section in the document and the easiest one to skim.

### The problem

Suppose the pilot runs for six weeks and thirty deterioration events are
recorded. What is the event rate? You cannot say, because you do not know how
many shifts were watched. A shift with no recorded events might be a shift where
nothing happened, or a shift where nobody wrote anything down. Those two look
identical in the data and they mean opposite things.

If you assume unrecorded shifts were quiet, every rate in the pilot is
understated and Orb looks better than it is. If you assume they were unwatched,
you throw away real data. There is no statistical fix. The only fix is to make
"nothing happened" something somebody actively says.

### The proposal

At the end of every shift, on every pilot ward, the same charge nurse who would
have recorded an event confirms one of two things:

- one or more events, recorded on the form in section 5; or
- "No deterioration events this shift." One tap.

Three shifts a day, two wards, for the length of the pilot. Assuming the
hospital's shifts are the standard three (07:00 to 15:00, 15:00 to 23:00, 23:00
to 07:00), that is six confirmations a day. Tell me your actual shift
boundaries: Orb's coverage arithmetic is configured to them, and if they are
wrong the coverage figure is wrong.

### What Orb does with it

A ward-shift with neither a recorded event nor a no-events confirmation is
excluded from every denominator and reported on the face of the scorecard as
uncovered time. Orb does not guess. It shows you how much of the pilot it was
actually watching, and every rate is computed only over that.

This means a missed confirmation costs you data, not accuracy, which is the
right way round. It also means the scorecard tells you, week by week, whether
the recording discipline is holding, while there is still time to fix it.

### What it costs

One tap, six times a day, attached to a handover that already happens. If that
proves too much, the honest conclusion is not to relax the requirement but to
narrow the pilot to one ward, where it can be sustained. A pilot with uncertain
denominators produces a number nobody can defend in a due-diligence
conversation, which makes the whole exercise a waste of your staff's time.

---

## 7. Who judges whether an alert was a false alarm

Detection lead time answers "was Orb early". The false-alert rate answers "how
often would Orb have interrupted a nurse for nothing", and that question is a
clinical judgement. It needs a clinician, and it needs a method.

### Who

You, or one delegate you name in writing. One adjudicator, or two with a stated
rule for disagreements. Not me: a vendor grading its own homework produces a
number no reviewer will accept, and rightly.

### What sample

A defined sample, not a convenient one. My proposal:

- Every alert on two randomly selected days per week, rather than a fixed
  number of alerts. Random days protect against the natural tendency to review
  the interesting cases, which biases the rate upward or downward depending on
  temperament.
- Plus every alert associated with a recorded outcome event, reviewed
  separately, because those are the cases that matter most and there will not
  be many.
- Orb will not print a false-alert rate until it has at least ten adjudicated
  episodes. Below that it says "insufficient data", never "0%".

### Blinded, as far as is honest

The review screen shows you the patient, the vitals at the time, and what Orb
saw. It does not show you whether that patient later had a recorded outcome
event; knowing the answer while grading the question is the fastest way to make
an agreement metric meaningless. You are judging: given what was visible at that
moment, was this a real deterioration?

### The four possible answers

| Verdict | Meaning | Effect on the rate |
|---|---|---|
| True positive | A real deterioration | Counts in the numerator as correct |
| False positive | No deterioration | Counts in the numerator as a false alert |
| Artefact | The signal itself was wrong: a disconnected lead, a bad reading | Excluded from both sides. Blaming Orb for a monitoring failure is not honest |
| Indeterminate | The record does not settle it | Excluded from both sides. Recorded as indeterminate and reported |

Artefact and indeterminate are kept distinct on purpose. Folding an artefact
into "false positive" punishes Orb for a hardware fault; dropping either one
silently flatters the rate. Both are counted and both are shown.

### How often

About thirty minutes a week, alongside the weekly walk-through of the
scorecard. Doing it weekly rather than at the end matters: it is done while the
cases are fresh, and it surfaces a recording problem in week two rather than in
week seven.

---

## 8. What breaks if each piece is skipped

Put plainly, so the trade-offs are visible.

| If this is skipped | What is lost |
|---|---|
| The event list is not agreed | Nothing is recorded. Detection lead time does not exist and the pilot has no headline number. |
| The event list is agreed but nobody is named | Everybody assumes somebody else did it. Same result as above, discovered in week six. |
| Events are recorded but the source is not | Lead time drifts later and longer with no way to tell how much. The number looks better and is indefensible. |
| Events are recorded but only by chart review | Lead time is systematically inflated. Reviewers will ask how the ground truth was captured, and the answer will not survive the question. |
| The per-shift "no events" confirmation is skipped | Every rate is silently wrong in Orb's favour and nobody can say by how much. This is the failure that looks like success. |
| Adjudication has no named person | The false-alert rate reports "unadjudicated" for the entire pilot. Orb will not fill it in with a guess. |
| Adjudication uses a convenience sample | The rate reflects which cases were interesting, not which cases occurred. |
| The whole thing is left until after go-live | It cannot be retrofitted. The events of week one are gone, and week one is when the ward's behaviour is least influenced by Orb, the cleanest data in the pilot. |

---

## 9. What this still cannot prove

Worth being clear about before you sign anything, so it is never oversold on
your behalf.

- A lead time is not a benefit. Three hours between Orb's flag and an ICU
  transfer is evidence Orb saw something. It is not evidence that acting three
  hours earlier would have changed the outcome. Only a phase where clinicians
  act could show that, and by then the clean measurement is gone.
- The numbers will be small. Serious deterioration events on two wards over a
  few weeks are a handful. Expect wide uncertainty and resist reporting a single
  figure as though it were precise.
- It is one hospital. It establishes what happens here. It does not generalise.
- There is no control group. Nothing in this pilot supports a causal claim about
  patient outcomes, and nothing produced from it should be phrased as one.

If the numbers come out badly, they get reported as they are. A shadow pilot
that measures an unfavourable result and says so is doing exactly what it was
built for. One that only ever produces good numbers was never an instrument.

---

## 10. Decision sheet

*Physician champion, pilot wards. Please complete, sign and return before the
dress rehearsal (weeks 9 to 10 after signature). Shadow mode cannot start
without it.*

---

**Decision 1, the event list.** I confirm these events are to be recorded:

- [ ] ICU / critical-care transfer
- [ ] Rapid-response / medical-emergency call
- [ ] Code blue / cardiac arrest
- [ ] Unplanned escalation of care
- [ ] Death

Add or remove: ______________________________________________________________

My definition of "unplanned escalation of care" for these wards:

_____________________________________________________________________________

_____________________________________________________________________________

Expected deaths on a palliative pathway:  [ ] record and mark  [ ] do not record

---

**Decision 2, who records events.**

Role: ______________________________  (proposed: charge nurse of the shift)

Named backup when that role is absent: ______________________________________

Recorded: [ ] at the time  [ ] before end of shift  [ ] other: _______________

---

**Decision 3, the per-shift confirmation.**

- [ ] I accept the once-per-shift "no deterioration events" confirmation, on
      every pilot ward, every shift, and understand that shifts without it are
      excluded from the pilot's denominators and shown as uncovered time.

Our actual shift boundaries are:

Shift 1 ______ to ______   Shift 2 ______ to ______   Shift 3 ______ to ______

Confirmation is done by: ____________________________________________________

---

**Decision 4, false-alert adjudication.**

Adjudicator: ________________________________________________________________

Second adjudicator, if any: _________________________________________________

Rule for disagreement: ______________________________________________________

Sample: [ ] all alerts on two random days per week, plus all alerts linked to a
recorded outcome   [ ] other: _______________________________________________

Frequency: [ ] weekly, about 30 minutes   [ ] other: ________________________

---

**Decision 5, start date.**

Outcome capture begins on: ______________  (must be on or before the first day
of shadow mode)

---

**Acknowledgement**

- [ ] I understand that without outcome capture, detection lead time is not
      weak; it is undefined, and cannot be reconstructed after the fact.
- [ ] I understand that nothing Orb produces during the silent phase carries
      clinical authority, and that recording an outcome does not change care.

| | Name | Signature | Date |
|---|---|---|---|
| Physician champion | | | |
| Ward manager / nursing lead | | | |
| Orb (Samuel Morningstar) | | | |

---

## Related documents

| Document | What it covers |
|---|---|
| [SHADOW_MODE.md](SHADOW_MODE.md) | How the pilot runs; section 5.1 is the metric this document unblocks |
| [PILOT_ROADMAP.md](PILOT_ROADMAP.md) | The 13-week sequence and where this sits on it |
| [TRAINING.md](TRAINING.md) | What each role learns before go-live, including this form |
