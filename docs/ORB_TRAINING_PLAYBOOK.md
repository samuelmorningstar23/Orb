# Orb: Model Training & Dataset Playbook

> The plan for replacing synthetic hackathon data with **real clinical data** to train Orb's
> models for production. This is the critical-path document: some items (PhysioNet
> credentialing) have 1–2 week lead times and should be started **immediately**.

_Status: draft v1, 2026-06-25. Owner: Samuel. Honest, working document, not marketing._

---

## 0. TL;DR: what to do this week

1. **Start PhysioNet credentialing today** (§4), it gates MIMIC-IV + eICU and takes ~1–2 weeks.
2. Pull the **free/open** datasets that need no credentialing (§3) so we can prototype now.
3. Develop every training pipeline against **MedGemma 4B** on the Mini; defer 27B work to the
   48 GB / 96 GB machines (see `memory: orb-hardware-timeline`).
4. Standardise the **train-in-CUDA (Kaggle) → convert-to-MLX/GGUF (deploy)** path (§6).

---

## 1. The six models and what each one actually needs

| Model | Job in Orb | Trains on | Where it runs | Train where |
|---|---|---|---|---|
| **Vigil CfC** (liquid net, ~12 MB) | Deterioration trajectory from irregular vitals | Vitals time-series + deterioration/mortality labels | CPU/MPS, always-on | Mac or Kaggle (tiny) |
| **Forecast LSTM** (~3 MB) | Length-of-stay, discharge-prob, acuity | Admission-window features + LoS/discharge labels | CPU/MPS | Mac or Kaggle (tiny) |
| **MedGemma 4B** (LoRA) | Lens vision, simple text, Vigil explanations, voice | Medical QA + image-report pairs + note drafting | MLX (4B fits 8 GB) | Kaggle T4/P100 → MLX |
| **MedGemma 27B** (LoRA) | Sage deep reasoning, discharge synthesis | Medical QA + clinical reasoning + note drafting | Ollama (48 GB+ only) | Rented A100 / M3 Ultra |
| **Whisper** (LoRA) | Scribe medical ASR | Paired medical **audio + transcript** | MLX | Kaggle T4 → MLX |
| **BGE-M3** | Sage RAG embeddings | **No fine-tune**, used off-the-shelf; we only build the indexed corpus | MLX/PyTorch | n/a (corpus curation) |

> **Key correction baked in:** Vigil's constant monitoring is the **CfC net + MEWS**, not an LLM.
> The LLM (4B) only writes the explanation card when the risk score spikes.

---

## 2. Dataset → model mapping (what feeds what)

### 2.1 Vigil CfC + Forecast LSTM: vitals & outcomes
These need **time-series vitals with real outcome labels** (LoS, discharge, deterioration, mortality).

| Dataset | Access | What it gives | Notes |
|---|---|---|---|
| **MIMIC-IV** (v3.x, PhysioNet) | Credentialed (§4) | ICU+hospital EHR: vitals, labs, LoS, mortality, transfers | Primary source. US (Boston). |
| **MIMIC-IV-ED** | Credentialed | Emergency dept triage + vitals | Good for admission-time features. |
| **eICU-CRD** (PhysioNet) | Credentialed | Multi-center US ICU (200+ hospitals) | Cross-site generalisation. |
| **HiRID** (PhysioNet) | Credentialed | High-resolution Swiss ICU | Optional, European signal. |

**Label engineering:** derive deterioration labels from future MEWS/early-warning crossings,
ICU transfer, or mortality within N hours; LoS from admit→discharge timestamps; acuity classes
from trajectory of the warning score. Document the exact label definitions in the training repo.

The offline Vigil adapter now accepts **already-labelled** deterioration and
mortality extracts through `--labelled-extract`. Its
[input contract and validation commands](VIGIL_LABELLED_EXTRACT.md) require
prediction-time feature availability, a subject-separated split and training-only
normalization. The adapter does not select an outcome definition, obtain dataset
access, or establish clinical acceptance. Only synthetic adapter checks have run.

### 2.2 MedGemma (4B + 27B): clinical language & reasoning
| Dataset | Access | Use |
|---|---|---|
| **MedQA (USMLE)** | Open | Clinical QA reasoning |
| **MedMCQA** | Open | **Indian** medical entrance MCQs, directly relevant to India target |
| **PubMedQA** | Open | Evidence-grounded biomedical QA |
| **MMLU clinical/medical subsets** | Open | Eval + light tuning |
| **MIMIC-IV-Note** (discharge summaries, radiology) | Credentialed | Note drafting / discharge synthesis (Scribe, Discharge) |
| **mtsamples** | Open (already used) | Note formatting (SOAP/handoff) |
| **ACI-Bench** | Open | Doctor–patient dialogue → clinical note (Scribe drafting + verify) |

