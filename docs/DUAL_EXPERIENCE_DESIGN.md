# Dual Doctor/Nurse Experience: Design Proposal

*Drafted 2026-08-10 for Samuel's review. No code has been written against this
document. Ruling being executed: dual experiences start now, in parallel with
pilot work, but per the roadmap, deep divergence remains post-pilot
(`docs/PILOT_ROADMAP.md`, "Post-window"), so every phase below is sized against
the Week-13 hard date and says explicitly when it competes with it.*

**What already exists, stated honestly:** the pilot's "dedicated nurse mode" is
built and code-complete, `src/pages/panels/nurse/NursePanel.tsx` is a genuinely
task-shaped home (Due now / My patients / Handover / End of shift) with
`ObsEntrySheet`, `MedRoundSheet`, `HandoverSheet` and `OutcomeCaptureSheet`
behind it. But it is a **tab inside a shared shell**, one of ten tabs a nurse
sees, composed by role flags in `ClinicianShell.tsx:getAccessibleTabs()`. This
document is therefore not "build nurse mode". It is: **graduate two experiences
out of one flag-composed shell without forking the shell**, and give doctors
the composed home they never got.

---

## 1. The case for divergence

### 1.1 What each role actually does per shift

**A ward nurse** works a queue of timed obligations across a bay, under
constant interruption: obs due on an escalation schedule, doses due against
order frequencies, an I-PASS handover at shift end, and (in this pilot) the
end-of-shift outcome confirmation that is the evidence instrument's ground
truth. Her questions are *"who is going off?"*, *"what do I owe and how late is
it?"*, *"how do I hand over?"*, the exact framing already written at the top of
`NursePanel.tsx:19-36`. She works standing, one-handed, on a shared bedside
tablet, sometimes gloved.

**A ward doctor** makes decisions against data: who is deteriorating since I
last looked, what needs my signature or acknowledgment, what results came back,
what do I ask Sage, and, for roughly one hour of the day, the ward round.
`RoundPanel.tsx:1-31` states the doctor's constraint precisely: "a tablet in
one hand and roughly 90 seconds per patient." The rest of the day is desk and
corridor work across roster, orders, notes, and messages.

One is **task-shaped** (a queue with deadlines), the other **decision-shaped**
(a triage over evidence). The codebase already knows this, the comment block
in `NursePanel.tsx` says it in as many words, but only the nurse side ever got
a home built around its shape.

### 1.2 Where one-shell-with-flags fails the nurse (concrete screens)

1. **Ten tabs, four of which matter on shift.** `getAccessibleTabs('nurse')`
   (`ClinicianShell.tsx:200`) returns `shift, vigil, orders, relay, sage,
   scribe, lens, forecast, coding, appointments`. Revenue Integrity (`coding`)
   and Forecast sit in the rail with the same visual weight as My Shift. On a
   tablet at ≤1024px the rail collapses to icon-only 44px targets
   (`ClinicianShell.css:679-740`) whose labels are `title` attributes, 
   **hover-only affordances, unreachable on touch** (the exact failure class
   the 2026-07-30 audit charged 181/372 findings to, per the design-direction
   record). A nurse on a bedside tablet navigates ten unlabeled icons.
2. **The rail's one primary action is a 403 for her.** The "Admit patient" CTA
   renders for every non-pharmacist role (`ClinicianShell.tsx:572`), but
   `POST /api/admissions/admit` requires `patient.write`
   (`admission_routes.py:57`), which nurses (and admins) do not hold
   (`permissions.py:117-134`). The flag gate (`role !== 'pharmacist'`) drifted
   from the permission model. This is the signature failure mode of
   flag-composition: two sources of truth about what a role can do.
