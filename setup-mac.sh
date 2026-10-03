#!/usr/bin/env bash
#
# Orb Hospital OS — Apple Silicon setup (validated end-to-end 2026-07-28).
#
# Tuned for: MacBook Pro, Apple Silicon, 64 GB unified memory, dev profile.
#
# What this does, in order:
#   1. Installs uv (user-space Python toolchain) + Python 3.12 if needed
#   2. Creates server/.venv and installs backend deps
#   3. Installs frontend deps and builds the SPA
#   4. Writes .env (dev profile, 27B text model + 4B vision model)
#   5. Pulls the Ollama models
#   6. Initialises the Helix pharmacy tables, runs migrations, seeds the DB
#
# The generated .env includes ORB_DB_ENCRYPTION_KEY, so the database this script
# seeds is SQLCipher-encrypted at rest from birth. See "Encryption at rest" in
# SETUP-NOTES.md for the migration path if an older plaintext orb.db is present,
# and for what that encryption does and does not protect.
#
# Safe to re-run. It never overwrites an existing .env.
#
#   ./setup-mac.sh              # everything
#   ./setup-mac.sh --skip-models   # skip the ~21 GB of Ollama pulls
#   ./setup-mac.sh --skip-seed     # leave the database alone
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$REPO_ROOT/server"
VENV="$SERVER_DIR/.venv"
ENV_FILE="$REPO_ROOT/.env"

PULL_MODELS=1
DO_SEED=1
for arg in "$@"; do
  case "$arg" in
    --skip-models) PULL_MODELS=0 ;;
    --skip-seed)   DO_SEED=0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '\033[0;34m[info]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[0;32m[ ok ]\033[0m  %s\n' "$*"; }
warn() { printf '\033[0;33m[warn]\033[0m  %s\n' "$*" >&2; }
err()  { printf '\033[0;31m[fail]\033[0m  %s\n' "$*" >&2; }
step() { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }

# ─────────────────────────────────────────────────────────────────────────────
step "1/6  Python toolchain"

if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv (no sudo, lands in ~/.local/bin)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null 2>&1 || { err "uv install failed — see https://astral.sh/uv"; exit 1; }
ok "uv $(uv --version | awk '{print $2}')"

info "Ensuring Python 3.12 is available"
uv python install 3.12

if [ ! -x "$VENV/bin/python" ]; then
  info "Creating venv at server/.venv"
  uv venv --python 3.12 "$VENV"
else
  ok "venv already exists — reusing"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "2/6  Backend dependencies"

# lightning-whisper-mlx pins tiktoken 0.3.3, which has no Python 3.12 wheel and
# needs a Rust toolchain to build from source. Everything except Scribe's local
# ASR works without it — see docs/SETUP.md §5 and the note at the end of this run.
REQ_CORE="$(mktemp)"
grep -v 'lightning-whisper-mlx' "$SERVER_DIR/requirements.txt" > "$REQ_CORE"

info "Installing backend deps (torch + FlagEmbedding are large — expect a few minutes)"
uv pip install --python "$VENV/bin/python" -r "$REQ_CORE"
rm -f "$REQ_CORE"
ok "backend dependencies installed"

# ─────────────────────────────────────────────────────────────────────────────
step "3/6  Frontend"

if ! command -v node >/dev/null 2>&1 || [ "$(node -v | sed 's/v\([0-9]*\).*/\1/')" -lt 20 ]; then
  err "Node 20+ is required (Vite 8). Install it, then re-run:"
  err "    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash"
  err "    export NVM_DIR=\"\$HOME/.nvm\"; . \"\$NVM_DIR/nvm.sh\""
  err "    nvm install 22 && nvm alias default 22"
  exit 1
fi
ok "node $(node -v)"

cd "$REPO_ROOT"
if [ -f package-lock.json ]; then npm ci; else npm install; fi
info "Building the SPA (this also runs the TypeScript typecheck)"
npm run build
ok "frontend built into ./dist — the backend will serve it on port 8000"

# ─────────────────────────────────────────────────────────────────────────────
step "4/6  Environment (.env)"