### 2.3 Lens: medical vision (MedGemma 4B multimodal)
| Dataset | Access | Use |
|---|---|---|
| **MIMIC-CXR** (images + free-text reports) | Credentialed | Chest X-ray → findings/impression (best fit for Orb's report format) |
| **NIH ChestX-ray14 / CheXpert** | Open (research) | Pretraining / augmentation, labels only |
| **PTB-XL** (ECG) | Open (PhysioNet, mostly open) | ECG strip interpretation |

### 2.4 Whisper: medical ASR
| Dataset | Access | Use |
|---|---|---|
| **PriMock57** | Open (Babylon, **UK** primary-care mock consults: audio + transcript + notes) | Real medical ASR + note pipeline, strong fit for UK-first |
| **Medical Speech, Transcription & Intent** (Kaggle) | Open | Symptom utterances |
| **Synthetic TTS over medical text** | We generate | Bootstraps drug-name/terminology coverage before real dictation |
| **Real dictation audio** | Partner hospitals (later) | Highest value; PHI-sensitive |

### 2.5 BGE-M3 RAG corpus (no model training: corpus building)
Index real, citable guidelines: **WHO**, **NICE (UK)**, **AHA/ACC**, **Surviving Sepsis Campaign**,
national formularies (BNF for UK, India's NLEM), and institutional protocols. This is curation +
chunking + embedding into `sqlite-vec`, not fine-tuning.

---

## 3. Free now / gated later

**Pull immediately (no credentialing):** MedQA, MedMCQA, PubMedQA, mtsamples, ACI-Bench,
PriMock57, PTB-XL, NIH ChestX-ray14, public guideline PDFs for RAG.

**Gated (start credentialing now):** MIMIC-IV, MIMIC-IV-ED, MIMIC-IV-Note, MIMIC-CXR, eICU, HiRID.

---

## 4. PhysioNet credentialing: do this first (1–2 week lead time)

1. Create a **PhysioNet** account (physionet.org).
2. Complete **CITI "Data or Specimens Only Research"** training → upload the completion report.
3. Get a **reference** (supervisor/colleague), required for credentialing.
4. Submit the credentialing application; wait for approval (~days to ~2 weeks).
5. For **each** dataset, sign its specific **Data Use Agreement** (e.g. PhysioNet Credentialed
   Health Data License 1.5.0).

**DUA constraints that affect us:**
- **No redistribution** of the data, ever. Do not commit it to git or upload to public Kaggle.
- Cloud use is allowed but the data must stay **private** (private Kaggle datasets / private buckets).
- No attempts to re-identify; report any accidental identification.

---

## 5. Multi-country calibration (UK → India → AU → EU → US)

The current LSTM was hard-calibrated on **Indian** vital-sign means/stds. For a multi-country
product, parameterise calibration **per region** rather than baking in one set:
- Keep the LayerNorm/normalisation stats as a **loadable per-region profile**.
- UK-first: lean on PriMock57 (UK) + NICE guidelines; validate before India rollout.
- Track which dataset each model checkpoint was trained/validated on (model card per checkpoint).

---

## 6. The training & conversion pipeline (CUDA → Apple)

Because we **train on NVIDIA (Kaggle)** but **deploy on Apple Silicon (MLX/Ollama)**:

```
  Kaggle (T4/P100, PEFT/QLoRA)          Apple Silicon (deploy)
  ┌───────────────────────────┐         ┌──────────────────────────┐
  │ HF base + LoRA fine-tune  │  ──►    │ merge LoRA → base         │
  │ (4B, Whisper, eval)       │  export │ convert → MLX  (mlx_lm)   │
  │                           │  weights│   or → GGUF (Ollama)      │
  └───────────────────────────┘         └──────────────────────────┘
```

- **4B / Whisper:** QLoRA on a single Kaggle T4/P100 (16 GB). 30 hrs/week free quota.
- **27B:** QLoRA needs **A100 40 GB** (rented; Colab Pro+, RunPod, Lightning), or local MLX once
  the **M3 Ultra 96 GB** arrives.
- **CfC / LSTM:** tiny, train directly on the Mac or Kaggle CPU.
- Existing MLX-native LoRA on the Mini is also fine for 4B once the 48 GB laptop is back; choose
  per machine. (The current repo trained MLX-natively, which skips the conversion step.)

**Conversion specifics to standardise:** merge adapter with `mlx_lm.fuse` (MLX path) or
`llama.cpp` convert + quantize to GGUF (Ollama path); re-run eval **after** conversion to confirm
no quality regression from quantization.

---

## 7. Evaluation gates (before any checkpoint ships)

- **MedGemma:** MedQA / MedMCQA / PubMedQA accuracy vs. the base model (must not regress);
  note-drafting faithfulness on ACI-Bench; **hallucination rate** on Scribe verify stage.
- **Lens:** report-vs-reference agreement on a held-out MIMIC-CXR split; radiologist spot-check.
- **Vigil CfC / Forecast LSTM:** AUROC/AUPRC for deterioration; MAE for LoS; calibration plots.
- **Whisper:** WER overall + **drug-name/medical-term WER** specifically.
- Every shipped checkpoint gets a **model card**: data sources, train date, metrics, known limits.

---

## 8. Governance reminders (non-negotiable)

- Credentialed data **never** touches git, public Kaggle, or any cloud bucket without access control.
- Keep a clear separation: **training data** (offline, governed) vs. **product DB** (`orb.db`, gitignored).
- These models are **not regulatory-cleared**; clinical claims require validation + clearance
  (see `memory: orb-regulatory-reality`). Eval metrics here are engineering gates, not clinical proof.

---

### Open questions to resolve as we go
- Which **partner hospital(s)** can provide de-identified real data, and under what agreement?
- Do we want a **per-region model zoo** (UK/India/... checkpoints) or one global model + region calibration?
- Real dictation audio source for Whisper, timeline?
