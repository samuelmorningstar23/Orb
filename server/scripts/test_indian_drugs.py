import os
import sys
from pathlib import Path

# Add server dir to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from services.rxnorm_corrector import RxNormCorrector

def test_corrections():
    db_path = SERVER_DIR / "datasets" / "rxnorm" / "rxnorm_drugs.db"
    corrector = RxNormCorrector(str(db_path))
    
    test_phrases = [
        "Patient was given doloo six hundred and fifty",
        "Prescribed monoceff 1g IV",
        "Take crocin for fever",
        "Start on pantocidd once daily",
        "We ordered augmentinn 625",
        "Patient took combiflamm for pain"
    ]
    
    print("\n=== Indian Drug Name Correction Test ===")
    for phrase in test_phrases:
        corrected, logs = corrector.correct(phrase)
        print(f"Original:  {phrase}")
        print(f"Corrected: {corrected}")
        if logs:
            for log in logs:
                print(f"  → Corrected '{log['original']}' to '{log['corrected']}' (distance: {log['distance']})")
        else:
            print("  → No corrections made.")
        print()

if __name__ == "__main__":
    test_corrections()
