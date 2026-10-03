# Orb: Demo Readiness

**Audit date:** 2026-08-08 (overnight ~01:05–05:10, final proof pass 06:00–08:00)
**Wave A, reconnaissance. Wave B, fixes. Wave C, polish. Wave 7, judgement calls. Wave E, final proof.**
**Investor audit: today.**

> ## ⇢ 2026-08-18 RE-SWEEP, the current status of record is now the
> ## "RE-VERIFIED 2026-08-18" block at the top of `docs/DEMO_SCRIPT.md`.
>
> A fresh cold-start browser sweep of the built bundle was run 2026-08-18.
> Headlines: every demo-critical safety beat below still passes live (Sepsis
> Six interlock, wrong-room flag, Administer-modal allergy restatement, ward
> scoping, Bridge login); **demo-day now requires a morning reseed** (nine
> days of clock drift re-created the stale ward this file's reconciliation
> pass cleared); **ADM001 hits an MFA-enrollment wall** (`.env`
> `ORB_MFA_REQUIRED_ROLES=admin`) so Act 6 needs a decision; login lands on
> the new "Orb Today" screen; and one genuine UI defect was found and fixed, 
> order-set previews collapsed non-blocking allergy alerts to "Cleared"
> (`OrderSetsPanel.tsx`, the Ananya Kapoor cross-reactivity beat; server was
> always right, the screen threw the caution away). §13.5's "PARTIAL" is
> resolved: the server forecast now emits a measured day-0
> (`forecast_routes.py::hospital_forecast`, pinned in
> `test_forecast_service.py`). Wave E remains the last full 13-tab admin
> visual sweep, repeating it is blocked by the MFA wall on the shared dev DB.

> ## ⇢ WAVE E IS THE CURRENT STATUS OF RECORD, it is immediately below.
> Wave 7's log follows it, then Wave C's, then Wave B's, then Wave A's
> reconnaissance body, preserved verbatim.
>
> **The running order for demo day is a separate file: `docs/DEMO_SCRIPT.md`.**

---

# RECONCILIATION PASS: 2026-08-09

**Every numbered item in the Wave A reconnaissance body (§1.1, §16.4) has been
re-checked against the code as it stands today** and carries a dated verdict on
its own heading: `, FIXED / PARTIAL / NOT FIXED _(2026-08-09: why)_`.

Read the verdict, not the `**BLOCKER**` severity beside it, the severity is
Wave A's original assessment and is left untouched on purpose. The recon wave
stamped *everything* `NOT FIXED` because it was a recon wave; a later wave then
fixed code without updating headings. **The diagnoses were accurate; the statuses
were stale.** Every item body below is preserved verbatim.

### Where it actually stands: 89 items

| Verdict | Count |
|---|---|
| FIXED | 29 |
| PARTIAL | 16 |
| NOT FIXED | 32 |
| Non-defect (STRENGTH / OK / ASK / N-A) | 12 |

Of the 32 that were `NOT FIXED`, **roughly a third were stale demo data rather
than code**, and **the database was reseeded on 2026-08-09 11:20**, which
cleared that whole cluster at once (admissions now 36–72 h old, Sepsis Six clock
3–4 min, Relay down from 80,234 messages to 7, stale Copilot suggestions and
order-set applications gone, surgical schedule repopulated). See DEMO_SCRIPT §6.

Two of the named code defects have since been fixed: **§8.5** (the Impact
dashboard is now in rupees, natively denominated rather than FX-converted) and
the Sepsis Six block reason, which now names the actual allergy.

**§13.1 (`W1 30/8`) is also now fixed**, the heatmap shows the six real wards,
and a capacity-consistency bug it surfaced (`30/48` header vs `34/72` forecast)
was fixed with it. ~~The genuine code defects still open and visible on screen are
§3.4 (retrieval markup can reach the clinician), §2.7, §5.3, §6.4, §10.3, §10.4,
§13.5, §13.7, §13.8, §14.3 and §15.1.~~ **Update 2026-08-10: that entire list has
been worked and re-verified against the code, ten of the eleven are now FIXED;
§13.5 is PARTIAL (a client-side day-0 anchor; the server forecast still starts at
tomorrow). Each heading below carries its dated evidence. That moves the tally
above to FIXED 39 · PARTIAL 17 · NOT FIXED 21.**

### The three things worth knowing before you read anything else

**1. Realtime was never broken in code, it was started wrong.** `main:app`
serves no `/socket.io/`, so every realtime feature dies silently. That is now
impossible to do by accident: `server/main.py` refuses to import under
`main:app` with a loud banner, and a `/socket.io/` tripwire route returns a 500
naming the fix if a handshake ever reaches FastAPI. Both were tested. §16.1.

**2. `ORB_DISABLE_SIMULATOR` does not exist.** It appears nowhere in the code, 
not in `.env`, not in `.env.example`, and no Python reads it. It was a one-off
shell export on a long-dead process. The real switch is `ORB_INGESTION_MODE`,
and this box's `.env:39` is `simulation`, so vitals move. ⚠️ **But
`.env.example:101` ships `ingestion`**, a fresh box built from the example
would have frozen vitals and no error anywhere. §16.2.

**3. A single stale-data root cause explains a whole cluster of "blockers".**
`reset_db_and_seed.py` anchored every admission to an *absolute* window
(21–23 May 2026). The demo DB was last seeded 2026-08-01, so the ward is eleven
weeks old and every elapsed-time display is correctly reporting an ancient
number: the Sepsis Six clock (§2.3), "250h late" on the med round (§5.2), "Day
79 of admission" (§7.4), an empty surgical schedule (§10.1), 0% bundle
compliance (§13.2). **The anchor is now a rolling 3-day window**, so a reseed
finally produces a ward admitted in the last three days:

```bash
cd /Users/samuelmorningstar/Documents/Projects/Orb/server && .venv/bin/python scripts/reset_db_and_seed.py
```

This was **not** run for you, it destroys and rebuilds the demo database, which
is your call on demo day, not mine. _(Superseded: the reseed WAS run, see the
header above and DEMO_SCRIPT §6. §13.1 (`W1 30/8`) was a code constant rather
than stale data, and has since been fixed separately.)_

### Built this pass

* **Roster Import UI**, genuinely absent, now `src/pages/panels/admin/RosterImport.tsx`
  + `src/api/rosterImport.ts`, an Admin tab. Verified end to end in the browser:
  preview → dry run → commit (run `RI-5B4FA85CA3`, a deliberate no-op). §8.11.
* **Order Sets UI**, was **already built and wired** at `OrdersPanel.tsx:188`.
  The report that it was missing came from `grep -r "order.set" src/`, whose `.`
  cannot match the capital S in `orderSets`. Nothing was rebuilt. §6.1.

---

# WAVE E: FINAL DEMO PROOF (2026-08-08, 06:00–08:00)

Everything below was measured on **this machine, this morning**, against the real
backend and the real Vite dev server, signed in as real seeded users. Where a
number is stated it was timed, not estimated. **167 fresh screenshots** (dark and
light) are in `docs/screenshots/wavee/`.

**Files changed by this wave:** `server/routes/sage_routes.py`,
`server/tests/test_fast_contract_scaffolding.py`, `dist/**` (rebuilt),
`docs/DEMO_READINESS.md`, `docs/DEMO_SCRIPT.md` (new),
`docs/screenshots/wavee/**` (new). No test was weakened, no feature removed, no
file deleted.

## THE HEADLINE, STATED PLAINLY

**Sage is roughly ten times slower than the demo script assumes, and the cause is
not Orb's code.** The local models on this box decode at **~9 tokens/second**
(medgemma:4b), measured straight from Ollama with Orb removed from the path
entirely. Everything else in this report is good news; this one item is the thing
the founder must know before he stands in front of someone.

| What was assumed | What was measured this morning |
|---|---|
| General question, Thinking OFF: **sub-second** | **27–95 s to the first visible word** (5 questions, twice, two backend configurations) |
| Chart question: **6–8 s** | **74–127 s** |
| n/a | Raw Ollama, no Orb: **medgemma:4b = 8.9–9.2 tok/s** (3 runs) |

Two separate causes, and they compound:

1. **The 4B model has stopped honouring the fast contract's leading marker.**
   `[Sage] fast contract: no LEADING marker (trailing_marker=True …)` appears in
   the backend log on **every single fast query**, 12 of 12 observed. The
   contract requires a 3-token `NOACT|` marker to OPEN the reply so streaming can
   start immediately; the model is emitting it at the END instead. The streamer
   correctly fails CLOSED when no marker opens the reply, so **nothing streams at
   all** and the clinician stares at an empty bubble for the entire generation.
   That is why first-token ≈ total time in every measurement. The safety design
   is behaving exactly as documented; the model is not holding up its end.
2. **The box itself is slow today.** 9 tok/s for a 4-billion-parameter Q4 model on
   a 64 GB Mac is roughly 5× below what that hardware should do. It is not memory
   pressure, I evicted the 27B and re-measured: **8.9 tok/s, unchanged**. It is
   not the vitals simulator either, I restarted the backend with
   `ORB_DISABLE_SIMULATOR=1` and re-measured: **15–95 s, no better**. Ollama's
   `llama-server` sits at 1.7 % CPU while generating, so it is on the GPU, not
   the CPU. Competing for that GPU this morning: XProtect (91 % CPU),
   mediaanalysisd (85 %), Opera GX, WhatsApp.

**What to do before the demo, in order of cost:**

1. **Quit WhatsApp, Opera GX and anything else running**, and let Spotlight /
   Photos analysis finish. Re-run the one-line check below and see what the box
   actually does when it is not sharing.
2. **Warm both models and leave them warm** (`ORB_OLLAMA_KEEP_ALIVE=-1` already
   does this, confirm with `ollama ps` that both `medgemma:4b` and
   `medgemma:27b` are resident before the investor sits down).
3. **Re-measure, and set the script to the number you actually get:**
   ```
   curl -s -X POST http://localhost:11434/api/generate \
     -d '{"model":"medgemma:4b","prompt":"Explain sepsis in 200 words.","stream":false,"options":{"num_predict":200,"num_ctx":4096},"keep_alive":-1}' \
     | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["eval_count"]/(d["eval_duration"]/1e9),"tok/s")'
   ```
   At 9 tok/s, plan the script around Sage answering in ~30 s. At 40 tok/s the
   original script works as written.
4. **Do not try to fix this by cutting context or switching models tonight.**
   The measurement above isolates it to the inference stack, which is exactly the
   thing not to touch the night before an audit.

**`docs/DEMO_SCRIPT.md` is written for the slow case**, the safety features lead,
because they are deterministic, instant, and the most impressive thing Orb does.

---

## WHAT I FIXED

### E.1: The output contract was printing itself into Sage's answers: **BLOCKER: FIXED**

Observed in the browser at 06:20, DOC001, Thinking OFF, on the question *"First-line
management of an acute severe asthma attack in an adult?"*. The model answered
correctly in markdown, then wrote its own detail contract out as ordinary prose,
in the chat bubble, where a clinician reads it:

```
… Consider intubation and mechanical ventilation if the patient deteriorates.

Details: concise None None None The primary goals are bronchodilation and
reducing airway inflammation. … This inf
```

and, a few questions later, the same thing in fully spelled-out Title Case:

```
Details:

Display Mode: concise
Patient Facts Summary: None
Local Policy Guidance: None
External Guidance: None
Clinical Assessment: The ECG changes associated with hyperkalemia reflect …
Cited Sources: ["EKG Interpretation"]
```

This is the same failure class Wave 7 fixed one level up, one level worse. Every
existing rule keys on `<tag>` with angle brackets; this variant has **no angle
brackets anywhere**, so nothing cut it. `docs/DEMO_READINESS.md`'s own Wave 7
lesson applied to itself: the fixtures in
`server/tests/test_fast_contract_scaffolding.py` all had brackets, so that file
was green while this shipped.

**Fixed** in `server/routes/sage_routes.py`, a new `_BARE_CONTRACT_CUT_RE`
folded into `_contract_cut()`, which is the one place the buffered parser, the
untagged-output rescue, and the streamer all cut on, so all three paths are
covered by one change. The streamer additionally holds back a trailing
`Details` / `display_mode` until it is decided, because a streamer that has
already emitted "Details:" cannot take it back.

**The pattern is deliberately narrow, and this is the reasoning:** leaking
scaffolding is embarrassing, but cutting real clinical prose loses information a
clinician needed. So only two shapes count, the literal token `display_mode`
(never English), and `Details:` followed by the display-mode enum *and* the next
field's `None` placeholder. `"the referral details are in the handover note"` is
untouched, and there is a test that says so.

10 new tests in `server/tests/test_fast_contract_scaffolding.py`, written failing
first (10 failed / 15 passed), then passing. `test_fast_contract_scaffolding.py`
+ `test_fast_contract_marker.py`: **73 passed**. All eight Sage test files:
**517 passed**.

### E.2: The production SPA bundle was stale: **ROUGH: FIXED**

`dist/` was built at 04:06, before Wave 7's frontend work landed. Fifteen source
files were newer than the bundle, including the chart timeline fix, the keyboard
shortcuts and `VigilPanel`. `run-orb.sh` without `--dev` serves that bundle on
port 8000, so anyone opening `http://localhost:8000` tomorrow would have seen a
build with none of Wave 7 in it. Rebuilt (`npx vite build`, 902 modules, clean).
**Both origins now serve the same code.**

---

## WHAT I BROKE, AND WHAT IS LEFT ON THE DEMO DATA

Stated first because it is my footprint, not the product's.

1. **Two order-set applications are recorded against Suresh Reddy.** Probing
   whether a nurse can apply an order set, I applied *Acute severe asthma (adult)*
   twice, once as NUR001, once as DOC001, creating 18 draft orders. **I
   cancelled all 18 through the product's own state machine** (draft → cancelled,
   audited, nothing deleted); his chart is back to its 6 real orders. But the
   *application ledger* correctly keeps the record, so the Order Sets screen for
   Suresh Reddy now shows:
   > Already applied to this patient
   > Acute severe asthma (adult) · 8/8/2026, 6:51 AM · DOC001 · 9 order(s)
   > Acute severe asthma (adult) · 8/8/2026, 6:51 AM · NUR001 · 9 order(s)

   **This is on the Sepsis Six screen you will demo.** Either reseed, or say
   "that is this morning's test run, and the fact you can see it is the point".
2. **Three messages from me are in Suresh Reddy's case room** (ids 80584, 80606,
   80610), the last of which is *"Give Rajesh Iyer 40mg prednisolone now."*, the
   wrong-room test. It carries a live, open wrong-room flag (id 444), and the
   warning renders correctly under the message. It sits at the very bottom of the
   transcript, partly under the composer, so **typing the sentence live is the
   better demo**, it auto-scrolls.
   **The side effect that needs a decision:** one of those messages
   (*"chase Ananya Kapoor's repeat VBG and start her insulin infusion"*) made the
   Relay ambient copilot raise a suggested action **"START INSULIN INFUSION ·
   Target: Ananya Kapoor"** in **Suresh Reddy's** copilot sidebar. It is correctly
   badged **STALE**, and marking it stale is the right behaviour, but it is a
   cross-patient suggestion with a **Confirm** button visible on the screen you
   plan to demo. A reseed clears it; otherwise keep the eye on the transcript.
3. ~~**Pre-existing demo-data staleness is unchanged**~~, **RESOLVED by the
   reseed, 2026-08-09 11:20.** Everything in items 1–3 above described the same
   stale database and is now gone. Verified after reseeding: `messages` 80,234 →
   **7** (so the three audit messages, their wrong-room flag, and the stale
   cross-patient Copilot suggestion no longer exist); `order_set_applications`
   and `sage_actions` → **0**; admissions **36–72 h old** rather than ~11 weeks;
   Sepsis Six elapsed **3–4 minutes** against its 60-minute target rather than
   226 h; the mismatched COPD ward-round plan on Ananya Kapoor's DKA timeline is
   gone with the rest of it.

   Two consequences worth knowing before you demo:
   * **The wrong-room beat must now be typed live**, there is no pre-seeded
     example left to point at. That was already the recommended way to do it.
   * The reseed **preserves** the audit hash chain and shadow-mode evidence
     (31,789 `sweeper`/`chat` rows) rather than wiping them; only the 136,502
     disposable `simulator`-lane rows were removed. The audit chain was re-sealed
     as part of the run.

---

## EVERY WORKFLOW, RE-RUN: RESULTS

### Nav sweep: 46 tabs × 2 themes, 5 roles

Every nav tab for DOC001, DOC004, NUR001, PHA001 and ADM001, in dark and light.
**Zero console errors. Zero failed requests. Zero HTTP ≥ 400.** Login itself was
clean for all five roles in both themes. The workflow passes were re-run in light
theme in full: **the only network entry in the entire light sweep** is the Vite
dev-server font abort noted at the bottom of this section. All 13 Admin tabs:
0 console, 0 net, in both themes.

Two Admin tabs (**Trust Center**, **Pilot Scorecard**) look thin if you screenshot
them 2 s after the click, they lazy-load. Given 7 s they render in full. Not a
bug; worth knowing if you are clicking fast in front of someone.

Two screens are thin, and both are correct empty states, not failures: **Lens**
(328–360 chars, an image-upload surface with nothing uploaded) and **Pharmacy
Verify Queue** (371 chars, an empty queue).

### The workflows

