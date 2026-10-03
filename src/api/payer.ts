/**
 * Orb — Insurance / TPA desk API client.
 *
 * ORB TRANSMITS NOTHING TO ANY PAYER. Every function whose name contains
 * "record" posts a record of something a human did on the payer's own portal,
 * by e-mail or on paper. There is no integration with any TPA, insurer, scheme
 * or clearing house, and there is no function here that claims otherwise —
 * `recordSubmission`, never `submitClaim`, because a clerk who believes Orb
 * sent the claim does not send the claim, and the hospital eats the bill when
 * the payer's window closes.
 *
 * Money crosses this boundary as INTEGER PAISE and is rendered through
 * src/lib/money.ts (never parseFloat, never toFixed on a rupee value). The
 * server also sends a pre-rendered `*_display` string beside each amount; the
 * panel prefers the server's string for anything that appears next to an
 * amount the server computed, so a client that formatted differently could not
 * draw a screen that disagrees with the record it just created.
 *
 * NOT CONFIGURED IS NOT ZERO. Amounts that nobody has entered arrive as `null`
 * with a display string that says so in words. Never coerce one to 0.
 */
import { apiFetch } from './client';

/* ── Types ─────────────────────────────────────────────────────────────────
 * Money fields are integer paise or null. `*_display` is the server's rendered
 * string, already in Indian grouping with a ₹ symbol, or the words for unset.
 */

export type ClaimStatus =
  | 'draft' | 'preauth_requested' | 'preauth_approved' | 'preauth_denied'
  | 'submitted' | 'queried' | 'approved' | 'partially_approved' | 'denied'
  | 'settled' | 'closed' | 'cancelled' | 'appealed';

export type ClaimType = 'cashless' | 'reimbursement' | 'scheme';

export type PayerType =
  | 'self_pay' | 'tpa' | 'insurer' | 'pmjay' | 'state_scheme' | 'cghs' | 'esic'
  | 'corporate' | 'charity';

export interface Settlement {
  claim_type: ClaimType;
  claimed_paise: number;
  claimed_display: string;
  approved_paise: number | null;
  approved_display: string;
  disallowed_paise: number | null;
  disallowed_display: string;
  copay_percent_bp: number;
  copay_paise: number | null;
  copay_display: string;
  /** null until the payer's decision records it. */
  tds_paise: number | null;
  tds_display: string;
  expected_receipt_paise: number | null;
  expected_receipt_display: string;
  settled_paise: number | null;
  settled_display: string;
  short_settled_paise: number | null;
  short_settled_display: string;
  patient_liability_paise: number | null;
  patient_liability_display: string;
  hospital_receivable_paise: number | null;
  hospital_receivable_display: string;
  is_short_settled: boolean;
  is_over_settled: boolean;
  is_decided: boolean;
  is_received: boolean;
}

export interface ClaimEvent {
  id: number;
  claim_id: number;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  amount_paise: number | null;
  amount_display: string;
  denial_code: string | null;
  denial_reason: string | null;
  remark: string | null;
  document_ref: string | null;
  actor_id: string | null;
  occurred_at: string | null;
  recorded_at: string | null;
}

