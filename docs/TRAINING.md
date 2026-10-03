# Orb: Training Plan for the Pilot Wards

*Version 2 · first written 1 August 2026, updated 3 September 2026 · For the
physician champion, the nursing lead, the chief pharmacist and the hospital IT
lead. Delivered in weeks 9 to 10 after signature, on the hospital's own
appliance, using fictional patients.*

---

## 1. What this is

Before anybody uses Orb on a real patient, I teach it to them on the real
machine, in their own ward, with patients who do not exist. This document is
the plan for that: who is trained, for how long, on what, and, the part that
matters, what each person must be able to do unsupervised before they are
signed off.

Training is not a demonstration. A demonstration ends with people nodding. This
ends with a checklist that somebody has watched them complete.

| | |
|---|---|
| When | Weeks 9 to 10 after signature, once the appliance is installed and hardened |
| Where | On the ward, on the hospital's own appliance, on the devices staff will actually use |
| Who is trained | The physician champion, the charge nurses of both wards, the chief pharmacist, and the hospital IT lead (as system administrator) |
| Patients | Fictional, created for the session and removed afterwards (section 3) |
| Who delivers it | Me, on site |
| Total | About four hours, split by role; nobody sits through all of it |

Everybody who is trained is trained before shadow mode starts and before they
have any clinical access to Orb. During the silent phase of shadow mode nobody
uses Orb at all. Training happens first so that when access does open, the
learning has already been done rather than being done live on patients.

---

## 2. Before training can happen

Training cannot be scheduled until all of these are true. Status as of
3 September 2026.

| Prerequisite | State |
|---|---|
| Appliance installed on the ward, hardened, on its own network drop | Install phase |
| Every trainee has a real account with their own credentials: no shared logins, no demo accounts | The admin bootstrap tool exists (`create_admin.py --enroll-mfa`); accounts are created at install |
| Demo accounts and demo passwords removed from the appliance | Required before install; I check it |
| Nurse mode: vitals entry with live NEWS2, medication round, I-PASS handover and the end-of-shift outcome screen as the nurse's home | Built, 10 August 2026, and used end to end in the 14 August dry run |
| Manual vitals entry for clinicians | Built, part of nurse mode |
| Outcome-capture decisions signed by the champion | See [OUTCOME_CAPTURE.md](OUTCOME_CAPTURE.md); the screen is built, the definition is yours |
| The deterioration-event screen available to the recording role | Built and open to the charge-nurse role |
| On-device speech engine installed on the appliance | Install-day step; it is not installed on my development machine, so dictation is demonstrated on the appliance or not at all |
| Whether Lens (medical image analysis) is switched on for this hospital | Ships off in production. Section 7.5 |
| Quick-reference cards printed and laminated, one per role, at every nurse station | Cards in section 10 |

---

## 3. Ground rules for the training day

**Fictional patients only.** Every patient created during training is invented.
No real patient's details are entered, not even as an example, not even
partially. If a trainee starts typing a real name, I stop them.

**Real accounts, fictional patients.** Everybody signs in as themselves and
enrols their own authenticator app during the session. Training on a shared
account teaches the wrong habit and produces an audit trail that names nobody.

**The training patients are removed afterwards, before shadow mode starts.**
That is not tidiness. Fictional patients left on the appliance would be scored,
alerted on and counted in the pilot's numbers, which would corrupt the one
piece of evidence the whole pilot exists to produce. Because the erasure tool
currently refuses patients with shadow-mode rows (section 9.4), I start shadow
mode from a database seeded without the training patients rather than relying
on erasure, and I confirm the ward is clear before the roster import runs.

**Nobody is asked to memorise anything.** The quick-reference cards in section
10 exist so that the answer to "what do I press" is on the wall, not in
someone's memory at 3 a.m.

**Questions about what Orb gets wrong are the best questions in the room.** I
answer them fully. A member of staff who knows the failure modes is safer than
one who has been told it works.

---

## 4. Module 0: Everyone (45 minutes)

Every role attends this. It is the part that decides whether the pilot is safe.

### 4.1 What Orb is

Orb is the ward's clinical system: the patient board, the chart, orders,
medications, messaging, notes and an AI assistant. It runs on one computer in
this building. It replaces the system you use today for these wards.

Nothing leaves the hospital. The AI models run on the machine down the
corridor. There is no cloud service, no external account, and no internet
connection required for any clinical function. When you ask the assistant about
a patient, the question and the answer stay on that machine.

### 4.2 Signing in, and the security you will meet

1. Open a browser, go to the ward address, land on Clinician Login.
2. Enter your Employee ID and password.
3. If your account has two-factor authentication enabled, enter the six-digit
   code from your authenticator app.

Things that will happen and are not faults:

- Five wrong passwords from one workstation in fifteen minutes locks that
  workstation out for fifteen minutes. Eight wrong attempts against your account
  locks the account for fifteen minutes, from anywhere.
- Each authenticator code works once. Signing in twice quickly means waiting
  for the next code.
- Every sign-in, successful or not, is recorded permanently.

Enrol your authenticator now. Click the small lock icon on your user card at the
bottom of the sidebar, scan the code with Google Authenticator, Authy or
1Password, and type one live code to confirm. Enrolment is self-service and
takes a minute.

> Stated plainly: on the pilot appliance two-factor authentication is required
> for administrator accounts and optional for clinicians. Enforcement is a
> per-role setting the hospital can widen, and the Admin console reports
> enrolment by role so the gap is visible rather than assumed. If you want it
> mandatory for doctors and nurses too, tell me; the cost is a few seconds at
> every sign-in.

