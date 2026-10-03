# Orb Hospital OS: Deployment Runbook

This is a practical, honest guide to running Orb in a container. It reflects how the
repo actually behaves today (config defaults, required secrets, hardcoded endpoints)
rather than an idealized setup. Read the **Caveats** section before any real deployment.

Deliverables that back this runbook:
- `Dockerfile`, multi-stage (frontend build + Python runtime)
- `.dockerignore`, keeps the build context lean and PHI/secrets out of image layers
- `docker-compose.yml`, `ollama` + `orb` services with persistent volumes
- `server/scripts/backup_db.py`, consistent SQLite backups
- `server/scripts/encrypt_db.py`, migrate a plaintext `orb.db` to SQLCipher, or check its state (§11)
- `scripts/backup.sh` + `scripts/install-backup-schedule.sh` + `scripts/restore-drill.sh`, 
  appliance backups: verify-then-rotate, launchd schedule, monthly restore drill (§9,
  and the full runbook in [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md))

---

## 1. Prerequisites

- **Docker Engine 24+** and the **Docker Compose v2 plugin** (`docker compose`, not
  `docker-compose`).
- **Disk**: budget for the model weights. `medgemma:27b` is ~17 GB in VRAM/RAM and
  several GB on disk; the 4B fallback is far smaller. Plus a couple GB for the image.
- **RAM / GPU**: MedGemma 27B realistically needs a capable GPU (or a lot of RAM for
  slow CPU inference). On modest hardware use the **4B fallback** (see §4).
- **A reverse proxy** for anything beyond localhost, unless you enable Orb's opt-in TLS
  (`ORB_TLS_CERT` + `ORB_TLS_KEY`, see §3). By default Orb serves cleartext HTTP inside
  the container (see Caveats).

Optional NVIDIA GPU passthrough for Ollama requires the
[`nvidia-container-toolkit`](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html);
uncomment the `deploy.resources` block in `docker-compose.yml`.

---

## 2. Quick start

From the repo root:

```bash
# 1. Set the required production secrets (the app refuses to boot without them: see §3).
export ORB_JWT_SECRET="$(openssl rand -hex 32)"
export ORB_INGESTION_API_KEY="$(openssl rand -hex 32)"

# 2. Build and start both services (ollama + orb).
docker compose up -d --build

# 3. Pull an LLM into Ollama (one-time; persisted on the ollama-models volume).
docker compose exec ollama ollama pull medgemma:27b

# 4. Check health.
curl -fsS http://localhost:8000/api/health
# -> {"status":"ok","service":"orb-hospital-os","version":"0.1.0"}
```

The compose file uses `${VAR:?...}` guards, so `docker compose up` fails fast with a
clear message if `ORB_JWT_SECRET` / `ORB_INGESTION_API_KEY` are unset. You can also
put them in a `.env` file next to `docker-compose.yml` (it is git-ignored).

Logs / lifecycle:

```bash
docker compose logs -f orb        # app logs (startup banner shows the active profile/mode)
docker compose ps                 # service + health status
docker compose down               # stop (volumes are preserved)
docker compose down -v            # stop AND delete volumes (DESTROYS the DB, be careful)
```

---

## 3. Environment variables

The container is configured through `ORB_*` env vars read by `server/config.py`.
The important ones:

