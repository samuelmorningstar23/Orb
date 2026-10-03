# Key Ceremony: Orb Encryption Keys

*Version 1 · 2026-08-01 · A procedure to be performed in person, with witnesses,
and signed. Not a guide to be read alone.*

---

## 0. Read this out loud before you begin

> Orb's database is encrypted. The key that unlocks it is not stored inside the
> database, is not stored inside any backup, and is not held by any vendor,
> cloud service or support desk. There is no override, no master key, and no way
> to brute-force it. **If every copy of this key is lost, every patient record
> on this appliance and in every backup of it becomes permanently unreadable
> ciphertext.** Not delayed. Not expensive to recover. Gone, while you are
> looking at fourteen files that appear to contain it.
>
> That is why this is a ceremony and not a checklist. Two people are about to
> take custody of the only thing standing between this hospital and the loss of
> its clinical record.

Everybody in the room should have heard that before the first envelope is
sealed. It is the reason the procedure below is as heavy as it is.

---

## 1. What is being escrowed

**Two keys, not one.** Escrowing one and losing the other still loses patient
data.

| Key | Protects | If lost |
|---|---|---|
| `ORB_DB_ENCRYPTION_KEY` | `orb.db` and every backup of it, patients, vitals, admissions, orders, medications, notes, documents, messages, users, the audit log, the search indexes | The entire clinical record is unreadable |
| `ORB_UPLOAD_ENCRYPTION_KEY` | Uploaded media, Lens images and Scribe audio, encrypted individually with AES-256-GCM | The record restores with holes in it: notes referring to an X-ray nobody can open |

Both live in one file, `/opt/orb/.env`, on the appliance, the same machine the
backups exist to survive. That file is deliberately **excluded** from the backup,
because a backup containing its own key is not encrypted in any meaningful sense.

### One thing about these keys that changes how you handle them

Orb does not use the key string directly. It derives the actual 32-byte
encryption key by taking the **SHA-256 of the key string**, for both keys.

This has a consequence that is not obvious and matters here:

> **Never write a plain SHA-256 of a key onto the escrow sheet as a "checksum".
> That digest is not a checksum of the key, it *is* the key**, in exactly the
> form the database accepts. Anyone holding it can open the record.

§5 gives a transcription check that is safe. Use that one and no other.

---

## 2. When this happens

| Occasion | Which parts |
|---|---|
| **Install day, before any real patient data** | All of it. This is not a "next week" task. |
| Key rotation | All of it, plus §9 |
| Annual verification | §10 only, verify custody and the check digits. No envelope is opened. |
| A custodian leaves the hospital or the project | All of it, with new envelopes |
| An envelope is found opened, damaged, or missing | §11 immediately |

---

## 3. Who is in the room

Four roles. **Three people minimum.** The operator cannot hold every role.

| Role | Who | Does |
|---|---|---|
| **Chair** | Hospital-side, senior, CIO, IT lead, or their named delegate | Runs the ceremony, reads the statement in §0, holds the record sheet |
| **Operator** | The person who runs the appliance | Touches the keyboard. Nobody else does. |
| **Hospital custodian** | The person who will hold the safe envelope | Takes custody of Envelope A |
| **Off-site custodian** | The person who will hold the second envelope | Takes custody of Envelope B |
| **Witness** | Anyone present who is not the operator | Signs that they saw each step |

The chair may also be a custodian. The operator **may not** be the hospital
custodian, the whole point of the second copy is that it does not depend on the
operator being reachable, alive, or still involved.

**Write down who can open the hospital safe, and make sure it is more than one
living person.** A safe with one key-holder is a single point of failure wearing
a badge.

---

## 4. Materials

- The appliance, installed, with `.env` already generated.
- A printer **directly attached to the appliance by cable.**
  Not a network printer, not a shared queue, not AirPrint. A network print job
  is spooled, the document persists on the print server or on the printer's own
  disk, often for days, in cleartext, where nobody is watching it. That would put
  a plaintext copy of the encryption key somewhere outside this room, which
  defeats the entire ceremony.
