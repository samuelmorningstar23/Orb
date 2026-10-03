# Orb: Demo Script

**Written 2026-08-08 by the Wave E audit, from the app as it actually behaves this
morning, not from the spec.** Every question below was asked. Every screen below
was opened. Where something is slow or ugly it says so, and it says what to do
about it.

Evidence for every claim: `docs/DEMO_READINESS.md` (Wave E section, top of file).

---

## ⇢ RE-VERIFIED 2026-08-18: READ THIS BLOCK FIRST, THEN THE SCRIPT

A full browser sweep of the built bundle at :8000 was re-run on 2026-08-18
(cold start, real logins, the demo beats driven end to end). The core script
still holds: **the Sepsis Six interlock, the wrong-room flag and the Administer
modal all behave exactly as written below, verified live.** Four things have
changed since 2026-08-09, and two of them need a decision before demo day.

### 1. RESEED ON THE MORNING OF THE DEMO: this is now mandatory, not optional

The 2026-08-09 reseed fixed the stale ward *on that day*. Staleness is a
function of the clock, and nine days later it is all back: the Sepsis Six
panel reads **"9d 11h · 0 of 6 done"**, every Relay room carries a **99+**
unread badge of accumulated Vigil auto-alerts, the med round shows doses
**"20h late / 55 not charted"**, and the long-running simulator has walked
**every patient in the hospital to CRITICAL** (15+ simultaneous critical
alerts at boot; three of five demo-ward patients read EMERGENCY with SpO2 in
the low 80s). None of that is a code defect, it is what nine days of
unattended simulation does. The fix is the same one command, **run it the
morning of the demo, every time**:

```bash
cd /Users/samuelmorningstar/Documents/Projects/Orb/server && .venv/bin/python scripts/reset_db_and_seed.py
```

The reseed also clears two pieces of test residue a viewer would otherwise
see on the demo ward: a **duplicate Ceftriaxone order on Suresh Reddy**
(med 301, charted 2026-08-15 to verify the allergy override) and this sweep's
wrong-room probe sitting at the bottom of his case room.

### 2. ADM001 NOW HITS AN MFA-ENROLLMENT WALL: Act 6 needs a decision

`.env` sets `ORB_MFA_REQUIRED_ROLES=admin`, so signing in as **ADM001 goes
straight into a QR-code MFA enrollment modal**, Act 6 cannot start until it
is resolved (Cancel returns to the login card; the wall is fail-closed and
correct). Two ways to run demo day, pick one in advance:

* **Enroll it**, after the morning reseed, sign in as ADM001 once, scan the
  QR into your authenticator, and demo *with* MFA. In front of a CIO this is
  arguably a beat, not a bug: the governance account demands a second factor.
* **Lift it for the demo**, comment `ORB_MFA_REQUIRED_ROLES=admin` out of
  `.env` for the demo boot. Weakens the security story if he asks.

Do not enroll on the *un-reseeded* shared dev DB, the enrollment sticks.

### 3. The app has grown since this script was written: landmarks moved

* **Login now lands on "Orb Today"** (deteriorating list, new results, shift
  confirmation, pending signatures), not on Patients. Act 1 starts one click
  further: open **Patients** from the sidebar, or point at Today first; the
  deteriorating list is itself a strong opening beat.
* **Ward capacity is `/15`** (`ORB_WARD_BEDS=15` in `.env`), so the Command
  Center heatmap reads `5/15`, not the `5/12` written below.
* **The chart now carries a Sage alert assessment with Approve/Decline**, 
  a drafted, fact-selected AI assessment under every critical alert ("FROM
  THIS CHART / WHAT SAGE WOULD CONSIDER / WATCH FOR", model named, confidence
  stated, "approving this draft carries nothing out"). It is the strongest
  new thing on the chart, but on an un-reseeded ward the queue reads "10
  further assessments awaiting review", which looks like alert fatigue.
  Reseed and it starts clean.
