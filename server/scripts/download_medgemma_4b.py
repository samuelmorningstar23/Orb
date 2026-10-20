"""Manual one-off: download the MedGemma 4B weights into models/.

Relocated out of the server package root (was server/test_dl.py) so pytest's
test_*.py discovery does not collect/execute this network download during a
test run. Run it directly: `python scripts/download_medgemma_4b.py`.

The HF token MUST come from the environment (HF_TOKEN) — never hardcode it.
The previously committed literal token has been removed; if it was ever real,
rotate/revoke it on huggingface.co immediately.
"""

import os
import sys

from huggingface_hub import login, snapshot_download

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    sys.exit("HF_TOKEN environment variable is not set. Export it before running.")

login(token=HF_TOKEN)
try:
    snapshot_download(
        repo_id="google/medgemma-1.5-4b-it",
        local_dir="models/medgemma-1.5-4b-it",
        local_dir_use_symlinks=False,
    )
except Exception as e:
    print(f"Exception Type: {type(e).__name__}")
    print(f"Exception Details: {e}")
    if hasattr(e, "__cause__") and e.__cause__ is not None:
        print(f"Cause: {e.__cause__}")