- **Two tamper-evident envelopes**, the kind that cannot be re-sealed without
  visible damage. Ordinary envelopes with tape are not adequate; the whole
  auditable property of this scheme is that an envelope is either intact or
  visibly not.
- A pen with permanent ink.
- The record sheet in §12, printed twice.
- The hospital safe, open and available.
- A second machine, the standby Mac, for the verification in §6.

**Not permitted in the room:** phone cameras pointed at the screen or the sheet,
screen-sharing sessions, screen recording, and any cloud-synced note application.

---

## 5. Procedure

> **Tool-assisted option.** `server/scripts/escrow_sheet.py` automates the
> paperwork in Parts B–C: `generate` writes a print-ready sheet (both keys,
> the §5B check digits, sealing and §5D verification instructions) to a
> 0600 file, with a `--shred` flag to overwrite and delete it after
> printing, and `verify` recomputes the check digits from the live keys so
> an operator can confirm a printed sheet without any key touching the
> screen. It reads the keys from the environment or `--env-file /opt/orb/.env`,
> refuses to run unless **both** keys are present, and never prints key
> material to the terminal.
>
> ```bash
> cd /opt/orb/server
> .venv/bin/python scripts/escrow_sheet.py generate --env-file ../.env --shred
> .venv/bin/python scripts/escrow_sheet.py verify   --env-file ../.env
> ```
>
> The manual commands below remain the authority and the fallback, they are
> what you use on a recovery machine that has nothing but a shell, and they
> are the spec the tool is tested against. Everything else in this section
> (proof-reading aloud, the directly-attached printer, clearing the print
> queue, §5D drilling from the paper) still applies unchanged.

### Part A: Confirm the keys exist and encryption is real

The operator, at the appliance:

```bash
cd /opt/orb
grep -c '^ORB_DB_ENCRYPTION_KEY=' .env       # expect 1
grep -c '^ORB_UPLOAD_ENCRYPTION_KEY=' .env   # expect 1
ls -l .env                                    # expect -rw------- (mode 600)
```

Then confirm the database really is encrypted, setting a key is not the same as
the encryption being active, because if the SQLCipher driver is missing the
application logs a warning and runs on plaintext rather than failing:

```bash
cd /opt/orb/server
.venv/bin/python scripts/encrypt_db.py --check
```

**Do not proceed if the database is reported as plaintext.** There is nothing to
escrow yet, and sealing an envelope for a key that is not protecting anything
would be worse than not having one, it would produce a signed record that is
false.

If `.env` does not yet exist, generate the keys now:

```bash
ORB_DB_ENCRYPTION_KEY=$(openssl rand -hex 32)
ORB_UPLOAD_ENCRYPTION_KEY=$(openssl rand -hex 32)
```

64 hexadecimal characters each. Long enough that guessing is not a threat; short
enough that a human can retype it once under pressure.

### Part B: Produce the transcription check

The point of this step is that recovery day involves a human retyping 64
characters from paper, at speed, probably at night. A single wrong character
gives a key that is wrong in a way that looks exactly like a corrupt backup. The
check below turns that into an immediate, obvious failure instead of an hour of
misdirected panic.

```bash
cd /opt/orb
set -a && . ./.env && set +a

# DB key check digit: 8 hex characters
printf 'orb-escrow-check-v1:%s' "$ORB_DB_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8

# Uploads key check digit: 8 hex characters
printf 'orb-escrow-check-v1:%s' "$ORB_UPLOAD_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8
```

Two details, both deliberate:

- **`printf`, not `echo`.** `echo` appends a newline, which changes the digest.
  Recovery day would then fail the check on a perfectly correct key.
- **The `orb-escrow-check-v1:` prefix, and only the first 8 characters.** Without
  the prefix this would be a plain SHA-256 of the key, which, as §1 explains, is
  the actual encryption key. The prefix guarantees the printed value can never be
  the key. Eight characters is plenty to catch a typing error and useless to an
  attacker.

Write both check digits on the record sheet.

### Part C: Print

