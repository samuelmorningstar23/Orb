## 14. Pulse, Integrations & Data Exchange

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Appointments board: scheduled follow-ups, medication reviews, and post-discharge tasks.](assets/manual/appointments.png)

*The Appointments board: scheduled follow-ups, medication reviews, and post-discharge tasks.*

Orb is a local-first system: the clinical record, the AI models, and the database all live on hospital hardware. This chapter covers every place where data enters or leaves Orb, and the strict rules that govern each crossing:

- **Pulse**, a seasonal calendar for the hospital's state, built into Orb. It makes no connection to anything outside the building.
- **FHIR export**, standards-based export of a patient's record *out* of Orb, for EHR write-back and patient data portability.
- **Clinical data ingestion**, the inbound interface that lets bedside monitors, lab systems, and other hospital software write real vitals and results *into* Orb.
- **Appointments**, the shared scheduling layer that connects the patient portal's booking card to the clinician-facing schedule.

The unifying principle: Orb makes no internet connection of any kind. Data comes in only from systems inside the hospital, and data about patients leaves only through audited, permission-gated, standards-based export.

### 14.1 Pulse: season and conditions

#### What Pulse is for

Illness in India follows the seasons. The monsoon brings dengue, malaria and leptospirosis; summer brings heat illness; winter in the north brings bad air and chest infections. Pulse shows what usually rises in the hospital's state at this time of year, and what is coming up, so a ward, a pharmacy or a bed manager can plan ahead.

Pulse works from a seasonal calendar built into Orb. It makes no internet connection: no weather service, no air-quality feed, no flu feed, no drug-recall feed and no location lookup. Orb as a whole makes no internet connection of any kind.

#### Opening Pulse

Pulse lives in the header of the clinician shell, next to the notifications bell. Click the small heartbeat-line icon (its tooltip reads **"Pulse: season and conditions"**). A panel titled **Pulse**, with **Season and conditions** beneath, opens over the workspace. It reads the calendar afresh each time you open it; there is nothing to refresh, because the date only changes once a day. To close it, click the **X**, click the icon again, click anywhere outside the panel, or press **Escape**.

#### Reading the panel

Under the title the panel names the place and today's date, for example **Maharashtra**, or **Maharashtra (Konkan)** when the calendar is kept for one zone of a state. Next it says whether a clinician has checked the calendar:

- **"Draft: not yet reviewed by a clinician"** means nobody at your hospital has signed it off yet. Orb ships this way.
- **"Reviewed by [name] on [date]"** appears once the hospital's clinical lead has reviewed it.

| Section | What it shows |
|---|---|
| **This season** | One card for each entry in effect today for the hospital's state: a season, an environmental condition or a disease season. Each card shows a title (for example "South-west monsoon" or "Dengue season"), a **Peak now** tag when this is its peak, the period it usually runs ("June to September"), a short summary, a "Typically rises: ..." line naming the conditions that usually go up, and its sources, named in plain text. When nothing covers today the section says "Nothing in the calendar covers today." |
| **Coming up** | Entries that start in the next 60 days, one line each: "Tomorrow: ..." or "In 12 days: ...". The section is left out when nothing is coming up. |

The footer reads **"Built into Orb. Calendar compiled [date]."**

#### Where the state comes from

Pulse uses the hospital's state (or union territory) as your administrator set it in the site settings: the `state` line under `[site]` in `site.toml`, for example `state = "Maharashtra"` or `state = "MH"`. Pulse never asks your browser for your location and never looks anything up. If the state has not been set, the panel shows **"State not set"** and asks for it instead of guessing; tell your administrator.

#### Pulse and Forecast

Forecast shows the current season as one line of context, for example "Season: South-west monsoon (peak), Dengue season". It does not change any forecast number (see the Forecast chapter).

#### What Pulse does not do yet

Pulse does not show today's weather, the air-quality index, flu activity or drug recalls. It shows what usually happens in a season, not what is happening this week. Two more stages are planned and neither is built:

- **Stage 2.** A one-minute daily entry of official warnings (IMD heatwave warnings, the CPCB air-quality index) by a person the hospital names, and support for a weather and air-quality sensor on the hospital network.
- **Stage 3.** Disease trends drawn from the hospital's own admissions, and the CDSCO drug-recall list checked against the pharmacy's stock.

One option is ruled out: a separate internet-connected box that fetches the weather and pushes it into Orb. That box would be a bridge from the internet into Orb, which the offline rule does not allow.

### 14.2 FHIR export: getting data out of Orb

#### What FHIR export is for

FHIR R4 is the lingua franca of modern health data exchange. Orb can serialize its clinical record into standard FHIR resources so that:

- A hospital's main EHR can absorb what happened in Orb (write-back).
- A patient can take a complete machine-readable copy of their own record with them (data portability).

Export is read-only from Orb's perspective, it never modifies the record, but it is treated as the most sensitive operation in this chapter, because protected health information leaves the system.

#### What can be exported

| Export | Contents |
|---|---|
| **Patient** | A single FHIR Patient resource, demographics and identifiers for one patient. |
| **Everything bundle** | A complete FHIR collection Bundle for one patient: the Patient resource plus all Observations (vitals and labs), MedicationRequests, AllergyIntolerances, and Conditions. An optional "since" filter restricts the bundle to data recorded after a given moment, useful for periodic incremental syncs into an EHR. |
| **MedicationRequest bundle** | A searchset Bundle containing just the patient's medications as MedicationRequest resources. |

Two provenance guarantees apply to everything Orb exports:

1. Every exported resource is explicitly flagged as **originating from Orb**, so a receiving EHR always knows where the data came from.
2. Any content that was AI-assisted (for example, an AI-suggested medication that has not been clinician-verified) carries an **ai-draft** provenance tag. Receiving systems can, and should, treat draft-tagged content differently from verified clinical facts.

#### Who can export

- **Clinicians and administrators** with full patient-read access can export any patient's record through the export interface.
- **Patients** can export exactly one record: their own. This is the data-portability path.

There is no anonymous or unauthenticated export of any kind.

#### Patient self-export: "Download my record (FHIR)"

In the patient portal (Bridge), the **My Records** panel contains a button labeled **"Download my record (FHIR)"**. One click assembles the patient's complete everything-bundle and downloads it to their device as a JSON file named after their record. If the download cannot be completed, an error message appears directly beneath the button ("Could not download your record right now."), the patient can simply try again.

The file is a standard FHIR R4 Bundle, readable by any FHIR-aware application, another hospital's intake system, or a personal health record app.

#### Every export is audited: and the audit is fail-closed

Each FHIR export writes a **PHI-access** event to Orb's audit chain: who exported, what resource type, which patient, how many resources, from what network address, and when. This audit category is *fail-closed*: if the audit record cannot be persisted, the export itself is refused. It is impossible for patient data to leave Orb without a durable audit record of the fact. (Chapter on the audit chain covers how to review these events in the Flight Recorder.)

### 14.3 Inbound clinical data ingestion (administrators)

This section is for administrators and hospital integration teams. Clinicians never interact with the ingestion interface directly, they simply see its results: real vitals and lab values appearing on patient dashboards in real time.

#### Simulation mode vs. ingestion mode

Orb's live data layer runs in one of three operating modes, chosen at deployment time by the administrator:

| Mode | Simulation bots | Ingestion endpoints | Intended use |
|---|---|---|---|
| **ingestion** (default) | Off | On | Production. Only real data from real devices enters the record. |
| **simulation** | On | Off (requests are refused with a clear message) | Demos and training. Built-in bots generate synthetic vitals (every 5 seconds), reports, and lab results for the seeded demo patients. |
| **hybrid** | On | On | Development and integration testing, where simulated background activity and real test feeds coexist. |

The design intent is safety in both directions: in production, synthetic data can never contaminate the real record; in a pure demo, external systems can never write into what is understood to be a sandbox. The current mode is reported by the ingestion status query (below), so an administrator can always confirm which regime is active. Related to this, demo mode (which permits synthetic clinical *content* such as mock notes) defaults off in production deployments.

#### Authentication: the ingestion API key

All ingestion endpoints are machine-to-machine and are protected by a dedicated API key sent in the `X-API-Key` header of every request. Two properties matter to administrators:

- **No shipped default in production.** A production deployment refuses to start unless the ingestion key has been explicitly set by the operator. (A well-known development key exists only for dev and test profiles and must never guard real data.)
- The key comparison is performed in constant time, resisting timing-based key-guessing attacks.

Treat this key like a password to the patient record, because that is exactly what it is. Rotate it if a connected system is decommissioned or a compromise is suspected.

#### The three ingestion formats

Orb accepts inbound clinical data in three formats, each on its own endpoint under the ingestion interface:

1. **JSON webhook**, a simple structured payload designed for modern devices and middleware. Its fields:

   | Field | Required | Meaning |
   |---|---|---|
   | patient_id | Yes | The Orb patient the data belongs to |
   | observations | Yes | A list of readings; each has a name, value, and unit, with optional LOINC code, reference range, abnormal flag, and status |
   | encounter_id | No | Encounter association |
   | source_name | No | Human-readable device name, e.g. "BedsideMonitor-ICU-3" |
   | message_id | No | Sender-side unique ID used for duplicate suppression |
   | observed_at | No | When the reading was actually taken |

