# Orb: build log, in plain English

What we built, why it matters on a hospital ward, and what it still does not do.
Written for someone who does not read code.


## What this all means

Orb is the computer system a hospital runs on: the patient records, the drug
charts, the observations, the messages between staff, and an assistant that can
answer questions about a patient. It is meant to replace the hospital's existing
system rather than sit beside it, and it runs on one machine inside the hospital
no patient information leaves the building. No real patient has been through it
yet. The whole point of the next few months is the pilot that changes that.

**The eleven pieces described below fall into four groups.**

*Getting the ward's own information into Orb, and getting it out.* Staff sign in
on a shared trolley tablet with a short PIN instead of a full password, so every
line in the record carries a real name instead of whoever left themselves logged
in. Years of records from the hospital's old system can be brought across, with
a rehearsal first and a permanent list of every row that would not come. And a
patient can be handed everything Orb holds about them, with the automatic check
on that export finally proven to work rather than merely written.

*Making sure a warning reaches a person.* When a patient's observations look bad,
Orb now opens something stubborn that is addressed to a named human, climbs to
more senior staff if nobody answers, and never quietly goes away. A doctor can
also apply a whole treatment protocol, the sepsis bundle, for instance, in one
action, and every single order it produces goes through exactly the same allergy
and dose safety checks as one typed by hand.

*Working the way a ward actually works.* Ward round mode drives a consultant bed
by bed with a screen that fits the ninety seconds available at each bedside and
takes the plan by dictation. And Orb now keeps working when the wi-fi drops:
during a round the tablet keeps showing the nurse's own patients, always labelled
with exactly how old the information is, and it flatly refuses to accept anything
written while there is no signal rather than promising to send it later.

*Being able to answer for itself afterwards.* Orb keeps a full, replayable record
of every answer the assistant gave, including the patient's chart exactly as it
looked at that moment, so an incident meeting six months later has evidence
instead of recollection. And there is now the machinery India's privacy law
requires: a consent register that cannot be edited after the fact, a "show me
everything you hold" export, a written retention policy, and a breach register.
Because nobody at our end can dial into a hospital's machine, there is also one
page that tells the hospital's own IT person what is wrong with it and what to do
about it, including a backup check that refuses to call a backup good just
because a file with the right name exists.

**The four things to hold on to, because they are true across nearly all of it.**

1. **Most of this has no screen yet.** The escalation chain, the order sets, the
   privacy machinery, the assistant's audit record and the status page all work,
   but a nurse, a doctor or an IT person cannot see any of them on a page. They
   are reachable only by Orb talking to itself. That is the largest single block
   of work remaining, and it is not small.
2. **Orb cannot ring anybody.** No pager, no bleep, no phone call, no text, no
   notification. A deterioration that nobody answers waits patiently and
   silently inside Orb. On a staffed ward with Orb on a screen that is loud
   enough; at three in the morning it is not.
3. **When Orb has to choose, it chooses the visible failure.** It would rather
   refuse to save something and say so than accept it and quietly lose it. It
   would rather show a nurse nothing than show her a number from twenty minutes
   ago as if it were current. It would rather refuse a whole eight-item protocol
   than apply seven of the eight. Several things below are slower or more
   annoying than they could be, and that is the reason.
4. **None of it has met a real patient, a real ward, or a real export from a real
   old system.** Everything here has been tested hard against situations we
   invented. The first real one will contain something nobody thought of.

**How to read the rest of this document.** Every section ends with a list headed
"What it does NOT do yet". Those lists are the most useful part of this document
and they are deliberately blunt. If you only have ten minutes, read those.


---

### Signing in with a PIN on a shared ward tablet

**What it does.** A ward has one or two tablets that live on a trolley and get
passed between everyone on shift. Until now, using one meant typing your full
password, every time, in front of whoever was standing there. So an
administrator now sets up each tablet once, and after that a nurse or doctor
signs in with a short PIN of their own: staff ID, four digits, in.

**Why it matters in a hospital.** When signing in is slow, staff stop doing it.
One person logs in at the start of the shift and everybody else uses that
session. It is not laziness, it is what happens when the safe thing is also the
slow thing. But the moment a login is shared, the record stops being able to
answer the only question that matters after something goes wrong: who actually
gave that drug, who actually read that chart, who actually changed that order.
Every line just says "the nurse whose login was open". A PIN takes about three
seconds, so signing in as yourself becomes the easy option rather than the
conscientious one, and every line in the record gets a real name against it.

**The safeguards, and why each one is there.**

*The PIN only works on the ward's own tablet.* The tablet holds a separate
credential of its own that an administrator installed. Someone who watches a
nurse type her PIN and then goes home and tries it on their own laptop gets
nothing at all, the PIN on its own is not a way in.

*Guessing is throttled twice.* Four digits is not many. Five wrong tries locks
that person's PIN for fifteen minutes. Separately, fifteen wrong tries across
*any* staff on the same tablet locks the tablet for fifteen minutes, that is
the one that stops someone standing in a corridor working through the whole
staff list a few guesses at a time.

*A locked PIN never locks you out of the hospital record.* This is the part we
argued about most. Being shut out of the record in the middle of a shift is
itself a danger to patients, so a locked PIN only closes the PIN door: the
password still works on the same tablet, and the sign-in screen offers it in one
tap. An administrator can also clear the lock immediately, and the same single
"unlock this person" action clears both a locked password and a locked PIN, so
nobody is left half-unlocked at three in the morning.

*Wrong staff ID and wrong PIN look exactly the same.* Including how long they
take. If a wrong PIN on a real staff ID came back slower than a made-up ID,
anyone could sit with the tablet and work out the hospital's entire staff list
from the delay alone, without ever guessing a PIN. The check deliberately does
the same amount of work either way.

*The tablet signs itself out after two minutes of being left alone,* and returns
to a blank PIN screen rather than to the last person's session, so the next
person picking it up signs in as themselves. The server enforces a short session
too, so this cannot be defeated by fiddling with the tablet.

*A lost tablet can be switched off from the office.* An administrator revokes it,
and any session still open on it stops working at the very next tap, not
whenever it would have expired on its own.

*The tablet PIN is not the same as the medication co-signing PIN.* Deliberately.
The co-sign PIN is a second, independent confirmation before the system acts on a
medication instruction. If unlocking the tablet used the same digits, anybody who
happened to see an unlock could also countersign a drug order in that nurse's
name, and two separate safety checks would quietly become one.

**What it does NOT do yet.**

- There is no screen in the admin area for any of this yet. Enrolling a tablet,
  seeing the list of tablets, revoking one, and giving a member of staff their
  first PIN all work, but for now they are done by whoever installs the system
  rather than through a page an administrator can click around in.
- The sign-in screen does not show a list of staff names to tap. You type your
  staff ID. A name list would be faster, but a stolen tablet would then be
  handing over the ward's staff list to whoever took it.
- There are no rules yet about changing a PIN regularly, and no check that two
  people have not picked the same four digits. Very obvious PINs (1234, 0000,
  all-same-digit) are refused, and that is all.
- Staff on a role the hospital has put behind an authenticator app cannot use a
  PIN at all; they still sign in with a password and a code. This is on purpose,
  but it means an administrator on a ward tablet has the slow experience.
