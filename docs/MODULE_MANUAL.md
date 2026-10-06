# Orb Hospital OS: Module Manual

**Architecture and clinical claims, module by module.**

*Edition: September 2026 · 29 clinical and foundation modules · Grounded in the codebase as it exists today. Counts re-measured against the repository on 2026-09-07.*

---

## How to read this manual

Every module in this manual's scope gets one entry, and every entry answers the same three questions:

1. **What it is**, the module in plain language.
2. **The claim**, the single sentence we are prepared to say to a doctor about how this makes their day easier, followed by the concrete mechanics behind it. If a mechanic isn't in the code, it isn't in this manual.
3. **The architecture**, what it is built from: services, models, data flow, and the guardrails that exist in code rather than in intention.

Each entry ends with an honest **Status** line. Orb is pre-pilot: no module has touched a real patient, vitals currently come from a simulator behind the `VitalsSource` interface, and the two neural networks (Vigil's CfC overlay, Forecast's LSTM) ship untrained and fail closed to deterministic rules. Where that matters, the status line says so. The pilot plan is shadow-mode first, and the instrumentation to prove or disprove these claims (Module 29) is itself part of the product.

**What this manual does not cover.** The 29 entries are the clinical modules and the foundation
they sit on. Orb also ships a hospital-administration layer and a set of utility surfaces, 17
registered route files in all, and they are deliberately not given entries here because this
manual exists to state *clinical* claims and none of them make one:

- **The administration desks**, `billing`, `payments`, `payer`/TPA, `procurement`, `frontdesk`
  (OPD and transfers), `housekeeping`, `workforce` (roster, attendance, payroll boundary),
  `dietary` and `biomedical`. Documented in `docs/ORB_TECHNICAL_MANUAL.md` §16, which is also
  where the switch semantics and the permission split for each desk live.
- **The compliance rails**, `abdm` (ABHA) and `nabh`. Also §16, and `docs/guide/35-dpdp-consent-and-data-rights.md` for the DPDP set.
- **Administration and platform utilities**, `admin`, `admin_users`, `compliance`,
  `clinical_records`, `roster_import` and `search`. Documented in `docs/guide/`, principally
  `25-admin-panel.md`, `31-api-reference.md` and `00-screen-to-code.md`.

Their absence here is a scoping decision, not an omission, and it is not a statement that they
are unbuilt: every one of them is registered in `server/main.py` and reachable today under its
own permission family.

A recurring design position runs through every entry, and it is worth stating once: **wherever a decision matters clinically, the deciding logic is deterministic and auditable; the AI drafts, summarizes, and explains, and a human confirms.** NEWS2 decides who is paged, not a neural network. A regex and a formulary decide what counts as an order, not the model's mood. Every claim below is a variation on that theme.

## The system at a glance

Orb is a single-appliance hospital operating system: one Apple Silicon box in the hospital's server room runs the entire stack, and no patient data ever leaves the building. It is built to **replace** the EHR for its wards, not to sit beside one.

| Layer | What we use |
|---|---|
| Frontend | React 19 + TypeScript + Vite; one clinician shell with role-scoped tabs; PWA offline support |
| API | Python FastAPI, async throughout; heavy work pushed off the event loop |
| Data | SQLite in WAL mode, with `sqlite-vec` (vector search), FTS5 (lexical search), and SQLCipher encryption at rest, all coexisting in one file |
| Realtime | Socket.IO (department-scoped rooms) for telemetry and messaging; SSE for token streams |
| LLMs | MedGemma 27B (careful tier) and 4B (fast/ambient tier) served by Ollama, selected per turn by a thinking-mode router with a safety floor |
| Speech | Whisper large-v3 on Apple MLX, streaming |
| Retrieval | BGE-M3 embeddings + BGE cross-encoder reranker, isolated in a worker process |
| Neural nets | PyTorch on MPS: CfC risk overlay (Vigil), multi-task BiLSTM (Forecast), both untrained today, both fail closed |

The manual is organized in seven parts:

- **Part I, Watching patients** (1–3): Vigil, Forecast, Command Center
- **Part II, Thinking with the doctor** (4–6): Sage, Sage Ambient Intelligence, Lens
- **Part III, Documenting and communicating** (7–11): Scribe, Rounds, Relay, I-PASS Handoff, Care Bundles
- **Part IV, Medication safety** (12–15): Orders & eMAR, Medication Safety Gate, Pharmacy Verification, Helix
- **Part V, The patient journey** (16–22): Admission & Discharge, Appointments, Surgical Suite, Nurse Mode, Bridge, Discharge Access, Pulse
- **Part VI, Running the hospital** (23–25): Revenue Integrity, Impact & ROI, EHR Downtime Mode
- **Part VII, The foundation** (26–29): Platform & Runtime, Security & Audit, Governance & Interop, Shadow Scorecard

## Table of Contents