| Variable | Required? | Default | Notes |
|---|---|---|---|
| `ORB_DEPLOYMENT_PROFILE` | no | `prod` | `prod` enforces the fail-closed secret checks and locks CORS to `ORB_CORS_ORIGINS`. `dev` relaxes both. |
| `ORB_JWT_SECRET` | **yes (prod)** | n/a | JWT signing key. **In `prod` the app raises `CRITICAL SECURITY ERROR` at import and will not start if this is unset.** Generate with `openssl rand -hex 32`. |
| `ORB_INGESTION_API_KEY` | **yes (prod)** | n/a | Protects the real-patient write path `/api/ingest/*`. **Also fail-closed in `prod`**, unset ⇒ the app refuses to boot. |
| `ORB_HOST` | no | `127.0.0.1` | The image/compose set this to `0.0.0.0` so the server is reachable outside the container. |
| `ORB_PORT` | no | `8000` | Listen port. |
| `ORB_DB_PATH` | no | `<server>/orb.db` | Set to `/data/orb.db` in compose and mounted on a volume for persistence. |
| `ORB_CORS_ORIGINS` | recommended | dev localhost list | Comma-separated allowed origins. Lock this to your real SPA origin in prod. **Single-origin deploy:** when the backend serves the built SPA itself, the Socket.IO handshake's `Origin` is that same public origin, include it here or the realtime socket is rejected, and within seconds the browser's socket retries hold every connection it allows to that address, so ordinary screens hang for 15 to 25 seconds at a time (see INSTALL_RUNBOOK F11). |
| `ORB_COOKIE_SECURE` | no | `auto` | Marks the `orb_session`/`orb_csrf` cookies `Secure`. `auto` decides per-request from the effective scheme (honouring `X-Forwarded-Proto`), so an HTTPS session gets a `Secure` cookie while a plain-HTTP LAN pilot still works. Force with `1`/`0`. |
| `ORB_LLM_MODEL` | no | `medgemma:27b` | Ollama model tag Orb queries. Use `alibayram/medgemma:4b` on small hardware. |
| `ORB_INGESTION_MODE` | no | `ingestion` | `ingestion` = real data only (safe default); `simulation` = synthetic bots; `hybrid` = both. |
| `ORB_OLLAMA_KEEP_ALIVE` | no | `1h` | How long Ollama keeps the model resident. |
| `ORB_TLS_CERT` | no | n/a | Path to a TLS certificate (PEM). When set together with `ORB_TLS_KEY`, `main.py`'s `uvicorn.run` serves HTTPS directly instead of cleartext HTTP. |
| `ORB_TLS_KEY` | no | n/a | Path to the TLS private key (PEM). Must be set alongside `ORB_TLS_CERT` for the app to serve HTTPS. If unset, terminate TLS at a reverse proxy. |
| `ORB_DB_ENCRYPTION_KEY` | recommended | n/a | Encrypts `orb.db` (and its `-wal`/`-shm` sidecars) with SQLCipher. Generate with `openssl rand -hex 32`. Unset ⇒ plaintext DB. A pre-existing plaintext DB must be migrated once, see §11. |
| `ORB_UPLOAD_ENCRYPTION_KEY` | recommended | n/a | Encrypts uploaded PHI media (Lens images, Scribe audio) with AES-256-GCM. Unset ⇒ uploads stored in cleartext. Set `ORB_REQUIRE_UPLOAD_ENCRYPTION=1` to make uploads fail closed instead of silently falling back. |

