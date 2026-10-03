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

# nvm + Node 22 (Vite 8 needs Node 20+)
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
grep -v lightning-whisper-mlx requirements.txt > /tmp/req_core.txt
uv pip install --python .venv/bin/python -r /tmp/req_core.txt
```

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
npm install        # reinstall fresh, the committed-era node_modules may be stale/other-arch
npm run dev        # http://localhost:5173
```

## 4. Ollama + models  ⚠️ needs a GUI install on macOS

macOS has no headless Ollama installer, download the app:

1. Get Ollama from <https://ollama.com/download> (or `brew install ollama` if you add Homebrew).
2. Launch it once so the `ollama` CLI + server are available.
3. Pull the models we use **on this 8 GB Mini** (4B only):
   ```bash
   ollama pull medgemma:4b          # the tag Orb's fast tier expects (see INSTALL_RUNBOOK.md)
   ollama pull bge-m3               # embeddings (or via FlagEmbedding in-process)
   ```
   **Do NOT pull `medgemma:27b` on the 8 GB Mini**, it won't load. Reserve it for the 48 GB+ box.

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
| Core backend deps (FastAPI, torch, FlagEmbedding, …) | ⏳ installing |
| `lightning-whisper-mlx` | ⏸️ deferred (see §5) |
| nvm + Node 22 | ✅ installed |
| Frontend `npm install` | ⬜ pending |
| Ollama + models | ⬜ **needs you** (GUI install, §4) |