| Workflow | Result |
|---|---|
| Login, all 5 roles, both themes | **PASS**, 0 console errors |
| Ward board + live vitals | **PASS**, vitals demonstrably change between samples 9 s apart |
| Patient chart | **PASS**, `MRN-GM002 · 67Y · MALE` renders correctly (Wave A's FEMALE bug stays fixed) |
| Patient Story / chart timeline | **PASS**, strictly reverse-chronological (Aug 08 → Aug 01 → Jul 29 → May 25 → May 24 → May 23), "9 new since you last reviewed this chart on Jul 29, 05:30 AM", a "Reviewed to here" divider in the right place, 7 filter chips |
| Relay, open room, send | **PASS**, message rendered **once** (Wave A's duplicate is fixed) |
| Relay, wrong-room detection | **PASS**, see below |
| Nurse shift board | **PASS**, Due now (6) / My patients (5) / Handover, per-task lateness |
| Nurse observations | **PASS**, 7 NEWS2 parameters + ACVPU, live NEWS2 preview, RCP escalation table cited |
| eMAR administer modal | **PASS**, z-index 10050, **visible**, barcode-style drug-name confirmation, Cancel / Not given / Given (Wave A §5.1 fixed) |
| Ward Round | **PASS**, allergy banner, since-20:00 events, NEWS2 with escalation text, "Decide today" with Sage suggestions, S/K/N/P keys |
| Orders, individual | **PASS** |
| Orders, order sets | **PASS**, 5 cited protocols, patient picker by name/bed/MRN |
| Order set safety gate | **PASS**, see below |
| Keyboard shortcuts | **PASS**, `?` card lists `g p / g r / g o / g m / g s`, `⌘K`, `Esc`; `g r` really navigates to Ward Round |
| Admin, all 13 tabs | **PASS**, 0 console errors, 0 failed requests on every tab |
| Trust Center | **PASS**, trust score 85, 9 passing / 4 warnings / 0 failing, hash chain **verified over 70,760 sealed rows** |
| Patient Bridge | **PASS**, login renders |
| Sage | **WORKS, BUT SLOW**, see the headline |
| Voice / dictation | **OFF, and says so**, "Local speech recognition is not installed on this appliance, type the plan below" |

---

## SAFETY-CRITICAL PATHS: ALL CONFIRMED

Driven straight against the API, so these are the server's real answers, not the
UI's opinion of them.

**Ward scoping.** DOC001 sees 5 General Medicine patients; DOC004 sees 5 General
Surgery patients; the sets are **disjoint**. DOC001 asking for a surgery patient
gets **404 Patient not found** on the chart, the timeline, the orders and the
eMAR, 404 rather than 403, so the refusal does not itself leak that the patient
exists. DOC004 gets 200 on the same patient (control).

**Relay channel + DM scoping.** DOC001 and DOC004 share **zero** channel ids.
Reading a channel belonging to the other department: **403 Not authorized to
access this channel**. PHA001 has no `relay.read` at all and is refused the
channel list outright.

**Ward-brief scoping.** DOC001's brief contains only GM-1 patients; DOC004's
contains only SW-1 patients.

**Escalation chain.** Live, tiered and scoped: an open escalation on Rajesh Iyer
at **tier 2 of 3**, role "Duty registrar", with a named target and a due-at
timestamp. DOC001's and DOC004's escalation sets do not overlap.

**Wrong-room detection.** Fully proven end to end, and **deterministic, no model
in the path, so it is instant.** Posting *"Give Rajesh Iyer 40mg prednisolone
now."* into Suresh Reddy's case room returns the warning inline on the send
response and persists flag 444:

```
band: flag   score: 1.2
reasons: identity:full_name, clinical_detail:dose
suspected: Rajesh Iyer      suggested: "Case: Rajesh Iyer"
stage: deterministic        resolution: open
```

**THE SENTENCE MATTERS, AND THIS COST ME AN HOUR.** A *question* is deliberately
demoted: *"Rajesh Iyer's SpO2 is 84% on 4L, can we get an ABG on him?"* scores
the same 1.2 but lands in band **`review`** (`demoted:question`), which says
nothing to the clinician and defers to the 4B adjudicator, which is currently
30+ seconds behind. A bare mention with no clinical detail
(*"chase Ananya Kapoor's repeat VBG"*) is band `clear`. **Use an imperative with
a dose.** The exact sentence is in the demo script.

**The allergy interlock, the best thing in the product.** Sepsis Six previewed
against all four General Medicine patients who have allergy records shows a real
gradient of judgement, not a blanket block:

| Patient | Allergy on record | Verdict on the ceftriaxone line |
|---|---|---|
| **Suresh Reddy** | Ceftriaxone, anaphylaxis, **severe** | **STOPPED.** "Severe drug-safety alerts must be acknowledged with a reason before this order proceeds." Apply button **disabled** |
| **Ananya Kapoor** | Penicillin, rash, **moderate** | **Not blocked, but an allergy alert is raised**, beta-lactam cross-reactivity detected and surfaced, proportionate to a moderate rash |
| **Priya Nair** | Ibuprofen, bronchospasm | Clean, `interaction_screened: true` |
| **Rajesh Iyer** | none | Clean, `interaction_screened: true` |

Applying it anyway returns **428** with *"This order set was NOT applied, no
orders were created. A safety check stopped at least one item, and a set is
applied whole or not at all."*

**Coded-concept guard.** A free-text medication order is refused for **both** a
nurse and a doctor: *"order_type 'medication' requires a coded concept (code +
code_system). Only ['nursing','other'] may be free-text."*

**Nurse write scoping.** Nurse **cannot** edit the patient record (403,
`patient.write` missing) and **cannot** add an allergy (403); the doctor can do
both (200).
**Nurse CAN apply an order set, and that is by design, not a hole**, 
`server/permissions.py` grants the nurse role `ORDER_WRITE` explicitly, and
applied items land as **draft**, not active. If the investor asks who can order,
that is the honest answer.

**Admin-only surfaces.** `/api/admin/overview`, `/users`, `/roster-import/runs`,
`/shadow/scorecard`, `/impact/` → **403 for a doctor, 200 for an admin**.
Pharmacy: `/api/helix/orders/pending` → **200 for PHA001, 403 for DOC001 and
NUR001**.
One deliberate exception worth knowing: `/api/admin/models/provenance` returns
200 to **any signed-in clinician** (401 anonymous). It carries no PHI, it is the
model-governance transparency surface, so this reads as intended, but it is the
one admin path that is not admin-only and you should not be surprised by it.

**Thinking-mode routing.** The toggle reads **Thinking** / **Fast**, and Fast mode
prints its own warning under the composer: *"Fast mode. General questions are
answered by the small model, quicker, less accurate. **Patient, medication and
dose questions still go to the careful model.**"* The safety floor is on screen,
in the product, where the investor can read it.

---

## GATES

| Gate | Result |
|---|---|
| `npx tsc --noEmit` | **PASS**, exit 0 |
| `node scripts/check-contrast.mjs` | **PASS**, 378 token/surface pairs across 3 themes; 1,876 resolved component call sites |
| `npx vitest run src/` | **PASS**, `Test Files 35 passed (35) · Tests 259 passed (259)` in 5.48 s, exit 0 |
| `pytest tests/ -q --no-header` | **3 failed, 3771 passed, 2 skipped, 5 xfailed in 2133.28s (0:35:33)** |

### The exact pytest line, and what the 3 failures are

```
3 failed, 3771 passed, 2 skipped, 5 xfailed in 2133.28s (0:35:33)
```

```
FAILED tests/test_downtime_fhir_export.py::test_export_does_not_stall_the_event_loop
FAILED tests/test_relay_room_fit.py::test_cost_stays_linear_on_a_full_hospital_roster
FAILED tests/test_room_fit_adversarial.py::test_detector_cost_on_the_worst_case_message
```

**All three are CPU performance budgets, and all three pass alone.** Re-run on a
quiet machine (load average 1.32) immediately afterwards:

```
3 passed in 4.78s
```

They failed in the full run because that run was competing with a live backend
(simulator on), Ollama holding a 27B model resident, a headless Chromium
walkthrough and my own Sage benchmarks. The room-fit one is explicit about its
budget, it asserts `< 4 ms/message` on a 300-bed roster and measured **20.99 ms**
under contention. **None of them touch the code I changed** (`sage_routes.py`);
`room_fit_service.py` and the downtime exporter were not modified in this wave.

**Corroborating run:** the same suite, started 05:07 before my `sage_routes.py`
change and finishing 06:06, gave **`3763 passed, 2 skipped, 5 xfailed in 3505.99s`
0 failed**, on a machine that was quieter at the time. The delta of +8 passed is
the 10 new scaffolding tests minus 2 renamed/merged.

**The rule this proves, and it will bite you too:** *do not run pytest, vitest, a
browser sweep and the app at the same time on this box.* Vitest showed the same
pattern, run alongside pytest it reported *5 failed / 254 passed* (every failure
a `waitFor` timeout in the three heavy React Testing Library files, 60.7 s instead
of 4.3 s); run alone, **259/259 in 5.48 s**.

---

## STILL NOT FIXED: STEER AROUND THESE

1. **Sage latency.** The headline. Not fixable tonight; the demo script is written
   around it.
2. **A raw action blob leaked into one answer.** The DKA answer ended with
   `{"action_type":"answer","medication":null}` on its own line. Different
   mechanism from E.1 (this is the action-intent channel, not the detail
   contract) and it appeared once in ~15 questions. Not fixed, I could not
   reproduce it reliably enough to write an honest test, and guessing at a
   sanitiser for a path I have not characterised is how the E.1 class of bug got
   made in the first place.
3. **The chart lane failed to resolve a patient by name once.** *"Summarise Rajesh
   Iyer in bed GM-102"* asked in a fresh chat came back *"I don't have any
   information about Rajesh Iyer in my current view … you haven't specified which
   patient."* The census was clearly loaded (it named beds GM-101–GM-105) but the
   chart was not attached. **Mitigation for the demo: open the patient's chart
   first, then ask Sage.** The anchor is then unambiguous.
4. **Roster import and PIN enrolment still have no UI** (Wave B §1, §2, unchanged).
5. **Pharmacy (Helix) and Surgical Suite still contradict the medical charts**
   (Wave B §3, unchanged). Do not open them.
6. **Voice is off**, honestly labelled everywhere, but not demonstrable.
6b. **Three performance-budget tests fail whenever the box is busy**, the two
   room-fit detector cost tests and the downtime-export event-loop test. They pass
   in 4.78 s on a quiet machine. Nothing to fix today, but they are a real
   tripwire: `test_cost_stays_linear_on_a_full_hospital_roster` guards
   wrong-room detection at 300-bed scale, and its own failure message
   ("the pattern cache is probably thrashing again") says it has caught a genuine
   regression before. **Worth re-running on a quiet box after the demo before
   anyone concludes it is only ever contention.**
7. **A font request 404s on the Bridge login in the Vite dev server only**
   (`inter-latin-wght-normal.woff2`, `ERR_ABORTED`). Cosmetic, dev-server only,
   does not occur on the built bundle at :8000.
8. **`SITE TO COMPLETE, name the consultant who signed this set off`** is printed
   on every order set, and *"This protocol has not yet been signed off by a named
   consultant at this site"* on the Sepsis Six panel. This is deliberate honesty
   and it is the right answer for a CIO, but know it is on screen before he
   reads it out to you.

---

## HOW THE APP WAS STARTED, AND HOW TO START IT TOMORROW

```
cd /Users/samuelmorningstar/Documents/Projects/Orb
./run-orb.sh --dev
```

**Verified from a genuine cold start at 07:59**, every process killed first, then
this one command, on a quiet machine, which is exactly demo-day conditions:

* `/api/health` → **200**
* `/socket.io/?EIO=4&transport=polling` → **200** (§16.1's rule holds, the
  script uses `main:socket_app`, so realtime is live)
* Vite on 5173 → **200**
* `Simulation bot error` lines since start: **0**
* `Traceback` lines since start: **0**
* **First page load, unauthenticated, the entire browser console:**
  ```
  [debug] [vite] connecting...
  [info]  Download the React DevTools for a better development experience…
  [debug] [vite] connected.
  ```
  **Zero errors. Zero failed requests. Zero HTTP ≥ 400.**
* **After signing in as DOC001 and sitting on the board for 9 s: the console is
  completely empty and the network log is completely empty.**
* Both models resident in VRAM (`ollama ps`)

Screenshot of that first load: `docs/screenshots/wavee/w9-01-coldstart-first-load-dark.png`.

The one CRITICAL line in the startup log is **intentional and is a feature**:

```
CRITICAL orb.bootstrap: SHIPPED DEFAULT CREDENTIALS FOUND: ADM001, DOC001, …
(profile=dev, enforcement=off). This appliance must NOT be handed to a hospital
in this state.
```

If a CIO sees your terminal, that line is worth pointing at.


---

# WAVE 7: WHAT ORB NEEDED AND DID NOT HAVE (2026-08-08, 04:05–05:10)

The brief was open: *"fix and add any features that you think are necessary."*
Everything below is **additive**. No feature was removed, no test was weakened,
no flag was added to hide anything.

## The decision, and what it cost to make it

Before building anything I went looking for what already existed, because the
worst possible outcome of an open brief is a second copy of a feature the
product already ships. That search paid for itself immediately:

| Candidate from the brief | Verdict | Why |
|---|---|---|
| Global patient search | **Already built, do not rebuild** | `Cmd-K` command palette over `/api/search/patients`, department-scoped and audited (`src/components/CommandPalette.tsx`) |
| Patient timeline | **Already built, but BROKEN.** See W7.1 | `PatientStory` + `patient_story_service.py`. It existed, and it was silently misordering charts |
| Print / export | **Already built** | Handoff, discharge, orders, medication list print views + the downtime export |
| Demo/seed control in the UI | **Rejected** | An admin button that mutates the demo database the night before an audit is a foot-gun, not a feature. `scripts/reset_db_and_seed.py` already exists for a deliberate, supervised reseed |
| Undo for a clinical action | **Rejected** | The audit chain and the eMAR are append-only *on purpose*, corrections are new rows. A half-built undo over an append-only model is a safety hazard dressed as a convenience. This needs a design conversation, not an overnight implementation |
| Onboarding / first-run | **Rejected as lower value** | Wave C had just rebuilt the empty states on Orders, Order Sets and Revenue, and `AdminEmptyState` exists. The marginal gain was small next to the two below |
| **"What changed since I last looked"** | **BUILT** | The single most-asked question on a ward, and the product could not answer it at all |
| **Keyboard shortcuts + a reference card** | **BUILT** | `Cmd-K` existed and was undiscoverable; nothing else had a key |

---

## W7.1: The patient timeline was silently reordering charts: **BLOCKER: FIXED**

`services/patient_story_service.py` ended with:

```python
events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
```

That is a **lexical string sort** over a feed whose rows arrive in two different
timestamp dialects, sometimes from the same column:

* `orders.created_at`, `medications.started_at`, `clinical_documents.created_at`
  take SQLite's `CURRENT_TIMESTAMP` default → `"2026-07-29 13:28:52"` (space
  separator, no timezone).
* `risk_scores.timestamp` and everything the application layer writes → ISO,
  `"2026-07-29T09:30:52.102119+00:00"`.

`' '` (0x20) sorts before `'T'` (0x54). **Every bare-dialect row therefore sorted
below every ISO row regardless of when it actually happened.** A second, subtler
trap rides along: `'Z'` (0x5A) sorts after `'.'` (0x2E), so a whole-second stamp
compares as *later* than a sub-second one inside the same second.

**Measured on the real seeded demo database, not on a fixture.** Across the 30
admitted charts, 3 would have been rendered out of order. The worst, **Deepak
Joshi**, had 3 of 10 events misplaced, and the misplacement is the damaging
kind:

```
#   OLD (lexical string sort)                    CORRECT (parsed instant)
0   bundle 2026-07-29T09:30:52+00:00          << note   2026-07-29 13:28:52
1   alert  2026-07-29T08:43:21+00:00          << bundle 2026-07-29T09:30:52+00:00
2   note   2026-07-29 13:28:52                << alert  2026-07-29T08:43:21+00:00
```

**The newest note on the chart was filed third, underneath two older events.**
The Patient Story is the one surface a clinician reads specifically to establish
sequence, *was the antibiotic given before or after the lactate came back?*, so
an ordering defect there is a clinical defect, not a cosmetic one. Only 3 of 30
charts today because most demo rows were seeded through the application layer;
the proportion goes **up**, not down, once real HL7 ingestion and CPOE traffic
start writing through the SQLite defaults.

**Fixed** by moving the assembly into a new `services/patient_timeline_service.py`
that normalises **both** dialects to an aware UTC `datetime` before it sorts
anything, and normalises them again *inside SQL* (`strftime`, which converts a
tz-suffixed value to UTC) wherever a comparison has to be done by the database.
A timestamp neither can parse is now **dropped** rather than sorted to an
arbitrary end of the chart, a missing row is visible, a misplaced one is not.

`patient_story_service.get_patient_story` is now a thin adapter over that engine.
Its signature, its `types` filter and its exact five-key event shape
(`timestamp/type/title/detail/actor`) are **unchanged**, so `RoundPanel`,
`roundSummary.ts` and `tests/test_patient_story.py` were not touched.

### Three things got better on the way

1. **Five clinical streams that were simply missing** are now in the narrative:
   lab/vitals **observations** (the HL7 ingestion stream), **care bundles**, the
   **problem list**, **allergies**, and **order status transitions**. A "unified
   patient story" that omitted the labs was not unified.
2. **`actor` is now the clinician's name**, not their raw user id. The story used
   to print `DOC001`.
3. Severity is derived where it matters, an `HH`/`LL` critically abnormal lab no
   longer renders like a routine result, and a STAT order no longer renders like
   a routine one.

---

## W7.2: "What changed since I last looked": **NEW**

The question a doctor actually arrives with at 08:00. Orb stored every answer and
could not draw one.

**Ward board.** A `9 NEW` pill next to the patient's name, chart events since
*this* clinician last marked *that* chart reviewed.

**Patient Story.** A bar reading *"9 new since you last reviewed this chart on
Jul 29, 05:30 AM"*, a `NEW` tag on each of those rows, and a **"Reviewed to
here · Jul 29, 05:30 AM"** line drawn between the new group and the seen group.

**One control**: `Mark reviewed`.

### The five rules that make it trustworthy, and what each one prevents

| Rule | What the naive version does instead |
|---|---|
| **Never reviewed shows NO marker at all**, an absent watermark is not epoch zero | Lights up every event on every bed at first login, and the clinician learns within a day to ignore the badge |
| **The watermark moves only when the clinician says so** | It answers "has this been *drawn*", not "has this been *read*", a chart glanced at from a corridor silently clears its own badge |
| **"Mark reviewed" pins to the newest event actually RENDERED, not to `now`** | An observation charted between the render and the click gets marked read by a clinician who never saw it. **This is the one with a real patient-safety edge**, and it is asserted in both the frontend and the server tests |
| **The mark never moves backwards** | A replayed offline request (client.ts genuinely does this) resurrects a badge over events already acted on |
| **The count is computed by the DATABASE over every row, not over the truncated list** | "6 new" above a capped rail stops being true |

