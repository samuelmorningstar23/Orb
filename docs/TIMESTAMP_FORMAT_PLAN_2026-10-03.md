# Plan: one timestamp format for `vitals` and `risk_scores` (2026-10-03)

Status: **BUILT 2026-10-03.** Samuel approved R1-R5 as recommended. See section 9 for what landed.
Owner session: "Normalise vitals and risk_scores timestamp formats".
Evidence: probe and census scripts in a scratch folder outside the repository (re-runnable; described in section 2).

---

## 1. The problem in one paragraph

Both columns are ordered as TEXT (`ORDER BY timestamp DESC`, `MAX(timestamp)`), and the rows in them are written in up to five spellings: SQLite's `CURRENT_TIMESTAMP` (`2026-10-02 19:26:36`), ISO UTC (`...T13:26:35.789391+00:00`), ISO `Z`, ISO with an Indian offset (`...T22:56:35+05:30`), and naive ISO with no offset at all. Text order is only chronological inside a single spelling. Across spellings, the board shows the wrong "latest" row, and it is wrong in the direction that hides a deterioration.

## 2. What I verified (2026-10-03, through the real code paths)

I took a throwaway DB in the scratchpad, called the production writers, then called the production readers.

**A. risk_scores (your item 1). Confirmed.**
`nurse_shift_service.record_observation` charted a benign set taken 6 h earlier (stored `2026-10-02T13:26:35.789391+00:00`, level `stable`). Then `ingestion_service._persist_risk_score` wrote a `critical` row now (stored `2026-10-02 19:26:36`).

| Reader pattern | Picked |
|---|---|
| `ORDER BY timestamp DESC LIMIT 1` | stable (wrong) |
| `MAX(timestamp)` join (search, surgery, Sage census, narrator; the ward board used it until 2026-10-03, see section 8) | stable (wrong) |
| `vigil_routes._load_latest_risk` | stable (wrong) |
| `ORDER BY julianday(timestamp) DESC` | critical |

In IST the UTC day starts at 05:30, so a nurse set charted at 06:00 can mask every ingested critical score until 05:30 the next morning.

**B. vitals (your item 2). Confirmed, and worse than reported.**
Three readings for one patient: `+05:30` from 2 h ago (HR 60), `Z` from 5 min ago (HR 140, the true latest), and naive IST wall clock from 3 h ago (HR 80). The ingestion adapters store all three verbatim.

* `ORDER BY timestamp DESC, id DESC` picked **HR 60**, the 2-hour-old row.
* `ORDER BY julianday(timestamp) DESC` picked **HR 80**, the 3-hour-old row. SQLite reads a naive string as UTC, which puts an IST wall-clock reading 5.5 h in the future. **julianday() cannot fix this case.** The front end (`src/lib/datetime.ts:parseUTC`) and every Python `_parse_ts` also read naive strings as UTC, so these rows are displayed 5.5 h ahead too.
* The sink already knows the right rule. `vitals_source/sink.py:_parse_iso` and the HL7 parser read a naive device time in the appliance zone (IST) and convert to UTC. But that rule is only used for the backfill/skew decision; the stored row keeps the raw string. HL7 is the one lane that stores UTC `+00:00` today.

**C. Window filter (your item 3). Confirmed.**
`vigil_routes._load_recent_vitals(pid, 1)` returned a row charted 7 h ago. `timestamp > datetime('now', ?)` compares `...T...` against `... ...`, and `T` sorts above a space, so every ISO row from the cutoff's UTC date passes.

**D. Size of the backfill on this machine.**
A read-only census of a consistent copy of `server/orb.db` (taken with `scripts/backup_db.py`): `risk_scores` has 30 rows and `vitals` 583, **all already ISO `+00:00`** (the 2026-09-07 seed). The dev box runs the simulation lane, which never writes `risk_scores`, so the mix never formed here. There is no pilot DB. **Today the backfill would rewrite zero rows on the only DB that exists.** It exists to protect any DB that has run the ingestion or admission lanes (a demo box, a restored backup, the future pilot DB). The census found one other mixed column in the whole DB, `claim_events.occurred_at` (4 ISO, 1 space), which is out of scope here.

## 3. Writers today

