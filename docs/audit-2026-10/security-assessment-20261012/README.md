# Defensive security regression evidence — 12 October 2026

This bundle records bounded checks against an isolated copy of Orb with synthetic users, patients, media and SQLite data. No live configuration, database, uploads, backups, keys or model weights were copied. No native network or inference requests were made. The application source was not modified.

## Results

| Check | Evidence | Observed result |
|---|---|---|
| File boundaries | `verify_boundaries.py`, `boundary-probes.log`, `boundary-results.json` | Lens scope denial and a controlled inference failure leave encrypted, unregistered files; the restore extraction primitive replaces a harmless sibling of uploads. |
| Object authorization and local media | `verify_authorization.py`, `authorization-probes.log`, `authorization-results.json` | Foreign Scribe edit/revision and shared-ward pack disclosure succeed despite scoped reads returning 404; a revoked device passes socket authentication; QR output is plaintext 0644 under umask 022. |
| Realtime and MFA | `verify_realtime.py`, `realtime-probes.log`, `realtime-results.json` | Actual in-process Socket.IO polling accepts a revoked device token and delivers vitals after user revocation; concurrent TOTP consumption succeeds twice. |

All three commands exited 0: their assertions reproduce the defects. This is **not** a passing security suite.

`evidence-manifest.json` records hashes, exact original commands, source parity and limits. All 13 recorded repository source hashes match; all 12 corresponding copied Python files match. The restore shell script was reviewed in the repository and its extraction command reproduced separately; the full script was not run. An independent reviewer checked the proof scripts and source parity.

## Interpretation limits

- HTTP probes use the actual application routes and authentication against owned data, without startup lifespan. The socket proof uses the actual Engine.IO/Socket.IO ASGI application directly, without the outer HostGate; it demonstrates authorization parity and revocation defects, not a host/origin bypass.
- The revoked-device fixture is a correctly signed synthetic device-bound token. Production exploitation requires possession of an unexpired, previously issued token; the real PIN flow normally issues a 15-minute token. PIN issuance itself was not tested here.
- TOTP scheduling is controlled by a barrier after each actual SELECT. Verification, UPDATE and commit execute unchanged. Complete concurrent HTTP login requests were not exercised.
- Lens failure uses a controlled local generator that raises. Scope denial uses the actual route. Files remain encrypted and mode 0600; this demonstrates retention/storage defects, not an encryption bypass.
- Restore uses a normal top-level member named `review-sentinel.txt`, without traversal or symlinks, inside an owned temporary fixture. Full restore, its database/key/primary guards and installation sequence were not exercised.
- The raw authorization JSON key `foreign_department_name_present` is misnamed: the assertion establishes the foreign **patient's** name in the ward-pack HTML. Raw observations are preserved unchanged.
- JWTs, MFA secrets/codes, encryption keys, the synthetic DB, media, archive and source copy are excluded from this durable bundle.

## Reproduction boundary

These scripts are archived records of the run, not production tools or a standalone pytest suite. They expect a fresh owned work directory containing `source/server`, `source/dist` and `home`; use `scripts/e2e_server.py:copy_application` to copy only permitted source/reference files and built assets. Copy the three scripts into that directory, keep their `REPO` path pointed at the checkout under review, and execute **boundaries → authorization → realtime** in that order with the installed test Python environment. The latter two intentionally reuse the first script's synthetic database. Re-running in the original directory fails fixture uniqueness assertions; use a new directory. Never point their DB or source paths at an appliance.

The original temporary fixture is retained for review. The commands own no native listening ports; both socket sessions and server service tasks were closed. Browser evidence is packaged separately.
