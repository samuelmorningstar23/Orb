## 12. Bridge: The Patient Portal

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The Bridge patient portal presents the same clinical record in plain language.](assets/manual/bridge-patient-portal.png)

*The Bridge patient portal presents the same clinical record in plain language.*

Bridge is Orb's patient-facing portal. While every other module in this manual is built for clinicians and administrators, Bridge is the one screen designed to be handed to the patient, or to a family member at the bedside. It translates the clinical picture into plain, reassuring language: how the patient is doing right now, who is looking after them, how their vital signs are trending, what medications and reports are on file, and an AI assistant that answers everyday questions about their care in simple words.

Bridge is deliberately narrow. A patient signed into Bridge can see **only their own record**, every request is scoped on the server to the account that is signed in, and there is no way to browse other patients, wards, or staff tools. Bridge also includes a self-service follow-up appointment scheduler and, after discharge, a secure web view of the discharge summary that works without a portal account at all (see *Post-discharge access* below).

This chapter covers three related experiences:

1. The **signed-in patient portal** (the "Orb Bridge" screen) used during an admission.
2. The **AI question box** inside that portal, including exactly what it will and will not answer.
3. The **discharge access flow**, the secure link and QR code printed at discharge that lets a patient open their discharge summary on the hospital's patient Wi-Fi, after verifying their date of birth, and save a copy to their phone before they leave.

---

### 12.1 Signing in as a patient

Patients sign in on the **Patient Access** page (the patient variant of the login screen, staff use the *Clinician Login* variant of the same page).

1. Open the portal address your hospital provides and choose the patient login page. The card is titled **Patient Access**, with the subtitle **Orb Bridge**.
2. Enter your **Patient ID**, for example `P001` (the field shows this format as its placeholder).
3. Enter your **Password** in the field labeled **Password** ("Enter your password").
4. Select **Sign in**.

On success the portal opens directly on the Bridge home screen. If the account has multi-factor authentication enabled, a second step appears: an **Authenticator code** field asking for the 6-digit code from an authenticator app, with a **Verify code** button and a **Use a different account** link to start over.

Notes for administrators:

- The seeded demonstration environment includes one patient portal account: Patient ID **P001** (Raj Patel) with password **patient123**.
- In development builds only, a **Quick access** section appears under the form with a one-click chip for the demo patient. This section is compiled out of production builds, production login pages never display working credentials.
- If a patient leaves the password blank the page prompts "Please enter your password.", there is no default or shared password; every portal account requires a real credential.

Signing out: the **Sign out** button is always visible at the top right of the Bridge header.

---

### 12.2 The Bridge home screen

After signing in, the patient sees a single scrolling page headed **Orb Bridge** (with the Orb logo), a greeting with their name, and the welcome line:

> **Welcome, <first name>**
> "Here's your care overview. Everything in simple, clear language."

The page is organised as a row of cards, followed by a records panel and the question box:

| Area | Card / control | What it shows |
|---|---|---|
| Status | **Your Status** | Plain-language summary of how the patient is doing, with the level (Stable, Elevated or Critical) in a plain, uncoloured badge |
| Care team | **Your Care Team** | The attending physician, a ward nurse, and the care unit / ward / bed |
| Vitals | **Your Vitals & Trends** | Current readings with mini trend charts and normal-range context |
| Appointments | **Schedule Follow-Up** | Week calendar, time slots, and department picker for booking a follow-up |
| Records | **My Records** | Buttons for Medications, Report History, Lab History, Discharge Summary, and a FHIR download |
| AI assistant | Question box | "What would you like to know about your care?" |

The status card refreshes itself automatically every 30 seconds, so the page can be left open at the bedside.

#### Your Status

The **Your Status** card condenses the clinical risk picture into one of three levels, shown as a badge:

| Badge | Meaning shown to the patient |
|---|---|
| **Stable** (green) | "Your vitals are looking good. Your care team is happy with how things are going." |
| **Elevated** (amber) | "Your care team is keeping a closer eye on you right now. This is a normal precaution, they want to make sure you're comfortable and safe." |
| **Critical** (red) | "Your care team is monitoring you very closely and giving you their full attention. You're in good hands." |

