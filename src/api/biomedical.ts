/**
 * Orb Hospital OS — Biomedical asset & equipment API client.
 *
 * The equipment register, its preventive-maintenance and CALIBRATION schedules,
 * the breakdown/downtime log, and AMC/CMC contracts.
 *
 * Shared transport handles session ownership, refresh and CSRF. Domain errors
 * keep their machine-readable codes and structured refusal details.
 *
 * DATES ARE `YYYY-MM-DD` STRINGS ON THE WIRE, never `Date` objects. A `Date` is
 * an instant, and serialising one through the browser's timezone is how a
 * calibration due on 1 April becomes 31 March for a user east of UTC — which is
 * the exact off-by-one the server module is built to prevent. This file does no
 * date arithmetic at all: `is_overdue`, `days_overdue` and `cover_state` are
 * computed on the server and read here.
 *
 * MONEY is integer paise, rendered from the server's own `*_display` strings so
 * the screen cannot disagree with the record.
 */
import { domainRequest } from './domainTransport';

/** A refusal from the biomedical API, with its machine-readable code intact. */
export class BiomedicalApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'BiomedicalApiError';
    this.status = status;
    this.code = code;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled';
  }
}

function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  return domainRequest<T>(`/biomedical${path}`, options,
    (message, status, code) => new BiomedicalApiError(message, status, code));
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

export type Criticality = 'life_support' | 'critical' | 'routine';
export type AssetStatus =
  | 'in_service' | 'standby' | 'under_maintenance' | 'down' | 'condemned';
export type ScheduleType =
  | 'preventive_maintenance' | 'calibration' | 'electrical_safety';
export type Compliance = 'ok' | 'due_soon' | 'overdue' | 'inactive';
export type CoverState = 'active' | 'expiring' | 'expired' | 'terminated' | 'renewed';

export interface Asset {
  id: number;
  asset_tag: string;
  name: string;
  category: string;
  manufacturer: string | null;
  model: string | null;
  serial_number: string | null;
  department: string | null;
  ward: string | null;
  location_detail: string | null;
  criticality: Criticality;
  criticality_label: string;
  status: AssetStatus;
  purchase_date: string | null;
  commissioned_date: string | null;
  purchase_cost_paise: number | null;
  cost_is_configured: number;
  cost_display: string;
  warranty_expiry_date: string | null;
  warranty_active: boolean;
  warranty_days_remaining: number | null;
  vendor_id: number | null;
  produces_clinical_readings: number;
  notes: string | null;
  is_active: number;
  is_available: boolean;
}

export interface Schedule {
  id: number;
  asset_id: number;
  schedule_type: ScheduleType;
  schedule_type_label: string;
  interval_days: number;
  /** null means NEVER DONE — not "no data". See `never_done`. */
  last_done_date: string | null;
  next_due_date: string;
  effective_due_date: string | null;
  grace_days: number;
  lead_days: number;
  responsible: string | null;
  last_certificate_reference: string | null;
  is_active: number;
  never_done: boolean;
  is_overdue: boolean;
  days_overdue: number;
  days_until_due: number | null;
  due_soon: boolean;
  compliance: Compliance;
  /** An overdue CALIBRATION on a device Orb reads numbers from. */
  measurement_at_risk: boolean;
  /* Present when the schedule came back through a list/register join. */
  asset_asset_tag?: string;
  asset_name?: string;
  asset_ward?: string | null;
  asset_department?: string | null;
  asset_criticality?: Criticality;
  asset_status?: AssetStatus;
  asset_produces_clinical_readings?: number;
  /* Present only on the response to `recordCompletion`. */
  recorded_result?: string;
  recorded_was_overdue?: boolean;
  recorded_days_overdue?: number;
}

export interface ServiceCall {
  id: number;
  call_number: string | null;
  asset_id: number;
  asset_tag?: string;
  asset_name?: string;
  reported_at: string;
  reported_by: string | null;
  ward: string | null;
  fault_description: string;
  severity: 'breakdown' | 'degraded' | 'preventive';
  took_out_of_service: number;
  down_from: string | null;
  restored_at: string | null;
  /** Frozen at close. null while the call is open — see the effective figure. */
  downtime_minutes: number | null;
  downtime_minutes_effective: number;
  downtime_is_running: boolean;
  is_open: boolean;
  status: string;
  resolution: string | null;
  attended_by: string | null;
  cost_display: string;
}