# Model choice, for the record:
#   ORB_LLM_MODEL   medgemma:27b — official Ollama library, Q4_K_M, ~17 GB,
#                   MULTIMODAL (text + image input, 128K context). This is exactly
#                   what the README says to pull and what config.py already
#                   defaults to. Do NOT substitute alibayram/medgemma:27b — that
#                   community build packages Google's medgemma-27b-TEXT-it weights
#                   and cannot take images, which would silently break Lens.
#   ORB_VISION_MODEL medgemma:27b — same instance. config.py defaults VISION_MODEL
#                   to LLM_MODEL so exactly one model sits in unified memory.
#   ORB_AMBIENT_LLM_MODEL medgemma:4b — the 4B (also multimodal) serves Sage's
#                   bounded background ambient pass, so it stops contending with
#                   clinicians for the single 27B slot. config.py invites this
#                   tiering in its comments but never wires it up.
#
# Scribe correction/verify: config.py's dev defaults are provider "mlx" with a
# local weights dir (server/models/medgemma-1.5-4b-it) that ships empty and needs
# a gated Hugging Face download. Pointed at Ollama instead so Scribe works today.
#
# ORB_DB_ENCRYPTION_KEY is load-bearing, not decorative: with it set, orb.db and
# its -wal/-shm sidecars are opened through SQLCipher, and every backup taken of
# them is encrypted with the same key. Copy the generated value somewhere off this
# machine — without it those backups cannot be restored. It protects copies of the
# data (backups, exports, a disk read elsewhere), NOT this machine while it runs:
# the key lives in the same .env an attacker with filesystem access would read.
# Turn FileVault on as well.

if [ -f "$ENV_FILE" ]; then
  ok ".env already exists — leaving it untouched"
else
  info "Writing .env"
  umask 077
  cat > "$ENV_FILE" <<EOF
# Orb Hospital OS — generated by setup-mac.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Dev profile, Apple Silicon, 64 GB. Keep this file out of git.

ORB_DEPLOYMENT_PROFILE=dev
ORB_DEMO_MODE=true

# Secrets — regenerate with: openssl rand -hex 32
ORB_JWT_SECRET=$(openssl rand -hex 32)
ORB_INGESTION_API_KEY=$(openssl rand -hex 32)
ORB_DB_ENCRYPTION_KEY=$(openssl rand -hex 32)
ORB_UPLOAD_ENCRYPTION_KEY=$(openssl rand -hex 32)

ORB_HOST=127.0.0.1
ORB_PORT=8000
ORB_CORS_ORIGINS=http://localhost:5173,http://localhost:8000,http://127.0.0.1:5173

# ── Models ──
# Both are multimodal (text + image). 64 GB comfortably holds the 27B.
ORB_LLM_MODEL=medgemma:27b
ORB_VISION_MODEL=medgemma:27b
ORB_AMBIENT_LLM_MODEL=medgemma:4b
ORB_OLLAMA_KEEP_ALIVE=1h

# Scribe — route the correction/verify stages through Ollama rather than the
# unshipped local MLX weights. Cheap stages get the 4B; drafting gets the 27B.
ORB_SCRIBE_CORRECTION_PROVIDER=ollama
ORB_SCRIBE_CORRECTION_MODEL=medgemma:4b
ORB_SCRIBE_VERIFY_PROVIDER=ollama
ORB_SCRIBE_VERIFY_MODEL=medgemma:4b
ORB_SCRIBE_DRAFT_MODEL=medgemma:27b

# Local Whisper ASR is not installed (see the note at the end of setup).
ORB_WHISPER_DAEMON_ENABLED=false

# Lens image analysis — on, pointed at the multimodal 4B.
ORB_LENS_ENABLED=true

# Populate demo wards with simulated vitals. Set to "ingestion" for real data only.
ORB_INGESTION_MODE=simulation
EOF
  chmod 600 "$ENV_FILE"
  ok "secrets generated, .env locked to 0600"
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# ─────────────────────────────────────────────────────────────────────────────
step "5/6  Ollama models"

if [ "$PULL_MODELS" -eq 0 ]; then
  info "Skipping model pulls (--skip-models)"
elif ! command -v ollama >/dev/null 2>&1; then
  warn "Ollama is not installed. macOS has no headless installer — get the app:"
  warn "    https://ollama.com/download      (or: brew install ollama)"
  warn "Launch it once, then re-run this script. Orb boots without it; Sage,"
  warn "Lens, Bridge and Scribe drafting will just return errors until it's up."
else
  ok "ollama $(ollama --version 2>/dev/null | tail -1)"
  if ! curl -sf -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
    info "Starting the Ollama server in the background"
    nohup ollama serve > /tmp/ollama-serve.log 2>&1 &
    for _ in $(seq 1 20); do
      curl -sf -m 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  info "Pulling medgemma:27b  (~17 GB, multimodal — the long one)"
  ollama pull medgemma:27b
  info "Pulling medgemma:4b   (~3.3 GB, multimodal — ambient + Scribe cheap tier)"
  ollama pull medgemma:4b
  info "Pulling gemma3:1b     (~0.8 GB, light utility model)"
  ollama pull gemma3:1b
  ok "models pulled"

  # Confirm both really do accept images — 'capabilities' should list "vision".
  # Community medgemma repackagings of the text-only 27B weights do not, and the
  # failure mode is silent: Lens accepts an upload and returns prose about nothing.
  for m in medgemma:27b medgemma:4b; do
    if ollama show "$m" 2>/dev/null | grep -qi vision; then
      ok "$m reports vision capability"
    else
      warn "$m does NOT report vision capability — Lens image analysis will not work."
      warn "    Check: ollama show $m"
    fi
  done
