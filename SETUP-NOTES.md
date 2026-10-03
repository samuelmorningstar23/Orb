# Orb: setup notes (2026-07-28)

Everything below was run end-to-end on a Linux container before being handed to you,
so the sequence in `setup-mac.sh` is a tested path rather than a transcription of the
README. The README's model instructions are correct as written (see **Models** below);
one real bug bites on a cold database, and it's already fixed in the script.

## Run it

```bash
cd ~/Documents/Projects/Orb
./setup-mac.sh          # ~21 GB of model pulls; add --skip-models to defer
./run-orb.sh            # then open http://localhost:8000
```

Sign in with any seeded account, the password is literally `password`:

| ID | Name | Role |
|---|---|---|
| `DOC001` | Dr. Meera Sharma | doctor |
| `NUR001` | Priya Reddy | nurse |
| `PHA001` | Deepa Joshi | pharmacist, needed for Helix/pharmacy screens |
| `ADM001` | Rajesh Kumar | admin, Flight Recorder, governance |

`./run-orb.sh --dev` gives you Vite hot reload on :5173 instead of the built SPA on :8000.

## Models: the README was right

An earlier draft of this file claimed `medgemma:27b` didn't exist in the Ollama library and
that the 27B was text-only. **Both claims were wrong.** Corrected, with the evidence:

- `medgemma` **is** in the official Ollama library, with 9 tags. `medgemma:27b` (17 GB,
  Q4_K_M) and `medgemma:4b` (3.3 GB) are both labelled **Text + Image input**, 128K context.
  So `ollama pull medgemma:27b`, exactly what the README says, and what `config.py`
  already defaults to, is correct.
- Google publishes **three** MedGemma variants: 4B multimodal, 27B **multimodal**
  (`google/medgemma-27b-it`), and 27B **text-only** (`google/medgemma-27b-text-it`). The
  27B multimodal is trained on radiology, histopathology, ophthalmology and dermatology
  images alongside text. The text-only one is a *different* checkpoint, tuned for
  inference-time reasoning on text benchmarks.
- The trap: the community build `alibayram/medgemma:27b` packages the **text-only**
  checkpoint, its own model card says "trained exclusively on medical text", and Ollama's
  listing marks `alibayram/medgemma` as non-vision. Point Orb at it and Lens breaks
  silently: the endpoint accepts an image upload and returns confident prose about nothing.

So `.env` uses the plain library tags, and `setup-mac.sh` now runs `ollama show` after
pulling, warning if either model fails to report a `vision` capability.

A methodological note, since it nearly caused a second wrong call: checking the registry
manifest for an `application/vnd.ollama.image.projector` layer is **not** a valid vision
test. Current Ollama embeds the projector inside the model blob, `gemma3:4b`, which is
unambiguously vision-capable, has no projector layer either. `ollama show` is the check
that actually means something.

## MedGemma generations: which one Orb runs (checked 2026-09-07)

Short answer: **MedGemma 1 everywhere that actually serves a clinician.** The only 1.5 in
the tree was a name in the MLX code path whose weights were never downloaded here.

Two facts settle most of this.

**MedGemma 1.5 only exists at 4B.** Google's 1.5 model card opens by saying it "is only
available as a 4B multimodal instruction-tuned variant" and points anything else at the
MedGemma 1 card. There is no 1.5 27B from Google, and none from the community either
(checked every `medgemma` repo on Hugging Face, all 1.5 entries are 4B). So the 27B tier
runs MedGemma 1 because MedGemma 1 is the only 27B that exists, not because Orb is behind.
The deck research on 5 September reached the same conclusion, see
`docs/fundraising/RESEARCH_2026-09-05.md`.

**Ollama's official library has no 1.5 tags.** `ollama.com/library/medgemma` carries 9
tags and every one is v1. `ollama show` on the pulled `medgemma:27b` and `medgemma:4b`
returns the Health AI Developer Foundations licence dated 15 November 2024, which is the
v1 text. Release history on the 4B: initial release 20 May 2025, multimodal bug fix
9 July 2025, updated to 1.5 on 13 January 2026.

Where each lane sat before this check:

| Lane | Model | Generation |
|---|---|---|
| Sage, Lens, Bridge, Scribe drafting (`ORB_LLM_MODEL`, `ORB_VISION_MODEL`) | `medgemma:27b` | 1 |
| Ambient, fast tier, Scribe correction and verify (`ORB_AMBIENT_LLM_MODEL`, `ORB_FAST_MODEL`) | `medgemma:4b` | 1 |
| Voice agent, and dev-profile Scribe correction/verify defaults | `models/medgemma-1.5-4b-it` via MLX | 1.5 in name only |