### 4.3 Finding your way

- Sidebar on the left: one tab per module. You only see the tabs your role can
  use.
- Header on the right: clock, system health, notification bell, an English /
  हिन्दी toggle (today it translates the navigation only; the rest of the
  interface stays English), and a theme toggle with a Night Shift high-contrast,
  low-brightness setting for overnight wards.
- Modules keep their place. Switching tabs and coming back does not lose a
  half-written note.

### 4.4 The patient board and NEWS2

The board lists every admitted patient in your department, worst first. Critical
patients carry a red badge and float to the top regardless of sorting.

Risk is scored with NEWS2, the Royal College of Physicians early warning score
you already know. It is a fixed, published, auditable table, not a black box:
respiratory rate, oxygen saturation, supplemental oxygen, blood pressure, heart
rate, temperature and consciousness, each scoring 0 to 3.

- Critical: NEWS2 7 or more
- Elevated: NEWS2 5 to 6, or any single parameter scoring 3
- Stable: everything else

Orb implements Scale 2 for COPD and chronic type-2 respiratory failure, selected
per patient, and labels the parameter "SpO2 (Scale 2)" when it is in use so you
can always see which scale produced the number. When an alert fires, the chart
shows a "Why NEWS2" panel listing every parameter's points and the trend over
the last six observations, and it says in its own footer that the score is
computed by Orb from the RCP table, not written by a model.

Two numbers, and knowing which is which matters. On the board, the right-hand
percentage is a ranking convenience that lets the sickest sort to the top. It is
not a probability that the patient will deteriorate. In the patient's chart, the
badge shows the raw NEWS2 integer, "NEWS2 7 · CRITICAL". Every escalation
decision is taken from the raw score and its band, never from the percentage.

### 4.5 Downtime: what you look at when everything is off

Every patient has a DOWNTIME button on their chart. It opens a plain, printable,
read-only page: demographics, allergies at the top, latest vitals with the NEWS2
score, and current medications. It uses no AI and no network, which is exactly
why it still works when those are gone. The appliance also writes a whole-ward
pack to disk every hour that can be printed even when the server itself is
down.

If the appliance, the network or the power fails, you print those pages and work
on paper, exactly as you would today. Nothing clinical depends on Orb being up.

### 4.6 Shadow mode: why you cannot act on Orb yet

This is the part people find strange, so I explain it properly rather than
asserting it.

For the first phase of the pilot, Orb watches the ward and says nothing.

| Phase | Who sees Orb's clinical output | What you do |
|---|---|---|
| A, Silent | Nobody. I and the champion see a scorecard; no clinician sees a live alert | Work exactly as you do today |
| B, Champion only | The physician champion, and nobody else | The champion judges the alerts, case by case. They are explicitly not asked to act on them |
| C, Phased access | Access opens by role and by ward, on a schedule the champion sets | Alerting comes before drafting; both come before anything that touches a chart |

Why. The pilot exists to answer one question: when Orb said a patient was
deteriorating, was it right and was it early? That question can only be
answered while nobody is acting on the answer. From the moment a clinician sees
an Orb alert, Orb has changed the outcome it is being measured against, and
there is no way to get the clean measurement back.

There is a second reason and it is about your safety, not the data. A
monitoring system that has never been measured on this ward, with this case mix
and these staffing patterns, has an unknown false-alarm rate. Alert fatigue is a
documented patient-safety hazard in its own right. The silent phase measures
how often Orb would have interrupted you, at zero cost, before anybody is
interrupted.

If you see an Orb clinical output during the silent phase: it carries no
clinical authority. Do not let it change what you do. Tell me so the leak can be
closed; a visible output during Phase A is a defect and I treat it as one.

This will feel wrong at least once. There will be a moment when Orb has flagged
something and nobody is allowed to use it. That discipline is what makes the
eventual number mean anything, and one influenced case cannot be un-influenced.

Any phase can be reverted, immediately, without a meeting, at the champion's or
my discretion. Reverting is a normal operational action and is not treated as a
failure.

---

## 5. Module 1: Doctors (90 minutes)

**Attendees:** the physician champion and the ward doctors.
**Tabs a doctor sees:** Today, Patients, Ward Round, Orders, Messages, Sage,
Scribe, Lens (if enabled), Forecast, Command, Simulator, Revenue, Appointments.

### 5.1 The chart (20 min)

Open a patient. The command bar across the top carries identity, bed, NEWS2
badge, diagnosis and live vitals; hover any vital cell for a six-hour trend.
When a colleague is on the same chart, a presence chip says so, which is how two
people avoid charting the same patient unknowingly. When an alert has fired, the
chart also carries Sage's written assessment with Approve and Decline buttons;
during Phase A you will not see it, and in later phases it is a draft, not an
instruction.

Practise: Diag. Reports, Lab. Reports (critical values carry a red badge and the
trend arrows cover fifteen analytes), Notes, Story (one merged timeline of the
whole admission), HANDOFF (auto-drafted I-PASS), DOWNTIME, PRINT RECORD, PRINT
MEDS.

Only a doctor can admit a patient and only a doctor can sign a note. Those two
are the boundaries of the role.

> Known gap as of 1 August, to re-check at install: the Admit Patient button was
> visible to nurses, but the server refuses the action because admitting
> requires a permission nurses do not hold. Nothing unsafe happens; the record
> is simply not created and the refusal is logged. If it is still visible at
> install, tell nurses not to use it.

### 5.2 Ordering, and the safety gate (25 min)

Practise placing a lab order and a medication order, and practise applying an
order set.

