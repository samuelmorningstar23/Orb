# Patient summary snapshots

Orb can prepare a local model draft for each **actual active admission** and
serve the saved result without making a chart reader wait for generation.
The initial feature is opt-in. It is an engineering implementation, not a
clinically validated summarizer or permission to use an unreviewed draft for
treatment. The original chart remains the source of truth.

## Enable and qualify

Configure before starting the API process:

```sh
ORB_SUMMARY_ENABLED=1
ORB_SUMMARY_REFRESH_SECONDS=1800
ORB_SUMMARY_WORKERS=1
ORB_SUMMARY_MAX_PENDING=256
ORB_SUMMARY_SCAN_SECONDS=5
ORB_SUMMARY_GENERATION_TIMEOUT_SECONDS=120
ORB_SUMMARY_MODEL=medgemma:27b
ORB_SUMMARY_MODEL_REVISION=<immutable-local-model-artifact-release-or-digest>
ORB_SUMMARY_MAX_SOURCE_CHARS=12000
ORB_SUMMARY_CONTEXT_TOKENS=16384
```

The model must already exist locally; this feature does not download or train
a model. Both `ORB_MODELS_ENABLED` and background-model policy still apply.
When generation is paused, a missing draft reports `generation_paused` and
does not accumulate a queue of unserviceable model requests. A previously
saved draft can still be read within its freshness and source-validity bounds.

Use a pinned serving model name and set `ORB_SUMMARY_MODEL_REVISION` when
replacing weights behind the same alias. The cache identity includes that
revision, the prompt version, facility, patient, current admission and source
revision. A serving alias alone does not detect files replaced behind it.

The service has a bounded durable work ledger and fixed background worker
count. A database lease permits one scheduler/worker group across API workers;
the lease and a per-job claim token fence late results from an old owner. The
deployment's separately qualified local inference pool must have capacity for
the configured summary model in its **background** lane. Increasing
`ORB_SUMMARY_WORKERS` does not create GPU capacity.

Periodic summaries use required-work priority in a qualified pool, rather
than the optional ambient governor, and do not yield away their assigned slot
merely because a clinician is active. Reserve a **dedicated background worker**
to prevent continuous foreground arrivals starving the 30-minute work. A
shared worker's interactive reservation alone does not guarantee background
throughput. The default single-daemon path retains ambient preemption and
cannot guarantee periodic coverage during sustained interactive load.

## Reading and refreshing

`GET /api/patients/{patient_id}/summary` performs authorised database reads and,
if a missing or invalidated draft needs work, coalesces a durable refresh job.
It never waits on an LLM. `POST` to the same path followed by `/refresh`
requests a bounded refresh and returns HTTP 202 with job metadata. Neither
route signs a note, updates the chart or grants wider patient access.

Both require `patient.read_all` **and** `sage.query`, use the established
department/facility patient-access guard and PHI audit, and return
`Cache-Control: no-store`. The service rechecks patient access inside the read
transaction. An unauthorised or unknown patient has the same 404 result.
The existing multi-facility route fence continues to apply where that
deployment has not cleared the surrounding patient route group.

The JSON result distinguishes:

| Status | Meaning |
| --- | --- |
| `ready` | A review-required draft matches the checked source revisions and is within the source-age limit. It is still an **as-of draft**, not a signed clinical record. |
| `outdated` / `new_observations` | Routine observations arrived after capture. The retained draft is explicitly marked out of date and carries an as-of time; consult live observations. It does not claim to describe the current patient state. |
| `unavailable` | No eligible draft is returned. Reasons include a missing/expired draft, a changed clinical record, an inactive admission, paused generation or a disabled feature. Job metadata distinguishes queued, running, failed and deferred work. |

`generated_at` records completion. **Freshness is measured from
`source_captured_at`, not completion**: a two-minute generation does not buy
two extra minutes of apparent source freshness. `source_checked_at`, current
and captured revision numbers, `source_age_seconds` and
`refresh_after_seconds` allow a reader to show the age and expire a displayed
draft between polls. No summary prose is returned after the source-age limit.

## What invalidates a draft

Migration 89 installs database triggers, so a clinical mutation and its
revision update commit together, including writes outside one HTTP process.

* Changes to admissions, demographics/history, medications/administrations,
  allergies, problems, orders, released lab records, notes, reviewed imaging
  and care bundles hard-invalidate the draft. Corrections and deletions to
  observations also hard-invalidate it. A draft generated while those sources
  change is discarded, never published against the newer record.
* Routine **appends** to vitals and noncritical risk-score streams update a
  separate observational revision. They make an existing draft `outdated`,
  without forcing one generation for every monitor sample. The worker may
  publish its explicitly as-of draft if the admission and hard revision are
  still the same.
* An appended risk score already classified `critical` by Orb hard-invalidates.
  The summarizer introduces no new clinical threshold. During repeated
  critical changes, a current draft may remain unavailable; live escalation
  and the original chart must remain usable independently.
* Discharge/readmission or transfer to another facility cannot reuse a draft
  keyed to the previous admission. The scheduler removes cached prose from
  retired admissions, expired captures and superseded model/prompt identities.
  Its cache cleanup does not delete clinical or inference audit records.

## Scope and limitations

This first draft covers the current admission and current longitudinal
history. It includes only released lab results, signed Scribe notes and
human-reviewed imaging text. It excludes contact details, media paths and raw
transcripts from the model payload. It deliberately bounds high-volume
streams: the latest 24 observations, 12 risk scores and 20 entries from each
of nursing notes, signed Scribe notes and clinical documents. Ordering uses
actual instants, including legacy timestamps and explicit offsets.

This is **not the complete patient history**. Important older findings may
fall outside the selected window. The model and API receive section-level
matching/included/omitted counts, oldest/newest included times, unavailable
sections and source identifiers. The patient card must preserve this warning.
All matching active allergies and problems and current medication records
are retained; they are not silently trimmed to make a model call fit.

If another source has over 500 matching rows, the overall source payload is
too large or its conservative byte/context preflight fails, the job reports
`source_context_too_large`. It does not silently drop the beginning of the
record. A deployment with larger charts needs a separately evaluated
hierarchical/retrieval summary strategy or a larger qualified context budget.
Strict JSON validation and engine completion checks reject malformed,
incomplete and output-budget-truncated generations. These checks do not prove
the model's prose is factually correct.

## Scheduling and capacity

The first census is spread across a refresh window, reserving generation
time at its end. Opening a chart accelerates only that missing patient's
job. Repeated requests for the same admission coalesce; continuous routine
observations do not create a generation storm. Refresh starts ahead of the
source-age expiry when capacity is available.

For 100 occupied beds and a 30-minute schedule, the baseline is **200 summary
generations per hour**, before source-change refreshes, failures/retries and
interactive use. One worker must therefore sustain an average below 18
seconds per summary, with headroom, to meet that baseline alone. A worker
averaging 60 seconds cannot meet it; increasing the queue size does not help.
The configured governor, background yield policy and shared workload can also
prevent the schedule from meeting its target. Expired drafts become
unavailable rather than being described as fresh. A 30-minute target is not
a completion guarantee on unqualified hardware.

Qualify with actual source sizes, the intended local model/context, the
200-request mixed workload and background inference enabled. Synthetic
generator tests establish cache, race, admission and scheduling behaviour;
they establish neither model throughput nor clinical accuracy.

Focused tests: `server/tests/test_patient_summary_snapshots.py`. The service,
route and schema are `server/services/patient_summary_service.py`,
`server/routes/patient_summary_routes.py` and
`server/migrations_his/m0089_patient_summary_snapshots.py`.