export interface Contract {
  id: number;
  contract_number: string;
  contract_type: 'amc' | 'cmc' | 'warranty';
  vendor_name: string | null;
  start_date: string;
  end_date: string;
  value_paise: number | null;
  value_is_configured: number;
  value_display: string;
  notice_days: number;
  covers_calibration: number;
  covers_parts: number;
  response_hours: number | null;
  status: string;
  scope_notes: string | null;
  is_expired: boolean;
  days_remaining: number | null;
  in_notice_window: boolean;
  cover_state: CoverState;
  asset_count?: number;
  assets?: { id: number; asset_tag: string; name: string; ward: string | null }[];
}

export interface DueRegister {
  as_of: string;
  overdue: Schedule[];
  overdue_count: number;
  due_soon: Schedule[];
  due_soon_count: number;
  overdue_calibration_count: number;
  overdue_life_support_count: number;
  never_done_count: number;
  measurement_at_risk_count: number;
}

export interface Overview {
  as_of: string;
  assets_total: number;
  assets_down: number;
  assets_under_maintenance: number;
  life_support_assets: number;
  measuring_assets: number;
  overdue: Schedule[];
  overdue_count: number;
  overdue_calibration_count: number;
  overdue_life_support_count: number;
  measurement_at_risk_count: number;
  never_done_count: number;
  due_soon: Schedule[];
  due_soon_count: number;
  open_calls: ServiceCall[];
  open_call_count: number;
  contracts_expiring: Contract[];
  contracts_expiring_count: number;
  contracts_expired_count: number;
  assets_with_no_schedule: Asset[];
  assets_with_no_schedule_count: number;
  downtime_top: DowntimeRow[];
}

export interface DowntimeRow {
  asset_id: number;
  asset_tag: string;
  asset_name: string;
  ward: string | null;
  department: string | null;
  criticality: Criticality;
  status: AssetStatus;
  downtime_minutes: number;
  downtime_hours: number;
}

export interface WardAttention {
  ward: string;
  as_of: string;
  asset_count: number;
  overdue: Schedule[];
  overdue_count: number;
  overdue_calibration_count: number;
  measurement_at_risk_count: number;
  due_soon: Schedule[];
  due_soon_count: number;
  unavailable: Asset[];
  unavailable_count: number;
  open_calls: ServiceCall[];
  open_call_count: number;
  headline: string;
}

export interface BiomedicalMeta {
  asset_categories: string[];
  criticalities: Criticality[];
  asset_statuses: AssetStatus[];
  unavailable_statuses: string[];
  schedule_types: ScheduleType[];
  schedule_type_labels: Record<string, string>;
  schedule_results: string[];
  service_call_statuses: string[];
  open_call_statuses: string[];
  service_call_severities: string[];
  contract_types: string[];
  contract_statuses: string[];
  default_lead_days: number;
  default_notice_days: number;
  due_rule: string;
  contract_rule: string;
}

/* ── Reads ────────────────────────────────────────────────────────────────── */

export function getMeta(): Promise<BiomedicalMeta> {
  return request<BiomedicalMeta>('/meta');
}

export function getOverview(): Promise<Overview> {
  return request<Overview>('/overview');
}

export function getDueRegister(params: {
  scheduleType?: ScheduleType | '';
  ward?: string;
  onlyOverdue?: boolean;
} = {}): Promise<DueRegister> {
  return request<DueRegister>(`/due${query({
    schedule_type: params.scheduleType,
    ward: params.ward,
    only_overdue: params.onlyOverdue,
  })}`);
}

export function getCoverageGaps(): Promise<{ assets: Asset[]; count: number }> {
  return request('/coverage-gaps');
}

export function getWardAttention(ward: string): Promise<WardAttention> {
  return request<WardAttention>(`/wards/${encodeURIComponent(ward)}/attention`);
}

