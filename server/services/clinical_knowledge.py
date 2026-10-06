"""Orb Clinical Knowledge Base — RAG Pipeline for Clinical Guidelines

Embeds hospital SOPs, formularies, and external clinical guidelines into a
SQLite-vec vector store (and FTS5 lexical index) for retrieval-augmented Sage queries.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import numpy as np
from typing import List, Tuple, Dict, Any, Optional
from config import DB_PATH, EMBEDDING_MODEL, EMBEDDING_DIM, SAGE_RERANKING_ENABLED
from database import db_session
from services.knowledge_worker import (
    KnowledgeWorkerError,
    embed_texts_isolated,
    isolation_enabled,
    rerank_scores_isolated,
)

# ─── Lazy-loaded embedding model (in-process path ONLY) ───
# By default the heavy models live in the knowledge worker subprocess (see
# services/knowledge_worker.py — in-process loads starved the event loop of
# the GIL and froze the API for minutes). This in-process path survives for
# ORB_KNOWLEDGE_INPROCESS=1 (tests / constrained envs) and for the worker
# process itself.
_embed_model = None


def _in_worker() -> bool:
    return os.getenv("ORB_KNOWLEDGE_WORKER", "") == "1"


def _use_isolated() -> bool:
    return isolation_enabled() and not _in_worker()

# Serializes ALL work against the shared HuggingFace models (BGE-M3 embedder and
# the cross-encoder reranker) — both lazy construction and every encode/score
# call. Retrieval runs concurrently on separate threads (interactive Sage via
# to_thread, plus the prefetch/critical-alert warm path in
# nervous_system_triggers.warm_patient_context), and the tokenizers' Rust core is
# not safe for concurrent use: two threads scoring at once raise
# RuntimeError("Already borrowed") or, worse, silently interleave truncation
# settings. The lock also prevents two threads double-loading the ~2 GB model.
# Reentrant so a nested acquire (init inside a guarded call site) cannot deadlock.
_MODEL_LOCK = threading.RLock()


def _local_snapshot_or_name(model_name: str) -> str:
    """Resolve `model_name` to its fully-cached local HF snapshot path, if any.

    FlagEmbedding's constructors call snapshot_download() on every init, which
    revalidates etags against Hugging Face even when every file is already on
    disk. On a throttled/blocked network those requests can hang with no
    timeout — wedging Sage's first query and the test suite. Handing the
    constructor a local *path* skips the hub entirely. Falls back to the plain
    name when the model isn't cached; with HF_HUB_OFFLINE=1 (set in config.py,
    Orb is strictly offline) that fails fast instead of downloading, and search
    degrades to lexical-only until the model is installed.
    """
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(model_name, local_files_only=True)
    except Exception:
        return model_name


def _get_embedder():
    global _embed_model
    # Models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record): the
    # embedder is never loaded. A RuntimeError, like the isolation guard
    # below, so search degrades to lexical only.
    from services.model_switch import refuse_when_models_off
    refuse_when_models_off("knowledge embedder")
    if _embed_model is not None:
        return _embed_model
    # Guard against reintroducing the GIL-starvation bug: with isolation on,
    # the ~2 GB embedder must NEVER be constructed in the API process. Failing
    # loudly here turns an accidental in-process load into a caught error
    # (search degrades to lexical-only) instead of a minutes-long API freeze.
    if _use_isolated():
        raise RuntimeError(
            "BUG: in-process embedder load attempted while knowledge isolation "
            "is enabled. Route through services.knowledge_worker instead"
        )
    with _MODEL_LOCK:
        # Double-checked: the construction below takes seconds and ~2 GB, so a
        # second thread arriving mid-load must wait for it rather than start its own.
        if _embed_model is None:
            # On constrained hardware / fast-boot demos, ORB_SKIP_KNOWLEDGE_BASE must ALSO skip the
            # ~2GB BGE-M3 load at query time (not just at init) — otherwise the first Sage query
            # silently loads 2GB and stalls. Use the zero-vector mock so Sage runs LLM-only.
            if os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes"):
                class MockEmbedder:
                    def encode(self, texts, **kwargs):
                        return {"dense_vecs": [np.zeros(EMBEDDING_DIM, dtype=np.float32) for _ in texts]}
                _embed_model = MockEmbedder()
                print("  ⏭  Clinical knowledge embedder skipped (ORB_SKIP_KNOWLEDGE_BASE) — Sage runs LLM-only")
                return _embed_model
            try:
                from FlagEmbedding import BGEM3FlagModel
                _resolved = _local_snapshot_or_name(EMBEDDING_MODEL)
                try:
                    _embed_model = BGEM3FlagModel(_resolved, use_fp16=True)
                except Exception:
                    if _resolved == EMBEDDING_MODEL:
                        raise
                    _embed_model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=True)
                print("  ✓ Clinical knowledge embedder loaded")
            except ImportError:
                # Fallback mock for environments without FlagEmbedding but where schema init is needed
                class MockEmbedder:
                    def encode(self, texts, **kwargs):
                        return {"dense_vecs": [np.zeros(EMBEDDING_DIM, dtype=np.float32) for _ in texts]}
                _embed_model = MockEmbedder()
                print("  ! Clinical knowledge embedder mocked (FlagEmbedding missing)")
    return _embed_model


def _embed_text(text: str) -> bytes:
    """Embed a single text string and return as bytes for sqlite-vec."""
    return _embed_texts([text])[0]


def _embed_texts(texts: list) -> list:
    """Embed multiple texts; list of float32 byte blobs.

    Default path runs in the isolated knowledge worker (separate process, own
    GIL). A KnowledgeWorkerError propagates to callers, all of which already
    degrade: search_guidelines → lexical-only retrieval, init_knowledge_base →
    logged, non-fatal. The in-process path serves ORB_KNOWLEDGE_INPROCESS=1
    and the worker process itself.
    """
    if _use_isolated():
        # Skip-mode must not spin up a 2 GB worker just to fabricate zeros.
        if os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes"):
            return [np.zeros(EMBEDDING_DIM, dtype=np.float32).tobytes() for _ in texts]
        return embed_texts_isolated(
            texts, EMBEDDING_MODEL, batch_size=8, max_length=512
        )
    model = _get_embedder()
    with _MODEL_LOCK:
        embeddings = model.encode(texts, batch_size=8, max_length=512)["dense_vecs"]
    return [np.array(e, dtype=np.float32).tobytes() for e in embeddings]


# ═════════════════════════════════════════════════════════════════
# CLINICAL DOCUMENTS CORPUS
# ═════════════════════════════════════════════════════════════════

CLINICAL_DOCUMENTS = [
    # ── DEMO LOCAL SOPs (Trust Tier 1) ──
    {
        "document_id": "SOP-INF-001",
        "category": "Infection Control",
        "title": "Orb General Hospital Infection Control Policy",
        "source": "Orb General Hospital Operations Manual",
        "source_type": "hospital_sop",
        "source_origin": "demo_local",
        "trust_tier": 1,
        "specialty": "Infectious Disease",
        "effective_date": "2025-01-15",
        "version": "v2.1",
        "is_synthetic": True,
        "content": "All clinical staff must adhere to the 5 Moments for Hand Hygiene as defined by the WHO. Additionally, for any patient with suspected or confirmed Clostridioides difficile (C. diff) infection, soap and water handwashing is mandatory; alcohol-based hand rub is insufficient as it does not eliminate spores. Contact precautions, including gown and gloves, must be donned prior to entering the room and removed before exiting. Equipment must be disinfected with bleach-based wipes (sporicidal). Patients should be isolated in a single room with a dedicated bathroom whenever possible. Isolation precautions can only be discontinued after 48 hours of formed stool."
    },
    {
        "document_id": "SOP-CODE-001",
        "category": "Emergency",
        "title": "Code Blue Response Protocol",
        "source": "Orb General Hospital Resuscitation Committee",
        "source_type": "hospital_sop",
        "source_origin": "demo_local",
        "trust_tier": 1,
        "specialty": "Critical Care",
        "effective_date": "2024-11-01",
        "version": "v4.0",
        "is_synthetic": True,
        "content": "Upon activation of a Code Blue, the primary nurse remains with the patient to initiate BLS and apply backboard. The first responding physician acts as the Code Team Leader until the ICU Attending or Critical Care Fellow arrives. The Code Cart contains epinephrine 1mg/10mL prefilled syringes in Drawer 2. Amiodarone 150mg vials are in Drawer 3. For adult cardiac arrest, epinephrine 1mg IV/IO is administered every 3-5 minutes. Defibrillation for VF/pVT starts at 120J on our Zoll biphasic monitors, escalating to 150J, then 200J. Capnography (ETCO2) must be connected to the monitor once an advanced airway is placed to assess CPR quality and ROSC."
    },
    
    # ── DEMO LOCAL FORMULARY (Trust Tier 2) ──
    {
        "document_id": "FORM-ID-001",
        "category": "Antibiotics",
        "title": "Empiric Antibiotic Guidelines - Sepsis",
        "source": "Orb General Hospital Antibiogram",
        "source_type": "local_formulary",
        "source_origin": "demo_local",
        "trust_tier": 2,
        "specialty": "Infectious Disease",
        "effective_date": "2026-01-01",
        "version": "2026",
        "is_synthetic": True,
        "content": "Due to high local rates of extended-spectrum beta-lactamase (ESBL) producing E. coli (currently >25% in the community), empiric coverage for sepsis of unknown origin or suspected urinary/intra-abdominal source should default to Piperacillin-Tazobactam 4.5g IV q6h or Meropenem 1g IV q8h. Ceftriaxone is NO LONGER recommended for empiric severe sepsis coverage at this institution. For suspected MRSA (prior MRSA infection, IV drug use, indwelling lines), add Vancomycin 15-20 mg/kg IV q8-12h targeting a trough of 15-20 mcg/mL."
    },

    # ── EXTERNAL GUIDELINES (Trust Tier 3) ──
    {
        "document_id": "EXT-SEPSIS-001",
        "category": "Sepsis",
        "title": "Sepsis Early Recognition & Screening",
        "source": "Surviving Sepsis Campaign (SSC) 2021",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Critical Care",
        "effective_date": "2021-10-01",
        "version": "2021",
        "is_synthetic": False,
        "content": "Screen for sepsis using qSOFA (quick Sequential Organ Failure Assessment): Respiratory rate >= 22/min, altered mentation (GCS < 15), systolic BP <= 100 mmHg. Two or more criteria suggest organ dysfunction. Full SOFA scoring should follow for ICU patients. Lactate > 2 mmol/L is a key biomarker of tissue hypoperfusion. Early recognition is critical: for every hour delay in antibiotic administration, mortality increases by approximately 7.6%."
    },
    {
        "document_id": "EXT-SEPSIS-002",
        "category": "Sepsis",
        "title": "Sepsis 1-Hour Bundle",
        "source": "Surviving Sepsis Campaign (SSC) 2021",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Critical Care",
        "effective_date": "2021-10-01",
        "version": "2021",
        "is_synthetic": False,
        "content": "The SSC 1-hour bundle mandates within 1 hour of sepsis recognition: 1) Measure lactate level (re-measure if initial lactate > 2 mmol/L). 2) Obtain blood cultures BEFORE administering antibiotics. 3) Administer broad-spectrum antibiotics. 4) Begin rapid administration of 30 mL/kg crystalloid for hypotension or lactate >= 4 mmol/L. 5) Apply vasopressors if patient is hypotensive during or after fluid resuscitation to maintain MAP >= 65 mmHg."
    },
    {
        "document_id": "EXT-SEPSIS-003",
        "category": "Sepsis",
        "title": "Sepsis Fluid Resuscitation & Vasopressors",
        "source": "Surviving Sepsis Campaign (SSC) 2021",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Critical Care",
        "effective_date": "2021-10-01",
        "version": "2021",
        "is_synthetic": False,
        "content": "Initial resuscitation: 30 mL/kg IV crystalloid (balanced crystalloid preferred over normal saline) within the first 3 hours for sepsis-induced hypoperfusion. Reassess fluid responsiveness with dynamic measures (passive leg raise, pulse pressure variation). First-line vasopressor: Norepinephrine titrated to MAP >= 65 mmHg. Add vasopressin (up to 0.03 U/min) as second-line. Dobutamine for myocardial dysfunction with persistent hypoperfusion despite adequate volume and MAP."
    },
    {
        "document_id": "EXT-CARD-001",
        "category": "Cardiac",
        "title": "STEMI Management Protocol",
        "source": "AHA/ACC 2023 STEMI Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Cardiology",
        "effective_date": "2023-01-01",
        "version": "2023",
        "is_synthetic": False,
        "content": "STEMI diagnosis: ST elevation >= 1mm in 2 contiguous leads (>= 2mm in V1-V3 for men, >= 1.5mm for women). Door-to-balloon time target: < 90 minutes for primary PCI. If PCI not available within 120 minutes, administer fibrinolytic therapy within 30 minutes. Administer aspirin 325 mg (chewed), P2Y12 inhibitor (ticagrelor 180mg or clopidogrel 600mg), and unfractionated heparin. Morphine only for refractory pain (may delay P2Y12 absorption). Beta-blockers within 24 hours if no contraindications."
    },
    {
        "document_id": "EXT-RESP-001",
        "category": "Respiratory",
        "title": "ARDS Berlin Criteria & Management",
        "source": "ARDS Network / Berlin Definition 2012",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Pulmonology",
        "effective_date": "2012-06-01",
        "version": "2012",
        "is_synthetic": False,
        "content": "ARDS Berlin criteria: 1) Acute onset within 1 week. 2) Bilateral opacities on CXR not fully explained by effusions/atelectasis. 3) Respiratory failure not fully explained by cardiac failure. 4) PaO2/FiO2 ratio: Mild 200-300, Moderate 100-200, Severe < 100 (with PEEP >= 5). Management: Low tidal volume ventilation (6 mL/kg predicted body weight), plateau pressure < 30 cmH2O. Prone positioning for 12-16 hrs/day if P/F < 150. Conservative fluid strategy. Consider neuromuscular blockade for severe ARDS (P/F < 150) in first 48 hours."
    },
    {
        "document_id": "EXT-METAB-001",
        "category": "Metabolic",
        "title": "Diabetic Ketoacidosis (DKA) Protocol",
        "source": "ADA Standards of Care 2024",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Endocrinology",
        "effective_date": "2024-01-01",
        "version": "2024",
        "is_synthetic": False,
        "content": "DKA diagnostic criteria: Blood glucose > 250 mg/dL, arterial pH < 7.3, serum bicarbonate < 18 mEq/L, ketonemia or ketonuria. Treatment: 1) IV fluid resuscitation with 0.9% NaCl 1-1.5 L/hr for first 1-2 hours, then 250-500 mL/hr. Switch to 0.45% NaCl when Na is normal or elevated. 2) Regular insulin 0.1-0.14 U/kg/hr continuous infusion. 3) Add dextrose (D5) when glucose < 200 mg/dL. 4) Potassium replacement: if K+ < 3.3 hold insulin and replace aggressively; 3.3-5.3 give 20-30 mEq/L in each liter of IV fluid; > 5.3 recheck q2h. 5) Bicarbonate only if pH < 6.9."
    },
    # ── ADDITIONAL EXTERNAL GUIDELINES ──
    {
        "document_id": "EXT-STROKE-001",
        "category": "Neurology",
        "title": "Acute Ischemic Stroke: IV tPA Protocol",
        "source": "AHA/ASA 2019 Stroke Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Neurology",
        "effective_date": "2019-01-01",
        "version": "2019",
        "is_synthetic": False,
        "content": "IV alteplase (tPA) 0.9 mg/kg (max 90 mg): 10% as bolus over 1 minute, remainder over 60 minutes. Eligibility: onset within 4.5 hours, no contraindications (recent surgery, active bleeding, INR > 1.7, platelets < 100k, glucose < 50). BP must be < 185/110 before and < 180/105 for 24h after. CT head must exclude hemorrhage. NIHSS should be documented. Door-to-needle target: < 60 minutes. For large vessel occlusion (LVO), consider mechanical thrombectomy within 24 hours if eligible per imaging."
    },
    {
        "document_id": "EXT-CARD-002",
        "category": "Cardiac",
        "title": "Acute Heart Failure Management",
        "source": "AHA/ACC 2022 Heart Failure Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Cardiology",
        "effective_date": "2022-01-01",
        "version": "2022",
        "is_synthetic": False,
        "content": "Acute decompensated heart failure: 1) IV furosemide 40-80mg (or 1-2x home dose) for volume overload. Target urine output 0.5-1 mL/kg/hr. 2) Nitroglycerin IV if SBP > 110 for afterload reduction. 3) Avoid NSAIDs, CCBs (verapamil/diltiazem) in HFrEF. 4) If cardiogenic shock: dobutamine or milrinone; consider mechanical support (IABP, Impella). 5) Continue GDMT (beta-blocker, ACEi/ARB/ARNI, MRA, SGLT2i) unless hemodynamically unstable. 6) Daily weights, strict I/O, BMP and BNP trending."
    },
    {
        "document_id": "EXT-ID-001",
        "category": "Antibiotics",
        "title": "Community-Acquired Pneumonia (CAP) Treatment",
        "source": "ATS/IDSA 2019 CAP Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Infectious Disease",
        "effective_date": "2019-10-01",
        "version": "2019",
        "is_synthetic": False,
        "content": "Outpatient CAP: amoxicillin 1g TID or doxycycline 100mg BID or azithromycin 500mg day 1 then 250mg (if local resistance < 25%). Inpatient non-ICU: beta-lactam (ampicillin-sulbactam 3g q6h or ceftriaxone 1-2g daily) + macrolide (azithromycin) or respiratory fluoroquinolone alone (levofloxacin 750mg daily). Inpatient ICU: beta-lactam + macrolide or beta-lactam + respiratory fluoroquinolone. If MRSA risk: add vancomycin or linezolid. If Pseudomonas risk: piperacillin-tazobactam or cefepime."
    },
    {
        "document_id": "EXT-ID-002",
        "category": "Antibiotics",
        "title": "Urinary Tract Infection (UTI) Treatment",
        "source": "IDSA 2010 / EAU 2024 UTI Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Infectious Disease",
        "effective_date": "2024-01-01",
        "version": "2024",
        "is_synthetic": False,
        "content": "Uncomplicated cystitis: nitrofurantoin 100mg BID x 5 days (first-line) or TMP-SMX 160/800mg BID x 3 days (if local resistance < 20%) or fosfomycin 3g single dose. Avoid fluoroquinolones for uncomplicated UTI. Complicated UTI / pyelonephritis: ceftriaxone 1g IV daily or fluoroquinolone (ciprofloxacin 400mg IV q12h). ESBL-producing organisms: ertapenem or meropenem based on susceptibility. Catheter-associated UTI: treat only if symptomatic; remove or replace catheter before starting antibiotics."
    },
    {
        "document_id": "EXT-VTE-001",
        "category": "Hematology",
        "title": "DVT/VTE Prophylaxis",
        "source": "CHEST 2012 / ASH 2018 VTE Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Hematology",
        "effective_date": "2018-01-01",
        "version": "2018",
        "is_synthetic": False,
        "content": "Medical patients: enoxaparin 40mg SC daily or heparin 5000U SC q8-12h for Padua score >= 4. Surgical patients: enoxaparin 40mg SC daily starting 6-12h postop or heparin 5000U SC q8h. Mechanical prophylaxis (IPC) if pharmacologic contraindicated. Duration: throughout hospitalization; extended (up to 35 days) for high-risk surgeries (hip/knee arthroplasty, abdominal cancer surgery). Contraindications: active bleeding, severe thrombocytopenia (plt < 25k), recent CNS surgery."
    },
    {
        "document_id": "EXT-LYTES-001",
        "category": "Metabolic",
        "title": "Potassium Correction Protocol",
        "source": "WHO / UpToDate Clinical Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Internal Medicine",
        "effective_date": "2024-01-01",
        "version": "2024",
        "is_synthetic": False,
        "content": "Hypokalemia: Mild (3.0-3.4): oral KCl 40-80 mEq/day in divided doses. Moderate (2.5-2.9): IV KCl 10-20 mEq/hr via central line (max 40 mEq/hr in emergencies), continuous telemetry required. Severe (< 2.5): aggressive IV replacement with telemetry, check Mg (correct hypomagnesemia first). Hyperkalemia: Mild (5.5-6.0): kayexalate, dietary restriction. Moderate (6.0-6.5): IV insulin 10U + D50, calcium gluconate if ECG changes. Severe (> 6.5 or ECG changes): calcium gluconate 1g IV stat, insulin/D50, consider dialysis. Recheck K+ q2-4h during correction."
    },
    {
        "document_id": "EXT-VENT-001",
        "category": "Respiratory",
        "title": "Ventilator Weaning Protocol",
        "source": "ATS/ACCP Evidence-Based Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Pulmonology",
        "effective_date": "2017-01-01",
        "version": "2017",
        "is_synthetic": False,
        "content": "Daily readiness assessment: 1) FiO2 <= 0.4, PEEP <= 8, adequate oxygenation. 2) Hemodynamically stable (no/low-dose vasopressors). 3) Patient can initiate breaths. 4) No continuous sedation. Spontaneous Breathing Trial (SBT): T-piece or PS 5-8/PEEP 5 for 30-120 minutes. Pass criteria: RR < 35, SpO2 > 90%, HR < 140, no distress. Rapid Shallow Breathing Index (RSBI) < 105 predicts success. If SBT passes, assess cuff leak and airway patency before extubation. Post-extubation: prophylactic NIV for high-risk patients."
    },
    {
        "document_id": "EXT-TRANS-001",
        "category": "Hematology",
        "title": "Blood Transfusion Thresholds",
        "source": "AABB 2016 Transfusion Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Hematology",
        "effective_date": "2016-10-01",
        "version": "2016",
        "is_synthetic": False,
        "content": "Restrictive strategy recommended: transfuse pRBC if Hgb < 7 g/dL for hemodynamically stable patients (including ICU). Liberal threshold Hgb < 8 g/dL for cardiac surgery and acute coronary syndrome. Platelets: transfuse if < 10k (prophylactic) or < 50k with active bleeding or planned procedure; < 100k for neurosurgery. FFP: for active bleeding with INR > 1.5 or massive transfusion protocol. Massive transfusion: 1:1:1 ratio pRBC:FFP:platelets. Type and screen before all transfusions; crossmatch for non-emergent."
    },
    {
        "document_id": "EXT-SEDATION-001",
        "category": "Critical Care",
        "title": "ICU Sedation & Delirium (PADIS)",
        "source": "SCCM 2018 PADIS Guidelines",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Critical Care",
        "effective_date": "2018-09-01",
        "version": "2018",
        "is_synthetic": False,
        "content": "Pain: assess with BPS or CPOT q4h. First-line: IV acetaminophen, low-dose opioids (fentanyl preferred for short-acting). Agitation: target light sedation (RASS 0 to -1). First-line: propofol or dexmedetomidine (preferred over benzodiazepines which increase delirium risk). Delirium: screen with CAM-ICU q12h. Non-pharmacologic prevention: early mobilization, sleep hygiene, reorientation, minimize benzodiazepines, avoid physical restraints. Pharmacologic: avoid haloperidol for prevention; consider low-dose haloperidol or atypical antipsychotics for symptomatic hyperactive delirium only."
    },
    {
        "document_id": "EXT-SEPSIS-004",
        "category": "Sepsis",
        "title": "Septic Shock: Corticosteroid Use",
        "source": "Surviving Sepsis Campaign (SSC) 2021",
        "source_type": "external_guideline",
        "source_origin": "external",
        "trust_tier": 3,
        "specialty": "Critical Care",
        "effective_date": "2021-10-01",
        "version": "2021",
        "is_synthetic": False,
        "content": "IV hydrocortisone 200mg/day (50mg q6h or 200mg continuous infusion) is suggested for adults with septic shock who require ongoing vasopressor therapy despite adequate fluid resuscitation. Start after vasopressor initiation if MAP remains < 65 despite norepinephrine >= 0.25 mcg/kg/min for > 4 hours. Taper gradually (not abrupt discontinuation). Do NOT use the ACTH stimulation test to guide therapy. Fludrocortisone 50mcg daily may be added (APROCCHSS trial). Monitor for hyperglycemia and hypernatremia."
    },
]


# ─── Query Expansion for Medical Abbreviations ───

MEDICAL_ABBREVIATIONS = {
    "dka": "diabetic ketoacidosis",
    "stemi": "ST elevation myocardial infarction",
    "nstemi": "non-ST elevation myocardial infarction",
    "acs": "acute coronary syndrome",
    "cap": "community acquired pneumonia",
    "hap": "hospital acquired pneumonia",
    "vap": "ventilator associated pneumonia",
    "uti": "urinary tract infection",
    "dvt": "deep vein thrombosis",
    "vte": "venous thromboembolism",
    "pe": "pulmonary embolism",
    "ards": "acute respiratory distress syndrome",
    "aki": "acute kidney injury",
    "chf": "congestive heart failure",
    "hf": "heart failure",
    "afib": "atrial fibrillation",
    "icp": "intracranial pressure",
    "tbi": "traumatic brain injury",
    "sbt": "spontaneous breathing trial",
    "rsi": "rapid sequence intubation",
    "map": "mean arterial pressure",
    "prbc": "packed red blood cells",
    "ffp": "fresh frozen plasma",
    "tpa": "alteplase tissue plasminogen activator",
    "bls": "basic life support",
    "acls": "advanced cardiac life support",
    "rosc": "return of spontaneous circulation",
    "etco2": "end tidal carbon dioxide",
    "rrt": "renal replacement therapy rapid response team",
    "niv": "non-invasive ventilation",
    "bipap": "bilevel positive airway pressure",
    "cpap": "continuous positive airway pressure",
    "esbl": "extended spectrum beta-lactamase",
    "mrsa": "methicillin-resistant staphylococcus aureus",
    "c diff": "clostridioides difficile",
    "c. diff": "clostridioides difficile",
}

DRUG_ALIASES = {
    "lasix": "furosemide",
    "flagyl": "metronidazole",
    "zosyn": "piperacillin tazobactam",
    "vanco": "vancomycin",
    "levo": "levofloxacin norepinephrine",
    "norepi": "norepinephrine",
    "epi": "epinephrine",
    "neo": "phenylephrine neosynephrine",
    "versed": "midazolam",
    "precedex": "dexmedetomidine",
    "diprivan": "propofol",
    "rocuronium": "rocuronium neuromuscular blockade",
    "sux": "succinylcholine",
    "lovenox": "enoxaparin",
    "coumadin": "warfarin",
    "tylenol": "acetaminophen paracetamol",
    "advil": "ibuprofen",
    "dilaudid": "hydromorphone",
}


def _expand_query(query: str) -> str:
    """Expand medical abbreviations and drug aliases for better FTS5 recall."""
    q_lower = query.lower()
    expansions = []
    
    for abbrev, expansion in MEDICAL_ABBREVIATIONS.items():
        if re.search(r'\b' + re.escape(abbrev) + r'\b', q_lower):
            expansions.append(expansion)
    
    for alias, generic in DRUG_ALIASES.items():
        if re.search(r'\b' + re.escape(alias) + r'\b', q_lower):
            expansions.append(generic)
    
    if expansions:
        return query + " " + " ".join(expansions)
    return query


def chunk_text(text: str, words_per_chunk: int = 150, overlap: int = 50) -> List[str]:
    """Splits text into chunks of `words_per_chunk` with `overlap`."""
    words = text.split()
    chunks = []
    if len(words) <= words_per_chunk:
        return [text]
        
    start = 0
    while start < len(words):
        end = start + words_per_chunk
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        start += (words_per_chunk - overlap)
    return chunks


def _corpus_fingerprint() -> str:
    """Stable fingerprint of the corpus + embedding config.

    If this is unchanged since the last successful build, the vector store is
    already current and we can skip the expensive DROP + re-embed on boot.
    """
    h = hashlib.sha256()
    h.update(EMBEDDING_MODEL.encode("utf-8"))
    h.update(str(EMBEDDING_DIM).encode("utf-8"))
    # Hash the full corpus content deterministically.
    payload = json.dumps(CLINICAL_DOCUMENTS, sort_keys=True, default=str)
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


def init_knowledge_base():
    """Initializes the RAG knowledge base. Runs on app startup.

    Set ORB_SKIP_KNOWLEDGE_BASE=1 to skip (fast boots / demo before the BGE-M3
    embedding model is downloaded). Sage RAG retrieval is disabled when skipped.

    The vector store is only DROPped and re-embedded when the corpus fingerprint
    (corpus content + embedder id + dim) changes. On an unchanged corpus the
    rebuild is skipped, making boots fast/deterministic and preserving any
    knowledge already embedded (instead of wiping it every restart).
    """
    if os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes"):
        print("  ⏭  Knowledge base init skipped (ORB_SKIP_KNOWLEDGE_BASE)")
        return
    # Models switched off (ORB_MODELS_ENABLED, or the Orb Ward Record): nothing
    # is embedded, so no embedder or knowledge worker starts.
    from services.model_switch import models_enabled
    if not models_enabled():
        return

    fingerprint = _corpus_fingerprint()

    # db_session() guarantees the connection is closed/rolled back — the previous
    # `with get_db() as db:` only managed the transaction and LEAKED the
    # connection (sqlite3's context manager does not call close()).
    with db_session() as db:
        cursor = db.cursor()

        # Fast-path: if the corpus is unchanged AND the tables already exist with
        # data, skip the rebuild entirely.
        try:
            meta_exists = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='clinical_knowledge_meta'"
            ).fetchone()
            kb_exists = cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='clinical_knowledge'"
            ).fetchone()
            if meta_exists and kb_exists:
                stored = cursor.execute(
                    "SELECT value FROM clinical_knowledge_meta WHERE key = 'corpus_fingerprint'"
                ).fetchone()
                row_count = cursor.execute("SELECT COUNT(*) FROM clinical_knowledge").fetchone()
                if stored and stored[0] == fingerprint and row_count and row_count[0] > 0:
                    print("  ✓ Knowledge base up-to-date (corpus unchanged) — skipping rebuild")
                    return
        except Exception as e:
            # If the check fails for any reason, fall through and rebuild.
            print(f"  ! Knowledge base fingerprint check failed ({e}); rebuilding")

        # Metadata table (persists the fingerprint across boots).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clinical_knowledge_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # Corpus changed (or first run) — drop and rebuild.
        try:
            cursor.execute("DROP TABLE IF EXISTS clinical_knowledge_vec")
            cursor.execute("DROP TABLE IF EXISTS clinical_knowledge_fts")
            cursor.execute("DROP TABLE IF EXISTS clinical_knowledge")
        except Exception:
            pass

        # 1. Create Core Metadata Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clinical_knowledge (
                id INTEGER PRIMARY KEY,
                document_id TEXT,
                chunk_index INTEGER,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'external_guideline',
                source_origin TEXT NOT NULL DEFAULT 'external',
                trust_tier INTEGER NOT NULL DEFAULT 3,
                specialty TEXT,
                effective_date TEXT,
                version TEXT,
                is_synthetic BOOLEAN DEFAULT 0
            )
        ''')
        
        # 2. Check FTS5 availability and create index if possible
        fts_available = False
        try:
            db.enable_load_extension(True)
            cursor.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS clinical_knowledge_fts USING fts5(
                    title, content, source, category, specialty,
                    content=clinical_knowledge, content_rowid=id
                )
            ''')
            fts_available = True
        except Exception as e:
            print(f"  ! FTS5 not available: {e}. Lexical search will fallback to LIKE.")

        # 3. Create sqlite-vec extension tables
        try:
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            cursor.execute(f'''
                CREATE VIRTUAL TABLE IF NOT EXISTS clinical_knowledge_vec USING vec0(
                    embedding float[{EMBEDDING_DIM}]
                )
            ''')
        except Exception as e:
            print(f"  ! Error loading sqlite-vec: {e}")
            return
            
        print("  ! Initializing clinical knowledge base (chunking & embedding...)")
        
        # 5. Ingest & Embed
        texts_to_embed = []
        metadata_to_insert = []
        
        for doc in CLINICAL_DOCUMENTS:
            chunks = chunk_text(doc["content"], words_per_chunk=150, overlap=50)
            for idx, chunk in enumerate(chunks):
                # We embed a composite text to give the vector more context
                composite_text = f"Title: {doc['title']}\nCategory: {doc['category']}\nSource: {doc['source']}\nContent: {chunk}"
                texts_to_embed.append(composite_text)
                
                metadata_to_insert.append({
                    "document_id": doc.get("document_id"),
                    "chunk_index": idx,
                    "category": doc["category"],
                    "title": doc["title"],
                    "content": chunk,
                    "source": doc["source"],
                    "source_type": doc["source_type"],
                    "source_origin": doc["source_origin"],
                    "trust_tier": doc["trust_tier"],
                    "specialty": doc.get("specialty"),
                    "effective_date": doc.get("effective_date"),
                    "version": doc.get("version"),
                    "is_synthetic": doc.get("is_synthetic", False)
                })
        
        # Batch embed
        try:
            embeddings = _embed_texts(texts_to_embed)
            
            for meta, emb in zip(metadata_to_insert, embeddings):
                # Insert metadata
                cursor.execute('''
                    INSERT INTO clinical_knowledge 
                    (document_id, chunk_index, category, title, content, source, source_type, 
                     source_origin, trust_tier, specialty, effective_date, version, is_synthetic)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    meta["document_id"], meta["chunk_index"], meta["category"], meta["title"], 
                    meta["content"], meta["source"], meta["source_type"], meta["source_origin"],
                    meta["trust_tier"], meta["specialty"], meta["effective_date"], meta["version"], 
                    meta["is_synthetic"]
                ))
                
                rowid = cursor.lastrowid
                
                # Insert vector
                cursor.execute('''
                    INSERT INTO clinical_knowledge_vec (rowid, embedding)
                    VALUES (?, ?)
                ''', (rowid, emb))
                
                # Insert FTS
                if fts_available:
                    cursor.execute('''
                        INSERT INTO clinical_knowledge_fts (rowid, title, content, source, category, specialty)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (rowid, meta["title"], meta["content"], meta["source"], meta["category"], meta["specialty"]))
                    
            # Record the corpus fingerprint so the next boot can skip the rebuild.
            cursor.execute(
                "INSERT OR REPLACE INTO clinical_knowledge_meta (key, value) VALUES ('corpus_fingerprint', ?)",
                (fingerprint,),
            )
            db.commit()
            print(f"  ✓ Knowledge base populated with {len(metadata_to_insert)} chunks")
        except Exception as e:
            db.rollback()
            print(f"  ! Error populating knowledge base: {e}")


# ═════════════════════════════════════════════════════════════════
# RERANKING
# ═════════════════════════════════════════════════════════════════
#
# The fused (RRF) candidate list from hybrid retrieval is re-ordered by a
# second-stage reranker before the top-`limit` are returned. Two paths:
#
#   1. Cross-encoder (preferred, if the model is installed) — a query/passage
#      relevance model (sentence-transformers CrossEncoder or FlagEmbedding
#      FlagReranker). Gated behind availability; NEVER loads a 2GB model just to
#      check, and degrades silently to (2) when absent.
#   2. Lexical/heuristic reranker (always available, used today) — scores each
#      candidate on term coverage + BM25-style lexical overlap saturation +
#      trust-tier weighting, blended with the retrieval fusion prior.
#
# Reranking is deterministic, dependency-free on path (2), and never raises: a
# failure falls back to the pre-rerank order so retrieval never breaks.

# trust_tier -> multiplicative weight (tier 1 = local hospital SOP, most trusted).
_TRUST_TIER_WEIGHT = {1: 1.00, 2: 0.92, 3: 0.85}
_DEFAULT_TRUST_WEIGHT = 0.85

# Small, high-frequency words that carry little retrieval signal.
_RERANK_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "is",
    "are", "was", "be", "by", "at", "as", "if", "it", "this", "that", "what",
    "how", "when", "should", "management", "patient", "patients", "use", "used",
})

# Blend weights for the lexical reranker's final score.
_W_COVERAGE = 0.45      # fraction of query terms present in the candidate
_W_OVERLAP = 0.30       # BM25-style term-frequency saturation
_W_TRUST = 0.15         # trust-tier weighting
_W_PRIOR = 0.10         # normalized retrieval (RRF) fusion prior

_cross_encoder = None
_cross_encoder_checked = False


def _cross_encoder_disabled() -> bool:
    """Explicit cross-encoder kill switch (ORB_SAGE_CROSS_ENCODER=0).

    The lexical reranker is not merely a fallback for machines without the
    weights — it is a SHIPPING path (any appliance where the reranker fails to
    load runs on it). Its relevance floor therefore has to be calibratable on a
    machine that DOES have the model, which needs a way to turn the model off.
    """
    return os.getenv("ORB_SAGE_CROSS_ENCODER", "").strip().lower() in (
        "0", "off", "false", "no")


def _rerank_terms(text: str) -> List[str]:
    """Tokenize into lowercase content terms (drop punctuation, stopwords, tiny words)."""
    terms = []
    for raw in re.split(r"[^a-zA-Z0-9]+", text.lower()):
        if len(raw) > 2 and raw not in _RERANK_STOPWORDS:
            terms.append(raw)
    return terms


def _get_cross_encoder():
    """Return a cross-encoder reranker if one is installed, else None.

    Best-effort and cached. Availability is gated so the lexical path is used
    whenever a cross-encoder is not present (the case in the dev venv).
    """
    global _cross_encoder, _cross_encoder_checked
    # Models switched off: no reranker is loaded; the lexical path ranks.
    from services.model_switch import models_enabled
    if not models_enabled():
        return None
    if _cross_encoder_checked:
        return _cross_encoder
    # Same in-process guard as the embedder: with isolation on, the reranker
    # lives in the knowledge worker; loading it here would re-wedge the API.
    if _use_isolated():
        raise RuntimeError(
            "BUG: in-process reranker load attempted while knowledge isolation "
            "is enabled. Route through services.knowledge_worker instead"
        )

    with _MODEL_LOCK:
        # Double-checked, and the "checked" flag is only raised once the load has
        # finished (in the finally below). Raising it up-front let a thread that
        # arrived mid-load read _cross_encoder as None and silently degrade that
        # query to lexical ranking.
        if _cross_encoder_checked:
            return _cross_encoder
        try:
            # Never attempt heavy model loads on constrained/fast-boot profiles.
            if os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes"):
                _cross_encoder = None
                return None

            if _cross_encoder_disabled():
                _cross_encoder = None
                return None

            model_name = os.getenv("SAGE_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
            # Try FlagEmbedding's FlagReranker first (pairs with the BGE-M3 embedder), then
            # sentence-transformers CrossEncoder. Only import when the package is present.
            try:
                import importlib.util
                if importlib.util.find_spec("FlagEmbedding") is not None:
                    from FlagEmbedding import FlagReranker  # type: ignore
                    _resolved = _local_snapshot_or_name(model_name)
                    try:
                        reranker = FlagReranker(_resolved, use_fp16=True)
                    except Exception:
                        if _resolved == model_name:
                            raise
                        reranker = FlagReranker(model_name, use_fp16=True)

                    def _score(pairs):
                        out = reranker.compute_score(pairs, normalize=True)
                        return out if isinstance(out, list) else [out]

                    _cross_encoder = _score
                    print("  ✓ Sage cross-encoder reranker loaded (FlagReranker)")
                    return _cross_encoder
                if importlib.util.find_spec("sentence_transformers") is not None:
                    from sentence_transformers import CrossEncoder  # type: ignore
                    ce = CrossEncoder(_local_snapshot_or_name(model_name))

                    def _score(pairs):
                        return list(ce.predict(pairs))

                    _cross_encoder = _score
                    print("  ✓ Sage cross-encoder reranker loaded (CrossEncoder)")
                    return _cross_encoder
            except Exception as e:  # pragma: no cover - depends on optional heavy deps
                print(f"  ! Cross-encoder reranker unavailable ({e}); using lexical reranker")

            _cross_encoder = None
            return None
        finally:
            _cross_encoder_checked = True


def _lexical_rerank_score(query_terms: List[str], candidate: Dict[str, Any],
                          max_prior: float) -> float:
    """Heuristic relevance score for one candidate. Higher is better.

    Combines:
      * term coverage — fraction of DISTINCT query terms present in the candidate,
      * lexical overlap — BM25-style tf saturation summed over query terms,
      * trust-tier weight — favor locally-governed / higher-trust sources,
      * fusion prior — the normalized RRF score from hybrid retrieval.
    """
    if not query_terms:
        return _TRUST_TIER_WEIGHT.get(candidate.get("trust_tier"), _DEFAULT_TRUST_WEIGHT)

    text = f"{candidate.get('title', '')} {candidate.get('content', '')}"
    doc_terms = _rerank_terms(text)
    if not doc_terms:
        coverage = overlap = 0.0
    else:
        tf: Dict[str, int] = {}
        for t in doc_terms:
            tf[t] = tf.get(t, 0) + 1
        distinct_q = set(query_terms)
        matched = sum(1 for t in distinct_q if t in tf)
        coverage = matched / len(distinct_q)

        # BM25-ish tf saturation: tf/(tf+k1), averaged over distinct query terms.
        k1 = 1.5
        sat = 0.0
        for t in distinct_q:
            f = tf.get(t, 0)
            if f:
                sat += f / (f + k1)
        overlap = sat / len(distinct_q)

    trust = _TRUST_TIER_WEIGHT.get(candidate.get("trust_tier"), _DEFAULT_TRUST_WEIGHT)
    prior = 0.0
    if max_prior > 0:
        prior = min(1.0, max(0.0, candidate.get("fusion_score", 0.0) / max_prior))

    return (_W_COVERAGE * coverage + _W_OVERLAP * overlap
            + _W_TRUST * trust + _W_PRIOR * prior)


# ═════════════════════════════════════════════════════════════════
# RELEVANCE (distinct from ORDERING)
# ═════════════════════════════════════════════════════════════════
#
# _lexical_rerank_score above answers "which of these should come first?".
# That is NOT the same question as "does this passage have anything to do with
# the query?", and the difference is exactly the DEF-4 defect: its blend adds
# _W_TRUST * trust + _W_PRIOR * prior = up to 0.25 to a passage that shares NOT
# ONE content term with the query. A ranking score is allowed to carry priors;
# a RELEVANCE score, which decides whether Orb makes a citation claim to a
# clinician, must not — trust tier says how much a source is to be believed,
# never whether it is on topic.
#
# So relevance is scored separately, on a [0, 1] scale, from query↔passage
# evidence ONLY:
#   * cross-encoder path — the normalized reranker output is already
#     P(relevant | query, passage); used verbatim.
#   * lexical path — coverage of the query's content terms plus BM25-style tf
#     saturation, with no trust and no fusion prior.
_W_REL_COVERAGE = 0.60
_W_REL_OVERLAP = 0.40


def _lexical_relevance_score(query_terms: List[str], candidate: Dict[str, Any]) -> float:
    """Pure query↔passage lexical relevance in [0, 1]. No trust, no fusion prior.

    A candidate sharing no content term with the query scores exactly 0.0 —
    which is the property the relevance floor depends on and which the ranking
    blend does not have.
    """
    if not query_terms:
        # No content terms to match on: relevance is unknowable, not zero. The
        # floor must not silently delete the whole corpus for a query like "why?".
        return 1.0
    text = f"{candidate.get('title', '')} {candidate.get('content', '')}"
    doc_terms = _rerank_terms(text)
    if not doc_terms:
        return 0.0
    tf: Dict[str, int] = {}
    for t in doc_terms:
        tf[t] = tf.get(t, 0) + 1
    distinct_q = set(query_terms)
    matched = sum(1 for t in distinct_q if t in tf)
    coverage = matched / len(distinct_q)
    k1 = 1.5
    sat = sum(tf[t] / (tf[t] + k1) for t in distinct_q if t in tf)
    overlap = sat / len(distinct_q)
    return _W_REL_COVERAGE * coverage + _W_REL_OVERLAP * overlap


def rerank_results(query: str, candidates: List[Dict[str, Any]],
                   top_k: Optional[int] = None) -> List[Dict[str, Any]]:
    """Re-order retrieval candidates by second-stage relevance to `query`.

    Uses a cross-encoder when available, otherwise a robust lexical/heuristic
    reranker. Adds `rerank_score` and `rerank_method` to each returned candidate.
    Never raises — on any error the input order is preserved. Returns the top_k
    (or all) candidates in descending relevance.
    """
    if not candidates:
        return candidates

    try:
        expanded = _expand_query(query)
        method = "lexical"
        scores: List[float]

        if _use_isolated():
            # Cross-encoder scoring happens in the knowledge worker process —
            # any failure (worker crash, model absent, skip mode) degrades to
            # the lexical reranker exactly like the legacy in-process path.
            if (os.getenv("ORB_SKIP_KNOWLEDGE_BASE", "").lower() in ("1", "true", "yes")
                    or _cross_encoder_disabled()):
                scores = None  # type: ignore
            else:
                try:
                    passages = [f"{c.get('title', '')}\n{c.get('content', '')}"
                                for c in candidates]
                    scores = rerank_scores_isolated(expanded, passages)
                    method = "cross_encoder"
                except (KnowledgeWorkerError, Exception) as e:  # noqa: BLE001
                    print(f"  ! Isolated reranker unavailable ({e}); using lexical reranker")
                    scores = None  # type: ignore
        else:
            ce = _get_cross_encoder()
            if ce is not None:
                try:
                    pairs = [[expanded, f"{c.get('title', '')}\n{c.get('content', '')}"]
                             for c in candidates]
                    # FlagReranker tokenizes queries and passages at DIFFERENT max
                    # lengths, mutating the shared Rust tokenizer's truncation config
                    # twice per batch — concurrent scoring from the query and warm
                    # paths raises "Already borrowed" or silently truncates wrongly.
                    with _MODEL_LOCK:
                        scores = [float(s) for s in ce(pairs)]
                    method = "cross_encoder"
                except Exception as e:  # pragma: no cover - optional heavy path
                    print(f"  ! Cross-encoder scoring failed ({e}); using lexical reranker")
                    scores = None  # type: ignore
            else:
                scores = None  # type: ignore

        if scores is None:
            q_terms = _rerank_terms(expanded)
            max_prior = max((c.get("fusion_score", 0.0) for c in candidates), default=0.0)
            scores = [_lexical_rerank_score(q_terms, c, max_prior) for c in candidates]

        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
            c["rerank_method"] = method

        # Stable sort: ties keep the original (retrieval-fused) order.
        ordered = sorted(
            range(len(candidates)),
            key=lambda i: (-candidates[i]["rerank_score"], i),
        )
        reranked = [candidates[i] for i in ordered]
    except Exception as e:
        print(f"  ! Reranking failed ({e}); preserving retrieval order")
        reranked = candidates

    return reranked[:top_k] if top_k is not None else reranked


# ═════════════════════════════════════════════════════════════════
# RELEVANCE FLOOR — the "no guideline matched" path
# ═════════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS (DEF-4, seen on the ward 2026-08-14): "Ventilator Weaning
# Protocol" was cited as evidence on every drafted assessment — DKA, dengue,
# CAP-with-sepsis and UTI alike. Retrieval could not say "nothing here is
# relevant", because nothing in the pipeline ever asked:
#
#   * dense retrieval runs `k = 15` unconditionally, so 15 neighbours come back
#     whatever their distance (measured: the nearest chunk to a dengue query sat
#     at cosine distance 0.9963 — orthogonal — and was still returned);
#   * the lexical lane ORs every >3-char token, so one incidental word matches;
#   * RRF fuses on RANK ONLY and throws the similarity away, so `fusion_score`
#     is bounded by 2/(60+1) = 0.0328 and is indistinguishable between a perfect
#     hit ("diabetic ketoacidosis…" → 0.032787) and garbage ("where is the
#     hospital parking garage…" → 0.032522);
#   * so `search_guidelines` could never return [] on a non-empty corpus, and
#     sage_assessment_service's `evidence_note` ("No guideline in Orb's corpus
#     matched this alert") could never fire.
#
# The floor is applied to the RELEVANCE score above, not to the fusion score,
# because relevance is the only signal on a scale where a number means
# something. It is DERIVED, not picked: services/retrieval_calibration.py runs a
# labelled in-corpus/out-of-corpus set through this exact path and cuts at the
# maximum-margin point between the two score distributions
# (server/eval/retrieval_thresholds.json). The fallbacks below are used only
# until a calibration is committed, and each is justified rather than tuned:
#
#   cross-encoder 0.10 — the normalized reranker score IS P(relevant), so 0.10
#     is "worse than 9:1 odds against, do not make a citation claim on it".
#   lexical 0.10 — with _W_REL_COVERAGE = 0.60 this is one content term in six,
#     i.e. the passage must share at least a token of real vocabulary with the
#     query rather than riding a trust-tier prior.
_FALLBACK_MIN_RELEVANCE = {"cross_encoder": 0.10, "lexical": 0.10}


def _relevance_floor(method: str) -> float:
    """Minimum relevance a candidate needs to be shown as evidence.

    Resolution order: ORB_RETRIEVAL_RELEVANCE_FLOOR (ops override — a float, or
    "off"/"none" to disable the floor entirely) → the calibrated threshold in
    server/eval/retrieval_thresholds.json → the justified fallback above.
    """
    override = os.getenv("ORB_RETRIEVAL_RELEVANCE_FLOOR", "").strip().lower()
    if override:
        if override in ("off", "none", "disabled"):
            return 0.0
        try:
            return max(0.0, float(override))
        except ValueError:
            print(f"  ! Ignoring unparseable ORB_RETRIEVAL_RELEVANCE_FLOOR={override!r}")
    try:
        from services.retrieval_calibration import load_thresholds
        key = ("min_relevance_cross_encoder" if method == "cross_encoder"
               else "min_relevance_lexical")
        value = load_thresholds().get(key)
        if value is not None:
            return max(0.0, float(value))
    except Exception as e:  # pragma: no cover - defensive
        print(f"  ! Could not load calibrated relevance floor ({e}); using fallback")
    return _FALLBACK_MIN_RELEVANCE.get(method, _FALLBACK_MIN_RELEVANCE["lexical"])


def score_relevance(query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Annotate each candidate with `relevance_score` + `relevance_method`.

    Self-sufficient: it does NOT require reranking to have run. When the
    cross-encoder already scored a candidate its output is reused (it is a
    strictly better relevance signal and already paid for); otherwise the
    lexical relevance is computed, which is deterministic and model-free.
    """
    q_terms = _rerank_terms(_expand_query(query))
    for c in candidates:
        if c.get("rerank_method") == "cross_encoder" and c.get("rerank_score") is not None:
            c["relevance_score"] = float(c["rerank_score"])
            c["relevance_method"] = "cross_encoder"
        else:
            c["relevance_score"] = _lexical_relevance_score(q_terms, c)
            c["relevance_method"] = "lexical"
    return candidates