- Coded orders (lab, imaging, medication, procedure) require a validated code:
  LOINC, SNOMED or ICD-10. Click Validate; a tick fills the description in for
  you. The server re-checks the code independently, so an invalid code cannot
  be saved by any route.
- Nursing and free-text orders do not need a code.
- Order sets (Sepsis Six, ACS, DKA and others, each citing its protocol) are
  applied whole or not at all. Every item is screened against the chosen
  patient's allergies and doses; if one item is stopped, the set does not apply
  and the button says why. Practise this on the fictional patient with a
  charted anaphylaxis, so you see the refusal before you meet it at 2 a.m.

Every medication order passes through the drug-safety gate, and every other
route by which a drug can enter Orb passes through the same one: adding a
medication from the chart, an ambient suggestion from Relay, an AI-drafted
action, a medication extracted from a signed note, a pharmacy approval. There
is no back door.

| What you see | What it means |
|---|---|
| Nothing | No allergy or interaction match. The order is created |
| Drug-safety review dialog | Mild or moderate alerts. The order is created; read and close |
| Severe drug-safety alerts dialog | The order is blocked. To proceed you must type a clinical justification of at least ten characters and click Acknowledge and Order. Your identity, your words and the alerts you overrode are written permanently to the tamper-evident record. Only a prescriber can do this; a nurse is never shown the override |
| Not screened chip | Orb could not map the drug to one of its twelve known drug classes, so it was not interaction-checked. Allergy matching by name still ran |
| No chip, no alerts | The drug was classified, but Orb ships only 55 curated interaction pairs, so "no alerts" often means "nothing on a short list matched", not "compared against a full database". Never read it as a clearance |
| "The drug-safety screen could not run" | The screen failed, so Orb refused the order rather than passing it unchecked. That is deliberate. Do not read an outage as clearance |

Practise the severe override on a fictional patient, so the first time you meet
that dialog is not at 2 a.m.

Two limits, stated plainly. The interaction database and the terminology set
are curated starter sets sized for a pilot. They catch the high-consequence
pairs (penicillin and cephalosporin cross-reactivity, warfarin with NSAIDs,
duplicate anticoagulation, potassium combinations) and they are not
comprehensive. "No alerts" means nothing in the local database matched. It is
not a clinical clearance and it does not replace pharmacist review.

### 5.3 Scribe (15 min)

Dictate a note, review the structured draft, edit it, sign it.

- Transcription is done locally by Whisper, on the appliance. Speech does not
  leave the building. If the status line reads "transcription engine
  unavailable", the speech engine has not been installed on that machine;
  typing still works and nothing else is affected.
- The draft is a draft. Nothing files itself. A note enters the record when a
  doctor signs it, and only then. If the verification step has flagged
  something in the draft, the sign button will not let you past it silently;
  you acknowledge the flag by name.
- Read the transcript before signing. Whisper mis-hears drug names and numbers,
  and there is a clinical spelling corrector behind it that fixes common cases
  but not all of them. The correction is a help, not a guarantee.
- Whisper handles English, Hindi and code-switching between them; the language
  is selected per session.

### 5.4 Relay (10 min)

Team messaging, with a case room per patient. Practise sending, and practise
reading an ambient Suggested Action card. Also practise the wrong-patient
warning: send a message about one patient into another patient's room and watch
Orb say "this looks like it belongs to" the other patient, with a one-click
move. The message is not blocked; it is flagged.

A suggestion card has three buttons: Acknowledge, Dismiss, Confirm. Only
Confirm does anything downstream: it routes the item to the pharmacy queue, to
Scribe, or to the transfer workflow, and moves the card to Handled Actions with
a status. Dismissed and confirmed cards both stay visible as a record of what
was suggested and what was decided.

Executing an action card is a doctor-only capability. Nurses can read and send
in Relay; they cannot confirm an action card.

### 5.5 Sage for doctors (20 min)

Covered in full in section 7. For doctors specifically, practise:

- a general clinical question, and read where the citations come from;
- a question about a patient on screen, using "this patient";
- asking Sage to do something ("add paracetamol 500 mg for the patient in bed
  4") and watching it produce a draft that requires your explicit Approve and
  Send to Helix click, and even then it lands in the pharmacy queue, not on the
  medication chart;
- reading a High-Risk Query banner and a Limited evidence banner, and knowing
  what each means.

---

## 6. Module 2: Nurses (90 minutes)

**Attendees:** charge nurses of both wards, plus any nurse who will use Orb.
**Tabs a nurse sees:** My Shift (the nurse home), Patients, Orders, Messages,
Sage, Scribe, Lens (if enabled), Forecast, Revenue, Appointments.

### 6.1 The shift board (15 min)

The nurse's home is a task-shaped view of the shift rather than the doctor's
patient board: who needs observations, what medications are due, what has
changed since handover, with a NEWS2 badge on every row. Practise starting a
shift on it, on the tablet you will actually use.

### 6.2 Recording observations (20 min)

Practise on a tablet, standing up, with gloves on. That is the real test.

- Enter a full set of observations for a fictional patient.
- Watch the NEWS2 score compute as you type: each abnormal value shows its
  points beside the field, and the panel names the drivers before anything is
  saved.
- Enter a set with a deranged single parameter and see the single-parameter
  escalation rule lift the patient to elevated even with a low total.
- Enter a patient on Scale 2 (COPD) and see the SpO2 parameter labelled
  differently.

Where the vitals come from. On selected high-acuity beds the monitors will feed
Orb directly and read-only, once the adapter has been tested against your
monitors with the biomedical team; Orb can never send anything to a monitor or
change a setting on one. Everywhere else, and as the backup everywhere, a nurse
types them. Both paths are recorded with their source, so it is always visible
which readings came from a machine and which from a person.

### 6.3 The medication round (25 min)

Open My Shift, then Med round for a patient. The round groups what is due,
what is overdue (past the due time derived from the ordered frequency by more
than fifteen minutes), and what is continuous. Treat the due times as a prompt
derived from frequency, not as a prescriber's schedule.

The Administer dialog is built around the five rights:

1. Read the summary: patient and bed, drug, dose, route, time. Check each line
   against the medication in your hand and the wristband on the patient.
2. Type the drug name exactly as charted into the confirm box. The Given button
   stays disabled until it matches. That is a deliberate comparison, not a
   formality.
3. Given: records the dose. Or:
4. Not given: requires a reason from the list (Refused, Held, clinical decision,
   NPO, Unavailable, Other). "Other" requires a written detail. A withheld dose
   can never be recorded without a reason.

Things that will happen:

- The Administer button is greyed out with "Pharmacist verification required"
  on any medication that reached the chart through an AI pathway and has not
  been checked by a pharmacist. That block is enforced on the server too, not
  just in the screen. It cannot be worked around, and it is not a fault.
- If the drug matches a charted allergy, the Administer dialog leads with the
  allergy and refuses. You will not be shown an override, because only a
  prescriber can clear a block over a charted allergy, with a reason, on the
  record. Call the prescriber. This was tightened on 15 August 2026 after I
  found a path where an old override could be carried forward by a nurse; that
  path is closed and tested.
- Recording twice because the network hiccuped charts one dose, not two.
- Administration records are never edited or deleted. A correction is a new
  row.

> Stated plainly: the type-to-confirm box is a check, not a barcode scanner. It
> forces you to compare the name in your hand with the name on the chart. It
> cannot verify the vial. Do the full five-rights check at the bedside every
> time.

### 6.4 Handover (15 min)

The Handover tab on My Shift drafts an I-PASS handover for each patient:
Illness severity, Patient summary, Action list, Situation awareness, Synthesis.
It is a draft. Read it, edit it, and confirm it, with the read-back fields
completed by the receiver. It is a starting point that saves typing, not a
handover somebody else wrote for you.

### 6.5 Recording a deterioration event (15 min)

This is the single most important thing the charge nurse does for the pilot,
and it takes twenty seconds. The screen is on My Shift, at the end of the
shift.

At the end of every shift, on every pilot ward, the charge nurse records either:

- any deterioration events that occurred (ICU or critical-care transfer,
  rapid-response call, code blue, unplanned escalation of care, or death), with
  the time and how you know it; or
- "No deterioration events this shift." One tap.

Why the "no events" tap matters more than it looks. Without it, a shift where
nothing happened and a shift where nobody wrote anything down look identical in
the data, and they mean opposite things. Orb refuses to guess: a shift with
neither an event nor a confirmation is excluded from the pilot's arithmetic and
shown as uncovered time. So a missed tap costs data, not accuracy. It just
costs data, every time.

Practise it twice: once recording an event, once confirming none. Full detail
in [OUTCOME_CAPTURE.md](OUTCOME_CAPTURE.md).

---

## 7. The AI, honestly: everyone attends this (30 minutes)

Delivered once, to the whole group. If a member of staff attends nothing else,
they attend this.

### 7.1 What Sage is for

Sage is a clinical assistant you ask questions in plain language, about one
patient, the whole ward, a drug, or a protocol. It reads the live chart,
searches the hospital's own guideline library, and answers.

It is decision support. It is not an ordering system and it is not a second
opinion with authority.

### 7.2 It drafts. It never commits.

Sage cannot write to a chart. That is enforced by ordinary software around the
model, not by asking the model nicely, so a model error cannot bypass it.

- Ask Sage to add a medication and you get a Chart Action card with the
  medication marked Drafted. Nothing has happened.
- Only your explicit Approve and Send to Helix click sends it onward, and even
  then it lands in the pharmacy verification queue, not on the medication
  chart.
- Any other chart change is refused outright: "I understood your request to
  update the chart, but direct modifications are not currently supported. No
  chart change has been made." A phrasing like "stop the heparin for this
  patient" is caught by a rule before the model is even asked.
- Phrases in which the model claims to have acted ("I've added", "Order
  placed", "Chart updated", a bare "Done.") are stripped from every answer,
  typed or spoken, before you can see them.
- Ambient suggestions in Relay require Confirm. Acknowledge and Dismiss do
  nothing downstream.
- No automated path can approve itself. An AI or background process can never
  acknowledge a severe drug-safety alert or administer an unverified
  medication. Human review is structurally required, not merely recommended.

### 7.3 Every answer carries its sources, and the sources are checked

Sage does not answer from the model's memory. It assembles the patient's facts
and searches the hospital's guideline library first, then writes.

- Show details under any clinical answer expands into Patient Facts, Local
  Policy, External Guidance, Clinical Assessment, Caveats and Escalation.
- Guideline documents carry a trust tier, and your hospital's own policy
  outranks external guidance. Local Policy and Local Formulary rank above
  External Guideline, and the details panel separates them visually.
- Citations are verified, not trusted. The model must say which documents it
  used; Orb then checks those against what was actually retrieved. A cited
  document that was not in the evidence pack is discarded and the answer's
  confidence is downgraded, which is what puts the amber Limited evidence badge
  on screen. The source count you see reflects only verified citations. When
  the model forgets to cite at all, Orb attaches the sources it retrieved and
  labels them as attributed by the system, not by the model.

The banners, and what each is telling you:

| Banner | What happened |
|---|---|
| High-Risk Query, verify with attending | The question matched a high-risk pattern: chest pain, respiratory failure, stroke, sepsis and shock, high-risk drugs, or any dose change. Treat the answer as a discussion aid and confirm before acting |
| Limited evidence, verify independently | Retrieval was weak, or citations failed validation. Check the source yourself |
| Partial data | The chart is missing categories Sage would normally use: no recent labs, no signed notes. The answer was formed without them |
| Stopped by user | You pressed Stop. The answer is incomplete |

These banners are applied by Orb's own logic, not by the model, so the model
cannot suppress them. The answer footer also shows how long the first words
took, so nobody has to guess whether the system is slow or stuck.

### 7.4 What it gets wrong

I say all of this out loud in the room. It is the section that makes the rest
credible.

- It is bounded by the local library. Sage does not search the internet. Its
  knowledge is the hospital's uploaded guidelines plus the model's own training.
  A thin corpus in your specialty produces tangential matches, which is exactly
  what "Limited evidence" is flagging.
- The underlying model is not validated on your data, and Orb says so. The
  Admin console's model registry states it plainly: MedGemma 27B, pretrained,
  not fine-tuned on this hospital's data, decision support only. The registry
  also states that Vigil's optional learned overlay is untrained with no
  checkpoint installed, so risk scoring runs on NEWS2 alone, and that the
  Forecast model is trained on synthetic data and is not validated for clinical
  use. The Trust Center reads "1 of 4 models validated", and the one is NEWS2.
- It has been caught being wrong, twice that I will tell you about. During
  benchmarking it reported a NEWS2 score of 7 for a patient the deterministic
  engine scored 11; that was found, root-caused and fixed, and every NEWS2
  figure in an answer is now copied from the engine. And in the 14 August dry
  run the escalation board showed NEWS2 10 where the nurse had charted 19,
  because manually entered vitals were reaching the alert path by a different
  lane; fixed the same day and now a permanent test. The lesson in both cases
  is that the deterministic score on the chart is the one that decides.
- Voice transcription mis-hears names and doses. Read the transcript back.
- Ambient suggestions come from informal chat, which is why they carry
  confidence thresholds and why nothing below them is shown at all.
- It is slow, on purpose, and here are the numbers. A 27-billion-parameter
  model on the hospital's own hardware is measurably slower than a cloud
  chatbot. On my development machine, first words for a question that carries
  a patient's chart take about 6 to 12 seconds with the model warm; short
  definitional questions land in 3 to 5 seconds; a full written assessment of
  an alert takes 30 to 50 seconds. Under three seconds is not achievable for
  chart questions on this hardware, so do not promise it to anyone. That is the
  price of the patient data never leaving the building, and it is a price the
  hospital chose. The alert itself is a different matter: it reaches the screen
  in well under a second, because no model is involved.
- There is a speed/accuracy switch, and it has a floor. The pill beside the
  send box reads Thinking (the careful 27B model, the default) or Fast (the
  small 4B model). Fast mode is measurably less accurate: in a head-to-head on
  Orb's own prompts the small model was wrong on 40% of single-fact drug and
  dose recall. So the switch is overridden for you on anything about a specific
  patient, anything involving a medication or a dose, and anything about a
  score Orb computes itself; those always go to the careful model, and the
  answer says so. Teach staff to read the label on the answer, not to assume.
- It gets slower when the ward is busiest. I measured it: during an alert storm
  a Sage answer took 20.7 seconds against 8 to 12 seconds when quiet, because
  the small ambient model was holding the graphics processor at a 76% duty
  cycle. Since 10 August background work is bounded and yields while a
  clinician is asking, but the effect is not zero, and it is disclosed here
  rather than discovered at the bedside.

### 7.5 Where nothing goes

- Every inference runs on the appliance. There is exactly one engine and it is
  local. No patient question, no chart content and no answer is sent to any
  outside service.
- Whisper transcription is local. The audio does not leave the machine.
- One exception exists and it ships switched off. The browser's own speech
  recognition, used only for the live on-screen transcript and the automatic
  stop-on-silence, sends audio to a cloud service, which would be spoken
  patient data leaving the building. It is off by default; the server tells the
  browser whether it is permitted, and if the browser cannot get an answer it
  assumes not permitted. With it off, voice still works entirely: you tap to
  stop instead of relying on silence detection, and Whisper still transcribes
  locally. Staff will see the status line read "Listening (local transcription;
  tap to stop)", and that is the correct, private state.
- The Pulse panel pulls public data in (weather, air quality, influenza
  surveillance, drug recalls), and its footer states the rule: inbound only,
  zero patient data transmitted.
- Lens (medical image analysis) ships switched off in production. It has no
  validated imaging model behind it; it is a general vision model writing
  reference prose in the most regulated area Orb touches. I do not train staff
  on Lens unless the champion has explicitly asked for it to be enabled, and if
  it is enabled, I train it as reference only, never as a diagnostic read.

### 7.6 The one rule

Sage advises. A clinician decides. Every answer, and especially every answer
carrying a banner, is an input to your judgement and never a substitute for it.

---

## 8. Module 3: Pharmacist (45 minutes)

**Attendee:** the chief pharmacist, or the pharmacist covering the pilot wards.
**Tabs a pharmacist sees:** exactly three: Pharmacy, Verify Queue, Sage.

### 8.1 The Verify Queue: the job that matters most (20 min)

Any medication that reached a patient's chart through an AI pathway (extracted
from a signed note, drafted by Sage, suggested by the ambient assistant) is held
as unverified and cannot be administered until a pharmacist clears it. The
nurse's Administer button is disabled, on the server as well as in the screen,
until you act.

