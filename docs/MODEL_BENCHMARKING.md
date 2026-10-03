# Orb: MedGemma Benchmarking & Evaluation Guide

How to benchmark the MedGemma models Orb uses, what the numbers mean, and which other
evaluations are worth running. Harness: [`server/scripts/benchmark_medqa.py`](../server/scripts/benchmark_medqa.py).

> **Why this matters for Orb specifically.** Orb's entire pitch is "we don't ship unvalidated
> AI." The single most valuable fundraising/clinical artifact we can produce is an **honest,
> reproducible benchmark of our own models**, including where they're weak. This is the
> validation discipline the "Epic Sepsis Model failed external validation" narrative demands we
> hold ourselves to. Publish the method, the score, and the confidence interval, not a
> cherry-picked headline.

---

## TL;DR: which box runs which model

| Model | Fits in… | Where to run |
|---|---|---|
| **MedGemma 4B** (`medgemma-4b-it`, Q4 ≈ 2.5 GB) | ~6 GB RAM, CPU-OK | This repo's sandbox, any laptop, Kaggle CPU |
| **MedGemma 27B** (`medgemma:27b`, Q4 ≈ 16.5 GB) | ~18–20 GB RAM **or** ≥24 GB VRAM | **Kaggle T4×2 (free)**, Colab, or a GPU VM. *Not* a 16 GB-RAM box. |
| **MedGemma 1.5 4B** (`medgemma:1.5-4b`, Q4 ≈ 3.3 GB) | ~6 GB RAM | Same, but it is a **reasoning** model: budget 1,024+ tokens and CoT mode, never `--letter-only`. See §2b. |

The 27B **will not run on a 15–16 GB machine** (Q4 weights alone are ~16.5 GB, plus KV cache and
runtime overhead). That is why the flagship benchmark goes to Kaggle.

---

## 1. The harness

`benchmark_medqa.py` runs a model over a multiple-choice medical QA set and reports **accuracy
with a 95% Wilson confidence interval**, checkpointing every 10 questions (so a run resumes if
interrupted). Two backends, identical scoring:

- `--backend ollama` → `POST {base_url}/api/chat`, the *exact* call the Orb app uses. Point it
  at `medgemma:27b` on your Mac Studio or a Kaggle/Colab GPU running `ollama serve`.
- `--backend llamacpp` → loads a local `.gguf` via `llama-cpp-python` (CPU-friendly; used for the
  4B tier without a GPU).

Two prompting modes:

- **`--letter-only` (direct answer):** forces a single answer letter (llama.cpp uses a GBNF
  grammar `root ::= [A-E]`; Ollama uses a strict instruction + `num_predict=4`). Fast, trivially
  parseable, CPU-viable. **This is a lower bound**, no chain-of-thought.
- **default (chain-of-thought):** the model reasons, then emits `Answer: X`. Higher accuracy,
  ~10× slower and ~512 tokens/question. Use this on GPU for the headline number.

Datasets: `medqa` (USMLE 4-option), `medmcqa`, `pubmedqa`, or any local `--dataset jsonl` with
`{question, options:{A..}, answer_idx}`.

---

## 2. Result: MedGemma 4B on MedQA (run in Orb's sandbox, CPU)

| Model | Dataset | Mode | N | **Accuracy** | 95% CI | Unparsed |
|---|---|---|---|---|---|---|
| MedGemma-4B-it (Q4_K_M) | MedQA-USMLE test | direct-answer | 1,273 | **55.4%** (705/1273) | [52.6%, 58.1%] | 6 |

*Per-question results: [`docs/benchmarks/medqa_medgemma-4b-it-q4_letteronly.json`](./benchmarks/medqa_medgemma-4b-it-q4_letteronly.json).
Run on Orb's cloud sandbox (4 CPU cores, 15 GiB RAM, no GPU), ~0.18 q/s (~2 h wall).*

### Why 55.4% vs Google's official 64.4%