> **Authentication & sessions.** Browser login sets an **HttpOnly `orb_session`
> cookie** (the JWT, unreadable to JS so an XSS can't exfiltrate a session) plus a
> JS-readable `orb_csrf` double-submit token; the SPA no longer persists the JWT in
> localStorage. Cookie-authenticated **mutations** must echo the CSRF token in the
> `X-CSRF-Token` header (the client does this automatically); `Authorization: Bearer`
> requests (machine clients) stay CSRF-exempt and still work. `POST /api/auth/refresh`
> slides the session forward without re-login. Because auth is now cookie-based, the
> app must be served **same-origin** (the API and SPA share `window.location.origin`,
> which is already the default) so the cookie is first-party.

> **Note on the Ollama URL:** `server/config.py` **hardcodes**
> `OLLAMA_BASE_URL = "http://localhost:11434"` and does not read it from an env var.
> `docker-compose.yml` works around this by having the `orb` service **share the
> `ollama` service's network namespace** (`network_mode: "service:ollama"`), so the
> app's `localhost:11434` resolves to the Ollama container. A side effect is that the
> Orb port (`8000`) is published on the `ollama` service, not on `orb`. See the
> comments at the top of `docker-compose.yml` for the cleaner layout to adopt if/when
> config is taught to read `ORB_OLLAMA_BASE_URL`.

---

## 4. Ollama models

Models are pulled into the `ollama` container and cached on the `ollama-models`
volume, so a pull survives restarts and image rebuilds.

```bash
# Full-quality clinical model (needs a capable GPU / lots of RAM):
docker compose exec ollama ollama pull medgemma:27b

# Small-hardware fallback: pull the 4B model and point Orb at it:
docker compose exec ollama ollama pull alibayram/medgemma:4b
#   then set ORB_LLM_MODEL and restart orb:
#   ORB_LLM_MODEL=alibayram/medgemma:4b docker compose up -d orb

# Inspect what's installed:
docker compose exec ollama ollama list
```

If Orb logs `LLM warmup got HTTP 404 for model '<name>'`, the model tag in
`ORB_LLM_MODEL` is not pulled in Ollama, pull it or fix the tag. Until then Sage,
Lens, Bridge and Scribe LLM calls return empty.

---

## 5. Backup & restore

> **On the Mac Studio appliance, do not follow this section, follow
> [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md) and §9 below.** This section covers
> the *containerised* deployment. The appliance uses `scripts/backup.sh` (verify-then-rotate)
> on a **launchd** schedule, plus a monthly **restore drill**, and it states the key-escrow
> problem in full. Backups are only real once a restore has been tested.

Use `server/scripts/backup_db.py`. It takes a **transactionally-consistent** snapshot
via SQLite's online backup API (safe on a hot, WAL-mode DB, unlike `cp orb.db`), writes
a timestamped file, verifies it with `PRAGMA integrity_check`, and prunes old backups.
It opens both ends through `database.py`'s codec-aware helpers, so it works whether the
live DB is plaintext or SQLCipher-encrypted.

> **When `ORB_DB_ENCRYPTION_KEY` is set, the backups are encrypted too, with the same
> key.** That is the point (a backup volume never holds plaintext PHI), but it means a
> restore is impossible without the key, and the key normally lives in `.env` on the very
> machine the backup exists to survive. **Escrow the key separately** (password manager,
> sealed envelope, KMS, anywhere but the appliance) before you rely on these backups, and
> remember that rotating the key strands every backup taken under the old one. The stdlib
> `sqlite3` CLI cannot open an encrypted backup either; inspect one with a SQLCipher-aware
> connection (see §11).

**Back up (from the running container):**

```bash
# Writes /data/backups/orb-backup-YYYYmmdd-HHMMSS.db (ORB_DB_PATH defaults to /data/orb.db)
docker compose exec orb python scripts/backup_db.py --out-dir /data/backups

# Keep only the last 30 days:
docker compose exec orb python scripts/backup_db.py --out-dir /data/backups --retain-days 30
```

Because `/data` is a named volume, backups written there persist. To pull a copy onto
the host:

```bash
docker compose cp orb:/data/backups ./orb-backups
```

**Schedule it** with a host cron entry (runs daily at 02:00):

```cron
0 2 * * *  docker compose -f /path/to/Orb/docker-compose.yml exec -T orb python scripts/backup_db.py --out-dir /data/backups --retain-days 14
```

**Restore** (stop writers first so nothing races the swap):

```bash
docker compose stop orb
# Overwrite the live DB with a chosen backup, inside the volume:
docker compose run --rm --entrypoint sh orb -c \
  'cp /data/backups/orb-backup-YYYYmmdd-HHMMSS.db /data/orb.db && rm -f /data/orb.db-wal /data/orb.db-shm'
docker compose start orb
```

Run the script anywhere (no app imports needed), e.g. against a host DB:

```bash
python server/scripts/backup_db.py --db-path ./server/orb.db --out-dir ./backups
python server/scripts/backup_db.py --help
```

---

## 6. Caveats (read before production)

These are honest limitations of the current container/build, not TODOs hidden in a
changelog:

- **TLS is opt-in.** By default the app serves plain HTTP. Set `ORB_TLS_CERT` +
  `ORB_TLS_KEY` (see §3) and `main.py`'s `uvicorn.run` serves HTTPS directly. Otherwise,
  put Orb behind a TLS-terminating **reverse proxy** (nginx, Caddy, Traefik) and only
  expose the proxy publicly, JWTs and PHI would otherwise cross the wire in cleartext.
- **The SPA is not served by the backend.** `main.py` mounts no `StaticFiles` route for
  the built frontend. The image ships `dist/` at `/app/dist`, but you must serve it from
  your reverse proxy (or add a static mount). The backend only answers `/api/*`,
  Socket.IO, and the authenticated `/uploads/*` route.
- **Embeddings & Whisper are degraded in this Linux container.** The image installs
  `server/requirements-dev.txt`, deliberately **omitting** `FlagEmbedding` (BGE-M3
  semantic embeddings) and `lightning-whisper-mlx` (on-device ASR), both are Mac-/GPU-
  only and will not build on Linux. Consequences: embeddings-backed semantic retrieval
  (Sage) and in-container Whisper transcription (Scribe) are unavailable/degraded. Text
  LLM features work because **Ollama runs as an external service**, not inside the image.
- **CPU-only inference for torch.** The image installs the PyTorch **CPU** wheel (no GPU
  passthrough for the Orb container). Ollama can still use a GPU if you enable the
  `deploy.resources` block.
- **The container image cannot encrypt the database yet.** DB encryption needs the
  `sqlcipher3` driver, which is pinned in `server/requirements.txt`, but the image
  installs `server/requirements-dev.txt` (see the bullet below), and that file does not
  carry it. Setting `ORB_DB_ENCRYPTION_KEY` in compose therefore logs
  `no SQLCipher driver … falling back to PLAINTEXT sqlite3` and keeps running on a
  plaintext `/data/orb.db`. **Check the startup log rather than assuming.** Until the
  pin is added to the image build, rely on volume/host encryption there; §11's SQLCipher
  path applies to the bare-metal (Apple Silicon) install, where `requirements.txt` is
  what gets installed.
- **Encryption at rest is available but opt-in, and it is not full-disk encryption.**
  Set `ORB_DB_ENCRYPTION_KEY` and `orb.db` is SQLCipher-encrypted (§11); set
  `ORB_UPLOAD_ENCRYPTION_KEY` and uploaded media is AES-256-GCM encrypted. Set neither
  and both hold PHI in cleartext on the mounted volumes. Even with both set, the keys sit
  in the container's environment / `.env` alongside the data, so this protects **copies**
  (backups, exports, a volume snapshot read elsewhere), not a live or stolen host whose
  environment the attacker can also read. Still encrypt the underlying storage
  (LUKS/FileVault/cloud volume encryption) and restrict volume access.
- **Not HIPAA-certified.** This deployment has not been through a HIPAA compliance
  assessment. Treat it as a self-hosted system for which *you* are responsible for the
  surrounding controls (TLS, at-rest encryption, access control, audit retention,
  backups, BAAs).
- **`ORB_INGESTION_MODE` defaults to `ingestion`** (real data only). Do not flip it to
  `simulation`/`hybrid` on a deployment that handles real patients, it enables synthetic
  clinical-data bots.

---

## 7. Bare-metal / one-command install (`install.sh`)

For a non-container host (e.g. the Apple Silicon Mac Studio Orb targets), the
repo-root `./install.sh` sets a machine up in one command. It is **idempotent and
safe to re-run**, it never overwrites `.env`, existing TLS certs, or data.

```bash
./install.sh                 # backend deps + secrets + TLS + migrations + frontend build
./install.sh --backend-only  # skip the npm build (API-only host)
./install.sh --help
```

What it does:

1. Preflight checks (`python3` ≥ 3.11, `openssl`, and `node`/`npm` for the build).
2. Creates `server/.venv` and installs `server/requirements.txt`. Heavy/native
   pins (`torch`, `lightning-whisper-mlx`) that fail on a given host are surfaced
   as warnings, the core API deps still install.
3. Generates `.env` (repo root, `chmod 600`) with fresh `openssl rand -hex 32`
   secrets for `ORB_JWT_SECRET`, `ORB_INGESTION_API_KEY`, `ORB_DB_ENCRYPTION_KEY`,
   and `ORB_UPLOAD_ENCRYPTION_KEY`, **only if `.env` does not already exist**.
   Because the DB key is generated here, a database created from this point on is
   encrypted at rest automatically; a plaintext `orb.db` that predates it needs the
   one-time migration in §11. Escrow the generated DB key off the machine, backups
   are encrypted with it (§11).
4. Generates a **self-signed dev** TLS cert into `./certs` (only if absent) and
   points `ORB_TLS_CERT`/`ORB_TLS_KEY` at it. See §3 / below for a real cert.
5. Runs migrations via `server/migrate.py` (see §8).
6. Builds the frontend into `./dist` unless `--backend-only`.
7. Prints backup scheduling instructions and a start-up summary.

Load the generated secrets before starting the server:

```bash
set -a && . ./.env && set +a
cd server && ../server/.venv/bin/python main.py
```

**TLS cert placement (production):** replace the self-signed dev files with a
CA-issued cert (do not ship the self-signed one), or terminate TLS at a reverse
proxy and unset `ORB_TLS_CERT`/`ORB_TLS_KEY`:

```bash
cp /path/to/fullchain.pem certs/orb.crt
cp /path/to/privkey.pem   certs/orb.key
chmod 600 certs/orb.key
```

---

## 8. Schema migrations (`server/migrate.py`)

Migrations are additive and idempotent, defined inside `database.init_db()` and
keyed off `SCHEMA_VERSION` + the `schema_version` ledger table. `migrate.py` is a
thin wrapper around that existing logic, it does **not** define schema. The app
also runs `init_db()` on boot, so this runner is for controlled pre-boot ops/CI.

```bash
python server/migrate.py --status   # print current vs target version, no changes
python server/migrate.py            # apply migrations (safe to repeat)

# Target a specific DB:
ORB_DB_PATH=/data/orb.db python server/migrate.py --status
```

---

## 9. Backups on the appliance: schedule, drill, escrow

**Full runbook: [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md).** This is the
summary; that document is what an operator should have open during an incident.

`scripts/backup.sh` is the bare-metal path. It delegates the snapshot to
`server/scripts/backup_db.py` (SQLCipher-aware, online backup API, consistent on a hot
WAL database), then **verifies before it rotates**: size floor, opens the result with
`ORB_DB_ENCRYPTION_KEY`, `PRAGMA integrity_check`, and real row counts in `patients`,
`vitals`, `audit_logs`, `users`. If verification fails it exits 2 and **rotates nothing**,
so a bad run can never prune good backups. The old outer `openssl enc` layer is gone, it
used the same key as the database, so it added no protection, and it made restore a
two-step process people got wrong under pressure.

```bash
cd /path/to/Orb
set -a && . ./.env && set +a     # every command below needs the key

# 1. Install the schedule (daily 02:30, launchd, + monthly restore drill).
./scripts/install-backup-schedule.sh --offsite /Volumes/OrbBackup --with-drill

# 2. Prove a backup is restorable. Monthly. This is the one manual duty.
./scripts/restore-drill.sh

# 3. Ad-hoc backup, e.g. before an upgrade or a migration.
./scripts/backup.sh
```

**Use launchd, not cron**, this is a Mac appliance. `install-backup-schedule.sh` writes
and loads the plist, validates it with `plutil -lint`, verifies it registered, and is
idempotent (`--uninstall` removes it). It installs a **LaunchAgent**, not a LaunchDaemon,
so the job runs as the operator rather than root, a root job would have to read the
operator's `.env` to get the DB key, concentrating key and ciphertext at one privilege
level. The trade-off, argued in full in the script header: **the appliance must have
automatic login enabled**, or an unattended reboot stops backups silently.

Two failure modes worth knowing before you hit them:

- **macOS TCC.** launchd jobs cannot read `~/Documents`, `~/Desktop`, `~/Downloads` or
  iCloud Drive. A repo in one of those loads fine, looks healthy in `launchctl list`, and
  fails *every night* with `Operation not permitted`. The installer preflights for this
  and refuses to report success if the job cannot reach the repo. Keep the appliance repo
  somewhere like `/opt/orb`, a Documents folder is also iCloud-synced on many Macs, which
  would push PHI off-box and break the no-cloud promise.
- **`server/uploads/` IS backed up**, this line previously said it was not, which was
  wrong and would have led an operator either to build a redundant manual copy or to
  accept a gap that does not exist. `scripts/backup.sh` tars the uploads directory with a
  matching timestamp (`<stamp>-uploads.tar`), chmods it 600, copies it off-box alongside
  the database, and rotates the two together, so a restore gets a database and a media set
  from the same moment. Override the location with `ORB_UPLOADS_DIR`; if the directory is
  absent the script warns and skips media rather than failing the DB backup.
  What remains true and important: Lens images and Scribe audio are PHI encrypted with a
  *second* key (`ORB_UPLOAD_ENCRYPTION_KEY`), so **escrow both keys**. A database backup
  restored without the upload key gives you notes referring to images you cannot open.

**Restore** (drill it first, see the runbook for the full procedure):

```bash
./scripts/restore-drill.sh --backup backups/orb-backup-YYYYMMDD-HHMMSS.db   # prove it FIRST
# stop the server
mv server/orb.db server/orb.db.broken-$(date +%Y%m%d-%H%M%S)   # keep the old one
cp backups/orb-backup-YYYYMMDD-HHMMSS.db server/orb.db && chmod 600 server/orb.db
rm -f server/orb.db-wal server/orb.db-shm                      # stale sidecars
# start the server with the SAME ORB_DB_ENCRYPTION_KEY
```

> **The key is not in the backup.** Backups are SQLCipher-encrypted with
> `ORB_DB_ENCRYPTION_KEY`, which lives in `.env` on the machine the backups exist to
> survive. Lose it and every backup is permanently unreadable ciphertext, no recovery, no
> override. Rotating the key strands every backup taken under the old one. Escrow options
> and their trade-offs are laid out in
> [`docs/BACKUP_AND_RESTORE.md` §6](BACKUP_AND_RESTORE.md#6-the-key-escrow-problem); pick
> one **before** real patients, and note that an on-appliance backup does not survive the
> appliance, set `--offsite`.

Overrides: `ORB_BACKUP_DIR`, `ORB_BACKUP_RETAIN`, `ORB_BACKUP_OFFSITE_DIR`,
`ORB_BACKUP_MIN_BYTES`, `ORB_DB_PATH`.

---

## 10. Metrics (Prometheus) & Grafana

Orb exposes `GET /metrics` in the Prometheus text exposition format, implemented
**without** `prometheus_client` (zero new dependencies) in
`server/services/metrics_service.py`. Metrics: process uptime, DB size on disk,
patient/order/audit row counts, active admissions, and LLM queue depth, coarse
operational counts only, **no PHI**.

`/metrics` is open by default (standard scrape convention). To gate it, set
`ORB_METRICS_TOKEN` and scrape with `Authorization: Bearer <token>`.

**Prometheus scrape config:**

```yaml
scrape_configs:
  - job_name: orb-hospital-os
    scrape_interval: 30s
    metrics_path: /metrics
    static_configs:
      - targets: ["127.0.0.1:8000"]
    # If ORB_METRICS_TOKEN is set:
    # authorization:
    #   type: Bearer
    #   credentials: "<ORB_METRICS_TOKEN>"
    # If serving HTTPS with the dev self-signed cert:
    # scheme: https
    # tls_config:
    #   insecure_skip_verify: true
```

**Grafana import:** Dashboards → New → **Import** → upload
`ops/grafana-dashboard.json` → select your Prometheus data source (the
`DS_PROMETHEUS` variable). Panels: uptime, DB size, LLM queue depth, active
admissions, record counts, and HTTP request rate.

> You must supply the Prometheus server and Grafana instance, Orb ships the
> exposition endpoint and the dashboard JSON, not the monitoring stack. The HTTP
> request-rate panel reads `orb_http_requests_total`, which is only populated if
> the app increments it via `metrics_service.inc(...)`; until then it is empty.

---

## 11. Encryption at rest (SQLCipher)

`orb.db` holds the PHI system of record. Setting `ORB_DB_ENCRYPTION_KEY` makes the
app open it, and its `-wal`/`-shm` sidecars, through **SQLCipher** (AES-256), so the
file on disk is ciphertext. Uploaded media (Lens images, Scribe audio) is a separate
control: `ORB_UPLOAD_ENCRYPTION_KEY` encrypts those with AES-256-GCM.

The driver (`sqlcipher3-wheels==0.5.7`, importable as `sqlcipher3`) is pinned in
`server/requirements.txt`, so a bare-metal install
(`pip install -r server/requirements.txt`) already has it. **The Docker image does
not**, it installs `requirements-dev.txt`, which omits the pin, so the container
silently falls back to a plaintext DB (see §6). It is the only
distribution shipping a CPython 3.12 arm64 wheel, `sqlcipher3-binary` has none, and
`pysqlcipher3` needs a local SQLCipher build. If the key is set but no driver is
importable, the app does **not** fail, it logs a warning and runs on plaintext sqlite,
so check the startup log rather than assuming the key alone did the job.

`ORB_DB_ENCRYPTION_KEY` may be any length: `database._sqlcipher_key_hex` hashes it to a
32-byte raw key (`PRAGMA key = "x'<64 hex>'"`), which is what the app and
`encrypt_db.py` both use, so they agree byte-for-byte. It is the *string in `.env`* that
matters for recovery; keep that exact value.

**Validated 2026-07-30 on Apple Silicon / Python 3.12:** SQLCipher runs alongside the
`sqlite-vec` loadable extension (vec0 vector search), FTS5 full-text search, and WAL
mode. The earlier concern that the codec would conflict with the loadable extension or
the WAL/mmap PRAGMA tuning does not hold on this build.

### Enable it

```bash
# 1. Generate a key and put it in .env (chmod 600).
echo "ORB_DB_ENCRYPTION_KEY=$(openssl rand -hex 32)" >> .env

# 2. Confirm the driver is installed and see what state the DB is in.
cd server && .venv/bin/python scripts/encrypt_db.py --check
```

A database **created** after the key is in place is encrypted from birth, nothing else
to do. An **existing plaintext `orb.db` must be migrated once.** SQLCipher cannot read a
plaintext file, so until you migrate, every query fails with `file is not a database`:

```bash
# Stop the server first: the script refuses if anything is listening on :8000.
cd server && .venv/bin/python scripts/encrypt_db.py --migrate
```

The migration is deliberately cautious: it checkpoints WAL, copies the plaintext original
to `orb.db.plaintext-<timestamp>`, exports into a keyed database via `sqlcipher_export`,
and **verifies before swapping**, per-table row counts for the clinically load-bearing
tables, `audit_logs` hash-chain *linkage* (a broken chain after migration is
indistinguishable from tampering), the vector index, and an FTS5 query. Any mismatch
aborts and leaves the original in place. On success it confirms the app's own connection
path can open the result.

> The plaintext backup is **kept by default** and still contains unencrypted PHI. Delete
> it once you have confirmed the encrypted DB works (`rm server/orb.db.plaintext-*`), or
> pass `--no-keep-plaintext` to have the script remove it after verification.

Everything that opens `orb.db` must go through `database.py`'s codec-aware helpers
(`_open_raw_connection`, `_open_raw_connection_at`, `get_db`/`db_session`), the stdlib
`sqlite3` driver and the `sqlite3` CLI both fail on an encrypted file. Note also that
`sqlite3.Row` raises `TypeError` on a SQLCipher cursor; `database._row_factory_for(conn)`
picks the right Row class for whichever driver opened the connection.

### What it protects: and what it does not

Stated plainly, because it is easy to oversell:

- **Protected:** copies of the database that leave the machine or outlive it, nightly
  backups, exports, a cloned volume or disk image, a decommissioned SSD. Anyone holding
  the file without the key has ciphertext.
- **Not protected:** a **running** appliance, or a **stolen** one. The key lives in `.env`
  on the same disk as the database, so an attacker with filesystem access reads the key
  as easily as the DB and decrypts at will. Database-level encryption is not a substitute
  for host security.
- **Recommended alongside:** **FileVault** (or LUKS / cloud volume encryption) for
  full-disk coverage, and filesystem permissions (Orb already forces `0600` on `orb.db`
  and its sidecars).
- **Stronger posture:** source the key from the **OS keychain** / a secret store at boot
  instead of a dotfile, so it is never at rest next to the data it protects. Orb does not
  do this today.

### Key custody is now a recovery risk

Backups of an encrypted DB are encrypted **with the same key**, so the key is part of the
restore path:

- **Escrow the key off the appliance**, password manager, sealed envelope, KMS. If the
  machine dies and the key died with it, every backup is unrecoverable ciphertext. This is
  a realistic way to lose the entire record.
- **Rotating the key strands existing backups.** There is no re-key tooling; plan a
  restore-then-re-encrypt cycle and keep the old key until the old backups age out.
- **Test a restore** with the key you actually escrowed, not the one on the box.

Not compliance certification: this is one technical safeguard (HIPAA addressable
implementation specification, GDPR Art. 32) now implemented and verifiable. The
surrounding programme, DR drills, retention, DPIA, BAAs, is still on you.
