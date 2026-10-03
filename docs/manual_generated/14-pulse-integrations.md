## 14. Pulse, Integrations & Data Exchange

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Appointments board: scheduled follow-ups, medication reviews, and post-discharge tasks.](assets/manual/appointments.png)

*The Appointments board: scheduled follow-ups, medication reviews, and post-discharge tasks.*

Orb is a local-first system: the clinical record, the AI models, and the database all live on hospital hardware. This chapter covers every place where Orb touches the outside world, and the strict rules that govern each crossing:

- **Orb Pulse**, a live environmental intelligence feed (weather, air quality, flu activity, drug recalls) that flows *into* Orb and never sends anything out.
- **FHIR export**, standards-based export of a patient's record *out* of Orb, for EHR write-back and patient data portability.
- **Clinical data ingestion**, the inbound interface that lets bedside monitors, lab systems, and other hospital software write real vitals and results *into* Orb.
- **Appointments**, the shared scheduling layer that connects the patient portal's booking card to the clinician-facing schedule.

The unifying principle: data about the world flows in freely; data about patients leaves only through audited, permission-gated, standards-based export.

### 14.1 Orb Pulse: environmental intelligence

#### What Pulse is for

Hospitals do not operate in a vacuum. A heat wave fills the wards with dehydrated elderly patients; a spike in air pollution brings asthma and COPD exacerbations; flu season strains respiratory isolation capacity; an FDA recall may affect a drug your pharmacy stocks. Orb Pulse pulls these public signals into one popover and translates them into concrete clinical alerts with recommended actions.

Pulse is strictly **one-directional**. It pulls from free public data services and pushes nothing back. No patient identifier, vital sign, or clinical detail ever leaves the building through Pulse, the only things transmitted are the map coordinates used to fetch local weather and air quality.

#### Opening Pulse

Pulse lives in the header of the clinician shell, next to the notifications bell. Click the small heartbeat-line icon (its tooltip reads **"Orb Pulse, Environmental Intelligence"**). A translucent panel slides open over the workspace.

When at least one environmental alert is active, the trigger icon carries a small colored badge, red for a high-severity alert, amber for moderate, so you can see at a glance that conditions warrant attention without opening the panel.

To close the panel, click the icon again, click anywhere outside the panel, or press **Escape**. If you are inside a detail view, Escape first steps back to the main panel, then closes it.

#### Location

The first time you open Pulse, your browser asks for permission to share your location:

1. If you **allow**, Pulse uses your coordinates, looks up the nearest city name, and shows it in the panel header (during lookup the header briefly reads **"Locating..."**).
2. If you **decline** (or the browser has no location service), Pulse falls back to its configured default city.

Your chosen location is remembered between sessions on that device. To refresh or change it, click the location label (with the map-pin icon) in the panel header, Pulse will re-request your position.

#### Reading the panel

The panel is organized top to bottom, most urgent first. Each section names its data source in small text on the right of its label.

| Section | Source label | What it shows |
|---|---|---|
| **Active Clinical Alerts** | Orb Pulse Engine | Alert cards generated from the feeds below. High-severity alerts carry a red warning triangle; moderate alerts an amber circle. When nothing is active you see "No active environmental alerts". |
| **Weather** | Open-Meteo | Current temperature, conditions, "feels like" temperature and humidity, plus a seven-day forecast strip (day, icon, high/low). |
| **Air Quality** | Open-Meteo AQI | The US Air Quality Index number, color-coded by category (Good, Moderate, Unhealthy for Sensitive Groups, Unhealthy, Very Unhealthy, Hazardous), with the current PM2.5 reading. |
| **Flu Surveillance** | CDC FluView | The national influenza-like illness (ILI) rate and an activity level (minimal, low, moderate, high, very high). |
| **Recent FDA Drug Recalls** | openFDA | The three most recent FDA drug enforcement actions: product, reason for recall, and classification. |

The panel footer always reads **"Inbound only · Zero patient data transmitted"** and includes a **Refresh** button.

#### Detail views

Every card is clickable. Clicking opens a detail overlay with a back arrow in the top-left corner:

- **Weather Details**, full current readings (Humidity, Wind Speed, Pressure, Coordinates), the complete 7-Day Forecast, any Clinical Recommendations, and a source attribution link.
- **Air Quality Details**, the AQI hero number and category, plus individual pollutant rows: PM2.5, PM10, Ozone, Nitrogen Dioxide, and Carbon Monoxide, with Clinical Recommendations when the index is elevated.
- **Flu Surveillance Details**, the ILI percentage, Source, Surveillance Week, Year, and (when real CDC data is flowing) the number of Patients Surveyed.
- **Alert Details**, for any clinical alert: the full message, Severity, Type, At-Risk Groups, and a checklist of Recommended Actions.
- **Drug Recall Details**, the full product description, Reason for Recall, Recall Number, Status, Recalling Firm, Location, Report Date, and Initiation Date.