export interface Claim {
  id: number;
  claim_number: string | null;
  patient_id: string;
  patient_name: string;
  patient_mrn?: string | null;
  patient_ward?: string | null;
  admission_id: string | null;
  invoice_id: number | null;
  payer_id: number;
  payer_code: string | null;
  payer_name: string | null;
  payer_type: PayerType | null;
  claim_submission_mode: string | null;
  portal_url: string | null;
  policy_id: number | null;
  policy_number: string | null;
  claim_type: ClaimType;
  package_code: string | null;
  preauth_number: string | null;
  preauth_requested_paise: number | null;
  preauth_approved_paise: number | null;
  preauth_status: string | null;
  claimed_paise: number;
  claimed_display: string;
  approved_paise: number | null;
  disallowed_paise: number | null;
  tds_paise: number | null;
  settled_paise: number | null;
  patient_liability_paise: number | null;
  status: ClaimStatus;
  external_ref: string | null;
  submitted_at: string | null;
  decided_at: string | null;
  settled_at: string | null;
  created_at: string | null;
  notes: string | null;
  settlement: Settlement;
  /** Whether the STORED decision-time liability still matches a live
   *  recomputation. False means somebody edited the policy after the decision;
   *  the stored figure is the record and the screen must say so. */
  settlement_matches_record: boolean;
  stored_patient_liability_paise: number | null;
  age_days: number | null;
  age_bucket: string;
  aging_anchor: string | null;
  aging_anchor_source: string;
  aging_anchor_label: string;
  is_stale: boolean;
  is_open: boolean;
  legal_next_statuses: ClaimStatus[];
  hospital_receivable_applies: boolean;
  transmission_notice: string;
  number_is_configured?: boolean;
  events?: ClaimEvent[];
}

export interface Payer {
  id: number;
  code: string;
  name: string;
  payer_type: PayerType;
  tariff_plan_id: number | null;
  tariff_plan_code: string | null;
  claim_submission_mode: string | null;
  portal_url: string | null;
  credit_days: number | null;
  credit_days_display: string;
  preauth_required: boolean;
  is_active: boolean;
  claimable: boolean;
  contact_name: string | null;
  contact_phone: string | null;
  contact_email: string | null;
  notes: string | null;
}

export interface Policy {
  id: number;
  patient_id: string;
  /** Resolved from the patient register, as on a claim. null when the id
   *  names no registered patient. */
  patient_name?: string | null;
  patient_mrn?: string | null;
  admission_id: string | null;
  payer_id: number;
  payer_code: string | null;
  payer_name: string | null;
  payer_type: PayerType | null;
  policy_number: string | null;
  member_id: string | null;
  scheme_beneficiary_id: string | null;
  subscriber_name: string | null;
  valid_from: string | null;
  valid_to: string | null;
  /** null when nobody has entered it. NOT zero cover. */
  sum_insured_paise: number | null;
  sum_insured_is_configured: boolean;
  sum_insured_display: string;
  copay_percent_bp: number;
  copay_display: string;
  preauth_required: boolean;
  is_primary: boolean;
  status: string;
  verified_by: string | null;
  verified_at: string | null;
  claimable: boolean;
}

export interface AgingBucket {
  bucket: string;
  count: number;
  claimed_paise: number;
  claimed_display: string;
}

interface Wrapped {
  notice: string;
  notice_short: string;
  transmits_to_payer: false;
}

export interface WorkQueue extends Wrapped {
  generated_at: string;
  total: number;
  buckets: AgingBucket[];
  stale_after_days: number;
  stale_count: number;
  awaiting_payer: number;
  awaiting_hospital: number;
  claims: Claim[];
}

export interface DeskSummary extends Wrapped {
  open_claims: number;
  stale_claims: number;
  denied_open: number;
  awaiting_preauth: number;
  confirmed_receivable_paise: number;
  confirmed_receivable_display: string;
  estimated_receivable_paise: number;
  estimated_receivable_display: string;
  short_settled_count: number;
  short_settled_paise: number;
  short_settled_display: string;
}

export interface DenialRow extends ClaimEvent {
  claim_number: string | null;
  patient_id: string;
  patient_name: string;
  claim_status: ClaimStatus;
  payer_code: string | null;
  payer_name: string | null;
  denial_code_label: string;
  amount_at_stake_paise: number;
  amount_at_stake_display: string;
}

export interface DenialLog extends Wrapped {
  codes_are_local: string;
  total: number;
  by_code: { denial_code: string; label: string; count: number; amount_paise: number; amount_display: string }[];
  denials: DenialRow[];
}

export interface PayerMeta extends Wrapped {
  payer_types: PayerType[];
  claim_types: ClaimType[];
  claim_statuses: ClaimStatus[];
  legal_transitions: Record<string, ClaimStatus[]>;
  aging_buckets: string[];
  denial_reason_codes: { code: string; label: string }[];
  denial_codes_are_local: boolean;
}