- The tablet's own credential does not expire on its own. It lasts until somebody
  revokes it, so a tablet that quietly disappears stays valid until it is noticed
  and switched off.
- When the tablet signs itself out after two minutes, it forgets the session on
  the tablet but does not tell the server to tear it down. The session still runs
  out on its own a few minutes later, and nothing on the tablet can reach it in
  the meantime, but there is a short window where it is technically still alive
  on the server. Ending it immediately would also end that person's session on
  any computer they are logged into elsewhere, which is why it is not done yet.

---

### Escalation chain: a deterioration that somebody has to answer for

**The problem, in one sentence.** When a nurse typed a worrying set of
observations into Orb, the system worked out, correctly, that the patient was
deteriorating, and then that warning reached nobody in particular.

There was a warning message posted into the patient's chat room, and that was
all. Nobody was named as responsible for it. Nothing recorded that a human being
had read it. Nothing at all happened if no human being did. On a busy ward that
message scrolls up the screen behind twenty others in a few minutes, and once it
has scrolled away there is no difference between a warning that was dealt with
and a warning that everybody missed.

This mattered more than it sounds, because typing observations in by hand is not
a backup that hospitals rarely use. It is the only way vital signs get into Orb
at all when the bedside monitors lose their network connection, or on a ward that
has no monitors. The one route with no chase-up was the route the ward falls back
on when things are already going wrong.

**What it does now.**

When a set of observations scores high enough on the standard early-warning scale
the hospital already uses, Orb opens what it calls an escalation. An escalation is
a small, stubborn thing. It has:

- a patient,
- a person or a role it is addressed to,
- a status that starts at "waiting for someone to say they have seen this",
- and a running record of everything that has happened to it.

It is addressed first to a ward nurse. If nobody says they have seen it within
ten minutes, it climbs to the duty registrar. If nobody says they have seen it
within fifteen minutes after that, it climbs again to the consultant. Each of
those steps is written down with the time and who it went to, so afterwards
anybody can read the whole story rather than guess at it.

One honest caveat about that climb, because it is easy to read the paragraph
above as more automatic than it is: Orb works out that a step is overdue and
records it the next time anybody opens the escalation list, or whenever the timed
instruction the installer can set up pokes it along. There is no clock running
inside Orb by itself. On a staffed ward somebody looks often enough to hide this;
overnight with nobody logged in, a step that was due at 03:10 may not be written
down until somebody opens the screen. That gap is listed again at the bottom of
this section, because it matters.

Somebody acknowledging it, a single tap that means "I have seen this and I am
dealing with it", records their name and the time, and stops the clock climbing.
It does **not** make the escalation go away. It stays on the ward's list until
somebody closes it and says, in their own words, what was actually done. A close
button with no reason attached would just be a dismiss button, and a dismiss
button is how a ward learns to clear alerts without reading them.

If nobody ever acknowledges it and nobody ever closes it, it stays visible.
Forever. Three days later it is still there, still saying nobody has answered it.
Nothing in Orb quietly retires it, because a warning that disappears on its own
is the exact thing that went wrong in the first place.

**Two more things it gets right, which are less obvious.**

*It does not nag.* If a patient's observations are taken again ten minutes later
and are still bad, that does not create a second escalation for the same patient
that somebody has to answer separately. The new reading is added to the one
that is already open. One patient who is deteriorating produces one thing to
answer, with a full history behind it, not a growing pile.

*It does not miss a spike.* If a patient scores badly and then, a minute later, a
better reading arrives, Orb still raises the escalation for the bad one. Looking
only at the most recent number would have quietly lost it, and that is the
dangerous direction to be wrong in.

The ward can change the steps and the timings. A twelve-bed high-dependency unit
and a forty-bed general ward do not want the same waiting times, and each site
sets its own. If somebody mistypes that setting, Orb logs a loud complaint and
falls back to the standard steps rather than stopping escalating altogether, a
typo in a settings file must never be the reason a deterioration goes unanswered.

**Who it goes to, and the honest limitation there.**

Orb knows who works in the hospital and which department they belong to, and it
knows which consultant is named as looking after each patient. That is all it
knows. It does **not** have a duty rota. Nothing in Orb says who is covering this
ward at three in the morning on a Sunday, and nothing records seniority, so Orb
cannot actually look up "the registrar".

So it makes the best guess it can from what it has, and it says out loud that it
is a guess. The nurse step goes to the nurse who took the observations if Orb
knows who that was, otherwise to a nurse on that ward. The registrar step goes to
a doctor on that ward who is not the named consultant. The consultant step goes to
the consultant named on the patient's admission. When it cannot name any
individual at all, it addresses the escalation to a role on a ward instead, "any
nurse on General Medicine", and it stays on the list of everybody who fits that
description. It never quietly addresses a warning to nobody.

**What it does NOT do yet. Read this part.**

- **It cannot ring anybody.** Orb has no pager, no bleep, no phone call, no text
  message and no phone notification. An escalation appears inside Orb and posts
  a message in the patient's chat room. If the ward has Orb open on a screen,
  that is loud. If it is three in the morning and nobody is looking at a screen,
  the escalation is faithfully waiting and completely silent. This is the single
  biggest gap and it is not a small piece of work to close.
- **The chase-up is not yet started automatically by the ward's own screens in
  every case.** Orb pushes the escalation along whenever somebody opens the
  escalation list, and there is an instruction the installer can schedule to
  push it along on a timer. But there is no always-running background clock
  inside Orb doing it by itself yet. In practice on a staffed ward somebody
  looks often enough; overnight on a quiet ward with nobody logged in, a step
  that was due at 03:10 may not be recorded until somebody opens the screen.
  That is a real gap and it is written down here rather than glossed over.
- **The bedside screens do not raise it at the moment of typing yet.** Orb has a
  safety net that catches a worrying set of observations shortly after it is
  saved, whichever part of the system saved it, this is what makes the feature
  work at all today, and it was built that way deliberately because "somebody
  will remember to wire up the next screen too" is exactly the assumption that
  caused the original problem. But a safety net runs a little behind. Connecting
  the observation screens directly, so the escalation opens the instant Save is
  pressed, is a small change and is the next thing to do.
- **There is no screen for any of this yet.** The escalation list, the
  acknowledge button and the close-with-a-reason box all exist and work, but they
  are currently reachable only by the system talking to itself. A nurse cannot
  see them on a page. That is the next piece of work.
- **A patient who is discharged with an escalation still open keeps it open.**
  Orb will not close it quietly on their behalf. That is the safer of the two
  mistakes, but it does mean somebody has to tidy it up.
- **Nobody is chased for the tidying up.** If an escalation is acknowledged and
  then forgotten without being closed, it sits on the list indefinitely. It is
  visible, which is the important part, but no report yet says "these four have
  been sitting acknowledged for two days".
- **It cannot tell a real deterioration from a bad reading.** A blood-pressure
  cuff on the wrong arm or a mistyped number raises exactly the same escalation
  as a genuinely sick patient. Somebody closing it can say "wrong reading" and
  that is recorded, but Orb does not know the difference by itself.

---

### Order sets: a whole protocol ordered in one action, with nothing switched off

**What it does.**

A doctor treating a patient with suspected sepsis has to order roughly eight
things quickly: a lactate, blood cultures, a full blood count, kidney bloods,
oxygen, an antibiotic, a bag of fluid, and hourly urine measurement. Typing
eight orders one at a time, at three in the morning, is slow and it is where
things get missed.