fi

# ─────────────────────────────────────────────────────────────────────────────
step "6/6  Database"

# Encryption at rest: with ORB_DB_ENCRYPTION_KEY set, the app opens orb.db through
# SQLCipher — which CANNOT read a plaintext file. A database left over from a run
# before the key existed therefore fails every query with "file is not a database",
# migrations and seeding included. Detect that here (bytes 0-14 of a plaintext
# SQLite file are the literal "SQLite format 3") and name the one-time fix rather
# than letting the seed die confusingly. Not automatic: encrypt_db.py rewrites the
# system of record, so an operator should run it knowingly.
DB_FILE="${ORB_DB_PATH:-$SERVER_DIR/orb.db}"
if [ -n "${ORB_DB_ENCRYPTION_KEY:-}" ] && [ -f "$DB_FILE" ]; then
  DB_HEADER="$(head -c 15 "$DB_FILE" 2>/dev/null || true)"
  if [ "$DB_HEADER" = "SQLite format 3" ]; then
    warn "$DB_FILE is PLAINTEXT but ORB_DB_ENCRYPTION_KEY is set — SQLCipher cannot"
    warn "open it, so migrations/seeding/boot will fail with 'file is not a database'."
    warn "Migrate it once (server stopped), then re-run this script:"
    warn "    cd server && .venv/bin/python scripts/encrypt_db.py --check"
    warn "    cd server && .venv/bin/python scripts/encrypt_db.py --migrate"
    warn "Or delete it to start clean — a fresh DB is created encrypted: rm $DB_FILE"
  else
    ok "orb.db is encrypted at rest (SQLCipher)"
  fi
fi

if [ "$DO_SEED" -eq 0 ]; then
  info "Skipping seed (--skip-seed)"
else
  cd "$REPO_ROOT"
  info "Applying schema migrations"
  "$VENV/bin/python" server/migrate.py

  # The Helix pharmacy tables are created by services.helix_db.init_helix_db(),
  # which normally only runs at app startup — so a first-run seed hits
  # "no such table: helix_inventory_lots" and silently skips the whole pharmacy
  # catalog. Creating them up front makes the seed complete on a cold database.
  info "Initialising Helix pharmacy tables"
  ( cd "$SERVER_DIR" && "$VENV/bin/python" -c \
      "import sys; sys.path.insert(0,'.'); from services.helix_db import init_helix_db; init_helix_db()" \
      2>/dev/null || true )

  info "Seeding clinical + pharmacy demo data"
  ( cd "$SERVER_DIR" && "$VENV/bin/python" scripts/reset_db_and_seed.py )
  ( cd "$SERVER_DIR" && "$VENV/bin/python" scripts/seed_helix.py )
  ok "database seeded"
fi

# ─────────────────────────────────────────────────────────────────────────────
step "Setup complete"
cat <<'EOF'

  Start it:
      ./run-orb.sh
  then open  http://localhost:8000

  Sign in with any seeded account (password is literally "password"):
      DOC001  Dr. Meera Sharma    doctor
      NUR001  Priya Reddy         nurse
      PHA001  Deepa Joshi         pharmacist   (needed for Helix / pharmacy)
      ADM001  Rajesh Kumar        admin

  Hot-reloading frontend instead of the built SPA:
      npm run dev        → http://localhost:5173

  ── Encryption at rest ──────────────────────────────────────────────────────
  When .env carries ORB_DB_ENCRYPTION_KEY (this script generates one for a new
  .env), orb.db is SQLCipher-encrypted — and so is every backup taken of it. Back
  that key up somewhere off this machine: without it, no backup can be restored.
  Honest scope: the key sits in .env on the same disk as the database, so this
  protects COPIES (backups, exports, a disk read elsewhere), not this machine
  while it is running or if it is stolen. Turn FileVault on too. Details, and the
  migration path for an older plaintext orb.db:
      server/.venv/bin/python server/scripts/encrypt_db.py --check
      SETUP-NOTES.md  ("Encryption at rest")  /  docs/DEPLOYMENT.md §11

  ── Not installed: local Whisper ASR ────────────────────────────────────────
  Scribe's voice transcription needs lightning-whisper-mlx, which pins
  tiktoken 0.3.3 — no Python 3.12 wheel, builds only with Rust. Everything else
  (Vigil, Sage, Lens, Bridge, Relay, Helix, Forecast, Surgical) works without it.
  To add it later:
      curl https://sh.rustup.rs -sSf | sh -s -- -y
      source "$HOME/.cargo/env"
      server/.venv/bin/pip install lightning-whisper-mlx==0.0.10
  then set ORB_WHISPER_DAEMON_ENABLED=true in .env.

EOF