export function getAssets(params: {
  ward?: string; department?: string; category?: string;
  criticality?: string; status?: string; search?: string;
  includeInactive?: boolean; limit?: number;
} = {}): Promise<{ assets: Asset[]; count: number }> {
  return request(`/assets${query({
    ward: params.ward, department: params.department, category: params.category,
    criticality: params.criticality, status: params.status, search: params.search,
    include_inactive: params.includeInactive, limit: params.limit,
  })}`);
}

export function getAsset(assetId: number): Promise<Asset & {
  schedules: Schedule[]; service_calls: ServiceCall[];
  contracts: Contract[]; history: Record<string, unknown>[];
  downtime_minutes_90d: number;
}> {
  return request(`/assets/${assetId}`);
}

export function getSchedules(params: {
  assetId?: number; scheduleType?: ScheduleType | ''; ward?: string;
} = {}): Promise<{ schedules: Schedule[]; count: number }> {
  return request(`/schedules${query({
    asset_id: params.assetId, schedule_type: params.scheduleType, ward: params.ward,
  })}`);
}

export function getServiceCalls(params: {
  assetId?: number; ward?: string; status?: string;
  openOnly?: boolean; limit?: number;
} = {}): Promise<{ service_calls: ServiceCall[]; count: number }> {
  return request(`/service-calls${query({
    asset_id: params.assetId, ward: params.ward, status: params.status,
    open_only: params.openOnly, limit: params.limit,
  })}`);
}

export function getDowntime(params: { ward?: string; days?: number; limit?: number } = {}):
Promise<{ as_of: string; window_days: number; assets: DowntimeRow[]; count: number; total_minutes: number }> {
  return request(`/downtime${query({
    ward: params.ward, days: params.days, limit: params.limit,
  })}`);
}

export function getContracts(params: { status?: string; contractType?: string } = {}):
Promise<{ contracts: Contract[]; count: number }> {
  return request(`/contracts${query({
    status: params.status, contract_type: params.contractType,
  })}`);
}

export function getExpiringContracts(withinDays?: number): Promise<{
  as_of: string; expiring: Contract[]; expiring_count: number;
  expired: Contract[]; expired_count: number;
}> {
  return request(`/contracts/expiring${query({ within_days: withinDays })}`);
}

/* ── Writes ───────────────────────────────────────────────────────────────── */

export interface AssetInput {
  asset_tag: string;
  name: string;
  category?: string;
  manufacturer?: string | null;
  model?: string | null;
  serial_number?: string | null;
  department?: string | null;
  ward?: string | null;
  location_detail?: string | null;
  criticality?: Criticality;
  status?: AssetStatus;
  purchase_date?: string | null;
  commissioned_date?: string | null;
  purchase_cost_paise?: number | null;
  warranty_expiry_date?: string | null;
  produces_clinical_readings?: boolean;
  notes?: string | null;
  /** The Add form: a tag already on the register is refused (409,
   *  code 'already_exists') instead of overwriting that machine's record. */
  create_only?: boolean;
}

export function saveAsset(input: AssetInput): Promise<Asset> {
  return post<Asset>('/assets', input);
}

export function setAssetStatus(assetId: number, status: AssetStatus, reason?: string):
Promise<Asset> {
  return post<Asset>(`/assets/${assetId}/status`, { status, reason });
}

export interface ScheduleInput {
  asset_id?: number;
  asset_tag?: string;
  schedule_type: ScheduleType;
  interval_days: number;
  last_done_date?: string | null;
  first_due_date?: string | null;
  grace_days?: number;
  lead_days?: number;
  responsible?: string | null;
  certificate_reference?: string | null;
}

export function saveSchedule(input: ScheduleInput): Promise<Schedule> {
  return post<Schedule>('/schedules', input);
}

export interface CompletionInput {
  schedule_id?: number;
  asset_id?: number;
  schedule_type?: ScheduleType;
  performed_date?: string;
  result?: 'pass' | 'pass_with_adjustment' | 'fail';
  certificate_reference?: string | null;
  performed_by?: string | null;
  cost_paise?: number | null;
  notes?: string | null;
}

