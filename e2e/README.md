# Orb e2e suite — Playwright golden journeys (A6)

Five "golden journey" end-to-end specs that drive the real Orb clinician shell
(React + Vite) against the real FastAPI + Socket.IO backend, plus a concurrency
load test.

## The five journeys

| Spec | Journey | Role |
|------|---------|------|
| `01-login-and-vigil.spec.ts` | Log in, land on the shell, see the Vigil/Patients panel render with ≥1 patient | doctor |
| `02-scribe-note.spec.ts` | Open Scribe, exercise the compose/record + note-structure governance UI (no real audio/LLM) | doctor |
| `03-orders-cpoe.spec.ts` | Open Orders, create an order with a valid LOINC code, see it in the list with a status chip | doctor |
| `04-pharmacy-verify.spec.ts` | Log in as pharmacist, open the Verify Queue, assert it renders (empty state OK) | pharmacist |
| `05-admin-flight-recorder.spec.ts` | Log in as admin, open Admin → Flight Recorder, assert timeline + chain-integrity badge | admin |

## Prerequisites

1. **Backend seeded + running on :8000.** From the repo root:
   ```bash
   # one-time (or to reset): seed 13 demo users + 30 patients into server/orb.db
   cd server && . .venv-dev/bin/activate && \
     ORB_DEPLOYMENT_PROFILE=dev ORB_INGESTION_MODE=simulation \
     python scripts/seed_clinical_data.py

   # run the backend (ASGI app object is `socket_app` in server/main.py)
   ORB_DEPLOYMENT_PROFILE=dev ORB_INGESTION_MODE=simulation \
     python -m uvicorn main:socket_app --port 8000
   ```
   The backend seeds the curated terminology subset (LOINC/SNOMED/ICD-10) on
   startup, which spec 03 validates against.

2. **Frontend dev server on :5173.** From the repo root:
   ```bash
   npm run dev
   ```
   Vite proxies `/api` and `/socket.io` to the backend on :8000.

3. **Chromium.** This repo's sandbox ships a pre-installed Chromium at
   `/opt/pw-browsers/chromium`; `playwright.config.ts` pins `executablePath` to
   it automatically when `PLAYWRIGHT_BROWSERS_PATH` is set. On a normal machine,
   run `npx playwright install chromium` once and unset `ORB_CHROMIUM_PATH`.
   **Do not run `playwright install` in the sandbox — downloads are blocked.**

## Running

```bash
# Boots BOTH servers automatically (webServer in the config) and reuses any that
# are already listening:
npx playwright test

# If you have already started the backend + vite yourself, skip Playwright's
# webServer management (avoids any double-boot ambiguity):
ORB_E2E_NO_WEBSERVER=1 npx playwright test

# A single journey:
ORB_E2E_NO_WEBSERVER=1 npx playwright test 01-login-and-vigil.spec.ts
```

Suggested `package.json` scripts (add manually — not added for you):
```json
"test:e2e": "playwright test",
"test:e2e:manual": "ORB_E2E_NO_WEBSERVER=1 playwright test",
"test:e2e:report": "playwright show-report"
```

## Traces, screenshots, reports

- **Traces** are retained on failure at `test-results/<spec>/trace.zip`.
  Open one with:
  ```bash
  npx playwright show-trace test-results/<spec>/trace.zip
  ```
- **Screenshots** on failure land alongside the trace (`test-failed-1.png`).
- **HTML report** is written to `playwright-report/`:
  ```bash
  npx playwright show-report
  ```

## Login credentials used

All demo staff accounts use the password **`password`**. They are dev-only
quick-access accounts defined in `server/scripts/seed_clinical_data.py` and are
gated behind the DEV build flag in the UI.

| Role | Employee ID | Password | Name |
|------|-------------|----------|------|
| doctor | `DOC001` | `password` | Dr. Meera Sharma |
| nurse | `NUR001` | `password` | Priya Reddy |
| pharmacist | `PHA001` | `password` | Deepa Joshi |
| admin | `ADM001` | `password` | Rajesh Kumar |

Verify the backend accepts them:
```bash
curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"employee_id":"DOC001","password":"password"}'
```

The `login()` helper (`e2e/helpers.ts`) watches the `/api/auth/login` response
and throws a clear "Login failed for … HTTP <status>" error if credentials are
rejected, so a bad seed fails fast instead of hanging.

## Load test

See `scripts/loadtest_sage.py` (run from the repo root with the dev venv active):
```bash
. server/.venv-dev/bin/activate
python scripts/loadtest_sage.py --target login   # server/ASGI concurrency ceiling
python scripts/loadtest_sage.py --target read    # cheap read endpoint
python scripts/loadtest_sage.py --target sage    # LLM path (needs a GPU + Ollama)
```
It ramps concurrency (1,2,4,8,16,32,…) and prints p50/p95 latency + throughput.
The Sage/LLM number is intentionally single-slot (`llm_queue.max_concurrent = 1`
behind the GPU admission lock) — that ceiling requires a GPU host to measure.