The wording is intentionally calm at every level. Bridge never shows patients raw risk scores, alarm names, or clinical terminology, the level is derived from the same monitoring data clinicians see in Vigil, but the presentation is reassurance-first. If no status is available yet, the card reads "Your care team is monitoring you."

#### Your Care Team

The **Your Care Team** card answers "who is looking after me?" It lists:

- The **Attending Physician** (name and department), taken from the patient's assigned attending.
- A **Nurse** from the same department where one is on record (a ward nurse is shown on a best-effort basis).
- A **Care unit** line at the bottom: department, ward, and bed, for example "Care unit: Cardiology · Ward 3A · Bed 12".

If no team has been assigned yet the card reads "Your care team will appear here once assigned."

#### Your Vitals & Trends

The **Your Vitals & Trends** card shows up to five vital signs: heart rate (bpm), systolic BP (mmHg), oxygen (SpO₂, %), temperature (°C) and respiratory rate (/min). Each shows the latest reading, when it was measured ("Today 14:32", or the date for an older reading), and a small trend line (sparkline). The sparkline plots up to 24 points drawn from the last 48 recorded measurements, so a patient or relative can see whether a number is settling or climbing. A vital that has never been measured reads **No recent reading**.

Readings are printed as charted: whole numbers, and temperature to one decimal place. That is the same number the ward sees and NEWS2 scores.

The card does not call any reading normal, high or low, and nothing on it is colour-coded. Whether a reading is a concern depends on the patient's condition, their oxygen target and the whole set of observations, and that judgement belongs to the care team. A fixed patient-facing range cannot make it: a systolic BP of 90, which NEWS2 scores 3 points, would sit inside a typical "normal" band. The card ends with:

> "Your care team reviews every reading in the context of your condition. If you feel worse, tell your nurse straight away."

If a patient asks what a number means, staff should answer it themselves rather than point back to the card.

---

### 12.3 Asking questions: the Bridge AI assistant

At the bottom of the page is a single input labelled **"What would you like to know about your care?"** with a send (paper-plane) button. The patient types a question and presses **Enter** or the send button. The answer streams in word-by-word into a panel headed **Orb Bridge**, so the patient sees the response being written rather than waiting on a blank screen.

The assistant runs entirely on the hospital's local AI engine, the question and the answer never leave the building. It shares that engine with clinical AI features, so at busy moments an answer may take a few extra seconds to begin.

#### What the assistant knows

The assistant is given only a minimal, patient-safe snapshot: the patient's name, their current status level and status summary, a simplified one-line description of their condition, and their four current vital readings. It does **not** see the full chart, clinical notes, lab values, imaging, or staff conversations. This is a deliberate privacy and safety boundary, the assistant can explain and reassure, but it cannot reveal or interpret detailed clinical material.

#### What it will and won't answer

The assistant follows strict patient-facing rules:

- It uses **simple, everyday language**, no medical jargon.
- It is warm, brief, and reassuring; answers are kept short because patients are often tired or anxious.
- It **never gives medical advice**. For any clinical decision, "should I stop this tablet?", "is this result bad?", it will say, in its own words, *please ask your nurse or doctor*.
- If asked about a medication, it explains simply what the medication is for, but tells the patient to confirm details with their doctor.

If the assistant cannot be reached (for example the AI engine is offline), the panel shows: "Sorry, I couldn't connect to the assistant. Please ask your nurse or doctor directly."

#### Emergency and crisis guardrails

Every question is screened **before** it ever reaches the AI model by a deterministic red-flag filter written in everyday patient language. If the message matches an emergency or crisis pattern, the AI is never consulted; the portal instead returns a fixed, pre-written safety message and simultaneously raises a real-time alert to the care team.

Two categories are screened:

1. **Acute physical emergencies**, chest pain or pressure, trouble breathing or choking, stroke signs (face drooping, slurred speech, sudden numbness or weakness), heavy or unstoppable bleeding, severe allergic reactions or throat swelling, fainting, collapse, or seizures. The fixed response tells the patient this may be a medical emergency, to press the call button or have someone alert staff immediately, and to call the local emergency number if in immediate danger, and confirms the care team has been notified.
2. **Self-harm or suicidal thoughts.** The fixed response is compassionate, asks the patient to press the call button so a nurse can be with them, and points to crisis support (in the US, call or text 988), and confirms the care team has been notified.

