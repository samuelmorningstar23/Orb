/**
 * Orb — ABDM / ABHA rails API client.
 *
 * ORB IS NOT CONNECTED TO THE ABDM GATEWAY. Every function here records a step a
 * human performed somewhere else — on the ABDM/NHA portal, in the patient's ABHA
 * app, or on the HIP console of a system that IS onboarded. There is no
 * `sendToAbdm`, no `syncCareContexts`, no `fetchAbhaProfile`, and there must
 * never be one: a clerk who believes Orb linked the care context does not link
 * it, and the patient's discharge summary never reaches their PHR.
 *
 * The server attaches `notice` / `notice_short` / `gateway_connected: false` to
 * EVERY payload (see server/routes/abdm_routes.py `_wrap`). The panel renders
 * its own literal copy of the sentence too, so the words are on screen before
 * the first round trip and still there when every fetch fails.
 *
 * NO ABHA IN A URL. `checkAbha` is a POST with the value in the body, not a GET
 * with a query parameter: an ABHA number is a national health identifier, and a
 * query string lands in the browser history, the proxy log and the access log —
 * none of which are the encrypted database.
 *
 * UNVERIFIED IS UNVERIFIED. `recordAbha` has no verification field, by design.
 * `verification_status` arrives from the server and is the only thing any screen
 * may key on; never infer "verified" from the presence of a number.
 */
import { apiFetch } from './client';

/* ── Types ───────────────────────────────────────────────────────────────── */

export type VerificationStatus =
  | 'unverified' | 'verified' | 'verification_failed' | 'deactivated';

export type VerificationOutcome = 'verified' | 'failed' | 'inconclusive';

export type ConsentStatus = 'requested' | 'granted' | 'denied' | 'expired' | 'revoked';

export type QueueBucket = 'ready_to_link' | 'abha_unverified' | 'no_abha';

export interface Wrapped {
  notice: string;
  notice_short: string;
  gateway_connected: false;
  transmitted_to_abdm: false;
}

export interface CodeLabel {
  code: string;
  label: string;
}

export interface AbdmMeta extends Wrapped {
  verification_statuses: VerificationStatus[];
  verification_methods: CodeLabel[];
  verification_outcomes: VerificationOutcome[];
  verification_places: CodeLabel[];
  identity_sources: CodeLabel[];
  hi_types: string[];
  link_methods: CodeLabel[];
  encounter_types: string[];
  consent_statuses: ConsentStatus[];
  consent_transitions: Record<string, string[]>;
  purpose_codes: CodeLabel[];
  requester_types: string[];
  queue_buckets: { id: QueueBucket; label: string }[];
  recognised_abha_suffixes: string[];
  checks_abha_check_digit: false;
  milestone_status: string;
}

export interface Verification {
  id: number;
  abha_identity_id: number;
  patient_id: string;
  method: string;
  outcome: VerificationOutcome;
  performed_where: string;
  performed_by: string;
  performed_at: string | null;
  recorded_at: string | null;
  recorded_by: string | null;
  reference: string | null;
  failure_reason: string | null;
  notes: string | null;
}

export interface CareContext {
  id: number;
  abha_identity_id: number;
  patient_id: string;
  patient_name?: string | null;
  care_context_reference: string;
  display: string | null;
  hi_type: string;
  encounter_type: string;
  encounter_ref: string | null;
  encounter_date: string | null;
  status: 'linked' | 'unlinked';
  is_linked: boolean;
  linked_at: string | null;
  linked_by: string | null;
  link_method: string;
  link_method_label: string;
  link_reference: string | null;
  unlinked_at: string | null;
  unlinked_by: string | null;
  unlink_reason: string | null;
  notes: string | null;
  abha_number?: string | null;
  abha_number_formatted?: string | null;
  transmitted_to_abdm: false;
}

export interface AbhaIdentity {
  id: number;
  patient_id: string;
  patient_name?: string | null;
  patient_mrn?: string | null;
  patient_ward?: string | null;
  abha_number: string | null;
  /** Display form (XX-XXXX-XXXX-XXXX), derived server-side from the stored digits. */
  abha_number_formatted: string | null;
  abha_address: string | null;
  verification_status: VerificationStatus;
  /** The words a screen shows. Never render the raw status string. */
  verification_label: string;
  /** The ONE boolean to key on. Computed from the stored status, nothing else. */
  is_verified: boolean;
  verified_at: string | null;
  verified_by: string | null;
  verification_method: string | null;
  verification_reference: string | null;
  is_primary: boolean;
  status: 'active' | 'superseded' | 'withdrawn';
  source: string;
  notes: string | null;
  recorded_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  transmitted_to_abdm: false;
  verifications?: Verification[];
  care_contexts?: CareContext[];
  events?: AbdmEvent[];
}

export interface AbdmEvent {
  id: number;
  entity_type: string;
  entity_id: number;
  patient_id: string | null;
  event_type: string;
  from_status: string | null;
  to_status: string | null;
  detail: string | null;
  reason: string | null;
  actor_id: string | null;
  occurred_at: string | null;
  recorded_at: string | null;
}