An order set is that whole list, written down once, applied in one action. Orb
now ships five: the sepsis bundle, first-hour treatment for a heart attack,
first-hour treatment for diabetic ketoacidosis, routine care after an
operation, and acute severe asthma. Each one names the national or
international guideline it came from, in full, so anybody can check where the
list came from and argue with it if they disagree.

Applying a set does not create one mysterious "sepsis bundle" entry. It creates
the eight real, separate orders, each one exactly as if it had been typed by
hand. Every one can be changed before it is applied, dropped from the list
entirely, or cancelled afterwards. They all arrive as drafts: the doctor still
has to confirm each one. Orb also records one plain statement in the permanent
log, "the sepsis bundle was applied to this patient, by this doctor, at this
time, citing this guideline, and here are the eight orders it produced", 
because after an incident somebody always needs to know whether eight decisions
were made or one.

**Why it matters in a hospital.**

Speed, and completeness. The sepsis bundle has a one-hour clock attached to it,
and the reason hospitals measure it is that parts of it get forgotten. A list
that can be applied in one action is a list that gets finished.

But the far more important thing here is what the set is *not* allowed to do.

The obvious danger with any "do eight things at once" button is that it quietly
becomes the way round the safety checks. If Orb stops a doctor who types
"ceftriaxone" by hand for a patient with a documented penicillin-type allergy,
but does not stop the same drug when the sepsis bundle types it, then the
button has switched the allergy alarm off, and within a month everybody is
using the button.

So the rule here is absolute and it is the thing that was tested hardest. Every
single order a set produces goes through exactly the same checks as a
hand-typed one, run at the moment the order is written:

- the allergy and drug-interaction check, using the same piece of software the
  ordinary ordering screen uses, not a copy of it;
- the dose guard, which stops a dose that is ten times too big, stops
  concentrated potassium being pushed into a vein rather than diluted and
  dripped in, and stops the vinca cancer drugs being given into the spine, 
  all of which are recognised, named, fatal mistakes;
- the check that the patient is actually still in the hospital;
- and the check that every order code is one this hospital recognises.

And if any single item in a set is stopped, **the whole set is refused and
nothing at all is written**. Not seven of eight. A half-applied protocol is
worse than a refused one, because the notes then show a bundle nobody finished
and nobody knows to finish.

If the block is a severe allergy, the doctor can override it, but only in the
way they always could: by typing a reason, which is permanently recorded
against their name. If the block is a dose that is ten times too large, or a
drug going down a route that is never correct, there is no override on this
path at all. That is deliberate. A dangerous number sitting inside a protocol
file is not one doctor's judgement about one patient; it is an instruction that
would go to every future patient who gets that protocol, and the fix is to
correct the file, not to click past it once per patient.

The protocols themselves live in a plain file that an engineer edits and that
gets reviewed and deployed like any other change. There is deliberately no
screen that lets a ward rewrite clinical protocol at three in the morning. And
if that file is broken, a missing comma, a mistyped field name, a dose the
dose guard would stop, Orb refuses to start and says which file and which
line. It will never start up with the protocol list quietly empty, because a
ward that opens the list and finds nothing there has no way to tell the
difference between "we have no protocols" and "ours vanished".

**What it does NOT do yet. Read this part.**

- **Nothing chooses the protocol for you.** Orb will not notice that a patient
  looks septic and offer the bundle. A person picks it. That is on purpose for
  now, but it does mean the time saving only arrives once somebody has already
  decided what is going on.
- **There is no screen for it yet.** The protocol list, the preview, and the
  apply action all exist and work, but only through Orb talking to itself. A
  doctor cannot yet see a button on a page. That is the next piece of work and
  it is not small.
- **The drug choices are starters, not this hospital's policy.** The antibiotic
  in the sepsis bundle in particular is the one a general ward would commonly
  reach for, and it must be replaced with whatever this hospital's own
  antibiotic policy says before anybody uses it on a patient. Each protocol
  also has a blank space for the name of the consultant who signed it off, and
  those blanks are still blank.
- **There is no per-patient cleverness inside a set.** It will not halve a dose
  because the kidneys are poor, or drop an item because of the patient's
  weight, or adjust anything for a child. Every one of the shipped protocols is
  written for an adult. The doctor changes or removes items by hand.
- **Orb does not run the clock.** The sepsis bundle has a one-hour target;
  applying the set does not start a timer, chase anybody, or report at the end
  of the hour whether the six things were done. Orb has separate machinery that
  does track a sepsis bundle's timing, and the two are not yet joined up.
- **A protocol does not schedule future doses.** "Every six hours" is written
  on the order for the ward to read. Orb does not turn it into a list of times.
- **The drug codes in the shipped protocols are Orb's own local codes**, not
  national ones. That is honest rather than ideal, inventing plausible-looking
  official drug codes for an offline hospital system is how a fake code ends up
  in a discharge letter. A hospital that has loaded a real drug dictionary
  should put its own codes in the file.
- **The safety check and the writing of the orders are a fraction of a second
  apart**, not one indivisible instant. If somebody recorded a brand new
  allergy in that gap, the order would still be written. It would be caught
  again before the drug was actually given, which is the moment that matters
  most, but the gap is real and is written down here rather than glossed over.
- **Changing the protocol file needs a restart.** There is no live reload. That
  is deliberate, the protocol somebody saw at the start of a shift should be
  the one they apply at the end of it, but it does mean an urgent correction
  needs somebody with access to the server.

---

### The privacy law machinery: consent, "show me my data", and the right to be forgotten

India's Digital Personal Data Protection Act says that if you hold information
about a person, you have to be able to prove four things: that they agreed, that
you can show them what you hold, that you only keep it as long as you should,
and that you tell them when something goes wrong. Orb could not do any of that
before this piece of work. It can now.

**What it does**

*Consent.* Orb keeps a register of what each patient agreed to, care and
treatment, the assistant drafting notes, sharing updates with a named relative,
research, a portal login. Each entry records what was agreed, when, how it was
taken (a signed form, spoken with a witness, tapped through on the portal, a
thumb impression at the desk), which version of the privacy notice the patient
was shown, and which language it was in. If a guardian signed for a child or for
someone who could not sign themselves, their name and relationship go in too.

A patient can change their mind. When they do, Orb writes a **new** entry saying
so. It never goes back and edits the original. This matters more than it sounds:
if the original could be altered, then a year later nobody, not the hospital,
not the patient, not an inspector, could tell whether the consent on file was
the one actually given. The database itself refuses to change or remove a
consent entry, so this is not a promise the software makes, it is something the
software cannot break.

*"Show me everything you hold about me."* An administrator can produce a single
document listing everything Orb holds about one patient: registration details,
admissions, notes, medicines, allergies, observations, problems, images,
appointments, surgery records, the assistant's working notes, the consents they
gave, and a list of every time a member of staff opened their record, with who
and when. It also states plainly what it has left out and why, rather than
quietly dropping things.

The export builds itself from the database as it actually is on the day it runs,
rather than from a list somebody wrote once. So when a new feature starts storing
something about patients, it appears in the export automatically instead of
being forgotten. And there is a hard rule underneath it: if Orb cannot prove from
the structure of a table that every row it would hand over belongs to this one
patient and nobody else, it leaves that table out and says so. Handing one
patient a fragment of another's record while claiming to protect privacy would be
the worst possible way to fail at this.

