# Orb Hospital OS: Deployment and configuration reference

This is the configuration reference for running Orb natively on its host: the Apple
Silicon appliance for a pilot, or a developer's machine. The step-by-step appliance
install is [`INSTALL_RUNBOOK.md`](INSTALL_RUNBOOK.md); this file holds the settings,
backup, migration, metrics and encryption detail it points to. It reflects how the repo
actually behaves today rather than an idealized setup. Read the **Caveats** section
before any real deployment.

The container deployment (`Dockerfile`, `docker-compose.yml`) was retired on 4 October
2026. Orb is installed directly on the appliance, and a container on macOS cannot reach
the GPU. The files, and this document as it stood with its container sections, are in
`docs/archive/retired-docker-2026-10-04/`.

Deliverables that back this runbook:
- `install.sh` (one-command host install, §7) and `setup-mac.sh` (Apple Silicon dev setup)
- `run-orb.sh`, starts the stack on a configured machine
- `server/scripts/backup_db.py`, consistent SQLite backups
- `server/scripts/encrypt_db.py`, migrate a plaintext `orb.db` to SQLCipher, or check its state (§11)
- `scripts/backup.sh` + `scripts/install-backup-schedule.sh` + `scripts/restore-drill.sh`, 
  appliance backups: verify-then-rotate, launchd schedule, monthly restore drill (§9,
  and the full runbook in [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md))

---

## 1. Prerequisites

- **The host**: the appliance in [`HARDWARE_SPEC.md`](HARDWARE_SPEC.md) for a pilot.
- **Ollama** on the same machine. Orb reaches it at `ORB_OLLAMA_BASE_URL` (default
  `http://localhost:11434`).
- **Disk**: budget for the model weights. `medgemma:27b` is ~17 GB in VRAM/RAM and
  several GB on disk; the 4B fallback is far smaller.
- **RAM / GPU**: MedGemma 27B realistically needs a capable GPU or Apple unified memory
  (or a lot of RAM for slow CPU inference). On modest hardware use the **4B fallback**
  (see §4).
- **TLS** for anything beyond localhost: Orb's opt-in TLS (`ORB_TLS_CERT` +
  `ORB_TLS_KEY`, see §3) or a reverse proxy. By default Orb serves cleartext HTTP (see
  Caveats).

---

## 2. Quick start

On the appliance, follow [`INSTALL_RUNBOOK.md`](INSTALL_RUNBOOK.md). On a development
Mac, from the repo root:

```bash
./setup-mac.sh        # one time: Python, dependencies, .env, models, seeded DB
./run-orb.sh          # backend serves the built SPA on http://localhost:8000
curl -fsS http://localhost:8000/api/health
```

`./run-orb.sh --dev` adds Vite hot reload on `http://localhost:5173`, and `--lan` makes
it reachable from a second device on the same network (test data only).

---

## 3. Environment variables

Orb is configured through `ORB_*` env vars in `.env`, read by `server/config.py`.
The important ones:

| Variable | Required? | Default | Notes |
|---|---|---|---|
| `ORB_DEPLOYMENT_PROFILE` | no | `prod` | `prod` enforces the fail-closed secret checks and locks CORS to `ORB_CORS_ORIGINS`. `dev` relaxes both. |
| `ORB_JWT_SECRET` | **yes (prod)** | n/a | JWT signing key. **In `prod` the app raises `CRITICAL SECURITY ERROR` at import and will not start if this is unset.** Generate with `openssl rand -hex 32`. |
| `ORB_INGESTION_API_KEY` | **yes (prod)** | n/a | Protects the real-patient write path `/api/ingest/*`. **Also fail-closed in `prod`**, unset ⇒ the app refuses to boot. |
| `ORB_HOST` | no | `127.0.0.1` | Bind address. Loopback only by default; set `0.0.0.0` so ward devices can reach the appliance (INSTALL_RUNBOOK §7). |
| `ORB_PORT` | no | `8000` | Listen port. |
| `ORB_DB_PATH` | no | `<server>/orb.db` | Where the database file lives. |
| `ORB_CORS_ORIGINS` | recommended | dev localhost list | Comma-separated allowed origins. Lock this to your real SPA origin in prod. **Single-origin deploy:** when the backend serves the built SPA itself, the realtime socket accepts the page's own address (its `Origin` matches the `Host` header, http or https) whether or not it is listed; an empty value means same-origin only. The list is still needed for a front end on a different address, and behind a reverse proxy that rewrites `Host` (nginx's default; add `proxy_set_header Host $http_host;`, which keeps the port, where `$host` drops it). An origin the socket refuses does not just stop live updates: within seconds the browser's socket retries hold every connection it allows to that address, so ordinary screens hang for 15 to 25 seconds at a time (see INSTALL_RUNBOOK F11). |
| `ORB_ALLOWED_HOSTS` | no | derived | Extra host names Orb answers to, comma-separated (`host`, `host:port` or a URL), added to the ones it works out itself. Orb refuses with a 400 any request whose `Host` header is a name it does not know, which stops a DNS-rebinding web page from reaching it through a ward browser; a reverse proxy in front should still refuse unknown names itself. Any IP address always passes, and so do `localhost`, the hosts in `ORB_CORS_ORIGINS` and `ORB_PUBLIC_APP_BASE_URL`, `ORB_HOST` when it is a name, and this machine's own name and its `.local` name. List a name only if staff or a device reach Orb by a name in none of those, or a reverse proxy sends its upstream name as the `Host` (an nginx `upstream` block without `proxy_set_header Host $http_host;`). `X-Forwarded-Host` is never read. `*` turns the check off and the Trust Center shows it red. The log names each refused host once (INSTALL_RUNBOOK F15). The Vite dev server (`npm run dev`, port 5173) answers to the same names (`vite.hosts.ts`), so a tunnel such as ngrok must be listed here too. |
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