export interface ConsentArtefact {
  id: number;
  artefact_id: string | null;
  request_id: string | null;
  patient_id: string;
  patient_name?: string | null;
  abha_identity_id: number | null;
  requester_name: string;
  requester_type: string;
  purpose_code: string;
  purpose_label: string;
  purpose_text: string | null;
  hi_types: string[];
  hi_types_recorded: boolean;
  date_range_from: string | null;
  date_range_to: string | null;
  expires_at: string | null;
  expires_at_display: string;
  days_to_expiry: number | null;
  /** What the row STORES. Reported beside effective_status, never instead of it. */
  status: ConsentStatus;
  /** What it MEANS right now — expiry is derived at read time, never trusted. */
  effective_status: ConsentStatus;
  is_expired: boolean;
  is_effective: boolean;
  is_revoked: boolean;
  /** Always false. A revoked artefact needs a NEW artefact, not a status change. */
  can_be_reinstated: false;
  granted_at: string | null;
  denied_at: string | null;
  denial_reason: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  revocation_reason: string | null;
  recorded_where: string;
  recorded_by: string | null;
  notes: string | null;
  transmitted_to_abdm: false;
  events?: AbdmEvent[];
}

export interface QueueEntry {
  encounter_ref: string;
  patient_id: string;
  patient_name: string | null;
  patient_mrn: string | null;
  department: string | null;
  ward: string | null;
  encounter_status: string;
  encounter_at: string | null;
  admission_type: string | null;
  abha_identity_id: number | null;
  abha_number: string | null;
  abha_number_formatted: string | null;
  abha_address: string | null;
  verification_status: VerificationStatus | null;
  verification_label: string;
  bucket: QueueBucket;
  bucket_label: string;
  age_days: number | null;
  is_stale: boolean;
}

export interface Queue extends Wrapped {
  encounters: QueueEntry[];
  total: number;
  counts: Record<QueueBucket, number>;
  buckets: { id: QueueBucket; label: string; count: number }[];
  stale_after_days: number;
}

export interface AbdmSummary extends Wrapped {
  identities: {
    total: number;
    verified: number;
    unverified: number;
    verification_failed: number;
    deactivated: number;
    patients_with_abha: number;
    /** "12 of 40" — a fraction with both numbers, never a bare percentage. */
    verified_of_total: string;
  };
  care_contexts: { linked: number; unlinked: number };
  consents: Record<ConsentStatus, number>;
  queue: { total: number; counts: Record<QueueBucket, number> };
}

export interface FormatCheck {
  valid: boolean;
  normalised: string | null;
  formatted?: string | null;
  username?: string | null;
  suffix?: string | null;
  suffix_recognised?: boolean;
  reason: string | null;
  /** Always false: a structural check is not a lookup. */
  checked_against_abdm: false;
  note: string;
}

export interface CheckReply extends Wrapped {
  number?: FormatCheck;
  address?: FormatCheck;
}

/* ── Calls ───────────────────────────────────────────────────────────────── */

const post = <T>(path: string, body: unknown) =>
  apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) });

export const getMeta = () => apiFetch<AbdmMeta>('/abdm/meta');
export const getSummary = () => apiFetch<AbdmSummary>('/abdm/summary');

export const getQueue = (params: { bucket?: QueueBucket | null; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.bucket) q.set('bucket', params.bucket);
  if (params.limit) q.set('limit', String(params.limit));
  const suffix = q.toString();
  return apiFetch<Queue>(`/abdm/queue${suffix ? `?${suffix}` : ''}`);
};

export const getIdentities = (params: { patient_id?: string; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.patient_id) q.set('patient_id', params.patient_id);
  if (params.limit) q.set('limit', String(params.limit));
  const suffix = q.toString();
  return apiFetch<Wrapped & { identities: AbhaIdentity[]; total: number }>(
    `/abdm/identities${suffix ? `?${suffix}` : ''}`,
  );
};

export const getIdentity = (identityId: number) =>
  apiFetch<Wrapped & { identity: AbhaIdentity }>(`/abdm/identities/${identityId}`);

/**
 * Structural check only — nothing is stored and nothing is looked up.
 * POST, so the ABHA never appears in a URL.
 */
export const checkAbha = (body: { abha_number?: string; abha_address?: string }) =>
  post<CheckReply>('/abdm/check', body);

/**
 * Record an ABHA a patient presented. IT IS STORED UNVERIFIED — there is no
 * field here that can say otherwise, and adding one would defeat the module.
 */
export const recordAbha = (body: {
  patient_id: string;
  abha_number?: string | null;
  abha_address?: string | null;
  source?: string;
  notes?: string | null;
  is_primary?: boolean;
}) => post<Wrapped & { identity: AbhaIdentity }>('/abdm/identities', body);

/** RECORD a verification a human performed elsewhere. Orb verified nothing. */
export const recordVerification = (
  identityId: number,
  body: {
    method: string;
    outcome: VerificationOutcome;
    performed_by: string;
    performed_at?: string | null;
    performed_where?: string;
    reference?: string | null;
    failure_reason?: string | null;
    notes?: string | null;
  },
) => post<Wrapped & { identity: AbhaIdentity }>(
  `/abdm/identities/${identityId}/verification`, body,
);