* **The allergy override is prescriber-only end to end** (ruled and built
  2026-08-15): a nurse administering across a charted severe allergy is
  refused with no override control and told who can authorise; the Administer
  modal now restates the charted allergy on its face. The Act 4a line "resolve
  these on the individual orders" now ends in a genuinely demoable override
  flow, prescriber login, written reason, re-armed type-to-confirm, named
  button, audit row.
* **Order-set previews now show a middle verdict** (fixed 2026-08-18): a
  non-blocking allergy alert renders as an amber **"Allergy alert"** chip with
  the cross-reactivity sentence, instead of collapsing to "Cleared". Act 4a's
  "try Ananya Kapoor" rebuttal beat depends on this chip, it is what makes
  "four patients, four different answers" visible on screen rather than only
  in the API.

### 4. Act 4c (the C-201 refusal): run it from a FRESH chat

Verified 2026-08-18: asked *with a patient chart already anchored*, Sage
correctly refuses the C-201 patient but then **pivots to the anchored
patient** ("…I only have Suresh Reddy in GM-104. His latest potassium is
6.2 mEq/L…"). Transparent, but it dilutes the refusal beat. Click **New Chat**
first, then ask the C-201 line; expect ~30 s in Thinking mode, with the
high-risk-query banners rendering above the answer while it generates.

*(Also checked 2026-08-18: socket.io live at boot, health 200 in 1 s, zero
backend tracebacks across the whole sweep, clean shutdown, light theme clean,
lab-trend arrows correctly coloured by distance-from-range on all 15 analytes,
P001 Bridge login works. The one console error on first load, a ServiceWorker
registration failure, appeared only in the sandboxed audit browser; verify
once in real Chrome and then ignore it.)*

---

## READ THIS FIRST: THE ONE THING THAT CHANGES THE SCRIPT

> ### ⇢ UPDATED 2026-08-09: THE BOX HAS RECOVERED. RE-MEASURE, THEN CHOOSE.
>
> The paragraph below was true on 2026-08-08 and is **no longer true**.
> Re-measured on a quiet box at 00:0x on 2026-08-09:
>
> | | 2026-08-08 (as found) | 2026-08-09 (quiet box) |
> |---|---|---|
> | `medgemma:4b` decode | **~9 tok/s** | **71.7 tok/s** |
>
> That is an **8× recovery**, and it clears this script's own ≥30 tok/s bar for
> "run it as written, and put the Sage moments back where they belong."
>
> The 27–95 s and 74–127 s figures were **measurement of a contended machine**,
> not a defect in Orb. Nothing in the Sage code path changed to cause either the
> slowness or the recovery, the box was being eaten by other applications.
>
> **Two caveats before you move Sage earlier:**
> 1. A first Sage question measured **16 s to first content while three
>    background jobs were running**. Run step 5 below on a genuinely quiet box
>    and use *that* number to decide. Do not trust either figure without
>    re-measuring on the morning.
> 2. Scribe note-structuring is a separate case: `.env:30` deliberately routes it
>    to **`medgemma:27b`**, so it is slower *by design* and the 4B decode rate
>    does not predict it. That is a quality choice, not a defect.
>
> The rest of this section is preserved as written, because if the box is busy
> again on the morning it is still the correct plan.

**Sage was slow on this machine on 2026-08-08.** Not 6–8 s. Measured that
morning: **27–95 seconds** for a general question, **74–127 seconds** for a chart
question. The cause is the inference stack, not Orb, raw Ollama with Orb removed
decoded at **~9 tokens/second** on a 4B model, about five times slower than this
hardware should manage. See `DEMO_READINESS.md` → "THE HEADLINE".

Because of that, **this script leads with the deterministic safety features, not
with Sage.** That is not a retreat. The allergy interlock, the wrong-room warning
and the cross-department refusal are the most investor-impressive things Orb does,
they are instant, and they show *judgement* rather than *generation*, which is
the harder and more valuable thing to have built.

### 30 minutes before the investor arrives

```bash
# 1. Quit WhatsApp, Opera GX, anything else. They were eating this box this morning.

# 2. Start the stack: this exact command, nothing else
cd /Users/samuelmorningstar/Documents/Projects/Orb
./run-orb.sh --dev

# 3. Confirm it is healthy
curl -s -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
curl -s -o /dev/null -w "socketio=%{http_code}\n" "http://127.0.0.1:8000/socket.io/?EIO=4&transport=polling"
# both must be 200. If socket.io is not 200, live vitals are dead: see DEMO_READINESS §16.1

# 4. Confirm both models are warm
ollama ps          # expect medgemma:4b AND medgemma:27b

# 5. MEASURE THE BOX. This decides which version of the script you run.
curl -s -X POST http://localhost:11434/api/generate \
  -d '{"model":"medgemma:4b","prompt":"Explain sepsis in 200 words.","stream":false,"options":{"num_predict":200,"num_ctx":4096},"keep_alive":-1}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(round(d["eval_count"]/(d["eval_duration"]/1e9),1),"tok/s")'
```

* **≥ 30 tok/s**, the box has recovered. Run the script as written, and put the
  Sage moments back where they belong (Act 3 can move earlier).
  *(It measured **71.7 tok/s** on 2026-08-09.)*
* **≈ 9 tok/s**, the box is as I found it. Run the script exactly as written:
  ask Sage *once*, early, and let it generate in a background tab while you talk.

### 6. THE RESEED: **DONE 2026-08-09 11:20**, nothing for you to decide

This section used to ask you to choose between reseeding and steering around a
stale ward. That choice has been made and executed. **The database has been
reseeded.** What follows is what you now have.

Almost every "Avoid" in the old version of this script was the **same bug**: the
demo database was last seeded 2026-08-01, and until 2026-08-09 the seed script
anchored admissions to a fixed *May 2026* window. Every elapsed-time display was
correctly reporting an ~11-week-old ward: Sepsis Six *"10d 8h"* against a
60-minute target, Normal Saline *"250h late"*, *"Day 79 of admission"*, an empty
surgical schedule, 0% bundle compliance, stale Copilot suggestions, and Relay
rooms carrying weeks of audit debris.

**All of that is gone.** Verified after the reseed:

| Was | Now |
|---|---|
| Admissions anchored to May 2026 (~11 weeks stale) | **36–72 hours ago**, rolling window |
| Sepsis Six clock *"10d 8h"* / *226 h* vs a 60-min target | **3–4 minutes** |
| 80,234 Relay messages, unread badges in the thousands | **7 messages**, badges clean |
| Impact dashboard in **US dollars** | **Rupees**, ₹7,02,000/yr with lakh grouping |

**Your ward is the one you rehearsed.** The seeder is deterministic about the
demo five, so the names, beds and diagnoses are unchanged:

> Ananya Kapoor GM-101 (DKA) · Rajesh Iyer GM-102 (COPD) · Priya Nair GM-103
> (dengue) · **Suresh Reddy GM-104 (CAP with early sepsis)** · Lakshmi Devi
> GM-105 (UTI with delirium)

Suresh Reddy's **severe ceftriaxone allergy is seeded**, so Act 4a's interlock
beat has its fuel. Two other allergies (Ananya/penicillin, Priya/ibuprofen) are
there if he asks you to try another patient.

If you ever need to re-run it, it is now safe to run repeatedly, and it
preserves the audit chain and shadow-mode evidence rather than aborting on them:

```bash
cd /Users/samuelmorningstar/Documents/Projects/Orb/server && .venv/bin/python scripts/reset_db_and_seed.py
```

**Both of the things this section used to warn you about are now fixed.** The
Impact dashboard is in rupees, and the Command Center heatmap shows the six real
wards at `5/12` each instead of `W1 30/8`. Nothing on the Command Center needs
steering around any more.

One setting to know if he asks: ward capacity defaults to **12 beds/ward**
(`ORB_WARD_BEDS`). Ward *names* are discovered from the site; the bed *count* is
configuration, because occupancy can only tell you how many beds are full, not
how many exist. The honest answer is *"twelve is the default, we set it to your
real bed count at install."*

**A note on the Impact dashboard after a reseed:** three of its four rows read
**₹0**, Documentation, Medication safety, Throughput, because those count
Scribe notes, pharmacist verifications and discharge reconciliations, and a
fresh database has none yet. This is the dashboard being honest ("0 recorded
events"), not broken. It is also an opportunity: **if you run Acts 3–5 before
showing him this screen, those rows fill in from the work you just did in front
of him.** That is a far better beat than a pre-populated number.

**Then, before he sits down:** open the browser at **1512 × 1100 or larger**
(the Ward Round columns are cramped below ~1080 px tall), sign in as
**DOC001 / password**, and leave it on the Patients board.

---

## THE RUNNING ORDER

Six acts, ~25 minutes. The story is: *a ward that watches itself* → *a system that
refuses* → *a system that shows its work*.

---

### ACT 1: The ward is alive (2 min)

**Sign in as DOC001 / password.** Land on **Patients**.

Say: *"This is a General Medicine ward. Nothing here is a mock-up, the vitals are
moving because a monitor feed is running, and everything you're about to see is a
real database on this machine, encrypted at rest, with no internet connection."*

Point at, in this order:

1. **The numbers moving.** HR, SpO2, RR change while you talk. Wait for it, it is
   the cheapest proof that this is a live system.
2. **"Sage, State of the Hospital"** at the top: *"3 patients need attention
   first"*, and it names them.
3. **The risk ordering.** Rajesh Iyer and Ananya Kapoor at the top, the other
   three below. These are NEWS2-derived, not hand-placed, **and the percentages
   move while you watch**, because the score is recomputed off the live feed.
   Don't quote a specific number; point at the *order*.
4. **The `9 NEW` badge on Rajesh Iyer.** *"That badge means nine things have
   happened on that chart since I last marked it reviewed. Not nine things since
   yesterday, since **I** last looked."*

**Open Rajesh Iyer.** Point at the header: `MRN-GM002 · 67Y · MALE`, and the NEWS2
band, **CRITICAL** (the number itself was oscillating 12–16 this morning as the
feed moved; say "critical", not "fourteen"). Then open the
**"Why NEWS2 …?, per-parameter contribution"** disclosure. *"It will not tell you
a score without telling you where the score came from."*

> **Avoid:** the Sepsis Six bundle panel on this chart reads *"9d 15h since
> 7/29/2026"*. The demo database has not been reseeded. If his eye lands on it:
> *"that's a bundle left open in test data from a week ago"*, and move on.

---

### ACT 2: The chart tells one story (2 min)

Still on Rajesh Iyer, click **🧬 Story**.

Say: *"Every EHR I've seen makes you open six tabs to answer 'what happened to this
patient'. This is one scrollable narrative, alerts, notes, prescriptions, doses
given, handover notes, care bundles, in one order."*

Point at:

1. **"9 new since you last reviewed this chart on Jul 29, 05:30 AM"** at the top.
2. **The "Reviewed to here" divider** part-way down the list. *"That's my
   bookmark. Everything above it is new to me."*
3. **The filter chips**, All / Admission / Alerts / Notes / Scribe / Meds / eMAR
   / Bundles.
4. Scroll to an **eMAR** row: *"Gave Prednisolone, 40 mg PO, Priya Reddy"*.
   *"Every dose, who gave it, when."*

Then press **`?`**.

The keyboard card opens: `g p` Patients, `g r` Ward Round, `g o` Orders,
`g m` Messages, `g s` Sage, `⌘K` find a patient. Close it and press **`g` then
`r`**, you land on Ward Round.

Say: *"That's a small thing, but it's the thing that tells you whether software
was built by people who watch clinicians work. A doctor on a round does not want
a mouse."*

---

### ACT 3: Ask Sage (start it here, come back to it) (1 min to start)

You are on Ward Round. Switch to **Sage** (`g s`).

**Set the toggle to `Fast`** (it defaults to `Thinking`). Note the line that
appears under the composer and **read it out**, it is one of the strongest things
on the screen:

> *"Fast mode. General questions are answered by the small model, quicker, less
> accurate. **Patient, medication and dose questions still go to the careful
> model.**"*

Say: *"You can make Orb faster. You cannot make it faster about a drug dose. That
floor is not a setting."*

**Ask exactly this, then leave it generating and carry on talking:**

> **What are the classic ECG findings in hyperkalaemia?**

It answers correctly, peaked T waves, prolonged PR, widened QRS, sine wave, with
a safety warning inline, and a **"Show details · 3 retrieved"** disclosure listing
the guidelines it read.

**On this box that takes ~30 seconds.** Do not stand in silence. Frame it and
move: *"While that runs, and I'll come back to why it takes as long as it does, 
let me show you the part I actually care about."*

**Go straight to Act 4.** Come back to the answer when it has landed.

> **If the box measured ≥ 30 tok/s**, ask a second one for the contrast:
> **First-line management of an acute severe asthma attack in an adult?**
>
> **If the box measured ~9 tok/s**, ask only one. One good answer beats three
> slow ones.

**When you come back to it**, point at **Show details** and the retrieved
guidelines, and say the honest thing about speed:

> *"That's a 27-billion-parameter model and a 4-billion-parameter model running on
> this laptop, with no internet. On the pilot appliance that's a GPU box, not a
> laptop I've been running builds on all night. What I'd ask you to take from it
> is not the speed, it's that it cited what it read, and that it refuses to
> answer some things at all. Which is next."*

> **Avoid:** do NOT ask Sage to summarise a named patient from a fresh chat. It
> failed to attach the chart once this morning and answered *"you haven't
> specified which patient"*. **If you want a chart answer: open the patient's
> chart first, then go to Sage.** And budget two minutes for it.

---

### ACT 4: The refusals. This is the demo. (8 min)

> Everything in this act is **deterministic**, rules, not the model. It is
> instant, it is the same every time, and it is what a CIO is actually buying.

#### 4a. The allergy interlock: *Orders → Order sets → Sepsis Six → Suresh Reddy*

Go to **Orders**, tab **Order sets**.

Before picking anything, read the header out: *"Cited protocols. Every item still
goes through the allergy interlock, the dose guard and terminology validation, 
and a set is applied whole or not at all."*

Click **Sepsis Six (1-hour bundle)**. The right panel says something worth
pausing on:

> *"The safety gates are patient-specific, allergies, weight, renal function, so
> there is nothing honest to show until Orb knows whose chart this is."*

In **Apply to**, type **`Suresh`** and pick **Suresh Reddy, GM-104 · MRN-GM004 ·
55M · CAP with early sepsis**.

Nine items screen. Eight come back **Cleared**. One does not:

> **Ceftriaxone 2 g IV, first dose STAT, after cultures are drawn**
> **STOPPED**, *Patient is allergic to Ceftriaxone (documented reaction:
> Anaphylaxis, urticaria, angio-oedema and hypotension after IV ceftriaxone
> in 2023).*

> **Changed 2026-08-09, this is better than it was.** Until this morning that
> line read *"Severe drug-safety alerts must be acknowledged with a reason before
> this order proceeds"*, a sentence about the **procedure**, which never named the
> finding. The screen now states the actual reaction, which means **you no longer
> have to supply the punchline yourself**; the software says it. Let him read it
> before you say the line below.

And the **Apply 9 order(s)** button is **disabled**, under:

> *"1 item(s) are stopped by a safety check. A set is applied whole or not at all, 
> resolve these on the individual orders, where the override is recorded against
> the dose it applies to."*

Say: *"Suresh Reddy had anaphylaxis to IV ceftriaxone in 2023. It's on his chart.
A junior doctor at 3 a.m. reaching for the sepsis protocol, which is the right
protocol, would have given him the drug that nearly killed him. Orb will not
apply the set. Not the drug: **the set**. Because a half-applied sepsis bundle is
its own emergency."*

Then the point that lands hardest:

> *"And notice it isn't refusing to let me override. It's refusing to let me
> override it **here**, in bulk, where the reason wouldn't be attached to
> anything. Order it individually and you can override it, with a reason, on the
> record, against that dose."*

**If he pushes, "so it just blocks everything with an allergy?", you have the
answer, and it is a very good one.** Change the patient to **Ananya Kapoor**
(penicillin allergy, moderate rash). Ceftriaxone is a cephalosporin, cross-
reactive with penicillin. Orb **raises the allergy alert but does not block**:
severity-proportionate, because a moderate rash is not anaphylaxis. Then try
**Priya Nair** (ibuprofen) and **Rajesh Iyer** (no allergies), both clean.

*"Four patients, four different answers. That's not a keyword filter."*

#### 4b. The wrong-room warning: *Messages → Case: Suresh Reddy*

Go to **Messages**, open **Case: Suresh Reddy**.

**Type it live, it reads better and it auto-scrolls into view.** This **exact
sentence**, into Suresh Reddy's room:

> **Give Rajesh Iyer 40mg prednisolone now.**

Orb flags it instantly, under the message:

> ⚠ **This looks like it belongs to Rajesh Iyer**
> *The message names Rajesh Iyer and records clinical detail, a measurement, dose
> or result, about them. It has already been sent; nothing is blocked.*
> **[ It's the right patient ]  [ Move to Case: Rajesh Iyer ]**

> The same message and flag are **already in that room** from this morning's audit
> (06:57), if you would rather point than type, but it sits at the very bottom of
> the transcript, partly under the composer, so you would have to scroll. Typing
> it live scrolls for you.

Say: *"Wrong patient, right ward. That is the single most common serious error in
a hospital and almost nothing catches it. Orb doesn't block the message, a nurse
mid-emergency does not need a modal, it delivers it and says 'this looks like it
belongs to Rajesh Iyer, want me to move it?'"*

> **⚠ THE SENTENCE MATTERS.** Use an **imperative with a dose**. A *question*
>, *"is Rajesh Iyer's SpO2 still 84?"*, is deliberately demoted to a quieter
> band and will show you **nothing**, because Orb is careful about crying wolf on
> a room where two patients are being discussed legitimately. It is correct
> behaviour and a terrible demo. **Do not improvise this line.**

#### 4c. The cross-department refusal: *Sage*

In **Sage**, ask:

> **What is the potassium result for the patient in cardiology bed C-201?**

Orb refuses. It is a General Medicine login; cardiology is not its ward.

Say: *"I'm a real doctor with a real login and it will not tell me. Scoping isn't
a screen that hides a button, it's enforced in the server. If I hit the API
directly for a surgical patient's chart I get a 404: not 'forbidden', which would
confirm the patient exists, **404**, as if they weren't there at all."*

> **Honest note:** the refusal wording here is softer than the crisp one Orb gives
> for a *real* patient in another department, bed C-201 doesn't exist, so it
> answers "no such patient in your view" rather than "not your department". Both
> are correct. If you want the sharp version, you need a real cross-department
> patient id, and that is a worse thing to type in front of someone.

---

### ACT 5: The nurse's shift, and where the evidence comes from (6 min)

**Sign out. Sign in as NUR001 / password.** She lands on **My Shift**, not on a
patient list.

Say: *"Same database, completely different product. A nurse doesn't want a chart, 
she wants a list of what is due."*

1. **Due now (6)**, medications and observations, each with how late it is.
2. Click an **Observations** task → the recording sheet: RR, SpO2, systolic,
   diastolic, HR, temperature, supplemental oxygen, and **ACVPU** consciousness.
   Type a few values and watch **NEWS2 compute live**, with the escalation
   sentence underneath. *"She sees the score before she saves it, so she knows
   what she's about to trigger."*
3. Point at the footnote, *"Observation intervals follow the RCP NEWS2 escalation
   table. Medication due times are derived from each order's frequency and the
   last charted dose, **Orb holds no prescriber-authored administration
   schedule**."* *"That last sentence is Orb telling you the limit of what it
   knows. I'd rather ship that than a number I can't defend."*
4. Click a **medication** task → the administration modal: patient, drug, dose,
   route, time, and a **barcode-style check**, *type the drug name exactly as
   charted to enable "Given"*. Three outcomes: **Cancel / Not given / Given**.
   *"'Not given' is a first-class outcome. Most systems make omission invisible."*
   **Cancel out**, do not chart a dose.
5. Tab to **Handover**, the I-PASS draft.

> **Avoid:** Suresh Reddy's Normal Saline reads **"233h 4m late"** and several
> rows say "N not charted". Stale seed data. Stay on **Due now** and do not scroll
> the whole list.

---

### ACT 6: Show your work (5 min)

**Sign out. Sign in as ADM001 / password → Admin.**

Four tabs, in this order. Do not open the others.

1. **Trust Center.** *"Live compliance posture, aggregated from the running
   system, not a PDF."* Trust score **85**. **9 passing, 4 warnings, 0 failing.**
   Point at the warnings: *"Those are amber because they are amber. If this
   screen were all green you should trust it less."*

2. **Flight Recorder / the audit chain.** *"SHA-256 hash chain over every audit
   row, alter or delete one and the chain breaks."* **Verified, chain intact,
   70,760 sealed rows**, broken down by kind: 30,597 API mutations, 14,131
   authorization, 10,706 clinical access, 9,025 PHI access, 3,279 AI inferences.
   *"Every question Sage was asked is in there. So is every refusal."*

3. **Model Governance.** This is the differentiator in front of a CIO, hold your
   nerve on it. It lists each model, its version, its task and whether it has been
   validated. **1 of 4 validated**, and the four are:

   > **This screen used to be a landmine and is now safe.** Until 2026-09-07 a box
   > could list five models, including a retired `sage_llm_hosted` hosted lane, because
   > the registry cleanup ran only on an empty database. Migration 38 and a reconcile in
   > `main.py`'s lifespan now repair it at upgrade and at every boot, so starting the
   > appliance is enough. No pre-demo check needed.

   | Model | Version | Validated |
   |---|---|---|
   | `vigil_news2`, early-warning score (NEWS2, incl. Scale 2 for COPD) | `rcp-news2-2017` | **yes** |
   | `vigil_cfc`, deterioration / sepsis overlay | `cfc-lnn-v0` | no |
   | `sage_llm`, clinical RAG assistant | `medgemma-27b` | no |
   | `forecast_lstm`, length-of-stay / acuity forecasting | **`untrained`** | no |

   Say it out loud: *"One of four. The forecast model literally says 'untrained'.
   I could have hidden this screen. And notice **which** one is validated, the
   deterministic early-warning score, the one that actually drives escalation. The
   things that are validated are the things we let make decisions. A hospital that
   buys clinical AI and can't get this list out of the vendor should not buy it."*

4. **Pilot Scorecard.** How the pilot will be measured, shadow-mode episodes,
   adjudication, outcomes.

**Close on:** *"There's no cloud in this. No PHI leaves the box. The database is
encrypted at rest, the audit log is tamper-evident, and the models run locally.
That's not a privacy feature we bolted on, it's why the architecture looks the
way it does."*

---

## THINGS TO AVOID, AND WHY: SAY THESE ONLY IF ASKED

| Do not open | Why |
|---|---|
| **Pharmacy (PHA001) / Helix** | All zeros. Demo data is seeded for General Medicine only. *"Configured per site; this box is seeded for one ward."* |
| ~~**Surgical Suite (DOC004)**~~, **cleared by the reseed** | Its schedule used to be empty / contradict the medical charts. It now holds five procedures spanning **−6 h to +90 h**, so the next-7-days window is populated. Safe to open; still General-Medicine-flavoured data. |
| ~~**Command Center heatmap tile**~~, **FIXED, now worth showing** | It read `W1 30/8` (thirty patients in an eight-bed ward) because `_normalize_ward` funnelled six real wards into one fictional one. It now reads the actual wards, **CCU · GM-1 · NW-1 · OW-1 · RCU · SW-1, each `5/12`**, with a `30/72 · 41.7%` census that agrees with the capacity forecast below it. The one number still at zero is **Sepsis Bundle Compliance 0%**, and after the reseed that is *true*: 16 bundles, no elements worked yet. Turn it into a beat, run Sepsis Six in front of him and let it move. |
| **Any microphone / "Dictate"** | Whisper is not installed. Every control now *says* so rather than failing silently, which is the right behaviour, but there is no voice demo. |
| **"Set up this tablet for PIN sign-in"** on the login card | There is no admin UI to mint the enrolment code. Sign in with employee ID. |
| ~~**Roster Import**~~, **NOW SHOWABLE (2026-08-09)** | It has a screen: **Admin → Roster Import**. Upload a ward-list CSV → it sniffs the delimiter and auto-maps the columns → **Dry run** shows exactly what would be created, updated and skipped, and says *"nothing has been written yet"* → **Apply**. Verified end to end. It is a genuinely good 90-second beat for a CIO, because the dry run is the whole trust argument: *"it tells you what it will do to your patient list before it does it, and it will not let you apply a plan you have not seen."* It also lists patients Orb holds that the file does not mention, and deliberately offers **no** button to discharge them. |
| **Asking Sage to summarise a named patient from a fresh chat** | Failed to attach the chart once this morning. Open the chart first. |
| **Improvising the wrong-room sentence** | A question is demoted to a silent band. Use the imperative with a dose. |
| ~~**Order Sets → "Already applied to this patient"**~~, **cleared by the reseed** | The two *Acute severe asthma* applications from the audit run are gone (`order_set_applications` is empty). Suresh Reddy's Sepsis Six preview is now clean: 8 Cleared, 1 STOPPED, no prior-application ledger to explain away. |
| ~~**The Sage Copilot panel in Suresh Reddy's room**~~, **cleared by the reseed** | The stale *"START INSULIN INFUSION · Target: Ananya Kapoor"* suggestion is gone (`sage_actions` is empty). The sidebar no longer carries a cross-patient suggestion with a Confirm button, so the wrong-room beat is no longer competing with it. |

### If he asks about speed

Do not bluff. *"Slower than it should be, and I know exactly why, the models on
this laptop are decoding at about a fifth of the rate this silicon should manage,
and I have that measured. The pilot appliance is a GPU box, not my dev machine.
What I'd ask you to judge today is the judgement, not the throughput."*

### If he asks "what's actually finished?"

*"The safety layer, the audit layer and the clinical workflows are finished, you
just watched them refuse me three times. The AI is real but it's the part I'd
still call a pilot. That's the honest split, and it's why the pilot is designed
to run in shadow mode first."*

### If he asks about the default credentials warning in the terminal

Point at it and let him read it:

> `CRITICAL orb.bootstrap: SHIPPED DEFAULT CREDENTIALS FOUND … This appliance must
> NOT be handed to a hospital in this state.`

*"That's Orb refusing to let me forget. It's a dev profile. It will not start in
that state on a hospital profile."*

---

## THE FIVE SENTENCES, IF YOU ONLY GET FIVE MINUTES

1. *"It runs entirely on this box. No internet, no cloud, encrypted at rest."*
2. *"It stopped a sepsis protocol because one drug in it nearly killed that
   patient in 2023, and it stopped the whole set, not just the drug."*
3. *"It caught a message about the wrong patient and offered to move it."*
4. *"Every row of the audit log is hash-chained. Seventy thousand rows, verified."*
5. *"It'll tell you two of its four models ship untrained, because I'd rather you
   find that out from me."*
