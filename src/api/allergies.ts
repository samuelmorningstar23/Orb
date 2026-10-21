/**
 * The chart's allergy controls (founding ward list item 5, 21 October 2026).
 *
 * The routes and the interlock already existed (routes/clinical_records_routes,
 * services/allergy_interlock); this is the client for them. An allergy added
 * here on day three reaches `patient_allergies`, the one list every safety
 * check reads: the order-time gate, pharmacy verification and the bedside
 * interlock. A deactivation carries a reason and is written to the audit
 * chain; the row is kept as inactive, never deleted.
 */
import { apiFetch } from './client';

export const ALLERGY_SEVERITIES = ['mild', 'moderate', 'severe', 'unknown'] as const;
export type AllergySeverity = (typeof ALLERGY_SEVERITIES)[number];

/** One `patient_allergies` row as the routes return it. */
export interface ChartAllergy {
  id: number;
  patient_id: string;
  /** Normalised on write (lower case). */
  substance: string;
  substance_class?: string | null;
  reaction?: string | null;
  severity?: AllergySeverity | string | null;
  status: 'active' | 'inactive' | 'resolved' | string;
  /** 'clinician', 'admission', 'roster_import', 'llm_extracted' or 'ingested_fhir'. */
  source?: string | null;
  recorded_by?: string | null;
  recorded_at?: string | null;
}

export interface AddAllergyInput {
  substance: string;
  reaction?: string | null;
  severity?: AllergySeverity;
}

export async function listAllergies(patientId: string, includeInactive = false): Promise<ChartAllergy[]> {
  const res = await apiFetch<{ allergies?: ChartAllergy[] } | ChartAllergy[]>(
    `/patients/${encodeURIComponent(patientId)}/allergies${includeInactive ? '?include_inactive=true' : ''}`,
  );
  const list = Array.isArray(res) ? res : res?.allergies;
  if (!Array.isArray(list)) throw new Error('The allergy list could not be read.');
  return list;
}

export async function addAllergy(patientId: string, input: AddAllergyInput): Promise<ChartAllergy> {
  return apiFetch(`/patients/${encodeURIComponent(patientId)}/allergies`, {
    method: 'POST',
    body: JSON.stringify({
      substance: input.substance,
      reaction: input.reaction || null,
      severity: input.severity || 'unknown',
      source: 'clinician',
    }),
  });
}

/** Deactivates one allergy with the reason the audit chain records. */
export async function deactivateAllergy(patientId: string, allergyId: number, reason: string): Promise<{ ok: boolean }> {
  return apiFetch(`/patients/${encodeURIComponent(patientId)}/allergies/${allergyId}/deactivate`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}