```bash
cd /opt/orb
grep -E '^ORB_(DB|UPLOAD)_ENCRYPTION_KEY=' .env
```

Print **two identical copies**, one per envelope, on the directly-attached
printer. Each sheet must carry, handwritten or printed:

```
ORB ENCRYPTION KEYS, <Hospital name>
Appliance          : <hostname / serial>
Sealed on          : <date>
Ceremony reference : <reference from the record sheet>

ORB_DB_ENCRYPTION_KEY     = <64 hex characters>
  check digit             = <8 hex>
ORB_UPLOAD_ENCRYPTION_KEY = <64 hex characters>
  check digit             = <8 hex>

Recovery instructions: docs/KEY_CEREMONY.md §8
These keys are the ONLY way to read this hospital's clinical record.
There is no vendor copy and no override.
```

Proof-read both sheets against the screen, character by character, out loud, with
a second person following. Zero and letter O, one and letter l. Do it now, in the
room, while the screen is still in front of you, not on recovery night.

Then, before anything else: **clear the print queue and power-cycle the printer.**
Many printers retain the last job in memory.

### Part D: Verify the escrow actually works

**This is the step people skip, and it is the step that makes the difference
between an escrow and a hopeful piece of paper.**

Everything so far proves you printed *something*. It does not prove the printed
thing opens the database. Verify by using the paper, not the file.

On the **standby machine**, with the appliance's newest backup copied to it:

```bash
# 1. Type the DB key from the PRINTED SHEET. Do not copy it from .env,
#    do not scroll up in the terminal history. The sheet is what is being
#    tested. Read -s so it is not echoed to the screen.
read -s -p "ORB_DB_ENCRYPTION_KEY from the sheet: " ORB_DB_ENCRYPTION_KEY; echo
export ORB_DB_ENCRYPTION_KEY

# 2. Confirm the check digit matches the sheet BEFORE going further.
printf 'orb-escrow-check-v1:%s' "$ORB_DB_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8

# 3. Prove the key opens a real backup.
./scripts/restore-drill.sh --backup /path/to/orb-backup-YYYYMMDD-HHMMSS.db
```

Expected:

```
[drill]   decrypt+open ...... PASS
[drill]   integrity_check ... PASS
[drill]   rows:patients ..... PASS, <n> rows
[drill]   audit chain ....... PASS, <n> rows, linkage intact

[drill] RESULT: PASS
```

A `RESULT: PASS` reached from a key typed off the paper is the thing being
certified today. Anything else, stop, re-check the transcription, re-print.

Then clear the key from that shell:

```bash
unset ORB_DB_ENCRYPTION_KEY
history -c 2>/dev/null || true
```

An untested escrow is a guess about your own handwriting. Do not sign the record
sheet until this has passed.

### Part E: Seal

With everyone watching:

1. Fold each sheet so the key is not readable through the envelope. Hold it up to
   the light and check.
2. Place one sheet in **Envelope A** and one in **Envelope B**.
3. Seal both.
4. Every person present signs **across the seal**, on both envelopes, so any
   later opening destroys the signatures.
5. Write on the outside of each envelope, without opening it:

```
ORB ENCRYPTION KEYS, DO NOT OPEN
Envelope A of 2   (or B of 2)
Hospital     : ____________________
Appliance    : ____________________
Sealed       : ____________________  by ____________________
Ceremony ref : ____________________
Opening this envelope requires authorisation under docs/KEY_CEREMONY.md §8
and must be recorded on the ceremony record sheet.
```

6. Photograph the **outside** of each sealed envelope for the record. Never the
   inside.

### Part F: Store

| Envelope | Goes to | Held by | Condition |
|---|---|---|---|
| **A** | The hospital safe | Hospital custodian | Separate compartment from any backup media. Key and ciphertext in one container is a single point of compromise, not a backup strategy. |
| **B** | Off site, a different building | Off-site custodian (the Orb operator, for this pilot) | Somewhere that survives whatever destroys the first building |