/* ── Reads ────────────────────────────────────────────────────────────────── */

export const getMeta = () => apiFetch<PayerMeta>('/payer/meta');

export const getSummary = () => apiFetch<DeskSummary>('/payer/summary');

export const getWorkQueue = (params: { payerCode?: string; bucket?: string } = {}) => {
  const q = new URLSearchParams();
  if (params.payerCode) q.set('payer_code', params.payerCode);
  if (params.bucket) q.set('bucket', params.bucket);
  const qs = q.toString();
  return apiFetch<WorkQueue>(`/payer/work-queue${qs ? `?${qs}` : ''}`);
};

export const getPayers = (opts: { includeSelfPay?: boolean } = {}) =>
  apiFetch<Wrapped & { payers: Payer[] }>(
    `/payer/payers?include_self_pay=${opts.includeSelfPay === false ? 'false' : 'true'}`,
  );

export const getClaims = (params: { status?: string; search?: string; openOnly?: boolean } = {}) => {
  const q = new URLSearchParams();
  if (params.status) q.set('status', params.status);
  if (params.search) q.set('search', params.search);
  if (params.openOnly) q.set('open_only', 'true');
  const qs = q.toString();
  return apiFetch<Wrapped & { claims: Claim[]; total: number }>(
    `/payer/claims${qs ? `?${qs}` : ''}`,
  );
};

export const getClaim = (claimId: number) =>
  apiFetch<Wrapped & { claim: Claim }>(`/payer/claims/${claimId}`);

export const getPolicies = (patientId?: string) =>
  apiFetch<Wrapped & { policies: Policy[] }>(
    `/payer/policies${patientId ? `?patient_id=${encodeURIComponent(patientId)}` : ''}`,
  );

export const getDenials = () => apiFetch<DenialLog>('/payer/denials');

/* ── Writes ───────────────────────────────────────────────────────────────── */

const post = <T>(path: string, body: unknown) =>
  apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) });

type ClaimReply = Wrapped & { claim: Claim };

export const savePayer = (body: Partial<Payer> & { code: string; name: string }) =>
  post<Wrapped & { payer: Payer }>('/payer/payers', body);

/**
 * Capture a patient's cover.
 *
 * This is the ONLY payer-capture path in Orb: the clinical admission form
 * records no insurance at all, deliberately, because an admission must never
 * wait on an insurance card. `sum_insured_paise: null` stores "not known" —
 * it must never be sent as 0.
 */
export const capturePolicy = (body: {
  patient_id: string;
  payer_code: string;
  admission_id?: string | null;
  policy_number?: string | null;
  member_id?: string | null;
  scheme_beneficiary_id?: string | null;
  subscriber_name?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  sum_insured_paise?: number | null;
  copay_percent_bp?: number;
}) => post<Wrapped & { policy: Policy }>('/payer/policies', body);

export const verifyPolicy = (policyId: number) =>
  post<Wrapped & { policy: Policy }>(`/payer/policies/${policyId}/verify`, {});

export const createClaim = (body: {
  patient_id: string;
  claim_type?: ClaimType;
  payer_code?: string | null;
  admission_id?: string | null;
  claimed_paise?: number | null;
  package_code?: string | null;
  notes?: string | null;
  /** The same key again returns the claim it opened, never a second claim. */
  idempotency_key?: string | null;
}) => post<ClaimReply>('/payer/claims', body);

export const setClaimedAmount = (claimId: number, claimedPaise: number) =>
  post<ClaimReply>(`/payer/claims/${claimId}/amount`, { claimed_paise: claimedPaise });

/** RECORD that the desk raised a pre-auth with the payer elsewhere. Sends nothing. */
export const recordPreauthRequest = (
  claimId: number,
  body: { requested_paise: number; reference?: string | null; remark?: string | null },
) => post<ClaimReply>(`/payer/claims/${claimId}/preauth`, body);