You are the final human checkpoint on every drug the AI has touched. Practise
verifying one, and practise rejecting one.

### 8.2 The Pharmacy workspace (20 min)

Overview, Patient Board and Formulary. Practise the order lifecycle: Begin
Review, then Verify, Reject, or Suggest Substitute; then Schedule and dispense.
The lifecycle is a guarded state machine: an order cannot skip verification or
be dispensed twice, and the server refuses any transition not in the table.

Practise stock adjustment and read how dispensing deducts from inventory lots.

### 8.3 Sage for pharmacists (5 min)

You have Sage for clinical context. Everything in section 7 applies. Note
especially that "no alerts" from the drug-safety screen means nothing in Orb's
55-pair starter interaction set matched; it is not a clearance. The Not screened
chip means Orb could not even classify the drug. Its absence only tells you the
drug was classified, not that a meaningful comparison existed.

---

## 9. Module 4: Administrator (60 minutes)

**Attendee:** the hospital IT lead, plus whoever will be the named Orb
administrator.
**Note:** the administrator role reads clinical data for governance. It cannot
prescribe, place orders, chart vitals, sign notes, publish discharges, or
execute a Relay action card. It can work the pharmacy queue and the surgical
case list, and that includes verifying an AI-drafted medication into an active
order. I say that plainly in the room: an admin is not purely an observer, and
the person holding the account needs to know which of their buttons reach a
patient. If the hospital wants those two permissions taken out of the admin
role, that is a one-file change I make before install.

