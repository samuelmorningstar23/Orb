"""SIMPLE-class probe set: single-fact / definition / recall, unambiguous truth.

Each carries the ground truth I score against, so the scoring is auditable.
Driven through Orb's real prompt assembly, straight at Ollama.
"""
import json
import sys
import time

import httpx

sys.path.insert(0, "/Users/samuelmorningstar/Documents/Projects/Orb/server")
from database import db_session                                       # noqa: E402
from services.sage_service import (SCOPE_CONFIGS, _build_chat_messages,   # noqa: E402
                                   get_relevant_context)

OLLAMA = "http://localhost:11434"
MODELS = ["medgemma:4b", "medgemma:27b"]

# (question, ground truth I score against)
Q = [
    ("What is the antidote for paracetamol overdose?",
     "N-acetylcysteine"),
    ("What is the first-line drug for anaphylaxis, and what is the adult intramuscular dose?",
     "Adrenaline/epinephrine 0.5 mg IM (0.5 mL of 1:1000), anterolateral thigh"),
    ("What is the maximum daily paracetamol dose for a healthy 70 kg adult?",
     "4 g in 24 h (1 g QDS)"),
    ("What is the first-line drug treatment for torsades de pointes?",
     "IV magnesium sulfate 2 g"),
    ("What is the earliest ECG change in hyperkalaemia?",
     "Tall peaked/tented T waves"),
    ("Which vitamin must be given before glucose in a malnourished or alcohol-dependent patient, and why?",
     "Thiamine (vitamin B1), to avoid precipitating Wernicke encephalopathy"),
    ("What ascitic fluid neutrophil count makes the diagnosis of spontaneous bacterial peritonitis?",
     "Polymorphonuclear/neutrophil count >= 250 cells/mm3"),
    ("What is the target oxygen saturation range for a COPD patient at risk of hypercapnic respiratory failure?",
     "88-92%"),
    ("What reverses the anticoagulant effect of unfractionated heparin?",
     "Protamine sulfate"),
    ("What is the mechanism of action of furosemide?",
     "Inhibits the Na-K-2Cl (NKCC2) cotransporter in the thick ascending limb of the loop of Henle"),
    ("What serum potassium level must be corrected before starting an insulin infusion in DKA?",
     "K+ below 3.3 mmol/L - replace potassium first, withhold insulin"),
    ("What is the target INR range for a patient with a mechanical mitral valve replacement?",
     "2.5-3.5"),
    ("What is the loading dose of IV phenytoin in status epilepticus and the maximum infusion rate?",
     "20 mg/kg loading; max rate 50 mg/min (25 mg/min or less in elderly/cardiac disease)"),
    ("Define AKI stage 1 by the KDIGO criteria.",
     "Creatinine rise >= 26.5 micromol/L (0.3 mg/dL) within 48 h, or 1.5-1.9x baseline within 7 days, or urine output <0.5 mL/kg/h for 6-12 h"),
    ("What is the mechanism of action of metformin?",
     "Reduces hepatic gluconeogenesis (AMPK-mediated), increases peripheral insulin sensitivity; does not stimulate insulin secretion"),
    ("Which class of antibiotics should be avoided in myasthenia gravis?",
     "Aminoglycosides (also fluoroquinolones and macrolides) - neuromuscular blockade"),
    ("What CURB-65 score threshold indicates the patient should be considered for hospital admission?",
     "Score >= 2"),
    ("What is the normal serum anion gap range?",
     "8-12 mmol/L (roughly 3-11 depending on assay; up to 16 if potassium is included)"),
    ("What is the reversal agent for warfarin in life-threatening bleeding?",
     "IV vitamin K 5-10 mg PLUS four-factor prothrombin complex concentrate (FFP only if PCC unavailable)"),
    ("At what NEWS2 aggregate score should an emergency/critical care response be triggered?",
     "7 or more (also any single parameter scoring 3 = medium/urgent review; 5-6 = urgent)"),
]


def call(model, messages, opts):
    body = {"model": model, "messages": messages, "stream": True, "keep_alive": -1,
            "options": {"temperature": opts["temperature"], "repeat_penalty": 1.15,
                        "repeat_last_n": 256, "num_predict": opts["num_predict"],
                        "num_ctx": opts["num_ctx"]}}
    t0 = time.perf_counter()
    ttft, buf, stats = None, [], {}
    with httpx.Client(timeout=900, trust_env=False) as c:
        with c.stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                ch = (d.get("message") or {}).get("content")
                if ch:
                    if ttft is None:
                        ttft = (time.perf_counter() - t0) * 1000
                    buf.append(ch)
                if d.get("done"):
                    stats = d
                    break
    return {"answer": "".join(buf),
            "ttft_ms": round(ttft, 1) if ttft else None,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            "prompt_tokens": stats.get("prompt_eval_count"),
            "decode_tokens": stats.get("eval_count")}


def main():
    with db_session() as db:
        user = dict(db.execute("SELECT * FROM users WHERE id='DOC001'").fetchone())
    recs = []
    for i, (q, truth) in enumerate(Q, 1):
        ctx, scope, *_ = get_relevant_context(q, user, None, None)
        opts = SCOPE_CONFIGS.get(scope, SCOPE_CONFIGS["simple"])
        msgs = _build_chat_messages(q, ctx, None, scope)
        row = {"n": i, "q": q, "truth": truth, "scope": scope, "runs": {}}
        for m in MODELS:
            row["runs"][m] = call(m, msgs, opts)
            r = row["runs"][m]
            print(f"  S{i:>2} {m:<14} ttft={r['ttft_ms']:>7}ms tot={r['total_ms']:>8}ms [{scope}]",
                  flush=True)
        recs.append(row)
    with open(sys.argv[1], "w") as f:
        json.dump({"phase": "simple_set", "run_at": time.strftime("%FT%T"),
                   "records": recs}, f, indent=1)
    print("wrote", sys.argv[1])


if __name__ == "__main__":
    main()
