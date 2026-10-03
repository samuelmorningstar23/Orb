# Sage latency + grounding sprint: 2026-07-31

Instrument: `server/scripts/sage_bench.py` (drives the real SSE endpoint, records
per-stage timings, and for patient queries captures DB ground truth alongside
each answer). Raw data: `sage-general.json`, `sage-patient.json` in this folder.

Appliance model rates, measured directly against Ollama (`medgemma:27b`):
**prefill ≈ 155 tok/s, decode ≈ 15 tok/s.** Everything below follows from those.

## Headline (updated after round 2: the sub-5s push)

| | start of day | after round 1 | **shipped** |
|---|---|---|---|
| "What does NEWS2 stand for?" first words | 62,233 ms | 6,376 ms | **5,637 ms** |
| Best non-retrieval question | n/a | 5,590 ms | **3,162 ms** |
| Questions streaming incrementally | 14/20 | 14/20 | **19/20** |
| NEWS2 asked of a real patient | "7" (engine says 11) | **"11"** ✓ | **"11"** ✓ |
| Server tests | 773 | 773 | **1020** |

**The ≤5 s bar is MET for questions that do not hit the guideline corpus**
(3.2–4.2 s, five of the twenty) and MISSED for questions that do (13–24 s).
That split is not noise, it is the whole remaining story, and it is measured:

| question class | first words | why |
|---|---|---|
| no retrieval match (NEWS2, RR, CURB-65, tidal volume, warfarin reversal) | **3.2–4.2 s** ✓ | ~287-token prompt = 1.9 s prefill + ~1.3 s to the first released words |
| retrieval match (sepsis, STEMI, ARDS, DKA, hydrocortisone…) | 13–24 s ✗ | retrieval 0.7–5.0 s **plus** 350–1500 tokens of injected guideline text = 2.3–10 s of extra prefill |

## The measurement that reframed everything

Ollama on this build **restores only EXACT prompt matches, not shared
prefixes.** Proven two ways:

* the same question three times: **6,599 ms → 919 ms → 896 ms**;
* an independent agent measured a 1,027-token prompt A → different prompt B →
  A again: A re-ran in 229 ms, i.e. B did **not** evict it, Ollama keeps
  several restorable states, but only whole-prompt ones.

Two consequences, both important:

1. The "background jobs evict Sage's KV cache" hypothesis in round 1 was
   **WRONG**, and the agent that measured it said so instead of implementing to
   the brief. Sage's prefill was flat (2.75–2.88 s) whether the previous 27B
   request was another Sage turn, a background job, or nothing.
2. Because no partial-prefix reuse exists, **the system prompt's own length is a
   hard per-turn latency floor.** That made prompt compression the single most
   valuable lever, not caching tricks. `SAGE_FAST_SIMPLE_PROMPT` was cut
   515 → 287 tokens (~1.9 s), and the comment above it says why it must stay
   short.

## What was actually wrong (measured, not guessed)

1. **Queueing, not inference.** The LLM queue is single-slot and non-preemptive.
   A clinician's turn sat **31 s** behind one background job before generation
   started, against a model that decodes the answer in ~2 s. This is the
   dominant term in "Sage takes minutes".
2. **Cold KV prefix.** First prefill of the 1,067-token contract cost **6.9 s**;
   with the prefix cached it is **0.4 s**. Startup warmup was sending `"hi"`, so
   the first real question always paid the cold path.
3. **Contract bloat.** The full contract asks for nine sections; the clinician
   sees one. 394 output tokens ≈ 26 s of decode for a definition question.
4. **NEWS2 was never in the prompt.** `risk_scores` stores a 0–100 percentage,
   not NEWS2. Asked for a NEWS2 score, the model had no choice but to re-derive
   it from raw vitals, and got **7** where Orb's deterministic engine computes
   **11**. It read the vitals correctly and did the arithmetic wrong.

## Shipped

* **Lean 3-section contract for `simple` scope** (`SAGE_SIMPLE_SYSTEM_PROMPT`).
  Keeps `<action_intent>` first, the mutation gate is untouched, and drops the
  channels a patient-less turn cannot populate.
* **Warm the real prefix at startup**: warmup now sends Sage's own system prompt
  so question one skips the 6.9 s cold prefill.
