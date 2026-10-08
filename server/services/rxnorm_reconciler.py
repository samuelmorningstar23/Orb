"""Orb RxNorm & Helix Master Formulary Reconciler

Uses Double Metaphone approximations and Levenshtein distance matching 
to map raw transcript drug names directly to canonical Helix Master IDs.
"""
import re
from typing import Optional, Tuple
from database import db_session
from services.rxnorm_corrector import metaphone_key, levenshtein

def calculate_similarity(s1: str, s2: str) -> float:
    """Calculates normalized Levenshtein similarity between 0.0 and 1.0."""
    s1_clean = s1.strip().lower()
    s2_clean = s2.strip().lower()
    if not s1_clean or not s2_clean:
        return 0.0
    dist = levenshtein(s1_clean, s2_clean)
    max_len = max(len(s1_clean), len(s2_clean))
    return 1.0 - (dist / max_len) if max_len > 0 else 0.0

def calculate_metaphone_similarity(s1: str, s2: str) -> float:
    """Calculates phonetic metaphone token overlap similarity."""
    w1 = [metaphone_key(w) for w in re.findall(r'\b\w+\b', s1) if len(w) > 2]
    w2 = [metaphone_key(w) for w in re.findall(r'\b\w+\b', s2) if len(w) > 2]
    if not w1 or not w2:
        return 0.0
    intersection = set(w1).intersection(set(w2))
    return len(intersection) / max(len(w1), len(w2))

def reconcile_medication(name: str) -> Tuple[Optional[dict], float]:
    """Reconciles raw medication name to the helix_medication_master catalog.
    
    Returns (matched_medication_dict, score) where score is between 0.0 and 1.0.
    Returns (None, 0.0) if no match meets the 0.8 threshold.

    Matches CONFIRMED drugs only (identity_status 'active', grn-6): this is an
    ordering path (Relay and Sage cards confirm through it), so a draft waiting
    for its second pharmacist, or a retired drug, is never the match. The
    safety screens read every row on their own.
    """
    if not name or len(name.strip()) < 3:
        return None, 0.0

    best_match = None
    best_score = 0.0

    with db_session() as conn:
        meds = conn.execute(
            "SELECT * FROM helix_medication_master WHERE identity_status = 'active'"
        ).fetchall()
        
        for med in meds:
            med_dict = dict(med)
            cand_name = med_dict["name"]
            cand_generic = med_dict.get("generic_name", "") or ""
            
            # Check similarity against name and generic name
            score_name = calculate_similarity(name, cand_name)
            score_gen = calculate_similarity(name, cand_generic)
            
            # Check metaphone similarity
            meta_name = calculate_metaphone_similarity(name, cand_name)
            meta_gen = calculate_metaphone_similarity(name, cand_generic)
            
            # Hybrid scoring
            lex_score = max(score_name, score_gen)
            meta_score = max(meta_name, meta_gen)
            
            hybrid = (lex_score * 0.7) + (meta_score * 0.3)
            
            # Edge-case: if exact substring match
            if name.lower() in cand_name.lower() or name.lower() in cand_generic.lower():
                hybrid = max(hybrid, 0.85)

            if hybrid > best_score:
                best_score = hybrid
                best_match = med_dict

    if best_score >= 0.8:
        return best_match, best_score
        
    return None, best_score