In both cases a "patient red flag" event is published to the hospital's real-time notification layer so the duty team is alerted, including the patient's name, ward, and bed. The design principle is that an acute emergency must never be handled by a free-form generative model whose style is biased toward reassurance, the screening is rule-based and always wins.

A second protective layer defends the assistant itself: the patient's data snapshot is passed to the model inside explicit "data-only" fencing, with instruction-like text stripped, so that content in the record cannot be used to manipulate the assistant's behaviour (prompt-injection hardening).

---

### 12.4 My Records

The **My Records** card gives the patient read-only access to their own file through four buttons plus a download:

| Button | Opens | Contents |
|---|---|---|
| **Medications** | *Medication History* | Each medication with dosage, route, frequency, and a status badge (e.g. active) |
| **Report History** | *Clinical Report History* | The most recent clinical documents, title, document type, and date |
| **Lab History** | *Lab & Imaging History* | Diagnostic imaging reports, study type, clinical context, and date |
| **Discharge Summary** | *Discharge Summary* | The latest discharge summary, if one exists |
| **Download my record (FHIR)** | (download) | The full record as a standard FHIR bundle file |

Selecting any row opens a detail view:

- A **medication** detail shows Drug, Dosage, Route, Frequency, Status, and the start date.
- A **document** detail shows the full text of the note plus its type and timestamp.
- A **lab/imaging** detail shows the report analysis text (or "No analysis available.").
- The **discharge** detail shows the summary text and the discharge date.

Empty states are explicit: "No medications on record", "No documents on record", "No lab or imaging reports yet", and "No discharge summary available yet."

**Download my record (FHIR)** saves the patient's complete record to their device as a single machine-readable file (named `orb-record-<patient id>.json`). FHIR is the international standard format for health records, so the file can be given to another hospital, a GP, or a personal health app. If the download cannot be prepared the card shows "Could not download your record right now."

Everything in My Records is strictly self-scoped: the server resolves the patient's record from the signed-in account and never accepts a patient identifier from the page, so no configuration mistake can expose another patient's data here.

---

### 12.5 Scheduling a follow-up appointment

The **Schedule Follow-Up** card lets the patient book their own follow-up visit in four quick steps. Booked appointments are real records in the hospital's appointment system, clinicians see them in the patient's chart, and every booking and cancellation is written to the audit log.

#### Booking, step by step

1. **Pick a day** on the week-strip calendar. The strip shows one week (Sunday–Saturday) with the month and year above it. Use the left and right arrow buttons to move a week back or forward. Today's date is highlighted; days in the past and **Sundays** are greyed out and cannot be selected.
2. **Pick a time.** Once a day is chosen, a **Pick a time** row appears with six slots: **9:00 AM, 10:00 AM, 11:00 AM, 2:00 PM, 3:00 PM, 4:00 PM**.
3. **Pick a department.** A **Department** row appears with five options: **Primary**, **Cardio**, **Neuro**, **Ortho**, and **Lab**.
4. **Optionally add a note.** The field **"Note for your doctor (optional)"** accepts a short free-text message, the placeholder suggests "e.g. I'd like to discuss my medication…".
5. Select **Confirm Appointment**. The button shows a spinner while the booking is submitted.

On success the card flips to a confirmation: a green check mark, the word **Confirmed**, the date, time, and department of the booking, and a **New** button that resets the card so another appointment can be booked.

Safeguards built into booking:

