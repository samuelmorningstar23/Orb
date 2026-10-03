/**
 * Orb Hospital OS — Diet & Kitchen API client.
 *
 * Diet orders per admitted patient, the ward view, ward-wise meal plans, the
 * kitchen count and the kitchen indent.
 *
 * WHY THIS FILE USES A RAW `fetch` INSTEAD OF `apiFetch`.
 * The shared wrapper flattens an error body down to a message string, and every
 * refusal this module can produce is one the UI has to react to DIFFERENTLY:
 * `module_disabled` is a whole-panel state, a `conflict` on a locked meal plan
 * belongs beside the Lock button, and an `invalid` NPO window belongs beside the
 * date field. So the machine-readable `error` code has to survive the trip.
 * `request()` below keeps it on a typed `DietaryApiError` and still sends the
 * session cookie and CSRF token exactly the way `apiFetch` does. Same reasoning,
 * and same shape, as src/api/procurement.ts.
 *
 * THERE IS NO MONEY IN THIS MODULE. Not one paise field crosses this boundary,
 * so nothing here goes near src/lib/money.ts. Everything is a whole-number COUNT
 * of trays.
 *
 * ALLERGIES ARRIVE FROM THE SERVER'S LIVE READ OF `patient_allergies` and are
 * never cached or merged client-side. When `allergies_unavailable` is true the
 * list is EMPTY AND MEANINGLESS — render `allergies_message`, never "no known
 * allergies". Those two states look identical and mean opposite things.
 */
import { API_BASE, buildHeaders } from './client';

/* ── Vocabulary (mirrors the server's CHECK constraints) ─────────────────── */

export type DietType =
  | 'normal' | 'diabetic' | 'renal' | 'cardiac' | 'soft' | 'liquid' | 'npo';

export type Texture =
  | 'regular' | 'soft' | 'minced' | 'pureed' | 'liquid' | 'thickened';

export type MealService = 'breakfast' | 'lunch' | 'dinner';

export type PlanStatus = 'draft' | 'locked' | 'cancelled';
export type IndentStatus = 'draft' | 'issued' | 'cancelled';

/** The diet type that means the patient may not eat. Exported as a constant so
 *  no component ever compares against the string literal. */
export const NPO: DietType = 'npo';

/* ── Errors ───────────────────────────────────────────────────────────────── */