2. **HL7 v2 ORU**, the classic result-message format spoken by most legacy lab systems and monitor gateways. The raw message is posted as plain text (a JSON wrapper form is also accepted for senders that cannot post raw text).

3. **FHIR R4**, a FHIR Bundle (transaction, collection, or batch) or a single resource. Supported inbound resource types: Observation, Patient, Encounter, MedicationRequest, and AllergyIntolerance.

All three endpoints accept two optional tracing headers: a **correlation ID** (carried through Orb's logs so an event can be traced end to end; one is generated if the sender omits it) and a **message ID** (the sender's deduplication key, replaying the same message ID is treated as an idempotent success rather than creating duplicate data). A source-name header identifies the sending system.

There is also a development-only **replay** endpoint that accepts any raw payload and auto-detects its format, useful when testing fixtures during integration work. Replays are audit-logged.

#### What the sender gets back

Every ingestion request returns a structured result: an event ID, the correlation ID, a parse status, and counts of what happened (observations received, vitals inserted, observations inserted, duplicates skipped), plus any per-item errors.

| Parse status | Meaning | HTTP outcome |
|---|---|---|
| parsed | Everything ingested | 200 |
| partially_parsed | Some items ingested, some failed (details in the errors list) | 207 Multi-Status |
| duplicate | Message ID already seen; nothing re-inserted | 200 (idempotent success) |
| failed | Nothing could be ingested | 422 |

Successfully ingested vitals are emitted to connected dashboards immediately, a reading posted by a monitor appears on the patient's live chart without a page refresh.

#### Monitoring ingestion

Administrators (and any role granted ingestion-monitoring permission) can query recent ingestion events: each event's receive time, source type and name, parse status, any parse error, correlation ID, insert counts, and processing time, filterable by status (received, parsed, failed, duplicate). The response also reports the active ingestion mode. This is the first place to look when a device feed "goes quiet": if events are arriving but failing to parse, the parse errors will say why; if no events are arriving at all, the problem is upstream of Orb.

### 14.4 Appointments

Appointments in Orb have two faces: a **booking card** in the patient portal, and an **Appointments tab** in the clinician shell. Bookings made in one appear in the other.

#### Booking from the patient portal ("Schedule Follow-Up")

On the patient portal home screen, the **Schedule Follow-Up** card walks the patient through a four-step booking:

1. **Pick a date.** A week-strip calendar shows seven days at a time, with arrow buttons to move between weeks and the current month and year shown in the header. Today is highlighted; past days and Sundays are disabled and cannot be selected.
2. **Pick a time.** Once a date is chosen, a "Pick a time" section appears with six slots: 9:00 AM, 10:00 AM, 11:00 AM, 2:00 PM, 3:00 PM, and 4:00 PM.
3. **Choose a department.** Options: **Primary**, **Cardio**, **Neuro**, **Ortho**, and **Lab**.
4. **Optionally add a note.** A free-text field labeled "Note for your doctor (optional)", for example, "I'd like to discuss my medication".

When date, time, and department are all selected, the **Confirm Appointment** button appears. On success the card flips to a confirmation view: an animated check mark, the word **Confirmed**, the booked date, time, and department, and a **New** button to start another booking. If booking fails, an error message appears beneath the button and nothing is saved.

Above the calendar, the card lists **"Your upcoming appointments"**, every future booked appointment with its date, time, and type, each with a **Cancel** button. Cancelling asks no questions and takes effect immediately; the list then re-syncs with the server so it always reflects the truth.

Rules enforced by the system, regardless of what the interface shows:

- A patient can only ever book or cancel **their own** appointments. The patient identity is resolved from the login session, it cannot be supplied or spoofed in the request.
- Appointments cannot be booked in the past, and dates and times must be well-formed.
- Double-submissions are harmless: each booking carries a one-time idempotency token, so an accidental double click (or a network retry) returns the original booking instead of creating a duplicate slot.
- Cancellation is **soft**: the appointment's status changes to cancelled, but the record is kept for the audit trail.
- Every booking and cancellation is written to the audit chain as a clinical-record event, naming who acted, for which patient, and when.

Clinicians with patient-write permission can also book on behalf of a patient (naming the patient explicitly) and can cancel any appointment; the same validation, idempotency, and audit rules apply.

#### The clinician Appointments tab

Doctors, nurses, and administrators see **Appointments** in the sidebar of the clinician shell. The panel is headed **"Scheduled Appointments"** with the subtitle "Review daily clinical follow-ups, medication reviews, and post-discharge tasks."