- The server rejects dates in the past and malformed dates or times, so a stale page cannot create an invalid booking.
- Each booking carries a one-time idempotency key, so a double-click or a network retry can never create a duplicate appointment.
- Patients can only ever book **for themselves**. (Clinicians with write access can book on a patient's behalf from the staff side; that flow is covered in the Appointments chapter.)

If a booking fails, the card shows the error in red (for example "Could not book your appointment. Please try again.") and returns to the form with all selections intact.

#### Viewing and cancelling upcoming appointments

When the patient has future bookings, the card shows a **"Your upcoming appointments"** list above the calendar: each entry displays the date, time, and appointment type, with a **Cancel** button. Cancelling removes the entry immediately and marks the appointment cancelled on the server (the record is retained for the audit trail, it is a status change, not a deletion). If a cancel fails the card shows "Could not cancel. Please try again." and re-synchronises the list with the server either way. Past and cancelled appointments drop out of the upcoming list automatically.

---

### 12.6 Discharge summaries and post-discharge access

Bridge handles discharge in two ways: inside the signed-in portal, and through a standalone secure link that requires no portal account.

#### Inside the portal

Once the care team completes a discharge, the patient's **Discharge Summary** button in My Records shows the summary with a **Ready** badge and the discharge date. Opening it displays the full summary text.

#### The secure discharge link and QR code

At the moment a clinician discharges a patient, Orb generates a **secure discharge link** and a matching **QR code** that is printed on the discharge paperwork. It gives the patient the official discharge summary on their own phone, without a portal password.

**It opens only inside the hospital.** Orb runs on the hospital's own network and never answers from the internet, so the link works only on a network the hospital routes to Orb, usually a patient Wi-Fi. On mobile data or at home it does not open. That is deliberate: patient records stay in the building. The patient therefore opens the link and **saves a copy** before leaving (see below), or takes the printed summary home.

No link is issued, and the desk is told why, when the patient has no date of birth on file, when the hospital has not set the address patients' phones open, or when the hospital has switched discharge links off because no patient network reaches Orb. In that last case the printed summary is the handover.

Key properties of the link:

- It contains a long random access code unique to this discharge. The hospital stores only a cryptographic fingerprint of the code, never the code itself, so the working link cannot be reconstructed from hospital records.
- It **expires automatically 30 days** after discharge (the period is configurable by the administrator).
- Every access attempt, successful or not, is recorded in the security audit log with the source network address.

#### Opening the link: date-of-birth verification

Scanning the QR code (or typing the link) opens a page titled **Secure Medical Record** with the prompt "Please verify your identity to access your discharge summary."

1. Enter the patient's **Date of Birth** using the date field.
2. Select **Access Records** (the button reads "Verifying..." while it checks).

A notice at the bottom of the card reminds the user: "For your privacy, this link is monitored and will expire automatically."

If the date of birth matches, the page unlocks and displays the record under the hospital's configured name (or **Patient Portal** if none is set) and the title **Patient Portal: Discharge Summary**, with a green **✓ Verified Identity** badge. The document shown is the formal **DISCHARGE SUMMARY**, laid out for reading or printing, including: patient details (name, age/sex, ward and bed, registration and admission numbers, dates of admission and discharge, doctor's name), final diagnosis and management, history and examination findings, the hospital course narrative, the **Discharge Medication** list (each drug with dose, route, and frequency), an optional **Hospital Course Timeline** table of dated events, the follow-up instruction ("Follow up in OPD after 1 week or earlier in emergency"), and the attending physician's signature block.

A successful verification is valid for **one hour**; after that, re-opening the record requires entering the date of birth again.

#### Saving a copy before leaving

Once the doctor has released the summary, a **Save a copy** button sits above the record, with the note that the page only opens on the hospital's Wi-Fi and will not open from home. It opens the phone's print screen: on Android choose **Save as PDF**, on iPhone tap **Share**, then **Save to Files**. The saved PDF holds only the discharge summary, not the page around it, and opens later with no network.

While the summary is still a draft, there is nothing final to save: the page says the summary is being finalised and asks the patient to have the ward staff finish it before they leave, then scan the code again.

#### Wrong answers, lockout, and throttling

The verification step is defended against guessing:

- After **5 incorrect date-of-birth attempts** (the default; configurable), the link is **temporarily locked for 15 minutes** (also configurable). The page states "Too many failed attempts. Access is temporarily locked until <time>." and disables the form. Once the lock time passes, verification can be attempted again.
- The page never reveals how close a guess was or how many attempts remain, an incorrect entry simply reports that the date of birth was incorrect.
- Independently of any single link, repeated attempts from the same network address across *all* discharge links are throttled, so an attacker holding many links cannot sidestep the per-link lockout.
- The date-of-birth comparison itself is performed in constant time, and failed and successful attempts alike are written to the audit log for security review.

