# Orb operations: what the status page means and what to do

This is for the person looking after the Orb appliance on site. The status page
(Admin, and `./scripts/orb.sh status` in a terminal) checks eight things and
ends every problem with a command. This page explains each check and each
command. Nothing here needs a vendor on the phone.

Run every command from the Orb folder (the one with `run-orb.sh` in it). The
commands read the settings file `.env` in that folder, the same way Orb does.

## The commands

| Command | What it does |
| --- | --- |
| `./scripts/orb.sh status` | The status page in the terminal. Add `--deep` for the slower checks, `--json` for the raw data. Exit code 0 is working, 1 needs attention, 2 urgent. |
| `./scripts/orb.sh backup` | Takes a backup now, checks it, and writes a receipt beside it (`<name>.verified.json`). Add `--offsite DIR` to copy it off the machine as well. |
| `./scripts/orb.sh install-backup-schedule` | Runs the backup every night (macOS launchd, as the signed-in operator). |
| `./scripts/orb.sh restore --list` | Lists the backups on this machine, newest first, with whether each one was checked. |
| `./scripts/orb.sh restore` | Prints the restore steps. It does not restore anything by itself. |
| `./scripts/orb.sh encrypt-database` | Encrypts the patient database at rest. Orb must be stopped. Add `--check` to only report. |
| `./scripts/orb.sh restart` | Stops Orb and starts it again in the same terminal, so you can watch it start. |
| `./scripts/orb.sh accounts` | Lists staff accounts, administrators without a second sign-in step, and accounts still using a password Orb ships with. Slow on purpose. |
| `./scripts/orb.sh create-admin` | Creates an administrator. Only at this machine's keyboard, never over the network. |

## The checks

### Orb server

Whether the Orb program is running, and whether it is in hospital mode. If it
says developer mode, the page lists what it can see (the profile, a development
signing secret, a running vitals simulator and so on): fix each line in `.env`,
then `./scripts/orb.sh restart`. If the page itself does not load, use
`./scripts/orb.sh status` in a terminal on the appliance.

### Patient database

Whether the database opens, whether its structure check passes (with `--deep`),
and whether it is encrypted on disk.

- **Structure check failed.** Take a backup of the current state first
  (`./scripts/orb.sh backup`), then restore the newest verified backup
  (`./scripts/orb.sh restore --list`, then `./scripts/orb.sh restore` for the
  steps).
- **Not encrypted.** Stop Orb and run `./scripts/orb.sh encrypt-database` before
  real patients are entered. Keep a copy of `ORB_DB_ENCRYPTION_KEY` somewhere
  that does not depend on this machine: without it every backup is unreadable.

### Backups

A backup counts as verified only when the check that runs after it found real
records in it and left a receipt beside it. `./scripts/orb.sh backup` and the
nightly schedule write that receipt, whether the check passed or failed.

- **No backup at all.** `./scripts/orb.sh backup`, then
  `./scripts/orb.sh install-backup-schedule`.
- **Failed its check.** The newest backup is unusable. Run
  `./scripts/orb.sh backup` by hand and read what it prints. Until a backup
  passes, there is nothing to fall back to.
- **Unchecked.** The newest backup has no receipt. Backups taken from the Admin
  screen (Data & Backups) do not write one yet. Run `./scripts/orb.sh backup`.
- **Out of date.** The nightly backup has probably stopped. Take one now and
  re-run `./scripts/orb.sh install-backup-schedule`.

Only verified backups are rotated out. A failed or unchecked backup file is
never deleted automatically; remove it by hand once a newer verified backup
exists. `./scripts/orb.sh restore --list` shows which is which.

### Medical AI

Whether the AI engine (Ollama) is answering and the models Orb uses are loaded.
If the engine is not answering, start it with `ollama serve` and check again
with `./scripts/orb.sh status`. If it answers but nothing is loaded, nothing is
broken: the first question to Sage takes about a minute while the model loads.
Admin, Model Governance lists the models this site runs.

### Bedside data feeds

Whether the ward monitor feeds are connected and delivering readings. Orb works
without them (nurses type observations in). If feeds are configured but none is
running, tell the ward to chart by hand, then `./scripts/orb.sh restart`. The
full error for a feed is in Admin, Ingestion.

### Storage space

Free space on the disks holding the database and the backups. When it is low,
remove old backups that have already been copied off the machine. Never delete
anything in the uploads folder: those are patient scans and recordings.

### Site settings

Whether this hospital's settings file (`server/site_config/site.toml`) is
loaded, and whether patients are admitted under department names it lists. A
department name that does not match exactly hides those patients from staff
recorded under the listed spelling.

### Sign-in and administrators

Whether an administrator can sign in, whether administrators use a second
sign-in step, and whether any account still uses a password Orb ships with.
List problem accounts with `./scripts/orb.sh accounts` and reset them from
Admin, Users. If there is no administrator at all, run
`./scripts/orb.sh create-admin` at the appliance's keyboard.