It is a **personal bookmark, not a clinical attestation**, not a sign-off, it
asserts nothing about the chart being acted on, and nothing safety-critical reads
it. The `phi_access` audit rows remain the evidence of who read what. That
sentence is in the migration, the service, the route and the button's tooltip,
because it is the thing a CIO will ask about.

### Scoping and audit

* `GET /api/chart/patients/{id}/timeline` and `POST .../reviewed` hang off
  `require_patient_access(...)`, the one guard that both refuses a cross-ward
  patient with a **404** (a 403 would confirm the patient exists) and writes the
  fail-closed `phi_access` row. Nothing here re-derives scope by hand; that
  duplication is what put holes in four read surfaces on 2026-08-05.
* `GET /api/chart/unseen` cannot use that dependency (it names no patient), so it
  derives scope from `department_scope(user)` **inside the query** and audits the
  read explicitly, exactly like `/api/search/patients`.
* Verified live: the same clinician's marks return `{'PAT-A73E5E98': 9}` scoped to
  General Medicine and `{}` scoped to Oncology.
* The board badge is deliberately **not** wired to the 30-second vitals poll, 
  once per board load and once per chart close. A per-tick encrypted-SQLite write
  is the exact shape behind the 2026-07-30 loop stalls.

**New table** (migration 17, `patient_review_marks`): two ids and a timestamp.
**No PHI.** Its absence for a (user, patient) pair is meaningful and is the
default.

---

## W7.3: Keyboard shortcuts, and a card that makes them discoverable: **NEW**

Before: exactly one shortcut existed (`Cmd-K`), and nothing anywhere in the
product mentioned it.

* `g` then `p / r / o / m / s / t / h` → Patients, Ward Round, Orders, Messages,
  Sage, My Shift, Pharmacy.
* `?` → the reference card. A keyboard icon in the header opens the same card,
  because **a shortcut layer whose only entry point is a shortcut is
  undiscoverable by definition**.
* `Esc` closes it.

**Why chords rather than modifiers.** Almost every Ctrl/Cmd/Alt combination on a
ward workstation is already spoken for, `Cmd-P` is print, and this product
genuinely prints. A two-key chord collides with nothing and needs no modifier.

**The three things a keyboard layer gets wrong, all guarded and all tested:**

1. **Firing while someone is typing.** A clinician writing *"Progress: patient
   settled"* contains `g`…`p`. `isTypingTarget` covers `input`/`textarea`/`select`,
   `contentEditable` (the Relay composer and Scribe editor are not `<input>`s) and
   the `textbox`/`searchbox`/`combobox` roles. **Verified in the live app**: the
   chord typed into a real focused field did nothing; the same chord from the page
   body navigated.
2. **Firing under a modal.** Switching tabs behind an open discharge confirmation
   leaves a live clinical dialog over a *different* patient's board. Guarded on
   `[role="dialog"][aria-modal="true"]`, which every Orb modal already sets.
3. **Stealing an accelerator.** Any `metaKey/ctrlKey/altKey` returns immediately, 
   which is also what leaves `Cmd-K` untouched.

Plus: the prefix **expires after 1.5 s**, so a stray `g` cannot lie in wait and
eat the next keystroke. (This is why driving the chord through two separate
browser round-trips did nothing during verification, the guard working as
designed.)

### The fourth thing, which I only found by driving it: **FIXED**

The **Ward Round already binds bare `s` / `n` / `p` / `k`** (seen / next /
previous / skip) on its own panel root. React attaches its listener inside the
document, so it runs *before* a window-level one.

Reproduced live before the fix: pressing `g` then `s` on the Ward Round **marked
the current bed SEEN *and* navigated to Sage.** One of those is a clinical
action, taken by a keystroke the clinician aimed somewhere else. (The round
counter went from `0 of 5 seen` to `1 of 5` while the screen changed to Sage.)

Fixed with the general rule rather than a special case: **a key another surface
has already consumed is not ours**, the handler returns on `e.defaultPrevented`
and drops the armed prefix. Local bindings beat global ones, which is also why
`g s` deliberately does nothing while the Ward Round has focus.

Re-verified live after the fix: the panel consumes the `s`, the tab does not
change. Pinned by a test that installs a real listener calling `preventDefault`
(`defaultPrevented` is derived from that call and cannot be faked as an event
property), and confirmed load-bearing by removing the guard, the test fails.

The round marks my test left behind were **localStorage-only** (`roundQueue.
saveRoundState`, no server write, no clinical record) and have been cleared; the
demo round is back at `0 of 5 seen`.

Shortcuts route through the **same `orb:goto-tab` bus** every other navigation
source uses, so the existing RBAC filter is the one place that decides whether a
tab may open. A nurse pressing `g o` for Orders gets nothing, and the card only
lists what her role can reach.

---

## Gates

| Gate | Result |
|---|---|
| `pytest tests/ -q --no-header` (bare) | **3763 passed, 0 failed**, 2 skipped, 5 xfailed, Wave 7 adds **34** server tests (31 new in `test_patient_timeline.py`, 3 in `test_patient_story.py`) |
| `npx tsc -b` (**the real typecheck**, see §C2) | **0 errors** |
| `node scripts/check-contrast.mjs` | **passes**, 378 token pairs, **1876** call sites (was 1852), 3 themes |
| `npx vitest run` | **259 passed / 35 files** (was 235 / 33), Wave 7 adds **24** (16 keyboard, 8 story-review) |
| Live browser | dark, light **and** night, zero console errors across the run |

*Two full-suite runs before this one each showed exactly one failure, and neither
was a Wave 7 regression in the product, the first was `inspect.getsource` reading
a route file I was editing mid-run, the second is §W7.4 below. Both are green now,
and the run above was clean end to end with no files being touched during it.*

### The tests are load-bearing: ten mutations, ten failures

Green suites lie about failure classes their fixtures cannot express, so every
guard was re-checked by breaking the implementation and confirming the suite
noticed:

| Mutation | Caught by |
|---|---|
| Bare SQLite timestamps read as LOCAL time | 2 tests |
| `new_count` derived from the truncated list instead of the DB | 6 tests |
| `mark_reviewed` allowed to move backwards | 1 test |
| `GET timeline` loses `require_patient_access` | 2 tests (the 404 **and** the audit row) |
| `/unseen` loses its department predicate | 1 test, the one written for it |
| Frontend: mark reviewed as of `now` instead of the newest rendered event | 1 test |
| Frontend: absent watermark treated as epoch zero | 1 test |
| Frontend: failed save swallowed silently | 1 test |
| Frontend: global layer stops yielding a key a panel already consumed | 1 test |

One mutation was **not** caught and is worth recording: sorting by the *emitted*
timestamp string rather than the parsed datetime still passes, because the engine
emits one normalised ISO-Z spelling. That is correct, the trap only exists on
the raw column values, and the tests that cover it (`test_story_orders_mixed_
timestamp_dialects_by_real_instant`) assert their own premise before asserting
the behaviour.

## W7.4: A test-isolation trap the new tests walked straight into: **FIXED**

The first full-suite run after Wave 7 came back with **one** failure, in a file
Wave 7 never touched:
`test_sage_sticky_anchor.py::test_naming_a_patient_who_exists_nowhere_suppresses_the_anchor`.
It **passed in isolation**. Running it after the new tests reproduced it every
time.

Root cause, and it is worth knowing about beyond this wave: `patients.status`
**defaults to `'admitted'`**, so any fixture patient that is not explicitly
discharged stays on the shared session database's ward for the rest of the run.
Sage's patient-name matcher is deliberately **fuzzy at 0.70** (the code says why:
ASR mis-transcriptions). Measured: a fixture patient I had named **"Lab Test"**
scores **0.857 against the ordinary English word "latest"**, so the assertion
"this name matches no patient anywhere" became an out-of-department denial
instead of `NO MATCHING PATIENT`.

A leaked test patient is therefore not a housekeeping problem. It can change the
answer another module gets from a clinical safety guard, in a way that looks like
a flake.

Fixed on both axes:
* Both new test modules discharge every patient they create on teardown (and
  close the `admissions` row too, closing only the patient leaves the residue
  clean on the census and still live on the ward board, which looks fixed and is
  not).
* Fixture names are now checked against the real matcher. Two were over the
  threshold, `"Lab Test"` (0.857 vs "latest") and `"Timeline Patient"` (0.741 vs
  "the patient"), and were renamed.

**Not changed, and flagged for after the demo:** the 0.70 fuzzy threshold means a
genuinely short patient surname can collide with a common English word in a
clinician's question. That is a deliberate property of that matcher, documented in
its own source, and re-tuning a clinical name-resolution threshold the night
before an audit is the wrong risk. Worth a look in the pilot.

## Lint

The five files Wave 7 created, `KeyboardShortcuts.tsx`, `PatientStory.tsx`,
`shortcuts.ts` and the two new test files, are **eslint-clean**. The ratchet is
still red (463 vs a baseline of 377 last updated 2026-07-29, i.e. before Waves A–C
added code); it was red on arrival at 457 for Wave C. **No reported problem lands
on a line Wave 7 wrote**, checked by matching every message against the changed
lines in `VigilPanel.tsx`, `ClinicianShell.tsx`, `client.ts` and `orbBus.ts`.
Somebody should re-baseline it deliberately, and still not tonight.

## Performance

The constraint was "must not slow an existing path". `get_patient_story` now runs
14 source queries instead of 8, so it was measured rather than assumed, on the
real encrypted demo database:

* Richest admitted chart (26 events): **median 0.28 ms, p95 0.34 ms**.
* `read_review_state` (the new sibling read on `/story`): **< 0.05 ms**.
* `unseen_counts_for_scope` for the whole ward board: **median 0.1 ms**, one
  query for every bed, not one per bed.

## Read this before demo day

1. **A review bookmark is seeded on Rajesh Iyer for DOC001**, dated 2026-07-29,
   so the `9 NEW` badge and the "Reviewed to here" line are on screen the moment
   you open the board. It is a bookmark, not clinical data, `patient_review_marks`
   holds two ids and a timestamp. To start from a clean state instead:
   `DELETE FROM patient_review_marks;`
2. **The backend was restarted** to load the new router and apply migration 17.
   It is on `main:socket_app`, port 8000, `ORB_DISABLE_SIMULATOR` **unset**, and
   **0 `Simulation bot error` lines** since restart, §2.1 is quiet on this build.
   The §16.1 start rule is unchanged and still critical.
3. **The keyboard card is the cheapest "this was built by people who use it"
   moment in the product.** Header icon → `g` `r` → Ward Round.

## What I deliberately did not do

* **Did not build a second timeline.** The right move was to fix the one that
  exists and put the new layer on top of it.
* **Did not widen the story event contract.** The engine carries `severity`,
  `badge` and a stable `id` per row; they are dropped in the adapter rather than
  added to a five-key shape that three surfaces and a pinned test depend on. The
  richer row is available on `/api/chart/patients/{id}/timeline`.
* **Did not re-baseline the lint ratchet.** Still red on arrival, still not
  worsened, Wave C's reasoning holds.
* **Did not touch the Ward Round layout.** Adding observations to the story feeds
  more events into the round's overnight window; the demo database has **0**
  observation rows so nothing changes today, and the window already displays its
  `hidden` overflow count honestly rather than swallowing it.

---

# WAVE C: PREMIUM POLISH (2026-08-08, 03:10–04:05)

Frontend only (`src/**`); **no server file was touched**. Everything below was found by
driving the real app in a real Chromium against the real backend, and re-verified the same
way. Before/after screenshots of every surface changed, in **dark, light and night**, are in
`docs/screenshots/wavec/` (`before-*` / `after-*`, 72 files).

## The three findings that matter most

### C0: Pressing "Switch to Night Shift" left the app half-converted: **BLOCKER: FIXED**

Found by toggling the theme live rather than reloading into it. **Every element whose CSS
`transition` list includes `background` kept the previous theme's background permanently.**
The transition is driven by a custom property that changes underneath it, and the computed
value stayed pinned at the start instead of landing on the end.

Measured on a live light → night flip: **12 of 34 painted elements did not change at all**,
including `.shell__nav-item` (the entire left navigation), `.shell__admit-patient` (the
primary call-to-action), `.clock-widget` and `.pulse-trigger` in the header. The result is a
true-black page still wearing an ivory nav rail, a white clock chip and a clay CTA.

**Why every previous theme sweep missed it, including Wave A's 223 screenshots.** Those set
`localStorage` and **reload**; a fresh page has no previous value to be stuck on, so all three
themes photograph perfectly. The defect exists only on the *toggle*, which is precisely the
path an investor takes when they ask to see Night Shift mode, and the button is in the header
of every screen.

Fixed by suppressing transitions across the swap, `:root.orb-theme-switching` in
`index.css`, applied and released **synchronously around a forced style flush** in
`ClinicianShell.tsx`. Deliberately **not** inside the `prefers-reduced-motion` block: this is
a correctness rule, not a motion preference.

**Two wrong versions were caught in the browser before this one, and both are worth
recording**, they are the same class of trap:

1. *Release on `requestAnimationFrame`.* **rAF does not fire at all while the document is
   hidden** (verified: `visibilityState: 'hidden'`, no callback in 500ms). The kill-switch
   stuck on the root permanently, which disables **every transition in the product** until
   the next theme change. A theme applied on a minimised window or a background tab would
   have done that to a real clinician.
2. *Release on a `setTimeout` backstop.* The class was released correctly, but the colours
   still froze from the second flip onward: without a forced flush, a hidden document
   coalesces the whole sequence and never observes the transitions-off state.

The shipped version does all four steps in one synchronous task with `void root.offsetHeight`
between them, which makes the browser commit the new values whether or not it intends to
paint. Verified through **four consecutive flips** (night → dark → light → night → dark)
driven by the real in-app button, in a hidden document, the hardest case: every sampled
element followed every flip, the class never stuck, transitions stayed at 0.14s. Evidence:
`docs/screenshots/wavec/after-13-live-theme-toggle-night.png`, reached by pressing the
button rather than reloading.

### C1: The flagship demo beat was dead in the order the demo script uses: **BLOCKER: FIXED**

Wave B's headline beat is *"Orders → Order sets → Sepsis Six → patient Suresh Reddy"*. Driven
in exactly that order, protocol first, patient second, **the screen did nothing**. No item
list, no STOPPED chip, no Apply button, no explanation. Just a title, a citation, and white
space.

`OrderSetsPanel.tsx` ran the safety-gate preview from the protocol card's `onClick` only,
while a separate effect *cleared* the preview whenever either input changed. Pick the patient
last and the click's fetch survived. Pick the protocol last, which is how the screen reads,
a full-height list of protocols on the left against a small picker top-right, and how the
demo script is written, and the effect wiped what the click had just fetched, and nothing
ever refetched it.

Both halves now live in one effect keyed on `(patientId, selectedSet)`, with a `live` guard
so a slow preview can never paint onto a patient it was not run for. The invalidation and the
refetch cannot drift apart again because there is exactly one place that decides what is on
screen for a given pair.

Pinned by `src/components/orders/OrderSetsPanel.order.test.tsx` (4 tests). **Confirmed the
test catches it**: with the old behaviour restored, 3 of the 4 fail.

### C2: `tsc --noEmit`, the frontend gate quoted in Waves A and B, typechecks zero files: **CRITICAL PROCESS: FIXED**

The root `tsconfig.json` is a solution-style file, `"files": []` plus two `references`. Run
`tsc --noEmit` against it and TypeScript ignores the references entirely:
`npx tsc --noEmit --listFiles | wc -l` returns **0**. Every "tsc --noEmit passes" in this
document so far was a gate that inspected nothing.

The real check is `tsc -b`, which is what `npm run build` runs. On arrival it reported
**11 errors, and `npm run build` therefore failed**, the production build of Orb did not
compile:

- 7 × `src/pages/panels/RelayPanel.tsx`, Wave B's `MergeableMessage` declared a
  `[k: string]: unknown` index signature, and TypeScript only accepts an index signature from
  a type that declares one. `Message` in `api/client.ts` does not, so the real Relay message
  type was not assignable and every call site was an error. Nothing in `messageMerge.ts` ever
  indexes by an arbitrary key, so the index signature is gone; the generic
  `T extends MergeableMessage` already carries every extra field through.
- 1 × `src/pages/panels/RoundPanel.test.tsx`, a `vi.fn()` mock typed with no parameters,
  called with one.
- 3 × my own new test file, same class, fixed in place.

**`tsc -b` now exits 0 and `npm run build` succeeds** (86 precache entries, PWA generated).
This one is worth saying out loud to the founder: a green gate that measures nothing is worse
than no gate, because it buys false confidence. **Change the documented command to
`npx tsc -b`.**

## Consistency, alignment and rhythm

| § | What a buyer would have seen | What was done |
|---|---|---|
| **C3** | **Every tab switch slid the page sideways.** Measured across the twelve doctor tabs, the first line of content began at x=**260** on Patients / Ward Round / Forecast / Appointments, **276** on Command, **288** on Simulator, **292** on Orders and Revenue, and its top edge ranged over 84–186px. Six panels each added their own 16–32px page padding on top of `.shell__body`'s gutter. Nobody names this; everybody reads it as software assembled by different teams | The page gutter is declared **once**, on `.shell__body`. Removed at source from Orders, Revenue, Simulator, Command, Lens and Scribe (each is mounted in exactly one place, this shell), including their responsive steps, which double-counted the shell's own. Re-measured across all twelve tabs: every panel wrapper now begins at **x=260**, and first content is at 260 on the eight list-style panels. The three remaining offsets are intentional and unchanged, Sage/Scribe/Lens are centred empty states, and Command's title is indented by its live-status dot, which itself sits flush at 260 with the KPI cards beneath it. **Zero console errors across the twelve-tab sweep** |
| **C4** | **Two sibling tabs, two page-title treatments.** "Orders" is Fraunces 28/500; "Order sets", one click away, was Inter 20/700 | Order Sets now uses the identical recipe |
| **C5** | **Three different recipes for the same status callout.** Order Sets drew a full-strength status border with **no fill**, on the warn tone that reads as a focused input, not a warning; OfflineBanner uses 12%/45%; DESIGN.md §2 specifies 10%/28% | Order Sets moved onto the §2 recipe, one shape, three tones (warn/danger/ok), so a clinician learns it once. **OfflineBanner deliberately left at 12%/45%**: its file documents the heavier border as an intentional emphasis choice (a stale clinical record is a hazard, not a degraded convenience) and its pairs are already covered by the call-site contrast scanner. Two recipes now, one of them a documented exception rather than an accident |
| **C6** | ~12 off-scale `rem` font sizes and off-scale padding/radii in Order Sets (0.66/0.72/0.74/0.76/0.78/0.82/0.85/0.88/0.92rem, `999px`, `14px`, `18px`) | Mapped onto the type scale and the 4px spacing grid; `--radius-full`; buttons back to `--radius-sm` like every other button |

