/**
 * Orb Hospital OS — NABH accreditation evidence API client.
 *
 * The incident / adverse-event register, the quality-indicator dashboard, and
 * the date-ranged evidence pack a hospital hands an assessor.
 *
 * WHY THIS FILE USES A RAW `fetch` INSTEAD OF `apiFetch`. The shared wrapper
 * flattens an error body down to a message string. Every refusal this module
 * can produce is one the UI reacts to DIFFERENTLY — `module_disabled` is a
 * whole-panel state, `open_actions` belongs on the close button,
 * `no_action_reason_required` has to open a field, `not_staff` is a permission
 * explanation — so the machine-readable `error` code has to survive the trip.
 * `request()` keeps it on a typed `NabhApiError`, and still sends the session
 * cookie and CSRF token exactly the way `apiFetch` does.
 *
 * ══════════════════════════════════════════════════════════════════════════
 *  THE ONE RULE THIS CLIENT ENFORCES ON THE VIEW
 * ══════════════════════════════════════════════════════════════════════════
 *
 * An indicator whose `state` is not `'reported'` HAS NO NUMBER. The server
 * already strips `value` and `display` to null before sending
 * (nabh_service._finalise), and `indicatorResult()` below is the second,
 * independent enforcement: it returns the WORDS for every non-reported state
 * and never falls back to `?? 0`, `?? '—'` or an empty string that a reader
 * could take for a zero.
 *
 * Two independent gates on purpose. This is the same discipline
 * src/api/scorecard.ts runs for the shadow scorecard, and for the same reason:
 * it should take two separate bugs, in two languages, to print a flattering
 * number on an accreditation document.
 */
import { API_BASE, buildHeaders, errorDetailMessage } from './client';