*Retention.* Orb has a written policy for each kind of information: how long it
is kept, why, and which rule says so, three years for inpatient records under
the Medical Council's regulations, three years for the medicines register under
the drug rules, and so on. Anyone with the right access can see a list of what is
now past its date. **Orb does not delete any of it.** It shows the list to a
person. A background job silently destroying medical records because a counter
ran out would be the most dangerous thing this system could do.

*The right to be forgotten, and where it stops.* This is the hard part, and it is
worth being blunt about it. The law gives a patient the right to have their data
erased. Other Indian law tells hospitals they must keep medical records. Orb's
own tamper-proof record of who opened which chart is built as a chain, where
removing one link breaks every link after it. All three cannot be obeyed at once.

Orb's answer is that the **reason** the information is held decides the outcome,
not the kind of information it is. Anything Orb keeps because a law requires it, 
the medical record, the medicines register, the images, the access log, the
consent register itself, is refused. Anything Orb keeps only because the patient
agreed to it, the assistant's working notes, an old dictation recording, a
portal login, a mailing list, is erased.

A refusal is never silence. Every refusal comes back with the reason and the
actual rule it rests on, so a patient who disagrees has something specific to
take to the regulator. Deciding and deleting are two separate actions performed
by a person, minutes or days apart, so nobody destroys anything by accident.

*Breaches.* There is a register for the day something goes wrong: what was
exposed, to whom, when it happened, when it was noticed, what was done to stop
it, and a separate dated entry for each time somebody was told, the regulator
and the affected patients being two different obligations. The Trust Center now
shows a red mark if a breach has been recorded but the regulator has not yet been
notified.

*Everything above respects who you are.* An administrator whose account covers
one ward sees only that ward's patients in these lists, the export, the erasure
register, the names on a breach report. When a breach affected patients outside
their ward, they are told how many, but not who. That is deliberate: someone has
to be able to see that an incident is bigger than their corner of the hospital
and escalate it, without being handed a list of strangers' names to do so.

**Why it matters in a hospital**

Two reasons, and only one of them is about the law. The obvious one is that a
hospital that cannot produce a consent record or answer a "what do you hold about
me" request is exposed, and the fines under this Act are large. The less obvious
one is that this is the conversation a hospital's IT director has before signing
anything. "Can you show me your consent register and your breach process" is a
question every buyer asks, and until now the honest answer was no.

**What it does NOT do yet. Read this part.**

- **There is no screen for any of it.** Everything described above works, but
  only through Orb talking to itself. Nobody can tap a consent form on a tablet
  at the bedside, and nobody can press a button to print a patient's data. That
  is the next piece of work and it is the larger half.
- **Nothing asks for consent at the right moment.** Orb will happily draft a note
  with the assistant for a patient who never agreed to it. The register records
  consent; it does not yet stand in the way of anything. Joining the two up is a
  decision about how much friction a ward will tolerate, and it has not been made.
- **Two of the erasable categories cannot actually be erased yet.** Old dictation
  recordings sit as files on the machine, and Orb does not delete files, a
  person with server access has to do it. Orb says so plainly in its answer to
  the patient rather than claiming the job is done.
- **The retention periods are a careful reading, not legal advice.** They were
  set from the national rules and deliberately kept in a table the hospital can
  edit, precisely because a hospital's own lawyer may read them differently. They
  should be reviewed before go-live.
- **Asking to be forgotten leaves a permanent mark.** The request, the decision
  and anything actually deleted are all written into the record of who did what.
  That is unavoidable if the hospital is to prove it handled the request
  properly, but it does mean the request itself can never be erased. Orb tells
  the patient this in the answer rather than letting them find out later.
- **A few tables are left out of the export on purpose.** Where a table could
  hold a reference to a second patient, the check that spots a message posted in
  the wrong patient's chat, for example, Orb refuses to include it rather than
  risk showing one person another's details. Those are named in the export with
  the reason, and somebody may have to look them up by hand.
- **Only doctors can record a consent.** Not nurses, not administrators. Note
  that the list above describes how a consent was *taken*, a signed form,
  spoken with a witness, tapped through on the portal, a thumb impression at the
  desk, but whichever of those happened, the person who then puts it into Orb
  has to be a doctor. In a real hospital the treatment consent is usually taken
  at the registration desk by somebody who is neither a doctor nor an
  administrator, and Orb has no such role yet. Widening this properly means
  adding a permission rather than loosening an existing one, because the
  permission that currently allows consent capture also allows editing the
  patient's record.
- **Nobody has rehearsed the breach process.** The register works. It has never
  been used in anger, and a form nobody has ever filled in under pressure is not
  the same as a process.

---

### Keeping a replayable record of every answer the assistant gives

**What it does.**

Until now, when Orb's clinical assistant answered a question, what survived was
the answer and a short summary of how it went. If somebody came back six months
later and asked "why did it say that?", there was no honest way to find out.

The reason is worth being precise about, because it is not obvious. The
assistant's answer depends on the patient's chart at that exact moment, the
observations, the drugs, the last blood test, the last scan report. Charts
change every hour. So a record that simply notes "this answer was about Mr
Kumar" is not a record at all: read it in six months and it fetches today's
chart, which is not what the assistant saw. It would produce a confident,
detailed, wrong account of what happened.

So Orb now keeps a copy of the inputs, not a pointer to them. For every single
turn, whether it answered, refused, or failed, it stores:

- the question, word for word;
- who asked, what their job is, and which ward they were allowed to see
  patients from at that moment;
- the patient's chart exactly as it was, the observations, the risk score and
  what drove it, the drugs, the recent bloods, the scan report, the last signed
  note, frozen, so a change tomorrow cannot rewrite yesterday;
- the guideline passages that were pulled up to answer with, in full, with the
  document numbers;
- which of the two models answered, whether the doctor had the "think harder"
  switch on or off, and whether the safety rule overrode that switch and sent
  the question to the careful model anyway;
- the answer as the clinician saw it, and separately the raw text the model
  produced first, because the two are not always the same, and when the safety
  filter has rewritten something, that is exactly the thing a review needs to
  see;
- and a fingerprint of the complete text that was handed to the model, so a
  reviewer can prove the stored pieces really are the pieces that were used.

An administrator with audit rights can ask for any single turn back and get the
whole thing reconstructed. Nobody else can: a doctor cannot read back another
doctor's conversations, and an administrator who is tied to one ward cannot open
a turn about a patient from another ward, Orb answers "not found", exactly as
if the turn did not exist, because saying "you are not allowed" would itself
confirm that the patient is on the system.

**What it costs, because that was the hard part.**

Storing a whole chart on every question is the obvious way to do this and it is
the wrong one. A ward conversation is usually five or six questions about the
same patient in a row, and the chart does not change between them. Storing it
six times is six times the disk.

So Orb stores each piece of a turn under a fingerprint of its own contents. Ask
a second question about the same patient and the chart is already on file, so it
costs nothing to reference it again. The same is true of the guideline passages,
which come from a small library and repeat constantly. Everything is compressed
as well, and anything absurdly large, somebody pasting an entire discharge
letter into the box, is cut off at a fixed size, and the record says so rather
than pretending it is complete.