> **Ollama URL:** `ORB_OLLAMA_BASE_URL` (default `http://localhost:11434`) tells Orb
> where Ollama answers. On the appliance Ollama runs on the same machine, so the
> default is right.

---

## 4. Ollama models

Models are pulled into Ollama's own store on the host, so a pull survives restarts.

```bash
# Full-quality clinical model (needs a capable GPU / lots of RAM):
ollama pull medgemma:27b

# Small-hardware fallback: pull the 4B model and point Orb at it:
ollama pull alibayram/medgemma:4b
#   then set ORB_LLM_MODEL=alibayram/medgemma:4b in .env and restart Orb.

# Inspect what's installed:
ollama list
```

If Orb logs `LLM warmup got HTTP 404 for model '<name>'`, the model tag in
`ORB_LLM_MODEL` is not pulled in Ollama, pull it or fix the tag. Until then Sage,
Lens, Bridge and Scribe LLM calls return empty.

---

## 5. Backup & restore

> **On the Mac Studio appliance, do not follow this section, follow
> [`docs/BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md) and §9 below.** This section describes
> `backup_db.py`, the building block those scripts use. The appliance uses `scripts/backup.sh` (verify-then-rotate)
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

**Back up by hand** (for example before an upgrade). The script needs no app imports:

```bash
python server/scripts/backup_db.py --db-path ./server/orb.db --out-dir ./backups
# Keep only the last 30 days:
python server/scripts/backup_db.py --db-path ./server/orb.db --out-dir ./backups --retain-days 30
python server/scripts/backup_db.py --help
```

**Restore** with [`BACKUP_AND_RESTORE.md`](BACKUP_AND_RESTORE.md) §5, which stops the
server first so nothing races the swap.

---

## 6. Caveats (read before production)

These are honest limitations of the current build, not TODOs hidden in a
changelog:

- **TLS is opt-in.** By default the app serves plain HTTP. Set `ORB_TLS_CERT` +
  `ORB_TLS_KEY` (see §3) and `main.py`'s `uvicorn.run` serves HTTPS directly. Otherwise,
  put Orb behind a TLS-terminating **reverse proxy** (nginx, Caddy, Traefik) and only
  expose the proxy publicly, JWTs and PHI would otherwise cross the wire in cleartext.
- **Encryption at rest is available but opt-in, and it is not full-disk encryption.**
  Set `ORB_DB_ENCRYPTION_KEY` and `orb.db` is SQLCipher-encrypted (§11); set
  `ORB_UPLOAD_ENCRYPTION_KEY` and uploaded media is AES-256-GCM encrypted. Set neither
  and both hold PHI in cleartext on disk. Even with both set, the keys sit in `.env`
  alongside the data, so this protects **copies** (backups, exports, a disk image read
  elsewhere), not a live or stolen host whose environment the attacker can also read.
  Still encrypt the underlying storage (FileVault on the appliance) and restrict who can
  sign in to the machine.
- **Not HIPAA-certified.** This deployment has not been through a HIPAA compliance
  assessment. Treat it as a self-hosted system for which *you* are responsible for the
  surrounding controls (TLS, at-rest encryption, access control, audit retention,
  backups, BAAs).
- **`ORB_INGESTION_MODE` defaults to `ingestion`** (real data only). Do not flip it to
  `simulation`/`hybrid` on a deployment that handles real patients, it enables synthetic
  clinical-data bots.

---

## 7. Bare-metal / one-command install (`install.sh`)

For the appliance (the Apple Silicon Mac Studio Orb targets) or another host, the
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
ORB_DB_PATH=/path/to/orb.db python server/migrate.py --status
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
`server/requirements.txt`, so a native install
(`pip install -r server/requirements.txt`) already has it. It is the only
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
