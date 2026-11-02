# Orb: Local Development Setup

How to bring Orb up on an Apple Silicon Mac. Written for the current dev box (Mac Mini M2, 8 GB)
but works on the 48 GB / 96 GB machines too. **No `sudo` required** for the toolchain.

> On the 8 GB Mini we run the **dev profile** (4B models only). 27B is for the 48 GB+ machines.
> See `memory: orb-hardware-timeline`.

---

## 1. Toolchain (user-space, no sudo)

```bash
# uv: Python toolchain & venv manager
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"          # add to ~/.zshrc

# nvm + Node 22 (Vite 8 needs 20.19+ or 22.12+)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
nvm install 22 && nvm alias default 22
```

## 2. Backend (Python 3.12)

```bash
cd server
uv python install 3.12
uv venv --python 3.12 .venv

# NOTE: lightning-whisper-mlx is temporarily EXCLUDED: it pins an ancient
# tiktoken (0.3.3) with no py3.12 wheel that needs Rust to build. Install the
# rest now; handle Whisper ASR separately (see §5).
(
  set -euo pipefail
  # Relative -c includes resolve beside this file, so keep it in server/.
  req_core="$(mktemp .requirements-core.XXXXXX)"
  trap 'rm -f "$req_core"' EXIT
  grep -v lightning-whisper-mlx requirements.txt > "$req_core"
  uv pip install --python .venv/bin/python -r "$req_core"
)
```

### Migrating an existing Python environment

The October security migration uses Torch 2.14.1 and Transformers 5.19.0 directly
for local BGE inference. FlagEmbedding, sentence-transformers and Accelerate are
no longer required. Installing updated requirements into an old environment does
not remove those old packages or unrelated extras. Provision a **fresh** Python
3.12 environment (for example `.venv-next`), install the chosen platform's pinned
requirements, and run `pip check`, the resolved dependency audit and application
checks before changing the service interpreter. Preserve the current environment
until the replacement passes. `install.sh` and `setup-mac.sh` reuse existing
environments; they do not purge packages.

When using pip, install `server/requirements-bootstrap.txt` first. The slim test
requirements include the real local embedding adapter and synthetic model tests;
they exclude the optional Apple speech SDK. A successful slim audit does not
certify the complete Apple speech dependency tree. Runtime inference uses
explicit local snapshots, disables remote model code and never downloads weights.
Safetensors are preferred; an existing local PyTorch checkpoint is loaded only
with `weights_only=True` on the patched Torch version.

### Backend environment variables
```bash
export ORB_DEPLOYMENT_PROFILE=dev          # use 4B/MLX, not 27B (required on 8 GB)
export ORB_JWT_SECRET="<any-dev-secret>"   # prod profile REFUSES to start without this
export ORB_LLM_MODEL=medgemma:4b           # override the 27B default while on the Mini
```

#### Demo vs. production data safety (`ORB_DEMO_MODE`)
`ORB_DEMO_MODE` gates all **synthetic/simulated clinical content** so fabricated
data can never enter a real deployment. It **defaults ON in the `dev` profile and
OFF in `prod`** (override with `ORB_DEMO_MODE=1`/`0`).

When **off** (prod): the surgery-completion "post-op note" is written as a factual
stub for the surgeon to author (no fabricated blood-loss/complications), and the
vitals/report/lab simulation bots stay disabled unless `ORB_INGESTION_MODE=simulation`.
When **on** (the demo): the illustrative mock notes and (with `ORB_INGESTION_MODE=simulation`)
the live simulation bots run, so the demo is fully populated.

### Run the backend
```bash
cd server
source .venv/bin/activate
uvicorn main:socket_app --host 0.0.0.0 --port 8000 --reload
# health check: curl http://localhost:8000/api/health
```

## 3. Frontend (Vite + React)

```bash
npm ci             # install the committed lockfile; replace stale/other-arch node_modules
npm run dev        # http://localhost:5173
```

## 4. Ollama + models  ⚠️ needs a GUI install on macOS

macOS has no headless Ollama installer, download the app:

1. Get Ollama from <https://ollama.com/download> (or `brew install ollama` if you add Homebrew).
2. Launch it once so the `ollama` CLI + server are available.
3. Pull the models we use **on this 8 GB Mini** (4B only):
   ```bash
   ollama pull medgemma:4b          # the tag Orb's fast tier expects (see INSTALL_RUNBOOK.md)
   ollama pull bge-m3               # optional Ollama embedding artifact; the local BGE adapter uses its provisioned Hugging Face snapshot
   ```
   **Do NOT pull `medgemma:27b` on the 8 GB Mini**, it won't load. Reserve it for the 48 GB+ box.

### Orb-owned offline daemon and prompt-cache profile

After provisioning models, start the daemon through the canonical launcher:

```bash
# Quit an existing Ollama desktop/service daemon first if it owns port 11434.
# This launcher never stops or reconfigures another daemon and never pulls models.
ORB_OLLAMA_SWA_FULL=auto ./scripts/serve-ollama.sh
```

