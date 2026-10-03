/** Roster import — the client half of `server/routes/roster_import_routes.py`.
 *
 *  The backend has been complete and tested for a while (field catalogue,
 *  header sniffing, a dry run that writes nothing, a one-transaction commit
 *  that writes the admission alongside the patient, and an applied-run
 *  history). Nothing in `src/` called it, so an admin had no way to get a
 *  ward list into Orb except by hand. This module and
 *  pages/panels/admin/RosterImport.tsx are that surface.
 *
 *  THE CONTRACT THAT MATTERS: the three steps all post the SAME file. The
 *  commit recomputes the plan server-side from the uploaded bytes rather than
 *  trusting anything the client learned during the dry run, so a commit can
 *  never write something the dry run did not show for the same file + mapping.
 *  The UI therefore keeps the File object around across all three steps and
 *  must re-send it — it must never try to "apply the plan" it is holding.
 */
import { API_BASE, buildUploadHeaders, handleResponseError } from './client'

/** One target field an import can populate. The server's key is `name`, not
 *  `id` — see `field_catalogue()` in server/services/roster_import.py. */
export interface RosterField {
  name: string
  label: string
  required: boolean
  kind: string
  /** Why the field matters. `department` in particular explains that a wrong
   *  value hides the patient from that department's boards — worth surfacing. */
  note: string
}

export interface RosterFieldCatalogue {
  fields: RosterField[]
  required: string[]
  date_orders: string[]
  max_bytes: number
  max_rows: number
}

/** A row the sniffer thinks might be the header line. */
export interface RosterHeaderCandidate {
  line: number
  text: string
  fields: string[]
  recognised_columns: string[]
  possible_columns: string[]
}

export interface RosterIssue {
  line: number | null
  code: string
  severity: string
  message: string
  field: string | null
  value: string | null
}

/** Step 1 — reads nothing from the patient table, writes nothing anywhere. */
export interface RosterPreview {
  delimiter: string
  headers: string[]
  header_line: number
  header_confident: boolean
  header_candidates: RosterHeaderCandidate[]
  row_count: number
  rejected_row_count: number
  /** header -> orb field. The UI seeds its mapping editor from this. */
  suggested_mapping: Record<string, string>
  unmapped_headers: string[]
  missing_required: string[]
  sample_rows: Record<string, string>[]
  issues: RosterIssue[]
  filename: string
}

/** What would happen to one patient. `action` is create | update | skip. */
export interface RosterPlannedRow {
  line: number
  mrn: string | null
  name: string | null
  action: string
  reason: string | null
  patient_id: string | null
  values: Record<string, unknown>
  changes: Record<string, unknown>
  admission: Record<string, unknown> | null
  issues: RosterIssue[]
}

/** Patients Orb still holds on these wards that the file does not mention.
 *  Nothing here is acted on — absence from a roster is not a discharge. */
export interface RosterAbsent {
  wards: string[]
  patients: Record<string, unknown>[]
  count?: number
  note: string
}

/** Step 2 — writes nothing. `applied` is always false. */
export interface RosterDryRun {
  delimiter: string
  mapping: Record<string, string>
  date_order: string
  update_existing: boolean
  header_line: number
  default_department: string | null
  counts: Record<string, number>
  rows: RosterPlannedRow[]
  file_issues: RosterIssue[]
  absent_from_roster: RosterAbsent
  applied: boolean
  filename: string
}

/** Step 3 — one transaction. Re-running the same file changes nothing. */
export interface RosterCommit {
  run_id: string
  summary: Record<string, number>
  admissions: Record<string, number>
  created_patient_ids: string[]
  updated_patient_ids: string[]
  created_admission_ids: string[]
  header_line?: number
  [k: string]: unknown
}

/** A row of `roster_import_runs`. Note the timestamp column is `started_at`
 *  (not `applied_at`) and the id is `id` (not `run_id` — that name only exists
 *  on the commit response). */
export interface RosterRun {
  id: string
  actor_user_id: string
  source_filename: string
  started_at: string
  note: string | null
  rows_total: number
  rows_created: number
  rows_updated: number
  rows_unchanged: number
  rows_skipped: number
  issue_count: number
  [k: string]: unknown
}

/** The mapping travels as a JSON string in a multipart field, matching
 *  `_parse_mapping` on the server. */
function toForm(
  file: File,
  extra: Record<string, string | number | boolean | null | undefined> = {},
): FormData {
  const fd = new FormData()
  fd.append('file', file, file.name)
  for (const [k, v] of Object.entries(extra)) {
    if (v === null || v === undefined || v === '') continue
    fd.append(k, String(v))
  }
  return fd
}

async function postForm<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'include',
    // NB: no Content-Type — the browser must set the multipart boundary itself.
    headers: buildUploadHeaders(),
    body,
  })
  if (!response.ok) await handleResponseError(response)
  return response.json()
}

export async function getRosterFields(): Promise<RosterFieldCatalogue> {
  const response = await fetch(`${API_BASE}/admin/roster-import/fields`, {
    credentials: 'include',
    headers: buildUploadHeaders(),
  })
  if (!response.ok) await handleResponseError(response)
  return response.json()
}

/** Step 1. `headerLine` is only sent once the admin has answered the
 *  which-line-is-the-header question that `header_confident: false` asks. */
export async function previewRoster(file: File, headerLine?: number): Promise<RosterPreview> {
  return postForm('/admin/roster-import/preview', toForm(file, { header_line: headerLine }))
}

export async function dryRunRoster(
  file: File,
  mapping: Record<string, string>,
  opts: {
    dateOrder?: string
    updateExisting?: boolean
    headerLine?: number
    defaultDepartment?: string
  } = {},
): Promise<RosterDryRun> {
  return postForm(
    '/admin/roster-import/dry-run',
    toForm(file, {
      mapping: JSON.stringify(mapping),
      date_order: opts.dateOrder ?? 'auto',
      update_existing: opts.updateExisting ?? true,
      header_line: opts.headerLine,
      default_department: opts.defaultDepartment,
    }),
  )
}

export async function commitRoster(
  file: File,
  mapping: Record<string, string>,
  opts: {
    dateOrder?: string
    updateExisting?: boolean
    headerLine?: number
    defaultDepartment?: string
    note?: string
  } = {},
): Promise<RosterCommit> {
  return postForm(
    '/admin/roster-import/commit',
    toForm(file, {
      mapping: JSON.stringify(mapping),
      date_order: opts.dateOrder ?? 'auto',
      update_existing: opts.updateExisting ?? true,
      header_line: opts.headerLine,
      default_department: opts.defaultDepartment,
      note: opts.note,
    }),
  )
}

/** Applied-import history, newest first. Dry runs are not recorded. */
export async function listRosterRuns(limit = 50): Promise<{ runs: RosterRun[] }> {
  const response = await fetch(`${API_BASE}/admin/roster-import/runs?limit=${limit}`, {
    credentials: 'include',
    headers: buildUploadHeaders(),
  })
  if (!response.ok) await handleResponseError(response)
  return response.json()
}
