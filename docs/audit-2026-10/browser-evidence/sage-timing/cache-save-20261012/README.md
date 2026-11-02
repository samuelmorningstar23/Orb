# Final cache profile: warm browser targets passed

All nine warm first-readable targets passed, all 11 real LLM turns completed successfully, and all 11 real browser journeys passed. Initial cold readable text took 19.982s and remains a recorded miss. Separate chart preparation took 23.255s before Send, with chart-open to readable 25.409s; that delay is not hidden inside the passing submit timer. This is idle synthetic qualification, not a universal or busy-ward SLA.

| Category | First readable seconds, in observation order | Result |
| --- | --- | --- |
| Initial cold general |19.982|Miss retained|
| Warm general |1.631,0.723,0.969|3/3 pass|
| Warm patient |3.545,3.532,3.416|3/3 pass|
| Warm department |5.377,0.432,0.429|3/3 pass|
| Separate chart-prepared patient |2.039|Submit target passed; chart-open 25.409s|

`timings.json` is the original unmodified harness output. `summary.json` records exact browser/server/cache counters, all 11 manual thinking/safety-floor/completion verifications, unchanged application source provenance and the explicitly tested operational delta. `runtime/` holds final safe logs, metadata, cleanup, launcher/config snapshots and independent parity evidence. No temporary HOME, private keys, DB or weights were copied. All 22 screenshots are listed and hashed in `evidence-manifest.json`.

The same questions were used with a fresh synthetic DB. The patient prompt was 1005 tokens (573 cached/432 evaluated), versus 1092 in the prior run. No paired patient latency claim is made. The general 697-token prompt after department now restored 696 cached tokens, with only one token evaluated.

Chart preparation waited behind actual Timeline generation: background lane held 21.854s, then the actual prefix warm took 470ms. The existing Timeline, FIFO/background priorities and clinical behavior were preserved.

Selected visually inspected screenshots: `04-general.png`, `08-patient.png`, `09-department.png`, `prewarmed-patient-chart.png`. Every patient and record is synthetic.
