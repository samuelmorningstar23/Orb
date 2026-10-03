/**
 * Orb Hospital OS — Housekeeping & facilities API client.
 *
 * Bed/room turnaround state, cleaning requests, turnaround reporting and linen.
 *
 * WHY THIS FILE USES A RAW `fetch` INSTEAD OF `apiFetch`, same reason
 * src/api/procurement.ts gives: the shared wrapper flattens an error body to a
 * message string, and every refusal this module produces is one the UI has to
 * react to DIFFERENTLY. `module_disabled` is a whole-panel state;
 * `housekeeping_refused` on an illegal transition belongs on the state control
 * itself. The machine-readable `error` code has to survive the trip.
 *
 * NO PHI CROSSES THIS CLIENT. No type in this file has a patient id, a name, an
 * MRN or a diagnosis, and none may grow one. The only clinical fact present is
 * the boolean `clean_type === 'terminal'`.
 *
 * THE STATE MACHINE IS NOT DUPLICATED HERE. `getMeta()` returns the server's own
 * `legal_transitions` table and the panel drives its buttons from it. A hard-coded
 * copy in the UI is a second state machine, and the one that is wrong is always
 * the one the user can see.
 */
import { buildHeaders, API_BASE, errorDetailMessage } from './client';

/* ── Vocabulary ───────────────────────────────────────────────────────────── */

export type UnitState =
  | 'occupied' | 'vacant_dirty' | 'cleaning_in_progress' | 'vacant_ready' | 'out_of_service';

export type RequestType =
  | 'discharge_turnaround' | 'transfer_turnaround' | 'spillage'
  | 'routine' | 'deep_clean' | 'post_maintenance';

export type Priority = 'routine' | 'urgent' | 'immediate';
export type CleanType = 'standard' | 'terminal';
export type TaskStatus = 'open' | 'in_progress' | 'done' | 'cancelled';
export type LinenDirection = 'issue' | 'return_soiled' | 'return_clean' | 'condemned';

/* ── Wire types ───────────────────────────────────────────────────────────── */

export interface HousekeepingUnit {
  id: string;
  unit_code: string;
  ward: string;
  room: string | null;
  bed_label: string | null;
  unit_type: string;
  is_isolation_room: boolean;
  state: UnitState;
  state_since: string | null;
  is_active: boolean;
  notes: string | null;
  minutes_in_state?: number | null;
  clean_was_skipped?: boolean;
}

export interface CleaningTask {
  id: string;
  unit_id: string;
  unit_code?: string;
  ward: string;
  request_type: RequestType;
  priority: Priority;
  clean_type: CleanType;
  expected_minutes: number;
  status: TaskStatus;
  source_type: 'manual' | 'discharge' | 'transfer';
  description: string | null;
  precaution_note: string | null;
  vacated_at: string | null;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  ready_at: string | null;
  cancel_reason: string | null;
  /** ready_at − vacated_at. The bed was out of action for this long. */
  turnaround_minutes: number | null;
  /** completed_at − started_at. Somebody was working for this long. */
  cleaning_minutes: number | null;
  is_terminal_clean: boolean;
  elapsed_minutes: number | null;
  minutes_over_expected: number | null;
  is_overdue: boolean;
  unit_events?: UnitEvent[];
}

export interface UnitEvent {
  from_state: UnitState | null;
  to_state: UnitState;
  reason: string | null;
  actor_user_id: string | null;
  actor_role: string | null;
  occurred_at: string;
}

export interface WardReadiness {
  ward: string;
  registered_units: number;
  ward_capacity: number | null;
  /** Capped at 100. Above that the bed-count setting is stale, and
   *  `coverage_note` says so in words instead of a percentage. */
  coverage_pct: number | null;
  over_capacity?: boolean;
  coverage_note?: string | null;
  occupied: number;
  vacant_dirty: number;
  cleaning_in_progress: number;
  vacant_ready: number;
  out_of_service: number;
  not_ready_registered: number;
  /** What the capacity/forecast screen says right now. Repeated verbatim so the
   *  two numbers can be compared without opening two tabs. */
  capacity_view_available: number | null;
  capacity_view_admitted: number;
}