3. **Patient lookup lands her in a prescriber's chart.** The "My patients" card
   says "one tab over" for lookups, but that tab is `VigilPanel`'s detail
   view: add-medication (prescribing), discharge, forecast, timeline
   projection (`VigilPanel.tsx`, ~2,400 lines of doctor-shaped chart). She
   holds `MEDICATION_WRITE`, so the server permits a prescription from that
   modal; nothing but UI shape steers her toward the guarded eMAR path that
   `MedRoundSheet.tsx:11-24` so carefully preserves.
4. **The shell frame carries no nurse state.** Overdue counts live inside
   NursePanel's totals strip. When she is on Relay or Scribe, nothing in the
   frame says obs went overdue, the task-shaped home only works while she is
   looking at it. (The doctor's equivalent, critical alerts, got a global
   mechanism: `initCriticalAlerts()` at `ClinicianShell.tsx:361`.)

### 1.3 Where one-shell-with-flags fails the doctor (concrete screens)

1. **No home at all, the shell can only pick a panel.** The doctor lands on
   the Vigil roster, and `getAccessibleTabs` itself documents the compromise
   (`ClinicianShell.tsx:190-196`): Ward Round sits second "because a doctor is
   not always rounding… move 'round' ahead of 'vigil' if the ward wants it to
   open the app." A single ordered tab list cannot answer "what does a doctor
   need first?" because the answer is a *composition*: deteriorating patients
   + pending signatures + new results + Sage, four tabs' worth of first
   minute. The pieces all exist: per-bed unseen counts (`VigilPanel.tsx:99-112`,
   `GET /chart/unseen`), the sign queue (`ScribePanel.tsx` with `signScribeNote`
   permission-gated to doctors), round resumption state
   (`components/round/roundQueue.ts` persists progress per day), Sage. No
   screen composes them.
2. **Ops surfaces intermixed with clinical.** Command Center, the What-If
   Simulator and Revenue Integrity share the rail with Patients and Orders, 
   twelve tabs. The same dilution the nurse gets, in the other direction.
3. **A doctor holding `OUTCOME_RECORD` has no path to use it.**
   `permissions.py:113-115` grants doctors `outcome.record` explicitly "for
   the site that names the ward doctor as the shift's one person", but
   `OutcomeCaptureSheet` renders only inside NursePanel (`NursePanel.tsx:388`),
   and doctors have no `shift` tab. The permission model planned for a
   configuration the UI composition cannot serve. Flag-drift again, mirrored.

### 1.4 What divergence is NOT

Both roles keep the same data, the same server truth, the same ward scope
(`permissions.department_scope`, one derivation, `permissions.py:195-222`),
the same design language, the same offline machinery, and mostly the same
panels. The divergence is in **homes, navigation composition, and the shape of
the patient-lookup surface**, not in forked features.

---

## 2. Information architecture

### 2.1 Nurse experience: task-shaped (mostly exists; changes are composition)

```
NURSE HOME  = "My shift"            ← landing, exists today (NursePanel)
├── Due now                          flat cross-patient queue: obs + doses,
│                                    deterioration-then-lateness sort (exists)
├── My patients                      ward cards → taps open NURSE-SHAPED
│                                    patient sheet (Phase 2, today this is a
│                                    card with Record obs / Med round actions;
│                                    deep lookup detours into Vigil)
├── Handover                         I-PASS across the list (exists)
└── End of shift                     outcome capture / no-events confirm (exists)

RAIL (trimmed): My Shift · Patients · Messages · Sage · Scribe · [More ▸]
                                     "More" holds Orders, Lens, Forecast,
                                     Appointments, reachable, not ambient.
                                     `coding` leaves the nurse set (see §7 Q1).
```

The nurse-shaped patient sheet (Phase 2) is a **read-and-act** surface, not a
chart: identity + allergies + NEWS2 trend + obs history + eMAR state + latest
plan/handover note, with exactly two write affordances, Record obs, Med round
both routing into the existing guarded sheets. No prescribing, no discharge,
no forecast controls. It replaces "lookup = Vigil detail" for nurses; Vigil
stays reachable from More for the rare deep read.

