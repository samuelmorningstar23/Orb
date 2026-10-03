import os
from datasets import load_dataset

cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datasets")
os.makedirs(cache_dir, exist_ok=True)

try:
    ds1 = load_dataset("intelmedica/medical-tts-parquet-2-16khz", split="train", streaming=True, cache_dir=cache_dir)
    print("Dataset 1 columns:", next(iter(ds1)).keys())
except Exception as e:
    print("Error loading ds1:", e)

try:
    ds2 = load_dataset("ekacare/eka-medical-asr-evaluation-dataset", split="train", streaming=True, cache_dir=cache_dir)
    print("Dataset 2 columns:", next(iter(ds2)).keys())
except Exception as e:
    print("Error loading ds2:", e)
