# Real offline Sage browser timing

This opt-in harness owns a newly seeded temporary database and backend. It logs
in as the synthetic General Medicine doctor through the real browser, submits
general, patient-chart and department-overview questions, and captures each
answer. It never attaches to an existing Orb server, fulfills an API request,
downloads a model, or loads a live database or `.env` file.

Build the current frontend, install the repository's pinned Python ML runtime
and Playwright Chromium, and use already installed local weights:

```sh
node scripts/sage_browser_timing.cjs \
  --python=/absolute/path/to/owned/runtime/bin/python \
  --inference-url=http://127.0.0.1:11434 \
  --embedding-model=/absolute/path/to/installed/bge-m3/snapshot \
  --reranker-model=/absolute/path/to/installed/bge-reranker-v2-m3/snapshot \
  --warm-runs=3 \
  --journeys=true \
  --out=/tmp/orb-sage-timing-unique-run
```

The output directory must not exist. The default application port is `18120`;
`--port=18121` selects another unused loopback port. `--timeout-ms=180000` is the
answer-completion ceiling, not a passing latency target. Coordinate an idle
CPU/GPU window before running. The installed model is fixed to `medgemma:27b`;
thinking mode and the clinical safety floor stay on and model fallback is off.

Both local snapshot arguments are optional together. Omitting them explicitly
qualifies an empty offline Hugging Face cache where retrieval may degrade; that
run does **not** establish installed dense-retrieval performance. With snapshots,
the launcher initializes the real shipped synthetic corpus and vectors inside
its disposable database before serving. It uses the production isolated
knowledge worker. The snapshots are read as local model sources; all application
database, cache and export writes remain under the temporary checkout. No
`ORB_SKIP_KNOWLEDGE_BASE` shortcut is used.

The measured boundary starts in the capturing browser click handler, before
React handles submit. The first readable answer must be in the newest visible
answer bubble, contain at least 12 characters and two letter-or-number words,
and have visible ancestors. Progress labels, empty bubbles, punctuation and old
answers do not count. The DOM is sampled with `requestAnimationFrame`, so times
have approximately one display-frame precision. Completion is measured when the
previously readable answer remains rendered and the real UI leaves streaming.
Expanded details may scroll that answer outside the viewport; this does not
prevent completion measurement. A hidden/unmounted answer cannot complete.
A passing turn also
requires HTTP 200, explicit SSE `done:true`, a nonempty answer, no error/truncated
stream, the expected query scope and actual careful-model provenance. A patient
turn must use the verified seeded patient's chart.

The requested strict first-readable targets are general `<3000 ms`, patient
`<5000 ms`, and department `<10000 ms`. Full completion times and server
retrieval/first-content/total timings are recorded separately. The initial turn
is a residency diagnostic, excluded from the warm SLA. The harness never unloads
shared models to manufacture a cold run. Every later turn must observe a
resident careful model and meet its target; unsuccessful initial or later
answers still fail the run. Repetitions are all retained, with no best-of retry.

This is **idle synthetic qualification**. Background model warmups, narrator,
proactive assessment, ambient inference and chart prewarming are explicitly
disabled only in the disposable launcher. The result does not establish latency
during a busy ward or alert storm. Ordinary non-model maintenance remains enabled;
its work and any overlap with a query remain in the backend log. Model tags/digest/residency, runtime, hardware,
frontend bundle hash, corpus row counts, browser timing and screenshots are saved
in `timings.json`, `backend.log` and numbered PNG files. Non-loopback browser
HTTP requests are blocked and fail qualification. Local model/readiness metadata
uses bounded native loopback HTTP reads that ignore inherited proxy settings.
`--journeys=true` also enables
the thirteen local modules inside the temporary backend, then runs the existing
eleven real browser journeys after timing and saves their report/artifacts.

`--chart-prewarm=true` adds a separate patient measurement after all baseline
turns. It opens the verified patient's real Vigil chart, waits for the authorized
prefetch response, then submits the same patient question in Sage. This option
enables the existing chart prewarm feature only; automatic background model jobs
remain off. The report records chart-open-to-first-readable and
submit-to-first-readable times, the prefetch response, and the actual backend
model-prefix log with its prompt token/evaluation counters. A facts-cache hit
alone cannot qualify this additional measurement. The baseline observations are
retained and are never replaced by the chart case. To measure only that separate
workflow against a newly owned backend, additionally use
`--chart-prewarm-only=true`; the report then explicitly has no baseline turns.

Exit `0` requires all successful warm turns to meet the first-readable target,
no external request, browser error or page exception, graceful backend cleanup, and successful
optional journeys. Misses remain in the report and exit `1`.

Harness regressions use synthetic DOM/data to verify measurement integrity.
They are not evidence of model speed:

```sh
node --test scripts/sage_browser_timing.test.cjs
python -m pytest server/tests/test_e2e_isolation.py server/tests/test_e2e_process_cleanup.py -q
```