Two copies, in two buildings, in two custody chains. That is the whole design,
and its cost is that both must be updated on rotation. **A forgotten stale copy
is worse than no copy**, it will be tried first in an emergency, and it will
fail.

### Part G: Sign

Complete both copies of the record sheet in §12. Chair keeps one; operator keeps
one. The signed record, not the envelope, is what an auditor or a court asks for.

---

## 6. Optional convenience copy: and its honest limits

A third copy in the macOS Keychain of the operator's own Mac is convenient: no
transcription errors, scriptable retrieval.

It is **not** a substitute for either envelope, for one reason: the recovery path
then depends on a second computer booting, unlocking and having a working
account, which is precisely the assumption that just failed on machine one.
Keychains are also lost on OS reinstall more often than people expect.

If you keep one, record it on the sheet, and remember it is a third thing to
update on rotation.

**Do not put these keys in a cloud-synced password manager.** It would put the
key to a hospital's clinical record outside the building, which contradicts the
promise the appliance is sold on and creates a copy nobody in this room controls.

---

## 7. What the escrow does not protect against

Stated plainly, so nobody has to discover it during due diligence.

- **It does not protect a running appliance.** The keys are in `.env` on the same
  disk as the data. Anyone with filesystem access on a live or stolen machine
  reads the key as easily as the database. Database encryption protects *copies*
  backups, exports, a cloned volume, a decommissioned SSD. Host security and
  FileVault are separate controls and both are still required.
- **A stronger posture exists and Orb does not implement it.** Sourcing the key
  from the OS keychain or a secret store at boot, so it is never at rest beside
  the data it protects, is better engineering. It is not built today.
- **Both custodians could still be unavailable at once.** Two copies in two
  buildings is a large improvement over one. It is not infinity.
- **The envelopes protect the key, not the backups.** If every backup is also
  lost, a perfectly preserved key opens nothing. Off-box backup copies are a
  separate control, see [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) §7.

---

## 8. Recovery: opening an envelope

Opening a sealed envelope is a controlled event. It happens when the appliance is
gone, `.env` is lost, or a restore has failed with `decrypt+open ... FAIL`.

**Authorisation.** The chair (or their successor in the role) authorises, in the
presence of at least one witness who is not the operator. Nobody opens an
envelope alone, and nobody opens one on a phone call.

**Procedure.**

1. Before breaking the seal, photograph the envelope and confirm the signatures
   across the seal are intact. If they are not, go to §11 first, you may be
   holding a compromised key, and that changes what you do next.
2. Break the seal. Record the time and everyone present.
3. Read the check digit line off the sheet.
4. On the recovery machine, type the key, do not photograph it, do not dictate
   it over a phone:

```bash
read -s -p "ORB_DB_ENCRYPTION_KEY: " ORB_DB_ENCRYPTION_KEY; echo
export ORB_DB_ENCRYPTION_KEY

# Confirm the transcription BEFORE using it. This is why the check digit exists.
printf 'orb-escrow-check-v1:%s' "$ORB_DB_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8
#   → must equal the check digit printed on the sheet.

# Prove the backup opens before touching anything else.
./scripts/restore-drill.sh --backup /path/to/orb-backup-YYYYMMDD-HHMMSS.db
```

5. Do the same for `ORB_UPLOAD_ENCRYPTION_KEY` if media is being restored.
6. Complete the restore per [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) §5 and
   [DRILLS.md](DRILLS.md).
7. **Re-seal.** An opened envelope is spent. Run §5 Parts C–G again with fresh
   envelopes, the same day. Record both events, the opening and the re-sealing, 
   on the record sheet.

**If the check digit does not match:** you mistyped, or the sheet is wrong.
Re-read it character by character with a second person. If it still does not
match, the other envelope is now the live copy, go and get it before doing
anything else.

**If both envelopes are gone and `.env` is gone:** the data is unrecoverable.
There is nothing further to try, and there is no support call that changes that.
Tell the hospital immediately and in writing. This sentence exists in this
document so that it is never the first time anyone has heard it.

---

## 9. Rotation