export interface ReadinessResponse {
  wards: WardReadiness[];
  capacity_basis: string;
  reconciliation_note: string;
}

export interface DischargeSweep {
  created?: number;
  already_present?: number;
  unmatched?: number;
  unmatched_wards?: Record<string, number>;
  scanned?: number;
  lookback_hours?: number;
  skipped?: boolean;
  error?: string;
  detail?: string;
}

export interface HousekeepingOverview {
  counts: {
    open: number; in_progress: number; overdue: number;
    terminal_open: number; immediate: number;
    /** Beds marked dirty that no open request covers: on no queue otherwise. */
    dirty_without_request?: number;
  };
  queue: CleaningTask[];
  readiness: ReadinessResponse;
  discharge_sweep: DischargeSweep;
  expected_minutes: { standard: number; terminal: number };
}

export interface TurnaroundWard {
  ward: string;
  completed: number;
  measured: number;
  unmeasurable: number;
  median_turnaround_minutes: number | null;
  p90_turnaround_minutes: number | null;
  longest_turnaround_minutes: number | null;
  median_cleaning_minutes: number | null;
  standard: { count: number; median_minutes: number | null; expected_minutes: number };
  terminal: { count: number; median_minutes: number | null; expected_minutes: number };
  over_expected: number;
}

export interface TurnaroundReport {
  window_days: number;
  measured_between: string;
  expected_minutes: { standard: number; terminal: number; source: string };
  wards: TurnaroundWard[];
}

export interface LinenItem { code: string; name: string; is_active: boolean }

export interface LinenLine {
  ward: string;
  item_code: string;
  item_name: string;
  issued: number;
  returned_soiled: number;
  returned_clean: number;
  condemned: number;
  /** Can be negative — a ward returning stock issued before the window opened.
   *  Reported as-is; clamping it to zero would read as "your linen reconciles". */
  outstanding: number;
}

export interface LinenSummary {
  window_days: number;
  definition: string;
  lines: LinenLine[];
  items: LinenItem[];
}

export interface HousekeepingMeta {
  states: UnitState[];
  /** THE SERVER'S transition table. The panel renders buttons from this. */
  legal_transitions: Record<UnitState, UnitState[]>;
  request_types: RequestType[];
  priorities: Priority[];
  clean_types: CleanType[];
  linen_directions: LinenDirection[];
  linen_items: LinenItem[];
  expected_minutes: { standard: number; terminal: number };
}

/* ── Transport ────────────────────────────────────────────────────────────── */

