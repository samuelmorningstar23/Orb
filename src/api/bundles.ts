/**
 * Orb Hospital OS — Care Bundle (Sepsis Six / deterioration) API
 *
 * Typed client for the /api/bundles routes (server/routes/bundle_routes.py).
 * Uses the shared apiFetch wrapper so it inherits centralized auth headers and
 * 401/403 handling — this file must NOT reach into client.ts internals.
 */

import { apiFetch } from './client';

/** Stable set of Sepsis Six element keys, in clinical display order. */
export type BundleElementKey =
  | 'oxygen'
  | 'cultures'
  | 'antibiotics'
  | 'fluids'
  | 'lactate'
  | 'urine_output';

/** Per-element status computed server-side in _decorate(). */
export type BundleElementState =
  | 'pending'
  | 'overdue'
  | 'done_on_time'
  | 'done_late';

export interface BundleElement {
  key: BundleElementKey;
  label: string;
  /** Minutes from trigger the element is meant to be completed within. */
  target_min: number;
  /** ISO timestamp the element was completed, or null if outstanding. */
  completed_at: string | null;
  state: BundleElementState;
}

export type BundleStatus = 'active' | 'completed' | 'cancelled';

/** A decorated care-bundle row as returned by the backend. */
export interface CareBundle {
  id: number;
  patient_id: string;
  bundle_type: string;
  triggered_at: string;
  trigger_reason: string | null;
  /** users.id of the clinician who opened it, or "vigil_auto". */
  triggered_by?: string | null;
  /** "clinician" or "vigil_auto". */
  source?: string | null;
  /** Who opened the bundle, resolved at read time: the clinician's name, or
   *  "Orb" for an automatic start. Null when the account is not on file. */
  started_by_name?: string | null;
  /** The NEWS2 that opened an automatic bundle, else null. */
  trigger_news2?: number | null;
  status: BundleStatus;
  /** Minutes elapsed since triggered_at (server-computed, snapshot). */
  elapsed_min: number;
  elements: BundleElement[];
  completed_count: number;
  total_count: number;
  /** % of elements done (regardless of timing). */
  completion_pct: number;
  /** Headline metric: % of elements done WITHIN target. */
  compliance_pct: number;
  /** True when no element has breached its target (nothing overdue/late). */
  on_track: boolean;
}

/** Response envelope for the list endpoints. */
interface BundleListResponse {
  bundles: CareBundle[];
}

/**
 * GET /api/bundles/patient/{patientId}?active_only=
 * List a patient's care bundles (newest first). Pass activeOnly to restrict to
 * the single open bundle.
 */
export async function getPatientBundles(
  patientId: string,
  activeOnly: boolean = false,
): Promise<CareBundle[]> {
  const qs = activeOnly ? '?active_only=true' : '';
  const data = await apiFetch<BundleListResponse>(
    `/bundles/patient/${encodeURIComponent(patientId)}${qs}`,
  );
  // Tolerate a malformed/empty payload: the widget reads bundles[0], so an
  // undefined here would crash it into an error card instead of its empty state.
  return data?.bundles ?? [];
}

/**
 * POST /api/bundles/patient/{patientId}/start
 * Open a Sepsis Six bundle for a patient. Idempotent server-side — returns the
 * existing active bundle if one is already open. Returns the decorated bundle.
 */
export async function startBundle(
  patientId: string,
  reason?: string,
): Promise<CareBundle> {
  return apiFetch<CareBundle>(
    `/bundles/patient/${encodeURIComponent(patientId)}/start`,
    {
      method: 'POST',
      body: JSON.stringify({ reason }),
    },
  );
}

/**
 * POST /api/bundles/{bundleId}/complete/{element}
 * Stamp a bundle element complete. Auto-completes the bundle when the last
 * element is done. Returns the updated decorated bundle.
 */
export async function completeBundleElement(
  bundleId: number,
  element: BundleElementKey,
): Promise<CareBundle> {
  return apiFetch<CareBundle>(
    `/bundles/${bundleId}/complete/${element}`,
    { method: 'POST' },
  );
}

/**
 * POST /api/bundles/{bundleId}/cancel
 * Cancel an active bundle. The reason is mandatory (422 without one) and is
 * written to the audit log with the caller's name. 404 when the bundle is not
 * on the caller's ward, 409 when it is no longer active.
 */
export async function cancelBundle(
  bundleId: number,
  reason: string,
): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>(`/bundles/${bundleId}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}