Measured over 400 realistic turns across twelve patients: **about 3 KB per
question**. At 200 questions a day that is roughly 600 KB a day, 18 MB a month,
**213 MB a year**. Stored the naive way it would have been two and a half times
that, and that comparison is against a design that was already compressing;
against genuinely storing everything raw it would be several times worse again.
There is a page an administrator can open that reports these numbers measured
from the real records rather than estimated, so nobody has to take this
paragraph on trust.

There is a clear-out that throws away turns older than 400 days, long enough to
cover a full year of review with room to spare, and once it is running on a
timer the store will settle at around 230 MB and stop growing. It is **not**
running on a timer yet; somebody has to trigger it. So until that is scheduled,
treat 213 MB a year as a figure that keeps adding up rather than one that levels
off. This is repeated in the list at the end of this section because it is the
sort of thing that is only noticed when a disk fills.

**Why it matters in a hospital.**

Two reasons.

The first is that a hospital will eventually have an incident involving this
assistant. Somebody will say "it told me the wrong dose" or "it missed the
allergy", and there will be a meeting. Without this, that meeting has no
evidence in it, only recollection, and a chart that has since moved on. With
it, the meeting can look at exactly what the assistant was shown and exactly
what it said, and reach a conclusion. That is the difference between a fixable
problem and an unanswerable one.

The second is regulatory. If Orb's assistant is ever taken down the medical
device route in India, the regulator's central demand after approval is that the
manufacturer keeps watching the product in the field and can produce evidence
about individual cases. This is that evidence. It was built now rather than
later because retro-fitting it means the first year of the pilot is
unreviewable.

**What it does NOT do yet. Read this part.**

- **The screen is for administrators only, and it is plain.** Since this was
  first written, the admin area has gained a Sage Provenance page that lists
  turns and lays one out readably (who asked, what the assistant was shown,
  what it said). It has no search or filter yet; it shows the latest 100 turns.
- **Spoken questions are now recorded like typed ones (updated 3 October
  2026).** A spoken turn stores the chart the voice assistant was given, the
  guideline passages it retrieved and a fingerprint of its instructions,
  alongside the question, the answer and which model ran. A spoken turn that
  had no chart in front of it still stores none rather than a made-up one.
- **Old turns are thrown away on a timer.** The 400-day cleanup now runs once a
  day by itself; nobody has to remember to trigger it.
- **The answer text is stored more than once.** Orb keeps the raw model output
  and the final answer separately, and for most turns these overlap heavily.
  That is deliberate for now, the difference between them is the safety
  filter's work and losing it would be worse than the disk, but it is the
  obvious next saving.
- **It does not prove the answer was correct.** It proves what the assistant was
  shown and what it said. Whether the answer was right is a clinical judgement,
  and this makes that judgement possible rather than making it for anybody.
- **The record is one hospital's, on one machine.** Nothing is sent anywhere.
  That is the point of the design, but it also means if the machine is lost, the
  record is lost with it, it depends entirely on the backups holding.
- **It is part of the numbered upgrade sequence now (step 44, 3 October
  2026).** The tables are created when Orb starts, so an administrator
  inspecting a brand-new machine finds them before the assistant has been used.
  They are not yet in the list of tables the backup restore check counts.

---

### Bringing the old hospital records across

Orb is meant to replace the hospital's existing computer system, not sit next to
it. That means on the day the hospital switches over, years of records that live
in the old system have to come across: the patients themselves, their past
admissions, their diagnoses, the drugs they were on, their test results, and
their old notes and reports. This is the machinery that moves them.

**What it does**

An administrator exports a file from the old system, a spreadsheet-style file,
which is the one format every old system can produce, and uploads it. Orb does
not assume it knows what the columns mean, because nobody can know that until
they see the file. Instead it looks at the column headings, suggests what each
one probably is, and asks the administrator to confirm before anything happens.

Then, always, comes a rehearsal. Orb reads the whole file and reports exactly
what it would create, what it would leave alone, and, the important part, 
every single row it could not use and the precise reason why. Nothing is written
during the rehearsal. Only when the administrator has read that report and
pressed the button does anything actually get saved.

**Why this matters in a hospital**

The nightmare with a job like this is not a migration that fails loudly. It is
one that reports success while quietly dropping four percent of the records. If
that happens, nobody finds out on the day. They find out months later, when a
doctor goes looking for a test result that should be there and simply is not,
and by then nobody remembers which file it came from or what went wrong. So the
count of failed rows is always exact, never rounded or shortened, and every
failed row is written down permanently with the line number in the original file
so somebody can go and look at it.

The second nightmare is the opposite: records coming across too enthusiastically
and pretending to be current. Three things in particular would have been
dangerous, and Orb is deliberately careful about each one.

- **Old patients must not appear to be in the building.** If five years of
  patients arrived marked as admitted, the ward screens the nurses actually use
  would fill up with hundreds of people who went home years ago, and the real
  patients would be lost among them. Imported patients are marked as discharged
  and are not placed on any ward.
- **Old medicines must not appear to be current prescriptions.** Orb checks a
  patient's current medicines for dangerous combinations, reads them out in
  handovers, and puts them on discharge letters. A drug the patient stopped
  taking in 2023 arriving as a current medicine would be checked, displayed and
  handed over as though the patient were still on it. Imported medicines are
  marked as stopped unless the old system explicitly says otherwise.
- **Old diagnoses must not be presented as today's problems.** If the exported
  file does not say whether a diagnosis is still active, Orb files it as
  historical rather than asserting something about the patient's health today
  that the old system never actually claimed.

In all three cases, if the old system does say what it means, Orb believes it.
The cautious choice is only ever the answer to silence, and any record that took
it says so on its own report.

**Telling old records from new ones**

Every record brought across is permanently marked with which system it came
from, that system's own reference number for it, and which import brought it in.
This matters clinically, not just administratively. An allergy typed in by the
hospital's own pharmacist and an allergy scraped out of a free-text box in a
twenty-year-old system are not equally trustworthy, and the person reading the
chart deserves to know which one they are looking at.

The reverse is protected just as carefully. If a patient is already in Orb and
an old file mentions them too, Orb does not relabel that patient as imported,
and it never overwrites information Orb already holds. It only fills in blanks.
The live record is more recent than any old export, and an import that quietly
undoes a correction a doctor made last week would be worse than one that
refuses to run.

**Messy files are expected, not treated as an accident**

Real exports from real hospitals are untidy, and the file is handled on that
assumption. Dates written three different ways in the same column are all read
correctly. Files saved in an older Windows text format, which is what a lot of
this software produces, are detected so that names with accents survive intact.
Title lines and blank rows above the real column headings are skipped. Names
with "Mr." stuck to the front are flagged for a human rather than edited, since
a patient's name is what staff check at the bedside.

What Orb will not do is guess at anything clinical. A date written 03/04/1990
could be March or April, so it is left blank and reported instead of being
picked. A lab result recorded as "less than 0.01" is kept as those words and is
not turned into the number 0.01, because it is not that number. A result of
"NEGATIVE" is not turned into zero. If a medicine has no dose recorded, Orb
writes the word UNKNOWN rather than inventing a plausible one, but it still
keeps the medicine, because the fact that the patient took that drug at all is
exactly what allergy history is for.