export class HousekeepingApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'HousekeepingApiError';
    this.status = status;
    this.code = code;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled' || this.status === 404;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/housekeeping${path}`, {
    ...options,
    credentials: 'include',
    headers: buildHeaders((options.method as string) || 'GET'),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === 'object' ? body.detail : null;
    throw new HousekeepingApiError(
      errorDetailMessage(body?.detail, response.status),
      response.status,
      detail?.error || `http_${response.status}`,
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

/* ── Calls ────────────────────────────────────────────────────────────────── */

export const getMeta = () => request<HousekeepingMeta>('/meta');

export const getOverview = (sync = true) =>
  request<HousekeepingOverview>(`/overview${query({ sync })}`);

export const getUnits = (ward?: string) =>
  request<{ units: HousekeepingUnit[] }>(`/units${query({ ward })}`).then(r => r.units);

/** `create_only` is set by the register form: a unit code already on the
 *  register is refused (409) instead of overwriting that bed. */
export const createUnit = (body: {
  unit_code: string; ward: string; room?: string; bed_label?: string;
  unit_type?: string; is_isolation_room?: boolean; initial_state?: UnitState; notes?: string;
  create_only?: boolean;
}) => post<HousekeepingUnit>('/units', body);

export const setUnitState = (unitId: string, state: UnitState, reason?: string) =>
  post<HousekeepingUnit>(`/units/${encodeURIComponent(unitId)}/state`, { state, reason });

export const recordOccupied = (unitId: string, reason: string) =>
  post<HousekeepingUnit>(`/units/${encodeURIComponent(unitId)}/occupy`, { reason });

export const getTasks = (params: {
  ward?: string; status?: TaskStatus; priority?: Priority; limit?: number;
} = {}) => request<{ tasks: CleaningTask[] }>(`/tasks${query(params)}`).then(r => r.tasks);

export const getTask = (taskId: string) =>
  request<CleaningTask>(`/tasks/${encodeURIComponent(taskId)}`);

export const raiseRequest = (body: {
  unit_id: string; request_type?: RequestType; priority?: Priority;
  clean_type?: CleanType; description?: string; precaution_note?: string;
}) => post<CleaningTask>('/tasks', body);

export const startCleaning = (taskId: string) =>
  post<CleaningTask>(`/tasks/${encodeURIComponent(taskId)}/start`);

export const completeCleaning = (taskId: string, note?: string) =>
  post<CleaningTask>(`/tasks/${encodeURIComponent(taskId)}/complete`, { note });

export const cancelRequest = (taskId: string, reason: string) =>
  post<CleaningTask>(`/tasks/${encodeURIComponent(taskId)}/cancel`, { reason });

export const syncDischarges = (lookbackHours?: number) =>
  post<DischargeSweep>(`/sync-discharges${query({ lookback_hours: lookbackHours })}`);

export const getTurnaround = (params: { ward?: string; days?: number } = {}) =>
  request<TurnaroundReport>(`/turnaround${query(params)}`);

export const getReadiness = (ward?: string) =>
  request<ReadinessResponse>(`/readiness${query({ ward })}`);

export const getLinen = (params: { ward?: string; days?: number } = {}) =>
  request<LinenSummary>(`/linen${query(params)}`);

export const recordLinen = (body: {
  ward: string; item_code: string; direction: LinenDirection; quantity: number;
  task_id?: string; note?: string; idempotency_key?: string;
}) => post<{ id: number; duplicate_ignored?: boolean }>('/linen', body);

/* ── Display helpers ──────────────────────────────────────────────────────── */

export const STATE_LABEL: Record<UnitState, string> = {
  occupied: 'Occupied',
  vacant_dirty: 'Vacant: needs cleaning',
  cleaning_in_progress: 'Cleaning in progress',
  vacant_ready: 'Ready',
  out_of_service: 'Out of service',
};

/** Status tone per state. `vacant_dirty` is WARN and not "neutral": a dirty bed
 *  is a bed somebody may be about to offer. */
export const STATE_TONE: Record<UnitState, 'ok' | 'warn' | 'danger' | 'info' | 'neutral'> = {
  occupied: 'info',
  vacant_dirty: 'warn',
  cleaning_in_progress: 'info',
  vacant_ready: 'ok',
  out_of_service: 'danger',
};

export const PRIORITY_LABEL: Record<Priority, string> = {
  routine: 'Routine', urgent: 'Urgent', immediate: 'Immediate',
};

export const REQUEST_TYPE_LABEL: Record<RequestType, string> = {
  discharge_turnaround: 'Discharge turnaround',
  transfer_turnaround: 'Transfer turnaround',
  spillage: 'Spillage',
  routine: 'Routine clean',
  deep_clean: 'Deep clean',
  post_maintenance: 'Back from maintenance',
};

export const LINEN_DIRECTION_LABEL: Record<LinenDirection, string> = {
  issue: 'Issued to ward',
  return_soiled: 'Returned soiled (to laundry)',
  return_clean: 'Returned clean (to store)',
  condemned: 'Condemned',
};

/**
 * Minutes → "4h 15m", or "27d 21h" past a day. Returns an em dash for null,
 * never "0m": a turnaround that has not been measured and a turnaround of zero
 * are opposite facts, and the one that reads as excellent must never be the
 * unmeasured one. Days, because "669h 5m" has to be divided by 24 in the
 * reader's head.
 */
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return '—';
  const total = Math.round(minutes);
  if (total < 60) return `${total}m`;
  if (total >= 1440) {
    const d = Math.floor(total / 1440);
    const h = Math.floor((total % 1440) / 60);
    return h ? `${d}d ${h}h` : `${d}d`;
  }
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}
