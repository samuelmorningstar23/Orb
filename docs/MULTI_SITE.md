# Running more than one facility on one Orb appliance

Runbook for the person who installs and runs Orb for a hospital group: what has to be true before a second facility is added, the go-live drill that proves it on a copy, and what to watch afterwards. Written 20 October 2026 (multisite-18, wave 14 of the hospital-system build).

## Where this stands today

**No second facility can be added to a hospital's appliance yet, and that is deliberate.** Orb's create gate refuses while any screen group has not been taught to keep two facilities' patients apart. On 20 October 2026, 47 route groups are still on the facility fence's blind list (`facility_fence.FACILITY_BLIND_SURFACES`). Waves 12 and 13 swept most of them, but clearing a group from the fence is new behaviour that needs its own review, so the list has not been regenerated yet. Until an Orb release ships that list empty, the drill below can only be rehearsed with the fence opened inside a test process, which is what `server/tests/test_two_facility_e2e.py` does.

Everything else in this runbook works now and can be prepared ahead: facility 1's identity, the group-wide grant, the audit index, the departments list, ingestion keys, mirrors and the reverse proxy.

Check the gate at any time, signed in as an administrator with group-wide access, on the Facilities screen (Admin, Facilities), or from the appliance:

```
GET /api/facilities/create-check   ->  {"allowed": false, "reasons": [{code, message}, ...]}
```

## What Samuel ruled, and what follows from it

- **One appliance per legal entity** (ruling M1, MS-R1 (a)). Every facility on the appliance carries the same PAN. Orb refuses a facility whose GSTIN or PAN names another legal entity.
- **Isolation is keyed on data, not on the switch** (ruling M2). The moment the facilities table holds a second row, active or not, every account bound to a facility sees only that facility's patients, whatever `ORB_MULTI_SITE` says. Only a group-wide administrator sees every facility. Rows are never deleted. A restore of a two-facility backup on a machine with the switch off still keeps the facilities apart.
- **The runtime fence** turns any screen not yet swept into a 503 ("This screen is not available yet when more than one facility is on record") for everyone, instead of a disclosure. It holds `/socket.io` too.
- **Department, ward and bed names are unique across the group** (MS-R4 (a)). A department belongs to one facility, so a ward nurse is kept to her facility without any extra rule.
- **A department-less account** (administrator, pharmacist, cashier) sees the patients whose latest encounter is at its facility (MS-R5 (a)).
- **Group-wide reach** is a named, audited grant held only by administrators (MS-R6 (a)).
- **Moving a patient between facilities is refused** (MS-R3 (c)) until ruling R009 is made. Every admission, transfer, roster import and history import lane refuses a move into another facility and writes an audit row.
- **A restore rolls back every facility** (MS-R12 (a)). There is no per-facility restore. Data and Backups says so once a second facility exists or the switch is on.

## The switch: ORB_MULTI_SITE

Set in the operator's environment file (`ORB_MULTI_SITE=1`; `1`, `true`, `yes` and `on` all count). Off by default.

It gates growth only:

- creating and closing facilities (the create gate also needs everything in the next section);
- staff at a facility other than 1, and moving an account's home facility;
- group-wide grants (`scripts/grant_enterprise_access.py` and User Management);
- facility labels and selectors on screen, the price list editors, the group identity match at the front desk and the visiting consultant's session facility switch.

It never gates isolation. Turning it off with two facilities on record stops all of the above, ends every visiting consultant's session at its next request, and changes nothing about who sees which patient. The drill proves this (step 11).

## Before the second facility: the create gate

`facility_identity.can_create_facility` refuses, with a reason for each, until all of these hold.

| Gate reason | What makes it pass |
|---|---|
| `switch_off` | `ORB_MULTI_SITE=1` in the environment of the server (and of any console script you run). |
| `no_group_grant` | The administrator adding the facility holds a live group-wide grant (below). |
| `screens_not_ready` | `FACILITY_BLIND_SURFACES` is empty. This is a property of the Orb release, not a setting. |
| `audit_index_missing` | `idx_audit_facility` is built (below). |
| `main_gstin_missing` | Facility 1 has its GSTIN, on the Facilities screen. |
| `pan_rule` | Every facility on record carries the same PAN. |

