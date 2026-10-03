# Orb v2: Agentic Execution & Platform Hardening

This document describes the v2 work on the Orb on-prem hospital OS: turning Sage
from a system that *suggests* actions into one that *executes* them, plus the
real-time coordination, UI, and reliability work that shipped alongside it.

> **Scope.** This covers the engineering changes on this branch. It is both a
> changelog and a short architecture guide for the new agentic pipeline. Where a
> behaviour was verified live, it says so; where a limitation remains, it says
> that too.

---

## 1. Headline: the agentic loop now executes

Before v2, a clinician message in a Relay case room produced a Sage "action
card", but **Confirm only flipped the card's state**, nothing downstream
happened. v2 wires the card straight through to the clinical action.

### 1.1 End-to-end flow

```
Clinician message in a Relay case room
        │
        ▼
sage_ambient_observe()            (background task, MedGemma 4B)
        │   classifies → possible_action → extracted_intent{action, target, rationale}
        ▼
sage_actions row  (state = 'suggested')
        │   rendered as a card in the Sage Copilot sidebar
        ▼
Clinician clicks **Confirm**
        │   POST /api/relay/channels/{id}/sage_actions/{aid}/transition  {state:'confirmed', version}
        ▼
create_and_dispatch_handoff()      (services/sage_handoff_service.py)
        │   durable sage_handoffs row + event log (idempotent)
        ▼
_dispatch_handoff()                 routes by the extracted VERB
        ├── medication verbs  → _dispatch_to_helix()    → chart med + Helix order
        ├── surgery/transfer  → _dispatch_to_transfer()  → create surgery + alert team
        └── document/note     → _dispatch_to_scribe()    → structured note
```

### 1.2 The core bug that was fixed

`_dispatch_handoff` originally routed only the **literal** `action_type == "prescribe"`.
But the 4B model emits verbs like `start_medication`, `add_medication`, `add`,
`transfer_to_surgery`, so **every card fell through to "no downstream route"** and
Confirm did nothing.

It now routes by keyword sets, **checking surgery first** so "start surgery"
isn't mis-routed to pharmacy:

| Bucket | Keywords (substring match on `action_type`) | Downstream |
|---|---|---|
| Surgery / transfer | `transfer, surger, surgical, operat, laparotomy, theatre, book or, book surgery` | `_dispatch_to_transfer` |
| Medication | `prescribe, medication, drug, antibiotic, start, stop, add, discontinue, switch, hold` | `_dispatch_to_helix` |
| Document | `document, note, chart` | `_dispatch_to_scribe` |

> `escalate` / `consult` deliberately match **nothing**, they have no safe
> downstream yet, so they resolve to `unsupported` rather than wrongly creating a
> surgery.

### 1.3 Medication path (`_dispatch_to_helix`)

- **Charts the med first** into the `medications` table (best-effort, wrapped in
  try/except) so it **always appears in Vigil**, even for a drug not in the Helix
  formulary. Parses the route (IV/IM/SC) from the rationale.
- **Then** creates the Helix pharmacy order (best-effort). A non-formulary drug is
  still charted; only the pharmacy order is skipped (returns `charted-only`).

**Verified:** doctor message *"Add IV metronidazole 500mg TID"* → card →
Confirm → metronidazole charted in Vigil (route `IV` parsed). Formulary drug
(Pantoprazole) → **Vigil chart + Helix order** both created.

### 1.4 Surgery path (`_dispatch_to_transfer`)

On Confirm of a transfer/surgery card:

1. Creates a `surgeries` row (`status = emergency_pending`) with **real
   comorbidities** snapshotted from the patient profile (no more demo hardcode).
2. Resolves a **real lead surgeon** via `_resolve_surgeon_id` (see §4).
3. Creates **targeted** `surgery_team_assignments`, Lead Surgeon + Scrub Nurse +
   Anesthetist only, each marked `alerted`.
4. Emits `surgery:emergency_intake` (Suite refresh) and per-user `message:new`
   `surgical_alert`s so **only the assigned staff** are notified live.
5. An async task generates the AI prep package (ASA risk, checklist, instruments,
   complications) via MedGemma.

**Verified live:** DOC001 (Medicine) confirms a transfer → surgery created and
assigned to **DOC004 (General Surgery)** → appears in DOC004's **Surgical Suite**
as an emergency case with a full, accurate detail view; the three role alerts post
to the case room, and **only the recipient sees an actionable Confirm/Decline** on
their own alert.

---

## 2. Real-time coordination (Relay + Socket.IO)

- `server/ws/realtime.py`, each user joins a personal room `user:{user_id}` on
  connect, enabling **targeted** delivery instead of channel-wide broadcast.
- New message type `surgical_alert` (see the schema fix in §6).
- `post_message` is async and emits `message:new` to the channel room.
- New `POST /channels/{id}/read` + `mark_channel_read()`, finally writes
  `messages.read_by`, so Relay **unread badges actually clear**.
- Discharged patients' case rooms are filtered out of the channel list.

---

## 3. Surgical Suite

- `/schedule` LEFT JOINs `risk_scores` so Suite cards show **real risk** (was
  always `stable`).
- Detail reads comorbidities from the `surgeries` snapshot first, falling back to
  the patient profile.
- **Allergies safety affordance (new):** the detail header shows red `⚠ <drug>`
  pills, or a green **`NKDA`** badge when none, an explicit "checked, none known"
  signal, not just absence. The render normalizes allergies whether they arrive as
  a parsed array or a raw JSON string, so a string can never crash the panel or
  silently hide a real allergy.
- Active Cases shows the real `surgery_team_assignments` (assigned + `alerted`).

---

## 4. Surgeon resolution (`_resolve_surgeon_id`)