Other messages the page can show:

| Message | Meaning | What to do |
|---|---|---|
| "This secure link has expired for your protection. Please contact the hospital for a new link." | The 30-day validity window has passed | Contact the hospital; staff can issue the summary again |
| "This secure link has been revoked. Please contact the hospital." | The hospital has invalidated this link | Contact the hospital |
| "We could not verify your identity. Please ensure you used the correct link." | The link is not recognised (mistyped, incomplete, or throttled) | Re-scan the QR code or re-copy the full link |
| "Your identity was verified, but the clinical record could not be loaded or is empty." | Verification succeeded but no summary is on file | Contact the hospital |

---

### 12.7 Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| Patient can't sign in | Wrong Patient ID or password | Verify the ID format (e.g. `P001`); passwords are required, there is no default. An administrator can reset the account |
| "This account must set up an authenticator app before signing in." | MFA enrollment is required but not completed | An administrator must enroll the account before portal sign-in |
| Status card stuck on "Loading your status…" | The backend is unreachable or the patient record isn't linked to the portal account | Check that the server is running and that the patient row is linked to the login account |
| "We're looking into your records." as the status | The signed-in account has no linked patient record | Link the portal account to the correct patient record |
| Vitals card shows "No recent reading" | No vitals have been recorded for that measure | Expected until monitoring data exists; readings appear automatically |
| Care team card is empty | No attending physician has been assigned | Assign an attending; a ward nurse is shown automatically once a department is known |
| AI answer never appears / connection message shown | Local AI engine is not running or is busy | Confirm the AI engine is up; ask the patient to try again, or answer the question in person |
| Booking fails with a date error | The selected day passed while the page was open | Re-select a current date; past dates are rejected by the server |
| "Download my record (FHIR)" button is greyed out | The record is still loading, or no patient record is linked | Wait for the page to finish loading; verify account-to-patient linkage |
| Discharge link reports "locked" | Five wrong date-of-birth entries | Wait for the stated time (15 minutes by default) and retry with the correct DOB |
| Discharge link "expired" | More than 30 days since discharge | Hospital staff must provide the summary through another channel |

---

### 12.8 Safety & limitations

- **No medical advice.** The Bridge assistant explains and reassures; it does not diagnose, interpret results, or advise on treatment. It is instructed to redirect all clinical decisions to the nurse or doctor, and staff should present it to patients that way.
- **Emergencies bypass the AI.** Messages describing emergencies or self-harm are caught by a rule-based screen before any AI involvement, answered with fixed safety instructions, and escalated to the care team in real time. The portal is **not** an emergency channel, and its fixed responses say so, patients in distress are always directed to the call button, staff, or emergency services.
- **Minimal AI context.** The assistant sees only the patient's name, simplified status, one-line condition, and current vitals, never the full chart. Its answers are therefore general by design; it cannot discuss specific lab values, imaging findings, or notes.
- **Patient-education ranges only.** The normal ranges printed on the vitals card are general guidance bands, not the clinical alarm thresholds the care team uses. The card says as much on screen.
- **Reassurance-first status wording.** Even a Critical status is phrased calmly. Clinicians should be aware that the patient-facing wording deliberately understates urgency; it is not a substitute for a proper conversation about deterioration.
- **Strict self-scoping.** All portal data, status, vitals, records, appointments, downloads, is resolved server-side from the signed-in account. A patient cannot request another patient's data, and staff tools are unreachable from a patient session.
- **Local processing.** Questions and answers are processed on the hospital's own hardware. Nothing typed into Bridge is sent to an external AI service.
- **Discharge links are bearer secrets.** Anyone holding the link *and* the patient's date of birth can read the discharge summary until the link expires. Advise patients to treat the printed QR code like a document containing health information. The design mitigations, hashed storage, 30-day expiry, 5-attempt lockout, cross-link throttling, one-hour sessions, and full audit logging, reduce but do not eliminate this inherent property of shareable links.
- **Booking is a request to attend, not triage.** The scheduler books fixed slots in named departments; it does not check clinician availability conflicts or clinical urgency. Patients needing urgent review should contact the hospital rather than book a routine slot.