| Writer | Table | Stores today | Problem |
|---|---|---|---|
| `ingestion_service._persist_risk_score` (~306) | risk_scores | column default `CURRENT_TIMESTAMP` | space format |
| `admission_service` baseline (~226) | risk_scores | column default | space format |
| `nurse_shift_service._record_observation_txn` (~1063 dedupe, ~1098, ~1109) | both | `observed_at.isoformat()` | keeps any client offset (an API caller sending `+05:30` is stored as `+05:30`); the UI sends `Z`, which becomes `+00:00` |
| `ingestion_service` vitals insert (~252), fallback (~180), live emit (~874) | vitals | device `observed_at` verbatim (JSON ~514, CSV ~633, FHIR ~802) | `Z`, offsets, naive |
| `vitals_source/manual_source.py` (~231) | vitals | caller's `observed_at` (admission passes aware UTC ISO) | fine today, no guard |
| `nervous_system_triggers` Scribe extraction (~698) | vitals | aware UTC ISO | fine |
| `bot_service` simulator (~357) | vitals | aware UTC ISO | fine |
| `scripts/seed_clinical_data.py` (~399, ~442, ~463) | both | aware UTC ISO | fine |

## 4. The two options

### Option 1 (recommended): one stored format, written explicitly, plus a one-off backfill

* **Canonical form:** `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`, i.e. `dt.astimezone(utc).isoformat(timespec="microseconds")`. It matches almost every ISO row already stored, so the backfill rewrites almost nothing that is already ISO. Text order equals time order.
* **One helper,** a new `server/services/clinical_time.py` (`hospital_time.py` says it never formats stored data, so it stays separate):
  * `utc_iso_now()`
  * `to_utc_iso(value, *, naive_tz=None)`. It accepts a datetime, `Z`, any offset, or SQLite's space format (read as UTC). A naive ISO string is read in `naive_tz`, and device lanes pass the appliance zone, the same rule as `sink._parse_iso` and HL7. It raises on garbage; it never guesses.
* **Writers:** every row in the table in section 3 stores through the helper. `_persist_risk_score` and the admission baseline pass the timestamp explicitly instead of relying on the column default. The nurse lane converts to UTC before storing *and* before the replay-dedupe lookup, so both sides use the same spelling. The ingestion live emit sends the canonical string, so the live push matches a reload.
* **Readers:** no ORDER BY changes anywhere. Two edits only:
  * `vigil_routes._load_recent_vitals` binds a canonical cutoff instead of `datetime('now', ?)`.
  * `fhir_export_service.observations_to_fhir(since=...)` normalises the caller's `since`.

  I checked every other time-window reader on these tables. They already bind `+00:00` cutoffs (vigil_service trend, ipass, escalation sweep) or already use julianday/datetime() (impact_service, admin_routes).
* **Backfill:** migration 42 (the number is claimed at build time; other branches add migrations). It rewrites only rows that do not match the canonical pattern:
  * space format: read as UTC (that is what `CURRENT_TIMESTAMP` is); lossless.
  * `Z` and explicit offsets: converted to UTC; same instant. The device's stated offset survives in the raw ingestion event.
  * naive ISO: **needs ruling R2.**
  * unparseable: left untouched, counted, logged.

  It updates by id in the boot migration transaction, writes one PHI-free audit row per table with counts per class, and is idempotent (a second run changes 0 rows).
* **Guard:** a source-scan test (like the Scale 2 AST guard) fails if any non-test `INSERT INTO vitals` or `INSERT INTO risk_scores` omits the timestamp column. That catches a future writer silently falling back to `CURRENT_TIMESTAMP`.

Why it wins: it fixes all three items, including naive device times (julianday cannot); it keeps every query on the `(patient_id, timestamp)` indexes; and it is what three peer sessions are already building on (see section 7).

### Option 2: wrap read sites in julianday()

* It fixes item 1 and the `Z`/offset half of item 2. It does **not** fix naive device times (proved in 2B).
* There are about 20 read sites (risk_scores: vigil_routes x2, search_routes, surgery_routes x2, surgery_service x2, sage_service x4, sage_narrator, bot_service x2, bridge, forecast x2, sage_ambient, voice_agent, discharge x2, world_model; vitals: about 15 more).
* `ORDER BY julianday(timestamp)` cannot use `idx_risk_patient (patient_id, timestamp DESC)`, so every "latest" becomes a per-patient sort. The ingestion lane writes one risk row per monitor message and never prunes (about 1,400 rows per patient per day), so the ward board would sort tens of thousands of rows per patient per load after a month. Expression indexes could recover that, but every query would have to repeat the index expression exactly.
* Every future reader has to remember the wrapper. That is how this class keeps coming back (see the notes in `migrations.py` `_m4` and `patient_story_service.py`).

