# Orb capacity acceptance runner

`scripts/loadtest_capacity.py` separates three kinds of evidence. A passing fake
model run is **not** an AI capacity claim and does not qualify a 100-bed hospital.

| Mode | What actually runs | What the result establishes |
|---|---|---|
| `--synthetic` | Small disposable TCP/SSE fixture, no Orb or model | Runner, transport, stream parsing and burst construction |
| `--orb-stub` | Real copied Orb app, migrations, temporary 100-patient DB, production JWT checks/role scopes, real routes/retrieval/queue, fake Ollama HTTP | Application/control-plane capacity with fake compute |
| `--orb-model-origin` | Same disposable Orb app, existing explicitly selected loopback inference daemon | Measured local model + application performance for the exact bounded workload |

No database, uploads, model weights, keys, appliance site settings or runtime logs
are copied. The launcher reuses the allowlisted application-source copier and
isolated/offline environment in `scripts/e2e_server.py`. All child processes and
temporary files belong to the run and are cleaned up. Installed local-model mode
never downloads, changes, stops or starts the selected daemon.

The default disposable fixture uses **unencrypted synthetic SQLite**, because
appliance encryption credentials are deliberately not copied. Its timings do not
qualify SQLCipher, encrypted uploads, TLS, clinical deployment configuration or
production security. Deployment acceptance must repeat the workload with those
actual controls enabled and verify encryption on the live test connection.

## Run the transport check

From the repository root, with the existing server Python environment:

```sh
server/.venv/bin/python -m pytest tests/test_loadtest_capacity.py -q
server/.venv/bin/python scripts/loadtest_capacity.py --synthetic --mode fresh-ai --requests 200 --concurrency 200 --output /tmp/orb-transport-200.json
```

The 200 tasks start behind one barrier. `fresh-ai` sends 67 general, 67 patient
and 66 department requests with unique query text and unique conversation IDs.
Written-word iteration markers avoid mistaking a benchmark nonce for a patient
name or bed locator. This bounded scripted mix is not a representative clinical
prompt corpus. Its first ten questions should be reviewed before qualifying a
deployment model. Clinical usefulness/correctness requires a separate evaluation.

## Run actual Orb without real inference

```sh
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --mode fresh-ai --requests 200 --concurrency 200 --output /tmp/orb-default-200.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --fixture-settings docs/perf/capacity-2026-10-21/mock-pool-settings.json --mode fresh-ai --requests 200 --concurrency 200 --output /tmp/orb-pool-200.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --prewarm-summaries --fixture-settings docs/perf/capacity-2026-10-21/mock-pool-settings.json --mode mixed --requests 200 --concurrency 200 --output /tmp/orb-mixed-200.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-stub --prewarm-summaries --mode summary --requests 200 --concurrency 200 --output /tmp/orb-summary-200.json
```

The mock-pool configuration's 200 slots are a **synthetic transport fixture
setting**, not a qualified capacity for any physical machine. Do not copy that
setting onto a hospital appliance.

Summary prewarming runs the real durable summary service with a synthetic
generator, then checks all 100 synthetic current admissions have ready snapshots.
Freshness is measured from `source_captured_at`, strictly less than 1800 seconds;
`generated_at` is reported separately and never resets the source snapshot's age.
An `outdated` as-of draft does not count as a current ready summary in this test.
The mixed workload is 40 general, 40 patient, 40 department, 40 cached-summary
reads and 40 authenticated roster reads. The summary-only run checks the cached
read path. Neither measures the real model cost of generating 200 summaries/hour.
The service's clock-injection tests cover invalidation and the 30-minute refresh
boundary without pretending a simulated clock is a 30-minute hardware soak.

## Run an explicitly bounded real-model qualification

Coordinate with the owner of the model daemon first. Other inference, model
loading, CPU load tests and battery/power changes contaminate these results.

```sh
server/.venv/bin/python scripts/loadtest_capacity.py --orb-model-origin http://127.0.0.1:11436 --allow-model-load --requests 1 --concurrency 1 --timeout 90 --output /tmp/orb-real-1.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-model-origin http://127.0.0.1:11436 --allow-model-load --requests 2 --concurrency 2 --timeout 90 --output /tmp/orb-real-2.json
server/.venv/bin/python scripts/loadtest_capacity.py --orb-model-origin http://127.0.0.1:11436 --allow-model-load --requests 4 --concurrency 4 --timeout 90 --output /tmp/orb-real-4.json
```

Run sequentially, not in parallel. The first run may include model loading. The
installed model must already expose the expected `medgemma:27b` alias. Real-model
mode has no background model workloads; therefore it cannot establish capacity
with simultaneous transcription, image analysis and monitoring. Empty offline
embedding caches may degrade guideline retrieval; inspect provenance and record
the installed embedding/reranker snapshots before a deployment qualification.

Default real-model limits are at most four concurrent requests and twelve total.
Higher limits require an explicit `--allow-heavy-model-load` opt-in on a dedicated
test host. The runner is loopback-only and does not accept remote hosts. A
non-owned disposable local app can be targeted only with a synthetic-data
manifest plus `--allow-nondisposable --ack-synthetic-data --allow-model-load`.
The flag's name acknowledges that the runner did not create that app; it does
not make a production app appropriate for a load test.

## Interpretation and acceptance

For general/patient/department requests, completed-answer limits are strictly
less than 3/5/15 seconds respectively. The report separately records first
content, first readable prose (at least 32 nonheading characters and four words),
successful terminal completion, full response time, queue wait, retrieval time,
model identity and output size. The readable threshold is an objective display
milestone, not a claim that an answer is already clinically usable. A whole-call
deadline bounds queueing, retrieval and a continuously active stream; an HTTP
read-gap timeout alone is insufficient.

Every expected request remains in the denominator, including HTTP 429/503,
timeouts, authentication failures and HTTP-200 SSE error events. A missing
terminal marker, malformed/truncated event, empty response, claimed answer-cache
hit or missing selected-model provenance cannot count as successful fresh AI.
Successful-only latency percentiles are explicitly labelled by sample count;
the all-request pass count is the acceptance decision. A rejected request that
returns quickly is still a failure.

Reports contain no clinical text, tokens, patient identifiers or error bodies.
They contain opaque run request IDs, counts, hashes, runtime/host metadata,
source hashes, role names and numeric timing. The host fingerprint describes the
generator/client and, in owned-copy modes, the colocated Orb child; it does not
verify the hardware behind an externally forwarded inference origin.

The bundled app fixture uses one real doctor account with General Medicine scope
and 100 synthetic admissions in that department. It proves requests authenticate
and use those permissions; it is not a 200-user, multiple-role or multi-facility
acceptance scenario. Separate authorisation tests cover scope isolation. A
deployment workload manifest must add the actual intended role/case/department
mix without weakening access checks or reusing administrative privileges.

Before sizing hardware, qualify the intended model/quantisation/context/output
length on candidate hardware; then test 200 fresh AI requests and the mixed
workload with real scheduled summary generation, ASR, image analysis, monitoring,
TLS/proxy/Wi-Fi delays and a worker failure. Repeat with realistic patient records
and scoped users, check safety/factuality and measure p95/p99 plus every failure.
Neither 200 connected sessions nor cached summary reads substitutes for 200
fresh simultaneous generations. No benchmark here proves clinical safety,
medical-device readiness or hospital availability.