## States

| § | Finding | Status |
|---|---|---|
| **C7** | **The disabled Apply button did not look disabled.** `.order-sets__apply:disabled` set `cursor` and nothing else, and because the button is not `.btn-primary` the global neutral-pair swap never reached it. A blocked Sepsis Six rendered a full clay Apply button, visually identical to a live one, *the single control the entire safety claim of that screen rests on* | **FIXED**, flat neutral pair, no opacity. Verified in the browser: `background: rgb(232,230,223)`, `disabled: true` |
| **C8** | Orders / Order Sets / Revenue empty states were one line of grey text floating in a void: no icon, no shape, no statement of what would appear, and **the error state used the same container**, so "no orders for this patient" and "we could not load the orders" were the same picture. On a CPOE screen those must never be confusable | **FIXED**, shared shape across the three, so nothing jumps between states; dashed hairline for empty, danger wash + `role="alert"` + Retry for failed, and the failed copy says plainly *"This is a display failure, not an empty chart."* |
| **C9** | **Ward Round's scroll affordance did not exist in two of three themes.** The cue was a hardcoded `rgba(0,0,0,0.14)`, faint on ivory, nearly gone on dark, and literally black-on-black on night. Two of the three summary columns routinely hold 375–693px of text in a **164px** box, so a column with 500px of unread clinical text below the fold looked exactly like a column that had ended | **FIXED**, derived from `--text-primary`, so it is a dark haze on ivory and a light one on charcoal. Measured per theme: dark `srgb(.96 .96 .93 / .2)`, light `srgb(.16 .16 .15 / .2)`, night `srgb(1 1 1 / .2)`. Covers widened 24→32px so the last partial line dissolves instead of being sliced mid-glyph |

## Design-system debt that was still shipping

| § | Finding | Status |
|---|---|---|
| **C10** | **All three Scribe dialogs referenced CSS that does not exist.** `.admission-modal-backdrop`, `.admission-modal`, `.admission-modal__header`, `.admission-modal__close`, `.admission-modal__form` and `.animate-scale-up` were defined by **no stylesheet in the project**, confirmed at runtime by walking `document.styleSheets` for matching rules: zero. With no `position: fixed`, the "overlay" was an ordinary block in the document flow: it did not cover the page, was not centred, and pushed the panel below it down. The Structured AI Note dialog papered over part of it inline with `blur(16px)` and `zIndex: 100`, below every other overlay in the app (9999) | **FIXED**, written properly, matching `.modal-overlay` in Vigil.css/PatientShell.css. Verified in the browser: fixed, covers the viewport, centred, `--bg-elevated`, `--radius-xl`, `--elev-3`, z-index 9999 |
| **C11** | `@keyframes scale-up` was undefined project-wide, so the Surgical Suite's confirmation dialog also had no entrance | **FIXED**, declared in `index.css`, not a panel stylesheet: panel CSS ships in that panel's lazy chunk, so defining it next to Scribe would have meant it existed only after someone opened Scribe |
| **C12** | **`backdrop-filter`, explicitly retired by DESIGN.md, was still in four places**: Scribe's modal (16px), Vigil's ambient window (24px), Surgical's confirm (10px), and the Pharmacy verify queue's *cards* (24px + saturate, making it the only panel in Orb whose cards sampled the page behind them). A full-viewport 24px backdrop blur is also the most expensive thing those panels can ask the appliance GPU to do while live vitals animate behind them | **FIXED**, zero `backdrop-filter` remain in `src/`. Canonical `rgba(0,0,0,0.6)` scrim; the pharmacy cards are now flat opaque paper |
| **C13** | The header's critical-alert bell was **the only emoji in the global chrome**, sitting among five line-SVG buttons at a different size (36 vs 34), a different radius (10 vs 8), a different fill, and behind a blur. Worse: **muted and unmuted were both 🔔**, the only difference between "this ward will be paged" and "it will not" was a colour shift on a glyph that paints in its own colours and ignores CSS `color` entirely | **FIXED**, line-SVG bell / slashed-bell that actually carry the state, on a shared `.shell__icon-toggle` recipe. Verified identical geometry to the theme toggle beside it |
| **C14** | I-PASS handover, a surface this document says to lead with, coloured illness severity with raw Tailwind hexes `#dc2626 / #d97706 / #16a34a`, plus `#6b7280` and `rgba(128,128,128,0.3)` borders. Fixed values do not follow the theme, and they sit outside the contrast gate because the gate resolves tokens | **FIXED**, status **fill** tokens (the chip is a background; its label is `--status-on-fill`), `--border-subtle`, `--text-muted` |

## Gates

| Gate | Before Wave C | After |
|---|---|---|
| `npx tsc -b` (**the real typecheck**) | **11 errors, `npm run build` FAILED** | **0, build succeeds** |
| `npx tsc --noEmit` (the quoted gate) | passes, but **inspects 0 files**, do not rely on it | unchanged; stop quoting it |
| `node scripts/check-contrast.mjs` | passes, 378 pairs, 1801 call sites | passes, 378 pairs, **1852** call sites, 3 themes |
| `npx vitest run` | 231 passed / 32 files | **235 passed / 33 files** |
| `npm run build` | **fails** | **succeeds** |
| `pytest tests/ -q --no-header` (bare) | 3728 passed / 2 skipped / 5 xfailed | **3728 / 2 / 5, 0 failed, unchanged**, as expected: no server file was touched |
| `node scripts/lint-ratchet.mjs` | **already failing on arrival** | still failing, **not worsened**, see below |

**On the lint ratchet, honestly:** it reports 457 problems against a baseline of 377 last
updated 2026-07-29, i.e. before Waves A and B added code. Files Wave C never touched already
account for **448** of the 457 on their own, so the ratchet was red before this wave started.
The four files Wave C edited contribute 9, every one of them the same pre-existing
`react-hooks/set-state-in-effect` pattern that was in those files beforehand; the file
rewritten most heavily, `AlertPermissionButton.tsx`, is at **0**. Someone should re-baseline
it deliberately, but not tonight, and not by me, because that would hide whatever Waves A
and B actually added.

## Still not right: be honest about these

1. **The Ward Round summary columns are genuinely too short.** On a 900px window the three
   columns get **164px** each while the dictation block below takes 272px of the 707px
   available. "Current issues" holds 693px of content. The scroll cue now works in every
   theme and the last line dissolves rather than being sliced, but a doctor still sees about
   a quarter of the column. This is a height-budget decision, not a bug, and I deliberately
   did **not** re-cut it: Wave B had just fixed a serious overlap in this exact layout and
   pinned it with tests, and re-litigating the flex budget the night before an audit is the
   wrong risk. **Recommend:** on the demo machine, run the browser at ≥1080px tall, or plan
   to shrink the dictation block after the audit.
2. ~~**The Sepsis Six block reason reads generically.**~~ **FIXED 2026-08-09.** The ceftriaxone
   line came back STOPPED with *"Severe drug-safety alerts must be acknowledged with a reason
   before this order proceeds"*, a sentence about the PROCEDURE that never named the finding,
   on the single most-demoed screen in the product. `order_sets_service.py:753` was taking the
   HTTP envelope's message and discarding `safety.allergy_alerts`, which already carried the
   real sentence; the dose-guard branch twenty lines below had always surfaced its findings
   properly, so the two paths simply disagreed. It now reads:

   > **STOPPED**, *"Patient is allergic to Ceftriaxone (documented reaction: Anaphylaxis, 
   > urticaria, angio-oedema and hypotension after IV ceftriaxone in 2023)."*

   Verified on screen: 8 items Cleared, ceftriaxone STOPPED with the named reaction, and
   *"a set is applied whole or not at all"*. `tests/test_order_sets.py` 55/55 still pass.
3. **Emoji are still used as content** in the What-If Simulator's six scenario preset chips
   (🌙🛏🤒🚑⚡📈) and in a few Scribe headings (✨). Unlike the header bell these are labels
   inside playful controls rather than the global chrome, and swapping them is a copy change,
   not a styling one. Left deliberately.
4. **Scribe still shows three microphone controls at once**, two in the composer plus the
   floating Sage orb (Wave A §2.8). Untouched: the underlying question is which of them owns
   the mic, which is behaviour, not polish.
5. **`.order-sets__config`** prints *"editable by an engineer, in the site configuration file
   not through this API"*. Saying "this API" to a clinician is odd, but the string is
   composed server-side and is out of scope.

## Operational note added by Wave C

The Vite dev server that had been running since 31 July was restarted (its HMR module graph
had gone stale and was serving a cached transform). It is back on **5173**, and the backend
is untouched on **8000** (`main:socket_app`, `ORB_DISABLE_SIMULATOR` unset). **The backend
start rule in §16.1 is unchanged and still critical.**

---

# WAVE B: WHAT WAS FIXED (2026-08-08, 02:15–03:20)

Everything below was re-verified in a real Chromium session against the real backend
(`main:socket_app`, port 8000, `ORB_DISABLE_SIMULATOR` unset) and the real Vite dev server,
signed in as DOC001 and NUR001. Where a fix could be pinned by a test, it has one.

## Fixed and verified in the browser

| § | What was wrong | What was done | Evidence |
|---|---|---|---|
| **2.1** | Vitals simulator crashed on **every** 5-second tick; nothing on any ward board moved for a week | `server/services/bot_service.py`, a NULL vitals channel is coalesced to the patient's severity target before any arithmetic, **and** the per-patient body is isolated so one bad row can never cost the whole ward its tick | **0** `Simulation bot error` lines since restart (was 583 in 50 min); 61 vitals rows written in 2 min; Rajesh Iyer's strip moves live |
| **2.2** | Every patient rendered `FEMALE` | `src/lib/demographics.ts` normalises every stored dialect (`'Male'`/`'M'`/`'male'`/…); Vigil + Appointments now use it | Chart header reads `MRN: MRN-GM002 · 67Y · MALE` |
| **2.3** | `226:42:54 elapsed · 60m target` with a `0% at risk` badge on a NEWS2 13 patient | Timer past a day renders as an age (`9d 11h`) with the trigger datetime; the badge now reads `0 of 6 done · past the 60-minute target`, unmisreadable in either direction | Chart reads `9d 11h · since 7/29/2026, 2:43:45 PM · 60m target · 0 of 6 done · past the 60-minute target` |
| **3.1** | Sage answered a **general** question about a named patient, with a fabricated glucose, server injected the user's last 6 turns into every prompt; "New Chat" only cleared the client | Migration 16 adds `sage_conversations.conversation_id`; prompt history is scoped to one thread; **a missing thread id yields NO history** (safe default); "New Chat" re-mints the id | Fresh thread → DKA question answers cleanly, no patient named anywhere in the message **or** the provenance bar (`· 3 retrieved`, previously `Rajesh Iyer · 3 retrieved`) |
| **3.2** | Fast (4B) lane printed the raw XML contract into the chat bubble, `["DOC-ID"]` placeholder included | Both the streamer and the buffered parser now cut the visible answer at the first contract tag, not only at `<details>`; sections are still harvested for the details panel | Hyperkalaemia question, Thinking OFF → clean prose in the bubble, sections in the collapsed panel, zero XML |
| **3.9** | Pharmacist Sage threw two uncaught 403s on `/api/vigil/patients` | `src/lib/roles.ts`, the transcription-hint prefetch is role-gated (mirrors `ROLE_PERMISSIONS`) and its failure is `console.debug`, never an error | No 403 on the pharmacist path |
| **4.1** | Every sent Relay message rendered twice (duplicate React key) | `src/lib/messageMerge.ts`, the socket echo *claims* its own optimistic bubble, and `mergeById` is a hard one-entry-per-id invariant whatever order the three paths land in | 3 messages sent, each rendered **once**, no duplicate-key warning |
| **4.2** | Unread badges read 5428 / 5147 / 5093 | Unread now excludes the reader's **own** posts; badge caps at `99+`; the harness backlog was marked read for the demo accounts through the product's own read path (nothing deleted) | Badges are two-digit |
| **5.1** | Five-rights AdministerModal opened *behind* the nurse sheet, the eMAR round was dead | A documented four-rung stacking ladder: sheet 10000 → administer 10050 → safety interlock 10100 → session-expired 10200 | Modal is the top element at its own centre; screenshot of a usable "Administer medication" dialog over the round |
| **5.2** | `144h 8m late`, `0 obs overdue` impossible | Downstream of 2.1, fixed by the simulator | Nurse board: `0 obs overdue`, doses `32m late` / `3h 9m late` |
| **6.1** | **Order Sets had no UI at all** | Built: `src/api/orderSets.ts`, `src/components/orders/OrderSetsPanel.tsx` + CSS, reachable from an Orders sub-tab. Preview runs the real gates; Apply is disabled while anything is stopped; there is deliberately **no** control that skips a gate | Sepsis Six on Suresh Reddy → ceftriaxone item **STOPPED** by the allergy interlock, Apply disabled, "a set is applied whole or not at all" |
| **6.2 / 6.3 / 13.9** | `e.g. PAT001` 404'd on its own example; no patient picker anywhere | `src/components/common/PatientPicker.tsx`, search by name/bed/MRN over the server-scoped ward list, with a disclosed raw-id fallback that validates before it fires. Used by Orders **and** Revenue Integrity | Typing "Suresh" resolves `Suresh Reddy · GM-104 · MRN-GM004` |
| **7.1** | Dictation panel painted over the bottom 340px of all three summary columns; text unreachable | The summary owns the remaining height and contains its own overflow; each column scrolls independently with sticky headings and a scroll-shadow affordance; dictation is `flex: 0 0 auto` | Columns end y=538, dictation starts y=550, no overlap; long columns scroll |
| **7.2** | "Dictate the plan" did nothing, **silently** | Three silent-failure paths closed (absent `mediaDevices`, the unguarded second `new MediaRecorder`, `recorder.start()`); and `local_asr_available` is now reported by `/sage/ai-posture`, so the control says what it cannot do **before** the doctor speaks | Panel reads "Local speech recognition is not installed on this appliance, type the plan below." |
| **8.1** | Security tab empty while Overview counted 39 auth + 3 authz events | It asked for `event_category='auth'`; the rows are `authentication` / `authorization` / `security`. The audit route now accepts a comma-separated list | Tab requests all three categories |
| **13.6** | Appointments marked the wrong day as TODAY between 00:00–05:30 IST | `localDateKey()` in `src/lib/datetime.ts`; the only `toISOString().split('T')` in `src/` is gone | n/a |
| **14.1** | Bridge showed two identities (header "Raj Patel", body "Welcome, Geeta") | The clinical record is the authority in the chrome too; the seed now renames the P001 account to the patient it links, and the live DB was repaired | `users.P001.name` = `Geeta Sharma` |
| **NEW** | **Alert storm**, fixing 2.1 exposed that `critical` patients were exempt from the alert cooldown *entirely*, so a steady-state critical patient re-alerted every 5s and buried the scripted Relay thread | Deterioration still bypasses the cooldown (band escalation **or** a risen NEWS2), with a 30-second floor. Clinical intent preserved, noise removed | ~96 alerts/min → **5/min** hospital-wide |
| **NEW** | `patient_allergies` was **empty**, the allergy interlock had nothing to fire on anywhere in a walkthrough | Three clinically coherent allergies seeded (and added to `reset_db_and_seed.py`), chosen so the Sepsis Six set is genuinely stopped on the sepsis patient | The interlock visibly fires in Order Sets and the allergy banner shows on Ward Round |
| **7.3** | Raw `2026-08-01T19:07:35.972867+00:00` printed to a doctor mid-round | `formatUTCDateTime` in `roundSummary.ts` | No ISO string anywhere on the Ward Round page |
| **8.9** | Audit table read `Phi_access`, `Ai_inference`, `Api_mutation` | `src/lib/humanise.ts`, acronym-aware sentence case | Chips now read `PHI access`, `AI inference`, `Authentication` |
| **11.2** | Every structured note opened with `DIALOGUE: / Doctor: [Not explicitly stated in dictation]`, model scaffolding at the top of a document with a **Sign & Finalize** button | `prune_placeholder_sections()` drops a section only when *every* non-blank line is a placeholder; one real line keeps the whole section, and an all-scaffolding note is returned unchanged rather than blanked | 8 unit tests, incl. the conservative cases |
| **11.3** | `Pipeline: ASR -s`, a broken number shown to the clinician | Only stages that actually ran are printed | n/a |

## Tests added

| File | Pins |
|---|---|
| `server/tests/test_simulator_partial_vitals.py` | NULL/garbage vitals channels; **and** that one patient's failure cannot cost the ward its tick. *This file caught a second occurrence of the same bug in the clamp lines that the first fix had missed.* |
| `server/tests/test_alert_cooldown_storm.py` | A steady critical patient pages **once**, not 12×; deterioration and band escalation still page immediately; a 1-point flicker does not |
| `server/tests/test_sage_conversation_threads.py` | A new thread inherits nothing; the same thread still carries its own context; **a missing thread id yields no context, not everything** |
| `server/tests/test_fast_contract_scaffolding.py` | Loose contract sections never reach the clinician on either the streaming or buffered path, at six chunk sizes; a clinical `<` still streams; a contract-only reply never renders blank |
| `server/tests/test_scribe_placeholder_sections.py` | An empty DIALOGUE section is dropped; **a section with one real line keeps everything**; an all-scaffolding note is never blanked; a bracketed clinical value (`[K+ 6.8 …]`) is not mistaken for scaffolding |
| `src/lib/messageMerge.test.ts` | The full three-way send race in both orders yields exactly one bubble |