The third row is the one worth knowing about. `config.py` defaults the dev-profile Scribe
correction and verify stages to `medgemma-1.5-4b-it` over MLX, and `voice_agent_service.py`
points at the same directory. That directory does not exist on this machine, and `.env`
routes both stages through Ollama instead, so nothing has ever run on those weights here.
The tree reads as though Orb is partly on 1.5. It is not.


### Tried 1.5 on the 4B tier, and it does not fit (measured 2026-09-07)

MedGemma 1.5 4B is now pulled on this box as `medgemma:1.5-4b`, built from
`hf.co/unsloth/medgemma-1.5-4b-it-GGUF:Q4_K_M` because the official Ollama library has no
1.5. It passes the vision check that `alibayram/medgemma` failed: `ollama show` reports
gemma3, 3.88B plus a 419M CLIP projector, Q4_K_M, 128K context, capabilities `completion`
and `vision`. So it is a genuine multimodal build, not a text-only repackage.

It still should not take over the 4B lanes, for one reason that is not in any benchmark
table: **MedGemma 1.5 is a reasoning model. It opens with a `thought` block and it ignores
instructions telling it not to.**

What that does to Orb, measured rather than assumed:

| Check | `medgemma:4b` (v1) | `medgemma:1.5-4b` |
|---|---|---|
| MedQA, letter-only, n=1273 | 53.73%, CI [51.0, 56.5], 2 unparsed | **not measurable**, 1238/1260 unparsed |
| Sage fast-lane response, median | 439 chars | **1640 chars** |
| Opens with a `thought` block | no | **9 of 9 runs** |
| Fast-answer marker present | 0 of 9 | 3 of 9 |

The MedQA row is the clearest symptom. The harness gives letter-only mode 4 tokens and a
system prompt that says "Respond with ONLY the single capital letter, output nothing else".
v1 obeys and returns 3 tokens. 1.5 starts reasoning anyway, gets truncated mid-thought, and
98% of its answers fail to parse. On the same vignettes with a 1024-token budget, 1.5 spent
373 to 957 tokens and 4.7 to 16.4 seconds where v1 spent 3 tokens and under 2 seconds.

The v1 MedQA number reproduces the recorded 55.38% baseline in
`docs/benchmarks/medqa_medgemma-4b-it-q4_letteronly.json` within its confidence interval.
The small gap is backend, that run used llama.cpp with a GBNF grammar, this one used the
Ollama path the app actually calls.

Worth knowing about the marker row: **the current 4B fails that contract too, 0 of 9.** The
fail-closed gate in `MarkerAnswerStreamer` therefore already holds the whole fast answer
back and releases it in one lump. 1.5 does not create that problem, but it makes the lump
3.7x bigger, so it is strictly worse on the one axis the fast tier exists for. The 27B
passes 9 of 9 with the marker at character 0. Re-run with
`dev_tools/check_fast_model_contract.py`.

**That lump was fixed the same day.** The 4B's 0 of 9 is a property of the model and it
still is, but the gate no longer depends on it: Orb emits the marker itself for any model
not measured to emit one (`ORB_SAGE_MARKER_SOURCE`, default `auto`), and the deterministic
mutation gate, which never depended on the model, still decides the turn. Root cause,
before and after numbers, and the prompt experiments that were tried first are in
[`docs/perf/FAST_LANE_MARKER_2026-09-07.md`](docs/perf/FAST_LANE_MARKER_2026-09-07.md).

**The same day, the other half of it: a fast turn in the HISTORY was taking the marker
off the next careful turn.** The fast tier stores turns with no marker, Sage replays the
previous assistant turn verbatim, and this model copies its own last output, so toggling
thinking back ON handed the careful model a markerless example of its own and that turn
stopped streaming too. Measured 10/10 with no history and 5/10 with a stored fast turn in
front of it; 10/10 after. Orb now synthesizes the marker it had already concluded belonged
there, gated on the turn's recorded action state (`ORB_SAGE_HISTORY_MARKER`, default `auto`,
`off` to roll back). A chart-mutation turn is replayed as the safe message the clinician
actually read rather than the narration the model drafted, under `ACT|`, which measured
better than `NOACT|` on both a benign and a mutation follow-up
(`ORB_SAGE_HISTORY_MUTATION_MARKER`). Section 7a of the same write-up; re-run with
`dev_tools/check_history_marker_replay.py --suite replay|mutation`.

