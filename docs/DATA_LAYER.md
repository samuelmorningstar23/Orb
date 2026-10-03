# Orb: Data Layer

How Orb stores data today, and the seams that let it grow past a single SQLite
file.

## Backend abstraction (`server/db_backend.py`)

The backend is selected by `ORB_DB_BACKEND` (default `sqlite`). The module owns
backend selection, the SQL parameter placeholder (`PLACEHOLDER`, `?` for
SQLite, `%s` for Postgres), and the optional connection pool. Connection
*configuration* (SQLCipher, WAL/PRAGMA tuning, `row_factory`) lives in
`database.py`.

- `ORB_DB_BACKEND=sqlite`, the only wired backend. Per-call `sqlite3` connections
  (thread-safe: each thread gets its own), WAL journaling, 30 s busy timeout,
  mmap + cache tuning, optional SQLCipher encryption-at-rest.
- `ORB_DB_BACKEND=postgres`, **scaffolded, not yet wired.** Selecting it fails
  fast at import with an actionable message. The remaining work is SQL-dialect:
  Orb currently uses SQLite-specific syntax (`INSERT OR IGNORE`/`OR REPLACE`,
  `AUTOINCREMENT`, `PRAGMA`, FTS5, the `sqlite-vec` extension). Porting means
  routing those through a dialect layer and providing Postgres equivalents
  (`ON CONFLICT`, `SERIAL`/`IDENTITY`, `pgvector` for the RAG path). The seam
  (`db_backend` + `PLACEHOLDER` + the migration framework) is in place so that
  work is localized rather than spread across every call site.

## Connection pooling (`ORB_DB_POOL`)

**ON by default since 2026-08-01.** `get_db()` returns a **per-thread pooled
connection** (keyed by thread and DB path) with the PRAGMAs already applied,
avoiding ~8 PRAGMA statements per call on hot paths under load; `db_session()`
keeps a pooled connection open for reuse instead of closing it. `ORB_DB_POOL=0`
is an explicit opt-out that no appliance should take.

This section said the opposite until 2026-08-14, "Off by default. The default
per-call-connect model is correct and thread-safe", which was true when it was
written and became actively dangerous when the default flipped. The reasoning
inverted because **the unpooled path is the dangerous one**:

> Per-call connections mean a real `conn.close()` per query. On the SQLCipher
> driver Orb ships (`sqlcipher3-wheels`), `pysqlite_connection_close` holds the
> GIL across `sqlite3_close_v2`, verified by disassembling the shipped binary;
> upstream CPython's `_sqlite3` releases it there, this fork does not.
> `sqlite3_close_v2` by the last closer checkpoints the WAL, so the process
> freezes for ~1 ms per MB of WAL (measured: 17.5 MB WAL → 17.0 ms of GIL denied
> to every other thread, linear to 300 MB). Under ward-level churn on a 213 MB
> encrypted database, two threads contending the per-inode VFS mutex while one
> held the GIL deadlocked the appliance permanently: `/api/health` dead, 0.2%
> CPU, `kill -9` the only exit. Captured live 2026-08-01, three identical
> faulthandler dumps 8 s apart.

Safety must not depend on remembering a line in `.env`, so the safe path is the
default: an appliance that boots with no `ORB_DB_POOL` at all runs pooled. See
`server/db_backend.py` (~:143) for what keeps pooling safe, SAVEPOINT nesting,
the `close()`-is-a-no-op subclass, and audit writes joining the open transaction
all three of which must be preserved by any change there.

**Test-suite blind spot, stated because it has misled once already:** the suite
runs plaintext stdlib `sqlite3`, whose `close` *does* release the GIL. It cannot
reproduce the freeze in either mode. Green tests are not evidence here.

Pooling is also the natural home for a future Postgres backend, where a real
pool is essential.

## Versioned migrations (`server/migrations.py`)

