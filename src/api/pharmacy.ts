/**
 * Orb Hospital OS — Pharmacist Verification API
 *
 * The closed loop for AI/agentic medications: list meds charted with provenance
 * llm_extracted/agentic that no pharmacist has verified yet, then verify (with an
 * optional dose correction) or reject them. Each response carries the re-run
 * drug-safety screen so the pharmacist sees allergy/interaction alerts.
 */
import { apiFetch, buildHeaders } from './client';

export interface SafetyAlert {
  type: 'allergy' | 'interaction';
  severity: 'mild' | 'moderate' | 'severe';
  message: string;
  substance?: string;
  reaction?: string;
  with?: string;
  match?: string;
}

/** The re-run drug-safety screen attached to verify/reject responses and to the
 *  428 severe-ack payload. `screen_failed` = the screen itself crashed (treated
 *  as unsafe, never as "no alerts"); `interaction_screened` = false when the
 *  drug is outside the interaction knowledge base (coverage_note explains). */
export interface SafetyScreen {
  max_severity: string;
  blocking: boolean;
  safe: boolean;
  alerts: SafetyAlert[];
  interaction_screened?: boolean;
  coverage_note?: string | null;
  screen_failed?: boolean;
  override?: { by: string; reason: string };
  /** The permission that clears this block, named by the server: set for a
   *  severe allergy, which only a prescriber may override. */
  override_requires_permission?: string | null;
  /** Why the server refused the reason on the last attempt, in its words. */
  ack_refused?: string | null;
}

export interface PendingMedication {
  id: number;
  patient_id: string;
  patient: string | null;
  drug_name: string;
  dose: string;
  route: string;
  frequency: string;
  source: string;
  status: string;
  safety_alerts: SafetyAlert[];
  max_severity: string;
  interaction_screened: boolean;
  coverage_note: string | null;
  screen_failed: boolean;
}

export interface VerifiedMedication {
  id: number;
  patient_id: string;
  patient: string | null;
  drug_name: string;
  dose: string;
  route: string;
  frequency: string;
  source: string;
  status: string;
  verified_by: string | null;
  verified_at: string | null;
  notes: string | null;
  /** Who ordered it and who checked it, by name, read from users now. */
  ordered_by_name?: string | null;
  verified_by_name?: string | null;
}

export interface VerifyResult {
  status: 'verified';
  medication: VerifiedMedication;
  safety_alerts: SafetyAlert[];
  safety: SafetyScreen;
}

export interface RejectResult {
  status: 'rejected';
  medication: VerifiedMedication;
  reason: string;
  safety_alerts: SafetyAlert[];
  safety: SafetyScreen;
}

/** Typed 428 outcome of a verify attempt: the re-run screen found severe alerts
 *  (or failed outright) and the pharmacist must re-submit with an ackReason. */
export interface SevereAckRequired {
  code: 'severe_ack_required';
  message: string;
  safety: SafetyScreen | null;
}

/** List all AI/agentic meds awaiting pharmacist verification. */
export async function getPendingVerifications(): Promise<PendingMedication[]> {
  const data = await apiFetch<{ pending: PendingMedication[] }>('/pharmacy/pending');
  return data.pending;
}

/** Verify a med, optionally correcting its dose/route/frequency → active order.
 *
 * A severe (or failed) safety screen makes the server answer 428; this is
 * returned as a typed `SevereAckRequired` result — not thrown — so the caller
 * can open its acknowledge flow and re-submit with `ackReason`.
 *
 * Uses a raw fetch (not apiFetch) because the shared wrapper flattens error
 * bodies to a message string, which would drop the 428 safety payload.
 */
export async function verifyMedication(
  medId: number,
  corrections: { dosage?: string; route?: string; frequency?: string } = {},
  ackReason?: string,
): Promise<VerifyResult | SevereAckRequired> {
  const response = await fetch(`${window.location.origin}/api/pharmacy/${medId}/verify`, {
    method: 'POST',
    // Cookie session + CSRF token (buildHeaders adds X-CSRF-Token for POST).
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify({ ...corrections, ack_reason: ackReason || undefined }),
  });

  if (response.status === 428) {
    const body = await response.json().catch(() => ({ detail: {} }));
    const detail = body && typeof body.detail === 'object' && body.detail !== null ? body.detail : {};
    return {
      code: 'severe_ack_required',
      message: detail.message || 'Severe safety alerts must be acknowledged with a reason.',
      safety: (detail.safety as SafetyScreen) ?? null,
    };
  }

  if (!response.ok) {
    if (response.status === 401) {
      // Same session-expiry contract as apiFetch (App.tsx renders a re-auth modal).
      window.dispatchEvent(new CustomEvent('orb:session-expired'));
      throw new Error('Session expired');
    }
    const body = await response.json().catch(() => ({ detail: 'Unknown error' }));
    const detail = body?.detail;
    throw new Error(
      typeof detail === 'string' ? detail : detail?.message || `API error: ${response.status}`,
    );
  }

  return response.json();
}

/** Reject a med → discontinued, with the reason recorded. */
export async function rejectMedication(medId: number, reason: string): Promise<RejectResult> {
  return apiFetch<RejectResult>(`/pharmacy/${medId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}