/** A refusal from the NABH API, with its machine-readable code intact. */
export class NabhApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'NabhApiError';
    this.status = status;
    this.code = code;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled';
  }

  /** True when the account may report but may not read or adjudicate. */
  get isForbidden(): boolean {
    return this.status === 403;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/nabh${path}`, {
    ...options,
    credentials: 'include',
    headers: buildHeaders((options.method as string) || 'GET'),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === 'object' ? body.detail : null;
    throw new NabhApiError(
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

/* ── Types ────────────────────────────────────────────────────────────────── */

export type IndicatorState =
  | 'reported'
  | 'insufficient_data'
  | 'not_collected'
  | 'not_computable';

export interface Indicator {
  id: string;
  code: string;
  label: string;
  definition: string;
  unit: string;
  state: IndicatorState;
  /** NULL unless `state === 'reported'`. Never render this without checking. */
  value: number | null;
  /** NULL unless `state === 'reported'`. Never render this without checking. */
  display: string | null;
  numerator: number | null;
  denominator: number | null;
  numerator_label: string;
  denominator_label: string;
  n_observed: number;
  n_required: number;
  direction: 'lower_is_better' | 'higher_is_better' | 'neutral';
  is_proxy: boolean;
  caveat: string;
  sources: string[];
  blockers: string[];
  excluded: Record<string, unknown>;
  detail: Record<string, unknown>;
}

export interface IndicatorDashboard {
  period: { start: string; end: string; start_date: string; end_date: string };
  ward: string | null;
  generated_at: string;
  indicators: Indicator[];
  summary: {
    total: number;
    reported: number;
    insufficient_data: number;
    not_collected: number;
    not_computable: number;
  };
  denominators: {
    inpatient_days: {
      inpatient_days: number; patients: number; open_stays: number;
      assumed_ends: number; method: string;
    };
    dispositions: {
      recorded: number; discharge_events_observed: number;
      coverage: number; coverage_floor: number;
    };
  };
  register_usage: {
    total: number; in_use: boolean;
    by_type: Record<string, number>; by_severity: Record<string, number>;
    near_miss_share: number | null;
  };
  not_collected_catalogue: GapEntry[];
  attestation: string;
}

export interface GapEntry {
  code: string;
  label: string;
  reason: string;
  needed: string;
}

export type IncidentStatus =
  | 'reported' | 'under_investigation' | 'actions_pending' | 'closed' | 'void';

export interface IncidentAction {
  id: number;
  incident_id: number;
  action_type: string;
  description: string;
  owner_user_id: string | null;
  owner_description: string | null;
  due_date: string | null;
  status: 'open' | 'completed' | 'cancelled';
  completed_at: string | null;
  completed_by: string | null;
  completion_note: string | null;
}

export interface IncidentEvent {
  id: number;
  seq: number;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  /** NULL for an anonymous report — deliberately, not accidentally. */
  actor_user_id: string | null;
  detail: string | null;
  occurred_at: string;
}

export interface Incident {
  id: number;
  reference: string;
  incident_type: string;
  severity: string;
  title: string;
  description: string;
  occurred_at: string;
  reported_at: string;
  reported_at_precision: 'exact' | 'day';
  ward: string | null;
  department: string | null;
  location: string | null;
  patient_id: string | null;
  is_anonymous: boolean;
  reported_by: string | null;
  reporter_role: string | null;
  status: IncidentStatus;
  investigation_summary: string | null;
  root_cause: string | null;
  contributing_factors: string[];
  no_action_reason: string | null;
  closure_note: string | null;
  closed_at: string | null;
  void_reason: string | null;
  reopened_count: number;
  externally_reportable: boolean;
  allowed_transitions: IncidentStatus[];
  open_action_count?: number;
  actions?: IncidentAction[];
  events?: IncidentEvent[];
}

export interface NabhMeta {
  incident_types: string[];
  severities: string[];
  sentinel_severities: string[];
  statuses: IncidentStatus[];
  transitions: Record<string, IncidentStatus[]>;
  action_types: string[];
  dispositions: string[];
  death_dispositions: string[];
  ama_dispositions: string[];
  indicators: {
    id: string; code: string; label: string; unit: string;
    definition: string; direction: string; is_proxy: boolean; sources: string[];
  }[];
  not_collected_catalogue: GapEntry[];
  anonymity_statement: string;
  attestation: string;
  /** The hospital's wards (names only), for the ward selects. Optional: an
   *  older server does not send it. */
  wards?: string[];
  thresholds: {
    escalation_ack_minutes: number;
    readmission_days: number;
    closure_target_days: number;
    disposition_coverage_floor: number;
  };
}

export interface EvidencePack {
  reference: string;
  content_sha256: string;
  digest_note: string;
  period: { start: string; end: string; start_date: string; end_date: string };
  ward: string | null;
  generated_at: string;
  generated_by: string | null;
  indicators: Indicator[];
  indicator_summary: IndicatorDashboard['summary'];
  register: {
    count: number;
    anonymous_reports: number;
    by_status: Record<string, number>;
    incidents: Incident[];
  };
  not_collected: GapEntry[];
  limitations: string[];
  anonymity_statement: string;
  attestation: string;
}

export interface ExportRow {
  id: number;
  reference: string;
  period_start: string;
  period_end: string;
  ward: string | null;
  generated_by: string | null;
  generated_at: string;
  include_patient_identifiers: number;
  reported_count: number;
  insufficient_count: number;
  not_collected_count: number;
  incident_count: number;
  content_sha256: string;
}

/* ── The honesty rule, client side ────────────────────────────────────────── */

/** What the panel prints for a non-reported indicator. Words, never digits. */
export const STATE_TEXT: Record<Exclude<IndicatorState, 'reported'>, string> = {
  insufficient_data: 'Insufficient data',
  not_collected: 'Not collected',
  not_computable: 'Not computable',
};

export interface IndicatorResult {
  /** What to render. NEVER an empty string, a dash, or a zero. */
  text: string;
  /** True only when `text` is a measured number. */
  isNumber: boolean;
  state: IndicatorState;
}

/**
 * The one place the panel turns an indicator into something printable.
 *
 * A `reported` indicator with a missing `display` is treated as NOT reported.
 * That combination should be impossible — the server's `_finalise` will not
 * emit it — but "should be impossible" is how a zero reaches a page an assessor
 * reads, and the cost of the extra branch is one line.
 */
export function indicatorResult(indicator: Indicator): IndicatorResult {
  if (indicator.state === 'reported' && indicator.display) {
    return { text: indicator.display, isNumber: true, state: 'reported' };
  }
  const state = indicator.state === 'reported' ? 'not_computable' : indicator.state;
  return { text: STATE_TEXT[state], isNumber: false, state };
}

/** The one-line reason a number is absent, for the card body. */
export function indicatorReason(indicator: Indicator): string | null {
  if (indicator.state === 'reported') return null;
  if (indicator.blockers.length) return indicator.blockers[0];
  const shortfall = indicator.detail?.coverage_shortfall;
  if (typeof shortfall === 'string') return shortfall;
  if (indicator.n_required > indicator.n_observed) {
    return `Needs at least ${indicator.n_required} ${indicator.denominator_label}; `
      + `this period has ${indicator.n_observed}.`;
  }
  return 'There is not enough recorded data in this period to report a value.';
}

/* ── Calls ────────────────────────────────────────────────────────────────── */

export function getMeta(): Promise<NabhMeta> {
  return request<NabhMeta>('/meta');
}

export function getIndicators(params: {
  period_start?: string; period_end?: string; ward?: string;
} = {}): Promise<IndicatorDashboard> {
  return request<IndicatorDashboard>(`/indicators${query(params)}`);
}

export function getIncidents(params: {
  period_start?: string; period_end?: string; status?: string;
  incident_type?: string; severity?: string; ward?: string;
  include_void?: boolean; limit?: number;
} = {}): Promise<{ incidents: Incident[]; count: number; truncated: boolean }> {
  return request(`/incidents${query(params)}`);
}

export function getIncident(id: number): Promise<Incident> {
  return request<Incident>(`/incidents/${id}`);
}

export function reportIncident(body: {
  incident_type: string; severity: string; title: string; description: string;
  occurred_at?: string; ward?: string; department?: string; location?: string;
  patient_id?: string; is_anonymous?: boolean;
}): Promise<{ incident: Incident; anonymity_statement: string }> {
  return post('/incidents', body);
}

export function startInvestigation(id: number, note?: string): Promise<Incident> {
  return post(`/incidents/${id}/investigate`, { note });
}

export function recordInvestigation(id: number, body: {
  summary: string; root_cause?: string; contributing_factors?: string[];
}): Promise<Incident> {
  return post(`/incidents/${id}/investigation`, body);
}

export function addAction(id: number, body: {
  description: string; action_type?: string; owner_description?: string;
  due_date?: string;
}): Promise<Incident> {
  return post(`/incidents/${id}/actions`, body);
}

export function completeAction(id: number, actionId: number,
                               note?: string): Promise<Incident> {
  return post(`/incidents/${id}/actions/${actionId}/complete`, { note });
}

export function closeIncident(id: number, body: {
  closure_note?: string; no_action_reason?: string;
}): Promise<Incident> {
  return post(`/incidents/${id}/close`, body);
}

export function voidIncident(id: number, reason: string): Promise<Incident> {
  return post(`/incidents/${id}/void`, { reason });
}

export function recordDisposition(body: {
  patient_id: string; disposition: string; occurred_at?: string; notes?: string;
}): Promise<Record<string, unknown>> {
  return post('/dispositions', body);
}

export function generatePack(body: {
  period_start?: string; period_end?: string; ward?: string;
  include_patient_identifiers?: boolean;
}): Promise<EvidencePack> {
  return post<EvidencePack>('/export', body);
}

export function getExports(limit = 50): Promise<{ exports: ExportRow[] }> {
  return request(`/exports${query({ limit })}`);
}

/**
 * The Markdown pack, as text. Uses the same raw `fetch` so the module-disabled
 * and permission codes survive; the body is text, not JSON.
 */
export async function generatePackMarkdown(body: {
  period_start?: string; period_end?: string; ward?: string;
  include_patient_identifiers?: boolean;
}): Promise<string> {
  const response = await fetch(`${API_BASE}/nabh/export.md`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const parsed = await response.json().catch(() => null);
    const detail = parsed && typeof parsed.detail === 'object' ? parsed.detail : null;
    throw new NabhApiError(errorDetailMessage(parsed?.detail, response.status),
                           response.status, detail?.error || `http_${response.status}`);
  }
  return response.text();
}