def apply_relevance_floor(query: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop candidates that are not relevant enough to be cited to a clinician.

    Returns [] when nothing clears the floor — that empty list IS the honest
    "no guideline in Orb's corpus matched" answer, and callers already handle it
    (sage_service._get_guideline_context → ("", [], "low") → the assessment
    prompt's "RETRIEVED GUIDELINES: none" branch → `evidence_note`).

    Fails OPEN on an unexpected error in the scoring stage itself (retrieval
    keeps working), but a candidate that merely scores low is dropped — a
    tangential citation is worse than no citation.
    """
    if not candidates:
        return candidates
    try:
        scored = score_relevance(query, candidates)
        # Resolved once per call, not per candidate: _relevance_floor reads the
        # env and the calibrated file, and its unparseable-override warning
        # would otherwise print once per result.
        floors = {m: _relevance_floor(m) for m in {c["relevance_method"] for c in scored}}
        return [c for c in scored if c["relevance_score"] >= floors[c["relevance_method"]]]
    except Exception as e:  # pragma: no cover - defensive
        print(f"  ! Relevance floor failed ({e}); returning unfiltered candidates")
        return candidates


# ═════════════════════════════════════════════════════════════════
# SYNTHETIC DOCUMENTS: never retrievable at a real site
# ═════════════════════════════════════════════════════════════════
# The seed corpus carries invented "Orb General Hospital" SOPs and a fictional
# antibiogram (source_origin 'demo_local', is_synthetic). They are tier 1 and 2,
# the tiers the prompt tells the model outrank external guidance, so at a real
# hospital Sage could cite "Ceftriaxone is NO LONGER recommended for empiric
# severe sepsis coverage at this institution" as that hospital's own policy.
# Both retrieval lanes drop them unless the appliance runs the dev profile:
# search_guidelines here (the live lane) and sage_service._rerank_warm_pool (the
# prefetched pool, which may have been cached before this rule existed).
def synthetic_documents_allowed() -> bool:
    """True only when ORB_DEPLOYMENT_PROFILE is 'dev'. Read at call time."""
    import config
    return getattr(config, "DEPLOYMENT_PROFILE", "prod") == "dev"


def is_synthetic_document(doc: Dict[str, Any]) -> bool:
    return bool(doc.get("is_synthetic")) or (doc.get("source_origin") or "") == "demo_local"


def drop_synthetic(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`docs` without synthetic documents, unless this is a dev appliance."""
    if synthetic_documents_allowed():
        return docs
    return [d for d in docs if not (isinstance(d, dict) and is_synthetic_document(d))]


# ═════════════════════════════════════════════════════════════════
# APPROVAL FILTERING (bookkeeping owned by services.corpus_loader)
# ═════════════════════════════════════════════════════════════════
def _approved_document_ids(db) -> Optional[set]:
    """Set of document_ids allowed under `approved_only` retrieval.

    Built-in seed documents (not tracked in corpus_documents) are always
    considered approved — they are curated in-code. Loader-added documents are
    included only when their corpus_documents.approval_status == 'approved'.

    Returns None if the bookkeeping table/column is absent (nothing to filter on),
    signalling callers to skip filtering entirely (back-compatible).
    """
    try:
        has_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_documents'"
        ).fetchone()
        if not has_table:
            return None
        cols = [r[1] for r in db.execute("PRAGMA table_info(corpus_documents)").fetchall()]
        if "approval_status" not in cols:
            return None
        # Every doc the loader tracks that is NOT approved is excluded.
        approved = {
            r[0] for r in db.execute(
                "SELECT document_id FROM corpus_documents WHERE approval_status = 'approved'"
            ).fetchall()
        }
        loader_all = {
            r[0] for r in db.execute("SELECT document_id FROM corpus_documents").fetchall()
        }
        return {"approved": approved, "loader_tracked": loader_all}
    except Exception:
        return None


def search_guidelines(query: str, limit: int = 5, rerank: bool = True,
                      approved_only: bool = False,
                      min_relevance: Optional[float] = None) -> List[Dict[str, Any]]:
    """Hybrid search (Dense + Lexical) with RRF fusion, query expansion, reranking
    and a relevance floor.

    Returns [] when nothing in the corpus is relevant to `query`. That empty
    result is a real answer, not a failure — see the RELEVANCE FLOOR block above.

    Args:
        query: natural-language clinical query.
        limit: number of results to return.
        rerank: apply the second-stage reranker to the fused candidate pool
            (cross-encoder if installed, else the lexical/heuristic reranker).
        approved_only: exclude loader-added documents whose approval_status is not
            'approved'. Built-in seed docs are always retained. Default False for
            back-compatibility (include drafts + approved).
        min_relevance: override the calibrated relevance floor. None uses the
            calibrated/justified floor; 0.0 disables it — used by the calibration
            harness, which must observe the UNFILTERED score distribution to
            derive the floor in the first place.
    """
    # Expand abbreviations for better recall
    expanded_query = _expand_query(query)

    # Dense retrieval needs an embedding; lexical (FTS5) does not. If the
    # knowledge worker (or in-process embedder) is unavailable, degrade to
    # lexical-only retrieval instead of returning nothing — a guideline found
    # by BM25 beats an empty context.
    query_emb = None
    try:
        query_emb = _embed_text(expanded_query)
    except Exception as e:
        print(f"Embedding error: {e} — falling back to lexical-only retrieval")

    # db_session() ensures the connection is closed (the bare `with get_db()`
    # only managed the transaction and leaked the connection every query).
    with db_session() as db:
        # Check FTS5
        fts_available = False
        try:
            cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clinical_knowledge_fts'")
            fts_available = cursor.fetchone() is not None
        except Exception:
            pass

        # 1. Dense Retrieval (top 15) — skipped when no embedding is available
        dense_results = {}
        if query_emb is not None:
            try:
                db.enable_load_extension(True)
                import sqlite_vec
                sqlite_vec.load(db)

                dense_query = """
                    SELECT rowid, distance
                    FROM clinical_knowledge_vec
                    WHERE embedding MATCH ? AND k = 15
                """
                for rank, row in enumerate(db.execute(dense_query, (query_emb,)).fetchall()):
                    rowid, distance = row
                    # Convert distance to similarity score
                    similarity = max(0.0, 1.0 - distance)
                    dense_results[rowid] = {"rank": rank + 1, "score": similarity}
            except Exception as e:
                print(f"Dense retrieval error: {e}")

        # 2. Lexical Retrieval (top 15)
        lexical_results = {}
        if fts_available:
            try:
                # FTS5 bm25 rank is negative (smaller is better)
                lexical_query = """
                    SELECT rowid, rank
                    FROM clinical_knowledge_fts
                    WHERE clinical_knowledge_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT 15
                """
                # Clean query for FTS MATCH: strip punctuation and small words
                clean_words = []
                for word in expanded_query.split():
                    # Strip non-alphanumeric
                    w = re.sub(r'[^a-zA-Z0-9]', '', word).lower()
                    if len(w) > 3:
                        clean_words.append(w)
                clean_query = " OR ".join(clean_words)
                
                if clean_query:
                    for rank, row in enumerate(db.execute(lexical_query, (clean_query,)).fetchall()):
                        rowid, bm25_rank = row
                        lexical_results[rowid] = {"rank": rank + 1, "score": abs(bm25_rank)}
            except Exception as e:
                print(f"Lexical retrieval error: {e}")
        else:
            # Naive fallback: simple LIKE counts
            try:
                words = [w.lower() for w in query.split() if len(w) > 3]
                if words:
                    cursor = db.execute("SELECT id, content FROM clinical_knowledge")
                    scored = []
                    for rowid, content in cursor.fetchall():
                        content_lower = content.lower()
                        score = sum(content_lower.count(w) for w in words)
                        if score > 0:
                            scored.append((rowid, score))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    for rank, (rowid, score) in enumerate(scored[:15]):
                        lexical_results[rowid] = {"rank": rank + 1, "score": score}
            except Exception as e:
                print(f"Naive lexical error: {e}")

        # 3. Reciprocal Rank Fusion (RRF)
        k_rrf = 60
        fused_scores = {}
        all_rowids = set(dense_results.keys()) | set(lexical_results.keys())
        
        for rowid in all_rowids:
            dense_rank = dense_results.get(rowid, {}).get("rank", 1000)
            lexical_rank = lexical_results.get(rowid, {}).get("rank", 1000)
            
            rrf_score = (1.0 / (k_rrf + dense_rank)) + (1.0 / (k_rrf + lexical_rank))
            fused_scores[rowid] = rrf_score

        # Sort by RRF score. Keep a WIDER candidate pool than `limit` so the
        # second-stage reranker has real candidates to reorder (reranking only the
        # final N would be a no-op). Truncate to `limit` after reranking.
        #
        # Pool size is a LATENCY knob, not just a quality one: every candidate is
        # a full cross-encoder forward pass in the knowledge worker, and this
        # runs INSIDE the clinician's turn. Measured 2026-07-31 on the appliance,
        # a guideline-backed question spent 0.7-5.0 s in retrieval, dominated by
        # reranking a 20-candidate pool. 3x limit keeps a real reordering margin
        # (15 candidates for the default limit of 5) at roughly a third of the
        # cross-encoder cost. Tune with ORB_SAGE_RERANK_POOL if a corpus grows
        # large enough to need a wider net.
        pool_size = max(limit * 3, int(os.getenv("ORB_SAGE_RERANK_POOL", "12")))
        sorted_rowids = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)[:pool_size]

        if not sorted_rowids:
            return []

        # Approval filter (loader bookkeeping owned by corpus_loader).
        allow = _approved_document_ids(db) if approved_only else None
        allow_synthetic = synthetic_documents_allowed()

        # 4. Fetch Metadata for top results
        results = []
        placeholders = ",".join("?" * len(sorted_rowids))
        query_ids = [r[0] for r in sorted_rowids]

        meta_query = f"""
            SELECT id, document_id, title, content, source, source_type, source_origin, trust_tier, is_synthetic
            FROM clinical_knowledge
            WHERE id IN ({placeholders})
        """
        
        meta_dict = {}
        for row in db.execute(meta_query, query_ids).fetchall():
            meta_dict[row[0]] = {
                "id": row[0],
                "document_id": row[1],
                "title": row[2],
                "content": row[3],
                "source": row[4],
                "source_type": row[5],
                "source_origin": row[6],
                "trust_tier": row[7],
                "is_synthetic": bool(row[8])
            }
            
        for rowid, rrf_score in sorted_rowids:
            if rowid in meta_dict:
                meta = meta_dict[rowid]

                # Approval gating: drop loader docs that aren't approved. Seed docs
                # (not tracked by the loader) are always retained.
                if allow is not None:
                    doc_id = meta.get("document_id")
                    if doc_id in allow["loader_tracked"] and doc_id not in allow["approved"]:
                        continue

                # Invented local policy is never citable at a real site (see
                # SYNTHETIC DOCUMENTS above).
                if not allow_synthetic and is_synthetic_document(meta):
                    continue

                # Determine primary retrieval method for transparency
                dense_r = dense_results.get(rowid, {}).get("rank", 1000)
                lexical_r = lexical_results.get(rowid, {}).get("rank", 1000)
                if dense_r < lexical_r:
                    method = "dense"
                elif lexical_r < dense_r:
                    method = "lexical"
                else:
                    method = "hybrid"

                meta["retrieval_method"] = method
                meta["fusion_score"] = rrf_score
                results.append(meta)

        # Second-stage reranking of the fused candidate pool. Enabled by default
        # (and forced on when SAGE_RERANKING_ENABLED). Uses a cross-encoder when
        # installed, else the always-available lexical/heuristic reranker.
        #
        # Reranking now runs over the WHOLE pool (top_k=None) and truncation to
        # `limit` happens after the relevance floor — truncating first would
        # decide the citation set before anything had asked whether the
        # candidates were relevant at all.
        if (rerank or SAGE_RERANKING_ENABLED) and results:
            results = rerank_results(query, results, top_k=None)

        # Relevance floor: everything above this line answers "which corpus
        # chunks are CLOSEST to the query"; nothing above it can answer "is any
        # of this actually about the patient's problem". Applied unconditionally
        # — including on the rerank=False path, where it costs one pass of
        # deterministic lexical scoring and no model — because a caller opting
        # out of REORDERING has not opted into citing irrelevant guidelines.
        if results:
            if min_relevance is not None:
                results = score_relevance(query, results)
                if min_relevance > 0:
                    results = [r for r in results
                               if r["relevance_score"] >= min_relevance]
            else:
                results = apply_relevance_floor(query, results)

        return results[:limit]
