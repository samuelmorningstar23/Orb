#!/usr/bin/env python3
"""Benchmark a MedGemma model on a medical multiple-choice QA dataset.

This is an *evaluation* utility — it does not touch the app or its data. It runs a
model over a MCQA benchmark (MedQA-USMLE by default) and reports accuracy with a
95% Wilson confidence interval, saving per-question results for audit.

Two backends, same harness:
  * ``--backend ollama``   → POST {base_url}/api/chat  (the exact call the app uses;
                             point it at medgemma:27b on your Mac Studio or a GPU box)
  * ``--backend llamacpp`` → load a local .gguf via llama-cpp-python (CPU-friendly;
                             used to benchmark the 4B tier without a GPU)

Datasets (``--dataset``):
  * ``medqa``   MedQA-USMLE 4-option (parquet with columns: question, options|choices, answer_idx)
  * ``medmcqa`` MedMCQA 4-option
  * ``pubmedqa`` PubMedQA (yes/no/maybe)
  * ``jsonl``   any local .jsonl with {question, options:{A..}, answer_idx|answer}

Examples
--------
# 27B via Ollama (Mac Studio / Kaggle w/ ollama serve), full MedQA test set:
    python benchmark_medqa.py --backend ollama --model medgemma:27b \
        --dataset medqa --data medqa_test.parquet

# 4B via local GGUF on CPU, first 200 questions, deterministic:
    python benchmark_medqa.py --backend llamacpp --model ./medgemma-4b-it-Q4_K_M.gguf \
        --dataset medqa --data medqa_test.parquet --limit 200

Answers are parsed leniently (leading letter, "Answer: C", or matching the option
text). Runs are checkpointed to the output JSON so an interrupted run resumes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from pathlib import Path

LETTERS = ["A", "B", "C", "D", "E"]

SYSTEM_PROMPT_COT = (
    "You are a careful medical expert answering a multiple-choice question from a "
    "medical licensing exam. Read the question and options, reason briefly, then give "
    "your final answer. End your response with a line in exactly this format:\n"
    "Answer: <LETTER>"
)

SYSTEM_PROMPT_LETTER = (
    "You are a medical expert answering a multiple-choice question from a medical "
    "licensing exam. Respond with ONLY the single capital letter of the best answer "
    "(one of A, B, C, D, E). Output nothing else, no words, no punctuation, no explanation."
)

# GBNF grammar that forces a single answer letter (llama.cpp backend, --letter-only).
LETTER_GRAMMAR = "root ::= [A-E]"


# ----------------------------------------------------------------------------- data
def _norm_options(raw) -> list[str]:
    """Return options as an ordered list of strings from the many MedQA schemas."""
    if isinstance(raw, dict):
        # {"A": "...", "B": "..."} or {"0": "...", ...}
        keys = sorted(raw.keys(), key=lambda k: LETTERS.index(k) if k in LETTERS else int(k))
        return [str(raw[k]) for k in keys]
    if isinstance(raw, (list, tuple)):
        # list of strings, or list of {"key","value"} dicts
        out = []
        for item in raw:
            if isinstance(item, dict):
                out.append(str(item.get("value", item.get("text", item))))
            else:
                out.append(str(item))
        return out
    raise ValueError(f"Unrecognized options schema: {type(raw)}")


def _answer_index(row, options: list[str]) -> int:
    """Resolve the gold answer to a 0-based index across schemas."""
    for key in ("answer_idx", "answer_index", "cop", "correct_option_idx", "label"):
        if key in row and row[key] is not None:
            v = row[key]
            if isinstance(v, str) and v.strip() in LETTERS:
                return LETTERS.index(v.strip())
            try:
                iv = int(v)
                # MedMCQA 'cop' is 0-based; some sets are 1-based — heuristics:
                return iv if iv < len(options) else iv - 1
            except (ValueError, TypeError):
                pass
    # Fall back to matching the answer *text*.
    for key in ("answer", "answer_text", "correct_answer"):
        if key in row and row[key] is not None:
            a = str(row[key]).strip().lower()
            for i, opt in enumerate(options):
                if opt.strip().lower() == a:
                    return i
    raise ValueError(f"Could not resolve gold answer for row keys={list(row.keys())}")


def load_dataset(kind: str, path: str, limit: int | None) -> list[dict]:
    rows: list[dict]
    if path.endswith(".parquet"):
        import pyarrow.parquet as pq

        rows = pq.read_table(path).to_pylist()
    elif path.endswith(".jsonl"):
        rows = [
            json.loads(left_item)
            for left_item in Path(path).read_text().splitlines()
            if left_item.strip()
        ]
    elif path.endswith(".json"):
        rows = json.loads(Path(path).read_text())
    else:
        raise ValueError("data file must be .parquet/.jsonl/.json")

    items = []
    for r in rows:
        opt_field = next(
            (k for k in ("options", "choices", "opa_opb_opc_opd", "opts") if k in r), None
        )
        if opt_field:
            options = _norm_options(r[opt_field])
        elif all(k in r for k in ("opa", "opb", "opc", "opd")):  # MedMCQA flat
            options = [str(r["opa"]), str(r["opb"]), str(r["opc"]), str(r["opd"])]
        else:
            raise ValueError(f"No options field in row: {list(r.keys())}")
        gold = _answer_index(r, options)
        items.append({"question": str(r["question"]), "options": options, "gold": gold})
        if limit and len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------- prompt
def build_prompt(item: dict) -> str:
    lines = [item["question"], ""]
    for i, opt in enumerate(item["options"]):
        lines.append(f"{LETTERS[i]}. {opt}")
    return "\n".join(lines)


def parse_answer(text: str, n_options: int, options: list[str]) -> int | None:
    """Extract a 0-based answer index from free-text model output."""
    valid = set(LETTERS[:n_options])
    s = text.strip()
    # 0) bare letter (grammar-constrained letter-only output, e.g. "A" or "A.")
    if s and s[0].upper() in valid and (len(s) == 1 or not s[1].isalnum()):
        return LETTERS.index(s[0].upper())
    # 1) explicit "Answer: X" (last occurrence wins)
    m = re.findall(r"answer\s*[:\-]?\s*\(?\s*([A-E])\b", text, flags=re.IGNORECASE)
    if m and m[-1].upper() in valid:
        return LETTERS.index(m[-1].upper())
    # 2) a lone/leading letter like "C." or "(C)"
    m = re.findall(r"(?:^|\n|\s)\(?([A-E])\)?[\.\):]", text)
    cand = [c.upper() for c in m if c.upper() in valid]
    if cand:
        return LETTERS.index(cand[-1])
    # 3) match option text
    low = text.lower()
    hits = [i for i, opt in enumerate(options) if opt.strip().lower() in low]
    if len(hits) == 1:
        return hits[0]
    return None


# -------------------------------------------------------------------------- backends
class OllamaBackend:
    def __init__(self, model, base_url, temperature, num_predict, system):
        import httpx

        self._httpx = httpx
        self.model, self.base_url = model, base_url.rstrip("/")
        self.temperature, self.num_predict, self.system = temperature, num_predict, system

    def ask(self, prompt: str) -> str:
        r = self._httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "keep_alive": "30m",
                "options": {
                    "temperature": self.temperature,
                    "num_predict": self.num_predict,
                    "num_ctx": 4096,
                },
            },
            timeout=self._httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            # Loopback Ollama — skip httpx's proxy discovery (see
            # services/model_router.py). httpx.post() builds a throwaway Client
            # per call, so this pays the same untimed macOS configd lookup.
            trust_env=False,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]


class LlamaCppBackend:
    def __init__(self, model, temperature, num_predict, n_threads, n_ctx, system, letter_only):
        from llama_cpp import Llama
        from llama_cpp.llama_grammar import LlamaGrammar

        self.temperature, self.num_predict, self.system = temperature, num_predict, system
        self.grammar = (
            LlamaGrammar.from_string(LETTER_GRAMMAR, verbose=False) if letter_only else None
        )
        self.llm = Llama(
            model_path=model,
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_batch=512,
            verbose=False,
        )  # use the GGUF's embedded chat template (correct for MedGemma/Gemma-3)

    def ask(self, prompt: str) -> str:
        out = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": self.system},
                {"role": "user", "content": prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.num_predict,
            grammar=self.grammar,
        )
        return out["choices"][0]["message"]["content"]


# ----------------------------------------------------------------------------- stats
def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ------------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--backend", choices=["ollama", "llamacpp"], required=True)
    ap.add_argument("--model", required=True, help="ollama tag (medgemma:27b) or path to .gguf")
    ap.add_argument("--dataset", default="medqa", choices=["medqa", "medmcqa", "pubmedqa", "jsonl"])
    ap.add_argument(
        "--data", required=True, help="path to the dataset file (.parquet/.jsonl/.json)"
    )
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument(
        "--letter-only",
        action="store_true",
        help="direct-answer mode: force a single answer letter (fast, CPU-friendly). "
        "llama.cpp uses a GBNF grammar; omit for chain-of-thought.",
    )
    ap.add_argument(
        "--num-predict",
        type=int,
        default=512,
        help="max new tokens (auto-lowered to 4 for --letter-only)",
    )
    ap.add_argument("--base-url", default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--n-ctx", type=int, default=4096)
    ap.add_argument(
        "--out", default=None, help="results JSON (default: results/medqa_<model>_<ts>.json)"
    )
    args = ap.parse_args()

    items = load_dataset(args.dataset, args.data, args.limit)
    print(f"Loaded {len(items)} questions from {args.data} ({args.dataset}).", flush=True)

    system = SYSTEM_PROMPT_LETTER if args.letter_only else SYSTEM_PROMPT_COT
    num_predict = 4 if args.letter_only else args.num_predict
    mode = "letter-only (direct answer)" if args.letter_only else "chain-of-thought"
    print(f"Mode: {mode} | backend={args.backend} | num_predict={num_predict}", flush=True)

    backend: OllamaBackend | LlamaCppBackend
    if args.backend == "ollama":
        backend = OllamaBackend(args.model, args.base_url, args.temperature, num_predict, system)
    else:
        backend = LlamaCppBackend(
            args.model,
            args.temperature,
            num_predict,
            args.threads,
            args.n_ctx,
            system,
            args.letter_only,
        )

    model_slug = re.sub(r"[^\w.-]", "_", Path(args.model).name)
    out_path = Path(args.out) if args.out else Path("results") / f"{args.dataset}_{model_slug}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Resume from an existing checkpoint.
    done: dict[int, dict] = {}
    if out_path.exists():
        prev = json.loads(out_path.read_text())
        done = {d["i"]: d for d in prev.get("details", [])}
        print(f"Resuming: {len(done)} already done.", flush=True)

    correct = sum(1 for d in done.values() if d["is_correct"])
    unparsed = sum(1 for d in done.values() if d["pred"] is None)
    details = list(done.values())
    t0 = time.time()

    for i, item in enumerate(items):
        if i in done:
            continue
        try:
            raw = backend.ask(build_prompt(item))
        except Exception as e:  # noqa: BLE001 — one bad call shouldn't kill the run
            raw = f"<<ERROR: {e}>>"
        pred = parse_answer(raw, len(item["options"]), item["options"])
        is_correct = pred == item["gold"]
        correct += int(is_correct)
        unparsed += int(pred is None)
        rec = {
            "i": i,
            "gold": item["gold"],
            "pred": pred,
            "is_correct": is_correct,
            "raw": raw[:600],
        }
        details.append(rec)

        n = len(details)
        if n % 10 == 0 or i == len(items) - 1:
            acc = correct / n
            rate = n / max(1e-9, time.time() - t0)
            lo, hi = wilson_ci(correct, n)
            print(
                f"[{n}/{len(items)}] acc={acc:.3f} ({correct}/{n}) "
                f"95%CI[{lo:.3f},{hi:.3f}] unparsed={unparsed} {rate:.2f} q/s",
                flush=True,
            )
            out_path.write_text(
                json.dumps(
                    {
                        "model": args.model,
                        "backend": args.backend,
                        "dataset": args.dataset,
                        "n": n,
                        "correct": correct,
                        "accuracy": acc,
                        "ci95": [lo, hi],
                        "unparsed": unparsed,
                        "details": sorted(details, key=lambda d: d["i"]),
                    },
                    indent=2,
                )
            )

    n = len(details)
    acc = correct / max(1, n)
    lo, hi = wilson_ci(correct, n)
    print("\n" + "=" * 60)
    print(f"RESULT  model={args.model}  dataset={args.dataset}")
    print(f"  accuracy = {acc:.4f}  ({correct}/{n})")
    print(f"  95% CI   = [{lo:.4f}, {hi:.4f}]  (Wilson)")
    print(f"  unparsed = {unparsed} (counted wrong)")
    print(f"  saved    → {out_path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
