/**
 * Orb Hospital OS — Nurse Mode API
 *
 * The shift board (what is due, what is overdue, who is deteriorating) plus the
 * two observation endpoints: score a set live while it is being typed, and
 * record it. Medication administration and I-PASS handover are NOT here — they
 * go through api/client.administerMedication and api/ipass, which already own
 * those workflows and their safety guards.
 *
 * Routed through the shared apiFetch wrapper so it reuses the centralized auth
 * headers and 401/403 handling.
 */
import { apiFetch, API_BASE, buildHeaders, getUser } from './client';
import { newId } from '../lib/id';

/** NEWS2 as returned by services/vigil_service.compute_news2. */
export interface News2 {
  /** 0-1 normalized INDEX of the ordinal aggregate — not a probability. */
  score: number;
  level: 'critical' | 'elevated' | 'stable';
  raw_news2: number;
  single_param_3: boolean;
  drivers: string[];
  breakdown: { parameter: string; value: unknown; points: number; note: string | null }[];
  scoring_model: string;
  spo2_scale: 1 | 2;
  /** False when a scored vital was not measured: the aggregate is then a
   *  MINIMUM (an unmeasured parameter scores 0), and must be shown as one. */
  complete?: boolean;
  missing_parameters?: string[];
}

/** Minimum monitoring frequency + the RCP response for a score band. */
export interface ObsSchedule {
  interval_minutes: number;
  band: string;
  response: string;
}

export interface NurseAllergy {
  substance: string;
  reaction: string | null;
  severity: 'mild' | 'moderate' | 'severe' | 'unknown';
}

/** The bedside allergy screen, run by the board ahead of the bedside
 *  (allergy_interlock.summarize via nurse_shift_service._preview_allergy).
 *  ADVISORY: the server screens again when Given is pressed. `decision` 'block'
 *  with no `override` means the dose will be refused until a prescriber records
 *  a reason; `override` set means a prescriber already did, for this order and
 *  this allergy picture. */
export interface MedAllergyPreview {
  decision: 'allow' | 'warn' | 'block' | string;
  screen_failed: boolean;
  alerts: {
    substance?: string;
    reaction?: string | null;
    match?: string;
    decision?: string;
    effective_severity?: string;
    message?: string;
  }[];
  override?: { reason?: string; by?: string | null; at?: string | null; carried?: boolean } | null;
  override_requires_permission?: string | null;
  message?: string | null;
}

/** One eMAR row on the round. Due times are DERIVED from the order frequency
 *  and the last charted dose — see server/services/nurse_shift_service.py. */
export interface MedTask {
  medication_id: number;
  drug_name: string;
  dosage: string | null;
  route: string | null;
  frequency: string | null;
  schedule_kind: 'scheduled' | 'prn' | 'continuous' | 'stat' | 'unscheduled';
  interval_minutes: number | null;
  status: string;
  last_given_at: string | null;
  given_count: number;
  due_at: string | null;
  due_basis: 'last_given' | 'order_start' | 'stat_pending' | 'stat_given' | 'unscheduled';
  /** Scheduled slots since the anchor with no charted administration. */
  missed_slots: number;
  minutes_until_due: number | null;
  overdue: boolean;
  due_soon: boolean;
  /** AI-sourced med with no pharmacist verification — NOT administrable. */
  unverified: boolean;
  /** Absent only from a board cached before the field existed. */
  allergy?: MedAllergyPreview | null;
}