### Facility 1's identity

On the Facilities screen, as an administrator: legal name, address, GSTIN (the state code fills from it), PAN, HFR id, NABH number. This works with the switch off. A numbered invoice keeps the supplier details it was issued with.

### Facility 1's departments list

Once a second facility exists, an account whose department no facility lists sees no patient at all. Before the drill, every department facility 1's staff, admissions and wards use must be on facility 1's departments list.

- The operations status page (`GET /api/ops/status`, check "Facilities") lists `departments_not_in_the_list`. Caveat found during the drill rehearsal: that list also counts the ward written into each patient portal login's department column, so a ward name there is not a missing department. The seed script's check leaves portal logins out.
- There is no screen for the departments list yet. On the verify stack copy, `scripts/seed_second_facility.py` lists the missing ones at facility 1 itself before it adds the second facility (through `services/department_master.py`, the list's only writer, audited).

### The grant bootstrap

Group-wide access is a named grant with a reason. Only an administrator who already holds one can give or revoke one in User Management, so the first one is made at the appliance's own keyboard:

```
cd server
python scripts/grant_enterprise_access.py --grant ADM001 --reason "Group CIO"
python scripts/grant_enterprise_access.py --list
python scripts/grant_enterprise_access.py --revoke ADM001 --reason "Left the group"
```

- Needs `ORB_MULTI_SITE=1`, an interactive terminal, and the account id typed back to confirm. The account must be an active administrator, not a system account. One live grant per account.
- Run it with the same `ORB_DB_PATH` and `ORB_DB_ENCRYPTION_KEY` as the server. Never source the shipping environment file into a test run.
- Each grant and revoke is one transaction with a fail-closed security audit row (`enterprise_access_granted`, `enterprise_access_revoked`), actor role `console`, with the operating system user and host.
- The running server reads grants through a 30-second cache, so a console grant or revoke takes effect there within 30 seconds. A revoke made in User Management takes effect on the next request.
- Keep group-wide grants to the people who must see every facility (the group CIO, the group medical director's admin account). Every other administrator works at one facility.

### The audit index window

An audit row's facility lives inside its hashed metadata, so the audit chain never changes shape (ruling M6). Reading one facility's audit rows then needs an index on `json_extract(metadata_json, '$.facility_id')`. Building it reads every audit row, so it is a maintenance step with Orb stopped, never a migration.

```
cd server
# 1. Time it on a copy first (scripts/backup_db.py makes a consistent one).
python scripts/build_audit_facility_index.py --db /path/to/copy.db --check
python scripts/build_audit_facility_index.py --db /path/to/copy.db --build --yes
# 2. Then, with Orb stopped, on the real database.
../stop-orb.sh
python scripts/build_audit_facility_index.py --check
python scripts/build_audit_facility_index.py --build --yes
../run-orb.sh
```

- `--check` changes nothing. It counts the rows and confirms every row's metadata can be read as JSON. The build is refused while any row cannot be read, and those rows are named; audit rows are never edited.
- `--build --yes` builds the index in one transaction, holding the write lock for the whole run, and writes one `site_admin` row, `audit_facility_index_built`, with the row count and the seconds it took.
- Any future rebuild of `audit_logs` must recreate `idx_audit_facility`, or the create gate refuses again.

Measured on 20 October 2026, Apple M2 Max, encrypted database:

| Copy | Audit rows | File | Check | Build |
|---|---|---|---|---|
| The dev database (backup of 20 October 22:25) | 100,303 | 716 MB | about 1 s | 0.13 s |
| The same copy grown with copies of its own rows | 2,000,000 | 2.26 GB | 2.2 s | 3.1 s |

The check times include the command's own start-up; the build times are the index statement alone.

A 200-bed hospital writes about 41,000 to 56,000 audit rows a day (docs/CAPACITY_200_BED.md), so 15 to 20 million a year. On this hardware that is roughly half a minute to a minute of build after a year, and some minutes after several years, plus reading the file from disk if it is not in memory. Plan a 30-minute window: stop, check, build, start, and confirm the Facilities screen no longer lists `audit_index_missing`. Time it on a copy of the site's own database first: that number, not this table, sets the window.

## Ingestion keys

Bedside monitors and other senders must be bound to a facility once two facilities are on record. A reading from a sender bound to no facility, or naming a patient at another facility, files nothing and takes the unknown-patient path with an `ingestion_source_unbound` audit row (MS-R14 (a)): a vital filed on the wrong patient is worse than one held for review.

- **REST ingestion** (`POST /api/ingest/json`, `/api/ingest/hl7`, header `X-API-Key`): `ORB_INGESTION_API_KEY` is facility 1's key. Each other facility gets its own, `ORB_INGESTION_API_KEY_<CODE>` with the facility code in capitals (for example `ORB_INGESTION_API_KEY_SAT1`). One key per facility, never shared: a key given to two facilities binds neither (`key_shared`).
- **The MLLP monitor feed**: name its `ORB_MONITOR_SOURCE_ID` under the facility's block in `server/site_config/site.toml`:

  ```toml
  [[facilities]]
  code = "MAIN"
  ingestion_sources = ["monitor_hl7_main"]

  [[facilities]]
  code = "SAT1"
  ingestion_sources = ["monitor_hl7_sat1"]
  downtime_mirror_dir = "/Volumes/WardPacksSAT1"
  ```

  A source may be listed under one facility only. A source claimed twice stops the boot with the file and line.
- The operations status page shows each key's facility code and whether it is on record and active, the names (never the values) of `ORB_INGESTION_API_KEY_*` settings that bind nothing, and refused readings.
- Today `/api/ingest` is still on the fence's blind list, so REST ingestion answers 503 while two facilities exist, until the laboratory and ingestion sweep clears it. Plan any sender that posts over HTTP for after that release. The MLLP feed is not an HTTP route and is bound through `site.toml` as above.
- `site.toml` is read from `server/site_config/site.toml` (or the file `ORB_SITE_CONFIG_FILE` names) once, at start-up: restart Orb after changing it.

## Satellite mirrors and downtime packs

Each facility's ward packs (the printable downtime record) are kept to that facility.

- Facility 1 keeps the export folder packs have always gone to. Every other facility writes to `<export folder>/facilities/<CODE>/`.
- `ORB_WARD_PACK_MIRROR_DIR`, the appliance's mirror from before, becomes facility 1's only. It never receives another facility's packs.
- Give each other facility its own `downtime_mirror_dir` in its `[[facilities]]` block: an absolute folder, on storage at that facility (a nurse-station PC share or a mounted drive there). A satellite reached over a WAN has nothing during a link outage unless its mirror is local to it.
- No two facilities may share a mirror, and none may sit inside another's: the boot refuses that with the file and line. At every pack build, a mirror that overlaps another facility's folder is not used, for either facility, and the build reports `mirrors_refused`. A satellite with no mirror of its own gets none (`mirror_missing`); it never falls back to the shared one.
- Known gap: `scripts/backup.sh` archives only `<export>/ward-pack.*.latest.html`, so satellite packs are not in the nightly encrypted backup set. Until it gains `facilities/*/ward-pack.*.latest.html`, the satellite's own mirror is its only copy off the appliance.
- When a facility leaves the group, `scripts/facility_extract.py --facility SAT1 --out <empty folder> --reason "..."` writes its patients' records (FHIR R4) to a local folder at the console, audited fail-closed. A backup cannot do this: it is the whole appliance.

## A reverse proxy and the Host header (sockets)

Live screens (the ward board, Relay, live vitals) run over Socket.IO. Two checks decide whether a browser's socket is accepted, and a reverse proxy can break both.

- **The Host allow-list** (`services/host_gate.py`): a request whose `Host` is a name Orb does not know gets 400. Any IP address passes; `localhost`, the hosts in `ORB_CORS_ORIGINS` and `ORB_PUBLIC_APP_BASE_URL`, `ORB_HOST` when it is a name, and the machine's own names pass. `ORB_ALLOWED_HOSTS` adds names. `X-Forwarded-Host` is never read.
- **The socket's origin check** (`ws/realtime.py`, `_origin_allowed`): the page's `Origin` must be listed in `ORB_CORS_ORIGINS`, or be the page's own address (the `Origin` host and port equal to the `Host` header).

nginx's default `proxy_pass` sends the upstream's address as `Host`. The socket then sees an `Origin` of, say, `https://orb.sat1.hospital.local` against a `Host` of `127.0.0.1:8000`, refuses it, and the browser's retries hold every connection it allows to that address, so ordinary screens hang for 15 to 25 seconds at a time (INSTALL_RUNBOOK F11).

Pass the name staff typed through, with the port, and the upgrade headers:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $http_host;          # not $host: $host drops the port
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 120s;
}
```

With more than one facility, staff at each facility often reach the appliance by a name of their own. Each such name must pass both checks: either the proxy passes `Host` through as above (then the page's own address is accepted), or every facility's address is listed in `ORB_CORS_ORIGINS` and its name in `ORB_ALLOWED_HOSTS`. Never set `ORB_ALLOWED_HOSTS=*` (it turns the check off, and the Trust Center shows it red). A proxy in front should still refuse names it does not know.

Two facility-specific socket behaviours to expect:

- Sockets join facility rooms once two facilities are on record. The moment the second facility first appears, every open socket's facility view changes, so every open socket is dropped and reconnects at its next delivery. Tell the wards a brief reconnect is expected.
- While `/socket.io` is on the blind list the fence refuses every socket handshake with 503, so live updates stop everywhere while two facilities exist on a release that has not cleared it.

## Accounts at each facility

- Ward staff (doctor, nurse, ward clerk) work at their department's facility. Choose the department in User Management; there is no separate facility for them.
- Department-less staff (administrator, pharmacist, cashier) need a home facility. With the switch on, User Management offers it when the account is created, and `POST /api/admin/users/{id}/facility` moves it (audited fail-closed; the old home row is revoked, never deleted).
- An administrator manages only the accounts of the facilities in their own scope. A group-wide administrator manages every facility's.
- Visiting consultants: the session facility switch is built (multisite-19), but nothing writes a visiting access row yet, so for the first go-live a consultant who works at two facilities has an account at each (MS-R7, recommendation (b) for the first go-live).

## The go-live drill

Run it on the isolated verify stack, on a copy, before the second facility is added to the hospital's appliance. It needs an Orb release whose `FACILITY_BLIND_SURFACES` is empty.

**0. Confirm the release is ready.** From `server/`: `python -c "import facility_fence as F; print(F.FACILITY_BLIND_SURFACES)"` prints `()`. Otherwise stop: the create gate will refuse, and it should.

**1. Copy the database.** `python scripts/backup_db.py --out-dir <scratch>/drill --retain-days 9999` (online backup API, integrity-checked). Never copy a live database file with `cp`.

**2. Build the audit index on the copy, and time it.** As in "The audit index window", with `--db <copy>`. Write the seconds down: that is the real window for the appliance.

**3. Start the verify stack on the copy.** Its own backend on a free port with `ORB_DB_PATH=<copy>`, `ORB_DEPLOYMENT_PROFILE=dev`, `ORB_MULTI_SITE=1`, `ORB_INGESTION_MODE=ingestion` (not the simulator, whose ticks write into the copy) and its own Vite with that port in `ORB_CORS_ORIGINS`. Never point it at the hospital's database.

**4. Grant group-wide access** on the copy: `python scripts/grant_enterprise_access.py --grant <admin id> --reason "Two-facility go-live drill"`, with the stack's environment.

**5. Facility 1's identity.** Sign in as that administrator, open Facilities, enter facility 1's GSTIN and address. The create check should now list nothing.

**6. Write and check the plan.**

```
python scripts/seed_second_facility.py --example-plan > drill-plan.json
# Fill in the facility block: the satellite's own name, legal name, GSTIN
# (same PAN as facility 1), address. Keep or change the people.
python scripts/seed_second_facility.py --db <copy> --plan drill-plan.json --admin <admin id>
```

The check writes nothing. It prints "Ready" or every reason it is not: the switch, the profile, a name already in use anywhere in the group, the create gate's reasons, and facility 1's departments it will list first.

**7. Seed.**

```
python scripts/seed_second_facility.py --db <copy> --plan drill-plan.json --admin <admin id> \
    --accounts-out drill-accounts.txt --apply
```

Before it asks for anything, it runs every check of step 6 again and stops at any problem, having written nothing. A sign-in writes to the database it signs in to (an audit row, and a refused password counts toward the account's lockout), so a copy the script refuses is never signed in to, and the prod profile and the database at Orb's default location (or a hard link to it) are refused before the database is even opened. Only then does it ask for the administrator's password (and a TOTP code if the account has one) and sign in through the product. It then checks once more as that session, the create gate through the product as the Facilities screen asks it. If that refuses, the script says nothing of the plan was written and that the sign-in itself is on the copy's audit trail. Everything it writes goes through the product: the facility through `POST /api/facilities` (the create gate), the staff through User Management, the patients through the admission form signed in as the new facility's doctor, each with any allergy and medicine the plan gives. It records no observation, because a set entered by a script would be filed as charted by a person. Every request carries the user agent `Orb seed_second_facility.py (two-facility drill on a verification copy)`. Passwords go only to `drill-accounts.txt` (created 0600, never written over). It refuses the prod profile and the database at Orb's default location, and any plan name that labels itself (demo, test, sample and the like): nothing in Orb may present itself as a demonstration.

**8. The browser pass, one role at a time.** Sign in as each account below, open every screen its rail offers, and take a screenshot of each. On every screenshot, look for any patient name, hospital number, diagnosis or medicine of the other facility. One is a failure.

| Account | Expect to see | Must not see |
|---|---|---|
| Facility 1 nurse | Facility 1's ward board, charts, Relay rooms | Any facility 2 patient |
| Facility 2 nurse | Facility 2's ward board and its two patients; the role line names facility 2 | Any facility 1 patient |
| Facility 2 administrator | Facility 2's staff, its audit rows from after the facility was added, facility 2 in Facilities | Facility 1 patients, facility 1's audit history |
| Facility 2 pharmacist | Facility 2's verify queue and orders; stock "at" facility 2 on Helix | Facility 1's queue or patients |
| Facility 2 cashier | Facility 2's patients in the counter's patient search | Facility 1's patients, bills or receipts |
| Group-wide administrator | Both facilities: Command Center facility tiles, both in audit, both in Facilities | Nothing withheld |

Also try one cross-facility link directly: as the facility 2 nurse, open a facility 1 patient's chart address. It must answer "not found", and the audit view of the group-wide administrator (or of facility 1's administrator) must show a `patient_scope_denied` row with `scope_cause` facility. The row belongs to the patient's facility, so facility 2's administrator does not see it.

**9. Chart one observation set per facility 2 patient** as the facility 2 nurse. Facility 2's board updates live; facility 1's board does not tick for it.

**10. Two-facility soak.** `python scripts/soak_test.py --db <the seeded copy> --minutes 15` boots its own throwaway server on a copy of the copy and drives pilot-volume load. It drives facility 1's load only (it signs in as facility 1's doctor); keep the facility 2 nurse's board open in the browser while it runs and confirm it stays live and correct. The SLOs are the soak's own (health p99 under 250 ms, interactive reads p99 under 2 s).

**11. Boot with the switch off.** Restart the stack with `ORB_MULTI_SITE` unset. Repeat the facility 1 nurse, facility 2 nurse and facility 2 administrator passes: the isolation must be exactly the same. The create check answers that the switch is off, and the grant script refuses.

**12. Tear down.** Stop the stack, shred `drill-accounts.txt`, delete the copy and its backups.

**Pass**: every screenshot clean, the cross-facility link refused and audited, live updates kept to each facility, the soak within its SLOs, and the same isolation with the switch off. Then add the second facility to the hospital's appliance on the Facilities screen, in a window, after a verified backup.

## What the automated rehearsal covers

`server/tests/test_two_facility_e2e.py` runs the whole drill in a database of its own, with the fence opened inside the test process (as if every sweep were done):

- with the fence as the code ships it, the create gate refuses with one reason left, `screens_not_ready`, and the seed writes nothing;
- with every group cleared, the seed adds the facility through the product, and every account and patient lands at its facility;
- the browser pass as an API pass: every GET route the application serves, called as the facility 1 nurse, the facility 2 nurse, administrator, pharmacist and cashier, and the group-wide administrator, once with the account's own facility's ids and once with the other's, must never name the other facility's patients; the group-wide administrator must see both;
- after a boot with the switch off: the health probe still says two facilities, every account keeps its facility, the fence still holds blind screens shut, charts across the boundary are not found, growth is refused, and the API pass is still clean;
- the go-live criterion itself (`FACILITY_BLIND_SURFACES` empty and the gate open as the code ships) is a strict expected failure: it fails the day the fence is emptied, so the mark comes off and the drill runs with the real fence.

What only the browser pass covers: what the screens actually render (labels, tooltips, prints), live socket updates per facility, and the soak.

## What the rehearsal on a real copy found (20 October 2026)

The seed and an API pass per role were rehearsed on a copy of the dev database (30 patients, 20 accounts, 100,303 audit rows), with the fence opened in that one process:

- The seed made the facility, its department, five accounts and two patients in 1.9 s, and its read-back found every account and patient at the new facility.
- `GET /api/sage/insights` showed facility 1's patients to facility 2's administrator. `sage_proactive_service.get_active_insights` reads every facility's insights (owner multisite-10, left open in wave 12). The fence holds `/api/sage` shut today, so nothing leaks in the product, but the group must not be cleared until this is fixed. The automated rehearsal now plants an insight on each side and records this as an expected failure.
- `services/department_master.department_references` fails on a database that holds the knowledge corpus's vector tables ("no such module: vec0"), because ordinary connections do not load that extension. Moving a department between facilities would fail there. The seed script uses its own reader that passes over virtual tables.
- The operations page's "departments not in the list" counts patient portal logins' wards (see "Facility 1's departments list").

## Still open before go-live

- The fence regeneration: moving each ready group from blind to cleared in `server/tests/facility_sql_sites/` and regenerating the two tuples in `server/facility_fence.py`, reviewed as its own step. Groups the builders reported ready: `/api/billing`, `/api/payments`, `/api/payer`, `/api/audit`, `/api/flight-recorder`, `/api/abdm`, `/api/dietary`, `/api/biomedical`, `/api/procurement`, `/api/workforce`, and `/api/pharmacy-store` (one NABH lane aside). The rest still reach statements other sweeps own (contracts.md, waves 12 and 13 deferred).
- The Sage insight list per facility (above).
- A writer for visiting access rows (multisite-19 follow-up).
- Satellite ward packs in `scripts/backup.sh`.
- A two-facility load mix in `scripts/soak_test.py`.
- Every safety default the multi-site waves built strictest is listed for Samuel to confirm in docs/his_design/build/contracts.md (waves 10 to 13, "safety defaults to confirm"), and rulings MS-R2, MS-R3 (held with R009), MS-R8 (per-facility escalation ladders) and MS-R13 are recorded there with the option built.