/** Correct a mis-keyed ABHA. The server RESETS the verification to unverified. */
export const correctAbha = (
  identityId: number,
  body: { abha_number?: string | null; abha_address?: string | null; reason: string },
) => post<Wrapped & { identity: AbhaIdentity }>(
  `/abdm/identities/${identityId}/correction`, body,
);

export const recordDeactivation = (identityId: number, reason: string) =>
  post<Wrapped & { identity: AbhaIdentity }>(
    `/abdm/identities/${identityId}/deactivation`, { reason },
  );

export const getCareContexts = (params: { patient_id?: string; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.patient_id) q.set('patient_id', params.patient_id);
  if (params.limit) q.set('limit', String(params.limit));
  const suffix = q.toString();
  return apiFetch<Wrapped & { care_contexts: CareContext[]; total: number }>(
    `/abdm/care-contexts${suffix ? `?${suffix}` : ''}`,
  );
};

/**
 * RECORD that a human linked this episode on a HIP console elsewhere.
 * Named `linkCareContext` and not `sendLink`: it records a noun that already
 * happened. The server refuses if the ABHA is not verified.
 */
export const linkCareContext = (body: {
  identity_id: number;
  care_context_reference: string;
  hi_type?: string;
  display?: string | null;
  encounter_type?: string;
  encounter_ref?: string | null;
  encounter_date?: string | null;
  linked_by: string;
  linked_at?: string | null;
  link_method?: string;
  link_reference?: string | null;
  notes?: string | null;
}) => post<Wrapped & { care_context: CareContext }>('/abdm/care-contexts', body);

/** A reason is REQUIRED. A linked care context cannot silently unlink. */
export const unlinkCareContext = (careContextId: number, reason: string) =>
  post<Wrapped & { care_context: CareContext }>(
    `/abdm/care-contexts/${careContextId}/unlink`, { reason },
  );

export const getConsents = (params: { patient_id?: string; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.patient_id) q.set('patient_id', params.patient_id);
  if (params.limit) q.set('limit', String(params.limit));
  const suffix = q.toString();
  return apiFetch<Wrapped & { consents: ConsentArtefact[]; total: number }>(
    `/abdm/consents${suffix ? `?${suffix}` : ''}`,
  );
};

export const getConsent = (consentId: number) =>
  apiFetch<Wrapped & { consent: ConsentArtefact }>(`/abdm/consents/${consentId}`);

export const recordConsentRequest = (body: {
  patient_id: string;
  requester_name: string;
  purpose_code?: string;
  purpose_text?: string | null;
  hi_types?: string[];
  date_range_from?: string | null;
  date_range_to?: string | null;
  expires_at?: string | null;
  request_id?: string | null;
  identity_id?: number | null;
  requester_type?: string;
  notes?: string | null;
}) => post<Wrapped & { consent: ConsentArtefact }>('/abdm/consents', body);

export const recordConsentGrant = (
  consentId: number,
  body: { artefact_id: string; granted_at?: string | null; expires_at?: string | null },
) => post<Wrapped & { consent: ConsentArtefact }>(`/abdm/consents/${consentId}/grant`, body);

export const recordConsentDenial = (consentId: number, reason: string) =>
  post<Wrapped & { consent: ConsentArtefact }>(`/abdm/consents/${consentId}/denial`, { reason });

/**
 * RECORD that the patient withdrew consent. ONE WAY, NO RETURN.
 *
 * There is deliberately no `reinstateConsent` in this file and no route behind
 * one. If the patient consents afresh, the consent manager issued a NEW
 * artefact — record that one with `recordConsentRequest`.
 */
export const revokeConsent = (consentId: number, reason: string) =>
  post<Wrapped & { consent: ConsentArtefact }>(
    `/abdm/consents/${consentId}/revocation`, { reason },
  );

/* ── Display helpers ─────────────────────────────────────────────────────── */

/** Status → tone token family. Never opacity, never colour alone. */
export function verificationTone(
  status: VerificationStatus | null,
): 'ok' | 'warn' | 'danger' | 'muted' {
  switch (status) {
    case 'verified': return 'ok';
    case 'verification_failed': return 'danger';
    case 'deactivated': return 'danger';
    case 'unverified': return 'warn';
    default: return 'muted';
  }
}

export function consentTone(
  status: ConsentStatus,
): 'ok' | 'warn' | 'danger' | 'info' | 'muted' {
  switch (status) {
    case 'granted': return 'ok';
    case 'revoked': return 'danger';
    case 'denied': return 'danger';
    case 'expired': return 'muted';
    case 'requested': return 'info';
    default: return 'muted';
  }
}

export const CONSENT_LABEL: Record<ConsentStatus, string> = {
  requested: 'Requested',
  granted: 'Granted by patient',
  denied: 'Refused by patient',
  expired: 'Expired',
  revoked: 'Revoked by patient',
};

export function queueTone(bucket: QueueBucket): 'ok' | 'warn' | 'danger' {
  switch (bucket) {
    case 'ready_to_link': return 'ok';
    case 'abha_unverified': return 'warn';
    default: return 'danger';
  }
}