I do not recommend it, alone or as a belt-and-braces addition to Option 1.

## 5. Tests (written to fail on today's code, then pass)

Under Option 1 the readers do not change, so a test that hand-inserts mixed rows and asks a reader for "latest" would stay red even after the fix. Tests therefore go through the writers or through the migration.

1. **Writers store one format:** nurse obs (UI `Z` and an API `+05:30` taken_at), ingestion JSON with `+05:30` / `Z` / naive observed_at, CSV, FHIR, the admission baseline, Scribe extraction, and the simulator tick. Each stored string matches the canonical pattern and the correct instant; naive JSON is read as appliance-zone time.
2. **Mixed lanes, latest risk:** a nurse set at T-6 h, then an ingestion critical now. `_load_latest_risk`, search, the surgery board, the Sage census and the narrator all say `critical`. Today they say `stable`.
3. **Device offsets, latest vitals:** the three readings from 2B. `get_patient_risk` and the ward board row (`GET /api/vigil/patients`, which now picks the latest vitals row by text order) both show HR 140. Today it is HR 60.
4. **Window:** a 7 h old row is not in a 1 h window. Today it is.
5. **Migration:** hand-inserted space, `Z`, `+05:30`, naive and garbage rows. After the migration they are canonical with the same instants (naive per R2), garbage is untouched and counted, the audit row is written, a second run changes 0 rows, and the readers now pick correctly.
6. **Guard:** the source scan from section 4.

They run with this command, in isolation first, because other work is mid-edit:
`cd server && env -u ORB_MFA_REQUIRED_ROLES -u ORB_DEPLOYMENT_PROFILE .venv/bin/python -m pytest -q -p no:cacheprovider <files>`

## 6. Rulings needed