When a **medical** patient is transferred to surgery, the lead surgeon should be a
**surgeon**, not the medical attending. Resolution order:

1. The attending **iff they are themselves in a Surgery department**.
2. Otherwise the on-call **Surgery-department doctor**.
3. Otherwise the attending doctor, then the confirming clinician.

This is why a transfer from DOC001 (General Medicine) correctly lands on
DOC004 (General Surgery).

---

## 5. UI: premium liquid glass

- `src/index.css`, a design-token layer: elevation scale, a reusable
  `--glass-*` kit (saturate, highlight, sheen), type scale. The shared
  `.glass` / `.glass-card` / `.glass-strong` surfaces now use real frosted glass
  (backdrop blur + saturate, 1px top highlight, layered shadows, gradient sheen,
  gold hover glow).
- Shell sidebar, nav, and Sage bubbles/composer/suggestions reskinned to match.
- **Sage Copilot sidebar** (`SageAmbientSidebar.module.css`) was still a
  **light theme** (white panel, cyan accents), a glaring white block against the
  dark app. Converted to the dark liquid-glass system with a gold-gradient Confirm
  button.

---

## 6. Scribe

- Type or dictate a case → **SOAP / Discharge Summary / I-PASS Handoff** via one
  MedGemma streaming pipeline (`structure_transcript_stream`, format-selected).
- Generated notes are saved as **drafts** behind a verification gate; Sign/Reject
  are role-gated.
- **Admit to Vigil** from a structured note (QuickAdmit).
- Audio upload allowlist (webm/wav/mp3/m4a/ogg/flac); transcription offloaded with
  `asyncio.to_thread`.

**Verified live:** a typed CAP case generated a proper SOAP note (all four
sections), saved as a draft.

---

## 7. Vigil

- **Lab Trends** now parses the **real** seeded `lab_result` clinical documents
  (was 100% synthetic `Math.sin`).
- Admit writes ward/bed/diagnosis/attending/admission_date and computes an initial
  NEWS2 risk score.
- Discharge QR fetched as an authenticated blob (was 401); date guards against
  `Invalid Date`; dead state removed.

---

## 8. Reliability & security fixes

- **Startup crash fix:** the `messages` CHECK constraint (base schema + the
  `ambient_action` migration's table-copy) omitted `surgical_alert`. Once the
  agentic surgery flow wrote one, the **next boot's migration hit
  `CHECK constraint failed` and the backend refused to start.** Added
  `surgical_alert` everywhere the constraint is declared.
- Agentic dispatch: chart/insert steps are best-effort so a missing patient/actor
  can't crash a whole handoff.
- Test isolation: `test_handoff` teardown clears `surgery_team_assignments` before
  `surgeries` (the new targeted-alert child rows were tripping a FK on
  `DELETE FROM surgeries` under suite pollution).
- IDOR / membership guards and idempotency writeback on ambient-action execution;
  membership guard on alert resolution.
- Patient context fenced as DATA-ONLY with a sanitizer in the Sage bridge.
- Global `JSONDecodeError → 400` handler; loud warmup error when the LLM model is
  missing.
- Seed: surgeon accounts (`DOC004` / `dr_rathore`) standardized to the same
  `password` as every other demo account (was `password123`).

---

## 9. Known limitations

- **4B extraction is uneven.** On the 8GB Mac Mini, MedGemma-4B reliably extracts
  **medication** actions but frequently **misses surgery transfers** (classifying
  them as discussion rather than `possible_action`). The *execution* path is
  deterministic and fully working; the detection gate is the model. The
  fine-tuned 27B model (and/or a tuned ambient prompt) is the real fix.
- Neural checkpoints (Vigil CfC, Forecast LSTM) are **not trained**, predictions
  are flagged `model_trained=false`. Requires credentialed data + a training run.

---

## 10. Running & testing

```bash
# Backend (note the model override: the config default medgemma:27b 404s on the Mini)
cd server
ORB_DEPLOYMENT_PROFILE=dev ORB_JWT_SECRET=<secret> ORB_SKIP_KNOWLEDGE_BASE=1 \
  ORB_LLM_MODEL="alibayram/medgemma:4b" \
  .venv/bin/python -m uvicorn main:socket_app --host 127.0.0.1 --port 8000

# Reseed (idempotent demo data; run once with the backend up so Helix tables exist)
ORB_DEPLOYMENT_PROFILE=dev .venv/bin/python scripts/reset_db_and_seed.py

# Tests
ORB_DEPLOYMENT_PROFILE=dev ORB_JWT_SECRET=x ORB_SKIP_KNOWLEDGE_BASE=1 \
  .venv/bin/python -m pytest tests/ -q          # 411 passing

# Frontend
nvm use 22 && npm run dev                        # http://localhost:5173
npx tsc -p tsconfig.app.json --noEmit            # clean
```

**Demo logins** (all password `password`): `DOC001` (Medicine), `DOC004`
(General Surgery → sees the Surgical Suite), `NUR001` (nurse), `anesthetist_1`,
`PHA001` (pharmacist), `ADM001` (admin).

---

## 11. Commit summary (this session)

| Commit | Summary |
|---|---|
| Wire agentic execution | Sage card Confirm → med in Vigil + Helix; + audit batch |
| UI liquid-glass refresh | frosted surfaces, sheen, depth, gold accents |
| Agentic surgery | targeted alerts + surgery-card accuracy |
| Harden dispatch routing | verb routing, chart-on-noncatalog, test FK/contract fixes |
| Fix surgical_alert schema | startup-crash fix + surgeon resolution + login standardize |
| Dark Sage sidebar | light-theme → dark liquid-glass |
| Surgery allergies | red allergy pills / green NKDA + crash-safe render |

Verified throughout: **411 tests passing, `tsc` clean, 23 backend routers mounted in `main.py`.**
