"""Marker-contract compliance rate, N samples per probe, both models.

Aborts the stream once the marker decision is unambiguous, so each sample costs
prefill + a handful of decoded tokens rather than a whole answer.
"""
import json
import sys
import time

import httpx

sys.path.insert(0, "/Users/samuelmorningstar/Documents/Projects/Orb/server")
from database import db_session                                    # noqa: E402
from services.sage_service import (SCOPE_CONFIGS, _build_chat_messages,  # noqa: E402
                                   get_relevant_context)

OLLAMA = "http://localhost:11434"
MODELS = ["medgemma:4b", "medgemma:27b"]
N = 5

PROBES = [
    ("ACT",   "P", "Add paracetamol 500mg for this patient."),
    ("ACT",   "P", "Stop the heparin for this patient."),
    ("ACT",   "P", "Order a chest X-ray for this patient."),
    ("NOACT", "P", "What is this patient's NEWS2 score?"),
    ("NOACT", "P", "What would you check next for this patient?"),
    ("NOACT", None, "How do you dose IV alteplase in acute ischaemic stroke?"),
    ("NOACT", None, "How is potassium replaced in severe hypokalaemia?"),
]


def marker_of(txt):
    t = txt.lstrip()
    if t.startswith("```"):
        return "FENCE"
    if t.startswith("NOACT|"):
        return "NOACT"
    if t.startswith("ACT|"):
        return "ACT"
    return "NONE"


def probe(model, messages, opts):
    body = {"model": model, "messages": messages, "stream": True, "keep_alive": -1,
            "options": {"temperature": opts["temperature"], "repeat_penalty": 1.15,
                        "repeat_last_n": 256, "num_predict": opts["num_predict"],
                        "num_ctx": opts["num_ctx"]}}
    buf = ""
    with httpx.Client(timeout=900, trust_env=False) as c:
        with c.stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                ch = (d.get("message") or {}).get("content")
                if ch:
                    buf += ch
                    if len(buf.lstrip()) >= 24:
                        break
                if d.get("done"):
                    break
    return buf


def main():
    with db_session() as db:
        user = dict(db.execute("SELECT * FROM users WHERE id='DOC001'").fetchone())
        pid = db.execute("SELECT id FROM patients WHERE name='Rajesh Iyer'").fetchone()["id"]

    out = []
    for expect, pflag, q in PROBES:
        ctx, scope, *_ = get_relevant_context(q, user, pid if pflag else None, None)
        opts = SCOPE_CONFIGS.get(scope, SCOPE_CONFIGS["simple"])
        msgs = _build_chat_messages(q, ctx, None, scope)
        row = {"expect": expect, "q": q, "scope": scope, "res": {}}
        for m in MODELS:
            got = []
            for _ in range(N):
                got.append(marker_of(probe(m, msgs, opts)))
            row["res"][m] = got
            hit = sum(1 for g in got if g == expect)
            print(f"  {expect:<5} [{scope:<8}] {q[:44]:<46} {m:<14} {hit}/{N}  {got}",
                  flush=True)
        out.append(row)

    with open(sys.argv[1], "w") as f:
        json.dump({"n": N, "run_at": time.strftime("%FT%T"), "probes": out}, f, indent=1)
    print("wrote", sys.argv[1])


if __name__ == "__main__":
    main()