One existing test was **rewritten, not weakened**:
`test_sage_sticky_anchor.py::test_an_anchored_turn_is_classified_as_phi` asserted the sticky
anchor through the old user-scoped history. Its assertion is unchanged and now runs inside a
thread (which is what the Sage panel sends on every turn), and a **new companion test**, 
`test_the_anchor_does_not_cross_into_a_new_conversation`, pins the other half: across threads
the anchor must NOT carry. Net effect is one more guarantee than before.

## Gates

- `npx tsc --noEmit`, **passes**
- `node scripts/check-contrast.mjs`, **passes** (378 token pairs, 1801 call sites, 3 themes)
- `npx vitest run`, **231 passed / 32 files**
- `pytest tests/ -q --no-header` (bare), **3728 passed, 2 skipped, 5 xfailed, 0 failed** (8m38s).
  Baseline was ~3670; the delta is the 5 new files above.

## NOT done: steer around these

1. **§8.11 Roster Import still has no UI.** The backend and its tolerant-CSV importer are complete
   and tested; there was not enough of the night left to build the upload → mapping → dry-run →
   apply screen *properly*, and a half-built import screen in front of a CIO is worse than an
   honest "that runs from the admin CLI today". **Demo line:** show the Order Sets screen as the
   example of a site-configured, audited surface, and say roster import lands the same way.
2. **§1.1 PIN-on-device enrolment still has no UI.** Same reasoning, minting device codes and
   setting a PIN is an *authentication* surface, and rushing one at 3am is the wrong risk.
   **Demo line:** do not click "Set up this tablet for PIN sign-in"; sign in with employee ID.
3. **§9.1 Pharmacy (Helix) is still all zeros**, and **§10.1/§10.2 Surgical Suite still contradicts
   the medical charts.** Both are demo-*data* problems needing a reseed, which is too big a change
   to make safely tonight against a live demo DB. **Demo line:** do not open the Pharmacy role or
   the Surgical Suite; if asked, they are configured per-site and this box is seeded for General
   Medicine.
4. ~~**§13.1 `W1 30/8` ward capacity**~~, **FIXED**; the heatmap now reads six real wards at
   `5/12` each and the Command Center is worth showing rather than skipping. **§13.2
   sepsis-bundle 0%** remains, and after the reseed it is *accurate*: 16 bundles exist and
   none has a single element recorded, because nobody has worked them yet. Verified directly.
   **Better than a demo line:** run the Sepsis Six order set in front of him and let the tile
   move, instead of steering around it.
5. ~~**Residual stale rows**~~, **cleared by the reseed** (2026-08-09 11:20). The Aug-1 STAT
   order reading `228h 32m late` is gone with the rest of the old dataset.
6. **§12 voice is still off.** Whisper is not installed. Every dictation control now *says so*
   instead of failing silently, but the modality cannot be demonstrated. Cut it from the script.
7. **Not a bug, worth knowing:** the browser tab title showing `⚠ N critical alerts` only fires
   when the tab is **not focused** (`criticalAlerts.ts:isTabInattentive`). It accumulated to
   triple digits during this automated session because an automated pane never takes focus. On a
   real, focused demo window it will not appear at all.
8. **§2.9 the header bell** is still an emoji (🔕/🔔) rather than the line-SVG icon system. Its
   colours are already neutral, not amber, the "permanent error affordance" reading comes from
   the emoji, not the styling. Left alone deliberately: swapping an icon in the global chrome at
   the end of a long night, with no time to check both themes, is the wrong trade.

## Demo-day beat that is now REAL and worth leading with

**Orders → Order sets → Sepsis Six → patient "Suresh Reddy" (CAP with early sepsis).**
Nine items screen; the ceftriaxone line comes back **STOPPED** by the allergy interlock, 
"Anaphylaxis … after IV ceftriaxone in 2023" is on his chart, Apply is disabled, and the screen
says *"A set is applied whole or not at all, resolve these on the individual orders, where the
override is recorded against the dose it applies to."* That is the safety thesis, on a named
patient, in about fifteen seconds. It did not exist before tonight: the backend was complete, the
UI did not exist, and no patient in the database had an allergy.

## Operational, unchanged and still critical

**Start the backend as `main:socket_app`** (or `./run-orb.sh --dev`). `main:app` 404s every
`/socket.io/` request and silently kills all realtime, see §16.1.

**Environment left running for the morning:** backend
`uvicorn main:socket_app --host 127.0.0.1 --port 8000` with `ORB_DISABLE_SIMULATOR` **unset**
(`/api/health` 200, `/socket.io/` 200, **zero** `Simulation bot error` lines since it came up),
Vite on 5173, signed in as DOC001. There is a **new migration (16)**, the first backend start
after pulling these changes applies it automatically; nothing manual is required.

---

Every workflow below was driven end to end in a real Chromium browser against the real
FastAPI backend (port 8000, `main:socket_app`, simulator enabled) and the real Vite dev
server (port 5173). Console errors and failed network requests were captured per screen.
**223 screenshots (129 dark, 94 light)** are in `docs/screenshots/`, every nav tab for every
role, every Admin sub-tab, and every workflow step, in both themes.

Roles driven: **DOC001** (doctor, General Medicine), **DOC004** (surgeon), **NUR001** (nurse),
**PHA001** (pharmacist), **ADM001** (admin), **P001** (patient / Bridge).

Frontend gates as found: `npx tsc --noEmit` **passes**; `node scripts/check-contrast.mjs`
**passes** (378 token pairs, 1780 call sites, 3 themes). So the problems below are behavioural
and data problems, not token-contrast problems.

---

## How to read this

| Severity | Meaning |
|---|---|
| **BLOCKER** | Errors, hangs, shows nothing, or shows *wrong* data. Do not demo this path until fixed. |
| **ROUGH** | Works, but looks unfinished, misalignment, default controls, truncation, missing empty/error state. |
| **SLOW** | Leaves the user staring at nothing with no indication of progress. |
| **ASK** | Nothing is broken, but a sharp investor will ask and we would not enjoy answering. |

Status is `NOT FIXED` for everything, this was a recon wave.

---

# THE SHORT VERSION

Six things will visibly go wrong in front of the investor, in roughly this order of damage:

1. **Live vitals are not live.** The simulator has been crashing every 5 seconds for a week. Nothing on any ward board moves. `RECORDS WRITTEN (24H) = 0` is printed on the admin dashboard.
2. **Every patient's chart header says FEMALE.** A one-line comparison bug in `VigilPanel.tsx`. Fifteen male patients render as female.
3. **Sage answers a general question about a patient nobody asked about**, with a glucose value that contradicts the same patient's lab panel one tab away.
4. **Every message you send in Relay appears twice.**
5. **The nurse's five-rights medication confirmation opens behind another modal and is invisible.** The eMAR round cannot be completed.
6. **The demo data is a week stale and screams it**: "144h 8m late", "226:37:17 elapsed · 60m target", "55 earlier doses not charted", "5,428 unread".

Plus two listed demo workflows that have **no user interface at all**: order sets and roster import.

---

# 1. LOGIN

### 1.1: PIN-on-device is advertised but cannot be reached: **BLOCKER (for that workflow)**: PARTIAL _(2026-08-09: tablet-binding UI exists (Login.tsx:471); nothing mints a setup code and setOwnDevicePin (src/api/deviceAuth.ts:80) still has no caller)_
The login card offers "Set up this tablet for PIN sign-in". It opens a "TABLET SETUP CODE"
field which needs a one-time code that only an administrator can mint. There is **no admin
UI anywhere** that mints one: `POST /api/auth/devices` and `/api/auth/devices/{id}/revoke`
exist on the server and are not referenced anywhere in `src/`.

Worse, `setOwnDevicePin()` is defined in `src/api/deviceAuth.ts` and **is never called from
any component**, so even on an enrolled tablet a nurse has no way to set her PIN through
the product. The whole feature is API-only behind a login-screen link that leads to a
dead end. Screenshot: the setup panel is reachable, the flow is not.

### 1.2: Tablet setup code is a resizable `<textarea>`: **ROUGH**: NOT FIXED _(2026-08-09: Login.tsx:282 still a resizable textarea: now a deliberate keep (Login.css:212))_
The one-time-code field has a browser resize grip in its bottom-right corner and renders its
placeholder in monospace over two wrapped lines. Looks unfinished on the first screen anyone sees.

### 1.3: Login page is a small card in a very large empty field: **ROUGH**: PARTIAL _(2026-08-09: mark and titles improved; Login.css:16 still caps content at 440px)_
No product name beyond a 24px mark, no version, no build stamp, ~80% dead space at 1512×950.

### 1.4: Login is fast: **OK**: OK _(2026-08-09: unchanged)_
2.75 s from submit to a painted dashboard, consistently, across all five roles.

---

# 2. WARD BOARD / VIGIL

### 2.1: LIVE VITALS ARE FROZEN: **BLOCKER**: FIXED _(2026-08-09: bot_service.py _coalesce_vital + per-patient try/except; measured 210 rows/40s and moving on screen)_
**This is the single biggest problem.** The vitals simulator throws on every 5-second tick:

```
ERROR orb.vitals_source.simulator: Simulation bot error:
  unsupported operand type(s) for -: 'float' and 'NoneType'
```

Root cause, confirmed by traceback, `server/services/bot_service.py:145`:

```python
dia_shift = random.gauss(0, noise["dia"]) + reversion * (targets["dia"] - latest["diastolic_bp"])
```

Eight admitted patients have `diastolic_bp = NULL` in their most recent vitals row
(`PAT-8A83AB99`, `PAT-5CA522C6`, `PAT-C464F904`, `PAT-769746BB`, `PAT-9FAFD625`,
`PAT-9041E4AD`, `PAT-A96E4C3C`, `PAT-A4BAC4CF`). The exception escapes `_generate_vitals`
**for the entire batch**, so *no* patient gets a new reading, not just the eight.