**Rotating the key strands every backup taken under the old one.** There is no
re-key tooling.

If you rotate:

1. Keep the **old** key sealed and stored until the last backup written under it
   has aged out of retention (14 backups at daily cadence ≈ two weeks). Label the
   old envelopes `SUPERSEDED, RETAIN UNTIL <date>`.
2. Run the full ceremony for the new key. New envelopes, new record sheet, new
   ceremony reference.
3. Update the convenience copy in §6, if one exists.
4. Take a fresh backup and drill it under the new key before you rely on it.
5. Destroy the superseded envelopes only after the retention date, witnessed, and
   record the destruction.

Rotate for a reason, a suspected compromise, a custodian's departure, not on a
calendar. Every rotation is a window in which two keys are live and one of them
can be forgotten.

---

## 10. Annual verification

Once a year, and after any change of custodian:

- [ ] Envelope A is where the record says, seal intact, signatures unbroken
- [ ] Envelope B is where the record says, seal intact, signatures unbroken
- [ ] The list of people who can open the hospital safe is current, and has more
      than one living person on it
- [ ] The check digit on the record sheet still matches the live key:
      `printf 'orb-escrow-check-v1:%s' "$ORB_DB_ENCRYPTION_KEY" | shasum -a 256 | cut -c1-8`
- [ ] A restore drill has passed within the last month
      ([DRILLS.md](DRILLS.md))
- [ ] Both custodians confirm, in writing, that they still hold their envelope

Sign the verification block on the record sheet. Verification does **not** require
opening an envelope, if the check digit matches the live key, the escrow is
consistent with what is running.

---

## 11. If an envelope is opened, damaged or missing

Treat it as a key compromise, not an administrative problem.

1. **Assume the key is exposed.** Anyone with it and any backup can read every
   patient record in it.
2. Tell the chair and the hospital's information-governance lead the same day.
3. Verify the other envelope is intact.
4. **Rotate the key** (§9), then re-encrypt: take a fresh backup under the new key
   and let the old backups age out of retention. Until they do, backups readable
   with the exposed key still exist.
5. Record the whole sequence on the record sheet. Under India's DPDP framework
   this may be a reportable event, that is a question for counsel, and counsel
   is engaged before go-live for exactly this class of question.

---

## 12. Ceremony record sheet

*Print two copies. Complete in permanent ink. Chair keeps one, operator keeps one.
Do not store either copy inside an envelope.*