If a row's columns have slipped out of line, usually because an address
contains a comma that was not quoted properly, the whole row is refused. Once
the columns have shifted, every value after the break is in the wrong field, and
a patient can end up filed under a ward that does not exist.

**Running it more than once is safe**

Each record carries the old system's own reference number, so running the same
file twice changes nothing the second time. The database itself enforces this,
not just the program, so two administrators pressing the button at the same
moment cannot produce two copies. A large export can be split into several files
and loaded in any order.

The whole import is all-or-nothing. If anything goes wrong partway through,
nothing at all is saved. A half-finished migration is the worst possible
outcome, so it is not a state Orb can be left in.

Only administrators can do any of this, and every step is written into the
permanent record of who did what.

**What it does NOT do yet**

- **It only reads spreadsheet-style files.** If the old system can only produce
  a PDF, a printout, or its own database backup, somebody has to convert it
  first. Orb cannot read those.
- **It does not merge duplicate patients.** Old systems very often hold the same
  person twice under two different numbers. Orb spots this and reports it, but
  it will not decide that two records are the same human, that is a judgement
  with real consequences and it stays with a person.
- **There is no undo.** Every import keeps a full record of what it did, so it
  can be traced and unpicked by hand, but there is no button that reverses one.
- **Very large files must be split.** The limit is two hundred thousand rows per
  file. Beyond that the administrator has to split the export, typically by
  year. Because re-running is safe, this is inconvenient rather than risky.
- **The uploaded file sits briefly on the server's disk unencrypted.** It is
  written to a private folder next to the database, is not readable by other
  users of the machine, and is deleted as soon as the import finishes, whether
  it succeeded or failed. But for the moments it exists it is not protected the
  way the database itself is, and if the server were to lose power in the middle
  of an import the leftover file would stay there until somebody removed it.
  There is no automatic tidy-up of those leftovers yet.
- **Rejected rows that could not be matched to any patient are not covered by
  the right-to-be-forgotten process.** If a patient later asks to be erased, Orb
  finds and removes rejected rows that were successfully linked to them. A row
  that failed precisely because Orb could not work out who it belonged to cannot
  be found that way, and would have to be cleared out by hand.
- **The discharge date on imported admissions is stored in a place Orb does not
  otherwise use.** Orb records its own discharges elsewhere, so this field is
  filled in for imported stays and empty for everything else. It must not be
  used to work out whether somebody has been discharged.
- **Nobody has run this against a real hospital's export yet.** It has been
  tested hard against files built to be awkward on purpose. The first real
  export will contain something nobody thought of; that is the nature of the
  job, which is why the rehearsal step exists and why the list of refused rows
  is kept in full.

---

### Repairing the safety check on the "show me my data" export

**What it does.**

The export that hands a patient everything Orb holds about them has a protection
built into it. Orb also stores the hospital's medical reference library in a
special kind of search table that only certain parts of the system know how to
read. If the export walked into one of those tables unprepared it would stop
dead, and the patient would get an error instead of their record. So the export
recognises those tables, steps around them, and says in its own output that it
left them out and why. That protection was already there and it works.

What was wrong was the automatic check meant to prove it works. The check has to
set up the awkward situation on purpose: build one of these special tables, then
run the export from somewhere that cannot read it. It was doing both halves in
the same place, which is a bit like testing whether a stranger can open a locked
door by trying the handle yourself while holding the key. The setup step failed,
and because it failed the check never got as far as the export at all. So the
protection had never actually been exercised, even though somebody had gone to
the trouble of writing a check for it.

The check now does the two halves properly apart from each other, and it has
been confirmed to fail if the protection is removed, which is the only way to
know a check is really watching something. A second check was added for the
related case where the export meets a table it cannot read for some other
reason: one unreadable table must never cost the patient the rest of their
record, and the omission must be declared rather than passed over quietly.

**Why it matters in a hospital**

The whole point of this export is that a patient can ask what is held about them
and get a complete, honest answer. An export that fails halfway on the machines
that have the reference library loaded, which is every real one, would turn a
legal obligation into an error message. The protection against that was sound.
The proof that it was sound was not, and on a system heading for real patients
an untested safeguard should be treated as an unknown one until somebody checks.

**What it does NOT do yet.**

- **This changed no behaviour that a patient or a nurse would notice.** It was
  a repair to the testing, not to the export. Nothing new is exported and
  nothing new is withheld.
- **It does not prove the export is complete.** It proves the export survives
  tables it cannot read and admits to skipping them. Whether everything a
  hospital ought to hand over is actually in there is a separate question, and
  the honest answer is still that it has never been run against a real patient's
  full record with a records officer reading the output.

---

### Ward round mode: walking the ward with a tablet in one hand

A consultant's ward round is the busiest hour of the hospital day. The doctor
walks the corridor bed by bed with a tablet held in one hand, often gloved, and
has somewhere around a minute and a half at each patient before the round has to
move on. Ward round mode is Orb's attempt to fit into that minute and a half
instead of fighting it.

**What it does**

Orb drives the round rather than handing over a list to scroll. It works out the
order of the beds the way a person actually walks them, bed nine before bed ten,
not after it, which is the order a computer would choose if nobody stopped it, 
and it opens the round on the first bed. Each patient is marked seen, or skipped,
and Orb moves the doctor to the next bed automatically.

Skipping is treated as a real thing, not as an escape hatch. The patient who is
away in the X-ray department is skipped, the round carries on to people who are
in their beds, and Orb comes back to the skipped ones at the end rather than
letting them quietly become the patients nobody saw. All the way through, a strip
down the side shows how many beds are done, how many are left, and which ones
were deferred.

Each patient gets one screen, and it does not scroll and has no tabs. It answers
the three questions a ward round actually asks, side by side: what changed
overnight, what is wrong now, and what has to be decided today. "Overnight" is
not left vague, the screen names the exact moment it is counting from, which is
the previous evening's handover. Allergies are always on the screen.

The plan is dictated. The record button is the largest thing on the screen, it is
press once to start and press again to stop rather than something that has to be
held down, and the speech is turned into text on the hospital's own machine. The
text lands in a box the doctor edits before saving, because dictation is a draft
and never a signature. Typing works exactly as well for anyone who prefers it,
and the whole round can be driven from a keyboard.

Rounds get interrupted constantly, a crash call, a phone call, a relative in the
corridor. So the round survives. If the tablet locks or the battery dies, picking
it up again returns the doctor to the bed they were standing at, with everything
already marked still marked. At the end, Orb produces the list of who was not
finished and whether each one was deliberately deferred or simply never reached.

**Why this matters in a hospital**

Two things go wrong on ward rounds, and both of them are about time.

The first is that the round runs out of time before it runs out of patients, and
the people at the end of the corridor get a worse round than the people at the
start, or get missed altogether, with nobody quite able to say afterwards who
was seen and who was not. Orb keeping the queue is what makes that answerable.

The second is that the decisions made at the bed never make it into the record,
because writing them up takes longer than making them. Later in the day a nurse,
a pharmacist or the night team needs to know what the plan was, and the honest
answer is that it is in somebody's head. Dictating at the bed is the only version
of this that fits in the time available.