An ordered, ledgered migration framework, Alembic-style versioning without the
SQLAlchemy weight the app doesn't use. Each migration is `(version, name,
apply(conn))`; `run_pending_migrations(conn)` applies any not recorded in the
`schema_migrations` ledger, in ascending order, inside the init transaction, and
records each on success. `init_db()` runs it after the (proven, idempotent)
baseline blocks.

**Adding a schema change:** append a `Migration(N, "name", fn)` to `MIGRATIONS`
never renumber a shipped version, and keep each migration idempotent
(`CREATE ... IF NOT EXISTS`, introspection-guarded `ALTER`) so it's safe even if
the ledger is reset. This replaces adding another ad-hoc block to `init_db`;
the historical blocks remain as the baseline.

## What a backup is verified against (`scripts/lib/verify_restore.py`)

The migration ledger is not only a forward mechanism, it is how a backup can be
asked whether it is *whole*. One verifier, shared by `scripts/backup.sh`,
`scripts/restore-drill.sh` and `scripts/standby-restore.sh`, checks:

| Check | Why it exists |
|---|---|
| decrypt + `integrity_check` | the file opens with the key and is not corrupt |
| **schema version** vs `MAX(version)` in the backup's own `schema_migrations` | a backup older than the shipped code predates whole modules |
| **every migrated table present** | catches the same loss when the ledger is missing, reset or lying |
| row floors on `patients`/`vitals`/`audit_logs`/`users` | an empty-but-valid database is not a backup |
| row floors on migration-seeded reference tables | those rows are written *by* the migration, so zero can only mean loss |
| audit hash-chain linkage | a broken chain restores into something indistinguishable from tampering |
| module row counts | **reported, not judged**, see below |

The version expectation is read live from `server/migrations.py`, so it cannot go
stale; the table manifest is explicit in `verify_restore.py` and pinned to
`migrations.py` by `server/tests/test_warm_standby.py::test_manifest_covers_every_migrated_table`,
so a new migration that adds a table without adding it there fails the suite.

**Adding a migration therefore has a second step:** add its tables to
`MODULE_TABLES` in `scripts/lib/verify_restore.py`, or every backup path stops
protecting them at once.

Module row counts are deliberately *not* asserted. `invoices` is legitimately
empty before billing goes live and catastrophic empty after, and nothing inside
a backup file distinguishes those. A site promotes a group to a hard check with
`ORB_BACKUP_REQUIRE_NONEMPTY=billing,payments` once the module is live.

Until 2026-08-14 all three scripts asserted four tables, `patients`, `vitals`,
`audit_logs`, `users`, which is the entire pre-migration-20 schema. A backup
predating the admin spine passed every check and the drill printed `RESULT: PASS`
while containing none of the 62 tables migrations 20-26 added.

## Safety & scale

- **No destructive migrations.** All migrations are additive; the baseline
  blocks are introspection-guarded and re-runnable. Online DB backups
  (`/api/admin/backups`) provide the rollback path.
- **Indexing for scale.** Migration 1 adds composite indexes for the busiest
  per-patient / queue lookups (patient-story merge, pharmacy verify queue,
  per-patient audit review).
- **Encryption at rest** is opt-in via `ORB_DB_ENCRYPTION_KEY` + a SQLCipher
  driver (see `docs/DEPLOYMENT.md`).

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `ORB_DB_BACKEND` | `sqlite` | Storage backend (`sqlite`; `postgres` scaffolded, not wired) |
| `ORB_DB_POOL` | **on** | Per-thread connection pooling. `0` opts out, do not; see above |
| `ORB_DB_PATH` | `server/orb.db` | SQLite file path |
| `ORB_DB_ENCRYPTION_KEY` | unset | Enables SQLCipher encryption-at-rest when a driver is present |
| `ORB_BACKUP_REQUIRE_NONEMPTY` | unset | Module groups a backup must hold rows in, e.g. `billing,payments` |