### 2.2 Doctor experience: decision-shaped (new home, existing data)

```
DOCTOR HOME = "Today"               ← new landing tab (Phase 1)
├── Deteriorating                    top slice of the Vigil board (stability
│                                    sort exists), NEWS2 + level + unseen badge;
│                                    tap → orb:goto-tab(vigil) + open-patient
├── Pending my decision              Scribe drafts awaiting sign · unresolved
│                                    safety acks · round not finished ("7/14
│                                    seen, resume"), each deep-links to the
│                                    owning panel
├── New results                      aggregate of /chart/unseen counts, by bed
└── Ask Sage                         composer inline; submit → sage tab with
                                     the question carried over

RAIL (regrouped): Today · Patients · Ward Round · Orders · Messages · Sage ·
                  Scribe · [Clinical ▸ Lens, Forecast, Appointments]
                  [Hospital ▸ Command, Simulator, Revenue]
                  (Surgical Suite stays department-gated as today)
```

Everything on the doctor home is a **read composition over existing endpoints**
plus navigation through the existing bus (`gotoTab`/`onOpenPatient`,
`lib/orbBus`). It creates zero new write paths and zero new server surface, 
that is what makes it safe in the pilot window.

### 2.3 Screen-by-screen map: shared vs divergent

| Surface | Doctor | Nurse | Verdict |
|---|---|---|---|
| Home | **Today** (new, Phase 1) | **My Shift** (exists) | Divergent by design, the point of this doc |
| Patient board (Vigil roster) | Primary triage surface | Lookup, one tap from home | Shared panel, divergent prominence |
| Patient detail | Vigil chart (full, prescribing) | **Nurse patient sheet** (Phase 2) | Divergent, the highest-value split after homes |
| Ward Round | Exclusive (`round`) |, (her round IS the shift board, `NursePanel.tsx:131`) | Doctor-only, as today |
| Obs entry / eMAR / I-PASS / Outcome capture | Outcome capture only, via home block when `outcome.record` held (fix §1.3.3) | Exclusive today | Nurse-owned; doctor gets the one instrument the permission model promised |
| Orders (CPOE) | Full create + state machine | Task-shaped read + nursing orders (see §7 Q4) | Shared panel now; divergence is a governance question, not a UI one |
| Relay | Shared | Shared + phone-hardened (roadmap commitment) | Identical panel |
| Sage | Shared | Shared | Identical, trust features (citations, TTFT) are role-agnostic |
| Scribe | Create/edit/**sign** | Create/edit (sign gate exists server-side) | Shared; affordances already permission-shaped |
| Lens, Forecast, Appointments | Rail group | "More" group | Shared panels, divergent placement |
| Command, Simulator, Revenue | "Hospital" group | Not in nurse set | Doctor/admin-only |
| Admit patient CTA | Shown (holds `patient.write`) | **Hidden** (does not) | Permission-driven, not role-flag (§5) |
| Shell frame: offline/sync badges, theme, i18n, MFA, error boundaries, command palette, shortcuts | Shared | Shared | One implementation, always |

---

## 3. Navigation and shell: one shell, role-composed: recommended

### The options

**A. Two shells** (`ClinicianShell` + `NurseShell`): cleanest conceptual split,
each frame free to take its own layout decisions (e.g. bottom tab bar for the
nurse tablet).

**B. One shell, role-composed** (recommended): `ClinicianShell` stays the only
frame; what varies per role becomes **declarative config**, tabs (grouped),
home component, header widget set, primary CTA, instead of inline flags.

### Why B

1. **The style-drift risk is documented, not hypothetical.** The `.shell__body`
   comment (`ClinicianShell.css:521-536`) records what happened last time
   surface ownership fragmented: seven panels each invented their own gutter
   and the content origin wandered by up to 39px per tab. And the shared
   primitives (`.orb-panel`, `.orb-section`, `.orb-metric-card`, sort chips)
   live in `ClinicianShell.css` *specifically because* "no shared primitive
   belongs in a lazily-loaded chunk" (`NursePanel.css:1-16`). A second shell
   either duplicates that file (drift by copy) or imports it (a shell that
   isn't actually separate, the worst of both).
2. **The shell carries hard-won invariants that must not exist twice:** the
   offline queue/sync-failed badge machinery (`ClinicianShell.tsx:469-533`),
   the stale-chunk one-shot reload (`:220-273`), the theme-flip repaint fix
   (`:377-425`, a bug that took real instrumentation to find), the
   RBAC-filtered navigation bus where shortcuts and deep links go through one
   door (`:455-467`), per-panel error boundaries, MFA enrolment, critical
   alerts init. Plus its attack tests (`ClinicianShell.replay.attack.test.tsx`,
   `ClinicianShell.userchip.test.tsx`). Two shells = two copies of each, and
   two copies of every future fix.
3. **The role differences ARE composition.** `getAccessibleTabs` already
   expresses per-role tab sets; the pharmacist (3 tabs, no admit CTA) proves
   the shell can serve radically different experiences today. The failures in
   §1 are not "the shell can't diverge", they are "the divergence is expressed
   as scattered flags instead of one declarative config."

### The mechanism

One new file, `src/pages/shellConfig.tsx` (Phase 1):

```ts
interface ShellRoleConfig {
  home: Tab                       // landing tab id
  tabs: TabGroup[]                // ordered, grouped (groups render as
                                  // separators on desktop, "More" on tablet)
  headerWidgets: WidgetId[]       // e.g. nurse drops PulseWidget, gains
                                  // an overdue-count chip (Phase 2)
  primaryAction?: 'admit'         // present only if the role's permission
                                  // mirror includes patient.write
}
```

`ClinicianShell` consumes the config and loses its inline role conditionals.
`getAccessibleTabs` becomes derived from the config (single source). Behavior
today is reproduced exactly by the initial config, the refactor is
observable-behavior-neutral and provable by the existing shell tests.

### Honest tradeoffs of B

- The nurse experience inherits the shell's frame decisions (side rail /
  top-bar responsive transform). A dedicated bottom tab bar, genuinely better
  for one-handed tablets, is deferred to Phase 3, and if Phase 3 concludes the
  frames must truly differ, the config extraction is exactly the seam a real
  `NurseShell` would be cut along. Nothing in B is thrown away by a later A.
- Risk of `if (role)` re-accreting inside the shell body. Mitigation: the
  config is the *only* role input to the shell render; review rule that
  `user.role` may not appear in ClinicianShell JSX outside config lookup.

---

## 4. Tablet-first rules for the nurse experience

Already tokenised and enforced: `--tap-min: 44px` under
`@media (pointer: coarse)` (`src/index.css:356-368`, one rule, desktop stays
dense at the 24px WCAG 2.2 floor), no-opacity de-emphasis, status text/fill
token split, full-height sheets on phones (`NursePanel.css:1108`), numeric
`inputMode` on obs fields (`ObsEntrySheet.tsx`). The rules below are what
divergence **adds**, to be written into DESIGN.md when Phase 2 lands:

1. **No hover-only anything in the nurse lane, including `title`.** The
   icon-only rail's `title` labels (§1.2.1) are invisible on touch. Nurse rail
   items keep **visible text labels at every width** (5 trimmed tabs make this
   affordable; 10 did not). This generalises the Sage voice-toggle lesson from
   the 2026-07-30 audit.
2. **Primary actions live in the thumb zone.** Sheets (`ObsEntrySheet`,
   `MedRoundSheet`) pin their save/confirm bar to the sheet bottom, sticky
   above the on-screen keyboard (`visualViewport`-aware when the keyboard
   overlaps). Destructive/secondary actions go top, away from the resting
   thumb.
3. **Tap is the only required gesture.** No swipe-to-act, no long-press-only
   paths, no drag except scroll, gloves and screen protectors defeat all
   three. (Current nurse surfaces comply; make it a stated rule so future
   surfaces do.)
4. **Segmented buttons, never `<select>`,** for ≤6-option choices (ACVPU and
   oxygen already do this, codify it).
5. **One sheet at a time.** Portal sheets are full-screen at coarse pointer;
   a sheet never opens a second modal layer except the existing guarded
   `AdministerModal` interlock, which is the point of the flow.
6. **Cached data always wears its age.** The `useOfflineSnapshot` + banner
   pattern (board passed *through* the snapshot so a cached copy cannot render
   unbannered, `NursePanel.tsx:137-141`, `MedRoundSheet` props comment) is
   mandatory for every new nurse surface, stated as a rule.
7. **Interruption-proof by default.** Any nurse input surface mirrors drafts
   to session-scoped storage (`lib/nurseDrafts` owner-stamped pattern, shared
   tablet, PHI must not survive the session) and restores on reopen.
8. **Density floor:** clinical numbers in `--font-mono`/`tabular-nums` (global
   rule exists); body text never below `--text-sm` on coarse pointer; row
   height follows `--tap-min`, not content.
9. **Detection stays `pointer: coarse`**, never user-agent sniffing, never a
   "tablet mode" toggle. One mechanism, already proven.

Phone (roadmap: "phone-hardened Relay" in scope): Relay and the read-only shift
board are the phone surfaces; obs entry and eMAR remain tablet/desktop-primary
(see §7 Q6).

---

## 5. RBAC implications

The good news: **Phases 1–2 need zero new permissions.** Nurses already hold
everything their experience writes, `vitals.write` (ObsEntry),
`medication.write` exercised through the eMAR guards (MedRound),
`outcome.record` (OutcomeCapture, the blinded pilot instrument), `order.read`/
`order.write`, `relay.send`, Scribe create/edit. Doctor-home blocks compose
reads the doctor role already has. Divergence is presentation.

What the design surfaces, in order of importance:

1. **UI gating must key off permissions, not role names.** The admit-CTA
   defect (§1.2.2) and the doctor/`outcome.record` dead-end (§1.3.3) are both
   role-flag drift. `lib/roles.ts` already states the correct doctrine ("THE
   SERVER IS AUTHORITATIVE… a mirror… only to avoid firing a request we
   already know will be refused") but mirrors exactly one permission. Phase 1
   extends it to a full client-side mirror of `ROLE_PERMISSIONS` (or better, a
   `GET /api/me/permissions` echo so the mirror cannot rot), and `shellConfig`
   consumes the mirror: the admit CTA appears iff `patient.write`; the
   outcome-capture home block appears iff `outcome.record`. One doctrine, no
   per-feature role lists.
2. **The outcome-capture blinding boundary must survive composition.**
   `permissions.py:83-89` and `OutcomeCaptureSheet.tsx`'s header comment are
   explicit: the recorder must never see scorecard numbers. When the doctor
   home gains an outcome-capture block (fixing §1.3.3), it inherits the same
   rule, the home may show confirmation state and owed shifts, never rates.
   Scorecard reads stay behind `ADMIN_OVERVIEW`.
3. **`medication.write` conflates prescribe and administer.** The nurse-shaped
   patient sheet (Phase 2) closes the UI hole (no prescribing affordance in the
   nurse lookup path), but the server still accepts a nurse-authored
   prescription via the Vigil modal's endpoint. A
   `medication.prescribe` / `medication.administer` split is the honest fix, 
   **Phase 3, server-side, out of this doc's scope** (flagged as a follow-up;
   touching the permission model in the pilot window is risk without pilot
   payoff, since the eMAR guards already police administration).
4. **Nurse `order.write` breadth is a clinical-governance question, not a
   permission bug.** Whether ward nurses place non-nursing orders (verbal-order
   workflows are common in Indian wards) is the champion's call, §7 Q4. The
   UI can scope the nurse Orders view to nursing/task orders without touching
   the permission.
5. **Every new read endpoint follows the scope law.** The nurse patient sheet
   and any doctor-home aggregate reuse `department_scope` /
   `require_patient_access` and the fail-closed `phi_access` audit, the
   NUR001 lesson in `permissions.py:203-207` (a second, panel-local idea of
   scope is how the last leak happened) applies verbatim to home composition
   endpoints, which is a reason to prefer composing *existing* endpoints
   client-side (Phase 1 does exactly this) over minting aggregate ones.
6. **No new roles for the pilot.** Charge nurse remains an SOP designation on
   the existing `nurse` role, `outcome.record` is already granted role-wide
   and the "one named person per shift" rule is procedural. Revisit post-pilot
   (§7 Q5).

---

## 6. Phased build plan (engineering lane, alongside Week-13 work)

**Standing constraint for every phase:** nothing here may touch the pilot's
critical surfaces, shadow instrumentation, outcome capture writes, eMAR write
paths, offline queue machinery, auth. Phase 1 is deliberately shaped so its
riskiest change is a behavior-neutral refactor guarded by existing tests. Each
phase exits through the roadmap's discipline: targeted tests proven able to
fail, visual gate in all three themes, and the multi-agent audit before any
phase-exit that coincides with a roadmap phase exit.

### Phase 1: Composable homes (now → ~Week 5 · low risk · parallel-safe)

| Item | Size | Risk notes |
|---|---|---|
| `shellConfig.tsx` extraction, declarative per-role tabs/home/widgets/CTA; `getAccessibleTabs` derived from it; zero visual change | S | Provable by existing shell tests staying green + a config unit test. The seam everything later hangs on. |
| Permission mirror + permission-driven CTA/blocks (fixes admit-403; `lib/roles.ts` extension) | S | Removes a live defect; no server change (mirror), or one trivial echo endpoint. |
| **DoctorHomePanel ("Today")**, lazy panel composing vigil board slice, `/chart/unseen`, Scribe pending-sign, round resume state, Sage entry; deep-links via existing bus | M | New lazy chunk; cannot destabilize others (panel error boundary). Reads only. The largest Phase-1 item. |
| Outcome-capture block on doctor home behind `outcome.record` (fixes §1.3.3) | S | Reuses `OutcomeCaptureSheet` as-is; blinding rule §5.2. |
| Nurse rail trim to 5 labeled tabs + "More" group (config-driven) | S | Needs Samuel's Q1 answer first. Visual gate re-run. |

**Capacity warning:** the doctor home changes the doctor's landing screen in
the same window that produces Phase-4 training materials and the champion's
first access. Land it **before** training collateral is written (Weeks 9–10),
and keep it behind a config default that can flip back to `vigil` per
deployment, training must never describe a screen that moved last week.

### Phase 2: Divergent navigation & the nurse patient sheet (~Weeks 6–10 · medium · individually droppable)

| Item | Size | Risk notes |
|---|---|---|
| **Nurse-shaped patient sheet** (read-and-act lookup, §2.1) | M–L | The one genuinely new PHI surface: needs scope tests (`department_scope` reuse), fail-closed read audit, offline snapshot pattern, visual gate ×3 themes. |
| Grouped doctor rail (Clinical / Hospital groups) | S | Config-only once Phase 1 lands. |
| Per-role header widgets, nurse overdue-count chip in the frame (fixes §1.2.4) | M | Needs a cheap polled count (the shift board is already fetched; lift the total, don't add an endpoint). |
| Tablet rules codified into DESIGN.md + gate additions (visible-label rule, thumb-zone rule) | S | Docs + checker, no product risk. |
| Phone-hardened Relay pass | M | Already a roadmap commitment; listed here so the lane plans it once, not twice. |

**Capacity warning, this phase competes with the hard date.** Weeks 6–10 hold
the monitor bench test (Week 6), on-ward test (Week 8), and dress rehearsal
(Weeks 9–10). Phase 2 items are ordered so each is **independently droppable**;
the nurse patient sheet is the first thing to drop, nurses can pilot on
Vigil-detail lookup (they hold the permissions; it is shaped wrong, not
unsafe), and shipping it *after* training would mean retraining. Decision rule:
if by Week 6 the lane is carrying monitor-integration or rehearsal overflow,
Phase 2 reduces to the config-only items (rail groups, DESIGN.md rules).

### Phase 3: Deep divergence (post-pilot · explicitly deferred, matching the roadmap)

Not sized here; listed so the seams above are cut in the right places:
- Bottom tab bar / dedicated nurse frame if pilot feedback demands it (the
  shellConfig seam is where a real `NurseShell` would fork).
- `medication.prescribe`/`administer` permission split (server).
- Charge-nurse designation (role or per-user grant) if the SOP model chafes.
- Nurse phone experience beyond Relay (task list, obs entry?), needs pilot
  evidence about what nurses actually reach for on phones.
- Doctor home personalization ("my list" vs ward), needs a patient-assignment
  concept Orb does not have yet; do not invent it pre-pilot.

---

## 7. Open questions for Samuel

**Q1, Which tabs stay in the nurse rail?** Proposed: My Shift · Patients ·
Messages · Sage · Scribe, with Orders, Lens, Forecast, Appointments under
More, and `coding` (Revenue) removed from the nurse set entirely.
*Recommendation: as proposed. Revenue in a nurse's rail serves no shift task;
if a site wants nurse coding visibility later it is one config line.* The real
judgment call is Orders, see Q4.

**Q2, Does "Today" become the doctor's landing tab for the pilot, or ship
dark?** *Recommendation: ship ON during the shadow phase (clinicians aren't in
yet, feedback is free), freeze the choice at Week 8, before training materials.
The config flip stays available per site.*

**Q3, Build the nurse patient sheet for the pilot, or pilot on Vigil-detail
lookup?** *Recommendation: build it only if the lane is clear at Week 6 (the
Phase 2 decision rule). It is the highest-value divergence after the homes, but
it is also the only item here that adds a PHI surface during the pilot window,
and the pilot is safe without it.*

**Q4, Should nurses create orders, and which kinds?** They hold `order.write`
today and see full CPOE. Indian ward reality includes verbal-order workflows,
which argues for keeping it; prescribing discipline argues for scoping the
nurse UI to nursing/task orders. *Recommendation: put this to the physician
champion with the concrete screen; until then, leave behavior unchanged (Orders
moves under More but stays whole). This is a clinical-governance decision that
should not be made by a UI doc.*

**Q5, Who admits patients in the pilot wards?** The answer decides whether the
nurse ever needed the admit CTA (today it 403s, §1.2.2) or whether admission
is clerk/doctor work and the CTA simply disappears for nurses.
*Recommendation: fix the CTA to permission-driven now (it is a live defect
regardless); ask the hospital who owns admission before granting anyone new
permissions.*

**Q6, Phone scope for nurses.** Roadmap commits phone-hardened Relay.
*Recommendation: phones get Relay + a read-only shift board; obs entry and eMAR
stay tablet/desktop for the pilot, a 5-inch screen is where charting errors
live, and the tablet redundancy story (manual entry behind monitors) doesn't
need phones. Revisit with pilot evidence.*

**Q7, Does the charge-nurse distinction need product support for the pilot?**
`outcome.record` is role-wide; the "one named person per shift" is SOP.
*Recommendation: no product change for the pilot, the OutcomeCaptureSheet
already shows who recorded what, which is the accountability the SOP needs.
Revisit only if the ward reports confusion about whose job the confirmation
is.*

---

*Review requested before any Phase-1 code is written. The only urgent item
regardless of this doc's fate is the admit-CTA permission defect (§1.2.2),
which is a live 403 for every nurse and admin today.*