A summary strip of four metric cards sits at the top:

| Card | Meaning |
|---|---|
| **Today's Appointments** | Count of appointments dated today, with the number of active days beneath |
| **This Week Total** | All appointments in the visible horizon |
| **Critical Patients** | Appointments for patients whose latest risk assessment is critical |
| **Post-Discharge** | Follow-up reviews for recently discharged patients |

Below, the schedule is laid out as one column per day. Today's column is highlighted with an amber edge and a "Today" tag. Each appointment card shows the time, an acuity badge (critical, elevated, or stable, taken from the patient's most recent risk score), the patient's name, age, and sex, the appointment type, and the visit reason.

Two kinds of entries appear side by side:

- **Booked** appointments, real bookings made through the patient portal or by a clinician. These carry a gold **Booked** badge so they are unmistakable.
- **Predicted** entries, a synthesized working schedule generated each day from the current census: a handful of follow-ups for admitted patients (weighted with occasional post-discharge reviews for recently discharged ones), with deterministic times so the schedule does not reshuffle every time the page refreshes. Sundays carry no predicted entries.

The predicted schedule is planning scaffolding, not a source of truth, only entries with the **Booked** badge represent commitments made to a patient. If loading fails, the panel shows an error with a **Retry** button.

### Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Pulse asks for the hospital's state | The `[site] state` setting is missing from `site.toml` | Ask an administrator to add it (the state or union territory, by name or two-letter code) and restart Orb |
| Pulse shows "Draft: not yet reviewed by a clinician" | Nobody at this hospital has reviewed the seasonal calendar yet | Treat the cards as a starting point until the clinical lead has reviewed them |
| Pulse says "The seasonal calendar could not be loaded." | The calendar file is missing or failed its checks at startup, or the server did not answer | Click **Try again**. If the message says the calendar is not installed or has errors, an administrator must fix the file (the server log lists each problem) and restart Orb |
| Pulse has no weather, air-quality, flu or recall cards | Those internet feeds were removed; Orb makes no internet connection | Working as intended; see 14.1 |
| Patient's "Download my record (FHIR)" shows an error | Transient failure assembling or downloading the bundle | Try again; if it persists, a clinician or administrator can perform the export and check the audit trail |
| FHIR export refused for a clinician | The account lacks full patient-read permission | An administrator must grant the appropriate role; patients can only export their own record |
| Device feed refused with a "disabled in simulation mode" message | The deployment is running in simulation mode | An administrator must switch the deployment to ingestion (or hybrid) mode |
| Ingestion requests rejected as unauthorized | Missing or wrong `X-API-Key` | Verify the connected system is sending the current ingestion key |
| Data posted but not all of it appears | Partial parse (207 status) | Check the errors list in the response and the ingestion status view for the per-item failure reasons |
| Same reading appears once despite being sent twice | Duplicate suppression via the sender's message ID | Working as intended, replays are idempotent |
| Patient cannot select a booking date | Past dates and Sundays are disabled | Choose an upcoming non-Sunday date |
| "Cannot book an appointment in the past" | Date validation rejected the slot | Pick a future date |
| Appointments tab shows entries the patient never booked | Those are predicted schedule entries | Only cards with the gold **Booked** badge are real bookings |
| Appointments panel fails to load | Connectivity to the local server | Click **Retry**; if it persists, check that the Orb server is running |

### Safety & limitations

- **Pulse is background, not clinical guidance.** The calendar says what usually rises in a season in your state. It does not know your patients, your census, this year's weather or an outbreak happening now. Use it as a prompt for planning, never as evidence of a surge.
- **The calendar is a draft until a clinician reviews it.** It ships marked "Draft: not yet reviewed by a clinician" and says "Reviewed by [name] on [date]" only after the hospital's clinical lead has been through it.
- **Pulse makes no internet connection.** It fetches nothing and sends nothing; the calendar lives on the appliance.
- **FHIR export is the only door out for PHI**, it requires authentication and authorization on every request, and it is fail-closed on auditing: no audit record, no export. AI-assisted content leaves Orb tagged as draft so downstream systems cannot mistake it for verified clinical fact.
- **Ingestion is fail-safe by mode.** Production defaults exclude synthetic data; simulation mode excludes external writes. Confirm the mode after any redeployment.
- **The ingestion API key is a patient-record credential.** Store it in the connected system's secret store, never in shared documents, and rotate it on staff or vendor changes.
- **Predicted appointments are synthetic planning aids** derived from the current census. They are deliberately excluded from any notion of patient commitment, only **Booked** entries reflect real scheduling promises, and only those are backed by audited booking records.