There is also a quieter thing this screen does. A blank space on a medical screen
is dangerous, because "there is nothing to report" and "we could not find out"
look identical when both of them show nothing. Ward round mode never lets those
two look alike. Something still loading looks like it is loading. Something
genuinely empty says so and says plainly that nothing failed. Something that
failed to load says it failed, shows the actual reason, offers a retry, and says
in as many words that this is not an all-clear and the chart should be checked
directly. If the allergy information cannot be reached, the screen says the
allergies are unknown, it never shows an empty space, because an empty space
reads as "no allergies" to somebody about to prescribe.

The ward tablet is shared, so the round is also careful about what it leaves
behind. When a doctor opens the round, any other doctor's round is erased from
that tablet, not merely ignored, which patients a named doctor visited is itself
private. And the dictated text is never written to the tablet's storage at all.

**What it does NOT do yet**

- **A dictated plan that is not saved is lost if the page reloads.** This is
  deliberate, free clinical text about a named patient does not belong sitting
  on a shared ward tablet's storage, but it is a real cost, not a free choice.
  Orb refuses to let a patient be marked seen while their plan is still unsaved,
  and the end-of-round screen lists any unsaved plans, but if the browser is
  closed first the text is gone.
- **The round order assumes bed numbers follow the corridor.** On most wards they
  do. On a ward where they do not, the walk order will be wrong, and there is
  currently no way for the ward to correct it or set its own route.
- **Saving the plan writes a note, and nothing else.** If the plan is to stop an
  antibiotic, request a scan or change a dose, that still has to be done
  separately elsewhere in Orb. Nothing said at the bed turns into an order by
  itself.
- **The hand-on list is shown, not sent.** At the end of the round Orb displays
  who still needs seeing, but it does not deliver that list to a named colleague
  or attach it to the formal handover. Somebody still has to carry it.
- **The round lives on one tablet.** Progress is remembered on the device it was
  started on. A doctor who puts one tablet down and picks up another starts the
  round again from the beginning, and two doctors rounding on the same ward
  cannot see each other's progress.
- **Orb cannot tell when the dictation misheard something.** Drug names, doses
  and numbers are exactly what speech-to-text is worst at, and a wrong number
  reads as fluently as a right one. This is why the text is always shown for
  editing before saving, and why it is never saved automatically. Reading it back
  is the doctor's job and Orb cannot do it for them.
- **A busy night is summarised, not shown in full.** The overnight column shows
  the most significant handful of events and then says exactly how many more
  there were. It never hides the remainder silently, but seeing them means
  opening the full chart, which is the tab-switch this screen otherwise avoids.
- **The plan cannot be saved at all while the network is down.** Orb refuses,
  says plainly that nothing was saved, and leaves the text in the box so it can
  be saved the moment the connection returns. It deliberately does not promise to
  send it later: anything held on the tablet is wiped when the doctor signs out,
  and a ward round ends in a sign-out, so a promise to send it later would be a
  promise Orb breaks silently. But it does mean a round done in a wi-fi blackspot
  cannot be written up until the doctor walks somewhere with a signal.
- **No doctor has used this on a real ward round yet.** It has been built against
  a stopwatch and tested hard, but the ninety seconds is an assumption until a
  real consultant walks a real corridor with it. The first round will show
  something nobody predicted.

---

### Keeping working when the wi-fi drops mid-round

Hospital wi-fi is not evenly good. There are corridors, stairwells, lift lobbies
and the far end of a bay where a tablet simply has no signal, and a nurse walking
her round passes through them constantly. Until now, walking into one of those
spots meant Orb stopped being useful: the screen she was relying on emptied out,
or worse, quietly sat there showing numbers she had no way of knowing were old.

**What it does**

Orb now keeps a copy of the things a nurse needs during a round on the tablet
itself: her patient list, each patient's latest early-warning score, when their
next observations are due, and the whole drug chart for every patient on her bay.
When the signal goes, the screen does not empty. She can still see who is on her
bay, who is deteriorating, which doses are late and what the patient is allergic
to.

The important half of this is not the copy. It is that Orb tells her, in
unmissable words, that it is a copy. A red panel appears across the top of the
screen saying the device is offline, giving the exact clock time the information
was last true, and how old that makes it, "saved at 6:06 PM, eighteen minutes
old", followed by the plain statement that anything that changed since then is
not on this screen. The same panel appears inside the drug round and the
observations screen, not only on the list behind them, because on a tablet those
open full-screen and cover everything underneath. A nurse standing at a bedside
looking at a drug chart is looking at nothing else, so that screen has to speak
for itself.

Writing is refused outright while there is no signal, and this is deliberate. If
she tries to record observations, chart a dose, save a note or file a handover,
Orb stops her before anything is sent and says: this device is offline, your
entry has not been saved, reconnect and enter it again. It never says it will
send them later, because it will not. The numbers she typed are kept on the
screen so she does not have to remember them, but nothing is filed and Orb does
not pretend otherwise.

The moment the signal returns, Orb fetches everything fresh by itself and the red
panel disappears. She does not have to notice, or tap anything, or work out
whether what she is looking at has caught up.

And when she signs out, every saved copy on the tablet is destroyed. So is the
copy belonging to whoever was signed in before, if a different nurse picks the
tablet up, and so is anything left behind by a tablet whose battery died
mid-round.

**Why it matters in a hospital**

Two things could go wrong here, and they are not equally bad.

The first is that Orb shows nothing in a dead zone. That is annoying and it sends
a nurse walking back to the station to look something up.

The second is that Orb shows an early-warning score from twenty minutes ago as
though it were the current one. She reads it, decides the patient is stable, and
walks on. That is not annoying, that is a patient deteriorating unwatched, and it
is worse than showing her nothing at all, because a blank screen sends her to
find a connection, and a confidently wrong one sends her to the next bed. Every
choice in this feature is bent towards making the second thing impossible, even
where that makes the first thing more likely.

Refusing to write is the same reasoning pointed at the other direction. If a
nurse believes she charted a dose, she will not chart it again, and if that
belief was wrong there is now a dose that was given and never recorded, invisible
to the next nurse, to the doctor, and to the pharmacist. A refusal she can see is
recoverable. A confirmation she should not have trusted is not.

The sign-out rule is about a different risk. The tablet this is built for lives on
a trolley and passes between six nurses in a shift. A saved copy of a bay of named
patients, with their diagnoses and their drug charts, sitting on a device that has
changed hands is a straightforward breach of those patients' privacy, no
technical failure required, just the next person picking it up.

**What it does NOT do yet.**

- **Only two screens are saved: the nurse's shift board and a patient's chart.**
  Messages, the assistant, orders, the ward round and the hospital-wide summary
  are not saved and will show nothing useful without a connection.
- **The saved copy on the tablet is not scrambled.** The hospital's real records
  are encrypted where they are stored; this copy is protected differently, by
  being destroyed at sign-out and by refusing to open once it is more than twelve
  hours old. A tablet that is stolen while a nurse is still signed in is not
  covered by either of those. Locking the device itself is still the hospital's
  job.
- **Orb can only tell that the tablet has lost its network.** It cannot directly
  tell that the ward's own Orb machine has stopped answering while the wi-fi is
  perfectly fine. When a screen fails to load it does say "can't reach Orb"
  instead of "you are offline", which are different problems with different
  fixes, but that depends on the screen noticing its own failure, not on Orb
  knowing.
