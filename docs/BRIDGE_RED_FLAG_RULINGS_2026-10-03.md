# Bridge red flags: what was built, and six rulings for Samuel

Date: 2026-10-03. Backups: `docs/archive/pre-bridge-red-flag-2026-10-03/`.
Tests: `server/tests/test_bridge_red_flag_delivery.py` (14 tests; each guard was
proven by a mutation that put back the old behaviour).

## What was wrong (confirmed)

When a patient typed an emergency or self-harm phrase into the portal assistant,
Bridge did the right first thing: a fixed regex screen caught it before any
model saw it, and the patient got a fixed safe reply. That reply ended with
**"Your care team has been notified."** Nobody was.

1. The only signal was a `bridge.emergency.flag` event. Its `patient_id` was
   always None, because the `/ask` route built its context with
   `get_patient_status_simple`, whose dict has no id key.
2. The event's only subscriber is the ward-brief narrator. It rebuilds a summary
   card. It tells no one. The docstring said "the duty nurse / Relay layer
   subscribes"; nothing in Relay or escalation did.
3. The model's audit row for every ordinary Bridge answer
   (`purpose="bridge_patient_answer"`) also had `patient_id` None, same cause.

## What was built (all reversible)

- **The real patient id.** `/ask` now builds its context with
  `bridge_service.patient_ask_context`, which is the status dict plus
  `patient_id`. `/status` still returns the status dict unchanged, so the portal
  payload did not change (no id, no colour, no verdict, as ruled).
- **A case-room alert.** On a red flag, the Orb Bridge bot (`bot_bridge`, added
  to `SERVICE_ACCOUNT_IDS`) posts a `message_type='alert'` into the patient's
  Relay case room through `relay_service.send_message`, and pushes it live as
  `message:new` with that function's own serializer. It reads:

  > ⚠️ BRIDGE ALERT [PATIENT PORTAL]: possible medical emergency
  > The patient in bed RCU-01 typed "can't breathe" to the patient portal assistant.
  > Orb gave a fixed reply, not a model answer: press the call button now, and call the local emergency number if in immediate danger.
  > Orb does not page or phone anyone. Check on the patient now.

  Self-harm uses "words about self-harm or suicide" and mentions the crisis line.
- **The reply claims notification only when it is true.** "Your care team has
  been notified." is said only once the post is stored. No case room, no patient
  record, a failed write, or a write slower than 2 s: the patient gets the same
  reply without that sentence (the call-button instruction is unchanged), and
  the server logs an ERROR naming the patient.
- **The post survives the patient closing the page.** Delivery runs as its own
  task, so a cancelled response stream does not cancel it.
- The narrator still gets its event, now with the real patient id.

Browser-verified on an isolated stack: P001 typed "I can't breathe properly and
I feel scared"; DOC006, with Ravi Shankar's case room open, saw the alert arrive
live (no refetch) with the AUTO chip, quoting only "can't breathe". With the
case room detached, the reply dropped the "notified" sentence and the server
logged "has NO case room; it reached no clinician".

## Rulings needed

### R1. Who hears it now? (the big one)

As built, the alert reaches whoever has that patient's case room open, and the
room rises to the top of every other Relay list on its 30 s poll with an alert
badge. That is the same reach as every other Orb alert today. It is not a page.

| Option | What it does | Cost |
|---|---|---|
| A. As built | Case room post + live push to viewers of that room | A nurse on another screen may not see it for 30 s, or at all if Relay is closed |
| B. Add a ward-wide live alert | New socket event (say `bridge:red_flag`) to the department room every vitals.read user joins; `criticalAlerts.ts` raises a browser notification and chime | Small build. Must NOT reuse `message:new` for this: RelayPanel appends any `message:new` into whichever room is open, so the alert would show in the wrong room |
| C. Open an escalation chain | Acknowledgement, tier climb, board | `clinical_escalations` is NEWS2-shaped (trigger 'news2', score columns, one open chain per patient, so it would merge into an existing NEWS2 chain), and the board has no screen yet |

**Recommendation: B now, C later** once the escalation board has a screen and a
trigger type for non-NEWS2 causes.

### R2. How much of the patient's words go into the post?

As built: only the phrase the screen matched ("can't breathe", "want to die").

- Full message: better for spotting false positives ("what is a stroke?"), but
  it puts patient-written free text into a room Sage reads as clinical context
  (a prompt-injection surface) and spreads a self-harm disclosure wider.
- **Recommendation: keep the matched phrase.**

### R3. The patient-facing sentence

Even when true, "Your care team has been notified" means "posted to the case
room". Options: keep it, or soften it to "A message has been sent to your care
team." **Recommendation: soften**, because "notified" reads as "someone knows".
Needs your wording sign-off either way.

### R4. The crisis line is American

The self-harm reply says "in the US, call or text 988". Orb is sold in India.
India's national line is Tele-MANAS, 14416 (or 1-800-891-4416). It should be a
per-site setting, and a clinical advisor should sign off on the wording.
**Not changed**: this is patient-facing safety wording.

### R5. Some departments have no nurse login

In the dev data, P001 (Ravi Shankar) is in Pulmonology, whose only staff login
is DOC006, a doctor. No nurse can see that case room. For pilots: every
admitting department needs a nurse login holding Relay, or red flags (and every
Vigil alert) reach one doctor.

### R6. False positives and a record

- The regex is crude: "what is a stroke?" or "my father had a heart attack" now
  post an alert. I recommend no cooldown for now (a patient in crisis may type
  several times, and each message carries information) and a review after the
  pilot's first month.
- There is no audit row for a red flag; the post itself is the record. Option:
  add a `bridge_red_flag` audit event (string target ids only, see the audit
  int-id chain issue).
