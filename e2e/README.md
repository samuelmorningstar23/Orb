# Browser integration tests

Eleven Playwright journeys exercise the real backend and production frontend:

| Spec | Coverage | Role |
|---|---|---|
| `01-login-and-vigil.spec.ts` | Authentication, shell and seeded patient list | doctor |
| `02-scribe-note.spec.ts` | Compose input and note-format controls, without inference or audio capture | doctor |
| `03-orders-cpoe.spec.ts` | Create a terminology-validated order and read its status | doctor |
| `04-pharmacy-verify.spec.ts` | Pharmacy verification queue | pharmacist |
| `05-admin-flight-recorder.spec.ts` | Audit timeline and chain-integrity display | admin |
| `06-offline-workflows.spec.ts` (six journeys) | All thirteen local screens; complaint resolution persistence; care intervention outcomes and stale revisions; signed radiology report and addendum; administrative access denial; saved report definitions and preview/CSV cohort agreement | doctor/admin |

## Setup and execution

Install frontend dependencies with `npm ci`, create `server/.venv-dev` with Python
3.12 and `server/requirements-dev.txt`, and provision Chromium with
`npx playwright install chromium`. CI installs the Linux browser system dependencies.
`ORB_E2E_PYTHON` can select another prepared Python interpreter; otherwise the
runner tries `.venv-dev`, then `.venv`.

```bash
npm run test:e2e
# Run one journey:
npm run test:e2e -- 01-login-and-vigil.spec.ts
```

The runner builds the frontend, then `scripts/e2e_server.py` copies application
source and bundled reference text into a temporary directory and seeds a new demo
database there. Uploads, production databases, private site settings, model weights,
and environment secrets are excluded. Watchdog, backup, FHIR export and model-cache
paths also live in that directory. The real ASGI lifespan and routes run. Models
have no copied assets or reachable inference service; these tests do not establish
inference quality, dictation quality, clinical safety or appliance performance.

The default origin is `http://127.0.0.1:18000`. Override it with
`ORB_E2E_BASE_URL=http://127.0.0.1:18001` if needed. Managed mode requires a loopback
HTTP origin and refuses to reuse an existing listener. The temporary database is
removed when the server exits. Seeding and serving run in owned child process
groups; bounded termination cleans those children before deleting their checkout.
**No manual reseed of `server/orb.db` is necessary.**

Managed Playwright starts the disposable server with `--offline-modules`. This
enables the thirteen local documentation/administration modules in that temporary
demo only. It does not change the installed appliance's default-off switches,
clinical rules, external integrations or site configuration.

For an explicitly managed, disposable test instance:

```bash
# Terminal one: build and start an owned disposable demo with local modules.
npm run build
server/.venv-dev/bin/python scripts/e2e_server.py --host 127.0.0.1 --port 18000 --offline-modules

# Terminal two: connect only to that synthetic test instance.
ORB_E2E_NO_WEBSERVER=1 ORB_E2E_BASE_URL=http://127.0.0.1:18000 npm run test:e2e
```

Manual mode can mutate the caller's selected instance; select synthetic data only.
`ORB_CHROMIUM_PATH` explicitly selects a custom browser. Otherwise Playwright uses
its managed cache, including the standard `PLAYWRIGHT_BROWSERS_PATH` setting.

## Results

Failure traces and screenshots are written under `test-results/`; the HTML report
is under `playwright-report/`. Open it with `npm run test:e2e:report`.
The successful offline journeys also attach full-page PNG evidence to the report;
the reporting journey saves its synthetic CSV in its test output directory and
compares every exported patient ID with the rendered preview. These artifacts
contain disposable demo data only. CI uploads the HTML report and its attachments.

Demo staff logins are `DOC001`, `NUR001`, `PHA001`, and `ADM001`, password
`password`. The helper submits the real login form and reports rejected credentials.
These accounts exist only in the disposable seed for this runner.

Concurrency/load testing is separate: `scripts/loadtest_sage.py --target login`
or `--target read` can measure a selected test appliance. The `sage` target needs
provisioned local models and must be measured on the intended hardware.
