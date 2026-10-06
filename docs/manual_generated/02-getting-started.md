## 2. Getting Started

> **Dated snapshot, `docs/USER_MANUAL.md` is authoritative.** These per-chapter
> files were split from the manual on 2026-07-29 and are not regenerated when the
> manual changes. Verified claim corrections have been back-ported into them, but
> anything else may lag. If this file and `docs/USER_MANUAL.md` disagree, the
> manual is right. Do not circulate these chapters to a buyer without
> re-splitting them from the current manual.

![The clinician sign-in screen. Patients use the same address and are routed to the Bridge portal after signing in.](assets/manual/clinician-login.png)

*The clinician sign-in screen. Patients use the same address and are routed to the Bridge portal after signing in.*

This chapter walks you through your first minutes with Orb: opening the application, signing in as a clinician or as a patient, understanding what your role lets you see and do, and finding your way around the clinician workspace, the sidebar, the header controls, the command palette, and the security features that protect your session. By the end you should be able to sign in, orient yourself on any screen, enroll in two-factor authentication, and sign out safely.

Orb runs entirely on hospital hardware. There is no cloud account to create and no internet connection required to sign in, your credentials, your patients' records, and the AI models all live on the machine in your facility.

### 2.1 Opening Orb and choosing your sign-in

Open a web browser on a workstation connected to the hospital network and go to the Orb address your administrator has given you (on a single-machine installation this is typically the local machine's address). Orb has two separate front doors:

- **Clinician Login**, for doctors, nurses, pharmacists, and administrators. This is the default page you land on.
- **Patient Access**, the Orb Bridge portal, where admitted patients check their own status, records, and appointments.

Both pages share the same look: an animated aurora background, the Orb logo, and a single sign-in card. The card's title tells you which door you are at, "Clinician Login" with the subtitle "Orb Clinical Platform," or "Patient Access" with the subtitle "Orb Bridge." If you are already signed in and revisit the address, Orb skips the login page and takes you straight to your workspace.

### 2.2 Signing in as a clinician

#### The sign-in form

The Clinician Login card has two fields and one button:

1. **Employee ID**, your staff identifier (for example, `DOC001`).
2. **Password**, your account password.
3. Press **Sign In**. The button reads "Signing in…" while Orb verifies your credentials.

On success you land in the clinician workspace, opened to the first tab your role can access (for most clinical roles, the Patients board).

In demonstration and development installations, a **Quick access** section appears below the form with one-click chips for the demo accounts (each chip shows the role and the person's name). These chips are removed from production builds, a live hospital deployment never shows working credentials on the login page.

#### If your account uses two-factor authentication

If two-factor authentication (MFA) is enabled on your account, signing in is a two-step process:

1. Enter your Employee ID and password, then press **Sign In**.
2. The form reveals an **Authenticator code** field with the hint "Enter the 6-digit code from your authenticator app to finish signing in." Open your authenticator app (Google Authenticator, Authy, 1Password, or similar), type the current 6-digit code, and press **Verify code**.

If you started the sign-in by mistake, the **Use a different account** link below the button returns you to the password step. Each authenticator code is single-use: once a code has been accepted it cannot be replayed, so if you need to sign in twice in quick succession, wait for the next code (codes rotate every 30 seconds).

#### Sign-in errors and lockouts

Orb protects accounts against password guessing, and the messages you see tell you exactly what happened:

| Message | Meaning | What to do |
|---|---|---|
| "Invalid credentials" | The ID or password was wrong. | Re-check and try again. |
| "Too many login attempts. Please try again in 15 minutes." | Five failed attempts from your workstation within 15 minutes. | Wait 15 minutes, or use another workstation if urgent. |
| "Account temporarily locked after too many failed attempts. Try again later." | Eight failed attempts against your account (from any workstation). The lock lasts 15 minutes. | Wait, then try again; contact an administrator if you suspect someone else is trying your account. |
| "Invalid authenticator code, check the 6-digit code and try again." | The 6-digit MFA code was wrong or already used. | Wait for the next code in your app and re-enter it. |
| "This account must set up an authenticator app before signing in. Contact your administrator to enroll." | Your role requires MFA but your account has not yet enrolled. | Ask an administrator to complete enrollment for your account. |
| "Account deactivated. Contact an administrator." | Your account has been disabled. | Speak to your administrator. |

Every sign-in attempt, success or failure, including lockouts and MFA failures, is written to Orb's tamper-evident audit log with the time, workstation address, and outcome.

### 2.3 Demo accounts

A freshly seeded demonstration installation includes the following accounts. All clinician demo passwords are `password`; the demo patient's password is `patient123`.

| ID | Name | Role | Department | Password |
|---|---|---|---|---|
| DOC001 | Dr. Meera Sharma | Doctor | General Medicine | `password` |
| NUR001 | Priya Reddy | Nurse | General Medicine | `password` |
| PHA001 | Deepa Joshi | Pharmacist | Pharmacy | `password` |
| ADM001 | Rajesh Kumar | Administrator | Administration | `password` |
| P001 | Raj Patel | Patient (Bridge portal) | n/a | `patient123` |

The seed data also includes additional demo physicians across Cardiology, General Surgery, Orthopedics, Pulmonology, Neurology, and Anesthesiology (for example DOC002, Dr. Arjun Mehta, Cardiology), all with the same demo password. Surgeons, doctors in a Surgery department, such as Dr. Vikram Rathore, see a slightly different tab set (see below).

**These credentials are for evaluation only.** Before any real clinical use, an administrator must replace the demo accounts and passwords through the Admin panel's user management.

### 2.4 Roles and what each role sees

Orb enforces role-based access control (RBAC) on the server: every request is checked against a central permission map, and anything your role does not permit is refused with an "Insufficient permissions" error and recorded in the audit log. The interface mirrors the same map, tabs and buttons you cannot use are simply not shown.

#### What each role can do

| Capability | Doctor | Nurse | Pharmacist | Admin | Patient |
|---|:-:|:-:|:-:|:-:|:-:|
| View any patient record, vitals, and alerts | Yes | Yes | n/a | Yes (read-only) | Own data only |
| Create/edit patient records; prescribe or modify medications | Yes | Medications only | n/a | n/a | n/a |
| Create and edit Scribe draft notes | Yes | Yes | n/a | Read only | n/a |
| Sign or reject Scribe notes | Yes | n/a | n/a | n/a | n/a |
| Query Sage (AI assistant) | Yes | Yes | Yes | Yes | Ask via Bridge |
| Upload and analyze images in Lens | Yes | Yes | n/a | View only | n/a |
| Generate and publish discharge summaries | Yes | View only | n/a | View only | n/a |
| Read and send Relay messages | Yes | Yes | n/a | Yes | n/a |
| Execute Relay AI action cards | Yes | n/a | n/a | n/a | n/a |
| View Forecast and surgical schedule; surgery prep and management | Yes | Yes | n/a | Yes | n/a |
| Create lab/imaging/procedure orders (CPOE) | Yes | Yes | n/a | View only | n/a |
| Pharmacy (Helix): view inventory, verify orders, adjust stock | n/a | n/a | Yes | Yes | n/a |
| Governance: audit log, correlation tracing, ingestion monitor, admin dashboard | n/a | n/a | n/a | Yes | n/a |

Two practical consequences are worth calling out:

- **Nurses can draft but not sign.** A nurse can transcribe and structure a clinical note in Scribe, but only a doctor can sign it into the record (or reject it).
- **Admins observe; they do not treat.** The administrator role reads clinical data for governance purposes but cannot prescribe, order, sign notes, or publish discharges.

#### Sidebar tabs by role

The sidebar shows only the modules your role can use:

| Role | Sidebar tabs (in order) |
|---|---|
| Doctor | Patients, Orders, Messages, Sage, Scribe, Lens, Forecast, Command, Simulator, Revenue, Appointments |
| Doctor (Surgery department) | Same, but **Surgical Suite** replaces Appointments |
| Nurse | Patients, Orders, Messages, Sage, Scribe, Lens, Forecast, Revenue, Appointments |
| Administrator | Patients, Messages, Sage, Forecast, Command, Simulator, Revenue, Appointments, Admin |
| Pharmacist | Pharmacy, Verify Queue, Sage |

Each tab belongs to a named Orb "pillar", hover over a tab to see it (for example, hovering Patients shows "Patients · Orb Vigil"). The pillar name also appears as the page title in the header when the tab is open.

### 2.5 A tour of the clinician shell

Once signed in, every clinician works inside the same three-part layout: a sidebar on the left, a header across the top, and the active module filling the rest of the screen.

#### The sidebar

From top to bottom:

- **Orb logo.**
- **Navigation tabs**, one per module, each with an icon and label. Click a tab to switch modules. The tabs are fully keyboard-accessible: with focus on the navigation, the Up/Down (or Left/Right) arrow keys move between tabs, and Home/End jump to the first or last tab.
- **Admit Patient**, a prominent button that opens the patient admission form from anywhere in the app (see the Admissions chapter). Pharmacists do not see this button.
- **Your user card**, your avatar initial, name, and role · department (for example, "Physician · General Medicine"). The small lock icon on this card is the **Security & MFA** button, which opens two-factor authentication management (Section 2.8).
- **Sign Out** (Section 2.9).

Modules keep their state while you work: once you have visited a tab, switching away and back does not lose your place, an open patient chart, a half-written message, or a running analysis is still there.

#### The header

The left side of the header names the active module ("Orb Vigil," "Orb Scribe," and so on) with a one-line description beneath it. The right side holds, in order:

- **Sync status badge** (only when relevant), see "Working offline" below.
- **Clock**, the current date and a live clock with seconds, useful when timing observations or documenting in real time.
- **Pulse**, a heartbeat-line icon that opens the Pulse panel: the season in your hospital's state and what usually rises with it (next section).
- **Notifications bell**, controls browser notifications for critical alerts. On first click it asks the browser for notification permission; once granted, clicking toggles the alert chime between muted and unmuted (the tooltip always states the current state, e.g. "Critical-alert sound on, click to mute"). If notifications are blocked at the browser level, the button is disabled and its tooltip explains that they must be re-enabled in your browser settings.
- **Language toggle**, switches Orb's *navigation chrome* and the sign-in screen between English and Hindi. The button shows "हि" when you are in English (click for Hindi) and "EN" when you are in Hindi. **This is not a translated product:** Hindi covers 65 strings (sidebar tabs, header subtitles, theme/offline/role labels, the login form). All clinical module content remains English.
- **Theme toggle**, cycles through three themes: **Dark → Light → Night Shift → Dark**. Night Shift is a high-contrast, low-brightness theme designed for overnight wards and operating-room ambience. The button's tooltip names the theme you will switch *to*. Your choice is remembered on that workstation; if you never choose, Orb follows the operating system's light/dark preference.

#### The Pulse panel

**Pulse** shows the season for your hospital's state: what usually rises at this time of year, and what is coming up. Click the heartbeat-line icon to open it; the panel names the state and today's date at the top.

- **This season**, one card for each season in effect now: its name, when it usually runs, a **Peak now** tag at its peak, a short summary, a "Typically rises: ..." line, and the sources it was drawn from, named in plain text.
- **Coming up**, entries that start in the next 60 days ("In 12 days: Dengue season").
- **Review status**, "Draft: not yet reviewed by a clinician" until the hospital's clinical lead has reviewed the calendar, then "Reviewed by [name] on [date]".

Pulse takes the state from the hospital's site settings, never from your browser's location, and it makes no internet connection. If the state has not been set, Pulse asks for it instead of guessing. Close the panel with the **X**, Escape, or a click outside it. Section 14.1 has the details.

#### Working offline

Orb is built to keep working when the network drops. When that happens, a status badge appears in the header:

- **"Offline Mode: Caching locally"**, you are offline; reads come from the local cache and your actions are queued.
- **"Offline · N pending"**, N actions are queued and will be sent when the connection returns.
- **"Syncing · N pending"**, the connection is back and the queue is draining.
- **"Synced"** with a check mark, everything queued has been delivered (shown briefly).
- A red warning, "N queued action(s) failed to sync, please re-enter", means an action queued offline could no longer be applied when it reached the server (for example, the order it targeted had already been completed). **That action did not take effect**: review and re-enter it, then dismiss the notice with the ✕ button.

#### The workspace

The rest of the screen belongs to the active module. If a single panel hits an error, it fails in place, you will see "This panel hit an error and couldn't be shown" with a **Reload panel** button, while the rest of Orb keeps running. Immediately after a software update you may briefly see "Updating to the latest version…" as the page refreshes itself; this is routine.

### 2.6 The command palette (Cmd-K)

The fastest way to reach any patient from anywhere in the clinician shell:

1. Press **Cmd-K** (macOS) or **Ctrl-K**.
2. A search box opens: "Search patients by name, MRN, bed, or diagnosis…". Type at least two characters.
3. Results appear as you type, each showing a risk-colored dot (red = critical, amber = elevated, green = stable), the patient's name and diagnosis, their MRN, and their bed.
4. Use the Up/Down arrow keys (or the mouse) to highlight a result and press **Enter** (or click) to open it. Orb switches to the Patients tab and opens that patient's record.
5. Press **Esc** (or click outside the box) to close without selecting.

If nothing matches, the palette says so ("No admitted patients match …"). The search covers currently admitted patients.

### 2.7 When your session ends: the re-authentication overlay

Clinician sessions are valid for up to 24 hours, and two situations interrupt one sooner:

- **Session expired**, your sign-in token is no longer valid (it aged out, or your sessions were revoked, for example by signing out on another device). A blocking overlay appears titled **"Session expired"**: "For your security, your session has ended. Re-enter your password to continue as *your name*."
- **Screen locked**, you were inactive for 15 minutes at the workstation. The same overlay appears titled **"Screen locked"**: "This workstation locked after a period of inactivity. Re-enter your password to continue as *your name*."

In both cases the overlay works the same way:

1. Type your **Password** and press **Continue** ("Verifying…" while it checks). On success the overlay disappears and you resume exactly where you were, nothing you were working on is lost.
2. Or press **Sign out** to end the session and return to the login page (for instance, if a colleague needs the workstation).

The overlay is deliberately inescapable: pressing Escape does nothing, and no other part of the screen is clickable until you re-authenticate or sign out. If your account uses MFA, password-only re-entry is not sufficient; the overlay will tell you: "Your account requires MFA, sign out and log in again with your authenticator code."

The idle lock applies to staff only, the patient Bridge portal is not idle-locked.

### 2.8 Security & MFA enrollment

Two-factor authentication adds a second proof of identity, a rotating 6-digit code from an authenticator app on your phone, on top of your password. Orb strongly recommends it for every clinical account, and administrators can make it mandatory for chosen roles.

#### Enabling two-factor authentication

1. In the sidebar, click the **lock icon** on your user card (tooltip: "Security & MFA"). The **"Set up two-factor authentication"** dialog opens.
2. Follow the on-screen steps:
   - Open your authenticator app (Google Authenticator, Authy, 1Password, or any standard TOTP app).
   - Add a new account using **manual key entry** and paste the **Setup key** shown in the dialog (a **Copy** button is provided). Alternatively, copy the full **Provisioning URI** into apps that accept it. The dialog confirms the technical parameters: account "Orb Hospital OS," algorithm SHA1, 6 digits, 30-second period.
   - Your app now shows a rotating 6-digit code for Orb.
3. Type the current code into the **Verification code** field and press **Enable MFA** ("Verifying…" while it checks). This proves your app is set up correctly, MFA is not switched on until a live code is confirmed, so a half-finished setup can never lock you out.
4. On success the dialog shows **"✓ Two-factor authentication is now enabled"** and reminds you: "You'll be asked for a code from your authenticator app each time you sign in." Press **Done**.

There is no QR code in the current version, use your app's manual/setup-key entry option. If you close the dialog before confirming and open it again later, a fresh setup key is generated; always use the key currently on screen.

#### Disabling two-factor authentication

Open the same lock icon. If MFA is already enabled, the dialog (titled **"Two-factor authentication"**) switches to the disable flow. As a deliberate safeguard, turning MFA off requires **both** your current **Password** and a current **Authenticator code**; enter them and press **Disable MFA**. Enabling and disabling MFA are both recorded in the audit log.

### 2.9 Signing out

Click **Sign Out** at the bottom of the sidebar. Signing out in Orb is deliberately thorough: it revokes **all** of your active sessions, on this workstation and every other device where you were signed in. This is the correct response if you ever suspect your account has been used by someone else, sign out once, and every outstanding session dies. The sign-out is audit-logged.

On shared ward workstations, prefer signing out over walking away; the 15-minute idle lock is a safety net, not a substitute.

### 2.10 The patient's Bridge portal

Patients have their own, deliberately separate entrance: **Orb Bridge**.

1. Go to the Patient Access page (your care team or discharge paperwork provides the address; from the clinician login it is the patient-mode login page).
2. The card is titled **"Patient Access"** with the subtitle "Orb Bridge." Enter your **Patient ID** (for example, `P001`) and your **Password**, then press **Sign In**. In demo installations, a Quick access chip signs in the demo patient Raj Patel.
3. You arrive at the Bridge home: a header with the Orb logo, "Orb Bridge," a personal greeting, and a **Sign out** button; below it, your **Your Status** card, a **Schedule Follow-Up** card for booking appointments, **My Records** (medications, clinical reports, labs, discharge summary, and a downloadable copy of your record in standard FHIR format), and a question box where you can ask Orb about your care in plain language.

The same account-security rules apply to patients as to staff: wrong credentials produce "Invalid credentials," and repeated failures trigger the same temporary lockouts. A patient account can only ever see that patient's own information, this is enforced on the server for every single request, not just hidden in the interface. The Bridge portal is covered in full in its own chapter.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Invalid credentials" with a password you are sure of | Wrong ID field (e.g., Patient ID on the clinician page), stray spaces, or wrong keyboard layout | Confirm you are on the right login page (Clinician Login vs. Patient Access) and re-type both fields. |
| "Too many login attempts…" or "Account temporarily locked…" | Rate limiting after repeated failures | Wait 15 minutes. If you did not cause the failures, tell an administrator, the audit log shows where the attempts came from. |
| Authenticator codes are always "invalid" | The phone's clock has drifted | Codes are time-based (30-second window). Set the phone's clock to automatic/network time and try the next code. A code also fails if it was already used once, wait for the next one. |
| MFA-required account cannot sign in at all | Role requires MFA but the account never enrolled | An administrator must complete enrollment; the login page cannot enroll you. |
| "Session expired" appears immediately after signing in, repeatedly | Workstation clock badly wrong, or sessions being revoked elsewhere | Correct the workstation's date/time; if it persists, contact your administrator. |
| Quick access chips are missing on the login page | You are on a production build | Expected, production builds never show demo credentials. Sign in with your own account. |
| A tab you expect (e.g., Orders, Admin) is not in the sidebar | Your role does not include it | See the role table in Section 2.4. Access changes must be made by an administrator. |
| The notifications bell is grayed out | Browser-level notification block | Re-enable notifications for the Orb site in the browser's settings, then refocus the page. |
| Cmd-K / Ctrl-K does nothing | You are on the login page or the Bridge portal | The command palette exists only inside the clinician shell. |
| A red "failed to sync" badge appears in the header | An action queued while offline could not be applied | That action did not take effect. Re-enter it manually, then dismiss the badge with ✕. |

### Safety & limitations

- **Least privilege, enforced twice.** Permissions are enforced on the server for every request, independent of what the interface shows. Hiding a button is a courtesy; the server-side check is the guarantee. Denied attempts are audit-logged.
- **Everything is audited.** Sign-ins and failures, lockouts, MFA enrollment and removal, sign-outs, and permission denials all land in the tamper-evident audit log with time, actor, and workstation address (see the Admin chapter).
- **Your password is never stored.** Orb keeps only a strong one-way hash. Sessions live in a protected browser cookie rather than in scripts' reach, and the token is never written to browser storage.
- **Sign-out is global.** One Sign Out revokes every session on every device. There is no "sign out of this device only."
- **MFA has no self-service recovery.** If you lose the phone holding your authenticator app, an administrator must reset MFA on your account. There are no backup codes in the current version.
- **Demo credentials are a known risk.** The seeded accounts and passwords in Section 2.3 are public knowledge. Never expose an installation that still has them beyond a demo network.
- **Orb makes no internet connection.** Every part of Orb, Pulse included, works with no internet access at all. Pulse reads a seasonal calendar stored on the appliance; it fetches nothing and sends nothing. Its cards are seasonal context, not clinical directives; they never replace your judgment.
- **The idle lock is a fallback.** Fifteen minutes is a long time in a busy corridor. Sign out, or lock the workstation, when you step away.