export function recordCompletion(input: CompletionInput): Promise<Schedule> {
  return post<Schedule>('/schedules/complete', input);
}

export interface ServiceCallInput {
  asset_id?: number;
  asset_tag?: string;
  fault_description: string;
  severity?: 'breakdown' | 'degraded' | 'preventive';
  reported_at?: string;
  reported_by?: string | null;
  ward?: string | null;
  took_out_of_service?: boolean;
  down_from?: string | null;
  call_number?: string | null;
}

export function openServiceCall(input: ServiceCallInput): Promise<ServiceCall> {
  return post<ServiceCall>('/service-calls', input);
}

export function closeServiceCall(callId: number, input: {
  restored_at?: string; resolution?: string; attended_by?: string;
  cost_paise?: number | null; return_to_service?: boolean;
} = {}): Promise<ServiceCall> {
  return post<ServiceCall>(`/service-calls/${callId}/close`, input);
}

export interface ContractInput {
  contract_number: string;
  contract_type?: 'amc' | 'cmc' | 'warranty';
  start_date: string;
  end_date: string;
  vendor_name?: string | null;
  value_paise?: number | null;
  notice_days?: number;
  covers_calibration?: boolean;
  covers_parts?: boolean;
  response_hours?: number | null;
  status?: string;
  scope_notes?: string | null;
  asset_ids?: number[];
}

export function saveContract(input: ContractInput): Promise<Contract> {
  return post<Contract>('/contracts', input);
}

/* ── Display helpers ──────────────────────────────────────────────────────── */

/**
 * How a lapse reads on screen.
 *
 * `days_overdue` is 0 ON the first overdue day — the effective date and today
 * are the same day, so nothing has elapsed. "Overdue by 0 days" reads like a
 * rounding error and gets skipped, which is the worst possible outcome for the
 * one row that most needs reading, so day zero says "Overdue today".
 */
export function overdueLabel(schedule: Pick<Schedule, 'days_overdue' | 'is_overdue'>): string {
  if (!schedule.is_overdue) return '';
  if (schedule.days_overdue <= 0) return 'Overdue today';
  if (schedule.days_overdue === 1) return 'Overdue by 1 day';
  return `Overdue by ${schedule.days_overdue} days`;
}

/** How long until it falls due. Never used for an overdue row. */
export function dueLabel(schedule: Pick<Schedule, 'days_until_due' | 'is_overdue'>): string {
  if (schedule.is_overdue) return '';
  const days = schedule.days_until_due;
  if (days === null || days === undefined) return 'No due date';
  if (days === 0) return 'Due today';
  if (days === 1) return 'Due tomorrow';
  return `Due in ${days} days`;
}

/** Minutes → a duration a person reads. 0 is "None", not "0m". */
export function formatDowntime(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return '—';
  if (minutes <= 0) return 'None';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  return `${mins}m`;
}

/** A service call's downtime as a phrase. A call opened moments ago has 0
 *  minutes on the clock, and "Down for None and counting" read as a glitch. */
export function downtimeText(minutes: number | null | undefined, running: boolean): string {
  if (running) {
    const ongoing = minutes !== null && minutes !== undefined && minutes >= 1
      ? formatDowntime(minutes) : 'under a minute';
    return `Down for ${ongoing} and counting`;
  }
  return `Downtime ${formatDowntime(minutes)}`;
}

/** The tone a compliance state renders in. One mapping, so the register, the
 *  ward view and the asset drawer cannot colour the same state differently. */
export function complianceTone(compliance: Compliance): 'danger' | 'warn' | 'ok' | 'neutral' {
  switch (compliance) {
    case 'overdue': return 'danger';
    case 'due_soon': return 'warn';
    case 'ok': return 'ok';
    default: return 'neutral';
  }
}

export function coverTone(state: CoverState): 'danger' | 'warn' | 'ok' | 'neutral' {
  switch (state) {
    case 'expired': return 'danger';
    case 'expiring': return 'warn';
    case 'active': return 'ok';
    default: return 'neutral';
  }
}
