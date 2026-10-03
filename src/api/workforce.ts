/**
 * Orb Hospital OS — Workforce API client.
 *
 * Duty roster, staff attendance, overtime and the payroll EXPORT.
 *
 * WHAT THIS CLIENT WILL NEVER HAVE. There is no salary type, no PF/ESI/TDS
 * field and no money helper imported anywhere in this file. Orb records the
 * roster and the attendance; the hospital's payroll provider computes the pay.
 * The export's statutory columns arrive EMPTY and every row carries
 * `statutory_computed_by`, which is what makes that boundary visible on the
 * bureau's desk rather than only in a design document.
 *
 * WHY THIS FILE USES A RAW `fetch` INSTEAD OF `apiFetch`.
 * The shared wrapper flattens an error body down to a message string. Every
 * refusal this module can produce is one the UI has to react to DIFFERENTLY —
 * `module_disabled` is a whole-panel state, `roster_conflict` needs the list of
 * clashing shifts next to the form, `leave_type_required` belongs on one field,
 * `span_too_long` needs the times highlighted — so the machine-readable `error`
 * code (and, for a conflict, the `conflicts` array) has to survive the trip.
 * `request()` below keeps both on a typed `WorkforceApiError`, and still sends
 * the session cookie and CSRF token exactly the way `apiFetch` does.
 *
 * DURATIONS ARE INTEGER MINUTES on the wire. Decimal hours travel as STRINGS
 * the server rendered ("7.50"), never as floats — the panel displays the
 * server's own string so the screen can never disagree with the file.
 */
import { buildHeaders, API_BASE, errorDetailMessage } from './client';

/** A refusal from the workforce API, with its machine-readable code intact. */
export class WorkforceApiError extends Error {
  status: number;
  code: string;
  /** Present on a `roster_conflict`: the assignments that clash. */
  conflicts: RosterConflictDetail[];