Measured in the browser: Rajesh Iyer's vitals strip read `HR 124 / BP 83/35 / SpO2 80 /
Temp 37.4 / RR 35` and was **byte-identical 16 seconds later**. The newest vitals row in the
database is `2026-08-01T19:08:12`, seven days old.

Consequences visible elsewhere in the demo, all from this one bug:
- Admin → Overview: `RECORDS WRITTEN (24H) 0 · 0 vitals · 0 observations`
- Admin → Alerts: "No ingestion-driven alerts have been generated yet"
- Nurse board: "obs 144h 8m late"
- Patient chart: "SEPSIS SIX 226:37:17 elapsed · 60m target"
- I-PASS: "Readings analysed: 1", "Trend over shift: insufficient_data"
- Command Center: "SEPSIS BUNDLE COMPLIANCE 0%"

Note: a previous session had `ORB_DISABLE_SIMULATOR=1` set on the running backend, which
silences the log spam but of course also guarantees dead vitals. Do not ship the demo that way.

### 2.2: EVERY PATIENT RENDERS AS "FEMALE": **BLOCKER**: FIXED _(2026-08-09: VigilPanel.tsx:1177 via sexLabel() in src/lib/demographics.ts:18; renders '67Y · MALE' on screen)_
`src/pages/panels/VigilPanel.tsx:1146`:

```tsx
MRN: {patient.mrn} · {patient.age}Y · {patient.sex === 'M' ? 'MALE' : 'FEMALE'}
```

The database stores `'Male'` / `'Female'`, never `'M'`. So the chart header for **Rajesh Iyer
(DB: Male) reads `67Y · FEMALE`**, and so does every other male patient's.

It is worse than a cosmetic bug because the MedGemma assessment card *directly underneath it*
on the same screen says *"Rajesh Iyer, a 67-year-old **male**…"*. The screen contradicts itself.
The nurse board (`67M`, `55M`) and I-PASS (`34y Female`) get it right, only Vigil is wrong.

### 2.3: Sepsis Six timer reads 226 hours against a 60-minute target: **BLOCKER (credibility)**: PARTIAL _(2026-08-09: formatting fixed (CareBundleTimer.tsx:61 renders '10d 8h'); the absurd elapsed-vs-60m is STALE SEED DATA: see §16.4)_
The patient chart shows `SEPSIS SIX · 226:42:54 · elapsed · 60m target · 0/6 done · 0% complete`
with all six tasks stamped **Overdue**. Next to it, a badge reads **"0% at risk"** on a patient
whose NEWS2 is 13 CRITICAL, two contradictory statements 200px apart.

### 2.4: The MedGemma chart assessment takes 19 s to say its first word: **SLOW**: PARTIAL _(2026-08-09: streaming + skeleton added; StrictMode still double-fires the analysis request (VigilPanel.tsx:2242))_
Measured: **19.1 s** to first token, **30.1 s** to settle, on a warm 27B. The card reads
"Generating…" for nineteen seconds with no spinner, no progress, no elapsed counter.
The patient chart is the screen the founder will open most often.

Also: `GET /api/vigil/patients/{id}/analysis` fires and is immediately `ERR_ABORTED`
(React StrictMode double-mount), then re-fires, so the model is asked twice.

### 2.5: The patient-chart action bar is a ragged cluster of ten buttons: **ROUGH**: PARTIAL _(2026-08-09: two icons de-emoji'd; 💊/📝/🧬 and five bare ALL-CAPS buttons remain (VigilPanel.tsx:1246-1258))_
Three uneven rows mixing emoji-prefixed sentence case (`🖼 Diag. Reports`, `🧪 Lab. Reports`,
`💊 Medications`, `📝 Notes`, `🧬 Story`) with bare ALL-CAPS (`HANDOFF`, `DOWNTIME`,
`PRINT RECORD`, `PRINT MEDS`, `DISCHARGE`). Different fill treatments, ragged right edge,
no grouping. This is the most-photographed surface in the product.

### 2.6: "Suresh Reddy · CAP with early sepsis · 0%": **ASK**: PARTIAL _(2026-08-09: chart header reframed to NEWS2 (VigilPanel.tsx:1185); the list can still print a bare 0% (line 407))_
A patient whose diagnosis is *early sepsis* is scored 0% risk in the Active Patients list.

### 2.7: Sidebar user chip truncates in every role: **ROUGH**: FIXED _(2026-08-10: ClinicianShell.tsx:586-587: both user-chip lines carry title attributes recovering the full name and role · department on hover)_
`Dr. Meera Shar…` / `Physician · Gene…`, `Nurse · General …`, `Administrator · A…`,
`Pharmacist · Pha…`, `Dr. Vikram R…`. The signed-in user's own name is cut off on every screen.

### 2.8: Two floating buttons collide bottom-right: **ROUGH**: FIXED _(2026-08-09: single 48px FAB (SageFloatingAssistant.css:4), lifted to bottom:96px on the Sage tab)_
A small mic pip overlaps a large orange mic FAB on every panel, and the pair sits on top of
page content (Forecast's occupancy table, Ward Round's "Seen · next bed" button, the nurse
board's last row).

### 2.9: The header bell is permanently in a blocked/error state: **ROUGH**: FIXED _(2026-08-09: AlertPermissionButton.tsx:27 now line-SVG bell, plain disabled state)_
`aria-label="Notifications blocked, enable notifications in browser settings"`, rendered as
a 🔕 emoji in an amber-outlined button. On demo day the browser will not have notification
permission, so a persistent warning affordance sits in the chrome for the whole session.

---

# 3. SAGE

Sage is simultaneously the strongest and the most dangerous surface in the product.

### 3.1: A GENERAL QUESTION WAS ANSWERED ABOUT A PATIENT NOBODY MENTIONED: **BLOCKER**: FIXED _(2026-08-09: load-bearing fix is SERVER-side: sage_service.py:2902-2926 thread-scopes history; newConversationId is the client half)_
Screenshot: `docs/screenshots/sage-01-general-thinkingON-dark.png`.

Fresh login. Straight to the Sage tab. Landing screen, no chart opened, no patient selected.
Question typed: *"What is the diagnostic threshold for diabetic ketoacidosis?"*

The answer came back:

> **Hyperglycemia:** Blood glucose level greater than 250 mg/dL. **Rajesh Iyer's current
> glucose of 285 mg/dL meets this criterion.**
> …
> Therefore, while **Rajesh Iyer** meets the hyperglycemia criterion…

and the provenance bar underneath reads **"Hide details · Rajesh Iyer · 3 retrieved"**.

Two separate problems, both bad:

**(a) The patient came from stale server-side conversation history.** `routes/sage_routes.py:1420`:

```python
history = await run_db(get_conversation_history, user["id"], limit=6)
```

The server unconditionally injects that user's **last six turns** into every prompt.
`SagePanel` clears its own `messages` array on mount and on "New Chat", but the server does
not care. So "New Chat" is a lie: the previous conversation (in this DB, *"What is Rajesh
Iyer's NEWS2?"*) is still in the prompt. A clinician who asks a patient question and then a
general question gets the general question answered **about the previous patient**. On a shared
ward workstation that is a PHI-crossing problem, not just a demo problem.

Proved by contrast: the same question posted straight to `POST /api/sage/query` with no
history returns a clean, patient-free, correctly-cited answer. Twice.

**(b) The glucose is wrong.** Sage says Rajesh Iyer's glucose is **285 mg/dL**. His own chart's
LAB TRENDS panel, one tab away, says **GLUCOSE 117 mg/dL**. If the investor cross-checks, 
and this is exactly the check a careful investor performs on an AI clinical product, we have
an AI quoting a fabricated value against a named patient.

### 3.2: Fast mode dumps raw prompt scaffolding into the chat bubble: **BLOCKER**: FIXED _(2026-08-09: XML contract now legacy-only behind ORB_SAGE_XML_CONTRACT (sage_service.py:1203))_
With Thinking toggled **off** (the 4B "Fast" lane, the lane the demo script is built around),
the answer to *"What are the classic ECG findings in hyperkalaemia?"* rendered as:

> The classic ECG findings of hyperkalemia include peaked T waves … **Details: `<display_mode>`
> concise `<patient_facts_summary>` None `<local_policy_guidance>` None `<external_guidance>`
> None `<clinical_assessment>` … `<caveats>` … `<escalation>` … `<cited_sources>` `["DOC-ID"]`**

The structured-output parser fails on the small model, so the raw XML contract, including the
literal placeholder `["DOC-ID"]`, is printed to the clinician. This is on the **fast path**,
i.e. the exact beat the demo is supposed to use as its speed showcase.

### 3.3: "Sub-second general answers" did not hold; and Sage gets slower the longer you demo it: **SLOW**: FIXED _(2026-08-09: history hard-clamped (sage_service.py:1268); the 27-95s figures were a degraded box: now 71.7 tok/s)_
Measured in the browser, warm 27B + 4B, first token / settled:

| Question | Thinking | First token | Settled |
|---|---|---|---|
| DKA diagnostic threshold | ON | 3.6 s | 14.9 s |
| Hyperkalaemia ECG findings | **OFF (fast lane)** | **5.7 s** | 6.9 s |
| "Ananya Kapoor's latest NEWS2 and why?" | ON | **12.1 s** | 23.4 s |
| "And what should I do about it?" (follow-up) | ON | 4.8 s | 23.8 s |
| "Mechanism of action of metformin?" *(after New Chat)* | ON | **25.0 s** | 30.2 s |
| "Vikram Singh's cardiology plan?" *(refusal)* | ON | **0.41 s** | 1.6 s |

Three things to note:
- The fast lane (5.7 s) was **slower to first token** than the careful lane (3.6 s). The
  "sub-second contrast" demo beat does not currently exist.
- The patient question at 12.1 s is roughly double the ~6–8 s hardware floor we treat as known-good.
- The **general** metformin question took **25 s**, *because* the six-turn history bug (3.1a)
  stuffed two long Ananya Kapoor answers into the prefill. **Sage degrades monotonically over
  the course of a demo session.** The longer the founder talks, the slower it gets.

While waiting, the user sees `Searching clinical guidelines…` then
`Generating with local MedGemma, this can take a minute or two…`. Honest, but "a minute or
two" is not a sentence we want on screen in front of a cheque.

### 3.4: Raw retrieval markup is printed inside the prose: **ROUGH (severe)**: FIXED _(2026-08-10: '(DOC: …)' markers are stripped in the shared sanitizer all three lanes run (sage_routes.py:97-98 in FORBIDDEN_PHRASE_PATTERNS); test_sanitizer_lane_parity.py proves no lane lets one survive)_
> *"According to local policy **[LOC][Resp] Tier 2 Escalation Protocol for High-Risk Patients
> (DOC: LOC-SAFE-007)**, this warrants immediate escalation…"*

`[LOC][Resp]` and `(DOC: LOC-SAFE-007)` are internal retrieval tags. The same leak appears as
`[EXT][General] … (DOC: EXT-METAB-001)` on the raw SSE stream. A clinician should never see
a document ID.

### 3.5: Retrieval precision is visibly poor and we show it: **ASK**: NOT FIXED _(2026-08-09: ASK: no relevance threshold exists; observation still holds)_
For a DKA question, the "DOCUMENTS SAGE READ" panel listed:
1. Diabetic Ketoacidosis (DKA) Protocol ✓
2. **Blood Transfusion Thresholds** ✗
3. **ARDS Berlin Criteria & Management** ✗

with the caveat *"Sage did not attribute its answer to a specific document."* Two of three
retrieved documents are irrelevant, in the open, on the citation panel.

### 3.6: Every answer wears "Limited evidence" and "Partial data" banners: **ASK**: NOT FIXED _(2026-08-09: ASK: SageMessage.tsx:307,312 banners unchanged)_
`🔍 Limited evidence, Verify independently` and `⚠️ Partial data: No signed Scribe notes,
No recent imaging` appeared on both patient answers. Honest, and defensible, but the investor
will read them, and we should have the answer rehearsed.

### 3.7: "Recent Conversations" is a turn log full of duplicates: **ROUGH**: NOT FIXED _(2026-08-09: SagePanel.tsx:783 still one entry per user turn; server now sends conversation_id but client ignores it)_
The sidebar showed *"What is the diagnostic threshol…"* **three times**, *"What is Rajesh
Iyer's NEWS2?"* twice, and listed the follow-up *"And what should I do about it?"* as its own
separate conversation. Every entry is labelled **"General Medical"**, including the patient ones.

### 3.8: Cross-department refusal is excellent: **STRENGTH**: STRENGTH _(2026-08-09: cross-department refusal intact (sage_service.py:1464))_
*"What is Vikram Singh's cardiology plan?"* → **0.41 s**:
> "That patient is outside your department. There is a patient on record matching that, but
> they are not under your department's care… This is not the same as 'no such patient': the
> record exists, your access to it does not… This request has been recorded in the audit log."

Demo this. It is the best thirty seconds in the product.

### 3.9: Pharmacist Sage throws two 403s on load: **BLOCKER (minor)**: FIXED _(2026-08-09: SagePanel.tsx:301 gates getPatients() on canSeeWardList(); roles.ts:11 excludes pharmacist)_
Logging in as PHA001 and opening Sage produces, twice:
```
HTTP 403 GET /api/vigil/patients
[error] Error: Insufficient permissions  at apiFetch (src/api/client.ts:184)
```
`SagePanel`'s mount effect calls `getPatients()` unconditionally (to bias voice transcription)
without checking the role. Console-only today, but it is an uncaught permission error in the
one panel a pharmacist can reach.

### 3.10: Suggestion cards use emoji icons: **ROUGH**: NOT FIXED _(2026-08-09: SageEmptyState.tsx:5-8 emoji icons; emoji also spread to SageMessage.tsx:275-280)_
📋 🧪 🏥 💊 on the Sage landing screen, against a line-SVG icon system everywhere else.

---

# 4. RELAY

### 4.1: EVERY SENT MESSAGE RENDERS TWICE: **BLOCKER**: FIXED _(2026-08-09: RelayPanel.tsx:127 claimOptimistic + mergeById on all three paths)_
Sent one message. It appeared twice. Sent a second. It appeared twice, each copy carrying its
own wrong-room warning card. React logged:
```
Encountered two children with the same key, `79061`.
```
The database has exactly **one** row per message (`id 79061`, `id 79062`), this is purely a
client render race in `src/pages/panels/RelayPanel.tsx`:

1. `setMessages(prev => [...prev, optimisticMsg])`, temp id (line ~305)
2. socket `message:new` arrives with the **real** id; `handleNewMsg` (line ~118) checks
   `prev.find(m => m.id === msg.id)`, does not find the real id (only the temp one), appends
3. `sendMessage()` resolves; line ~315 swaps the optimistic entry **to the same real id**
4. → two entries, one id, duplicate key, doubled bubble

The `getMessages()` refresh that follows does not reliably win the race.

This is the most visible interaction defect in the product: the founder types a message on
stage and watches it print twice.

### 4.2: Unread badges read 5,428 / 5,147 / 5,093: **BLOCKER (credibility)**: PARTIAL _(2026-08-09: relay_service.py:52 sender_id filter is correct; harness volume in the demo DB can still inflate the count until a reseed)_
The Relay case-room list shows unread counts of **5428** (Priya Nair), **5147** (Rajesh Iyer),
**5093** (Suresh Reddy), **202** (General Medicine), and, inconsistently, **no badge at all**
on Ananya Kapoor, the CRITICAL patient. This is soak/benchmark harness debris in the demo DB.
Nobody believes a ward chat with five thousand unread messages.

### 4.3: The scripted ward thread is duplicated inside the room: **ROUGH**: NOT FIXED _(2026-08-09: retraction_harness.py:159 never deletes prior messages, so re-runs stack the narrative)_
The hyperkalaemia narrative appears twice in Priya Nair's room, once keyed to clinical time
0958 and once to 1011, the benchmark harness posted it more than once.

### 4.4: Every message is stamped 04:38–04:39 PM while its text cites 0744/0826/0950: **ROUGH**: NOT FIXED _(2026-08-09: seed stamps wall-clock at insert time; message text still cites 0744/0826/0950)_
Wall-clock timestamps are all bunched at the seed moment and contradict the narrative times
inside the messages the investor will be reading aloud.

### 4.5: Wrong-room detection is excellent: **STRENGTH**: STRENGTH _(2026-08-09: room-fit intact; with §4.1 fixed the warning now paints once)_
Posting *"Ananya Kapoor in GM-101 needs her insulin infusion rate increased…"* into Priya
Nair's room produced, in ~6 s:
> ⚠ **This looks like it belongs to Ananya Kapoor**, The message names Ananya Kapoor and
> records clinical detail, a measurement, dose or result, about them. **It has already been
> sent; nothing is blocked.** · *It's the right patient* · *Move to Case: Ananya Kapoor*

Right behaviour, right wording, right escape hatches. Ruined only by 4.1 printing it twice.

### 4.6: The ambient Sage sidebar works but reads as a wall: **ROUGH**: PARTIAL _(2026-08-09: sidebar now structured; the retraction narrative is still one paragraph via discussion_summary)_
It correctly caught the retraction:
> *"Potassium: Retracted 6.8, Current 4.1 RETRACTED by ward message: '…HAEMOLYSED SAMPLE…'
> Hyperkalemia treatment is no longer needed."*

but it is one run-on paragraph with no structure, in a single card, above ~700px of empty column.

---

# 5. NURSE

The nurse surfaces are the best-built part of Orb. Two blockers sit on top of them.

### 5.1: THE FIVE-RIGHTS CONFIRMATION MODAL OPENS BEHIND THE MED-ROUND SHEET: **BLOCKER**: FIXED _(2026-08-09: AdministerModal.css:18 z-index 10050 vs nurse-sheet 10000, both portalled to body; VERIFIED ON SCREEN: elementFromPoint at centre lands inside the Administer modal)_
Click **Administer** on any due dose. The screen dims a second time and *nothing else happens*.
No confirmation, no error, no network request. The button appears inert.

It is not inert. The modal is in the DOM, fully populated:
```
DIV.administer-modal-backdrop → "Administer medication / PATIENT Rajesh Iyer · Bed GM-102 /
DRUG Ipratropium nebulizer / DOSE 500mcg / ROUTE Nebulizer / TIME Aug 08, 01:43 AM (now) /
CONFIRM DRUG NAME / Barcode-style check: type the dru…"
```
It is painted **underneath** `DIV.nurse-sheet-backdrop`. Both declare the same stacking level:

- `src/components/vigil/AdministerModal.css:9` → `z-index: 10000`
- `src/pages/panels/nurse/NursePanel.css:534` → `z-index: 10000`

Equal z-index ⇒ DOM order decides ⇒ the nurse sheet, mounted later, wins.

**The eMAR medication round cannot be completed from the nurse Shift board.** This is demo
step 5 and it is dead.

### 5.2: The board screams "144h late", "227h late", "55 not charted": **BLOCKER (credibility)**: PARTIAL _(2026-08-09: nurse_shift_service.py:445 rolls due_at forward; missed_slots is uncapped and obs lateness has no cap: '250h 2m late' still on screen)_
Actual strings on the nurse "Due now" board:
`144h 8m late` · `143h 38m late` · `227h 2m late` · `55 not charted` · `36 not charted` ·
`5 not charted` · `55 earlier doses not charted`.

Downstream of the frozen simulator (§2.1) plus a seed DB that has not been re-run since Aug 1.
An investor reading "227 hours late" concludes either the data is fake or the ward is negligent.

### 5.3: Stale timestamps render as bare times with no date: **ROUGH**: FIXED _(2026-08-10: the shared formatter nurse/time.ts:31 clockTime() renders any older-day moment with its date ('Aug 9, 14:32'); same-day stays a bare clock time)_
`Last 12:37 AM · obs 144h 33m late`, the "last obs" was seven days ago but is formatted
time-only, so it reads as an hour ago. The two halves of the same line contradict each other.

### 5.4: Recording an observation set works, and works well: **STRENGTH**: STRENGTH _(2026-08-09: ObsEntrySheet intact with live NEWS2 scoring)_
Filled RR 28 / SpO2 88 / SBP 92 / DBP 54 / HR 118 / Temp 38.6 / On oxygen. The panel showed
live per-parameter contributions (`+3 +3 +2 +2 +1`) and a live **NEWS2 13** card with escalation
text and driver chips (*Severe tachypnea (RR 28)*, *Severe hypoxia (SpO2 88%)*,
*On supplemental oxygen*) plus the honest footer *"NEWS2 (RCP 2017), computed from the values
above, not yet recorded."*

Saved in <5 s, board updated to `NEWS2 13`, and the row flipped from `obs 144h late` to
`next obs in 30m`. Screenshot `nurse-04-obs-filled-dark.png` is the best screenshot in the set.
**Lead the nurse demo with this.**

### 5.5: I-PASS handover is strong, with two blemishes: **ROUGH**: PARTIAL _(2026-08-09: loading state fixed; ipass_service.py:302-321 raw enum, reading count and underscore blanks remain)_
Full I/P/A/S/S draft generated per patient with severity, problem list, overdue bundle actions,
vitals, contingency plans and a read-back block. Good.

- `Trend over the shift: **insufficient_data** (insufficient recent data)`, the raw enum is
  printed next to its own human translation.
- `Readings analysed: 1`, the frozen-vitals bug again.
- `Outstanding questions: ____________. Confirmed by: ____________ at ____________.`, 
  underscore blanks in body text read like a scanned paper form.
- Expanding a patient card took up to ~6 s in our run with no loading state.

### 5.6: The eMAR's own footer says Orb is not a real MAR: **ASK**: ASK _(2026-08-09: wording unchanged in MedRoundSheet.tsx:199 and NursePanel.tsx:308)_
Printed at the bottom of every medication round:
> *"Due times are derived from each order's frequency and the last charted dose, **Orb holds
> no prescriber-authored administration schedule.** Treat them as a working list, not a
> prescription."*

Defensible engineering honesty; a direct invitation for "so you don't actually have a
prescription and administration schedule?" Have the answer ready or reword it.

---

# 6. ORDERS

### 6.1: ORDER SETS HAVE NO USER INTERFACE: **BLOCKER (missing feature surface)**: FIXED _(2026-08-09: OrderSetsPanel wired at OrdersPanel.tsx:13,188: the doc's grep was a FALSE NEGATIVE ('.' cannot match the capital S in orderSets))_
Demo step 6 is *"apply an ORDER SET, see the safety gates fire."* It cannot be done.

The backend is complete and safety-enforced:
`GET /orders/sets`, `GET /orders/sets/{id}`, `POST /orders/sets/{id}/preview`,
`POST /orders/sets/{id}/apply`, `GET /orders/sets/applications/{patient_id}`, plus
`services/order_sets_service.py` with `OrderSetBlocked`, an `order_set_applications` table,
audit hooks, and `server/site_config/order_sets.json` full of cited, India-appropriate protocols.

```
$ grep -rni "order.set\|orderset" src/    →  (no matches)
```

Zero frontend. Per the standing order this must be **built**, not hidden.

### 6.2: The panel's own example patient ID 404s silently: **BLOCKER**: FIXED _(2026-08-09: OrdersPanel.tsx:230 PatientPicker replaces free-text id; explicit failure state at :266)_
The Patient ID field's placeholder is `e.g. PAT001`. Typing exactly that produces:
```
HTTP 404 GET /api/orders/patient/PAT001
[error] Failed to load orders Error: Patient not found
```
and the screen shows **nothing at all**, no error state, no "patient not found", no retry.
Real IDs in this database look like `PAT-A73E5E98`. The panel documents an ID format it does
not accept, then swallows the failure.

### 6.3: A doctor must know the patient's opaque internal ID, and the LOINC code: **BLOCKER (usability)**: PARTIAL _(2026-08-09: patient side solved; code entry still from memory: no terminology search endpoint exists)_
There is no patient picker (the ward board is one click away and is not used). There is no
code search: the Code field's placeholder is `e.g. 2524-7` and it wants a raw LOINC/SNOMED/ICD10
value typed from memory. No autocomplete, no name lookup, no favourites.

### 6.4: Three unstyled native `<select>` dropdowns: **ROUGH**: FIXED _(2026-08-10: OrdersPanel.tsx:490/500/510 all wear Orders.module.css:313 `.select`: appearance:none plus the same line-icon chevron as every other styled select)_
Order Type, Priority and Code System are raw OS `<select>` controls with the system chevron,
sitting inside an otherwise fully custom "Quiet Clinical" surface. This is the textbook
"default controls" tell.

### 6.5: Button hierarchy is inverted: **ROUGH**: PARTIAL _(2026-08-09: Create Order is now a filled primary; the header Close toggle carries the same fill)_
The largest, most saturated button on the Orders screen is **Close**. `Create Order` is a
low-contrast ghost button.

### 6.6: Empty Orders panel is 80% dead space: **ROUGH**: FIXED _(2026-08-09: Orders.module.css:74 bounded empty state with icon and state-specific copy)_
"Enter a patient ID to view orders." centred in ~700px of nothing.

---

# 7. WARD ROUND

### 7.1: The dictation panel physically covers the one-screen summary: **BLOCKER (visual)**: FIXED _(2026-08-09: Round.css:744 dictation is flex:0 0 auto in normal flow; summary absorbs the remainder)_
The three summary columns (`SINCE 20:00 YESTERDAY` / `CURRENT ISSUES` / `DECIDE TODAY`) run
from y=315 to y=888. The "Dictate the plan" + "PLAN FOR TODAY" block is an opaque overlay
starting at y≈545. The bottom 340px of all three columns is painted over.

Visible result: `CURRENT ISSUES` is clipped mid-sentence, *"Ananya Kapoor (GM-1-GM-101) is"*, 
and covered text bleeds through at the seam (*"Temp 38.2C, HR 140, RR 35, meets … rid"*).
The page does not scroll; there is no way to read the covered clinical text.

The panel is literally subtitled *"Bed-by-bed ward round with **one-screen summary** &
dictation"* and the summary is not on the screen.

### 7.2: "Dictate the plan" does nothing, silently: **BLOCKER**: FIXED _(2026-08-09: RoundDictation.tsx:143 explicit on-screen message when local ASR is absent)_
Clicking it produces no UI change, no error, no network request. See §11, local Whisper ASR
is not installed in this deployment (`ORB_WHISPER_DAEMON_ENABLED=false`). The button gives no
feedback whatsoever. Demo step 7 explicitly lists dictation.

### 7.3: Raw ISO-8601 timestamp printed to the clinician: **ROUGH**: FIXED _(2026-08-09: roundSummary.ts:192 formatUTCDateTime())_
> `Observations overdue · Warning`
> `Last set at 2026-08-01T19:07:35.972867+00:00 · due every 30 min`

### 7.4: "Day 79" / "Day 77" of admission: **ASK**: NOT FIXED _(2026-08-09: STALE SEED DATA: reset_db_and_seed.py date anchor; now fixed to a rolling window, needs a reseed)_
Ananya Kapoor is on day 79 of a DKA admission; Sage says Rajesh Iyer is on "day 77" of a COPD
exacerbation. The seeded admission dates have drifted badly against today's date.

### 7.5: Saving a typed plan works: **STRENGTH**: STRENGTH _(2026-08-09: save + confirmation intact)_
Typed plan → `Save plan to chart` → "Saved to the chart" in <5 s, and it appeared immediately
in the `SINCE 20:00 YESTERDAY` column as a timestamped WARD ROUND note. Clean.

### 7.6: The mic FAB sits on top of "Seen · next bed": **ROUGH**: FIXED _(2026-08-09: Round.css:785 mic is an inline control, no FAB)_

---

# 8. ADMIN

### 8.1: The Security tab is empty while security events exist: **BLOCKER**: FIXED _(2026-08-09: SecurityActivity.tsx:22 now queries the three real categories)_
Admin → Security renders:
> **No security events**, No authentication or permission events recorded yet

At the same moment, Admin → Overview reports `authentication: 39`, `authorization: 3`,
`security: 3` in the last 24h, the Auth Summary shows `Login Success 35 / Login Failure 4 /
Permission Denied 2 / Patient Scope Denied 1`, and Audit Logs lists login events by timestamp.
The Security tab is simply not reading the data that exists.

### 8.2: The user list is full of test debris: **ROUGH (severe)**: NOT FIXED _(2026-08-09: admin_users_routes.py:131 has no service-account filter, and bots are re-created into users at runtime)_
19 users, shown to an investor, including:
- `Dr U-A-5313F0`, `Dr U-B-5313F0`, `Dr U-C-5313F0`, load-test accounts, role Doctor, Active
- **Two `Dr. Vikram Rathore`**, `DOC004` and `dr_rathore`, both Doctor / General Surgery
- Bot accounts listed as staff with Reset-password and Deactivate actions: `Orb Sage` (system,
  **role Admin**), `Orb Vigil` (bot_vigil, role Doctor), `Orb Lab System` (bot_lab, role Doctor)
- A patient, `Raj Patel` / `P001`, in the staff user-management table

### 8.3: MFA is "Off" for every single user: **ASK / security**: NOT FIXED _(2026-08-09: CONFIG: TOTP is implemented; nobody has enrolled and the box runs profile=dev)_
Every row of the MFA column reads `Off`. The backend agrees, loudly, at boot:
```
CRITICAL orb.bootstrap: SHIPPED DEFAULT CREDENTIALS FOUND: ADM001, DOC001, DOC002, DOC004,
DOC005, DOC006, DOC007, NUR001, PHA001, anesthetist_1, anesthetist_2, dr_rathore
(profile=dev, enforcement=off). This appliance must NOT be handed to a hospital in this state.
WARNING orb.bootstrap: No administrator on this appliance has enrolled a TOTP factor.
```
Fine for a demo box; be ready for "walk me through your access controls".

### 8.4: The newest database backup is seven days old: **ASK**: NOT FIXED _(2026-08-09: RUNTIME: a detector exists (ops_health_service.py:456) but there is no backup scheduler)_
Data & Backups lists five backups, all dated **Aug 1, 2026**, 255–257 MB each (1.28 GB total).
Nothing since. On an appliance whose pitch includes backup/restore discipline.

### 8.5: The Impact dashboard was denominated in US dollars: **FIXED 2026-08-09**
> Was: `$25,814/month saved` · `$310K/year` · `$4000/ICU-day` · `$120/clinician-hour` · `$5000/ADE`
> Now: `₹58,500/month saved` · `₹7,02,000/year` · `Rs 15,000/ICU-day` · `Rs 1,500/clinician-hour` · `Rs 40,000/ADE`

The buyer is an Indian CIO and the financial model is in rupees. Fixed on both sides:

* **The values were re-sourced, not FX-converted.** Converting the US figures at ~₹83 would
  have produced ₹3.3 lakh per ICU day and ₹9,960 per clinician-hour, numbers an Indian CIO
  rejects on sight. Every cost assumption is now natively rupee-denominated
  (`impact_service.py:61-110`), and each carries a citation that says plainly it is a
  **SITE PLACEHOLDER**, how it was derived, and that it must be replaced with the hospital's
  own costing. `ward_bed_day_cost_inr` explicitly notes it is deliberately *below* the
  ₹10,000–15,000 ARPOB band, because ARPOB is revenue per occupied bed-day and using revenue
  there would overstate savings, the same unsourced-₹15,000 trap
  `fundraising/ORB_FINANCIAL_MASTER.md:724` already flags in the ROI model.
* **The currency now travels with the money.** `compute_impact()` returns a
  `currency: {code, locale}` block and the dashboard formats from it
  (`ImpactDashboard.tsx`), instead of a hardcoded `en-US`/`USD` pair. `en-IN` also gives
  correct Indian digit grouping, **₹7,02,000, not ₹702,000**. Env-overridable via
  `ORB_CURRENCY_CODE` / `ORB_CURRENCY_LOCALE`.
* **Keys were renamed `*_usd` → `*_inr`** across `impact_service`, `coding_service`,
  `impact_routes`, both panels and the tests, so no key claims a currency it no longer holds.
  The Revenue Integrity panel (`BASE_RATE_INR = ₹50,000`) was converted in the same pass.

Verified on screen at Admin → Impact: rupees present, no dollar amount anywhere on the page.

**Still true, and by design:** three of the four rows read `₹0` after a reseed
(Documentation, Medication safety, Throughput) because they count Scribe notes, pharmacist
verifications and discharge reconciliations, and a fresh database has none. The panel says
`0 recorded events` rather than inventing a number. Running Acts 3–5 before opening this
screen fills them in from work done in front of the investor.

### 8.6: Model Governance is honest to the point of being a liability: **ASK**: PARTIAL _(2026-08-09: the hosted-model row is gone; the honest untrained rows remain by design)_
The table is correct and I would not water it down, but rehearse these rows:

| Model | State as displayed |
|---|---|
| `forecast_lstm` | **untrained** · "SYNTHETIC v1 (demo), NOT trained/validated on real clinical data" · checkpoint **none** · **not validated** · inactive |
| `vigil_cfc` | "**UNTRAINED**, no checkpoint; overlay inactive until trained on real data" |
| `sage_llm` | medgemma-27b · "not fine-tuned on Orb data" · **not validated** · active |
| `vigil_news2` | rule-based RCP NEWS2 · **validated** · active |

The screen reads **1 of 4 validated** on this box as of 2026-09-07, and getting there took a
manual step that is worth knowing about.

`sage_llm_hosted`, a **hosted, off-appliance provider** listed inside a product whose entire
thesis is local-first and air-gapped, was removed from the code on 2026-08-09.
`model_governance_service.seed_default_models()` deletes any registry row whose key is not in
the known set, so a NEW database gets four models. But `list_models()` calls that seeder only
when the registry table is EMPTY (`if n == 0`), so on a database that already has rows the
cleanup never runs. The row survived on this appliance for a month, and it survived a full
`reset_db_and_seed.py` (the registry has no `patient_id` and no FK to one, so the reseed does
not clear it). It cleared only when `seed_default_models()` was called by hand on 2026-09-07.

**Fixed in code on 2026-09-07, so this no longer needs a pre-demo check.** Migration 38
retires unknown rows on any existing database, and `main.py`'s lifespan calls
`seed_default_models()` after `init_db()`, so every boot reconciles. Verified end to end on
this box: with migration 38 already applied and therefore skipped, a reintroduced
`sage_llm_hosted` row was gone after a single restart.

Reconciliation is deliberately NOT on the read path. `list_models()` backs
`GET /admin/models/provenance`, which any authenticated clinician hits inline on an AI
output, and a read that writes would take a SQLite write lock per client; it would also make
repair depend on someone opening a page. `tests/test_model_governance.py` pins that.

The same stale registry also fed the compliance posture export
(`compliance_service._model_posture`), which reported `total = len(models)`. That read
5 / 1 / 4 where the truth was 4 / 1 / 3, and now reads correctly.

The Forecast panel additionally wears a permanent **"● AI untrained | Untrained"** badge
beside its own title.

### 8.7: Pilot Scorecard: 3 of 7 prerequisites not met: **ASK**: NOT FIXED _(2026-08-09: DATA: 0/344 covered ward-shifts is the true state of the evidence instrument)_
- ✗ **Clinical outcome capture running**, 0 rows. *"Without this, detection lead time does not exist."*
- ✗ **False-alert adjudication running**, 0 rows in window.
- ✗ **Ward-shift coverage**, **0 of 344 ward-shifts covered (2,688 uncovered hours)**.
- 84,649 of 113,376 shadow events (**75%**) are simulator-lane synthetic.

The scorecard is our pilot evidence instrument and it currently reports that it has no
clinical evidence. That is the *correct* behaviour and it is still a hard slide.

### 8.8: Ingestion and Alerts tabs are empty: **ROUGH**: NOT FIXED _(2026-08-09: CONFIG: the box runs ORB_INGESTION_MODE=simulation, so no ingestion events exist)_
`0 events` / "No ingestion events recorded yet" and "No ingestion-driven alerts have been
generated yet". Both downstream of §2.1 and of `ORB_INGESTION_MODE=simulation`.

### 8.9: Raw enum names in the audit table: **ROUGH**: FIXED _(2026-08-09: AuditLogTable.tsx:164 humaniseEnum())_
Category column renders `Phi_access`, `Ai_inference`, `Api_mutation`, `Authentication`, 
snake_case with a capitalised first letter, straight from the enum.

### 8.10: Trust Center displays "dev profile": **ROUGH**: NOT FIXED _(2026-08-09: CONFIG: TrustCenter correctly reports the appliance's real profile)_
`Generated 8/8/2026 · **dev profile** · on-prem`. Trust score 85, 9 passing / 4 warnings /
0 failing, 1/5 models validated, hash chain verified over 63,143 rows. Good surface, 
run the demo on a non-dev profile.

### 8.11: ROSTER IMPORT HAS NO USER INTERFACE: **BLOCKER (missing feature surface)**: FIXED _(2026-08-09: NEW: src/pages/panels/admin/RosterImport.tsx + src/api/rosterImport.ts, wired at AdminPanel.tsx:16,73,122: VERIFIED end to end in the browser (preview → dry run → commit, run RI-5B4FA85CA3))_
Demo step 8 lists roster import. `server/routes/roster_import_routes.py` and the tolerant-CSV
importer service exist and are tested; `grep -rni "roster" src/` returns only ward-round
roster code. There is no import screen, no upload, no dry-run preview, no mapping UI.

### 8.12: Flight Recorder and audit chain are excellent: **STRENGTH**: STRENGTH _(2026-08-09: not a defect; not re-checked, no reason to think it regressed)_
"Chain verified · 63,144", 500-row replay with step controls, SHA-256 hash chain over every
audit row, per-category counts. This is a CIO-grade surface. Show it.

---

# 9. PHARMACY (HELIX)

### 9.1: Every pharmacy number is zero: **BLOCKER (credibility)**: NOT FIXED _(2026-08-09: DATA: reset_db_and_seed.py never seeds helix_patient_med_orders; populate_helix_from_medgemma.py is not wired in)_
Pharmacy Operations shows `Pending Review 0`, `Verified 0`, `Scheduled 0`, `In Transit 0`,
`Delivered 0`, and "Pharmacist Work Queue, 0 active, Queue clear, no pending orders."
Verify Queue: "Queue clear, No AI/agentic medications are awaiting verification."

Meanwhile the nurse board reports **6 doses overdue** and patients have live medication
records. The medication data and the pharmacy pipeline are not connected in the demo dataset,
so the pharmacist role has literally nothing to show. PHA001 has three sidebar items and all
three are empty.

---

# 10. SURGICAL SUITE (DOC004)

### 10.1: Schedule says 0 surgeries every day; Active Cases says 5: **BLOCKER**: NOT FIXED _(2026-08-09: DATA: /schedule is date-windowed and /cases is not; seeded surgeries are all in the past)_
The Schedule tab shows `Friday 7 Aug, 0 Surgeries`, `Saturday 8 Aug, 0`, `Sunday 9 Aug, 0`,
`Mon…, 0`, while the tab beside it carries a red **5** badge and lists five **SCHEDULED**
cases. Command Center independently reports "Operating Room · Today, No cases scheduled today."
Three surfaces, three different answers.

### 10.2: Surgical cases contradict the same patients' medical charts: **BLOCKER (credibility)**: NOT FIXED _(2026-08-09: DATA: reset_db_and_seed.py:215 staples procedures onto whichever patients come first)_
The five "active surgical cases" are the same five General Medicine patients, with unrelated
surgical diagnoses:

| Patient | Vigil chart says | Surgical Suite says |
|---|---|---|
| Ananya Kapoor 34F | Diabetic ketoacidosis | Acute cholecystitis → Laparoscopic Cholecystectomy, OR-1 |
| Rajesh Iyer 67M | COPD exacerbation | Perforated appendicitis → Open Appendectomy, OR-2 |
| Priya Nair 28F | Dengue with thrombocytopenia | Femoral shaft fracture → ORIF Femur, OR-3 |
| Suresh Reddy 55M | CAP with early sepsis | Small bowel obstruction → Exploratory Laparotomy |
| **Lakshmi Devi 80F** | **UTI with delirium** | **STEMI → Coronary Angioplasty (PCI), Cath Lab** |

Anyone who opens two tabs sees the same named patient with two different diagnoses.

### 10.3: The week strip is clipped with no scroll affordance: **ROUGH**: FIXED _(2026-08-10: SurgicalSuitePanel.css:84-104 paints a persistent thin themed scrollbar on the strip: the scrollbar IS the affordance, no longer display:none'd)_
The fourth day column ("Mon…", "0") is cut off at the viewport edge. The container scrolls
horizontally but shows no scrollbar, gradient, or arrow.

### 10.4: Empty-day cards are 480px of nothing: **ROUGH**: FIXED _(2026-08-10: SurgicalSuitePanel.css:149 `.suite__day-card--empty` drops the min-height to 0; applied whenever day.surgeries.length === 0 (SurgicalSuitePanel.tsx:672))_

---

# 11. SCRIBE

### 11.1: Structuring a note takes 12.7 s to first content, 21.1 s to settle: **SLOW**: NOT FIXED _(2026-08-09: BY DESIGN: .env:30 routes Scribe drafting to medgemma:27b; no code defect found, the path is fully async)_

### 11.2: Every structured note opens with two placeholder lines: **ROUGH (severe)**: FIXED _(2026-08-09: scribe_service.py:653 prune_placeholder_sections(), guarded by tests)_
```
DIALOGUE:
Doctor: [Not explicitly stated in dictation]
Patient: [Not explicitly stated in dictation]