```
════════════════════════════════════════════════════════════════════════════
  ORB HOSPITAL OS, ENCRYPTION KEY CEREMONY RECORD
════════════════════════════════════════════════════════════════════════════

  Ceremony reference   : ORB-KC-________________   (e.g. ORB-KC-2026-08-01-01)
  Hospital             : _______________________________________________
  Ward(s) in scope     : _______________________________________________
  Appliance hostname   : _______________________________________________
  Appliance serial     : _______________________________________________
  Date                 : ______________   Time started : ______________
  Location             : _______________________________________________

  Reason for ceremony  : [ ] Initial install    [ ] Rotation
                         [ ] Custodian change   [ ] Post-incident re-seal

────────────────────────────────────────────────────────────────────────────
  PEOPLE PRESENT
────────────────────────────────────────────────────────────────────────────
  Role                 Name                     Signature
  Chair                ____________________     ____________________
  Operator             ____________________     ____________________
  Hospital custodian   ____________________     ____________________
  Off-site custodian   ____________________     ____________________
  Witness              ____________________     ____________________
  Witness              ____________________     ____________________

────────────────────────────────────────────────────────────────────────────
  STEPS COMPLETED                                        Initials    Time
────────────────────────────────────────────────────────────────────────────
  §0  Loss statement read aloud to everyone present      ______     ______
  §5A Both keys confirmed present in .env (mode 600)     ______     ______
  §5A encrypt_db.py --check confirms DB is ENCRYPTED     ______     ______
  §5B Check digits generated                             ______     ______
  §5C Two sheets printed on a DIRECTLY ATTACHED printer  ______     ______
  §5C Both sheets proof-read aloud against the screen    ______     ______
  §5C Print queue cleared, printer power-cycled          ______     ______
  §5D ESCROW VERIFIED: key typed FROM THE SHEET on the
      standby machine, restore-drill.sh → RESULT: PASS   ______     ______
  §5E Envelopes sealed and signed across the seal        ______     ______
  §5F Envelope A placed in the hospital safe             ______     ______
  §5F Envelope B taken off site                          ______     ______

────────────────────────────────────────────────────────────────────────────
  CHECK DIGITS  (8 hex characters each, NOT the keys)
────────────────────────────────────────────────────────────────────────────
  ORB_DB_ENCRYPTION_KEY      check digit : ____________
  ORB_UPLOAD_ENCRYPTION_KEY  check digit : ____________

  Verification command:
    printf 'orb-escrow-check-v1:%s' "$KEY" | shasum -a 256 | cut -c1-8

────────────────────────────────────────────────────────────────────────────
  ESCROW VERIFICATION EVIDENCE
────────────────────────────────────────────────────────────────────────────
  Backup file drilled  : _______________________________________________
  Machine used         : _______________________________________________
  Drill result         : [ ] PASS    [ ] FAIL   (a FAIL stops the ceremony)
  patients rows        : __________   audit chain : [ ] intact  [ ] broken

────────────────────────────────────────────────────────────────────────────
  CUSTODY
────────────────────────────────────────────────────────────────────────────
  Envelope A location  : _______________________________________________
  Who can open it      : 1. ______________________  2. ______________________
                         (must be at least two living people)
  Envelope B location  : _______________________________________________
  Held by              : _______________________________________________

  Convenience copy (Keychain, optional) : [ ] none   [ ] on ______________

────────────────────────────────────────────────────────────────────────────
  ACKNOWLEDGEMENT, signed by the hospital
────────────────────────────────────────────────────────────────────────────
  I confirm that the statement in §0 was read aloud in my presence, and that
  I understand that if every copy of these keys is lost, the clinical record
  held on this appliance and in all of its backups is permanently
  unrecoverable. No vendor, supplier or support organisation holds a copy or
  can override the encryption.

  Name  ____________________  Role ____________________
  Signature ____________________  Date ____________________

────────────────────────────────────────────────────────────────────────────
  SUPERSEDED KEY  (rotation only)
────────────────────────────────────────────────────────────────────────────
  Previous ceremony ref     : ____________________
  Old envelopes retained at : ____________________
  Retain until (last backup under the old key ages out) : ____________
  Destroyed on ____________ witnessed by ____________________

════════════════════════════════════════════════════════════════════════════
  ANNUAL VERIFICATION LOG  (§10, no envelope is opened)
════════════════════════════════════════════════════════════════════════════
  Date        A intact   B intact   Digits match   Drill passed   Signature
  __________  [ ]        [ ]        [ ]            [ ]            __________
  __________  [ ]        [ ]        [ ]            [ ]            __________
  __________  [ ]        [ ]        [ ]            [ ]            __________

════════════════════════════════════════════════════════════════════════════
  ENVELOPE OPENING LOG  (§8, every opening, without exception)
════════════════════════════════════════════════════════════════════════════
  Date/time   Envelope   Authorised by      Reason            Re-sealed on
  __________  A / B      ________________   ________________  ____________
  __________  A / B      ________________   ________________  ____________

  Seal found intact on opening?   [ ] yes   [ ] no  → §11 applies
════════════════════════════════════════════════════════════════════════════
```

---

## Related documents

| Document | What it covers |
|---|---|
| [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md) | Backup, verification, restore, and §6 on the key-escrow options and their trade-offs |
| [DRILLS.md](DRILLS.md) | The restore drill that proves the escrowed key actually works |
| [INSTALL_RUNBOOK.md](INSTALL_RUNBOOK.md) | Install, hardening, and every failure mode |
| [DEPLOYMENT.md](DEPLOYMENT.md) §11 | Encryption at rest, what it protects and what it does not |