  constructor(message: string, status: number, code: string,
              conflicts: RosterConflictDetail[] = []) {
    super(message);
    this.name = 'WorkforceApiError';
    this.status = status;
    this.code = code;
    this.conflicts = conflicts;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled';
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/workforce${path}`, {
    ...options,
    credentials: 'include',
    headers: buildHeaders((options.method as string) || 'GET'),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === 'object' ? body.detail : null;
    throw new WorkforceApiError(
      errorDetailMessage(body?.detail, response.status),
      response.status,
      detail?.error || `http_${response.status}`,
      Array.isArray(detail?.conflicts) ? detail.conflicts : [],
    );
  }
  return response.json() as Promise<T>;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) });
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/* ── Types ────────────────────────────────────────────────────────────────── */

export type DayStatus = 'present' | 'absent' | 'leave' | 'weekly_off' | 'holiday';
/** The sixth state. It is never STORED — it is the absence of a record — but the
 *  UI must be able to name it, because "nobody wrote this day down" is a
 *  different fact from "this person did not come in". */
export type ActualStatus = DayStatus | 'unrecorded' | 'scheduled';

export interface ShiftType {
  code: string;
  name: string;
  start_minute: number;
  duration_minutes: number;
  start_time: string;
  end_time: string;
  crosses_midnight: boolean;
  allowance_code: 'night' | 'evening' | null;
  is_active: boolean;
}

export interface LeaveType {
  code: string;
  name: string;
  is_paid: boolean;
  is_active: boolean;
}

export interface Employee {
  employee_code: string;
  user_id: string | null;
  full_name: string;
  department: string | null;
  designation: string | null;
  employment_type: string;
  date_of_joining: string | null;
  date_of_exit: string | null;
  weekly_off_day: number | null;
  weekly_off_label: string;
  payroll_bureau_ref: string | null;
  is_active: boolean;
  notes: string | null;
}

export interface WorkforcePolicy {
  overtime_daily_minutes: number;
  late_grace_minutes: number;
  max_span_minutes: number;
  overtime_basis: string;
  statutory_boundary: string;
}

export interface WorkforceMeta {
  shift_types: ShiftType[];
  leave_types: LeaveType[];
  day_statuses: DayStatus[];
  employment_types: string[];
  attendance_sources: string[];
  policy: WorkforcePolicy;
  min_rest_minutes: number;
  payroll_export_columns: string[];
  statutory_columns: string[];
  statutory_computed_by: string;
  boundary: { orb_owns: string; provider_owns: string; message: string };
}

export interface RosterAssignment {
  roster_id: number;
  employee_code: string;
  employee_name: string;
  designation: string | null;
  department: string | null;
  ward: string | null;
  status: string;
  actual_status: ActualStatus;
  actual_in: string | null;
  actual_out: string | null;
  worked_minutes: number;
  overtime_minutes: number;
  late_minutes: number;
}

export interface RosterSlot {
  shift_code: string;
  shift_name: string;
  start_time: string | null;
  end_time: string | null;
  assignments: RosterAssignment[];
  planned_count: number;
  present_count: number;
  absent_count: number;
  leave_count: number;
  unrecorded_count: number;
  /** Rostered lines after today: not a gap, nothing to record yet. */
  scheduled_count: number;
}

export interface RosterDay {
  date: string;
  is_holiday: boolean;
  weekday: number;
  shifts: RosterSlot[];
}

export interface RosterBoard {
  date_from: string;
  date_to: string;
  department: string | null;
  days: RosterDay[];
  shift_types: ShiftType[];
}

export interface RosterConflictDetail {
  kind: 'overlap' | 'short_rest' | 'double_shift' | 'duplicate' | 'unknown_shift';
  roster_id?: number;
  roster_date?: string;
  shift_code?: string;
  shift_name?: string;
  department?: string | null;
  rest_minutes?: number;
  message: string;
}

export interface RosterEntry {
  id: number;
  roster_date: string;
  shift_code: string;
  employee_code: string;
  department: string | null;
  ward: string | null;
  status: string;
  warnings: RosterConflictDetail[];
}

export interface StandingConflict {
  employee_code: string;
  employee_name: string;
  kind: string;
  first: { roster_id: number; roster_date: string; shift_code: string; department: string | null };
  second: { roster_id: number; roster_date: string; shift_code: string; department: string | null };
  message: string;
}

export interface AttendanceRow {
  id: number;
  attendance_date: string;
  employee_code: string;
  employee_name: string;
  department?: string | null;
  designation?: string | null;
  shift_code: string | null;
  status: DayStatus;
  leave_type_code: string | null;
  first_in_at: string | null;
  last_out_at: string | null;
  worked_minutes: number;
  worked_hours: string;
  late_minutes: number;
  overtime_minutes: number;
  overtime_hours: string;
  source: string;
  remarks: string | null;
}

export interface SummaryIdentity {
  buckets_sum: number;
  days_in_period: number;
  holds: boolean;
  statement: string;
}

export interface EmployeeSummary {
  employee_code: string;
  employee_name: string;
  department: string | null;
  designation: string | null;
  employment_type: string;
  payroll_bureau_ref: string | null;
  period_start: string;
  period_end: string;
  days_in_period: number;
  days_working: number;
  days_present: number;
  days_absent: number;
  days_leave_paid: number;
  days_leave_unpaid: number;
  days_leave_total: number;
  days_weekly_off: number;
  days_holiday: number;
  days_unrecorded: number;
  /** In employment but after today, so not in any bucket yet. */
  days_still_to_come: number;
  counted_through: string | null;
  days_absent_or_unpaid_leave: number;
  leave_by_type: Record<string, number>;
  attendance_complete: boolean;
  unrecorded_dates: string[];
  worked_minutes: number;
  worked_hours: string;
  overtime_minutes: number;
  overtime_hours: string;
  late_marks: number;
  late_minutes: number;
  night_shift_count: number;
  evening_shift_count: number;
  holiday_shift_count: number;
  weekly_off_worked_count: number;
  identity: SummaryIdentity;
}

export interface PeriodSummary {
  period_start: string;
  period_end: string;
  year: number;
  month: number;
  department: string | null;
  /** Last day counted: today inside an open month, null for a future month. */
  counted_through: string | null;
  period_closed: boolean;
  employees: EmployeeSummary[];
  employee_count: number;
  incomplete_employee_count: number;
  unrecorded_day_count: number;
  still_to_come_day_count: number;
  identity_holds: boolean;
  totals: Record<string, number | string>;
}

export interface PayrollExportPreview {
  columns: string[];
  rows: Record<string, string>[];
  period_start: string;
  period_end: string;
  year: number;
  month: number;
  department: string | null;
  employee_count: number;
  incomplete_employee_count: number;
  counted_through: string | null;
  period_closed: boolean;
  still_to_come_day_count: number;
  file_name: string;
  warnings: string[];
  statutory_note: string;
}

export interface PayrollExportRun {
  id: number;
  period_start: string;
  period_end: string;
  department: string | null;
  file_name: string;
  row_count: number;
  employee_count: number;
  incomplete_employee_count: number;
  column_signature: string;
  sha256: string;
  generated_by: string | null;
  generated_at: string;
  notes: string | null;
}

export interface GeneratedExport {
  run: PayrollExportRun;
  file_name: string;
  csv: string;
  sha256: string;
  columns: string[];
  employee_count: number;
  incomplete_employee_count: number;
  warnings: string[];
  statutory_note: string;
}

export interface Holiday {
  holiday_date: string;
  name: string;
  is_restricted: boolean;
}

/* ── Calls ────────────────────────────────────────────────────────────────── */

export function getMeta(): Promise<WorkforceMeta> {
  return request<WorkforceMeta>('/meta');
}

export function getEmployees(params: {
  department?: string; include_inactive?: boolean; search?: string;
} = {}): Promise<{ employees: Employee[] }> {
  return request(`/employees${query(params)}`);
}

/** `create_only` is set by the Add drawer: a code already on the register is
 *  refused (409, code 'already_exists') instead of overwriting that record. */
export function saveEmployee(payload: Partial<Employee> & {
  employee_code: string; full_name: string; create_only?: boolean;
}): Promise<Employee> {
  return post<Employee>('/employees', payload);
}

export function getRoster(params: {
  date_from: string; date_to: string; department?: string;
}): Promise<RosterBoard> {
  return request(`/roster${query(params)}`);
}

export function getRosterConflicts(params: {
  date_from: string; date_to: string; department?: string;
}): Promise<{ conflicts: StandingConflict[]; conflict_count: number }> {
  return request(`/roster/conflicts${query(params)}`);
}

export function assignRoster(payload: {
  roster_date: string; shift_code: string; employee_code: string;
  department?: string | null; ward?: string | null; assigned_role?: string | null;
  status?: string; notes?: string | null; allow_short_rest?: boolean;
}): Promise<RosterEntry> {
  return post<RosterEntry>('/roster', payload);
}

export function cancelRosterEntry(rosterId: number): Promise<RosterEntry> {
  return post<RosterEntry>(`/roster/${rosterId}/cancel`);
}

export function getAttendance(params: {
  date_from: string; date_to: string; department?: string; employee_code?: string;
}): Promise<{ attendance: AttendanceRow[]; count: number }> {
  return request(`/attendance${query(params)}`);
}

export function recordAttendance(payload: {
  employee_code: string; attendance_date: string; status: DayStatus;
  shift_code?: string | null;
  /** FULL ISO instants with an offset — never a time-of-day. A night shift's
   *  punch-out lands on the next calendar date and the server will not guess. */
  first_in_at?: string | null; last_out_at?: string | null;
  leave_type_code?: string | null; source?: string; remarks?: string | null;
}): Promise<AttendanceRow> {
  return post<AttendanceRow>('/attendance', payload);
}

export function getSummary(params: {
  year: number; month: number; department?: string; employee_code?: string;
}): Promise<PeriodSummary | { employees: EmployeeSummary[] }> {
  return request(`/attendance/summary${query(params)}`);
}

export function getHolidays(params: {
  date_from?: string; date_to?: string;
} = {}): Promise<{ holidays: Holiday[] }> {
  return request(`/holidays${query(params)}`);
}

export function saveHoliday(payload: {
  holiday_date: string; name: string; is_restricted?: boolean;
}): Promise<Holiday> {
  return post<Holiday>('/holidays', payload);
}

export function deleteHoliday(holidayDate: string): Promise<{ deleted: boolean }> {
  return request(`/holidays/${holidayDate}`, { method: 'DELETE' });
}

/** Preview the payroll file. Deliberately side-effect free — it does NOT count
 *  as a send, so a clerk can look at February as often as they like. */
export function previewPayrollExport(params: {
  year: number; month: number; department?: string;
}): Promise<PayrollExportPreview> {
  return request(`/payroll-export${query(params)}`);
}

/** Generate the file AND record the run (checksum + header order). This is the
 *  copy that goes to the bureau; `payroll-export/runs` is the proof of what was
 *  sent when the bureau queries a figure three months later. */
export function generatePayrollExport(payload: {
  year: number; month: number; department?: string; notes?: string;
}): Promise<GeneratedExport> {
  return post<GeneratedExport>('/payroll-export', payload);
}

export function getExportRuns(limit = 25): Promise<{ runs: PayrollExportRun[] }> {
  return request(`/payroll-export/runs${query({ limit })}`);
}

/* ── Presentation helpers ─────────────────────────────────────────────────── */

/** Integer minutes → "7h 30m". For DISPLAY only: every figure that reaches the
 *  payroll file is the server's own string, computed by integer arithmetic
 *  there. This helper must never be the source of a number in the export. */
export function formatDuration(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return '—';
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (!h) return `${m}m`;
  return m ? `${h}h ${m}m` : `${h}h`;
}

/** A `<input type="datetime-local">` value → a full ISO instant with the
 *  browser's offset. The server takes instants precisely so that a night shift
 *  crossing midnight is unambiguous; sending the bare local string would strip
 *  the offset and re-introduce the guess this module exists to avoid. */
export function localInputToIso(value: string): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString();
}

/** An ISO instant → the value a `<input type="datetime-local">` wants. */
export function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`
    + `T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

/** Trigger a browser download of an already-generated CSV.
 *  Takes the text the server returned rather than re-fetching, so the file the
 *  user saves is byte-identical to the one the recorded checksum covers. */
export function downloadCsv(fileName: string, csvText: string): void {
  const blob = new Blob([csvText], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