SUBJECTIVE:
67-year-old gentleman presenting on day three of a COPD exacerbation...
```
Model scaffolding at the top of a clinical note the doctor is asked to **Sign & Finalize**.

### 11.3: Broken debug telemetry shown to the clinician: **ROUGH**: FIXED _(2026-08-09: ScribePanel.tsx:792 builds the pipeline string only from stages that ran)_
`Pipeline: ASR -s · Draft 20.084s`, "ASR **-s**" is a missing number (ASR never ran).

### 11.4: Two unlabelled mic buttons, plus the global mic FAB: **ROUGH**: PARTIAL _(2026-08-09: titles added; still three mic controls and no visible orange-vs-green explanation)_
Both composer buttons have empty accessible names (`aria-label` absent, no text). One is
orange, one is green, neither is explained. Three microphone controls on one screen.

### 11.5: The SOAP content itself is genuinely good: **STRENGTH**: STRENGTH _(2026-08-09: SOAP content and actions intact)_
Correct S/O/A/P split, expanded abbreviations (ABG, NIV), preserved the "day three of five"
steroid course, kept the 08:00 gas. Patient-link dropdown, note-type selector (SOAP /
Discharge Summary / I-PASS Handoff), and `Sign & Finalize` / `Reject` / `Admit to Vigil` actions.

---

# 12. VOICE: THE WHOLE MODALITY IS OFF

**BLOCKER for demo steps 5 and 7, NOT FIXED**

`.env` line 32–33: *"Local Whisper ASR is not installed"*, `ORB_WHISPER_DAEMON_ENABLED=false`.
There is no Whisper model in `server/models/`.

- The global mic FAB, the largest, most saturated control on **every** screen, is disabled:
  `aria-label="Voice input disabled by privacy posture (cloud speech egress not approved)"`.
  Clicking explains that browser speech recognition would send audio to a cloud service and
  points at `ORB_CLOUD_SPEECH_ALLOWED`.
- Ward Round "Dictate the plan", **silently does nothing**.
- Scribe's two mic buttons, unlabelled, and the pipeline reports `ASR -s`.
- Sage voice mode, same gate.

Product narrative that cannot currently be shown: ambient dictation, voice-driven Sage,
hands-free ward round. Either install local Whisper before tomorrow or cut voice from the
script and disable/relabel the buttons so nothing looks broken.

---

# 13. FORECAST / COMMAND / SIMULATOR / REVENUE / APPOINTMENTS

### 13.1: Ward Acuity Heatmap read "W1 30/8": **FIXED 2026-08-09**
Was: `ICU 0/8`, **`W1 30/8`**, `W2 0/8`, `W3 0/8`, thirty patients in an eight-bed ward with
the occupancy bar overflowing its track, while three wards sat empty, against a `CENSUS 30/32`
header. Vigil's ward labels (GM-1, CCU, SW-1, OW-1, RCU, NW-1) matched ICU/W1/W2/W3 at all.

Now: **`CCU 5/12 · GM-1 5/12 · NW-1 5/12 · OW-1 5/12 · RCU 5/12 · SW-1 5/12`**, header
`CENSUS 30/72 · 41.7%`. Verified on screen; no `W1` anywhere on the page.

Two distinct defects, both in `forecast_service.py`:

* **`_normalize_ward` funnelled every unrecognised ward into `W1`** via its final `return`. It
  recognised only ICU/W1/W2/W3, so all six real wards collapsed into one. A known ward now
  passes through unchanged; the digit heuristic survives only as a fallback.
* **Capacity was a hardcoded 8 beds × 4 fictional wards.** Ward *names* are now discovered at
  startup (`refresh_ward_capacity()`, called from `main.py`'s lifespan after `init_db()`) from
  site config if declared, else from the wards patients are actually admitted to. Bed *count*
  stays a site setting, `ORB_WARD_BEDS`, default 12, because how many beds a ward has is a
  fact about the building that Orb cannot infer: occupancy would only ever say how many are
  full, which would peg every ward at 100%. Startup logs
  `Ward capacity: 6 ward(s) x 12 beds = 72 (CCU, GM-1, NW-1, OW-1, RCU, SW-1)`.

**Set `ORB_WARD_BEDS` to this hospital's real bed count before a pilot.** 12 is a default, not
a measurement.

**A second bug surfaced while verifying this one, and is also fixed.** With the wards corrected,
the Command Center header read `30/48` while the capacity-forecast tile three rows below it read
`34/72`, from the same snapshot. `command_center_service` did
`from forecast_service import TOTAL_CAPACITY`, and because that is an **int**, the import bound a
copy taken before discovery ran; the dict beside it was shared by reference and updated fine.
It now sums the live dict (`_total_capacity()`).

The existing tests could not express this failure: they asserted the API equalled the imported
`TOTAL_CAPACITY`, and both were the same stale value, so they agreed while the screen did not.
`test_capacity_is_internally_consistent_after_ward_discovery` asserts the *invariant* instead, 
header total == sum of the per-ward capacities it is built from, and was confirmed to fail
against the old code before being kept.

### 13.2: "SEPSIS BUNDLE COMPLIANCE 0% · 0/31 bundles on track": **BLOCKER (credibility)**: NOT FIXED _(2026-08-09: DATA: nothing seeds care-bundle completion times, so compliance is 0% by construction)_
A zero-percent headline KPI on the operations dashboard, on a product sold partly on sepsis
bundle compliance.

### 13.3: Forecast wears an "Untrained" badge next to its title: **ASK**: ASK _(2026-08-09: the checkpoint genuinely does not exist; the badge is honest)_
`Orb Forecast Engine ● AI untrained | Untrained`, under a subtitle that says
"Powered by a multi-task LSTM neural network". Backend confirms:
`WARNING orb.forecast: No checkpoint at server/models/forecast_checkpoint.pt, using UNTRAINED model.`

### 13.4: Forecast x-axis loses the day number: **ROUGH**: FIXED _(2026-08-09: ForecastPanel.tsx:389 no longer slices the day number off the axis label)_
Ticks read `Sat Aug`, `Sun Aug`, `Mon Aug`… The date is truncated out of every label.

### 13.5: Forecast contradicts Command Center: **ROUGH**: PARTIAL _(2026-08-10: ForecastPanel.tsx:171-187 synthesizes a day-0 census anchor client-side, and steps aside if the server ever emits a real day 0; server-side predict_hospital_flow (forecast_service.py:519) still starts at now + 1 day)_
Forecast header: `Current Census 30/32 · 94% occupied`. Its own chart's first point (today,
Sat Aug 08) is **23**. The table below is clipped by the viewport mid-row (`75…`).

### 13.6: "TODAY" is on the wrong day in Appointments: **BLOCKER (conditional)**: FIXED _(2026-08-09: AppointmentsPanel.tsx:96 uses localDateKey(): the IST off-by-one is gone)_
The board marked **Fri, Aug 07** as TODAY while the app header said **Sat, Aug 8**, and
"Today's Appointments" counted Friday's 3 instead of Saturday's 5.

`src/pages/panels/AppointmentsPanel.tsx:94`:
```ts
const today = new Date().toISOString().split('T')[0]   // UTC date, not local
```
IST is UTC+5:30, so between **00:00 and 05:30 local** this returns yesterday. Our audit ran at
01:15–01:50 and hit it. **A daytime demo will not hit this**, but an early-morning rehearsal
will, the KPI is wrong when it fires, and it is a one-line fix.

### 13.7: The TODAY badge itself is truncated to "TOD…": **ROUGH**: FIXED _(2026-08-10: AppointmentsPanel.tsx:159-162: the badge now sits OUTSIDE the ellipsized label span, flexShrink:0, so it can never be the clipped tail)_
Whenever it does fire, the header clips it.

### 13.8: Every patient name on the Appointments board is truncated: **ROUGH (severe)**: FIXED _(2026-08-10: AppointmentsPanel.tsx:137 day columns are minmax(240px,1fr) and the board scrolls horizontally; a 7-day week overflows a 1512px viewport slightly by design: names win over fit)_
`Vinod …`, `Kavith…`, `Siddh…`, `Rame…`, `Sunita…`, `Anan…`, `Laksh…`, `Praka…`, `Ganes…`,
`Nandi…`, `Vikra…`, `Kamal…`, `Indira …`, **not one name is readable**, and the reason lines
are clipped too (`Imaging Rev…`, `Post-op wou…`, `Titrate medic…`). Both themes. Card heights
are ragged and Sunday Aug 09 is missing from the strip entirely.

### 13.9: Revenue Integrity repeats the Orders ID problem: **BLOCKER**: FIXED _(2026-08-09: RevenueIntegrityPanel.tsx:104 now uses PatientPicker)_
Same `e.g. PAT001` free-text patient ID, same 404 on the example, same empty screen.

### 13.10: Recharts renders at 0×0 in hidden panels: **ROUGH (console noise)**: FIXED _(2026-08-09: both ResponsiveContainer call sites wrapped in SizedChart)_
Every tab switch logs eight copies of
`The width(0) and height(0) of chart should be greater than 0…`
because all panels stay mounted under `display:none`. Harmless today; it is what a technical
diligence engineer sees first if they open the console.

### 13.11: What-If Simulator is in good shape: **STRENGTH**: STRENGTH _(2026-08-09: still the strongest screen in this section; three cosmetic blemishes remain)_
Presets, horizon selector, sliders, a clean occupancy-vs-capacity chart, and a
"Model assumptions & citations" disclosure. Blemishes: emoji preset chips (🌙 🛏 🤒 🚑 ⚡ 📈),
a raw `▶` disclosure marker, and uneven x-axis stepping (2h then 3h).

---

# 14. PATIENT BRIDGE (P001)

### 14.1: THE PORTAL SHOWS TWO DIFFERENT PATIENT IDENTITIES ON ONE SCREEN: **BLOCKER**: FIXED _(2026-08-09: PatientShell.tsx:307-317 renders the linked clinical record's name in both places)_
Header, top right: **"Raj Patel"** (the account).
Page headline: **"Welcome, Geeta"**.
Care team card: *"Care unit: General Surgery · Ward SW-1 · Bed SW-104"* under
*"Dr. Vikram Rathore, Attending Physician · General Surgery"*.

`server/scripts/reset_db_and_seed.py:233` links the `P001` login to whichever admitted patient
it finds, here **Geeta Sharma**, a General Surgery patient, while the user account is named
Raj Patel. The chrome greets one person, the record belongs to another.

On a patient-facing portal this reads as *"the patient is looking at someone else's chart."*
If the investor opens the Bridge at all, this is the first thing on the page.

### 14.2: Bridge layout is broken: **ROUGH (severe)**: PARTIAL _(2026-08-09: cards restyled; .bridge__content still max-width:800px in a 1512px viewport)_
- Three top cards forced to equal height: "Your Status" holds three lines of text above ~550px
  of empty card; "Your Care Team" the same.
- The vitals column is so narrow that **"In normal range" wraps to two lines on all five
  vitals**, and "Dr. Vikram Rathore" wraps mid-name.
- The "Schedule Follow-Up" mini-calendar is **clipped**: Saturday shows as `S` / `8` cut off
  at the card edge.
- Content sits in a ~750px column inside a 1512px viewport with the right half empty, and
  "My Records" is a different width again.

### 14.3: Vitals shown to the patient have no timestamp: **ROUGH**: FIXED _(2026-08-10: end-to-end on the trends surface: /bridge/vitals-trends carries per-vital measured_at (bridge_routes.py:338-353; test_bridge_vitals_timestamp.py) and VitalsTrendsCard renders 'Today 14:32' / 'Aug 9, 14:32', never fabricating a time. The /bridge/status simple_vitals dict still carries no timestamp, but nothing renders it: PatientShell shows only the trends card)_
`73.9 bpm, In normal range` with one decimal place and no "as of" date, from readings that
are seven days old.

---

# 15. OTHER ROUTES

### 15.1: `/downtime/:patientId` and `/handoff/:patientId` say "Patient not found" on a scope refusal: **ROUGH**: FIXED _(2026-08-10: both routes now return byte-identical refusal bodies for unknown vs out-of-ward ids (ipass_routes.py:35-40, downtime_routes.py:40-43: a refusal never confirms the record exists), and DowntimeMode.tsx:42 refusalMessage() rewords it Sage-style on screen; the handoff view still prints the uniform 'Patient not found' body verbatim)_
As DOC004 (General Surgery) against a General Medicine patient, both routes 404 and render
`Patient not found` / `Could not draft handoff: Patient not found`. Sage handles the identical
situation with a careful, correct message ("the record exists, your access to it does not").
These two routes tell the clinician the patient does not exist, which is both wrong and
inconsistent with our own stated design.

As the owning doctor (DOC001) both routes render correctly, the downtime snapshot and a full
I-PASS handoff with contingency plans, medications, open bundles and a plain-text export.
**Both are solid surfaces once you are in scope.**

### 15.2: Lens loads cleanly: **OK**: OK _(2026-08-09: unchanged)_
Clean drag-and-drop empty state. Not exercised with a real image this wave.

---

# 16. OPERATIONAL HAZARDS FOR DEMO DAY

### 16.1: Starting the backend the wrong way silently kills all realtime: **CRITICAL OPERATIONAL**: FIXED _(2026-08-09: main.py import-time _assert_correct_asgi_entrypoint() + /socket.io/ tripwire; BOTH TESTED: main:app refuses to boot, main:socket_app handshakes 200)_
The Socket.IO app is `main:socket_app` (`main.py:1134`). If the backend is started as
`uvicorn main:app`, as it was when I found it running, then **every** `/socket.io/` request
404s, on every screen, and live vitals push, Relay realtime, and alert delivery all silently
fall back to nothing. There is no visible error; the app just stops being live.

**Start it with `./run-orb.sh --dev`, or `uvicorn main:socket_app`. Never `main:app`.**

### 16.2: `ORB_DISABLE_SIMULATOR=1` was set on the running process: FIXED _(2026-08-09: the flag does not exist anywhere in the code: not in .env, not in .env.example, and no Python reads it; it was a one-off shell export. The real switch is ORB_INGESTION_MODE and .env:39 = simulation, so bots run. WARNING: .env.example:101 ships 'ingestion', which would freeze vitals on a fresh box with no error anywhere)_
It was set (presumably to silence the crash loop in §2.1). Leave it **unset** for the demo, 
but §2.1 must be fixed first, or unsetting it only restores the error spam.

### 16.3: Environment left running for Wave B: N/A _(2026-08-09: records a process state from the recon wave; nothing in code to verify. That process is dead; the stack now runs from ./run-orb.sh --dev)_
Backend: `.venv/bin/python -m uvicorn main:socket_app --host 127.0.0.1 --port 8000`, with
`ORB_DISABLE_SIMULATOR` **unset**, `/api/health` 200 and `/socket.io/` 200. Frontend: Vite on
5173. The simulator is therefore *running and crashing*, 583 `Simulation bot error` lines in
the first 50 minutes, one every 5 seconds, which is the live reproduction of §2.1.

### 16.4: Demo data has not been reseeded since 2026-08-01: PARTIAL _(2026-08-09: the ROOT CAUSE is fixed: reset_db_and_seed.py anchored admissions to an absolute 21-23 May 2026 window, so even a fresh reseed reproduced an 80-day-old ward; it is now a rolling 3-day window. The demo DB itself still needs an actual reseed to benefit. This one item drives §2.3, §5.2, §5.3, §7.4, §10.1 and §13.2)_
Every "N hours late", "day 79", "55 doses not charted", the 5,428 unread badges, the duplicated
Relay thread, the U-A/U-B/U-C load-test users and the week-old backups all trace to this.
A reseed immediately before the demo removes a large fraction of this document.

---

# 17. WHAT IS GENUINELY GOOD: LEAD WITH THESE

1. **Cross-department refusal in Sage**, 0.41 s, perfectly worded, audit-logged. (§3.8)
2. **Nurse observation entry with live NEWS2**, per-parameter scoring, escalation text, and
   it saves and updates the board. Best surface in the product. (§5.4)
3. **Relay wrong-room detection**, catches a cross-patient message, does not block it, offers
   to move it. (§4.5)
4. **Relay ambient copilot catching a lab retraction**, spotted the haemolysed-sample
   retraction and told the ward the hyperkalaemia treatment was no longer needed. (§4.6)
5. **Flight Recorder + tamper-evident audit chain**, 63,144 sealed rows, chain verified. (§8.12)
6. **Trust Center**, live compliance posture, 85, honest ambers, not a PDF. (§8.10)
7. **I-PASS handover draft** and the `/handoff/:id` page. (§5.5, §15.1)
8. **Scribe SOAP quality**. (§11.5)
9. **Model Governance honesty**, if we can hold our nerve, this is a *differentiator* in front
   of a CIO. (§8.6)
10. **What-If Simulator**. (§13.11)

---

# 18. RECOMMENDED FIX ORDER FOR WAVE B

**Tier 1, the demo breaks without these**
1. §2.1 vitals simulator crash (`bot_service.py:145`), unblocks six other symptoms
2. §2.2 the FEMALE bug (`VigilPanel.tsx:1146`), one line
3. §5.1 AdministerModal z-index, one line
4. §4.1 Relay duplicate-render race (`RelayPanel.tsx` ~118 / ~305)
5. §3.1 Sage history bleed across New Chat (`sage_routes.py:1420`)
6. §3.2 raw XML in fast-mode answers
7. §7.1 Ward Round dictation panel overlapping the summary
8. §14.1 Bridge identity mismatch
9. **Reseed the demo database** (§16.4), kills §2.3, §4.2, §4.3, §4.4, §5.2, §5.3, §7.4, §8.2, §8.4

**Tier 2, visible, fixable in an evening**
10. §13.1 ward capacity model (`W1 30/8`)
11. §10.1/§10.2 Surgical Suite data + schedule/active disagreement
12. §9.1 pharmacy pipeline has no data
13. §8.1 Security tab empty
14. §6.2 Orders silent 404 + wrong example ID
15. §13.9 same in Revenue
16. §13.8 Appointments name truncation
17. §12 voice: install Whisper, or relabel/disable every mic control so nothing looks broken

**Tier 3, build the missing surfaces (do NOT hide the features)**
18. §6.1 Order Sets UI, backend is complete and safety-enforced
19. §8.11 Roster Import UI, backend is complete and tested
20. §1.1 device enrolment + set-my-PIN UI

**Tier 4, polish**
21. §2.5 patient-chart action bar; §6.4 native selects; §11.2/§11.3 Scribe scaffolding and
    `ASR -s`; §3.4 `[LOC][Resp]` tags; §7.3 raw ISO timestamps; §5.5 `insufficient_data`;
    §8.9 `Phi_access`; §2.7 sidebar truncation; §2.8 mic FAB collisions; §2.9 blocked bell;
    emoji-vs-SVG icon consistency throughout.

---

# APPENDIX: EVIDENCE

**Screenshots:** `docs/screenshots/` (223 files, 129 dark, 94 light).
Naming: `<role|surface>-<nn>-<screen>-<theme>[-full].png`.
Light and dark were compared for every role's nav tabs and every Admin sub-tab: **no
theme-specific defect was found**. Every problem in this document reproduces identically in
both themes, which matches the passing contrast gate. The light pass is evidence that the
design system is sound and the failures are behavioural.

Most useful for Wave B:
- `sage-01-general-thinkingON-dark.png`, the patient leak, with "Rajesh Iyer · 3 retrieved"
- `sage-02-general-thinkingOFF-dark.png`, the raw XML dump
- `relay-03-wrongroom-dark.png`, duplicated message + duplicated warning
- `nurse-08-emar-administered-dark.png` / `nurse-09-administer-modal-dark.png`, the invisible modal
- `round-02-saved-dark.png`, the dictation panel covering the summary
- `chart-01-patient-full-dark.png`, FEMALE header + 226-hour sepsis timer
- `doc001-13-appointments-{dark,light}.png`, truncated names + wrong TODAY
- `doc001-10-command-{dark,light}.png`, `W1 30/8`, sepsis 0%
- `bridge-01-home-dark.png`, "Raj Patel" vs "Welcome, Geeta"
- `surgical-02-activecases-dark.png`, surgical diagnoses vs medical charts
- `admin-11-users-dark.png`, test users, duplicate doctor, bots as staff
- `nurse-04-obs-filled-dark.png`, the live NEWS2 surface (the good one)

**Console / network summary:** with the backend started correctly, the twelve doctor panels
produced **zero** console errors and **zero** failed requests. All errors recorded in this
document are behavioural or role-specific (PHA001 403s, Orders 404, React duplicate-key),
not a broken build. The only benign noise is Recharts' 0×0 warning from hidden panels and a
StrictMode `ERR_ABORTED` on `/api/auth/device` and `/api/vigil/patients/{id}/analysis`.

**Gates at time of audit:** `tsc --noEmit` exit 0 · `check-contrast.mjs` exit 0
(378 token/surface pairs across 3 themes; 1780 component call sites).