* **R1. Option.** Option 1 (recommended), or Option 2.
* **R2. Naive device times already stored.** Re-read them in the appliance zone (IST), the rule the sink and HL7 parser applied when they ingested those rows (recommended). The alternative is to leave them untouched and only report the count. This is the one part of the backfill that changes an instant rather than a spelling: it moves those readings 5.5 h earlier, to when they actually happened.
* **R3. Where the backfill runs.** As a boot migration (recommended while no pilot DB exists, since it is a no-op here), with a `scripts/backup_db.py` copy taken first on any DB where it has rows to rewrite. The alternative is an operator-run script. Before a pilot I would time it on a synthetic 1M-row DB either way.
* **R4. What risk_scores.timestamp means for the ingestion and admission rows.** Keep the WRITE time (recommended; it is today's meaning, only the spelling changes). Stamping the device's observed time instead would let a device with a slow clock push its fresh scores below an older nurse set, which is exactly what the sink's "liveness is receipt time" rule exists to prevent.
* **R5. Scope.** Recommended: these two tables only. `observations.observed_at` (labs) is stored verbatim by the same ingestion code and has the same bug on the lab card. I would put it through the helper in a follow-up with its own backfill ruling.

## 7. Found on the way (not in this change unless you say so)

* (Relay half FIXED 2026-10-03 by the risk-tiebreak session: timestamp first, id as tiebreak. See docs/archive/pre-risk-tiebreak-2026-10-03/NOTES.md.) `relay_service` (~77) took the latest risk by `MAX(id)` and `sage_proactive_service` (~165) by `ORDER BY id DESC`. A nurse back-charting an older reading gets the highest id, so Relay and the board can disagree about the current level. Ruling needed on whether "latest" means time or insertion.
* `bot_service` prune (~324, simulation mode only) deletes every `risk_scores` row beyond 100 per patient, including nurse-charted rows that carry a fail-closed audit entry. Dev-only today.
* The `MAX(timestamp)` joins return a patient twice if two rows tie exactly. That class belongs to the id-tiebreak session.
* `claim_events.occurred_at` is mixed in the dev DB (1 space row).

## 8. Coordination

* **"Add id tiebreak to latest-vitals readers":** compatible. They add `, id DESC` to vitals readers and leave risk_scores readers to this change.
* **"Show NEWS2 on the Vigil ward board row":** LANDED 2026-10-03 (verified). `list_patients` no longer reads risk_scores; both `MAX(timestamp)` joins are gone. It now picks each patient's latest vitals row with `ORDER BY v2.timestamp DESC, v2.id DESC LIMIT 1` (vigil_routes ~343-353), which is only correct under Option 1. It is pinned by `tests/test_vigil_board_news2.py` (all ISO `T` rows, so it does not fight the migration). The remaining vigil_routes items for this change are the chart route's risk history (~76), `_load_latest_risk` (~140) and the window filter (~152).
* **"Push nurse-charted vitals to open charts":** shares hunks in `nurse_shift_service._record_observation_txn` and `nervous_system_triggers` (~698). Different hunks; whoever lands second re-reads first. They will store through the helper rather than add their own.
* **"Fix SIRS rule"** and **"Stop passing the 0-1 risk index"**: no overlap.
* Before editing: back up every touched file to `docs/archive/pre-timestamp-format-2026-10-03/` with SHA256SUMS, hash-check each file before each edit, use targeted edits only, and diff against that backup afterwards.

## 9. What landed (2026-10-03)

Rulings: R1 Option 1, R2 naive device times read in the appliance zone, R3 boot migration, R4 write time for ingestion and admission risk rows, R5 these two tables only.

* **Helper:** `server/services/clinical_time.py`, with `format_utc`, `utc_iso_now`, `to_utc_iso`, `parse_to_utc`, `device_naive_tz`, `CANONICAL_GLOB` and `backfill_canonical_timestamps`.
* **Writers:**
  * `ingestion_service`: the vitals `observed_at` goes through `to_utc_iso(..., naive_tz=device_naive_tz())`. An undateable value is stored at receipt time and logged; the sender's string stays in the raw event. `_persist_risk_score` passes `utc_iso_now()`.
  * `admission_service`: the baseline risk row gets `to_utc_iso(now)`, the same instant as the admission vitals row.
  * `vitals_source/manual_source.record_at_admission`: `to_utc_iso(observed_at)`.
  * `nurse_shift_service._record_observation_txn`: converts `observed_at` to UTC and uses one `stored_at` for the dedupe key, both INSERTs, the audit metadata and `recorded_at`. `_push_vitals_update` uses `format_utc`, to match the nurse-push work.
  * `nervous_system_triggers._persist_extraction`, `bot_service` simulator tick, `scripts/seed_clinical_data.py`: through the helper.
* **Readers:**
  * `vigil_routes._load_recent_vitals` binds a canonical cutoff.
  * `fhir_export_service.observations_to_fhir` normalises `since` and raises ValueError on garbage; `GET /api/fhir/patient/{id}/$everything` returns 400 for an unreadable `since`.
* **Backfill:** migration 43, `canonical_vitals_risk_timestamps`. On the dev DB it is a no-op: 0 non-canonical rows in the 2026-10-03 01:20 backup, `backups/orb-backup-20261003-012045.db`. That DB's ledger stops at 38, so 39 to 43 apply together on the next server restart.
* **Timing:** 1.75M rows rewritten in 86 s on an unencrypted synthetic DB (1M space-format risk rows, 750k `Z`/offset/naive vitals rows). A second run took 1.9 s and rewrote 0. Expect somewhat slower on SQLCipher. Any DB created after this change stores canonical rows from the start, so only a pre-existing ingestion DB pays this cost.
* **Tests:** `server/tests/test_timestamp_canonical.py`, 13 tests. All 13 failed before the fix and all 13 pass after. `tests/test_vitals_source.py` had one assertion pinning the old spelling of an admission timestamp; it now expects the canonical spelling of the same instant.
* **Untouched on purpose:** `scripts/wedge_bench.py` (it already writes UTC ISO into its own bench DB), `observations.observed_at` (R5), and the found-on-the-way items in section 7.
* **Backup:** `docs/archive/pre-timestamp-format-2026-10-03/` with SHA256SUMS.
* **Full suite (2026-10-03):** 8,375 passed, 8 failed, 2 skipped, 7 xfailed. None of the 8 comes from this change:
  * 4 audit-chain checks: a pre-existing bug, an integer `target_id` in the ingestion Relay alert's audit row, hashed as a number but stored as TEXT. It was exposed by a new test from parallel work. Located with a per-test chain-verify plugin and split out as its own task.
  * 2 Sage name-matcher rows: a parallel change raised the fuzzy name minimum to 6 letters.
  * 1 Sage phrasing test (`test_a_bed_alone_is_enough_to_warn`): Sage answered "outside your department" where the test expected "NO MATCHING PATIENT". Cause not confirmed; it could be the run's test order or the name-rule change.
  * 1 scope diagnostic: `vigil_routes.py` was edited mid-run.
  The chain, phrasing and diagnostic failures (6) all pass in isolation.
