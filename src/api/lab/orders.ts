/**
 * Orb, Laboratory orders API (lis-7).
 *
 * An order the laboratory has taken in is the laboratory's for life: the
 * Orders screen's own status buttons and free-text result answer 409
 * `order_managed_by_lab` for it, whatever the module switch says now. Such an
 * order is cancelled only here, with a reason, which cancels every test not
 * yet reported, withdraws its unreleased results and keeps the reported ones.
 *
 * The Orders list (GET /api/orders/patient/<id>) carries the laboratory's view
 * of each order in the shapes below.
 */
import { labPost } from './index'

/** One test of a laboratory-managed order, its status in the ward's words. */
export interface LabOrderTest {
  test_id: string
  name: string
  short_name: string | null
  status: string
  status_text: string
  specimen_id: string | null
}

/** The laboratory's view of an order it manages. */
export interface LabOrderSummary {
  tests: LabOrderTest[]
  counts: Record<string, number>
  /** "5 tests: 1 reported, 4 waiting for collection" */
  summary: string
  rejected: number
  /** Set when every test was cancelled. `at` is a bare UTC timestamp. */
  cancelled: { by_name: string | null; at: string | null; reason: string | null } | null
}

/** Why the laboratory has not taken a laboratory order in. */
export interface LabIntakeNotice {
  kind: 'unmapped_code' | 'intake_failed'
  message: string
}

/** The fields the Orders list adds to every order. */
export interface LabOrderFields {
  lab_managed?: boolean
  lab?: LabOrderSummary | null
  lab_intake?: LabIntakeNotice | null
}

export interface CancelLabOrderResult {
  order: { id: string; status: string; [key: string]: unknown }
  already_cancelled: boolean
  cancelled_items: number
  withdrawn_results: number
  kept_released: number
  cancelled_specimens: string[]
}

/** Cancel an order the laboratory manages. A reason is required. */
export function cancelLabOrder(orderId: string, reason: string): Promise<CancelLabOrderResult> {
  return labPost<CancelLabOrderResult>(`/orders/${encodeURIComponent(orderId)}/cancel`, { reason })
}

/** The code the Orders routes answer for a manual move on a managed order. */
export const ORDER_MANAGED_BY_LAB = 'order_managed_by_lab'

