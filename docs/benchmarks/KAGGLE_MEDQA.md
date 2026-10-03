# Kaggle: MedQA benchmark for MedGemma (4B then 27B)

A self-contained Kaggle notebook. No HF token, no gated licenses, no repo clone, Ollama pulls the
models ungated and handles the chat template. You run **4B first**, then change one line and run
the **27B**.

Copy each **Cell** below into a new Kaggle notebook cell, in order.

---

## Before you start (one-time Kaggle setup)

1. Go to **kaggle.com → Create → New Notebook**.
2. Right sidebar → **Settings**:
   - **Accelerator → GPU T4 × 2**  (two 16 GB GPUs = 32 GB VRAM; needed for the 27B)
   - **Internet → On**
   - (Kaggle asks you to verify a phone number once to unlock GPU + Internet.)
3. Paste the cells below.

> **Time budget.** Kaggle gives ~30 GPU-hours/week and an interactive session up to ~12 h. For a
> long full run, use **"Save Version → Save & Run All (Commit)"**, it runs headless and keeps the
> result even if you close the browser.

---

## Cell 1: confirm the GPUs

```python
!nvidia-smi -L
```
You should see **two Tesla T4** lines. If you see none, fix the Accelerator setting above.

---

## Cell 2: install & start Ollama

```python
!apt-get -qq update && apt-get -qq install -y zstd   # Ollama's installer needs zstd to unpack; Kaggle lacks it
!curl -fsSL https://ollama.com/install.sh | sh
!pip -q install requests pyarrow

import os, time, subprocess, requests
os.environ["OLLAMA_HOST"] = "127.0.0.1:11434"
# Kaggle has no systemd, so start the server ourselves:
subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

for _ in range(60):
    try:
        if requests.get("http://127.0.0.1:11434/api/tags", timeout=2).ok:
            print("✅ Ollama is up"); break
    except Exception:
        pass
    time.sleep(1)
else:
    print("❌ Ollama did not start, re-run this cell")
```

---

## Cell 3: CONFIG (the only cell you edit between runs)

```python
# ---- RUN 1: 4B (matches Google's bf16 protocol) ----
MODEL = "medgemma:4b-it-bf16"
OUT   = "/kaggle/working/medqa_4b_bf16.json"

# ---- RUN 2: 27B (comment out the two lines above, uncomment these) ----
# MODEL = "medgemma:27b-it-q4_K_M"      # bf16-27B is ~54GB and won't fit; Q4 (~16GB) fits 2xT4 and is what Orb ships
# OUT   = "/kaggle/working/medqa_27b_q4.json"

LIMIT = None      # None = all 1273 questions. Set 300 for a quick check (~CI ±5.5%).
COT   = True      # True = chain-of-thought (matches the official 64.4% protocol). False = fast letter-only.
MAX_NEW_TOKENS = 512

print("Model:", MODEL, "| CoT:", COT, "| limit:", LIMIT)
```

---

## Cell 4: pull the model (downloads once)