- **The clock on the screen is the tablet's own clock.** If someone has set the
  tablet's time wrongly, the age reading is wrong too. Orb refuses a copy that
  claims to come from the future, which catches the obvious cases and not the
  subtle ones.
- **A saved drug chart cannot know what a colleague did.** It shows the round as
  it stood when it was saved. If another nurse gave a dose in the meantime, this
  screen will not know, which is exactly why it states its own age rather than
  presenting itself as the truth.
- **There is a limit on how much is kept**, and when it is reached the oldest
  saved screens are dropped first. On a very large ward that limit could be
  reached, and the honest effect is that some patients' details would not be
  available offline rather than that anything is shown wrongly.
- **No nurse has used this in a real dead zone yet.** It has been driven through
  every failure on a desk, including handing the tablet between two sign-ins, but
  the actual pattern of a ward's blackspots, how long they last, how often, in
  which corridor, is unknown until the pilot.

---

### The two screens that still promised to send things later

**What it does**

Two screens were left over from before the rule above: the ward round, and
messages.

If a doctor typed a plan for a patient with no signal, Orb took it and showed
"held on this tablet and sent when it reconnects". If a nurse sent a message in a
blackspot, it simply appeared in the conversation looking exactly like every
message that had really been sent. Both now refuse, in the same words every other
screen uses: this device is offline, your entry has not been saved, reconnect and
enter it again. The typed words stay on the screen so nothing has to be
remembered, and nothing at all is held back to send later.

**Why it matters in a hospital**

The promise to send it later was not being kept, and the way it failed was
silent.

Anything Orb holds back is thrown away the moment the person signs out. On the
shared ward tablet this is built for, signing out is not a deliberate act someone
chooses at the end of a shift, the tablet signs itself out after two minutes of
being put down, so the next person to pick it up cannot inherit somebody else's
session. That is the right thing for privacy, and it means a message or a plan
held back in a corridor has a life expectancy of about two minutes. The blackspot
it was waiting out usually lasts longer than that. So the thing being held back
was, in practice, usually destroyed, and nobody was ever told, not the person
who wrote it, who was no longer signed in to be told, and not the person it was
addressed to, who never knew it existed.

That produces the worst outcome this whole feature exists to prevent. A nurse who
watches "bed 4 deteriorating, please review" appear in the conversation believes a
doctor has been called. She does not call again by another route. A doctor who
sees a plan accepted believes the next shift will read it. Neither of them has any
reason to check, and there is nothing afterwards to show the message or the plan
ever existed. A refusal they can see while they are still standing there is
recoverable, walk to the station, use the phone, write it again in thirty
seconds. A confirmation that was never true is not recoverable at all.

It also means every screen in Orb now behaves the same way without a signal.
Before this, the nurse's screens refused and the doctor's round accepted, which is
the kind of split that gets learned wrongly by whoever works both ways.

**What it does NOT do yet.**

- **Refusing means the words have to be typed again later.** Orb keeps them on
  the screen in front of the person, but if they close the screen or the tablet
  signs itself out, the text is gone and there is no draft to come back to. This
  is the honest cost of not making a promise Orb cannot keep, and it is a real
  cost, a long plan dictated at a bedside is worth several minutes.
- **A safe way to hold work back has not been designed.** One could exist: work
  stored against the person who wrote it, surviving a sign-out, shown to them the
  next time they sign in, with anything undelivered stated plainly on screen
  rather than assumed sent. That is a larger piece of work than this was, and
  until it exists refusing is the safer of the two available answers.
- **Nothing warns anyone if work is discarded at sign-out.** There should now be
  nothing left to discard on these screens, but the mechanism that throws work
  away at sign-out still exists for other purposes and still does it silently.
- **The refusal depends on the tablet knowing it has no network.** A tablet with
  perfectly good wi-fi, where the ward's own Orb machine is not answering, does
  not count as offline. In that case the send is attempted and fails with an
  ordinary error, which is visible but is not this clear wording.

---

### The status page: what hospital IT reads when Orb misbehaves

**What it does**

Orb is sold as a box that sits in the hospital, and nobody at our end can dial
into it. That is a deliberate decision and a good one, but it has a consequence
somebody has to pay for: when something goes wrong at eleven at night, the person
who has to work out what, and what to do about it, is the hospital's own IT
person. Not us.

So there is now one page that answers that. It runs eight checks and writes each
one as a sentence:

- Is Orb itself running, and for how long?
- Is the patient record healthy?
- Did last night's backup happen, and was it checked?
- Is the medical assistant's brain loaded and answering?
- Are the bedside monitors still sending readings?
- Is there room left on the disk?
- Are this hospital's own settings loaded and sensible?
- Can people still sign in, and is there more than one administrator?

Each check comes back green, amber or red, and the page's headline is the worst
of the eight, so somebody glancing at it from across a room gets the true
answer, not an average that hides one broken thing behind seven working ones.
Anything that is not green also says what to do about it. "Backups are failing"
is not useful on its own at eleven at night.

There are two ways to read it: as a page inside Orb, and as plain text. The plain
text version exists because the moment somebody needs this most is often the
moment the browser, or the screen, or the network is itself the broken thing.
Text survives all of that, and it can be pasted into a message without turning
into an attachment nobody can open.

**Why it matters in a hospital**

Two things, and the second is the real one.

The first is simply that a hospital with no vendor to ring needs to be able to
see inside its own machine. Nothing here is new information, Orb always knew all
of it, but it was scattered across places only an engineer could reach.

The second is the backup check, and it is worth being blunt about why it is
written the way it is. Earlier in this project, fourteen nights in a row, Orb
wrote empty backup files, deleted the real ones to make room, and reported
success every single time. Nobody knew until somebody looked. So this page will
not call a backup good because a file with the right name exists. It says a
backup is verified only when something actually opened it, checked it and left a
written receipt saying so. A backup with no receipt is reported as unchecked, in
those words, and that is treated as a problem rather than as good news.

The page also carries no patient names, no hospital numbers, and nothing that
identifies a person, checked automatically, on all three ways of reading it.
This is the screen most likely to end up photographed and sent to a group chat
while somebody is trying to get help, and it has to be safe to do that.

**What it does NOT do yet**

- **It does not watch itself.** Somebody has to open the page. Nothing sends an
  alert when the backup check goes red, so a hospital that never looks is exactly
  as blind as before. Orb does have a separate heartbeat that can report out of
  the building, and joining the two together is the obvious next step.
- **There is no screen for it in the app yet.** The answer is available and the
  plain-text version can be read directly, but the page a hospital IT person
  would actually click on has not been built.
- **The deeper check is only run when asked for.** The ordinary reading is
  deliberately light so a dashboard can poll it, which means the more thorough
  look at the patient record only happens when somebody asks for it explicitly.
  It is recorded in the permanent log when they do.
- **It reports; it never repairs.** Nothing here restarts anything, retries a
  backup or clears space. That is on purpose for now, a status page that starts
  taking actions on its own is a status page nobody can trust to be telling the
  truth about what it just did, but it does mean every red line still needs a
  person.
- **Nobody has read it in anger.** Like most of this document: it has been tested
  against faults we invented. The first real Sunday-night failure will be one
  nobody wrote a check for.