### 9.1 Users (15 min)

Create an account, set a role, reset a password, deactivate an account. Practise
all four.

Role boundaries, as enforced on the server for every request:

| | Doctor | Nurse | Pharmacist | Admin |
|---|:-:|:-:|:-:|:-:|
| View any patient, vitals, alerts | ✓ | ✓ | n/a | ✓ read-only |
| Admit / edit a patient record | ✓ | n/a | n/a | n/a |
| Prescribe or modify medications | ✓ | ✓ | n/a | n/a |
| Override a charted-allergy block | ✓ | n/a | n/a | n/a |
| Create lab / imaging / procedure orders | ✓ | ✓ | n/a | view |
| Draft a Scribe note | ✓ | ✓ | n/a | read |
| Sign a Scribe note | ✓ | n/a | n/a | n/a |
| Publish a discharge summary | ✓ | view | n/a | view |
| Execute a Relay action card | ✓ | n/a | n/a | n/a |
| Pharmacy: verify, dispense, adjust stock | n/a | n/a | ✓ | ✓ |
| Surgical cases, WHO checklist, start/complete | ✓ | ✓ | n/a | ✓ |
| Audit log, tracing, ingestion monitor, admin dashboard | n/a | n/a | n/a | ✓ |

The money desks (billing, payments, front desk, procurement, cashier) have had
their own roles since 18 August 2026 and are not shown here; a cashier can be
staffed without any governance rights. The Admin console's Trust Center has a
Show matrix button that renders the full grid live from the server's own
permission map. That is the authoritative answer to "can a nurse do X?", not
this table, and not anybody's memory.

### 9.2 The audit trail (15 min)

Every state-changing action is recorded, hash-chained, and cannot be altered
without visibly breaking the chain.

Practise: filter the Audit Logs; take a correlation ID and open Trace to see
every event that shares it as one pipeline; open the Flight Recorder and replay
a window ("show me everything around 03:20 for bed 12") with chain verification
on each row.

What the chain proves and what it does not. It guarantees that recorded events
have not been altered or deleted. It cannot record actions taken outside Orb;
somebody reading a printed downtime sheet leaves no trace, and should not be
expected to.

### 9.3 Trust Center (10 min)

A live compliance report computed from the running system, not a static
document. Trust score, control checklist with the evidence behind each verdict,
MFA enrolment by role, encryption posture, and the RBAC matrix.

Amber and red appear when they are true. That is what makes the green ones mean
anything. Walk the amber items and know what each one is asking for. One of
them, the TLS row, is currently hardcoded to amber and does not read the
configuration; read it against the real deployment.

### 9.4 Data and backups (15 min)

- Read the backup status. Once scheduled at install, backups run nightly at
  02:30 and are verified before anything is rotated out; a failed verification
  deletes nothing, by design. Since 14 August the verifier checks every table,
  not a sample of them.
- Patient erasure. Type the patient ID, then type the exact phrase `ERASE {id}`
  to arm the red button. It is irreversible. It is refused for a currently
  admitted patient (discharge or transfer first), because erasing a live
  inpatient's record is a clinical-safety hazard. A single audit row recording
  the erasure survives, so the fact of it stays provable. Known defect, still
  open on 3 September: on the current build the purge aborts with a server
  error for any patient carrying real shadow-mode records, so nothing is erased
  (the transaction is atomic, so nothing is half-erased either). That is why I
  start shadow mode from a database seeded without training patients rather
  than erasing them.
- Downtime readiness. The Command Center shows what fraction of admitted
  patients have a snapshot fresher than 24 hours and the age of the stalest
  one. Capture all snapshots every admitted patient in one action. Treat an
  amber or red tile as a prompt before the end of the shift.
- Know where the two sealed key envelopes are and who can open the safe. Know
  that if both are lost, every backup is permanently unreadable and there is no
  vendor override.