It does not rehabilitate 1.5, and it sharpens one part of the case against it. With the
gate held shut, 1.5's opening `thought` block was withheld along with everything else.
With the gate open, whatever the model writes first is what the clinician reads first, and
for 1.5 that is 9 times out of 9 its reasoning. Wiring a reasoning model into the fast tier
now needs a thought-block cut in `MarkerAnswerStreamer` before anything else.

**Verdict.** Leave `ORB_FAST_MODEL`, `ORB_AMBIENT_LLM_MODEL` and the Scribe cheap stages on
`medgemma:4b`. Nothing in `.env` was changed. Where 1.5 is genuinely worth a new lane is
batch work where a 5 to 15 second think is free and Google's own numbers put it *ahead of
the 27B*: lab report PDF to structured JSON (91 vs 76 macro F1), EHR question answering
(89.6 vs 90.5, essentially matching a model seven times its size), and the capabilities v1
does not have at all, 3D CT and MRI, whole-slide histopathology, longitudinal chest X-ray
comparison, and anatomical bounding boxes (38.0 vs 16.0 IoU).

Note which lane that is and is not. Bridge is patient-facing chat, not extraction, and the
27B already beats 1.5 on the benchmark that matters there (EHRQA 90.5 vs 89.6), so Bridge
is not a candidate. The extraction win needs a lab-report ingestion path that Orb does not
have yet. Lens is the only existing lane 1.5 could plausibly take over, and the argument
there is mostly about freeing the pinned 27B slot, not about accuracy.

## Correction applied

**Seeding a cold database silently skips the entire pharmacy catalog.**
`scripts/reset_db_and_seed.py` calls `seed_helix()`, but the Helix tables are created by
`services.helix_db.init_helix_db()`, which only runs at *app startup*. On a fresh clone
the seed prints `Error seeding Helix database: no such table: helix_inventory_lots`,
keeps going, and reports "Seeding Complete!", you end up with 30 patients and an empty
pharmacy. `setup-mac.sh` calls `init_helix_db()` before seeding, then re-runs
`seed_helix.py`. Confirmed fix: 14 drugs + 14 inventory lots.