The launcher binds to loopback, disables cloud inference/web search and startup
pruning, uses one request per model, and requires f16 KV cache. It preserves
automatic batching and Orb's model, prompts, context windows and clinical gates.
`OLLAMA_NO_CLOUD` is an application setting, not an operating-system firewall;
provisioning pulls and other client actions still require their own network policy.
For a different loopback port, set `OLLAMA_HOST=127.0.0.1:11436` for the launcher
and the matching `ORB_OLLAMA_BASE_URL=http://127.0.0.1:11436` for Orb.

`ORB_OLLAMA_SWA_FULL=auto` enables `LLAMA_ARG_SWA_FULL=1` only for the tested
Ollama **0.35.1**, **Darwin arm64**, **at least 64 GiB** profile. Other versions
and smaller/different hosts retain their runner defaults. `on` requires that same
qualification and fails closed otherwise. `off` prevents the launcher from
enabling the setting; it does not override a llama.cpp system/user `config.ini`
that independently enables full SWA. Explicit `LLAMA_ARG_SWA_FULL` truthy/false
values are respected, and conflicts with `on`/`off` are rejected.

The qualified profile retains **one inference slot** and uses separate KV
buffers (`LLAMA_ARG_KV_UNIFIED=false`) with an 8,192 MiB RAM prompt cache
(`LLAMA_ARG_CACHE_RAM=8192`). Explicit conflicting or malformed cache settings
are rejected. Orb supplies only `slot-prompt-similarity=0.6` through its committed
[runner config](../config/ollama-xdg/llama.cpp/config.ini), binding
`XDG_CONFIG_HOME` for the owned daemon. This makes a shorter shared prompt save
to RAM before a different prompt replaces it; prompts, answers and inference
concurrency stay unchanged. The config contains no prompts or patient data.

The launcher first checks the original user config location (nonempty
`XDG_CONFIG_HOME`, otherwise `$HOME/.config`) and `/etc/llama.cpp/config.ini`.
Existing files, dangling config symlinks, relative/missing config roots or
uninspectable directories make `auto` skip the optimized profile and `on`
refuse startup. Original configuration and explicit inherited SWA settings are
preserved; nothing is merged or overwritten. The owned config must be present,
readable and contain exactly the expected narrow setting. `off` and unqualified
hosts leave config and cache environment settings unchanged.

This profile was measured with the existing cached 27B model, an 8,192-token
interactive context and full answers. It retains more KV history while keeping
the model's sliding attention mask. On the 64 GiB pilot, KV allocation increased
from 1,264 MiB to 3,968 MiB; model weights, compute buffers and RAM prompt cache
need additional memory. Larger contexts, other models and concurrent workloads
need separate sizing and acceptance checks. It does not guarantee latency for
every question or hardware configuration. The setting is supported by the
[pinned llama.cpp parser](https://github.com/ggml-org/llama.cpp/blob/b11232/common/arg.cpp#L1582)
and inherited by [Ollama's runner process](https://github.com/ollama/ollama/blob/v0.35.1/llm/llama_server.go#L417).
The INI is loaded before environment and CLI settings by the
[pinned config parser](https://github.com/ggml-org/llama.cpp/blob/b11232/common/arg.cpp#L667).
The controlled one-slot cache test is recorded in
[the causal verification](audit-2026-10/sage-cache-save-control-20261012.json);
it establishes cache retention, with browser latency acceptance recorded separately.

`setup-mac.sh` uses this launcher when it starts its own daemon on port 11434.
Its provisioning/version/model commands all use that canonical loopback endpoint;
use the standalone launcher for another port.
When a daemon already answers there, setup explicitly reports that the profile
was **not applied**. Exporting a backend variable or editing `.env` cannot change
the environment of an already-running daemon. The standalone launcher reads its
exported environment; it does not source `.env` automatically. Startup logs from
setup are private temporary files. Setup verifies that its own child owns the
listening port; startup failure cleans up that child and stops before model pulls.
Normal `run-orb.sh` startup also uses the launcher when the backend's configured
loopback engine is missing. It tracks and stops only that owned daemon when the
app exits, and continues booting if optional AI startup fails. Reused engines
retain their existing settings. Remote or malformed endpoints are never probed
by this startup helper.

## 5. Whisper ASR (deferred)

`lightning-whisper-mlx` doesn't install cleanly on Python 3.12. Options when we get to Scribe ASR:
- Install Rust (`curl https://sh.rustup.rs | sh -s -- -y`) so its old `tiktoken` builds, **or**
- Switch the dependency to the maintained **`mlx-whisper`** package (small code change in
  `scribe_providers` / `whisper_daemon`), **or**
- Run Whisper only on the 48 GB machine.

Tracked as an open item, not needed to boot the app or develop non-ASR modules.

---

## Current machine status (2026-06-25, Mac Mini M2 8 GB)

| Component | Status |
|---|---|
| uv + Python 3.12 venv | ✅ installed (`server/.venv`) |
| Core backend deps (FastAPI, torch, Transformers, …) | ⏳ installing |
| `lightning-whisper-mlx` | ⏸️ deferred (see §5) |
| nvm + Node 22 | ✅ installed |
| Frontend `npm install` | ⬜ pending |
| Ollama + models | ⬜ **needs you** (GUI install, §4) |