```python
!ollama pull {MODEL}
# warm it up and confirm it's on the GPU (should print "100% GPU"):
import requests
requests.post("http://127.0.0.1:11434/api/generate",
              json={"model": MODEL, "prompt": "ok", "stream": False}, timeout=600)
!ollama ps
```
`ollama ps` should show the model with **100% GPU**. (The 27B may show a small CPU % if it spills, 
that's fine, it still runs.)

---

## Cell 5: download MedQA (USMLE 4-option test set, ungated)

Use the `huggingface_hub` client (a bare `urllib` download 403s now that HF routes files through
its Xet CDN). Still no token needed for this public dataset.

```python
!pip -q install huggingface_hub hf_xet
from huggingface_hub import hf_hub_download
import pyarrow.parquet as pq

path = hf_hub_download(
    repo_id="openlifescienceai/medqa",
    filename="data/test-00000-of-00001.parquet",
    repo_type="dataset",
)
rows = pq.read_table(path).to_pylist()
data = [{"question": r["data"]["Question"],
         "options":  r["data"]["Options"],       # {"A": "...", "B": "...", ...}
         "gold":     r["data"]["Correct Option"]} # a letter, e.g. "B"
        for r in rows]
print(len(data), "questions | sample gold:", data[0]["gold"])
```

*Fallback (no filename needed):*
```python
!pip -q install datasets
from datasets import load_dataset
ds = load_dataset("openlifescienceai/medqa", split="test")
data = [{"question": r["data"]["Question"], "options": r["data"]["Options"],
         "gold": r["data"]["Correct Option"]} for r in ds]
print(len(data), "questions | sample gold:", data[0]["gold"])
```

---

## Cell 6: run the benchmark (accuracy + 95% CI, checkpointed)

```python
import re, json, math, time, requests

LETTERS = ["A", "B", "C", "D", "E"]
SYS_COT = ("You are a medical expert answering a USMLE-style multiple-choice question. "
           "Reason step by step, then end with a line in exactly this format:\nAnswer: X")
SYS_LET = ("You are a medical expert. Respond with ONLY the single capital letter (A-E) of the "
           "best answer. No words, no punctuation.")

def build(q):
    lines = [q["question"], ""]
    for k in sorted(q["options"]):
        lines.append(f'{k}. {q["options"][k]}')
    return "\n".join(lines)

def ask(prompt):
    r = requests.post("http://127.0.0.1:11434/api/chat",
        json={"model": MODEL, "stream": False,
              "messages": [{"role": "system", "content": SYS_COT if COT else SYS_LET},
                           {"role": "user", "content": prompt}],
              "options": {"temperature": 0, "num_ctx": 4096,
                          "num_predict": MAX_NEW_TOKENS if COT else 4}},
        timeout=600)
    r.raise_for_status()
    return r.json()["message"]["content"]

def parse(text, n):
    valid = set(LETTERS[:n]); s = text.strip()
    m = re.findall(r"answer\s*[:\-]?\s*\(?\s*([A-E])\b", text, re.I)   # "Answer: C"
    if m and m[-1].upper() in valid: return LETTERS.index(m[-1].upper())
    if s and s[0].upper() in valid and (len(s) == 1 or not s[1].isalnum()):  # bare "C"
        return LETTERS.index(s[0].upper())
    m = re.findall(r"(?:^|\n|\s)\(?([A-E])\)?[\.\):]", text)           # "(C)." / "C)"
    c = [x.upper() for x in m if x.upper() in valid]
    if c: return LETTERS.index(c[-1])
    return None

def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k / n; d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / d
    return (max(0.0, c-h), min(1.0, c+h))

def gold_idx(g):
    return LETTERS.index(g) if isinstance(g, str) else int(g)

items = data if LIMIT is None else data[:LIMIT]
correct = unparsed = 0; details = []; t0 = time.time()

for i, q in enumerate(items):
    g = gold_idx(q["gold"]); n = len(q["options"])
    try:
        raw = ask(build(q))
    except Exception as e:
        raw = f"<<ERROR {e}>>"
    p = parse(raw, n); ok = (p == g)
    correct += ok; unparsed += (p is None)
    details.append({"i": i, "gold": g, "pred": p, "ok": ok, "raw": raw[:400]})
    if (i + 1) % 10 == 0 or i == len(items) - 1:
        m = i + 1; acc = correct / m; lo, hi = wilson(correct, m)
        rate = m / (time.time() - t0)
        print(f"[{m}/{len(items)}] acc={acc:.3f} ({correct}) 95%CI[{lo:.3f},{hi:.3f}] "
              f"unparsed={unparsed} {rate:.2f} q/s", flush=True)
        json.dump({"model": MODEL, "cot": COT, "n": m, "correct": correct, "accuracy": acc,
                   "ci95": [lo, hi], "unparsed": unparsed, "details": details},
                  open(OUT, "w"), indent=2)

m = len(items); acc = correct / m; lo, hi = wilson(correct, m)
print("\n" + "=" * 60)
print(f"RESULT  {MODEL}  (CoT={COT})")
print(f"  accuracy = {acc:.4f}  ({correct}/{m})")
print(f"  95% CI   = [{lo:.4f}, {hi:.4f}]")
print(f"  unparsed = {unparsed} (counted wrong)")
print(f"  saved    → {OUT}")
print("=" * 60)
```

---

## How to do the two runs

**Run 1 to 4B (do this first):**
1. Run Cells 1 → 6 in order with the default Cell 3 (`medgemma:4b-it-bf16`, `COT=True`).
2. For a quick first look set `LIMIT = 300` in Cell 3 (~20–40 min). For the real number set
   `LIMIT = None` (full 1,273) and use **Save & Run All (Commit)** so it finishes headless.
3. Expect **~62–65%**, close to Google's official **64.4%** (bf16 + CoT). If it lands there,
   your harness and the model are both behaving.

**Run 2 to 27B (after 4B):**
1. In **Cell 3**, comment the 4B lines and uncomment the 27B lines (`medgemma:27b-it-q4_K_M`).
2. Re-run **Cell 3, Cell 4 (pull the 27B, ~16 GB), then Cell 6.**
3. The 27B is slower (~20–35 s/question with CoT). Use `LIMIT = 500` for a solid number
   (95% CI ≈ ±4.4%) in a few hours, or the full set via Commit if you have the GPU-hours.
4. Expect **mid-to-high 80s** (official 27B MedQA ≈ 87%). Q4 lands a point or two under bf16, 
   and Q4 is exactly what Orb runs in production, so this is the honest "our stack" number.

**Get the results out:** the JSON is written to `/kaggle/working/…`. After a committed run, find it
under the notebook's **Output** tab and download it. Send it to me (or paste the final `RESULT`
block) and I'll fold both numbers into `docs/MODEL_BENCHMARKING.md`.

---

## Knobs & troubleshooting

| Want | Change |
|---|---|
| Faster (rough) number | `COT = False` (letter-only, ~10× faster; a few points lower) |
| Shorter CoT (≈2× faster) | `MAX_NEW_TOKENS = 256` |
| Quick sanity run | `LIMIT = 300` |
| Match official 4B exactly | `medgemma:4b-it-bf16`, `COT = True`, `LIMIT = None` |
| Orb's production 4B number | `medgemma:4b-it-q4_K_M` (what Ollama serves by default) |

- **Ollama installer errors "requires zstd for extraction":** Cell 2 installs it via
  `apt-get install -y zstd`. If apt is blocked, use `!conda install -y -c conda-forge zstd`, then
  re-run the install line.
- **`ollama pull` fails / 404:** re-run Cell 2 first (server must be up), then Cell 4.
- **Out of memory on 27B:** make sure Accelerator is **T4 × 2**, not ×1. Q4 needs ~16 GB.
- **`unparsed` climbing:** the model isn't ending with `Answer: X`. Lower `MAX_NEW_TOKENS` won't
  help; instead set `COT = False` for a clean letter-only run, or raise `MAX_NEW_TOKENS`.
- **Session died mid-run:** the JSON checkpoints every 10 questions, so the partial result (with
  its accuracy so far) is preserved in `OUT`. Re-running Cell 6 starts over from question 0, for
  long runs prefer **Save & Run All (Commit)** so it finishes headless instead.
```
