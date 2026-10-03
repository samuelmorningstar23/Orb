/**
 * Orb Hospital OS — Procurement & non-pharmacy stores API client.
 *
 * Vendors, the non-pharmacy item master, indent → purchase order → goods
 * receipt, stock on hand, reorder points, consumption and expiry.
 *
 * WHY THIS FILE USES A RAW `fetch` INSTEAD OF `apiFetch`.
 * The shared wrapper flattens an error body down to a message string. Every
 * refusal this module can produce is one the UI has to react to DIFFERENTLY —
 * `module_disabled` is a whole-panel state, `over_receipt` belongs next to the
 * quantity field, `pharmacy_item_refused` needs the sentence about Helix, and
 * `insufficient_stock` needs the on-hand figure — so the machine-readable
 * `error` code has to survive the trip. `request()` below keeps it on a typed
 * `ProcurementError`, and still sends the session cookie and CSRF token exactly
 * the way `apiFetch` does.
 *
 * QUANTITIES ARE INTEGER MILLI (thousandths of a unit) on the wire, the same as
 * money is integer paise: `10 boxes` is `10000`. Convert at the input boundary
 * with `unitsToMilli`, never with a float multiply on a parsed string.
 */
import { buildHeaders, API_BASE, errorDetailMessage } from './client';

/** 1 unit = 1000 milli. Mirrors QUANTITY_SCALE in services/money.py. */
export const QUANTITY_SCALE = 1000;