- [How to read this manual](#how-to-read-this-manual)
- [The system at a glance](#the-system-at-a-glance)
- [1. Vigil, deterioration early warning](#1-vigil-deterioration-early-warning)
- [2. Forecast, census and discharge prediction](#2-forecast-census-and-discharge-prediction)
- [3. Command Center, one screen for whoever runs the house](#3-command-center-one-screen-for-whoever-runs-the-house)
- [4. Sage, the clinical AI assistant](#4-sage-the-clinical-ai-assistant)
- [5. Sage Ambient Intelligence, the half that comes to you](#5-sage-ambient-intelligence-the-half-that-comes-to-you)
- [6. Lens, medical image drafts](#6-lens-medical-image-drafts)
- [7. Scribe, voice-to-text documentation](#7-scribe-voice-to-text-documentation)
- [8. Rounds, the ward round as a driven queue](#8-rounds-the-ward-round-as-a-driven-queue)
- [9. Relay, messaging where machines page humans](#9-relay-messaging-where-machines-page-humans)
- [10. I-PASS Handoff, the shift handoff that drafts itself](#10-i-pass-handoff-the-shift-handoff-that-drafts-itself)
- [11. Care Bundles, the Sepsis Six stopwatch](#11-care-bundles-the-sepsis-six-stopwatch)
- [12. Orders & eMAR, ordering and the bedside record](#12-orders-emar-ordering-and-the-bedside-record)
- [13. Medication Safety Gate, one choke point for every drug](#13-medication-safety-gate-one-choke-point-for-every-drug)
- [14. Pharmacy Verification Queue, the human gate on AI medications](#14-pharmacy-verification-queue-the-human-gate-on-ai-medications)
- [15. Helix, pharmacy and inventory](#15-helix-pharmacy-and-inventory)
- [16. Admission & Discharge, the intake-to-exit spine](#16-admission-discharge-the-intake-to-exit-spine)
- [17. Appointments, follow-up booking](#17-appointments-follow-up-booking)
- [18. Surgical Suite, the operating-theatre command surface](#18-surgical-suite-the-operating-theatre-command-surface)
- [19. Nurse Mode (My Shift), the task-shaped home](#19-nurse-mode-my-shift-the-task-shaped-home)
- [20. Bridge, the patient's own window](#20-bridge-the-patients-own-window)
- [21. Discharge Access, the link the patient takes home](#21-discharge-access-the-link-the-patient-takes-home)
- [22. Pulse, season and conditions](#22-pulse-season-and-conditions)
- [23. Revenue Integrity, coding and CDI](#23-revenue-integrity-coding-and-cdi)
- [24. Impact & ROI Dashboard, the money model](#24-impact-roi-dashboard-the-money-model)
- [25. EHR Downtime Mode, the worst hour of the CIO's year](#25-ehr-downtime-mode-the-worst-hour-of-the-cios-year)
- [26. Platform & Runtime, the box everything sits on](#26-platform-runtime-the-box-everything-sits-on)
- [27. Security & Audit, who you are, whether you may, can we prove it](#27-security-audit-who-you-are-whether-you-may-can-we-prove-it)
- [28. Governance & Interop, the honesty layer](#28-governance-interop-the-honesty-layer)
- [29. Shadow Mode Scorecard, the pilot's evidence instrument](#29-shadow-mode-scorecard-the-pilots-evidence-instrument)
- [Appendix, the claims we are not yet allowed to make](#appendix-the-claims-we-are-not-yet-allowed-to-make)

---

## 1. Vigil: deterioration early warning

*Part I, Watching patients*

Vigil is the screen a ward keeps open all shift: every admitted patient, continuously scored with the RCP **NEWS2** early-warning system (including the COPD Scale-2 SpO2 variant and the single-parameter-3 rule), sickest floated to the top in real time. Any non-stable score automatically posts an alert into the patient's Relay case room and starts a timed nurse → registrar → consultant escalation ladder.

**The claim.** A deterioration is surfaced and paged within seconds of the numbers crossing a threshold, instead of waiting for the next obs round, an unread chart, or whoever happens to glance at a monitor.

How, concretely:

- Every vitals tick is scored with the full 7-parameter NEWS2; alerts carry the vitals snapshot, plain-language reasons, and the raw NEWS2 integer plus band, never a fabricated "probability."
- An unanswered alert cannot die in a channel: escalation chains climb tiers on timers until a bedside clinician acknowledges, and resolving one requires a typed reason.
- Alert fatigue is engineered against, not hoped away: a 120-second per-patient cooldown that a *worsening* patient bypasses (band escalation or NEWS2 rise re-pages within 30 s), mirrored client-side so a backgrounded tab chimes once, not every five seconds. Every suppressed alert is still logged, so the suppression rate is measurable.
- The score explains itself: a per-parameter "why this NEWS2" breakdown, an explicit "this is a MINIMUM" note when obs are incomplete, and a trend that says "insufficient data" rather than inventing a baseline of zero.
- A nurse's typed reading travels the same pipeline as a monitor reading, same score, same alerting, same audit, provenance-stamped `manual_entry` forever.

**Where the win lands.** Errors caught: the window between "the numbers turned bad" and "a human knew" shrinks from the obs-round interval to seconds. Secondarily, cognition offloaded, the ward scan starts from a worst-first, already-scored board.

**Architecture.** A deterministic NEWS2 rule engine (~130 lines from the published RCP table) is the displayed *and deciding* score. A learned CfC (liquid neural network) overlay predicting deterioration/sepsis/mortality at 1–24 h horizons is fully wired but may only *escalate* the NEWS2 band, never lower it. Every reading enters through the `VitalsSource` abstraction, in which the simulator, an HL7 v2 MLLP monitor adapter (Philips/GE vendor profiles, bounded queue, NACK backpressure, clock-skew tracking), and manual entry are peers with mandatory provenance.

- **AI:** none in the deciding path. MedGemma 27B streams a narrative chart assessment (SSE); the 4B tier appends a one-sentence recommendation to alerts, with a circuit breaker and a static fallback sentence.
- **Data flow:** source → sink (dedup, backfill guard) → ingestion → NEWS2 per tick → `vitals:update` to department-scoped Socket.IO rooms → alert bridge into Relay + shadow episodes stamped at threshold-crossing time.
- **Guardrails worth naming:** alert dispatch is gated solely by the NEWS2 band (reason strings are cosmetic and can never suppress an alert); alerts with no case room are recorded as `alert.dropped`, never silently skipped; out-of-scope requests return 404, not 403, so patient existence is not disclosed.

**Status.** Pre-pilot, simulated vitals only. The NEWS2 engine is deterministic per the published table; the CfC overlay is inert (no trained checkpoint exists). The HL7 monitor adapter is bench-testable but its vendor mappings are flagged `verified_against_device=False` until a real monitor message is captured.

**Key code:** `server/services/vigil_service.py`, `server/services/bot_service.py`, `server/services/vitals_source/`, `server/services/escalation_service.py`, `src/pages/panels/VigilPanel.tsx`

## 2. Forecast: census and discharge prediction

*Part I, Watching patients*

Four operational views: a 7-day census projection, a per-ward bed-capacity view with a 48-hour projection, a Discharge Readiness Board ranking every admitted patient by discharge likelihood, and retrospective admission-pattern analysis. Built around a multi-task LSTM for length-of-stay, discharge probability, and acuity trajectory, which **fails closed**: until a validated checkpoint is installed, every number comes from a transparent rule-based heuristic, and the UI says so.

**The claim.** The morning bed-and-discharge huddle starts from a ranked, always-current board instead of a whiteboard reconstruction.

How, concretely:

- The Discharge Board ranks every patient by discharge probability with estimated remaining days and a trajectory arrow, so discharge planning, pharmacy reconciliation, transport, family calls, works the top of a list instead of being rediscovered patient-by-patient on rounds.
- The census projection fits the hospital's own last 30 days of admissions (Poisson mean), adjusted by day-of-week only, "how full will Thursday be" is answered from the site's actual history. No weather or seasonal factor touches the number (ruled 6 October 2026); the season from Pulse sits beside it as one line of context.
- Numbers are deliberately calm: expected values only, no random draws, a 1-hour server cache, the figure quoted in the huddle is the same figure an hour later.
- Every prediction carries provenance (`prediction_source: lstm | heuristic`); the panel's chip reads Untrained/Unvalidated/Validated straight from Model Governance. The doctor always knows whether they're looking at a rule of thumb or a model.

**Where the win lands.** Cognition offloaded at the start of the day shift, the 08:00 bed meeting consumes a pre-built board. The safety angle is honesty: an untrained network is never allowed to masquerade as a prediction.

**Architecture.** A PyTorch multi-task BiLSTM (`LayerNorm(19) → 2-layer BiLSTM → temporal attention → 4 heads`; ~160k params, MPS/CPU) behind a service that extracts 48-step × 19-channel feature windows from SQLite, caches predictions on a ~10-minute bot cycle, and aggregates them into the flow/capacity/discharge views.

- **AI:** the LSTM only, no LLM. The single checkpoint ever produced is synthetic-data, gitignored, and marked "NOT for any clinical use" in its model card.
- **Guardrails worth naming:** untrained gate routes to the heuristic (random-init weights are never surfaced); the ICU-transfer head is omitted entirely unless the checkpoint declares it trained; the discharge board is department-scoped and PHI-read audited (2026-08-05 fix); `torch.load(weights_only=True)`.

**Status.** Pre-pilot; runtime is in heuristic mode. Clinical use requires MIMIC-IV training plus external validation per the training playbook.

**Key code:** `server/services/forecast_service.py`, `server/models/forecast_lstm.py`, `src/pages/panels/ForecastPanel.tsx`, `docs/MODEL_CARD_forecast_lstm.md`

## 3. Command Center: one screen for whoever runs the house

*Part I, Watching patients*

A single-pane operations board for the CMO, bed manager, or duty administrator: house census vs capacity, a ward acuity heatmap, a live list of deteriorating patients with NEWS2 scores, sepsis-bundle compliance, downtime-snapshot readiness, a 24-hour capacity forecast, and today's OR schedule, refreshed every 20 seconds. Beside it, the **What-If Capacity Simulator**: slider-driven surge scenarios projected hour-by-hour, reporting hours-to-overflow, beds short at peak, discharges needed, and the extra-nurse gap.

**The claim.** The person running the house stops phoning wards for numbers, and when a surge threatens, the simulator sizes the response in beds, discharges, and nurses *before* the bed meeting.

How, concretely:

- Active Deteriorations lists every critical/elevated patient in the caller's department, critical first, NEWS2 computed live, the "who needs eyes now" column without walking the wards.
- The acuity heatmap weights a critical patient 3× and elevated 1.5×, exposing the workload mismatch that raw occupancy hides: a 6/8 ward of critical patients glows hot; a 6/8 ward of stable ones doesn't.
- The Downtime Readiness tile shows what fraction of patients have a paper-fallback snapshot fresher than 24 h; its one button snapshots the whole house in a click.
- The simulator turns "flu surge at 1.5× with 8 beds closed" from an argument into arithmetic, deterministic, with every assumption (LoS, nurse ratio, admission fit) disclosed with citations, so the same scenario re-run in the meeting gives the same numbers.
- All surfaces share one source of truth with Forecast (the same capacity dict, imported by reference), so the Command Center header can never contradict the Forecast tab, a real bug class this codebase fixed and documented.

**Where the win lands.** Time saved for the operational layer across the whole shift; safety from the deterioration list, bundle compliance, and one-click outage preparedness.

**Architecture.** A pure-read aggregation service composing views over Vigil risk scores, sepsis bundles, the Forecast flow model, downtime snapshots, and the surgery schedule, every section independently try/except-guarded so one failing subsystem degrades to zeros while the board keeps rendering. The simulator is a deterministic hourly difference-equation engine seeded from the live Forecast baseline. **No neural network anywhere in this module.**

- **Guardrails worth naming:** pure reads by contract (the single write is the downtime Capture-all); deteriorations department-scoped and PHI-read audited; server-side clamps on every simulator knob.

**Status.** Pre-pilot. Board mechanics are real and deterministic; the clinical signal feeding them is simulated or heuristic today.

**Key code:** `server/services/command_center_service.py`, `server/services/capacity_sim_service.py`, `src/components/command/CommandCenter.tsx`, `src/components/capacity/CapacitySimulator.tsx`

## 4. Sage: the clinical AI assistant

*Part II, Thinking with the doctor*

Sage is Orb's chat-style decision-support assistant. A clinician asks in plain language, about one patient, the whole ward, a drug, or a protocol, and Sage assembles an evidence pack (live chart facts, hospital SOPs and formulary, external guidelines), sends it to a local MedGemma model, and streams back a cited, badge-annotated answer. It is deliberately **read-only against the chart**: anything that looks like an order becomes a draft a clinician must explicitly approve.

**The claim.** Sage answers the questions a ward round actually generates, "how is bed 4 doing," "what does our sepsis protocol say," "what dose?", from the hospital's own records and policies, in seconds, without the clinician leaving the chart or the data leaving the building.

How, concretely:

- A patient-scoped question automatically pulls demographics, latest vitals with trends, the deterministic NEWS2 with its per-parameter breakdown, active medications, recent labs, the last signed note, and the latest imaging read, and explicitly lists what is *missing* ("No recent labs"), so an answer formed on partial data says so.
- Hospital policy outranks the textbook: retrieval is trust-tiered (local SOP > local formulary > external guideline), so "empiric antibiotics for sepsis?" answers with the hospital's own antibiogram before the Surviving Sepsis Campaign.
- One toggle a doctor understands: **Thinking ON** = the careful 27B, **OFF** = the fast 4B, with a safety floor that routes patient-specific, dosing, and score questions to the 27B regardless, because the 4B was *measured* wrong on 40% of drug-recall questions. Every answer says which brain answered.
- Drafts, never executes: "add paracetamol 500 mg for Ajay" trips two independent gates (a deterministic order-grammar widened with the hospital's own formulary brand names, plus the model's declared intent marker). The model's prose is discarded; a policy-written message and an "Approve & Send to Helix" card take its place, and approval leads to the pharmacist verification queue, not the chart.
- Citations you can check: model-cited document IDs are validated against what was actually retrieved; fabricated IDs are dropped server-side (in one 70-turn eval, 3 of 4 model citations were invented) and confidence downgrades to "Limited evidence."
- Ward questions without ward disclosure: census answers are hard-scoped to the clinician's department; pure counts come from GROUP BY aggregates with k-anonymity small-cell masking, zero patient rows.
- Every answer is replayable: question, chart-as-it-was, retrieved passages, model/floor decision, and the *raw pre-guardrail model output* are stored append-only (DB triggers forbid UPDATE), so "why did Sage say that?" is answerable months later.

**Where the win lands.** Time (chart, protocol, and census lookup collapse into one box that streams first words in ~2–8 s on the appliance, measured, and the docs are honest that sub-3 s is not reachable for chart-carrying turns) and safety (the guardrail stack exists so the time saved never costs an invented dose or a phantom "order placed"). It lands at ward rounds, handoff, and the junior doctor's "what dose?" moment.

**Architecture.** A FastAPI SSE route orchestrates each turn: deterministic pre-LLM gates → department-scoped retrieval (chart assembly + hybrid RAG) → thinking-mode model selection → streaming generation through an incremental sentence sanitizer → citation validation, action-state override, provenance capture, shadow instrumentation.

- **AI:** MedGemma 27B and 4B via Ollama, chosen per turn by `thinking_mode` + a residency-aware `model_router` (an evicted 27B becomes a *labelled* downgrade, not an 80-second reload). RAG: BGE-M3 embeddings into `sqlite-vec` + FTS5 BM25, reciprocal-rank fusion, BGE cross-encoder rerank, the ~2 GB embedder isolated in a worker process after in-process loads froze the API's GIL.
- **Guardrails worth naming:** forbidden-phrase strip (fail-closed after 32 passes) removes any "order placed" self-confirmation on every lane including voice; Orb's deterministic NEWS2 is *pinned* into the prompt and the model forbidden to recalculate it, closing a measured "NEWS2 7 where Orb computes 13" failure; chart content is fenced as data with injection-hardened delimiters; a no-such-patient rule forbids inventing vitals.

**Status.** Pre-pilot; functional end-to-end on the appliance, clinically unvalidated. The corpus is ~19 seeded documents (several synthetic, flagged as such); real hospital SOPs need ingestion and sign-off before pilot. The 2026-08 evals candidly document the failure modes the guardrails compensate for.

**Key code:** `server/services/sage_service.py`, `server/routes/sage_routes.py`, `server/services/clinical_knowledge.py`, `server/services/thinking_mode.py`, `server/services/sage_provenance_service.py`, `src/pages/panels/SagePanel.tsx`

## 5. Sage Ambient Intelligence: the half that comes to you

*Part II, Thinking with the doctor*

Ask-Sage is pull. Sage Ambient is **push**: always-on observers subscribed to what is already happening, every Relay case-room message, a 15-second vitals/risk sweep, every event on the hospital nervous system, maintaining a live picture per room and per ward. It surfaces as a case-room sidebar (current concern, rolling summary, standing risk cards, suggested actions), a "Sage noticed" insights feed, and a state-of-the-ward brief for whoever comes on shift.

**The claim.** Sage watches every case-room conversation and every patient's numbers so the doctor doesn't have to re-read the room, it keeps the picture current, taps them on the shoulder only when something needs them, and turns one Confirm into the whole downstream clerical chain.

How, concretely:

- A doctor joining a busy case room mid-shift reads a five-line sidebar instead of scrolling fifty messages; it updates live as colleagues type.
- A critical value typed in ward shorthand ("K 6.8," "BP 85/50," "peaked T waves") becomes a standing risk card via *deterministic* guideline-thresholded rules, even when the model is busy, evicted, or wrong. A later retraction ("haemolysed sample, repeat is 4.1") deterministically takes the card down, so a withdrawn emergency never reads as live.
- A directive in chat ("start IV ceftriaxone 2g," "book the OR") becomes a one-tap action card; on Confirm, Sage does the cross-module clerical work, a pharmacy order flagged REVIEW REQUIRED into the pharmacist queue, or an admission transfer + emergency surgery record + targeted team alerts, all audited.
- Every 15 seconds, LLM-free rules sweep every patient and raise addressed insights, risk turned critical, SpO2 sliding across three readings, SIRS pattern with the actual numbers, antibiotics past 72 h, templated from real stored values so they *cannot* hallucinate a patient, auto-resolving when the condition clears.
- Coming on shift, the ward brief answers "who needs eyes first": census, risk mix, a worst-first watchlist, refreshed by the event bus when ward truth changes, scoped to the doctor's department.
- For "catch me up," Sage quotes the last six room messages verbatim and attributed, deliberately raw quotes, so a small model's paraphrase never wears the chart's authority.

**Where the win lands.** The two worst moments of the day: shift change and mid-shift catch-up (cognition offload), plus errors caught in the stream of work and clerical chains collapsed to one Confirm. The safety wins are deliberately deterministic; the AI only drafts and summarizes.

**Architecture.** Event-driven observers around a deterministic core. Three input streams, Relay messages (background task per message), the 15-second sweep, and the `event_bus` nervous system (`vigil.alert.*`, `scribe.note.signed`, `bridge.emergency.flag`), feed bounded per-room memory, a rules-based insights table, a versioned world model, and an in-memory ward brief.

- **AI:** the ambient 4B tier for message classification and the ward narrative, background priority, micro-batched, skipped entirely when evicted (deterministic paths remain). The 27B is used ambiently exactly twice, deliberately: signed-note EHR extraction (it writes to the chart, so accuracy beats latency) and chart-open KV-cache prewarm.
- **Guardrails worth naming:** every action requires clinician Confirm; the order-time medication safety gate is fail-closed (an *unavailable* gate also blocks, proven after a live near-miss injection test); room text is fenced as data; classifier-proposed actions pass a routable-verb whitelist with a confidence cap; autonomously charted vitals are bounded by plausibility ranges and extraction fails closed to writing nothing (a prior version fabricated a metoprolol order on the error path, removed); the sidebar renders an explicit *broken* state rather than a blank one, so "no warnings" can never mean "the warning system is down."

**Status.** Pre-pilot. The deterministic layers are code-complete with regression tests and are the honest core of the claim. The 4B layers are unvalidated and were measured misclassifying emergencies, which is exactly why the deterministic fast-paths exist. The post-Confirm surgical "AI package" is hardcoded demo content.

**Key code:** `server/services/sage_ambient_service.py`, `server/services/sage_proactive_service.py`, `server/services/sage_narrator_service.py`, `server/services/sage_handoff_service.py`, `server/services/nervous_system_triggers.py`, `src/components/relay/SageAmbientSidebar.tsx`

## 6. Lens: medical image drafts

*Part II, Thinking with the doctor*

A clinician drops an image (X-ray, ECG, CT slice, clinical photo) into the panel, optionally types one line of clinical context, and the local MedGemma 27B multimodal model streams back a structured first-pass reading in a fixed **Findings / Impression / Recommendations** skeleton. The text lands in an editable textarea; the clinician corrects it, ticks a responsibility declaration, and files it to the patient's record. Lens contains no purpose-built radiology algorithm and no regulatory clearance, and is engineered so that fact cannot be forgotten.

**The claim.** A structured, editable first draft of an image read in under two minutes, on-site, at 2 a.m., with no radiologist in the building, so the clinician edits a page instead of writing one, while the system makes it structurally impossible to file the AI's words as anyone's diagnosis.

How, concretely:

- Drag-and-drop to draft: the read streams in token by token, typically 30 s to 2 min, in a report skeleton the clinician already knows how to scan and correct.
- One line of context steers the read ("62M, 3 days productive cough, ?consolidation"), fenced as data-only, so pasted record text can never act as instructions to the model.
- The streamed text *is* the working copy, labelled "DRAFT · CLINICIAN REVIEW REQUIRED"; the doctor's edited version, not the machine's, is what reaches the care team.
- One click files everywhere: linking to the Vigil record auto-posts a "Clinician-reviewed AI imaging reference" to the patient's Relay case room and makes the read available to Sage and discharge documents.
- The filing button is hard-disabled until a patient is selected *and* the clinician ticks "I take clinical responsibility", enforced in UI logic, not requested in a tooltip.
- An "INVESTIGATIONAL, NOT FOR DIAGNOSTIC USE" banner is appended *by the application* after every completed stream, stored with the analysis, and travels with the report. It cannot be omitted or reworded by the model.

**Where the win lands.** Time and cognition at night and in wards where a radiologist read may be hours or days away, a second-opinion draft while waiting for (never instead of) the formal read. The safety posture is the inverse claim: Lens is engineered so its speed cannot quietly become diagnosis.

**Architecture.** A thin FastAPI streaming route hands a validated, encrypted-at-rest upload to MedGemma 27B via Ollama with the base64 image inline, streams tokens over SSE, and persists only on natural completion, cancelled streams save nothing.

- **Guardrails worth naming:** production-default-off gate (`ORB_LENS_ENABLED`; without it the endpoint 403s with an explicit "no validated imaging model" message); 25 MB cap, extension allowlist *plus* magic-byte sniffing, server-chosen filenames; department-scoped listings with PHI-read audit; a provenance chip on every draft linking to Model Governance.

**Status.** Pre-pilot, demo-profile only in practice: analysis ships disabled in production by default, precisely because MedGemma has no clinical validation for image interpretation. The not-a-diagnostic-device stance is enforced in code, not just stated in docs.

**Key code:** `server/services/lens_service.py`, `server/routes/lens_routes.py`, `src/pages/panels/LensPanel.tsx`

## 7. Scribe: voice-to-text documentation

*Part III, Documenting and communicating*

The doctor or nurse dictates (or types) an encounter; local Whisper large-v3 transcribes it, a drug-name dictionary and an LLM pass clean up the clinical vocabulary, and MedGemma drafts it into a SOAP note, discharge summary, or I-PASS handoff. A background verifier then compares the drafted note against the original transcript and flags anything the model invented or dropped. The note is held as an editable draft until a doctor signs it, with every lifecycle step audited.

**The claim.** The doctor talks through the encounter once and edits a pre-structured draft instead of composing the note, with an automated hallucination check standing between the AI's draft and the doctor's signature.

How, concretely:

- Whisper is primed with *this patient's actual chart* before it listens: name, diagnosis, active medication names, and attending are pulled into the decoder prompt, so the hardest words in medicine, drug names, come out right on the first pass.
- Misheard drugs are corrected deterministically first: an RxNorm metaphone index rewrites phonetic manglings ("phosphomycin" → "Fosfomycin"), then an LLM pass fixes acoustic confusions ("hyper" vs "hypo"), skipped entirely for short dictations to save 20–30 s.
- One click structures the transcript into SOAP, discharge, or I-PASS, streamed token-by-token so the doctor watches the draft write itself.
- A designed-in second reader: the verifier reports unsupported claims, omissions, and entity mismatches (drug names, doses, vitals) as clickable findings; the sign-gate refuses one-click signing (HTTP 428) of any note that failed, *or never received*, verification.
- Ward-rounds mode streams live: browser-side voice-activity detection chunks speech into utterances, each transcribed on the warm daemon and appended in real time.
- Built for the India market: per-session language picker with English, Hindi, and auto-detect for Hindi–English code-switching, on the same local multilingual model.
- The note has a real clinical lifecycle: nurses can dictate and edit drafts; only doctors sign or reject (reason mandatory); signed notes are immutable, amendable only through versioned revisions. The raw transcript is stored beside every note, so "what was said" vs "what was documented" is always comparable.

**Where the win lands.** Time first, documentation at the speed of speech, the doctor's job shifted from composing to editing (the pipeline even reports its own per-stage timings in the UI). Safety second: the transcript-vs-note verifier plus the fail-closed sign-gate catches fabrications before signature.

**Architecture.** A four-stage local pipeline, ASR → LLM correction → LLM drafting → LLM verification, with the ASR isolated in a persistent warm daemon process (ZMQ REQ/REP, subprocess fallback) so Whisper and the 27B never contend inside the server process.

- **AI:** Whisper large-v3 on Apple MLX; correction/verification on the 4B (dev) or 27B (prod); drafting always the 27B ("drafting always prioritizes quality"). The RxNorm corrector is deterministic. Speech is recognised only by the local engine on the appliance, which is installed with it and never downloads a model at runtime; the browser's cloud speech preview that used to be an optional egress path was removed on 4 October 2026 and cannot be switched on. When the engine is not installed, dictation says so up front and typing still works.
- **Guardrails worth naming:** a verifier that errors, returns empty, or emits unparseable JSON produces an explicit "unverifiable" verdict demanding manual review, never a silent pass; uploaded audio is encrypted at rest and decrypted only to a temp file for the ASR call; transcript and note are fenced as untrusted data in all three LLM stages; out-of-department note requests 404 identically to missing notes.

**Status.** Pre-pilot; implemented end-to-end but not yet operational in its default deployed shape: verification defaults OFF while the sign-gate fail-closes on unverified notes (so the Sign button cannot currently succeed from the UI without configuration), and the ASR runtime is deferred-at-setup, a Week-13 blocker for dictation, though typing + structuring works without it.

**Key code:** `server/services/scribe_service.py`, `server/services/whisper_daemon.py`, `server/services/rxnorm_corrector.py`, `server/ws/scribe_stream.py`, `src/pages/panels/ScribePanel.tsx`

## 8. Rounds: the ward round as a driven queue

*Part III, Documenting and communicating*

Built around one design sentence written at the top of the file: *a doctor at a bed with a tablet in one hand and roughly 90 seconds per patient.* Orb orders the beds the way a person walks the corridor, tracks seen/skipped/not-reached, survives interruptions, and shows a single non-scrolling screen per patient: what changed overnight, what is wrong now, what must be decided today, with allergies permanently on screen. The plan is dictated at the bedside and saved as a Ward Round note.

**The claim.** The doctor stops navigating the chart and starts walking the ward: Orb drives the sequence, pre-reads every chart into one 90-second screen, and takes the plan by voice so nothing has to be typed at the bed.

How, concretely:

- Beds sort in physical walk order (GM-9 before GM-10, not string order), with keyboard shortcuts and big touch targets for one-handed tablet use.
- Skip is genuinely usable: never-reached beds first, skipped beds only when nothing else is left, the patient in radiology doesn't stall the round, and a skip can never quietly become a patient nobody saw.
- One screen answers three questions without tabs: overnight feed since the 20:00 handover, current issues worst-first (server-computed NEWS2 with drivers, overdue obs, overdue meds), and today's decisions (pharmacist-unverified meds, Sage suggestions with provenance, a "no plan documented today" prompt).
- The next patient's story is prefetched while the doctor is still at the current bed, the walk between beds is off the critical path.
- The round ends with the artefact that matters: a hand-on list of skipped/not-reached patients, plus an explicit red list of plans dictated but never saved.

**Where the win lands.** Time and cognition at the highest-frequency moment of the inpatient day. Specific error-catching: absence of observations renders as a warning, never a clean bill; allergies show "UNKNOWN" rather than an empty row; marking a patient seen is blocked while an unsaved dictated plan would otherwise die on the tablet.

**Architecture.** A pure-frontend orchestration layer over endpoints that already exist and are already department-scoped server-side. The queue/resume state machine lives in a React-free, exhaustively tested module. It computes **no clinical score of its own**, a stated rule, so a second, subtly different NEWS2 can never disagree with the nurse's screen.

- **AI:** local Whisper for dictation only; the panel makes no LLM calls.
- **Guardrails worth naming:** offline saves are *refused*, not queued (the offline queue dies at sign-out, and a round ends in a sign-out, the code documents this failure mode); the dictation component is keyed by patient so a held recording can never transcribe into the wrong patient's plan; any other clinician's round state is purged from the shared tablet on mount; dictated text is never mirrored to localStorage.

**Status.** Pre-pilot; well-tested at the unit level, including offline-refusal and cross-patient dictation attack tests. Displayed scores derive from simulated vitals today.

**Key code:** `src/pages/panels/RoundPanel.tsx`, `src/components/round/roundQueue.ts`, `src/components/round/roundSummary.ts`, `src/components/round/RoundDictation.tsx`

## 9. Relay: messaging where machines page humans

*Part III, Documenting and communicating*

Every admitted patient gets a case room tied to their record; departments get channels; clinicians get audited DMs. Relay is also the delivery surface for Orb's automated systems: Vigil alerts, surgical action cards, and the NEWS2 escalation chain post into the same stream as the human conversation, context and decision never separate.

**The claim.** The doctor stops carrying the hospital in their head: everything about a patient, team chat, monitoring alerts, and the record of who answered, is in one room, and a deterioration nobody answers escalates itself up the seniority ladder instead of scrolling away.

How, concretely:

- Case rooms sort by Vigil risk (critical first) with unread counts and 24 h alert dots, start-of-shift triage is "sort by Urgency, work the Action Required list" rather than a handover phone round.
- Vigil pages the room, not a pager: alerts carry reasons, a vitals snapshot, and the raw NEWS2 band; a one-sentence MedGemma recommendation is appended to critical alerts, and if the model is down, the alert ships without it.
- A deterioration cannot die in a scrolled-past line: NEWS2 ≥ 5 opens an escalation chain addressed to a *named human*, with 10/15-minute acknowledgement timeouts. Unacknowledged, it climbs a tier automatically; it closes only with a written reason; a worse reading re-opens an acknowledged chain, ack is not a mute button.
- Confirm/Escalate is one-tap and race-safe: alert cards resolve exactly once hospital-wide (compare-and-swap; the loser sees who won), and every view and acknowledgement is timestamped for the shadow instrument, the pilot's evidence of how long alerts sat unopened.
- The chart can't quietly get the wrong patient's note: a wrong-room detector runs after every send (never delaying it), attaches a durable room-visible warning, and offers redirect. Originals are annotated, never deleted.
- AI suggestions never touch the chart alone: agentic medication dispatch demands the clinician's signing PIN (rate-limited, 15-min lockout), an explicit dose and frequency (a TBD dose is never dispatched), RxNorm and stock reconciliation, the allergy/interaction gate, and a cryptographic delegation hash sealing conversation → suggestion → signer onto the audit chain.
- Every automated post carries an AUTO badge; offline sends are refused up front with the text preserved rather than silently queued into oblivion on a shared ward tablet.

**Where the win lands.** Time: no pager round-trips, no "which nurse has the phone." Safety: the escalation ladder converts the classic failed-escalation mode, a line scrolled off a busy channel, into a timed process that only gets louder; wrong-chart documentation is caught at the moment of sending, not at next week's audit.

**Architecture.** A FastAPI route layer over the encrypted SQLite messaging store, with one deliberately singular access-control function (`get_channels_for_user`) authorizing every read path, REST and websocket alike, and Socket.IO for live delivery. Every read-then-write race is resolved in SQL (compare-and-swap or optimistic versions).

- **AI:** additive only, the 27B's one-sentence alert recommendation and the ambient 4B's wrong-room adjudication (promote-only). Everything that pages, escalates, or gates is deterministic.
- **Guardrails worth naming:** DMs are explicit membership rows with deliberately no admin bypass, every grant a fail-closed security audit event; unknown channel types fail closed; three separate PHI leak doors were found and closed in the 2026-08-05 audit; escalation writes share the transaction with the state change.

**Status.** Pre-pilot. Messaging and escalation logic is deterministic and extensively race-tested; the alert feed is simulated today. The honest gap is stated in the code's own messages: escalation has no roster mapping for a tier, so tier targeting is a best-approximation over the user table (the nurse who charted the observation, else the first nurse in the department), and delivery is in-app only, with no pager or push channel. Note that a `duty_roster` table now exists and the workforce desk maintains it (migration 24), but `escalation_service.py` does not read it; wiring the two together is open work, not a shipped capability.

**Key code:** `server/services/relay_service.py`, `server/services/escalation_service.py`, `server/routes/relay_routes.py`, `src/pages/panels/RelayPanel.tsx`

## 10. I-PASS Handoff: the shift handoff that drafts itself

*Part III, Documenting and communicating*

An auto-drafted structured handoff in the I-PASS standard (Illness severity / Patient summary / Action list / Situation awareness / Synthesis by receiver), assembled **purely from database rows**, demographics, problems, allergies, medications, open care bundles, the 12-hour vitals window, and the live NEWS2 band mapped to Stable/Watcher/Unstable. Two surfaces consume it: a per-patient view from the chart, and a nurse end-of-shift HandoverSheet that walks the whole ward list.

**The claim.** The outgoing clinician verifies a handoff instead of composing one: the sheet is already filled in from the chart, so handover time goes into judgement and worries, not transcription.

How, concretely:

- Illness severity is pre-assigned from the live NEWS2 band with the raw score and drivers shown, triage of the handover list is done before the huddle starts.
- The action list is assembled from what is actually open: active and PRN meds, open care bundles with overdue elements called out ("OVERDUE (sepsis6): Give IV antibiotics").
- Situation awareness computes the 12-hour trend and flags ACTIVE DETERIORATION; contingency lines ("if hypoxia → titrate O2 and call the covering doctor") are pre-drafted from the patient's actual risk drivers.
- The synthesis section is a deliberate read-back placeholder, the receiving clinician confirms understanding. The human closure step is kept, not automated away.
- The nurse sheet appends nurse commentary under an explicit heading, so a reader can always tell auto-drafted content from the human's; typed comments survive interruption via localStorage until saved.

**Where the win lands.** Time at every shift change, and safety in what a tired human forgets: allergies, PRN meds, an open sepsis bundle with two elements overdue, and pre-written escalation contingencies appear whether or not anyone remembered them.

**Architecture.** Deliberately deterministic, no LLM call exists in this module, so it can never fabricate content and works when Ollama is down. Persistence reuses the existing notes schema (`note_format='handoff'`).

- **Guardrails worth naming:** every sentence is templated from real rows; missing data degrades to ", " and "NEWS2 unavailable," never invented values; problems/allergies/meds each state "none recorded" explicitly rather than omitting the line; a breached Sepsis Six can never read as 100%.

**Status.** Pre-pilot, deterministic, covered by `test_ipass.py`. The contingency templates are clinically sensible heuristics that must be reviewed against the pilot site's own escalation protocol.

**Key code:** `server/services/ipass_service.py`, `src/components/handoff/IpassHandoffView.tsx`, `src/pages/panels/nurse/HandoverSheet.tsx`

## 11. Care Bundles: the Sepsis Six stopwatch

*Part III, Documenting and communicating*

A time-boxed protocol tracker, currently implementing the Sepsis Six (oxygen, blood cultures, IV antibiotics, IV fluids, lactate, urine output, each with a 60-minute target). A bundle opens manually from the chart or **automatically** when NEWS2 reaches 7 or a critical alert fires. A bedside widget shows a second-by-second clock, the six elements with per-element state, and one-tap "Mark done."

**The claim.** When sepsis is suspected, the team gets a running clock and a shared checklist instead of a verbal "start the Sepsis Six" that nobody can later prove happened on time.

How, concretely:

- Auto-trigger: the hour starts counting from *deterioration*, not from whenever someone opened the right screen, idempotently (never stacked), and clinician-cancellable.
- Each element is one tap, timestamped at the tap, with pending/overdue/on-time/late states, so the outstanding items are obvious at the bedside.
- Open bundles and their overdue elements automatically appear in the I-PASS action list and on the chart timeline, an unfinished bundle follows the patient across shifts instead of dying with the alert.
- Compliance is honest by construction: *completion* (how much is done) and *compliance* (how much was done within target) are computed separately, so a bundle finished late can never report 100%, exactly the number a pilot needs to be able to trust.
- The UI wording is engineered against misreading: an earlier "0% at risk" badge was read by every reviewer as "no risk" next to a NEWS2 16 patient, and the code documents why the phrasing changed. A days-old abandoned bundle displays as an age ("9d 3h"), not a 13,000-minute timer.

**Where the win lands.** Safety first, sepsis mortality rises measurably per hour of delayed antibiotics, and the bundle converts an alert into tracked, time-stamped execution. It is also the pilot's measurable-outcome instrument: demonstrable door-to-antibiotic compliance.

**Architecture.** A small deterministic service over one `care_bundles` table with element-timestamp columns; all state computed at read time; idempotent, non-raising triggers callable safely from the monitoring hot path. **No AI anywhere.**

**Status.** Pre-pilot, deterministic, tested. The 60-minute targets encode the widely-taught bundle but must be confirmed against the pilot hospital's own sepsis protocol before go-live.

**Key code:** `server/services/sepsis_bundle_service.py`, `server/routes/bundle_routes.py`, `src/components/vigil/CareBundleTimer.tsx`

## 12. Orders & eMAR: ordering and the bedside record

*Part IV, Medication safety*

Doctors place lab, imaging, medication, procedure, and nursing orders through a CPOE-lite panel validated against local LOINC/SNOMED/ICD-10 tables; orders move through a guarded state machine (draft → active → in progress → completed/resulted/cancelled) with an append-only event trail. Order sets apply a whole reviewed protocol as individually editable draft orders in one action. The eMAR is the bedside endpoint: each dose recorded as given or withheld (reason mandatory) into an append-only administration history, with the allergy interlock and dose guard firing **at the moment the drug goes in**, not just when it was ordered.

**The claim.** The doctor orders in seconds with the chart guaranteed to stay truthful, and the nurse charts a dose in one confirmed tap, while the system quietly catches the errors that chart clean everywhere else: the drug given after the allergy was reported, the 10× decimal slip, the double dose across a brand and its generic.

How, concretely:

- Validating a code auto-fills the official display name, so a correctly-worded coded order is *faster* to write than a free-text one; the server independently re-validates every code.
- Applying an order set replaces typing eight orders at 03:00 with one action, and every item passes the identical safety screen a hand-typed order would. If any single item is blocked, the whole set is refused with per-item reasons: a protocol can never be the quiet path around the interlocks.
- The allergy interlock re-screens at administration time against the allergy list *as it is now*, catching the antibiotic ordered at 02:00 before the daughter reported the penicillin reaction at 09:00, resolving brands through the hospital's own formulary (a charted "Monocef" screens as ceftriaxone), and failing closed: an unreadable allergy list blocks rather than reading as "no allergies."
- The dose guard blocks only the indefensible, the order-of-magnitude slip on ISMP high-alert drugs (digoxin 1.25 mg for 0.125 mg), KCl by IV push, intrathecal vincristine, warns on high-but-plausible, and deliberately stays silent on anything it cannot parse confidently, because a guard that cries wolf gets clicked through.
- Withholding is deliberately friction-free: a structured reason dropdown, never an interlock. Charting honesty is never punished.
- A per-modal idempotency key means a Wi-Fi drop and retry returns the already-charted record instead of a second dose; every override needs a typed clinical reason (junk like "ok" rejected) and commits in the *same transaction* as the dose, "the dose charted but the reason didn't" is not a reachable state.
- A discharged patient cannot receive a dose, a hole found by internal red-teaming on 2026-08-06 (enoxaparin administered post-discharge, 200 OK) and closed.

**Where the win lands.** Both, at the two highest-stakes moments of the day: signing the order and giving the drug. The errors this catches are exactly the ones that look clean on paper MARs and most EMRs.

**Architecture.** Deterministic Python services over encrypted SQLite. `orders_service` owns the state machine with a `pre_persist_gate` seam through which the safety screen runs before any write; `emar_service` is the single write path for a dose, replay dedupe, discharged-patient guard, allergy interlock, dose guard, override ledgers, and audit all execute in one transaction. **No AI**, AI-authored medications enter from other lanes and are structurally non-administrable here until pharmacist-verified.

- **Guardrails worth naming:** the duplicate-dose window is bounded by *time*, not row count (the LIMIT-400 bug that lost a week-old methotrexate dose is documented and fixed); override carry-forward is fingerprint-scoped, any change to the allergy picture, dose, or route re-arms the guard; a malformed order-set file stops the server at startup with file/line/field.

**Status.** Pre-pilot; functionally complete server-side and battle-tested by red-teaming. Honest gaps: the bedside override round-trip isn't in the AdministerModal UI yet (a BLOCK surfaces as raw error text); "barcode" confirmation is an enum with no scanner behind it; no scheduled due-time worklist yet.

**Key code:** `server/services/orders_service.py`, `server/services/emar_service.py`, `server/services/allergy_interlock.py`, `server/services/order_sets_service.py`, `src/components/vigil/AdministerModal.tsx`

## 13. Medication Safety Gate: one choke point for every drug

*Part IV, Medication safety*

Order-time drug safety as a single enforcement function: `enforce_medication_safety` is called by **every code path that writes a medication**, CPOE, Vigil med-add, Relay ambient actions, Sage agentic handoff, signed-note auto-extraction, Helix approve, so a drug cannot reach the chart without the same allergy and interaction screen, regardless of which door it came through.

**The claim.** The doctor gets the check a pharmacist would do in the first minute of reviewing an order, allergy, class, cross-reactivity, and interactions against the current med list, at the instant of ordering, on every entry path including the AI ones, with the system stating plainly when it could *not* check rather than implying safety.

How, concretely:

- Ordering amoxicillin for a charted penicillin allergy, or a cephalosporin for a penicillin-anaphylaxis patient (cross-reactivity), stops the order with a 428 before anything persists; overriding requires a ≥10-character clinical justification written permanently to the tamper-evident audit chain.
- Class allergies work the way clinicians actually chart them: an allergy recorded as "NSAIDs" correctly flags aceclofenac, nimesulide, etoricoxib, the most-prescribed drugs on an Indian ward, via stems plus enumerated members where a stem would over-fire (nystatin is not a statin).
- Coverage honesty is a feature the doctor sees: a drug outside the class map returns "drug–drug interactions were NOT screened, verify manually," because an unscreened drug must never look identical to a clean one. If the screen itself errors, ordering fails closed (503) rather than proceeding unchecked.
- **No AI can self-approve:** non-interactive paths can never acknowledge a severe alert away, a severe hit blocks the medication and routes it to human review, structurally, not as policy.
- Every screen, clean, alerting, blocked, or overridden, is audited with the entry point named, so "how often is this alert overridden, by whom, from where" is a query, not a chart review.
- In the voice lanes, ASR-mangled drug names ("amoxcilin," "metopro lol") are corrected in ~2 ms against a local RxNorm phonetic index; Relay reconciles spoken names to the hospital's own formulary IDs with a 0.8 similarity threshold, below it, no match is claimed.

**Where the win lands.** Errors caught at the cheapest possible moment, order time, before the drug reaches the chart, on every path at once. The audit trail also offloads the defensive-documentation burden: overrides record themselves.

**Architecture.** A single Python enforcement function wrapping a pure, shared pharmacology layer plus a SQLite interaction table, with a 428/503 HTTP contract mirrored across every surface. The same class map and name matcher serve order time, pharmacist verification, and the bedside interlock, one vocabulary, no drift between screening moments. **Deterministic by design**, so a screening verdict is reproducible and auditable.

**Status.** Pre-pilot. The machinery is complete and consistent; the clinical knowledge behind it is an explicitly hand-curated *starter* set (12 drug classes, 55 interaction pairs, ~19 cited dose rules), the code and docs both say a licensed source must be bulk-loaded before go-live, and the schema is ready for it. Known gap: a classified drug with no curated pair reads "No alerts," indistinguishable from a genuinely clean screen.

**Key code:** `server/services/medication_safety_gate.py`, `server/services/drug_safety_service.py`, `server/services/interaction_db.py`, `server/services/rxnorm_corrector.py`

## 14. Pharmacy Verification Queue: the human gate on AI medications

*Part IV, Medication safety*

One of only three tabs the pharmacist role gets. When Sage or any agentic pathway charts a medication, it lands with provenance `source='llm_extracted'` and a placeholder dose of **REVIEW REQUIRED**, on the chart but explicitly unverified, and structurally non-administrable. A pharmacist reviews each one, corrects dose/route/frequency, and verifies or rejects (reason recorded).

**The claim.** AI can draft a medication onto the chart, but no AI-extracted drug ever becomes an active order until a pharmacist has looked at it, and the safety screen re-runs at the exact moment they decide.

How, concretely:

- The backlog is one queue, not a chart-by-chart hunt; verify optionally dose-corrects in the same action.
- The full drug-safety screen re-runs on every verify and reject, failing closed on errors, so the decision screen always shows *current* alerts, a severe or failed screen demands an acknowledged reason (428) that lands on the audit chain.
- The placeholder dose makes the unverified state visible on the chart itself, not just in the queue.

**Where the win lands.** Safety first, the interlock between AI extraction and administration. With a time dividend: history import and ambient extraction can be aggressive *because* nothing they produce is trusted until this queue clears it.

**Architecture.** A provenance-gated review workflow over the medications table; deterministic; write-session re-guards against concurrent verification.

**Status.** Implemented, pre-pilot. It is the enforcement point of Orb's "AI drafts, humans decide" provenance model for medications.

**Key code:** `server/routes/pharmacy_routes.py`, `server/services/pharmacy_verification_service.py`, `src/components/helix/PharmacyVerifyQueue.tsx`

## 15. Helix: pharmacy and inventory

*Part IV, Medication safety*

The pharmacy pillar: the medication master catalog, lot-level physical stock, and a fixed audited state machine for every medication order (pending → reviewing → verified/rejected → scheduled → dispensed → delivered) where only legal next steps are ever offered. Two pharmacist workspaces: the Pharmacy panel (work queue, patient medication board, formulary with live stock) and the Verify Queue (Module 14).

**The claim.** No drug reaches a patient without a pharmacist's named sign-off, and the ward finds out the moment it happens, no phone calls to pharmacy, no guessing where a dose is.

How, concretely:

- The review dialog only ever offers the legal next steps for the order's current state; an illegal transition is refused server-side, and an idempotency key on every transition means a double-click or replayed request returns the recorded result instead of applying twice.
- Clicking Verify automatically posts "Pharmacy has verified the order… dispensing will commence" into the patient's Relay case room, the doctor and nurses see the sign-off unprompted.
- Dispensing decrements stock atomically with the status change, always from the earliest-expiring lot (FIFO); a stockout refuses with a clear error, and the remedy (restock, or a clinically-mapped substitute suggestion) is in the same dialog.
- A bare patient name on an approve request must match *exactly one* patient (case-insensitive equality, never a LIKE scan) or the order is refused, a medication can never be charted to the wrong patient by name ambiguity.
- The Patient Board answers "where is this patient's dose?" at a glance, "2/3 delivered, 1 in transit", so a nurse's call about a missing dose is a ten-second lookup, not an investigation.
- A failed panel load renders an explicit "connection failure, not an empty queue" error, the pharmacist can never mistake a dead backend for a clear queue.

**Where the win lands.** Errors caught at the pharmacy bench and at approve time (wrong patient, allergy/interaction, unverified AI meds, double-dispense), plus status phone calls between ward and pharmacy replaced by the auto-message and the board.

**Architecture.** A deterministic FastAPI service layer over six Helix-owned tables; no AI runs inside Helix, it is deliberately the deterministic human checkpoint downstream of Sage's AI drafting, protected by the shared medication safety gate.

**Status.** Pre-pilot. The state machine, idempotency, audit ledger, and stock deduction are real and tested; physical dispensing/delivery events are explicitly simulated (flagged `is_mock` in the DB so demo telemetry can't be mistaken for real supply-chain data), and each dispense deducts one unit regardless of quantity, demo-scale inventory, not a validated perpetual-inventory system.

**Key code:** `server/services/helix_service.py`, `server/services/helix_db.py`, `server/routes/helix_routes.py`, `src/pages/panels/HelixPanel.tsx`

## 16. Admission & Discharge: the intake-to-exit spine

*Part V, The patient journey*

Admission is a single transaction that lights up every module at once: the patient record, the encounter, the longitudinal profile, initial vitals with a seeded NEWS2 baseline, a Relay case room pre-populated with a structured admission brief, Sage's per-patient memory, and a unique one-time patient-portal login, all from one tabbed form. During the stay, one unified chart timeline merges **fourteen event streams** into a single rail with a per-clinician "since you last looked" watermark. Discharge produces a drafted summary anchored by a deterministic medication reconciliation, and a DOB-guarded QR link the patient takes home.

**The claim.** Admission stops being data entry into five systems, the chart answers "what happened since I last looked" in one glance, and the discharge summary, the hour-long chore that delays every bed, arrives pre-drafted with the medication list *computed*, not remembered.

How, concretely:

- One form, seven tabs, one transaction, the patient is live on Vigil, Relay, and Sage the moment admission completes.
- A doctor returning after a night off sees a divider and an exact "6 new since you last looked" count, computed by the database over all rows, so it stays true above a truncated list.
- The timeline never lets an unvetted extraction read like a charted drug: pharmacist-unverified medications carry an explicit UNVERIFIED marker, and "NOT given, reason" rows render with warning severity.
- The discharge narrative is LLM-drafted, but the DISCHARGE MEDICATIONS section is computed deterministically from the medication record, continued, stopped, PRN preserved (a dropped PRN opioid is named in code as the real safety defect this prevents), and injected as authoritative text the model must reproduce verbatim.
- The patient leaves with a QR link to their own summary, meds, and timeline, unlocked with their date of birth, no app, no account.

**Where the win lands.** Both ends of the stay: admission collapses to one form; discharge becomes review-and-sign. The safety weight is at discharge (deterministic med rec, PRN preservation) and in the timeline's honesty rules, an unparseable timestamp drops its row *visibly* rather than mis-ordering a chart a clinician reads specifically to establish sequence.

**Architecture.** Three cooperating layers: a transactional admission orchestrator; a declarative read-only timeline projection (each of the 14 streams generates both its detail SQL and its count SQL, so rail and badge can never disagree); and a discharge pipeline combining deterministic med rec, an LLM-drafted narrative (with a deterministic fallback if the model is down), and hashed access tokens. The timeline layer states its own epistemology: "if it disagreed with the eMAR, the eMAR is right."

- **Guardrails worth naming:** discharge is idempotent and atomic (the status flip is claimed first, so a double-click cannot write duplicate summaries or tokens); the review watermark never moves backwards, so offline replays cannot resurrect false "new" badges; admission generates a unique one-time portal password per patient, replacing a shared `patient123` default, called out in code.

**Status.** Pre-pilot; deterministic parts tested. The discharge narrative and the expected-course projection are unvalidated AI and framed as drafts, with heuristic floors.

**Key code:** `server/services/admission_service.py`, `server/services/patient_timeline_service.py`, `server/services/discharge_service.py`, `src/components/AdmitPatientModal.tsx`

## 17. Appointments: follow-up booking

*Part V, The patient journey*

Patients book their own follow-ups from the Bridge portal; clinicians book on a patient's behalf, cancel, and mark attended. The clinician-facing tab shows a day-by-day schedule grid with a KPI strip and acuity badges.

**The claim.** Patients book their own follow-up from their phone and the clinic list builds itself, no phone tag, no paper diary, and a double-tap can never create two slots.

How, concretely:

- Patients book for themselves only, the patient row is resolved from their login; any `patient_id` in the request body is ignored.
- Booking honours an idempotency key *and* a duplicate-slot guard; slot validation rejects past dates and anything beyond a 730-day horizon (a typo'd year once booked a slot 180 years out that never appeared anywhere).
- A completed appointment is a clinical fact, it cannot be cancelled, and "attended" is finally distinguishable from "no-show, still open."

**Architecture.** A small CRUD-with-guarantees router over one table, consumed by two very different frontends. No AI.

**Status.** Implemented, pre-pilot. One thing a reader must know: the clinician grid merges *real* bookings (badged "Booked") with deterministic, date-seeded **predicted** follow-up rows generated from the census, only the badged rows are real.

**Key code:** `server/routes/appointments_routes.py`, `src/pages/panels/AppointmentsPanel.tsx`

## 18. Surgical Suite: the operating-theatre command surface

*Part V, The patient journey*

A three-tier drill-down (week → day agenda → full case detail) over the theatre schedule, with per-case risk dots fed live from the patient's latest risk score. Emergency cases don't come from the booking book: Sage's ambient layer drafts a surgical-prep action from a live Relay conversation, and a clinician's confirmation creates the case, generates a prep package within a bounded 60 seconds, and pushes a live EMERGENCY INTAKE popup to every Suite screen. The backend also keeps a WHO Surgical Safety Checklist (2009) record per case with per-item attribution.

**The claim.** A surgeon sees the whole week of theatre, risk-ranked, in one glance, and an emergency case goes from a chat message to a staffed, briefed, allergy-checked theatre without a single phone call; the system will not let the case start until the team has actually confirmed.

How, concretely:

- Opening a case streams a structured pre-op risk assessment (ASA class, risk grade, anesthetic considerations, first-48h monitoring priorities) from the local MedGemma model, built from the patient's real chart, a pre-op briefing in seconds instead of paging through the chart. If the model is offline, a clearly labelled static fallback appears, never silence.
- Team assembly is accountable: each member gets a personal targeted alert (not a department blast); each Confirm/Decline posts a system message; and **Start Surgery is refused by the server**, not just greyed out, until every alerted member has responded and at least one confirmed.
- The case detail leads with what the anaesthetist must see, with a deliberate honesty rule: an empty allergy field renders "Allergies not recorded", an open question to ask the patient, never a fabricated "NKDA," because Orb has no basis to assert a negative.
- Completion is idempotent and files a post-op note with a compensating rollback, a failed note write reverts the status, so a completed operation can never exist without an operative record.
- The WHO checklist keeps per-item who/when attribution, concurrency-safe for two team members ticking simultaneously, every tick on the audit chain.

**Where the win lands.** Both, at the sharpest point of the hospital day: time assembling pre-op context and mustering an emergency team, and errors caught by hard server-side interlocks.

**Architecture.** A FastAPI route layer over three services, schedule/detail/streamed risk, Relay-escalated emergency intake, and the WHO checklist, feeding a socket-first React panel with polling fallbacks. This module is where the sync-SQLite-on-the-event-loop wedge was found; all blocking DB work now runs off the loop.

**Status.** Pre-pilot, and the most demo-weighted module in this group. Real and solid: the schedule, state machine, server-side gates, team alerting, auditing, checklist backend, and the streamed risk assessment (a genuine, unvalidated model call). Demo: the emergency prep package is a hardcoded template the UI badges "AI Generated" although no model ran (flagged for relabelling before shadow mode); the WHO checklist backend has **no frontend consumer yet**.

**Key code:** `server/routes/surgery_routes.py`, `server/services/surgery_escalation_service.py`, `server/services/who_checklist.py`, `src/pages/panels/SurgicalSuitePanel.tsx`

## 19. Nurse Mode (My Shift): the task-shaped home

*Part V, The patient journey*

The ward nurse's landing tab, deliberately not patient-shaped but **task-shaped**: a flat cross-patient "Due now" queue of overdue and due-soon observations and medication doses, sorted by deterioration first, lateness second. Five views: Due now, My patients, Deliveries, Handover (I-PASS across the whole list) and End of shift. Nurses record bedside obs with a live NEWS2 preview as they type, run the med round, sign for medication arriving on the ward, and keep working from a cached snapshot when the tablet loses network in a corridor dead zone. Deliveries is gated on the `helix.receive` permission rather than on the panel's role, so an account that reaches nurse mode without the grant is shown four tabs rather than one whose every request would 403.

**The claim.** A nurse opens one screen and sees every obs set and dose she owes across the whole bay, ranked so the deteriorating patient's late obs always outranks a stable patient's late paracetamol.

How, concretely:

- The board assembles the whole shift as tasks from existing tables: obs due times follow the RCP NEWS2 escalation table; med due times derive from order frequency anchored on the last given dose, each carrying a `due_basis` saying which anchor was used.
- The obs entry sheet scores the candidate set server-side on every keystroke, the same scoring code as the record path, so the nurse sees the NEWS2 she is about to create before saving.
- Recording obs dispatches a Vigil escalation into the case room, so a peri-arrest set summons the response without a second app.
- Med administration routes through the existing eMAR AdministerModal, there is exactly one medication-administration write path in the system.
- Unschedulable meds (PRN, continuous, unparseable frequency) are excluded from the owed-task list rather than guessed at, a med Orb cannot time is a med it must not imply is late.

**Architecture.** Three sync FastAPI endpoints (`nurse_routes.py`) over a pure-composition service: no new clinical rules, one genuinely new write (nurse-entered obs, tagged `nurse_manual`). The Deliveries tab is the exception, and deliberately so: it is served by two `helix_routes.py` endpoints under `helix.receive`, so the ward receipt lives with the custody chain it closes rather than in the nurse's own module. Deterministic throughout.

- **Guardrails worth naming:** ward scope is derived server-side from the authenticated user, the old `?department=` params were removed, so a client cannot widen scope from any request field; the board read is audited fail-closed *before* it is built; obs writes are idempotent on (patient, source, taken_at); even the persist-nothing NEWS2 preview is scope-checked, it previously leaked patient existence and the SpO2 Scale-2 flag (a diagnosis) to any `vitals.read` holder.

**Status.** Implemented and wired as the nurse landing tab; pre-pilot. Derived due times are explicitly a working list, not a prescription schedule.

**Key code:** `server/routes/nurse_routes.py`, `server/services/nurse_shift_service.py`, `src/pages/panels/nurse/NursePanel.tsx`, `src/pages/panels/nurse/DeliveriesSheet.tsx`, `server/routes/helix_routes.py` (the `helix.receive` ward-receipt route)

## 20. Bridge: the patient's own window

*Part V, The patient journey*

The one screen designed to be handed to the patient or a family member at the bedside: how they're doing in calm, jargon-free language (reassurance-first status wording), who is looking after them, vitals trends against plain-English normal ranges, medications, reports, discharge summary, a FHIR download of their whole record, and self-service follow-up booking. At the bottom, an AI question box streams answers in simple words from the hospital's own local model.

**The claim.** Bridge keeps patients and families informed and calm without a staff member in the room, so the doctor and nurses field fewer "what does this number mean?" interruptions and have a better-informed patient at every conversation.

How, concretely:

- The status card derives from the same risk scores clinicians see in Vigil, worded reassurance-first, the family stops flagging down a nurse every time a monitor number changes.
- The chat answers everyday questions ("what is this tablet for?") in simple language, hard-instructed to redirect every clinical decision to "please ask your nurse or doctor."
- A deterministic regex screen catches emergency and self-harm language *before any LLM call*, returns a fixed press-your-call-button response, and publishes a real-time `bridge.emergency.flag` event to the duty team, the chatbot becomes an extra escalation channel instead of a reassurance trap.
- The AI context is minimal by design: name, status level, one-line condition, four current vitals, never notes, labs, or the full chart.
- Every endpoint is structurally self-scoped: the patient record is resolved from the signed-in account; the client never supplies a patient identifier.

**Where the win lands.** Cognition offloaded from nurses and doctors during rounds and family visits, the repetitive explanation load is absorbed by the portal. Secondary safety win: patient-typed crisis language becomes a real-time alert instead of a chatbot reply.

**Architecture.** A FastAPI route module in which every endpoint depends on `require_patient_self()`, ownership is structural, not conventional. Chat streams from local Ollama on the background LLM lane, so clinical interactive traffic keeps GPU priority.

**Status.** Pre-pilot. Implemented end-to-end; the assistant is unvalidated and has never met a real patient. A known gap for the Mumbai market: the red-flag regexes are English-only, a patient typing in Hindi or Marathi will not trip the emergency screen.

**Key code:** `server/services/bridge_service.py`, `server/routes/bridge_routes.py`, `src/pages/PatientShell.tsx`

## 21. Discharge Access: the link the patient takes home

*Part V, The patient journey*

A public, unauthenticated page at `/discharge/:token`. The hospital issues a secure link at discharge; the patient opens it, enters their date of birth, and gets their discharge summary, medications, and timeline in a printable view, no account, no password, no app install.

**The claim.** The patient leaves with a link, not a login, their summary, med list, and follow-up instructions are one DOB entry away, which is the realistic bar for patients who will never create a portal account.

How, concretely:

- Two-step flow: token + DOB returns a short-lived JWT grant; the record never rides on the token alone.
- Per-token attempt lockout *plus* a per-IP sliding-window throttle across all tokens, holding many tokens does not bypass the lockout.
- Expiry and revocation are first-class states with distinct patient-readable messages; the printable view is the same `DischargePrintView` used clinically, so the patient's printout matches the clinical document.

**Where the win lands.** Time saved on medical-records requests and readmission risk reduced (the med list actually reaches the patient), while the CIO conversation about "an unauthenticated PHI surface" gets a concrete answer.

**Architecture.** A deliberately tiny public API (verify, summary) over a hardened token service: SHA-256-hashed-at-rest tokens (a documented fix, plaintext tokens once leaked into audit logs via correlation IDs), constant-time DOB comparison, uniform 401s that leak nothing about which part failed.

**Status.** Implemented and specifically hardened; pre-pilot. The per-IP throttle is in-process only (single-worker safe), a documented deferral.

**Key code:** `server/services/discharge_access_service.py`, `src/pages/DischargeAccess.tsx`

## 22. Pulse: season and conditions

*Part V, The patient journey*

A panel in the shell header that shows the season for the hospital's state: what usually rises at this time of year ("This season" cards with a period, a **Peak now** tag, a short summary, "Typically rises: ..." and the sources named in plain text) and what starts in the next 60 days ("Coming up"). It reads a seasonal calendar that ships with Orb (`server/site_config/seasonal_calendar.json`) for the state set in `site.toml` (`[site] state = "Maharashtra"`). It makes no internet connection of any kind.

**The claim.** The ward and the pharmacy see the monsoon coming before the dengue and leptospirosis cases do, from a calendar a clinician has checked, with no feed to fail and nothing leaving the building.

How, concretely:

- The calendar ships marked "Draft: not yet reviewed by a clinician" and shows "Reviewed by [name] on [date]" only after the pilot hospital's clinical lead has reviewed it.
- No state set means Pulse asks for it. It never guesses, never asks the browser for a location and never looks one up; an unknown state in `site.toml` stops Orb at startup with a "Did you mean" hint.
- Forecast shows the season as one line of context ("Season: South-west monsoon (peak), Dengue season") and applies no weather or seasonal adjustment to any number.

**Architecture.** One route, `GET /api/pulse/season` (`forecast.read`), over `server/services/seasonal_calendar.py`. The file is read once at startup and validated whole; a missing or broken file is logged and makes the route answer 503 with one plain sentence, but never stops Orb. No AI and no network anywhere in the path.

**Status.** Rebuilt 6 October 2026 under the strictly-offline rule (ruling R030). Until then Pulse pulled four live internet feeds (Open-Meteo weather and air quality, US FDA recalls, US CDC flu), located the user through browser geolocation and openstreetmap.org, and Forecast multiplied admissions by hand-picked weather guesses (+20% "cardiac" above 42 °C). All of that is gone. Staged plan, neither stage built: stage 2, a one-minute daily entry of official warnings (IMD heatwave, CPCB AQI) by a designated person, plus a hospital weather and air-quality sensor on the LAN; stage 3, disease trends from the hospital's own admissions and the CDSCO recall list checked against stock. Ruled out: a separate internet-connected box that fetches weather and pushes it into Orb, because it is a bridge from the internet into Orb.

**Key code:** `server/services/seasonal_calendar.py`, `server/site_config/seasonal_calendar.json`, `server/routes/pulse_routes.py`, `src/components/PulseWidget.tsx`

## 23. Revenue Integrity: coding and CDI

*Part VI, Running the hospital*

The "Revenue" tab: pick a patient and Orb scans their diagnosis, coded problem list, comorbidities, notes, and Scribe transcripts to suggest ICD-10/SNOMED codes (each with an evidence sentence and confidence), flag documentation gaps, and show a headline "Reimbursement: ₹captured → ₹potential (+₹uplift)" derived from a transparent, editable assumption table.

**The claim.** Before the bill goes out, the hospital sees which documented conditions were never coded, revenue already earned but left on the table, with the evidence sentence that supports each one.

How, concretely:

- Whole-token lexical matching of ~27 curated concepts with synonym lists, so "MI" matches but "admission" never does; medication→condition rules fire the classic CDI signal (metformin charted but diabetes never coded means its complication weight is uncaptured).
- Every emitted code is validated against the deployment's loaded terminology table, a partial load yields fewer suggestions, never bogus codes.
- A coder works from a ranked list, highest-value gaps first, instead of re-reading the chart.

**Where the win lands.** Money and time for the back office, the one module aimed at the revenue side of the buyer's P&L.

**Architecture.** A read-only deterministic engine, lexical mapper + rules + arithmetic, behind one endpoint. Despite the "AI-assisted" framing, the LLM refinement hook is a documented no-op stub today.

**Status.** Implemented, deterministic-only, pre-pilot. Rupee figures are structurally labelled ILLUSTRATIVE (CMS relative-weight concept with placeholder rates); a site must load its own PM-JAY or private tariff before any figure means money. A fixed honesty bug worth knowing: an unknown patient ID used to return the same all-zero report as a fully-documented patient, a typo'd ID read as a clean CDI review. It now 404s.

**Key code:** `server/services/coding_service.py`, `src/components/coding/RevenueIntegrityPanel.tsx`

## 24. Impact & ROI Dashboard: the money model

*Part VI, Running the hospital*

The "Impact" admin tab converts actually-recorded activity, Scribe notes authored, Vigil escalations, Sepsis-Six bundles started, pharmacist verifications, drug-safety reviews, discharge reconciliations, into rupee figures and prevented-harm estimates. Every figure is (counted event) × (named, cited assumption), denominated in INR with lakh/crore grouping.

**The claim.** The CFO does not get a vendor's savings claim, they get a model where every rupee traces to a counted event in this hospital's own database times an assumption they can replace, live.

How, concretely:

- Each metric carries the human-readable assumption that produced it (e.g. 7 min saved per note, vs published ambient-scribe studies) with its citation.
- Every assumption is overridable per-request, doc minutes per note, clinician hourly cost, ICU-day cost, ADE cost, avoidance rates, so diligence is interactive rather than adversarial.
- An empty database returns zeros with "no data yet" framing, never an exception or a fabricated figure.

**Where the win lands.** This is the buying-decision module: it lands in the CIO/CFO meeting and in fundraising. It is deliberately distinct from the Shadow Scorecard (Module 29), the scorecard is the clinical evidence (*was Orb right?*), Impact is the money model (*what was it worth?*). They are the two halves of the pilot's business case.

**Architecture.** Pure-read arithmetic, no writes, no AI; one admin endpoint; a dashboard that renders figure + assumption + evidence count together.

**Status.** Implemented; explicitly a diligence artifact on placeholder unit-economics a deploying site must replace.

**Key code:** `server/services/impact_service.py`, `src/pages/panels/admin/ImpactDashboard.tsx`

## 25. EHR Downtime Mode: the worst hour of the CIO's year

*Part VI, Running the hospital*

A durable local snapshot system plus a paper-like fallback page at `/downtime/:patientId`. Orb freezes each patient's must-know data, demographics, allergies severity-sorted and front-and-center, problems, active meds, latest vitals with rule-based NEWS2, recent signed notes, into local rows, served read-only with **zero external dependencies**: no live data, no streaming, no LLM, no network.

**The claim.** When everything else is down, the ward still has a safe, current, printable picture of every patient, something a cloud EHR structurally cannot promise.

How, concretely:

- Capture is pure DB reads plus in-process rule-based NEWS2 (deliberately bypassing the optional neural overlay), so a snapshot can always be taken on a box with no models and no connectivity.
- "Capture all" snapshots the whole admitted census in one admin action, with per-patient failure accounting, for drills and pre-downtime jobs; the Command Center's readiness tile tracks freshness.
- The page is high-contrast, print-on-one-tap, and renders entirely from the frozen snapshot, with allergies at the top, called out in code as the single most safety-critical field in a downtime view.

**Architecture.** A snapshot-capture service with a hard no-Ollama/no-network design contract. One deliberate inversion: the degraded-mode *read* path is not fail-closed-audited, wiring a fail-closed audit write into the surface whose job is to work when things are broken is exactly backwards (captures *are* audited).

**Status.** Implemented; not yet through a clinical pilot. Snapshot freshness depends on capture discipline, the operational cadence is a deployment decision.

**Key code:** `server/services/downtime_service.py`, `src/pages/DowntimeMode.tsx`

## 26. Platform & Runtime: the box everything sits on

*Part VII, The foundation*

One encrypted appliance in the hospital cupboard runs the whole record and all its AI: a single-process FastAPI + Socket.IO application on one Apple Silicon Mac Studio, all data in one SQLite file (WAL mode, `sqlite-vec`, SQLCipher), all inference local via Ollama plus a spawned knowledge-worker child process. Around it: a durable at-least-once event bus, a two-lane LLM admission queue over a ref-counted GPU gate, a residency-aware model router, and out-of-band liveness daemons that can report even when the event loop is frozen.

**The claim.** Indirect but real: answers arrive in seconds instead of minutes, the system stays up through network outages and background load, and no patient chart ever leaves the building, so there is no cloud-consent conversation to have. The doctor never sees this module; they feel it as "it answers fast and it was up at 3 a.m."

How, concretely:

- The interactive lane is reserved for the clinician: a dedicated semaphore separates clinician turns from background work, built after a measured incident where a Sage question sat 31 seconds behind a ward-narrator stream against a ~2-second answer.
- Models are kept warm on Sage's own system prompt every 5 minutes, 0.4 s vs 6.9 s to first words on question one, and if the 27B is evicted to reclaim ~17 GB, the router routes to the resident 4B with an explicit "downgraded" flag rather than an 80-second cold reload.
- The event bus gives ambient AI at-least-once delivery: every publish writes a per-subscriber ledger row, and a replay poller re-dispatches after crashes, "patient deteriorated" can never be silently dropped.
- A daemon-thread loop watchdog detects event-loop freezes that `/api/health` structurally cannot see (the health endpoint is served by the loop that froze), writing pre-diagnosed incident dumps and a status file someone on site can read while the API is frozen. Nothing is sent off the appliance: Orb is strictly offline.

**Architecture.** Concurrency disciplined by workload class: threads for work that releases the GIL in C (SQLite, HTTP), processes for work that does not (tokenizers, torch). The ~2 GB embedder lives in a spawn child because in-process loads froze the API. One SQLite writer, one uvicorn worker, enforced by design.

**Status.** Pre-pilot, battle-hardened by real incidents, three production event-loop freezes (GIL starvation, the configd proxy lookup, a SQLCipher close deadlock) are diagnosed in code comments and fixed, with measured soak results: 60-bed simulation, `/api/health` p99 of 41 ms over 1,797 probes; 2.7–3.2 s to first Sage words.

**Key code:** `server/main.py`, `server/services/event_bus.py`, `server/services/llm_queue_service.py`, `server/services/gpu_coordinator.py`, `server/services/model_router.py`, `server/services/loop_watchdog.py`

## 27. Security & Audit: who you are, whether you may, can we prove it

*Part VII, The foundation*

The identity, authorization, crypto, and evidence layer. Auth is bcrypt + 24 h JWTs in HttpOnly cookies with CSRF double-submit, token-version revocation, per-IP rate limiting plus per-account lockout, a stdlib TOTP MFA implementation, and a **separate signing PIN** so a stolen session cannot commit an order. At rest: SQLCipher whole-database encryption plus AES-256-GCM envelopes for uploads and PHI-derived fields. Every mutation lands on a SHA-256 hash-chained, trigger-enforced append-only audit log.

**The claim.** Indirect: the doctor can trust the chart (nothing can be edited after the fact without leaving evidence), keep working through an outage, and the hospital adopts without a data-leaves-the-building fight, while the login machinery is engineered *not* to lock a clinician out at 3 a.m.

How, concretely:

- Tampering is evident, not just forbidden: `BEFORE UPDATE/DELETE` triggers on the audit table `RAISE(ABORT)`, each row hash-chains to the previous, a unique index rejects chain forks, and the whole chain is re-verifiable on demand.
- Backups are proven before anything is rotated: size floor, SQLCipher decrypt, integrity check, and row-count assertions, built after a silent failure where zero-byte "backups" passed integrity checks while rotation pruned the real ones to make room.
- Lockout design is clinician-shaped: the per-account lockout is a throttle, not a permanent DoS; MFA's "no code yet" step never counts as a failure; an enrolment-ticket flow exists so the first admin on a fresh appliance can never brick themselves.
- Role is read from the DB on every request, never from the token, a demotion takes effect on the next request, which is what makes 24-hour tokens tolerable.
- Fail-closed audit categories: a PHI access refuses to proceed if its audit row cannot be durably written, verified by execution on 17 of 18 PHI surfaces.

**Where the win lands.** Safety and continuity rather than minutes: errors-of-record become impossible to hide, and the CIO-facing Trust Center turns MFA adoption, the RBAC matrix, and chain verification into live evidence instead of a policy PDF.

**Architecture.** One dependency makes all auth decisions from a single-statement DB snapshot per request, off the event loop. Two crypto layers under different keys; two audit layers (rich route-level events inside the caller's transaction, plus a middleware safety net for every mutation). All deterministic, no AI.

**Status.** Pre-pilot; encryption at rest shipped 2026-07-30, backup hardening done. Open items the repo itself states: the key-escrow ceremony (a lost `ORB_DB_ENCRYPTION_KEY` makes every backup permanently unreadable), per-device revocation, external audit-chain anchoring, and one live defect, cookie-authenticated mutations write middleware audit rows with a NULL actor (route-level events carry the actor correctly).

**Key code:** `server/auth.py`, `server/permissions.py`, `server/services/audit_service.py`, `server/services/mfa_service.py`, `server/services/pin_security.py`, `scripts/backup.sh`

## 28. Governance & Interop: the honesty layer

*Part VII, The foundation*

Four surfaces that make Orb auditable to the people who must sign off on it: a **model governance registry** (every AI surface's version, training-data lineage, live checkpoint presence, validation status), **FHIR R4 ingest/export** with provenance tags, the **flight recorder** (a read-only forensics timeline over the audit chain with cryptographic verification), and the shadow instrument (Module 29).

**The claim.** The direct beneficiary is the CMO and CIO, this is what lets the hospital adopt AI without taking Orb's word for anything, but the provenance chips also protect the doctor from ever unknowingly acting on an unvalidated model's output.

How, concretely:

- Every AI output can carry a provenance chip readable by any clinician: NEWS2 is marked *validated* (published RCP standard); the CfC overlay is registered "UNTRAINED, no checkpoint"; the forecast LSTM "SYNTHETIC v1, NOT trained/validated on real clinical data." `checkpoint_present` is computed from the filesystem at seed time, so the governance UI cannot over-claim a model Orb no longer ships.
- FHIR export marks every resource with an Orb source tag, marks AI-derived content `ai-draft`, and downgrades any unverified AI-suggested MedicationRequest to draft/proposal, it can never land in a receiving EHR as an active order.
- Ingest accepts HL7 ORU^R01, JSON webhooks, CSV replay, and FHIR behind a constant-time API-key check, with raw-payload provenance capture and fingerprint dedup, then recomputes NEWS2 and runs the same Vigil alert logic as native data.
- The flight recorder merges audit logs, the event bus, and order events into one time-ordered timeline, and its `verify_chain` imports the *writer's exact hash function*, including catching the erased-hash-restart tampering move.

**Architecture.** Write/read separation everywhere; the registry is seeded from one code-defined source of truth with live filesystem checks. No AI runs here, this layer observes and governs the models rather than executing them.

**Status.** Pre-pilot. The registry reflects reality today: only NEWS2 is validated. FHIR ingest is a constrained profile, not a general interface engine.

**Key code:** `server/services/model_governance_service.py`, `server/services/fhir_export_service.py`, `server/services/ingestion_service.py`, `server/services/flight_recorder_service.py`, `src/components/ProvenanceChip.tsx`

## 29. Shadow Mode Scorecard: the pilot's evidence instrument

*Part VII, The foundation*

For the silent phase of the pilot, Orb ingests two wards' data, runs the full Vigil → Sage → Relay loop **without clinicians seeing it**, and accumulates a scorecard against what clinicians actually did: detection lead time, false-alert rate, sensitivity and misses, clinician agreement. This is the artifact the Week-13 go/no-go decision rests on, and arguably the most CIO-relevant module in the product.

**The claim.** At the end of the pilot the CIO gets four numbers they can be cross-examined on, every figure carries its denominator, its date range, and its insufficient-data state, and the instrument is built so it cannot flatter Orb even by accident.

How, concretely:

- Episodes are threaded from alert birth through outcome, and a sweeper writes the *negatives*, alerts never opened, proposals never actioned, so the scorecard cannot score only successes.
- Never a bare 0%: every metric carries state, n-observed, and n-required; a metric below its floor renders the count and the floor, never a number. The UI's Figure component takes denominator and date range as *required props*, so a bare percentage cannot be written.
- Negative detection leads (clinician ahead of the machine) are reported, not clipped; a confirmed deterioration with no qualifying alert is a counted miss, never an imputed absence.
- The false-alert headline is per *alert*, because that is the unit a nurse experiences, the per-episode figure understated interruption burden 49× in pilot-shaped data.
- Evidence generation is incentive-isolated: charge nurses hold a write-only outcome permission and cannot watch the metrics move as they enter ground truth; the adjudication queue is blinded; "nothing happened" is distinguished from "nobody logged it," and uncovered shifts surface as uncovered time on the scorecard's face.
- Simulator-lane rows are excluded from every metric by default, with the excluded volume reported, simulated alerts cannot masquerade as evidence.

**Architecture.** Strict write/read separation: the event writer is fail-*open* (a shadow write must never fail a clinical action, the exact opposite failure policy from the fail-closed audit log, on purpose); the scorecard reads a corrections-aware view so revised adjudications never double-count. One verdict vocabulary shared by writer and reader, a local copy once silently discarded every clinician adjudication ever recorded.

**Status.** The instrument is implemented, hardened, and self-checking; **the evidence is zero until the pilot runs**, by design, it can only accumulate against real ward data.

**Key code:** `server/services/shadow_service.py`, `server/services/shadow_scorecard_service.py`, `server/routes/shadow_routes.py`, `src/pages/panels/admin/Scorecard.tsx`, `docs/SHADOW_MODE.md`

---

## Appendix: the claims we are not yet allowed to make

A manual that only lists strengths is marketing. These are the system-wide truths that bound every claim above, collected in one place:

- **No real patient has ever been scored, paged, transcribed, or summarized.** Vitals come from a simulator behind `VitalsSource`; the pilot's shadow phase is where every clinical claim gets tested.
- **Both neural networks ship untrained** (Vigil CfC, Forecast LSTM) and fail closed to deterministic rules. Today, "AI risk score" means NEWS2, and that is stated in the UI.
- **The LLMs are not clinically validated.** MedGemma 27B/4B are measured, not trusted: internal evals documented invented citations, fabricated chart data on the 4B, and wrong drug recalls, which is why the guardrail stack (citation validation, score pinning, mutation gates, verification sign-gates, pharmacist quarantine) is deterministic.
- **Curated knowledge bases are starters, not licensed databases**: 55 interaction pairs, ~19 dose rules, ~19 corpus documents. Licensed sources must be bulk-loaded per site before go-live.
- **Some demo content still wears an AI badge**, the surgical prep package is a hardcoded template labelled "AI Generated"; the clinician appointments grid mixes predicted rows with real ones; Helix dispensing is mock. Each is flagged in its module's status line.
- **Known open engineering items**: key escrow, per-device revocation, external audit anchoring, the Scribe verification default, Hindi/Marathi red-flag coverage in Bridge, Pulse stages 2 and 3 (logged official warnings and a LAN sensor; trends from the hospital's own admissions and CDSCO recalls against stock), and in-app-only escalation delivery (no pager/push integration yet).

The shadow scorecard (Module 29) exists precisely so that, at Week 13, the sentence "Vigil detects deterioration earlier than the ward noticed it" either has a number behind it or is retired. Until then, every claim in this manual is a design claim, engineered honestly, and awaiting evidence.