export interface NursePatientCard {
  patient_id: string;
  name: string;
  mrn: string | null;
  bed: string | null;
  ward: string | null;
  age: number | null;
  sex: string | null;
  diagnosis: string | null;
  department: string | null;
  attending_doctor_name: string | null;
  on_scale_2: boolean;
  allergies: NurseAllergy[];
  news2: News2 | null;
  /** `vitals.source` of the row `news2` was scored from. 'simulated' is
   *  synthetic and carries an honesty label on screen. */
  vitals_source?: string | null;
  /** The last set a PERSON took: the obs-due clock runs from this, never from
   *  a simulated or monitor row (nurse_shift_service.OBS_CLOCK_SOURCES). */
  last_obs_at: string | null;
  last_obs_source?: string | null;
  /** The vitals row behind last_obs_at, for "Entered in error". */
  last_obs_vitals_id?: number | null;
  /** Null while scoring is switched off and the ward has no interval set. */
  obs_interval_minutes: number | null;
  /** Null while scoring is switched off (the Orb Ward Record): no band and no
   *  response line, the due time is the ward's fixed interval. */
  obs_band: string | null;
  obs_response: string | null;
  obs_due_at: string | null;
  /** 'news2', or 'ward_interval' while scoring is switched off. */
  obs_due_source?: 'news2' | 'ward_interval' | null;
  /** Set while scoring is off and the ward has no interval: what to do. */
  obs_due_note?: string | null;
  obs_minutes_until_due: number | null;
  obs_overdue: boolean;
  obs_due_soon: boolean;
  medications: MedTask[];
  meds_overdue: number;
  meds_due_soon: number;
  /** Doses the bedside allergy screen will refuse without a prescriber. */
  meds_allergy_blocked?: number;
}

export interface ShiftBoard {
  generated_at: string;
  department: string | null;
  /** False while scoring is switched off on this computer (the Ward Record). */
  scoring_enabled?: boolean;
  patients: NursePatientCard[];
  totals: {
    patients: number;
    /** Null while scoring is switched off: band counts are a score. */
    critical: number | null;
    elevated: number | null;
    obs_overdue: number;
    meds_overdue: number;
    meds_allergy_blocked?: number;
  };
}

/** A submitted observation set. Every reading is optional (a partial set is a
 *  legitimate clinical record) but at least one must be present. */
export interface ObservationInput {
  heart_rate?: number | null;
  systolic_bp?: number | null;
  diastolic_bp?: number | null;
  respiratory_rate?: number | null;
  temperature?: number | null;
  spo2?: number | null;
  /** ACVPU. null when the box is empty: stored empty, never as alert. A nurse
   *  at the bedside must send one; the clerk may leave it empty. */
  consciousness?: string | null;
  /** null when the box is empty: stored empty, never as on air. */
  on_oxygen?: boolean | null;
  /** Stamped when the nurse hits Save; also the server-side dedupe key. For
   *  the ward clerk it is the time written on the paper sheet. */
  taken_at?: string;
  /** Readings the paper sheet showed but nobody could read. Each is stored
   *  empty and named on the record (Orb Ward Record). */
  illegible?: IllegibleField[];
}

/** The readings that can be marked illegible (nurse_shift_service.ILLEGIBLE_FIELDS). */
export type IllegibleField =
  | 'heart_rate' | 'systolic_bp' | 'diastolic_bp' | 'spo2'
  | 'temperature' | 'respiratory_rate' | 'consciousness' | 'on_oxygen';

export interface ObservationPreview {
  news2: News2;
  schedule: ObsSchedule;
  observation: Record<string, number | string | boolean | null>;
}

export interface ObservationRecorded {
  vitals_id: number;
  recorded_at: string;
  /** null when scoring is switched off on this computer (ORB_SCORING_ENABLED). */
  news2: News2 | null;
  schedule: ObsSchedule | null;
  replayed: boolean;
  /** 'nurse_manual' at the bedside, 'clerk_transcribed' from the paper sheet. */
  source?: string;
  /** When the set was keyed (UTC); recorded_at is when it was taken. */
  entered_at?: string;
  illegible?: IllegibleField[];
  /** false on a set the ward clerk copied more than the live alert limit
   *  after it was taken (2 hours by default): charted, but no alert raised. */
  live_hooks?: boolean;
  /** Said back to whoever keyed the set, e.g. "Charted. Older than 2 hours,
   *  so no alert was raised." Absent on an ordinary set. */
  message?: string;
}

