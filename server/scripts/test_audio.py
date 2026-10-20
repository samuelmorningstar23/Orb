import os

from datasets import load_dataset

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datasets")
ds2 = load_dataset(
    "ekacare/eka-medical-asr-evaluation-dataset", split="test", streaming=True, cache_dir=_CACHE_DIR
)
item = next(iter(ds2))
audio_data = item["audio"]
print(type(audio_data))
print(dir(audio_data))
try:
    print(audio_data["array"][:10])
except Exception as e:
    print(e)