export const recordPreauthDecision = (
  claimId: number,
  body: {
    decision: 'approved' | 'partially_approved' | 'denied';
    approved_paise?: number | null;
    reference?: string | null;
    denial_code?: string | null;
    denial_reason?: string | null;
  },
) => post<ClaimReply>(`/payer/claims/${claimId}/preauth/decision`, body);

/**
 * RECORD a submission the desk made on the payer's portal / by e-mail / on
 * paper. Named `recordSubmission`, and posted to `/submission` rather than
 * `/submit`, because it records a noun that already happened — it does not
 * perform a verb.
 */
export const recordSubmission = (
  claimId: number,
  body: { claimed_paise?: number | null; external_ref?: string | null; submitted_at?: string | null },
) => post<ClaimReply>(`/payer/claims/${claimId}/submission`, body);

export const recordQuery = (
  claimId: number,
  body: { query_text: string; answered?: boolean; document_ref?: string | null },
) => post<ClaimReply>(`/payer/claims/${claimId}/query`, body);

export const recordDecision = (
  claimId: number,
  body: {
    decision: 'approved' | 'partially_approved' | 'denied';
    approved_paise?: number | null;
    tds_paise?: number;
    denial_code?: string | null;
    denial_reason?: string | null;
    external_ref?: string | null;
  },
) => post<ClaimReply>(`/payer/claims/${claimId}/decision`, body);

/** Record money that actually arrived. The shortfall is computed server-side
 *  from the approved amount, the co-pay and the TDS — never typed by a clerk. */
export const recordSettlement = (
  claimId: number,
  body: { settled_paise: number; tds_paise?: number | null; utr_reference?: string | null },
) => post<ClaimReply>(`/payer/claims/${claimId}/settlement`, body);

export const recordAppeal = (claimId: number, body: { grounds: string }) =>
  post<ClaimReply>(`/payer/claims/${claimId}/appeal`, body);

export const closeClaim = (claimId: number, reason?: string) =>
  post<ClaimReply>(`/payer/claims/${claimId}/close`, { reason: reason ?? null });

export const cancelClaim = (claimId: number, reason: string) =>
  post<ClaimReply>(`/payer/claims/${claimId}/cancel`, { reason });

export const addNote = (claimId: number, note: string) =>
  post<ClaimReply>(`/payer/claims/${claimId}/note`, { note });

/* ── Display helpers ──────────────────────────────────────────────────────── */

/** Human labels for the lifecycle. The status string itself is never shown. */
export const STATUS_LABEL: Record<ClaimStatus, string> = {
  draft: 'Draft',
  preauth_requested: 'Pre-auth pending',
  preauth_approved: 'Pre-auth approved',
  preauth_denied: 'Pre-auth denied',
  submitted: 'With payer',
  queried: 'Query, ours to answer',
  approved: 'Approved',
  partially_approved: 'Partly approved',
  denied: 'Denied',
  settled: 'Settled',
  closed: 'Closed',
  cancelled: 'Cancelled',
  appealed: 'Appealed',
};

/**
 * Whose move is it. The single most useful cut of a TPA work queue: "the payer
 * has had this 40 days" and "we have not answered their query for 40 days" are
 * the same number and completely different mornings.
 */
export function whoseMove(status: ClaimStatus): 'payer' | 'us' | 'done' {
  if (status === 'preauth_requested' || status === 'submitted' || status === 'appealed') return 'payer';
  if (status === 'settled' || status === 'closed' || status === 'cancelled') return 'done';
  return 'us';
}

/** Status → the tone token family the badge uses. Never opacity. */
export function statusTone(status: ClaimStatus): 'ok' | 'warn' | 'danger' | 'info' | 'muted' {
  switch (status) {
    case 'settled':
    case 'approved':
      return 'ok';
    case 'denied':
    case 'preauth_denied':
      return 'danger';
    case 'queried':
    case 'partially_approved':
    case 'appealed':
      return 'warn';
    case 'submitted':
    case 'preauth_requested':
    case 'preauth_approved':
      return 'info';
    default:
      return 'muted';
  }
}