export interface ObservationVoided {
  vitals_id: number;
  patient_id: string;
  voided_at: string;
  voided_by: string;
  void_reason: string;
}

/**
 * The whole shift as tasks.
 *
 * Takes no scope argument on purpose. The ward is decided by the SERVER from
 * the authenticated user; this call used to send `?all_departments=true`, which
 * let a nurse scoped to one ward read every patient in the hospital. `department`
 * on the response says which ward the server chose — display it, never set it.
 */
export async function getShiftBoard(): Promise<ShiftBoard> {
  return apiFetch('/nurse/board');
}

/**
 * Score an observation set WITHOUT saving it, so the entry screen can show the
 * NEWS2 it is about to produce as the nurse types.
 *
 * Takes an AbortSignal because this fires on every keystroke: an in-flight
 * preview for a stale set must be cancelled, otherwise a slow response can land
 * after a newer one and show a score that does not match what is on screen.
 * Uses fetch directly rather than apiFetch so the signal can be threaded
 * through; a 401 here is a transient preview failure, not a session event.
 */
export async function previewNews2(
  patientId: string,
  obs: ObservationInput,
  signal?: AbortSignal,
): Promise<ObservationPreview> {
  const res = await fetch(`${API_BASE}/nurse/patients/${patientId}/news2-preview`, {
    method: 'POST',
    credentials: 'include',
    headers: buildHeaders('POST'),
    body: JSON.stringify(obs),
    signal,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail || `Preview failed (${res.status})`);
  }
  return res.json();
}

/**
 * Record a nurse-taken observation set.
 *
 * Deliberately NOT routed through offlineMutate. An obs set is the input to a
 * NEWS2 escalation decision, and the whole point of the entry screen is that
 * the nurse sees the resulting score before walking away — an optimistic
 * offline "saved" with a locally-guessed score would be a fabricated clinical
 * number. This fails loudly when offline so the reading is re-entered against a
 * live server. The Idempotency-Key still rides along, and the server dedupes on
 * (patient, source, taken_at) so a double-tap cannot chart the reading twice.
 */
export async function recordObservations(
  patientId: string,
  obs: ObservationInput,
): Promise<ObservationRecorded> {
  return apiFetch(`/nurse/patients/${patientId}/observations`, {
    method: 'POST',
    headers: { 'Idempotency-Key': newId() },
    body: JSON.stringify(obs),
  });
}

/**
 * Mark a recorded observation set as entered in error, with a reason.
 *
 * Nurse and doctor only (vitals.write); the server refuses the ward clerk. The
 * set stays on the record, struck out, and every screen stops reading it as the
 * latest. Online only, like recording: a correction to the chart is never queued.
 */
export async function voidObservation(
  vitalsId: number,
  reason: string,
): Promise<ObservationVoided> {
  return apiFetch(`/nurse/observations/${vitalsId}/void`, {
    method: 'POST',
    headers: { 'Idempotency-Key': newId() },
    body: JSON.stringify({ reason }),
  });
}

/** One patient on the ward clerk's list (GET /api/nurse/transcribe/patients). */
export interface TranscribePatient {
  patient_id: string;
  name: string | null;
  mrn: string | null;
  ward: string | null;
  bed: string | null;
  /** When this stay began (UTC), or null when nothing records it. */
  admitted_at: string | null;
}

export interface TranscribePatientList {
  department: string | null;
  patients: TranscribePatient[];
}

/**
 * The admitted patients of the ward clerk's own department (vitals.transcribe).
 * Works with the beds module switched off, unlike the bed board. Names, ward,
 * bed and admission time only: nothing clinical.
 */
export async function getTranscribePatients(): Promise<TranscribePatientList> {
  return apiFetch('/nurse/transcribe/patients');
}

/** Who is signed in — used to stamp the handover author line. */
export function currentUserName(): string {
  return getUser()?.name ?? 'this shift';
}