The official MedGemma-4B MedQA score is **64.4%** (4-option), per the
[MedGemma Technical Report](https://arxiv.org/abs/2507.05201) (Google Research / DeepMind).
Google's protocol is **zero-shot chain-of-thought at full precision (bf16)**. Ours was
**direct-answer at 4-bit**, deliberately, to run on a CPU sandbox. The ~9-point gap decomposes as:

| Factor | Our run | Official | Est. impact |
|---|---|---|---|
| **Reasoning** | direct single-letter answer | zero-shot **chain-of-thought** | **~6–8 pts** (biggest lever) |
| **Precision** | Q4_K_M (4-bit) | bf16 (full) | **~2–3 pts** (small models are quant-sensitive) |
| **Prompt/format** | our template | Google's exact template | ~0–1 pt (MCQA is format-sensitive) |
| **Answer parsing** | 6/1273 unparsed→wrong | n/a | ~0.5 pt |

Our scoring was integrity-checked: predictions spread across all four options and 55.4% ≫ 25%
chance, so this is a real direct-answer/4-bit result, **not a harness bug**, just a lower bound.
CoT + bf16 would recover most of the gap. The **27B is the model Orb ships**; benchmark it on
Kaggle with CoT (§3) for the number that belongs in the pitch. Also caveat public scores for
possible MedQA pre-training contamination, and weight an Orb-authored held-out set more heavily (§4B).

### Measured: the direct → concise-CoT tradeoff (4B, CPU)

To ground the "reasoning helps but costs time" claim with numbers rather than assertion, the same
40 MedQA questions were run in two modes. Raw data: [`benchmarks/proof_direct_vs_concise_cot_4b.json`](./benchmarks/proof_direct_vs_concise_cot_4b.json).

| Mode | Accuracy (n=40) | Time/q | Output tokens |
|---|---|---|---|
| direct (letter only) | 55.0% | 5.2 s | 1 |
| concise CoT (1–2 sentences) | 60.0% | 12.4 s | 59 |

**What's solid:** concise CoT emits ~59 tokens vs 1 and costs ~2.4× the time, and vs *unbounded*
CoT (~320 tokens, ~44 s/q, which **truncated before its answer and scored 0/3**), concise is
**~3.6× faster and more reliable**. So bounding the reasoning is the practical way to "keep CoT but
make it fast."
**What's noisy:** the +5-point accuracy lift is only 2/40 questions (95% CI ≈ ±15%), directional,
not precise, and modest on the small 4B. On the 27B the CoT lever is far larger (72.4% direct →
~85% expected with CoT). *Mechanism transfers; the absolute 4B number does not imply an 85% 4B.*

**Methodology:** MedGemma-4B-it (unsloth Q4_K_M GGUF), MedQA-USMLE 4-option **test** split
(1,273 questions), `--letter-only` direct-answer, temperature 0, single-letter GBNF grammar,
4 CPU threads. Reproduce:

```bash
pip install llama-cpp-python huggingface_hub pyarrow
# model (ungated community GGUF):
huggingface-cli download unsloth/medgemma-4b-it-GGUF medgemma-4b-it-Q4_K_M.gguf --local-dir .
# dataset → normalize to jsonl {question, options, answer_idx} (see repo scratch or use --dataset medqa on a parquet)
python server/scripts/benchmark_medqa.py --backend llamacpp \
  --model ./medgemma-4b-it-Q4_K_M.gguf --dataset jsonl --data medqa_test.jsonl \
  --letter-only --threads 4
```

**Context:** Google's MedGemma technical report cites ~**64%** for 4B and ~**87–89%** for 27B on
MedQA, but those use their best CoT prompting and full-precision weights. Expect our Q4 +
direct-answer number to sit **below** the paper's CoT figure, that gap is prompting/quantization,
not a broken model. Run CoT on GPU to close it.

---

## 2b. MedGemma 1.5 4B: why it is not benchmarkable in letter-only mode (2026-09-07)

MedGemma 1.5 was released on 13 January 2026 and exists **only at 4B**, so it is a
candidate for Orb's cheap tier and never for the 27B. Pulled here as `medgemma:1.5-4b`
from `hf.co/unsloth/medgemma-1.5-4b-it-GGUF:Q4_K_M`, since the official Ollama library
carries no 1.5 tag. It verifies as a real multimodal build (gemma3, 3.88B + 419M CLIP
projector, Q4_K_M, 128K, `vision` capability).

Both models were run through this harness on the same 1,273-question MedQA-USMLE test set
(`GBaker/MedQA-USMLE-4-options`, `phrases_no_exclude_test.jsonl`), `--backend ollama
--letter-only --temperature 0`, on an M2 Max.

| Model | N | Accuracy | 95% CI | Unparsed |
|---|---|---|---|---|
| `medgemma:4b` (MedGemma 1) | 1,273 | **53.73%** (684/1273) | [51.0%, 56.5%] | 2 |
| `medgemma:1.5-4b` | 1,260 | **not measurable** (13/1260) | n/a | **1,238** |

The v1 number reproduces the earlier 55.38% llama.cpp result (§2) inside its confidence
interval, the ~1.6-point gap being backend, GBNF grammar there versus a strict instruction
plus `num_predict=4` here. That agreement is what says the harness is sound, which in turn
is what makes the 1.5 row a finding rather than a bug.

**1.5 is a reasoning model and ignores format instructions.** Given a system prompt reading
"Respond with ONLY the single capital letter, output nothing else, no words, no
punctuation, no explanation" and a 4-token budget, it begins `thought\nThe user wants me
to...` and is cut off mid-reasoning, so 98% of its answers do not parse. Re-run with 1,024
tokens on the same vignettes, it spent **373 to 957 output tokens and 4.7 to 16.4 seconds**
where v1 spent **3 tokens and under 2 seconds**. Google's card flags this indirectly,
MedGemma "may make it more sensitive to the specific prompt used than Gemma 3".

Consequences for how to benchmark it, if someone wants the number:

- Letter-only mode is invalid for 1.5. Use CoT mode (drop `--letter-only`) with
  `--num-predict 1024` or higher, and budget roughly 10 to 20 s/question, so about 4 to 7
  hours for the full set rather than the ~8 minutes v1 takes.
- Any Orb-side eval that pins a strict output shape needs its parser widened first. `**B**`
  (markdown bold, which 1.5 emits on short questions) already defeats `parse_answer`.

Raw results: [`medqa_medgemma-1-4b_ollama_letteronly_2026-09-07.json`](./benchmarks/medqa_medgemma-1-4b_ollama_letteronly_2026-09-07.json)
and the unparsed 1.5 run, kept as evidence, in
[`medqa_medgemma-1.5-4b_ollama_letteronly_2026-09-07_UNPARSED.json`](./benchmarks/medqa_medgemma-1.5-4b_ollama_letteronly_2026-09-07_UNPARSED.json).

### The operational finding that actually decides the tier

Accuracy was never going to be the deciding number here. `dev_tools/check_fast_model_contract.py`
run against both tiers on Orb's own Sage prompts:

| Model | Fast-answer marker present | Median response |
|---|---|---|
| `medgemma:4b` | 0 of 9 | 439 chars |
| `medgemma:1.5-4b` | 3 of 9 | 1,640 chars |
| `medgemma:27b` | 9 of 9, at char 0 | ~870 chars |

The fast lane exists to answer quickly. Because `MarkerAnswerStreamer` is fail-closed, a
model that does not emit `NOACT|` or `ACT|` up front streams nothing and its
time-to-first-word equals its time-to-last-token. Both 4B tiers fail that, which is a
pre-existing problem worth its own fix, but 1.5 makes the withheld lump 3.7x bigger. So 1.5
is worse on the single axis the cheap tier exists for, independent of how it scores.

**The fix landed the same day, and it does not change this verdict.** Orb now emits the
gate marker itself for any model that was not measured to emit one, so the fast lane
streams instead of lumping; the marker counts above are unchanged, because they measure the
model and the fix removes the dependence on it rather than the behaviour. See
[`perf/FAST_LANE_MARKER_2026-09-07.md`](./perf/FAST_LANE_MARKER_2026-09-07.md) for the
before and after.

One consequence is worth carrying forward before anyone re-opens the 1.5 question. While
the gate stayed shut, a reasoning model's opening `thought` block was withheld along with
everything else. With the gate open, the first thing the model writes is the first thing
the clinician reads, and for 1.5 that is a `thought` block 9 times out of 9. So the cheap
tier is now a lane a reasoning model cannot be dropped into at all without a thought-block
cut in the streamer, which is a larger change than swapping a model name.

Where 1.5 is worth a run instead: the lanes where Google's own numbers put it **ahead of
the 27B** and where a 5 to 15 second think costs nothing, lab-report PDF to structured JSON
(91 vs 76 macro F1), EHR question answering (89.6 vs 90.5), plus capabilities v1 lacks
entirely, 3D CT and MRI, whole-slide histopathology, longitudinal CXR comparison and
anatomical bounding boxes (38.0 vs 16.0 IoU). Those are batch extraction and imaging evals,
see §4C. Bridge is **not** one of them, it is patient-facing chat and the 27B already wins
there (EHRQA 90.5 vs 89.6). The extraction win has no lane in Orb today.

---

## 3. Benchmarking the 27B on Kaggle (free): step by step

Kaggle gives **30 GPU-hours/week free**, and the **T4×2** accelerator = 2×16 GB = **32 GB VRAM**,
which holds the 27B Q4 comfortably.

1. **Account:** sign in at kaggle.com → **Notebooks → New Notebook**. In the right sidebar:
   **Settings → Accelerator → GPU T4 ×2**, and **Internet → On** (needed to pull the model).
   (One-time: Kaggle asks you to verify a phone number to enable GPU + internet.)

2. **Install Ollama + serve** (first cell):
   ```python
   !curl -fsSL https://ollama.com/install.sh | sh
   import subprocess, time, os
   os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
   subprocess.Popen(["ollama", "serve"])
   time.sleep(8)
   !ollama pull medgemma:27b      # ~16 GB Q4 download
   ```
   *(If `ollama pull medgemma:27b` 404s, pull a GGUF instead:
   `huggingface-cli download unsloth/medgemma-27b-it-GGUF medgemma-27b-it-Q4_K_M.gguf` and create
   a one-line Modelfile `FROM ./medgemma-27b-it-Q4_K_M.gguf` → `ollama create medgemma:27b -f Modelfile`.)*

3. **Get the harness + dataset** (second cell):
   ```python
   !pip -q install httpx pyarrow
   # upload benchmark_medqa.py as a Kaggle dataset, or paste it into a cell / clone your repo
   !curl -sSL -o medqa_test.parquet \
     https://huggingface.co/datasets/openlifescienceai/medqa/resolve/main/data/test-00000-of-00001.parquet
   ```
   Normalize the parquet to `medqa_test.jsonl` (the `data` column holds
   `{Question, Options, Correct Option}`), or extend the loader for that schema.

4. **Run** (third cell), CoT for the headline number, GPU makes it fast:
   ```python
   !python benchmark_medqa.py --backend ollama --model medgemma:27b \
       --dataset jsonl --data medqa_test.jsonl
   ```
   Full 1,273-question CoT run on T4×2 is roughly **30–60 min**. For a quick check add
   `--limit 200` (95% CI ≈ ±7%).

5. **Save the result:** download `results/*.json` from the notebook's output pane, or write it to
   `/kaggle/working/` (persisted with the notebook version).

**Free alternatives if Kaggle doesn't fit:**
| Option | GPU | Notes |
|---|---|---|
| **Google Colab (free)** | 1× T4 16 GB | 27B Q4 (~16.5 GB) barely exceeds VRAM → use llama.cpp with a few CPU-offloaded layers (slower) or run the 4B. Colab Pro removes the pain. |
| **HF Spaces (ZeroGPU)** | A100 slice | Free but rate-limited/queued; good for a one-off. |
| **GCP / AWS / Azure free credits** | any GPU VM | Not "free tier" but $300 GCP credits run an L4/A10 for many hours. |
| **Modal / RunPod / Lightning** | rented GPU | Cheap (~$0.5–1.5/hr for an L4/A10); a full CoT run costs well under $1. |

---

## 4. Other evaluations worth running (to actually judge the models)

Accuracy on MedQA is one axis. For a clinical product, judge these too:

### A. More medical-knowledge benchmarks (same harness, swap `--dataset`)
- **MedMCQA** (~4.2k test, Indian AIIMS/NEET MCQs), *directly relevant to the India-first plan.*
- **PubMedQA** (yes/no/maybe over abstracts), evidence reasoning.
- **MMLU, clinical subsets** (clinical_knowledge, professional_medicine, anatomy, college_medicine).
- **MedQA on 5-option** and **USMLE step subsets** for harder splits.

### B. Safety & hallucination (the axis that actually kills clinical deployments)
- **Faithfulness / hallucination rate** on Orb's own RAG outputs: does Sage cite only retrieved
  guidance, or invent it? Orb already has a fail-closed verifier (`scribe_service.py`) and a
  retrieval eval (`server/services/retrieval_eval.py`), extend these into a scored suite.
- **Refusal / uncertainty calibration:** on out-of-scope or under-specified prompts, does the
  model defer instead of confabulate? Build a small adversarial set (missing data, contradictory
  vitals, non-existent drugs).
- **Med-HALT** (medical hallucination benchmark), reasoning + memory hallucination tests.
- **Contamination check:** MedGemma may have seen MedQA in training. Caveat every public-benchmark
  number, and weight an Orb-authored held-out set more heavily.

### C. Task-specific evals that mirror what Orb actually does
- **Ambient note factuality (Scribe):** transcript → note; score against reference notes for
  omitted/invented facts, and check the RxNorm correction step.
- **Deterioration triage (Vigil/Sage):** does the model's narrative agree with the deterministic
  NEWS2 escalation? Disagreements are the interesting cases.
- **Drug safety (Helix):** interaction/allergy/dose-range questions with known-correct answers.
- **Multilingual (India):** MedMCQA + Hindi/code-switched clinical prompts, since ambient Hindi is
  a stated wedge.

### D. Operational metrics (report alongside accuracy)
- **Latency & throughput** per box (tokens/s, time-to-first-token) at the real `num_ctx`.
- **Concurrency:** how many simultaneous Sage conversations one Mac Studio sustains.
- **Cost-per-1k-questions** on each deployment target.

### E. Calibration
- **Expected Calibration Error (ECE):** are the model's confidences trustworthy? Critical when a
  clinician sees a probability. Requires logprobs (use the `llamacpp` backend or Ollama logprobs).

---

## 5. How to report (the Orb way)

For every model + benchmark, record: **model + quant + exact revision, dataset + split, prompting
mode (CoT vs direct), N, accuracy, 95% CI, unparsed rate, date, hardware.** Keep the per-question
JSON so any claim is auditable. Feed the headline numbers into the **Model Governance** surface so
each AI output can carry its model version and eval provenance, turning "we validate our models"
from a slide into a product feature.