---

## 10. Quick-reference cards

Print each on one side of A5, laminate, and put it at every nurse station, in
the pharmacy, and beside the appliance.

---

### ORB: DOCTOR

**Sign in** Employee ID + password (+ 6-digit code if enrolled)
**Home** Patients · sickest first · red badge = critical

**NEWS2** 7 or more critical · 5 to 6 elevated · any single parameter scoring 3 = elevated
The raw integer in the chart decides. The board percentage is only a sort order.

**Chart buttons** Diag. Reports · Lab. Reports · Medications · Notes · Story ·
HANDOFF · DOWNTIME · PRINT RECORD · PRINT MEDS

**Ordering** Coded orders need a validated code; click Validate for the tick.
Medication orders always pass the safety gate. Order sets apply whole or not at all.
· Mild/moderate: review dialog, order is created
· Severe: blocked. Type 10+ characters of clinical justification, then
Acknowledge and Order. Permanently audited. Prescribers only.
· Not screened chip = Orb could not classify that drug; it was not interaction-checked
· No chip is not proof of a real comparison; the shipped set is 55 pairs
· Screen failed = order refused. An outage is not a clearance.

**Only a doctor** admits a patient · signs a note · confirms a Relay action card ·
overrides a charted-allergy block.

**Sage** drafts, never commits. Approve and Send to Helix is the only thing that
moves a drafted medication, and it goes to the pharmacy queue, not the chart.
Red banner = high-risk, verify with the attending. Amber = weak evidence, check it
yourself. The footer shows how long the answer took.

**Sage advises. You decide.**

**Everything is on this machine. Nothing goes to a cloud.**

---

### ORB: NURSE

**Sign in** Employee ID + password (+ 6-digit code if enrolled)
**Home** My Shift: observations due, medications due, changes since handover

**Observations** Enter a full set; NEWS2 computes as you type, points shown per field.
COPD / chronic type-2 respiratory failure: Scale 2, target 88 to 92%.
Monitor-fed beds fill themselves. You are the backup everywhere.

**Medication round** My Shift → Med round → Administer
1. Check patient, drug, dose, route against the wristband and the vial
2. Type the drug name exactly as charted; Given stays locked until it matches
3. Given, or Not given + a reason (Refused / Held / NPO / Unavailable / Other)
· Greyed out with "Pharmacist verification required" = an AI-sourced drug pharmacy
has not cleared. Not a fault. Ask pharmacy.
· Refused for a charted allergy = call the prescriber. You cannot override it.
· The confirm box is a check, not a barcode scanner. Do the five rights.
· "Overdue" is derived from frequency, not from a prescriber's schedule.

**Handover** Handover tab drafts I-PASS. Read it, edit it, confirm it.

**END OF EVERY SHIFT, 20 seconds, never skip:**
Record any ICU transfer · rapid-response call · code blue · unplanned escalation
· death, with the time and how you know.
Otherwise tap "No deterioration events this shift."
No tap = that shift is not counted at all.

**Nurses draft notes; doctors sign them.**

**If it is all down:** DOWNTIME page on each patient, or the hourly ward pack.
Print. Work on paper.

---

### ORB: PHARMACIST

**Sign in** Employee ID + password (+ 6-digit code if enrolled)
**Your three tabs** Pharmacy · Verify Queue · Sage

**Verify Queue = the job.** Every medication that reached a chart through an AI
pathway is held unverified. The nurse cannot administer it until you clear it.
That block is enforced on the server, not just on screen.

**Lifecycle** Pending → Begin Review → Verify / Reject / Suggest Substitute →
Schedule → Dispense
Guarded: nothing skips verification, nothing dispenses twice.

**Remember**
· "No alerts" = nothing in Orb's 55-pair starter set matched. Not a clearance.
· Not screened = Orb could not classify that drug at all.
· Its absence does not prove a comparison happened; the set is that small.
· The interaction database is a curated starter set, not comprehensive.
· No automated process can approve itself. You are the human checkpoint.

---

### ORB: ADMINISTRATOR

**Sign in** Employee ID + password + 6-digit code (required for this role)
**Admin tabs** Overview · Pilot Scorecard · Audit Logs · Trace · Sage Provenance ·
Ingestion · Security · Alerts · Model Governance · Impact · Trust Center ·
Flight Recorder · Users · Roster Import · Data & Backups

**Daily, about 10 min**
- [ ] System health green?
- [ ] Any overnight ingestion gaps?
- [ ] Backup ran and verified?
- [ ] Alert storm or error spike?
- [ ] Scorecard still populating?

**Weekly**
- [ ] Downtime readiness green; run Capture all if not
- [ ] Trust Center: any control gone amber?
- [ ] Walk the week's alerts with the champion; adjudicate the sample

**Monthly**
- [ ] Restore drill, on the standby machine, with the key from the envelope

**Erase a patient** ID → type `ERASE {id}` → red button. Irreversible.
Refused while admitted; discharge or transfer first. Currently fails for
patients with shadow-mode rows (known defect).

**Answering "can a nurse do X?"** Trust Center → Show matrix. Live from the
server's own permission map. Not from memory.

**The audit chain** proves recorded events were not altered. It cannot record
what happened outside Orb.

**If both key envelopes are lost, every backup is permanently unreadable. There
is no vendor override.**

---

## 11. Competency checklists

Nobody is signed off on a role until every line is ticked by me, having watched
them do it. Unsupervised means unsupervised.

### Doctor