Also worth knowing: `install.sh` (the repo's own installer) defaults to the **prod**
profile and never seeds. `setup-mac.sh` replaces it for a dev/demo bring-up.

## What was verified

| Step | Result |
|---|---|
| `npm ci` + `npm run build` (includes `tsc -b` typecheck) | passes, 72 precache entries |
| `pip install -r requirements.txt` (minus MLX whisper) | clean |
| `server/migrate.py` | schema v0 → v5 |
| `reset_db_and_seed.py` | 13 users, 30 patients, 37 channels, 99 meds, 720 vitals, 90 labs, 60 notes, 5 surgeries; audit hash chain re-sealed |
| Backend boot | healthy; 176 API routes |
| `POST /api/auth/login` | returns JWT (field is `employee_id`, not `user_id`) |
| `GET /api/vigil/patients` | returns seeded ward with live risk scores |
| SPA served from `dist/` at `:8000` | HTTP 200 |
| RBAC | doctor token correctly denied `helix.read`, that's the permission model working, not a bug |
| `pytest` (backend) | **745 passed, 6 failed** |
| `npm run test` (vitest) | 19 passed, 4 files |

The 6 pytest failures are all in `test_reranker.py` and `test_retrieval_eval.py`, they
assert on semantic-search results, which need the BGE-M3 knowledge base this container
couldn't download (see below). They should pass on your Mac once the first boot has built
the index. Worth re-running to confirm: `cd server && .venv/bin/python -m pytest -q`.

## Two things only your Mac can do

**Ollama.** macOS has no headless installer. If `ollama` isn't on your PATH, grab the app
from <https://ollama.com/download> (or `brew install ollama`), launch it once, then re-run
`./setup-mac.sh`. Orb boots fine without it, Sage, Lens, Bridge and Scribe drafting just
return errors until it's up.

**First boot downloads BGE-M3 (~2.2 GB).** Sage's RAG index embeds the clinical corpus with
`BAAI/bge-m3` from Hugging Face on first startup. This container's proxy blocks
huggingface.co, so it's the one step that couldn't be validated here, it'll work on your
machine. Expect the first `./run-orb.sh` to sit at
`! Initializing clinical knowledge base (chunking & embedding...)` for a few minutes.
If it fails, the app still runs; Sage retrieval just returns nothing. Set
`ORB_SKIP_KNOWLEDGE_BASE=1` in `.env` to skip it deliberately.

## Deliberately not installed

`lightning-whisper-mlx`, Scribe's local ASR. It pins `tiktoken==0.3.3`, which has no
Python 3.12 wheel and only builds with a Rust toolchain. Excluded so the install doesn't
hang on a source build; `ORB_WHISPER_DAEMON_ENABLED=false` in `.env` matches. Every other
module works without it. To add it:

```bash
curl https://sh.rustup.rs -sSf | sh -s -- -y
source "$HOME/.cargo/env"
server/.venv/bin/pip install lightning-whisper-mlx==0.0.10
# then flip ORB_WHISPER_DAEMON_ENABLED=true in .env
```

The repo also references `google/medgemma-1.5-4b-it` MLX weights at
`server/models/medgemma-1.5-4b-it` for the voice agent and (in dev profile) Scribe's
correction/verify stages. That directory ships empty and the HF repo is gated. `.env`
routes those stages through Ollama instead. To use the real MLX weights, accept the
license on Hugging Face, then `HF_TOKEN=... server/.venv/bin/python server/scripts/download_medgemma_4b.py`.

## Note on the profile

`.env` is set to `ORB_DEPLOYMENT_PROFILE=dev` with `ORB_DEMO_MODE=true` and
`ORB_INGESTION_MODE=simulation`, synthetic vitals bots run so the wards look alive.
That combination is for a demo box only. Before this ever sees real patient data, flip to
`prod`, set `ORB_INGESTION_MODE=ingestion`, and read the compliance section of the README, 
the project is candid that backup/DR, retention and DPIA are still not built.

## Encryption at rest (added 2026-07-30)

`orb.db` is now opened through **SQLCipher** whenever `ORB_DB_ENCRYPTION_KEY` is set, 
which `setup-mac.sh` generates into `.env`, so a database seeded by the script is encrypted
from birth. The driver (`sqlcipher3-wheels==0.5.7`, imported as `sqlcipher3`) is pinned in
`server/requirements.txt`, so step 2 of the setup already installs it. Validated 2026-07-30
on this hardware (Apple Silicon, Python 3.12) and recorded in `server/database.py`'s own
note: the codec coexists with `sqlite-vec` vector search, FTS5, and WAL mode, the
long-standing worry that SQLCipher would break the RAG path did not materialise on this
build.

Two things worth knowing:

- **A plaintext `orb.db` from an earlier run cannot be opened once the key exists.** SQLCipher
  reads a plaintext file as garbage and everything fails with `file is not a database`. Migrate
  it once, with the server stopped:
  ```bash
  cd server
  .venv/bin/python scripts/encrypt_db.py --check     # report state only
  .venv/bin/python scripts/encrypt_db.py --migrate
  ```
  The migration verifies row counts, audit hash-chain linkage, vector search and FTS5 on the
  encrypted copy before swapping it in, and **keeps** the plaintext original as
  `server/orb.db.plaintext-<timestamp>`. That file is unencrypted PHI, delete it once you are
  happy (`rm server/orb.db.plaintext-*`).
- **Anything that opens the DB must use `database.py`'s codec-aware helpers**
  (`_open_raw_connection`, `_open_raw_connection_at`, `get_db`/`db_session`). The stdlib
  `sqlite3` module and the `sqlite3` CLI both fail on the encrypted file, which also means
  the repo-root `scripts/backup.sh` (it shells out to `sqlite3 .backup`) no longer works, 
  use `server/scripts/backup_db.py`, which goes through those helpers and writes an
  already-encrypted backup.

**Be honest about what this buys.** The key sits in `.env` on the same disk as the database,
so encryption protects *copies*, backups, exports, a disk read on another machine, not this
laptop while it is running, and not a stolen one whose `.env` is equally readable. Turn on
FileVault too; keeping the key in the macOS keychain instead of a dotfile would be the real
improvement. And because backups are encrypted with that same key, losing it loses the
backups: copy `ORB_DB_ENCRYPTION_KEY` somewhere off the machine before relying on them.