/** A refusal from the procurement API, with its machine-readable code intact. */
export class ProcurementApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = 'ProcurementApiError';
    this.status = status;
    this.code = code;
  }

  /** True when this deployment has switched the whole module off. */
  get isModuleDisabled(): boolean {
    return this.code === 'module_disabled';
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/procurement${path}`, {
    ...options,
    credentials: 'include',
    headers: buildHeaders((options.method as string) || 'GET'),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body && typeof body.detail === 'object' ? body.detail : null;
    throw new ProcurementApiError(
      errorDetailMessage(body?.detail, response.status),
      response.status,
      detail?.error || `http_${response.status}`,
    );
  }
  return response.json() as Promise<T>;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  });
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

/* ── Quantities ───────────────────────────────────────────────────────────── */

/** Whole units → milli. Rejects anything that is not a finite integer count,
 *  because a fractional pack typed into a box is a data-entry error, not a
 *  quantity to round. */
export function unitsToMilli(units: number): number {
  if (!Number.isFinite(units) || !Number.isInteger(units)) {
    throw new Error(`quantity must be a whole number of units (got ${units})`);
  }
  return units * QUANTITY_SCALE;
}

const INDIAN_GROUPING = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });

/** Milli → a display string. `null` is "—", never "0". Grouped the Indian way
 *  (1,00,000), the same as the server's format_quantity and as money. */
export function formatQuantity(milli: number | null | undefined, uom?: string | null): string {
  if (milli === null || milli === undefined) return '—';
  const negative = milli < 0;
  const abs = Math.abs(milli);
  const whole = INDIAN_GROUPING.format(Math.trunc(abs / QUANTITY_SCALE));
  const frac = abs % QUANTITY_SCALE;
  let body = frac === 0
    ? whole
    : `${whole}.${String(frac).padStart(3, '0')}`.replace(/0+$/, '');
  if (negative) body = `-${body}`;
  return uom ? `${body} ${uom}` : body;
}

/* ── Types ────────────────────────────────────────────────────────────────── */

export type GstTreatment = 'exempt' | 'taxable' | 'nil_rated' | 'zero_rated';

export interface Vendor {
  id: number;
  code: string;
  name: string;
  vendor_type: string;
  gstin: string | null;
  state_code: string | null;
  contact_name: string | null;
  contact_phone: string | null;
  payment_terms_days: number | null;
  rating: number | null;
  is_blacklisted: number;
  blacklist_reason: string | null;
  is_active: number;
}

export interface Store {
  id: number;
  code: string;
  name: string;
  store_type: string;
  department: string | null;
  ward: string | null;
  is_active: number;
}

export interface InventoryItem {
  id: number;
  code: string;
  name: string;
  category: string;
  uom: string;
  pack_size: number;
  hsn_sac: string | null;
  gst_treatment: GstTreatment;
  gst_rate_bp: number;
  /** null when nobody has ever received this item — NOT zero. */
  last_purchase_rate_paise: number | null;
  rate_is_configured: boolean;
  /** Always safe to render: an amount or the words "Not configured". */
  last_purchase_rate_display: string;
  tracks_expiry: boolean;
  reorder_level_milli: number | null;
  reorder_qty_milli: number | null;
  is_active: number;
}

export interface IndentLine {
  id: number;
  line_no: number;
  item_code: string;
  description: string;
  uom: string | null;
  quantity_milli: number;
  approved_quantity_milli: number | null;
  /** What a PO may be raised for. `null` until somebody has actually decided. */
  effective_quantity_milli: number | null;
  quantity_display: string;
  approved_quantity_display: string;
  status: string;
  remarks: string | null;
}

export interface Indent {
  id: number;
  indent_number: string | null;
  store_code: string | null;
  store_name: string | null;
  department: string | null;
  ward: string | null;
  priority: string;
  status: string;
  required_by: string | null;
  requested_by: string | null;
  requested_at: string;
  approved_by: string | null;
  rejected_reason: string | null;
  remarks: string | null;
  lines: IndentLine[];
  legal_transitions: string[];
}

export interface PurchaseOrderLine {
  id: number;
  line_no: number;
  item_code: string;
  description: string;
  hsn_sac: string | null;
  uom: string | null;
  quantity_milli: number;
  received_quantity_milli: number;
  outstanding_quantity_milli: number;
  quantity_display: string;
  received_quantity_display: string;
  outstanding_quantity_display: string;
  unit_rate_paise: number;
  gross_paise: number;
  discount_paise: number;
  gst_treatment: GstTreatment;
  gst_rate_bp: number;
  taxable_value_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  igst_paise: number;
  tax_paise: number;
  total_paise: number;
  status: string;
}

export interface PurchaseOrder {
  id: number;
  po_number: string | null;
  vendor_code: string | null;
  vendor_name: string | null;
  vendor_state_code: string | null;
  store_code: string | null;
  status: string;
  order_date: string | null;
  expected_date: string | null;
  is_interstate: number;
  subtotal_paise: number;
  discount_paise: number;
  taxable_value_paise: number;
  cgst_paise: number;
  sgst_paise: number;
  igst_paise: number;
  tax_paise: number;
  other_charges_paise: number;
  rounding_adjustment_paise: number;
  total_paise: number;
  outstanding_quantity_milli: number;
  is_fully_received: boolean;
  can_receive: boolean;
  legal_transitions: string[];
  lines: PurchaseOrderLine[];
}

export interface GoodsReceiptLine {
  id: number;
  line_no: number;
  item_code: string;
  description: string;
  batch_no: string | null;
  expiry_date: string | null;
  uom: string | null;
  accepted_quantity_milli: number;
  rejected_quantity_milli: number;
  accepted_quantity_display: string;
  rejected_quantity_display: string;
  rejection_reason: string | null;
  unit_rate_paise: number;
  total_paise: number;
}

export interface GoodsReceipt {
  id: number;
  grn_number: string | null;
  purchase_order_id: number | null;
  po_number: string | null;
  vendor_name: string | null;
  store_code: string | null;
  status: string;
  qc_status: string;
  total_paise: number;
  received_at: string;
  posted_at: string | null;
  lines: GoodsReceiptLine[];
  legal_transitions: string[];
  /** True when this response replayed an existing receipt instead of creating
   *  one — the idempotency key had already been used. */
  idempotent_replay?: boolean;
}

export interface StockRow {
  item_code: string;
  item_name: string;
  category: string;
  uom: string;
  store_code: string | null;
  store_name: string | null;
  on_hand_milli: number;
  on_hand_display: string;
  reorder_level_milli: number | null;
  reorder_level_display: string;
  reorder_qty_milli: number | null;
  is_low: boolean;
  last_purchase_rate_paise: number | null;
  last_purchase_rate_display: string;
  value_paise: number | null;
  value_display: string;
  last_movement_at: string | null;
}

export interface LowStockRow extends StockRow {
  shortfall_milli: number;
  shortfall_display: string;
  suggested_order_milli: number | null;
  suggested_order_display: string;
}

export interface ExpiryRow {
  item_code: string;
  item_name: string | null;
  category: string | null;
  uom: string | null;
  store_code: string | null;
  store_name: string | null;
  batch_no: string;
  expiry_date: string;
  days_to_expiry: number;
  is_expired: boolean;
  balance_milli: number;
  balance_display: string;
  value_at_risk_paise: number | null;
  value_at_risk_display: string;
}

export interface StockMovement {
  id: number;
  item_code: string;
  store_code: string | null;
  batch_no: string | null;
  expiry_date: string | null;
  movement_type: string;
  quantity_milli: number;
  quantity_display: string;
  balance_after_milli: number | null;
  value_paise: number | null;
  value_display: string;
  reference_type: string | null;
  reference_id: string | null;
  moved_by: string | null;
  moved_at: string;
  remarks: string | null;
}

export interface ConsumptionLine {
  department: string;
  store_code: string | null;
  item_code: string;
  item_name: string | null;
  category: string | null;
  uom: string | null;
  quantity_milli: number;
  quantity_display: string;
  value_paise: number;
  value_display: string;
  unvalued_movement_count: number;
  movement_count: number;
}

export interface ConsumptionReport {
  date_from: string | null;
  date_to: string | null;
  lines: ConsumptionLine[];
  departments: Array<{
    department: string;
    value_paise: number;
    value_display: string;
    line_count: number;
    unvalued_movement_count: number;
  }>;
  total_value_paise: number;
  total_value_display: string;
  unvalued_movement_count: number;
  /** False when some movement could not be valued. The UI must NOT present the
   *  total as complete when this is false. */
  value_is_complete: boolean;
}

export interface ProcurementOverview {
  low_stock: LowStockRow[];
  low_stock_count: number;
  expiring: ExpiryRow[];
  expiring_count: number;
  expired_count: number;
  open_purchase_orders: PurchaseOrder[];
  open_purchase_order_count: number;
  open_purchase_order_value_paise: number;
  open_purchase_order_value_display: string;
  pending_indent_count: number;
  draft_purchase_order_count: number;
  active_item_count: number;
  active_vendor_count: number;
  pharmacy_boundary_note: string;
}

export interface ProcurementMeta {
  item_categories: string[];
  expiry_tracked_categories: string[];
  vendor_types: string[];
  store_types: string[];
  movement_types: { inward: string[]; outward: string[]; consumption: string[] };
  transitions: Record<string, Record<string, string[]>>;
  receivable_po_statuses: string[];
  quantity_scale: number;
  pharmacy_boundary: { owner: string; message: string };
}

/* ── Calls ────────────────────────────────────────────────────────────────── */

export const getOverview = (storeCode?: string) =>
  request<ProcurementOverview>(`/overview${query({ store_code: storeCode })}`);

export const getMeta = () => request<ProcurementMeta>('/meta');

export const getVendors = (opts: { search?: string; includeBlacklisted?: boolean } = {}) =>
  request<{ vendors: Vendor[] }>(
    `/vendors${query({ search: opts.search, include_blacklisted: opts.includeBlacklisted })}`,
  ).then((r) => r.vendors);

/** `create_only` is set by the Add forms: a code already in the master is
 *  refused (409, code 'already_exists') instead of being overwritten. */
export const saveVendor = (
  vendor: Partial<Vendor> & { code: string; name: string; create_only?: boolean },
) => post<Vendor>('/vendors', vendor);

export const setVendorBlacklist = (code: string, blacklisted: boolean, reason?: string) =>
  post<Vendor>(`/vendors/${encodeURIComponent(code)}/blacklist`, { blacklisted, reason });

export const getStores = () =>
  request<{ stores: Store[] }>('/stores').then((r) => r.stores);

export const saveStore = (store: Partial<Store> & { code: string; name: string }) =>
  post<Store>('/stores', store);

export const getItems = (opts: { category?: string; search?: string } = {}) =>
  request<{ items: InventoryItem[] }>(
    `/items${query({ category: opts.category, search: opts.search })}`,
  ).then((r) => r.items);

export const saveItem = (item: Record<string, unknown> & { code: string; name: string }) =>
  post<InventoryItem>('/items', item);

export const getIndents = (opts: { status?: string; storeCode?: string } = {}) =>
  request<{ indents: Indent[] }>(
    `/indents${query({ status: opts.status, store_code: opts.storeCode })}`,
  ).then((r) => r.indents);

export const getIndent = (id: number) => request<Indent>(`/indents/${id}`);

export const createIndent = (payload: {
  store_code?: string;
  department?: string;
  ward?: string;
  priority?: string;
  required_by?: string;
  remarks?: string;
  submit?: boolean;
  lines: Array<{ item_code: string; quantity_milli: number; remarks?: string }>;
}) => post<Indent>('/indents', payload);

export const submitIndent = (id: number) => post<Indent>(`/indents/${id}/submit`);

export const decideIndent = (
  id: number,
  payload: { approvals?: Record<number, number>; reject?: boolean; reason?: string },
) => post<Indent>(`/indents/${id}/decide`, payload);

export const cancelIndent = (id: number, reason: string) =>
  post<Indent>(`/indents/${id}/cancel`, { reason });

export const getPurchaseOrders = (opts: { status?: string; openOnly?: boolean } = {}) =>
  request<{ purchase_orders: PurchaseOrder[] }>(
    `/purchase-orders${query({ status: opts.status, open_only: opts.openOnly })}`,
  ).then((r) => r.purchase_orders);

export const getPurchaseOrder = (id: number) => request<PurchaseOrder>(`/purchase-orders/${id}`);

export const createPurchaseOrder = (payload: {
  vendor_code: string;
  store_code?: string;
  indent_id?: number;
  expected_date?: string;
  is_interstate?: boolean;
  other_charges_paise?: number;
  terms?: string;
  lines: Array<{ item_code: string; quantity_milli: number; unit_rate_paise: number }>;
}) => post<PurchaseOrder>('/purchase-orders', payload);

export const submitPurchaseOrder = (id: number) =>
  post<PurchaseOrder>(`/purchase-orders/${id}/submit`);
export const approvePurchaseOrder = (id: number) =>
  post<PurchaseOrder>(`/purchase-orders/${id}/approve`);
export const issuePurchaseOrder = (id: number) =>
  post<PurchaseOrder>(`/purchase-orders/${id}/issue`);
export const cancelPurchaseOrder = (id: number, reason: string) =>
  post<PurchaseOrder>(`/purchase-orders/${id}/cancel`, { reason });

export const getGoodsReceipts = (purchaseOrderId?: number) =>
  request<{ goods_receipts: GoodsReceipt[] }>(
    `/goods-receipts${query({ purchase_order_id: purchaseOrderId })}`,
  ).then((r) => r.goods_receipts);

export const createGoodsReceipt = (payload: {
  purchase_order_id: number;
  store_code?: string;
  vendor_invoice_number?: string;
  vendor_invoice_date?: string;
  qc_status?: string;
  idempotency_key?: string;
  post?: boolean;
  lines: Array<{
    po_line_no: number;
    accepted_quantity_milli: number;
    rejected_quantity_milli?: number;
    batch_no?: string;
    expiry_date?: string;
    rejection_reason?: string;
  }>;
}) => post<GoodsReceipt>('/goods-receipts', payload);

export const getStock = (opts: {
  storeCode?: string;
  category?: string;
  lowOnly?: boolean;
  includeZero?: boolean;
} = {}) =>
  request<{ stock: StockRow[] }>(
    `/stock${query({
      store_code: opts.storeCode, category: opts.category,
      low_only: opts.lowOnly, include_zero: opts.includeZero,
    })}`,
  ).then((r) => r.stock);

export const getStockLedger = (itemCode: string, storeCode?: string) =>
  request<{ item_code: string; on_hand: StockRow; movements: StockMovement[] }>(
    `/stock/${encodeURIComponent(itemCode)}/ledger${query({ store_code: storeCode })}`,
  );

export const recordStockMovement = (payload: {
  item_code: string;
  store_code: string;
  movement_type: string;
  quantity_milli: number;
  batch_no?: string;
  department?: string;
  patient_id?: string;
  remarks?: string;
  idempotency_key?: string;
}) => post<StockMovement>('/stock/movements', payload);

export const getLowStock = (storeCode?: string) =>
  request<{ low_stock: LowStockRow[]; count: number }>(
    `/reports/low-stock${query({ store_code: storeCode })}`,
  );

export const getConsumption = (opts: {
  dateFrom?: string; dateTo?: string; department?: string; storeCode?: string;
} = {}) =>
  request<ConsumptionReport>(
    `/reports/consumption${query({
      date_from: opts.dateFrom, date_to: opts.dateTo,
      department: opts.department, store_code: opts.storeCode,
    })}`,
  );

export const getExpiry = (opts: { withinDays?: number; storeCode?: string } = {}) =>
  request<{ expiring: ExpiryRow[]; count: number; expired_count: number; within_days: number }>(
    `/reports/expiry${query({ within_days: opts.withinDays, store_code: opts.storeCode })}`,
  );