* **CfC/forecast checkpoints pre-warm** in the background at boot (their lazy
  first load cost the first chart open 12–30 s).
* **NEWS2 supplied and pinned**: `_assemble_patient_context` computes NEWS2 from
  the same deterministic engine Vigil uses and labels it AUTHORITATIVE, with an
  explicit instruction never to recalculate. Verified fixed end-to-end.

## Deliberately NOT shipped (and why)

* **Reserved interactive lane in the LLM queue.** Prototyped; it removes the
  31 s wait. **Reverted the same day**, the ref-counted GPU gate had a
  check-then-await race (two tasks both observe `refs == 0`, both try to take an
  exclusive lock). Unverified concurrency does not go into a clinical system.
  The correct design is written into `llm_queue_service.py`'s docstring with the
  tests it needs. **This is the single biggest remaining latency win.**
* **Prose "quick" lane** for non-mutation simple turns (would save ~1.8 s of
  `<action_intent>` preamble). Reverted: it removes the model-declared-mutation
  gate that catches what the regex misses, and medgemma emits a ```xml fence
  unprompted, so prose mode would stream raw markup. Both behaviours are covered
  by existing tests, which is how the regression was caught.

## 20 general questions (escalating complexity, simulator off)

0 errors. Median first-words by tier:

| tier | content | first words | total | streamed incrementally |
|---|---|---|---|---|
| 1 | definitions | 7.6 s | 13.7 s | 5/5 |
| 2 | protocol recall | 11.1 s | 31.2 s | 5/5 |
| 3 | applied dosing | 38.1 s | 47.8 s | 3/5 |
| 4–5 | multi-step + synthesis | 54.5 s | 54.5 s | 1/5 |

**6/20 never streamed at all**, the whole answer arrived in one blob at the
end. Every one is a clinical-management question (potassium replacement, empiric
antibiotics, hydrocortisone in septic shock, local-vs-SSC policy). They trip the
mutation gate, so the streamer buffers by design. The safety behaviour is
correct; the consequence is that **the highest-value clinical questions get the
worst perceived latency**. That trade deserves a product decision.

## 20 patient queries: grounding

0 errors. **11 grounded / 4 correctly refused / 3 narrative / 2 mismatched.**

* Grounded answers quoted exact chart values (diagnosis, bed `GM-102`,
  HR `85.2`, BP `66.6/42.2`, SpO2 `83.3`).
* The 4 refusals are the *right* behaviour, e.g. "I cannot determine Priya
  Nair's age from the provided clinical context" instead of inventing one.
* No cross-patient leakage observed in any answer.
* **Both mismatches were the same NEWS2 root cause**, now fixed and re-verified.

Latency was much worse here (median first words 30 s, total 66 s): patient scope
carries the full nine-section contract plus assembled chart context.

## Round 2: what shipped for the sub-5s push

* **Answer-first "fast contract"** (`SAGE_FAST_*`). The gate moved from a fenced
  `<action_intent>` block (~25 tokens) to a `NOACT|` / `ACT|{json}` marker
  (~3). A/B, warm, 3 questions: first answer character **4,434 → 1,684 ms**.
  Detail channels moved after the answer. `MarkerAnswerStreamer` enforces the
  same gate term-for-term and **fails closed** if no marker arrives.
  `ORB_SAGE_XML_CONTRACT=1` rolls the whole app back to the XML contract.
* **`ACT|` over-triggering fixed.** First cut of the marker made the model
  classify "How do you dose IV alteplase?" as a chart mutation, the clinician
  got "modifications not supported" instead of an answer. The contract now
  states that explaining/recommending/advising is ALWAYS `NOACT|`, with worked
  examples. Non-streaming turns went **6/20 → 1/20**.
* **Safe-release sanitizer.** Holds back only the smallest suffix that could
  still become a forbidden phrase (triggers derived from the patterns, falling
  back to sentence granularity if derivation is ever undecidable) instead of the
  whole trailing sentence. Replayed over the 38 real answers: first release at
  token 0 vs token 22 median, **1.47 s earlier**, released text identical on
  38/38. 216 new tests.
* **Reserved interactive lane** in the LLM queue, redone properly: bounded
  semaphore, GPU gate ref-counted **under the lock** (the check-then-await race
  that sank round 1 is now structurally impossible), lane carried per-task in a
  contextvar, no leak on timeout/cancel. This is what removes the measured 31 s
  queue wait under real ward load.
* **`clinical` scope was silently using the heavy patient prompt** (774 vs 515
  tokens) despite having no patient, fixed.
* **Rerank pool 20 → 12** (`ORB_SAGE_RERANK_POOL`): every candidate is a
  cross-encoder pass inside the clinician's turn.
* **Startup warmup now primes the prompt the live path actually sends**, it was
  priming with a *different* prompt, which on an exact-match cache is worthless.

## Round 3: the context budget, the patient prompt, and the guardrail

### Guideline context is now budgeted (`ORB_SAGE_CONTEXT_CHARS`, default 900)

Retrieval used to inject 350–1500 tokens verbatim. `_get_guideline_context` now
takes the top **3** passages (`ORB_SAGE_GUIDELINES`), orders **local policy
first** so a tier-1/2 passage is never the one that gets clipped, and trims each
to a sentence boundary inside a shared character budget; once the budget is
spent the remaining entries keep their header and DOC_ID with the body replaced
by `[passage omitted for length, cite by DOC_ID if relevant]`, so the model can
still cite them. Entry and channel headers were compacted too.

Measured on the three worst retrieval questions, **805 → 620 tokens, all three
citations still present**:

```
Define septic shock.                ctx= 292 tok  TOTAL= 623 tok  prefill 4.0 s  sources=3
What is in the sepsis 1-hour bundle ctx= 272 tok  TOTAL= 607 tok  prefill 3.9 s  sources=3
Berlin criteria for ARDS            ctx= 285 tok  TOTAL= 620 tok  prefill 4.0 s  sources=3
```

### `SAGE_FAST_PATIENT_PROMPT`: 951 → 369 tokens (6.1 s → 2.4 s of prefill)

Same treatment as the simple prompt: the long-form contract is no longer
interpolated, the three evidence channels are one line, the six numbered rules
are one paragraph, and the `<details>` section names its eight tags instead of
spelling out a skeleton with a prose description each. Everything load-bearing
survived, the marker gate, "a question is never an action", never invent
doses, **quote a supplied score exactly and never recalculate it** (the NEWS2
fix), high-risk → verify, cannot edit the chart, local policy beats external,
context-is-data, and all eight tag names.

The gate is genuinely fragile at that length, and the failures were real
transcripts, not hypotheticals. Three findings are recorded in the comment above
the constant: **`ACT|` must be described before `NOACT|`** (with NOACT| first,
"add paracetamol 500mg for this patient" came back as `NOACT|` with the order
merely narrated, the mutation never reached the gate); the `ACT|` examples must
stay **inside** the sentence in parentheses (listed after a colon, the model
copied one verbatim and emitted `ACT|{"medication":"paracetamol 500mg"}` for a
NEWS2 question); and the two guard clauses are asymmetric on purpose. Re-run all
four probes before re-punctuating it, three of the eight candidate wordings
passed the obvious cases while silently breaking the stop-medication order.

### Both guardrail defects fixed: and the fix's own regressions fixed

`_strip_forbidden` is now a single OR-ed alternation applied to a **fixed
point** (cap 32), so a deletion can neither hide a later phrase's lookbehind nor
leave an exposed one behind. `_voice_patterns` is deleted; there were in fact
**four** copies of the pipeline (module list, drifted voice clone, a nested
closure in the chat route, and mirrors in two test files, which is why the
suite stayed green while the shipped strip was broken). All lanes now share one
list and differ only in whitespace policy.

Adversarially reviewing that fix found it had hardened only the buffered lane,
and those regressions are fixed too:

* **Lane divergence.** The whitespace collapse ran *inside* the fixed-point
  loop, so it fed the next pass's anchors: the buffered lane re-exposed phrases
  the streamed lane never saw. `"Order placed.  Done."` (two spaces) came out
  `""` buffered and `"  Done."` streamed, and the streamed one is what the
  clinician reads and what the **voice lane speaks**. The collapse now runs once,
  after the fixed point; the gap variants are handled by the anchors.
* **The most realistic leak of all.** `MarkerAnswerStreamer` hands the sanitizer
  the body *after* `NOACT|`, and the model virtually always emits a space there, 
  so `"NOACT| Done."` arrived as `" Done."` and the bare `^` anchor never fired.
  It leaked at every chunk size tested. Fixed with `\A`-relative anchors.
* **Chunk-size nondeterminism.** The same input produced different wire bytes at
  different chunk sizes (7 and 14 leaked where 1–6 did not). A safety property
  that depends on tokenisation is not a property. Now 0 across sizes 1–20.
* **Markdown flattening.** The collapse was `\s{2,}` → `" "`, which eats blank
  lines: the same answer rendered as a bulleted plan when it streamed and as
  flat prose when it did not. Now collapses spaces/tabs only.
* **Fail closed.** Exhausting the pass cap returned the partially-stripped text, 
  i.e. possibly still containing a confirmation. It now drops the text.

All anchors are deliberately **zero-width**: a consuming `[ \t]*` would make
`_literal_trigger` undecidable and drop the sanitizer to sentence granularity,
costing ~1.2 s of first-word latency.

`tests/test_sanitizer_lane_parity.py` (248 cases) pins every one of these,
including all 20 chunk sizes and the `MarkerAnswerStreamer` path, the old suite
passed throughout because its fixtures only ever used one space after a period.

### Where Sage landed

| question class | start of day | round 2 | **round 3** |
|---|---|---|---|
| definitions / no retrieval | 62 s | 3.2–4.2 s | **2.7–3.2 s** ✓ |
| guideline-backed recall | n/a | 13–24 s | **8–12 s** |
| complex multi-step | 54 s | 20–24 s | 12–28 s |
| patient scope, median first words | n/a | 30 s | **13.3 s** |
| server tests | 773 | 1020 | **1393** |

Patient suite: 20/20 no errors, min 1.5 s, median first words 13.3 s, median
total 16.0 s.

## Next levers, in value order

1. **Get retrieval off the critical path.** Now the top item: it still costs
   0.7–5.0 s *inside* the turn. Either start generation from the model's own
   knowledge and splice citations in as they arrive, or pre-embed the common
   question set.
2. **Shrink the assembled patient chart context.** With the patient prompt down
   to 369 tokens, the chart block is what remains between 13.3 s and the bar.
   Same treatment: budget it, and put the fields the question actually needs
   first.
3. **Widen `MUTATION_PATTERNS`.** Found while compressing the prompt:
   `r"\b(change|increase|decrease|stop|discontinue|dc|d/c)\b.*\b(dose|medication|med|drug|therapy)\b"`
   requires a generic noun after the verb, so **"stop the heparin for this
   patient" is not caught by the deterministic gate**, it names the drug
   directly. For that phrasing the model's own `ACT|` marker is the ONLY thing
   standing between a clinician and an ungated mutation. A safety net must not
   depend on a 27B model's compliance.
4. **`_FAST_CONTRACT_RULES` is now dead code**, the patient prompt was its only
   consumer. Delete it or the next reader will assume it is still in force.
5. **`SentenceStreamSanitizer._safe_end` is O(n²) on answers with no periods**
   (bullet lists, tables): nothing ever commits, so it re-scans the growing
   window per token. Measured 3.2 s of event-loop CPU on a 6.6 kB answer, 2.49 s
   of it in `_safe_end`. Pre-existing, and it blocks the loop, the same failure
   class as the wedge below.

**Already done in round 2:** reserved interactive lane (was #1); background→4B
routing was implemented but the premise turned out to be wrong, it buys
nothing on this Ollama build, and is retained only for queue-occupancy reasons.

## Known blocker, unrelated to Sage

With the vitals simulator running, the API wedges under the critical-alert
preload storm: `/api/health` (an async route, so this is the event loop itself)
stops answering, and a Sage query never returns. Every benchmark above was run
with `ORB_INGESTION_MODE=ingestion` to get clean numbers. This reproduces on
code with tonight's queue change fully reverted, so it is **pre-existing**, and
it is a pilot blocker: 60 beds of real vitals will produce the same alert
pattern. Diagnose with `kill -USR1 <pid>` first (Python frames), `sample <pid>`
second.