Every detail view ends with a "Source:" badge linking to the originating public service, so you can verify the data yourself.

#### How clinical alerts are generated

Pulse converts raw environmental readings into alerts using fixed clinical thresholds. Each alert names the at-risk patient groups and lists concrete recommended actions (for example, a heat advisory recommends increasing IV fluid monitoring for at-risk patients and increasing rounding frequency on geriatric wards).

| Alert | Trigger | Severity |
|---|---|---|
| Heat Advisory | Temperature at or above 38 °C | Moderate; **high** at 42 °C or above |
| Cold Advisory | Temperature at or below 5 °C | Moderate |
| Storm Forecast | Rain showers or thunderstorms forecast within the next 3 days | Moderate; **high** for thunderstorms |
| Dangerous Heat Index | Temperature at or above 32 °C combined with humidity at or above 70% | High |
| Air Quality Alert | AQI above 100 | Moderate; **high** above 150 |
| Influenza Surge | National ILI rate above 3.0% | Moderate; **high** above 5.0% |

Alerts are sorted with high severity first, and the count drives the badge on the header icon.

#### Refresh behavior and the flu fallback

While the panel is open, Pulse refreshes itself automatically every 15 minutes; the **Refresh** button in the footer forces an immediate re-fetch. Behind the scenes each feed is cached to be a good citizen to the free public services: weather for 30 minutes, air quality and FDA recalls for one hour, and flu surveillance for 24 hours (the underlying data is weekly). If a feed is temporarily unreachable, Pulse quietly serves the last good data it has rather than showing an error.

Flu surveillance has an explicit fallback: when the CDC service cannot be reached at all, Pulse substitutes a **seasonal estimate**, a typical ILI value for the current month of the year. This is clearly labeled: the popover shows "(seasonal estimate)" next to the ILI figure, and the detail view's Source row reads "Seasonal Estimate (CDC API unavailable)". A seasonal estimate is a climatological guess, not surveillance, treat it as background context only, and never as evidence of an actual surge.

#### Privacy: the INBOUND_ONLY guarantee

Every single external data pull that Pulse makes is written to an ingestion audit trail recording the timestamp, the source, the exact address queried, the outcome, and a cryptographic fingerprint of the response, and every entry is stamped with the direction **INBOUND_ONLY**. Administrators with audit access can review this trail to verify that the only information transmitted outward was a pair of map coordinates. The dashboard payload itself carries the statement "INBOUND_ONLY, zero patient data transmitted", and the panel footer restates it in plain language.

The one nuance worth knowing: the city name shown in the header is resolved by sending your coordinates to a public map service (OpenStreetMap). Coordinates only, never anything about patients or users.

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
| Pulse shows the default city instead of yours | Location permission was declined, or the browser has no location service | Click the location label in the Pulse header to re-request; allow location access in your browser's site settings |
| Pulse header stuck on "Locating..." | Slow or unavailable device location | Wait a few seconds; if it persists, the last saved or default location is used, click the location label to retry |
| Flu section says "(seasonal estimate)" | The CDC service is unreachable | No action needed, but treat the figure as a monthly climatological estimate, not live surveillance |
| A Pulse section is missing entirely | That feed returned no data and no cache exists yet | Click **Refresh**; the section reappears when the feed responds |
| Pulse readings seem stale | Feeds are cached (weather 30 min; AQI and recalls 1 h; flu 24 h) | This is by design; **Refresh** re-checks, but data within its cache window is reused |
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

- **Pulse is situational awareness, not clinical guidance.** Its alerts are generated from fixed environmental thresholds and generic recommendations. They do not know your patients, your census, or your local epidemiology. Use them as a prompt for judgment, never as a substitute for it.
- **The flu fallback is an estimate.** When CDC data is unavailable, the labeled seasonal estimate reflects a typical year, not this year. Do not base surge decisions on it.
- **FDA recall cards are national enforcement actions.** A listed recall does not necessarily involve a lot your pharmacy holds. Always verify lot numbers with pharmacy before acting.
- **Pulse never transmits patient data**, the audit trail proves it, entry by entry, each stamped INBOUND_ONLY. The only outbound information is the coordinates used for local weather, air quality, and city-name lookup.
- **FHIR export is the only door out for PHI**, it requires authentication and authorization on every request, and it is fail-closed on auditing: no audit record, no export. AI-assisted content leaves Orb tagged as draft so downstream systems cannot mistake it for verified clinical fact.
- **Ingestion is fail-safe by mode.** Production defaults exclude synthetic data; simulation mode excludes external writes. Confirm the mode after any redeployment.
- **The ingestion API key is a patient-record credential.** Store it in the connected system's secret store, never in shared documents, and rotate it on staff or vendor changes.
- **Predicted appointments are synthetic planning aids** derived from the current census. They are deliberately excluded from any notion of patient commitment, only **Booked** entries reflect real scheduling promises, and only those are backed by audited booking records.
