# Single-slot full SWA: partial performance acceptance

This intermediate run retains every real answer, miss, screenshot and runtime log. All 11 browser journeys and all 11 LLM turn completions passed. Seven of nine warm speed checks passed; general questions after department answers missed the strict 3s target. Initial cold first readable was 20.141s and remains a recorded miss. This is not full latency acceptance.

| Category | First readable seconds, in observation order | Result |
| --- | --- | --- |
| Initial cold general |20.141|Miss retained|
| Warm general |1.452,4.906,4.878|1/3 pass|
| Warm patient |3.987,3.981,3.990|3/3 pass|
| Warm department |5.228,0.315,0.349|3/3 pass|
| Separate chart-prepared patient |3.810|Pass; chart-open to readable 5.310s|

`timings.json` is the original unmodified harness output. `summary.json` adds manual protected-posture verification, authoritative prompt cache counters and source acceptance references. `runtime/` contains only final daemon logs/metadata/preflight/cleanup; no temporary HOME, private keys, database or weights. The daemon and owned backend/browser exited cleanly; shared service remained unchanged and all 33 model cache files were unchanged.

Actual runtime allocation: one live 8192-token slot, f16 KV, 3328MiB full SWA plus 640MiB non-SWA. G2/G3 selected saved patient prompts but the runner explicitly forced reprocessing: zero cached 697 prompt tokens,4175/4182ms prefill. No further inference or source change was made for this evidence.

Selected visually inspected screenshots: `04-general.png`, `08-patient.png`, `09-department.png`, `prewarmed-patient-chart.png`. All patients and records are synthetic.
