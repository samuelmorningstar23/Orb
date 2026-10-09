/**
 * Orb Hospital OS — EHR Downtime Mode API
 *
 * Read/refresh a durable, local, read-only snapshot of the essential clinical
 * picture that keeps working when the EHR / network / LLM is down. The backend
 * assembles and serves these from local SQLite with no external dependencies.
 */
import { API_BASE, apiFetch, buildHeaders, handleResponseError } from './client';

export interface DowntimeDemographics {
  id: string;
  name: string;
  mrn: string;
  bed?: string | null;
  ward?: string | null;
  admission_date?: string | null;
  diagnosis?: string | null;
  status?: string | null;
  age?: number | null;
  date_of_birth?: string | null;
  sex?: string | null;
  phone?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
  address?: string | null;
  attending_doctor_id?: string | null;
  attending_doctor_name?: string | null;
}

export interface DowntimeAllergy {
  substance: string;
  substance_class?: string | null;
  reaction?: string | null;
  severity?: string | null;
  status?: string | null;
  source?: string | null;
}

export interface DowntimeProblem {
  problem: string;
  icd10_code?: string | null;
  snomed_code?: string | null;
  status?: string | null;
  onset_date?: string | null;
  source?: string | null;
}

export interface DowntimeMedication {
  drug_name: string;
  dosage?: string | null;
  route?: string | null;
  frequency?: string | null;
  status?: string | null;
  source?: string | null;
  notes?: string | null;
  /** Who verified an AI-extracted drug. Absent (not null) in snapshots
   *  captured before schema 2, which cannot say either way. */
  verified_by?: string | null;
}

export interface DowntimeVitals {
  timestamp?: string | null;
  heart_rate?: number | null;
  systolic_bp?: number | null;
  diastolic_bp?: number | null;
  spo2?: number | null;
  temperature?: number | null;
  respiratory_rate?: number | null;
  consciousness?: string | null;
  on_oxygen?: number | null;
}

export interface DowntimeNews2 {
  raw_news2?: number | null;
  score?: number | null;
  level?: string | null;
  drivers?: string[] | null;
  scoring_model?: string | null;
  spo2_scale?: number | null;
}

export interface DowntimeNote {
  id: number;
  note_format?: string | null;
  structured_note?: string | null;
  author_id?: string | null;
  /** Names, from 3 October 2026 on; older copies carry only the ids. */
  author_name?: string | null;
  signed_by?: string | null;
  signed_by_name?: string | null;
  signed_at?: string | null;
  created_at?: string | null;
}

/** A note written in the chart (clinical_documents), schema 2 onwards. */
export interface DowntimeDocument {
  id: number;
  doc_type?: string | null;
  title?: string | null;
  content?: string | null;
  author_id?: string | null;
  author_name?: string | null;
  created_at?: string | null;
}

/** One of Orb's rendered lab reports, schema 2 onwards. */
export interface DowntimeLabReport {
  id: number;
  title?: string | null;
  content?: string | null;
  author_id?: string | null;
  created_at?: string | null;
}

/** The newest interfaced result of one analyte, schema 2 onwards. */
export interface DowntimeLabResult {
  observation_name: string;
  value_numeric?: number | null;
  value_text?: string | null;
  unit?: string | null;
  reference_range?: string | null;
  abnormal_flag?: string | null;
  status?: string | null;
  observed_at?: string | null;
}

/** An open escalation chain at capture time, schema 2 onwards. */
export interface DowntimeEscalation {
  status: string;
  trigger?: string | null;
  news2?: number | null;
  level?: string | null;
  current_tier?: number | null;
  max_tier?: number | null;
  tier_role?: string | null;
  tier_label?: string | null;
  raised_at?: string | null;
  acknowledged_at?: string | null;
}

export interface DowntimeRiskScore {
  timestamp?: string | null;
  score?: number | null;
  level?: string | null;
  drivers?: string[] | string | null;
}

export interface DowntimeSnapshot {
  schema_version: number;
  captured_at: string;
  patient_id: string;
  demographics: DowntimeDemographics;
  allergies: DowntimeAllergy[];
  problems: DowntimeProblem[];
  medications: DowntimeMedication[];
  /** False on an Orb Ward Record computer, whose drug chart stays on paper:
   *  the page draws no medicines section. Absent from an older snapshot. */
  drug_chart?: boolean;
  latest_vitals: DowntimeVitals | null;
  news2: DowntimeNews2 | null;
  recent_notes: DowntimeNote[];
  recent_risk_scores: DowntimeRiskScore[];
  /* Schema 2 (2026-10-03). Optional because older snapshots, and copies this
     device cached from them, do not carry them; the page says so. */
  code_status?: string | null;
  open_escalations?: DowntimeEscalation[];
  recent_documents?: DowntimeDocument[];
  recent_lab_reports?: DowntimeLabReport[];
  lab_results?: DowntimeLabResult[];
  counts: {
    allergies: number; problems: number; medications: number; notes: number;
    documents?: number; lab_reports?: number; lab_results?: number;
  };
}

export interface DowntimeSnapshotEnvelope {
  id: number;
  patient_id: string;
  summary: string;
  /** A user id, or 'auto' for the scheduled capture. */
  created_by: string;
  created_by_name?: string | null;
  created_at: string;
  snapshot: DowntimeSnapshot;
}

export interface CaptureAllResult {
  count: number;
  total_admitted: number;
  failed: { patient_id: string; error: string }[];
  captured_at: string;
}

/** Freeze the current essential clinical picture for a patient into a local snapshot. */
export async function captureSnapshot(patientId: string): Promise<DowntimeSnapshotEnvelope> {
  return apiFetch(`/downtime/capture/${patientId}`, { method: 'POST' });
}

/** Read back the most recent snapshot for a patient (the downtime fallback view). */
export async function getLatestSnapshot(patientId: string): Promise<DowntimeSnapshotEnvelope> {
  return apiFetch(`/downtime/${patientId}/latest`);
}

/** Read the snapshot history (newest first) for a patient. */
export async function listSnapshots(patientId: string): Promise<{ snapshots: DowntimeSnapshotEnvelope[] }> {
  return apiFetch(`/downtime/${patientId}/history`);
}

/** Snapshot every admitted patient (pre-downtime drill / scheduled job). */
export async function captureAll(): Promise<CaptureAllResult> {
  return apiFetch('/downtime/capture-all', { method: 'POST' });
}

/** Fetch the ward-level print pack as raw HTML. A raw fetch, not apiFetch:
 *  the response is a text/html document and apiFetch would json() it. */
export async function fetchWardPackHtml(patientId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/downtime/${patientId}/ward-pack`, {
    credentials: 'include',
    headers: buildHeaders('GET'),
  });
  if (!response.ok) await handleResponseError(response);
  return response.text();
}
