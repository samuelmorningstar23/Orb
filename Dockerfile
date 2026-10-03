# syntax=docker/dockerfile:1
#
# Orb Hospital OS — container image (INSTALLABILITY workstream).
#
# Multi-stage:
#   stage 1 (frontend): build the Vite/React SPA into dist/
#   stage 2 (runtime):  run the FastAPI backend and ship the built dist/ alongside
#
# IMPORTANT TRADEOFFS (read before deploying):
#   * We install server/requirements-dev.txt, NOT server/requirements.txt.
#     requirements.txt pins two Mac-/GPU-only packages that DO NOT build on Linux:
#       - FlagEmbedding  (BGE-M3 semantic embeddings)  -> drags in a 2nd torch + transformers
#       - lightning-whisper-mlx (Whisper ASR)          -> Apple MLX only, no Linux wheel
#     They are imported lazily and are gracefully degraded when absent, so the app
#     still boots. NET EFFECT IN THIS CONTAINER: embeddings-backed semantic search
#     (Sage retrieval) and on-device Whisper transcription (Scribe) are DEGRADED /
#     unavailable. Text LLM features work because Ollama runs as an EXTERNAL service
#     (see docker-compose.yml), not inside this image.
#   * torch is installed from the PyTorch CPU wheel index. The container has no GPU
#     passthrough; the CPU build is ~10x smaller than the default CUDA wheel and is
#     all the (degraded) Linux code paths need. `torch==2.6.0` (a public specifier)
#     is satisfied by the `2.6.0+cpu` local build per PEP 440.
#   * requirements-dev.txt is a TEST dependency set and omits two runtime-critical
#     pins from requirements.txt, so they are installed explicitly below:
#       - cryptography : without it services/crypto_service + services/field_crypto
#                        degrade to writing PHI in CLEARTEXT after one warning line
#       - bcrypt       : requirements.txt pins 4.2.1; via passlib's extra alone the
#                        image would float to whatever bcrypt released that week

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — build the frontend (dist/)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS frontend
WORKDIR /build

# Install deps first for better layer caching (only re-runs when lockfile changes).
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the frontend source and build. `npm run build` == `tsc -b && vite build`,
# which emits the production SPA into /build/dist. (.dockerignore keeps node_modules,
# the committed dist/, and backend runtime data out of the build context.)
COPY . .
RUN npm run build

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Python runtime (FastAPI backend + static dist/)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Fail fast, no .pyc, unbuffered logs, and default to serving on all interfaces.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORB_HOST=0.0.0.0 \
    ORB_PORT=8000 \
    ORB_DB_PATH=/data/orb.db

# libgomp1 is required at runtime by torch (OpenMP); ca-certificates for outbound
# TLS. Everything else in requirements-dev.txt ships as a manylinux wheel, so no
# compiler toolchain is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached until requirements-dev.txt changes).
# The two extra pins mirror requirements.txt and are NOT in requirements-dev.txt —
# see the cryptography/bcrypt note in the tradeoffs header above.
COPY server/requirements-dev.txt /app/server/requirements-dev.txt
RUN pip install --no-cache-dir torch==2.6.0+cpu \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r /app/server/requirements-dev.txt \
        cryptography==44.0.0 bcrypt==4.2.1

# Backend source. .dockerignore excludes the venv, *.db, uploads, __pycache__ and
# downloaded model weights, so this stays lean.
COPY server/ /app/server/

# Built SPA from stage 1. main.py mounts this directory as StaticFiles with SPA
# fallback when ../dist exists relative to the server dir — /app/dist next to
# /app/server satisfies exactly that, so the image is a single-origin deploy
# (API + SPA on ORB_PORT). A reverse proxy in front of Orb is still the place to
# terminate TLS (see docs/DEPLOYMENT.md).
COPY --from=frontend /build/dist /app/dist

# Runtime data dirs. /data holds the SQLite DB (ORB_DB_PATH); uploads live under the
# server dir (config.UPLOAD_DIR is not env-overridable). Both are mounted as volumes
# in docker-compose.yml so data survives container replacement. Run as a non-root
# user for defense-in-depth (this image handles PHI).
RUN useradd --create-home --uid 10001 orb \
    && mkdir -p /data /app/server/uploads \
    && chown -R orb:orb /data /app/server/uploads
USER orb

WORKDIR /app/server
EXPOSE 8000

# Health probe hits the REAL endpoint the app exposes. main.py registers the health
# route at /api/health (there is no bare /health route), so we target that so the
# probe genuinely reflects app readiness. Uses stdlib only (no curl in the image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).status==200 else 1)"

# `python main.py` runs uvicorn (main:socket_app) bound to ORB_HOST:ORB_PORT.
CMD ["python", "main.py"]