/** A refusal from the dietary API, with its machine-readable code intact. */
export class DietaryApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'DietaryApiError';
    this.status = status;
    this.code = code;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled';
  }

  /** True when the caller's screen is simply stale (locked plan, order already
   *  superseded, indent already raised). Worth a reload prompt, not an apology. */
  get isConflict(): boolean {
    return this.code === 'conflict' || this.status === 409;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/dietary${path}`, {
    ...options,
    credentials: 'include',
    headers: buildHeaders((options.method as string) || 'GET'),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === 'object' ? body.detail : null;
    throw new DietaryApiError(
      detail?.message || (typeof body?.detail === 'string' ? body.detail : null) ||
        `Request failed (${response.status})`,
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

export interface Allergy {
  id: number;
  substance: string;
  substance_class: string | null;
  reaction: string | null;
  severity: string | null;
}

export interface DietOrder {
  id: number;
  patient_id: string;
  patient_name: string;
  patient_mrn: string | null;
  admission_id: string | null;
  current_admission_id: string | null;
  ward: string | null;
  bed: string | null;
  department: string | null;
  diet_type: DietType;
  diet_label: string;
  texture: Texture;
  texture_label: string;
  restriction_notes: string | null;
  notes: string | null;
  status: 'active' | 'superseded' | 'discontinued';

  /* NPO — the safety-relevant state. */
  is_npo: boolean;
  npo_label: string | null;
  npo_reason: string | null;
  npo_until: string | null;
  npo_until_iso: string | null;
  npo_hours_remaining: number | null;
  npo_expired: boolean;
  npo_end_unknown: boolean;
  npo_long_window: boolean;
  npo_notice: string | null;
  needs_review: boolean;

  /* Who ordered it. Never blank — falls back to the id, then to "unknown". */
  ordered_by: string;
  ordered_by_name: string | null;
  ordered_by_display: string;
  ordered_at: string | null;
  ordered_at_iso: string | null;

  supersedes_order_id: number | null;
  discontinued_by: string | null;
  discontinued_at: string | null;
  discontinue_reason: string | null;

  /* Live, from patient_allergies. NEVER a copy stored on the order. */
  allergies: Allergy[];
  allergies_unavailable: boolean;
  allergies_message: string | null;
  allergy_source: string;
}

export interface UnorderedPatient {
  patient_id: string;
  patient_name: string;
  patient_mrn: string | null;
  ward: string | null;
  bed: string | null;
  department: string | null;
}

export interface CountLine {
  diet_type: DietType;
  diet_label: string;
  texture: Texture;
  texture_label: string;
  meal_count: number;
}

export interface WardView {
  ward: string | null;
  department: string | null;
  rows: DietOrder[];
  npo_rows: DietOrder[];
  npo_count: number;
  npo_needs_review: DietOrder[];
  npo_needs_review_count: number;
  meal_count: number;
  order_count: number;
  unordered: UnorderedPatient[];
  unordered_count: number;
  counts: CountLine[];
  allergies_unavailable: boolean;
  allergies_message: string | null;
  allergy_source: string;
  npo_label: string;
  npo_expiry_notice: string;
}

export interface WardSummaryRow {
  ward: string;
  admitted: number;
  with_orders: number;
  without_orders: number;
  npo: number;
}

export interface MealPlanLine extends CountLine {
  id: number;
  meal_plan_id: number;
  line_no: number;
}

export interface LiveIndentRef {
  id: number;
  indent_number: string | null;
  status: IndentStatus;
  total_meals: number;
}

export interface MealPlan {
  id: number;
  service_date: string;
  meal_service: MealService;
  meal_service_label: string;
  ward: string;
  status: PlanStatus;
  total_meals: number;
  npo_count: number;
  order_count: number;
  lines: MealPlanLine[];
  lines_total: number;
  /** The server's own check that the header and the lines agree. If this is
   *  ever false the panel says so rather than picking one of the two numbers. */
  totals_agree: boolean;
  generated_by: string | null;
  generated_at: string | null;
  locked_by: string | null;
  locked_at: string | null;
  notes: string | null;
  indent: LiveIndentRef | null;
}

export interface KitchenIndentLine extends CountLine {
  id: number;
  kitchen_indent_id: number;
  line_no: number;
}

export interface KitchenIndent {
  id: number;
  indent_number: string | null;
  meal_plan_id: number;
  service_date: string;
  meal_service: MealService;
  meal_service_label: string;
  ward: string;
  status: IndentStatus;
  total_meals: number;
  lines: KitchenIndentLine[];
  lines_total: number;
  totals_agree: boolean;
  raised_by: string | null;
  raised_at: string | null;
  issued_by: string | null;
  issued_at: string | null;
  cancel_reason: string | null;
  notes: string | null;
  /** False when the site has not configured its own document series — the
   *  number printed is a placeholder and the screen has to say so. */
  number_series_is_configured?: boolean;
}

export interface KitchenWard {
  ward: string;
  lines: CountLine[];
  total_meals: number;
  npo_count: number;
  order_count: number;
}

export interface KitchenBoardPlan {
  id: number;
  ward: string;
  meal_service: MealService;
  meal_service_label: string;
  status: PlanStatus;
  total_meals: number;
  npo_count: number;
  order_count: number;
  lines: MealPlanLine[];
  indent: LiveIndentRef | null;
  /** Live count minus the counted-at-plan-time count. Positive = the ward has
   *  filled since. The number that settles the argument at the hatch. */
  drift: number;
}

export interface KitchenBoard {
  service_date: string;
  meal_services: MealService[];
  wards: KitchenWard[];
  hospital_total_meals: number;
  hospital_npo_count: number;
  plans: KitchenBoardPlan[];
  contains_phi: false;
}

export interface DietarySummary {
  active_orders: number;
  meals: number;
  npo_active: number;
  npo_needs_review: number;
  npo_long_window: number;
  stale_order_count: number;
  admitted_without_order: number;
  ward_count: number;
  npo_label: string;
  npo_expiry_notice: string;
  /** Whose wards these counts cover. The tiles are ward-scoped while the
   *  kitchen board is hospital-wide, so the panel captions them from this. */
  scope?: DietaryScope;
}

export interface DietaryScope {
  department: string | null;
  hospital_wide: boolean;
  /** Wards with somebody admitted under that department right now. */
  wards: string[];
}

/** What the caller may do on this desk, from the same checks the write routes
 *  use. Advisory: the server still refuses whatever it refuses. */
export interface DietaryCapabilities {
  write_orders: boolean;
  run_kitchen: boolean;
}

export interface DietaryMeta {
  /** The hospital's own calendar day, YYYY-MM-DD. */
  service_date?: string;
  /** The service the ward is counting for at this hospital-local hour. */
  current_meal_service?: MealService;
  capabilities?: DietaryCapabilities;
  diet_types: DietType[];
  diet_type_labels: Record<string, string>;
  textures: Texture[];
  texture_labels: Record<string, string>;
  meal_services: MealService[];
  meal_service_labels: Record<string, string>;
  npo_diet_type: DietType;
  npo_label: string;
  npo_expiry_notice: string;
  npo_max_window_hours: number;
  npo_long_window_hours: number;
  allergy_source: string;
  allergy_unavailable_message: string;
  allergies_are_read_not_copied: boolean;
}

export interface DietOrderInput {
  patient_id: string;
  diet_type: DietType;
  texture?: Texture;
  restriction_notes?: string | null;
  notes?: string | null;
  npo_reason?: string | null;
  /** Local datetime string from an <input type="datetime-local">. Sent as-is:
   *  the server reads a naive timestamp as HOSPITAL LOCAL TIME, which is what
   *  the clinician typing "08:00" means. Converting to UTC here would be a
   *  second timezone opinion, and this codebase has already paid for one. */
  npo_until?: string | null;
}

/* ── Calls ────────────────────────────────────────────────────────────────── */

export function getMeta(): Promise<DietaryMeta> {
  return request<DietaryMeta>('/meta');
}

export function getSummary(): Promise<DietarySummary> {
  return request<DietarySummary>('/summary');
}

export function getWards(): Promise<WardSummaryRow[]> {
  return request<{ wards: WardSummaryRow[] }>('/wards').then((r) => r.wards);
}

export function getWardView(ward?: string): Promise<WardView> {
  return request<WardView>(`/ward${query({ ward })}`);
}

export function getStaleOrders(): Promise<DietOrder[]> {
  return request<{ orders: DietOrder[] }>('/stale').then((r) => r.orders);
}

export function getPatientOrder(
  patientId: string,
  history = false,
): Promise<{ order: DietOrder | null; history?: DietOrder[] }> {
  return request(`/orders${query({ patient_id: patientId, history })}`);
}

export function saveDietOrder(input: DietOrderInput): Promise<DietOrder> {
  return post<{ order: DietOrder }>('/orders', input).then((r) => r.order);
}

export function discontinueDietOrder(orderId: number, reason?: string): Promise<DietOrder> {
  return post<{ order: DietOrder }>(`/orders/${orderId}/discontinue`, { reason })
    .then((r) => r.order);
}

export function getMealPlans(params: {
  service_date?: string; ward?: string; meal_service?: MealService;
} = {}): Promise<MealPlan[]> {
  return request<{ plans: MealPlan[] }>(`/meal-plans${query(params)}`).then((r) => r.plans);
}

export function buildMealPlan(input: {
  ward: string; meal_service: MealService; service_date?: string; notes?: string;
}): Promise<MealPlan> {
  return post<{ plan: MealPlan }>('/meal-plans', input).then((r) => r.plan);
}

export function lockMealPlan(planId: number): Promise<MealPlan> {
  return post<{ plan: MealPlan }>(`/meal-plans/${planId}/lock`).then((r) => r.plan);
}

export function getKitchenBoard(params: {
  service_date?: string; meal_service?: MealService;
} = {}): Promise<KitchenBoard> {
  return request<KitchenBoard>(`/kitchen${query(params)}`);
}

export function getIndents(params: {
  service_date?: string; meal_service?: MealService; ward?: string; status?: IndentStatus;
} = {}): Promise<KitchenIndent[]> {
  return request<{ indents: KitchenIndent[] }>(`/indents${query(params)}`)
    .then((r) => r.indents);
}

export function raiseIndent(mealPlanId: number, notes?: string): Promise<KitchenIndent> {
  return post<{ indent: KitchenIndent }>('/indents', { meal_plan_id: mealPlanId, notes })
    .then((r) => r.indent);
}

export function issueIndent(indentId: number): Promise<KitchenIndent> {
  return post<{ indent: KitchenIndent }>(`/indents/${indentId}/issue`).then((r) => r.indent);
}

export function cancelIndent(indentId: number, reason?: string): Promise<KitchenIndent> {
  return post<{ indent: KitchenIndent }>(`/indents/${indentId}/cancel`, { reason })
    .then((r) => r.indent);
}

/* ── Small helpers the panel and its tests share ──────────────────────────── */

/**
 * The words for an NPO window's remaining time.
 *
 * Returns the EXPIRED wording rather than a negative duration, because "-3.2 h"
 * is a number somebody has to interpret under time pressure and "window passed"
 * is not.
 */
export function npoWindowLabel(order: Pick<DietOrder,
  'npo_expired' | 'npo_end_unknown' | 'npo_hours_remaining'>): string {
  if (order.npo_end_unknown) return 'End time unreadable: review now';
  if (order.npo_expired) return 'Window passed: review now';
  const hours = order.npo_hours_remaining;
  if (hours === null || hours === undefined) return 'Ends: unknown';
  if (hours < 1) return `Ends in ${Math.max(1, Math.round(hours * 60))} min`;
  if (hours < 24) return `Ends in ${hours.toFixed(1)} h`;
  return `Ends in ${Math.round(hours / 24)} d`;
}

/**
 * The meal service for a local hour: breakfast until 10:00, lunch until 15:00,
 * dinner after. Mirrors the server's rule in dietary_routes and is used only
 * until /meta answers with the hospital's own `current_meal_service`.
 */
export function mealServiceForHour(hour: number): MealService {
  if (hour < 10) return 'breakfast';
  if (hour < 15) return 'lunch';
  return 'dinner';
}

/**
 * Sum a set of count lines. Used by the panel to render its own total beside the
 * server's, so a disagreement between the two is VISIBLE rather than hidden
 * behind whichever number the screen happened to pick.
 */
export function sumLines(lines: Pick<CountLine, 'meal_count'>[]): number {
  return lines.reduce((total, line) => total + line.meal_count, 0);
}