- [ ] Signs in with their own credentials and completes MFA enrolment
- [ ] Finds a named patient on the board and opens the chart
- [ ] States what NEWS2 7 means, and which of the two displayed numbers decides
- [ ] Opens Labs, Notes, Story and a six-hour vital trend
- [ ] Places a validated coded lab order
- [ ] Places a medication order and reads a moderate-alert review dialog
- [ ] Applies an order set to a patient with a charted allergy and explains why
      the whole set was held
- [ ] Completes a severe drug-safety override with a written justification, and
      states where that justification ends up
- [ ] Explains what the Not screened chip means, and why its absence is not
      proof that a real interaction comparison happened
- [ ] Dictates, reviews, corrects and signs a Scribe note (or, if the speech
      engine is not installed, types one and passes the sign gate)
- [ ] Asks Sage a patient question and reads its verified sources
- [ ] Asks Sage to add a medication, and states what has and has not happened
- [ ] Identifies the High-Risk and Limited-evidence banners and says what to do
- [ ] Triggers and resolves a wrong-patient warning in Relay
- [ ] Opens and prints a DOWNTIME snapshot
- [ ] States the Phase A rule: no clinical authority, tell the operator

### Nurse

- [ ] Signs in with their own credentials and completes MFA enrolment
- [ ] Starts a shift on My Shift and finds what is due
- [ ] Records a complete set of observations on a tablet, standing
- [ ] Reads the resulting NEWS2 band and states the single-parameter rule
- [ ] Identifies a Scale 2 patient and states why the target is 88 to 92%
- [ ] Completes a medication administration through the type-to-confirm check
- [ ] Records a withheld dose with a valid reason
- [ ] Explains why the Administer button is greyed out on an unverified drug,
      and what to do about it
- [ ] Explains what happens when a drug matches a charted allergy, and who to
      call
- [ ] Drafts, edits and confirms an I-PASS handover
- [ ] Records a deterioration event on the 20-second form, with its source
- [ ] Records a "no deterioration events this shift" confirmation
- [ ] States what happens to the pilot's numbers if that confirmation is skipped
- [ ] Opens and prints a DOWNTIME snapshot
- [ ] States the Phase A rule: no clinical authority, tell the operator

### Pharmacist

- [ ] Signs in with their own credentials and completes MFA enrolment
- [ ] Opens the Verify Queue and states why an item is in it
- [ ] Verifies one AI-sourced medication and rejects another
- [ ] States what a nurse sees before and after verification
- [ ] Moves an order through Begin Review → Verify → Schedule → Dispense
- [ ] Suggests a substitute and describes where it goes
- [ ] Adjusts stock and reads the resulting inventory movement
- [ ] States what "no alerts" and "not screened" each mean
- [ ] States the Phase A rule

### Administrator

- [ ] Signs in with MFA enrolled
- [ ] Creates a user, sets a role, resets a password, deactivates an account
- [ ] Filters the audit log and traces one correlation ID end to end
- [ ] Replays a time window in the Flight Recorder and reads chain verification
- [ ] Opens Trust Center, explains one amber control and its remediation
- [ ] Expands the RBAC matrix and answers a "can a nurse do X?" question from it
- [ ] Reads backup status and states when the last verified backup ran
- [ ] Performs a restore drill on the standby machine using the escrowed key
- [ ] Runs Capture all downtime snapshots and reads the readiness figure
- [ ] Opens the Pilot Scorecard and states what "uncovered time" means
- [ ] States where both key envelopes are and who can open the safe
- [ ] States what happens if both are lost

---

## 12. After training

- [ ] Ward confirmed clear of training patients (fresh seed for shadow mode)
- [ ] Every trainee's MFA enrolment confirmed in Trust Center
- [ ] Any demo account still present on the appliance removed
- [ ] Quick-reference cards laminated and placed
- [ ] Competency checklists filed with the pilot record, signed and dated
- [ ] Anything a trainee could not complete written down and scheduled, not
      quietly passed
- [ ] Champion confirms in writing that the wards are trained

Retraining. Anyone who joins the pilot wards after this session gets Module 0
plus their role module before they touch Orb. There is no shortcut and no
shadowing-a-colleague route; the audit trail names individuals, and so does the
responsibility.

---

## Trainer's notes, to myself

The mood to aim for is "this is a tool with known limits", not "this is
impressive". Staff who have been told a system is impressive stop checking it.
Staff who have been told exactly where it fails keep checking it, which is the
behaviour the pilot needs and the behaviour that keeps patients safe.

Spend the time on the refusals, not the features. The severe drug-safety block,
the greyed-out Administer button, the allergy refusal at the bedside, the "no
chart change has been made" message, the Phase A rule. Those are the moments
where a person meets the system saying no, and a person who understands why it
said no does not go looking for a way round it.

If someone asks a question I cannot answer, I write it down and say I will find
out. A wrong confident answer from the vendor in a training room is worse than
a gap, because it gets repeated on the ward for months.

---

## Related documents

| Document | What it covers |
|---|---|
| [USER_MANUAL.md](USER_MANUAL.md) | The full reference for every module, the source for everything summarised here |
| [SHADOW_MODE.md](SHADOW_MODE.md) | How the pilot runs, and the phases explained in section 4.6 |
| [OUTCOME_CAPTURE.md](OUTCOME_CAPTURE.md) | The deterioration-event recording in section 6.5, and the decision behind it |
| [HARDWARE_SPEC.md](HARDWARE_SPEC.md) | The appliance training is delivered on |
| [PILOT_ROADMAP.md](PILOT_ROADMAP.md) | Where training sits in the 13-week sequence |
