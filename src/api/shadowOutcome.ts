/**
 * Orb Hospital OS — Shadow outcome capture (the charge nurse's ground truth)
 *
 * The WRITE half of the pilot instrument's human-entry surface: record a
 * confirmed deterioration event, confirm a shift had none, and read what this
 * shift still owes. Server side: server/routes/shadow_routes.py, permission
 * `outcome.record` (nurse, doctor, admin).
 *
 * DELIBERATELY a separate module from api/scorecard.ts. That module is the
 * admin READ surface — metrics, adjudication queue, export — which the person
 * recording ground truth must never see (permissions.py denies it server-side:
 * a recorder who can watch the false-alert rate move as they enter outcomes
 * has been handed an incentive to enter them differently). Keeping the modules
 * apart means the capture screen cannot drag a scorecard call, or even a
 * scorecard TYPE, into the nurse bundle by accident.
 *
 * The observation-entry rule applies to both writes here: never optimistic,
 * never offline-queued. A confirmation the nurse believes was recorded but was
 * not silently turns an event-free shift into uncovered time; an outcome that
 * silently vanishes undefines the pilot's headline metric. Both go straight
 * through apiFetch and fail loudly.
 */
import { apiFetch } from './client';

const SHADOW_BASE = '/admin/shadow';

/** One ward's standing within a shift window. */
export interface WardShiftStatus {
  ward: string;
  confirmed: boolean;
  confirmed_at: string | null;
  /** Role only, never a name — see _window_body in shadow_routes.py. */
  confirmed_by_role?: string | null;
  events_recorded: number;
  status: 'unknown' | 'confirmed' | 'events_recorded' | 'outstanding';
}

/** A shift window: the exact period strings to POST back to /outcome/none. */
export interface ShiftWindow {
  shift: string;
  period_start: string;
  period_end: string;
  /** "07:00–15:00" in the appliance's local time. */
  label: string;
  is_current: boolean;
  wards: WardShiftStatus[];
}

export interface ShiftStatus {
  generated_at: string;
  /** False when the history could not be read — recording still works. */
  status_available: boolean;
  shifts: ShiftWindow[];
  note?: string;
  why?: string;
  max_backdate_hours?: number;
  /** The closed vocabularies the entry form may offer — served by the same
   *  route the POST validates against, so the form cannot drift from it. */
  event_types: string[];
  sources: string[];
  recognition: string[];
}

/** Current + previous shift for one ward: confirmed by whom, events recorded. */
export async function getShiftStatus(ward: string): Promise<ShiftStatus> {
  return apiFetch(`${SHADOW_BASE}/outcome/shift-status?ward=${encodeURIComponent(ward)}`);
}

/** Mirrors OutcomeIn in shadow_routes.py exactly. */
export interface OutcomeInput {
  patient_id: string;
  event_type: string;
  /** ISO-8601; omit for "now". The server rejects future and >72h-old times. */
  occurred_at?: string;
  source: string;
  recognised_by?: string;
  /** Free text, stored in phi_json — never exported. Max 1000 chars. */
  note?: string;
}

export interface OutcomeRecorded {
  recorded: true;
  shadow_event_id: number;
  event_type: string;
  source: string;
  recognised_by: string | null;
  occurred_at: string | null;
  note: string;
}

/** Record a confirmed deterioration event — the lead-time ground truth. */
export async function recordOutcome(input: OutcomeInput): Promise<OutcomeRecorded> {
  return apiFetch(`${SHADOW_BASE}/outcome`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

/** Mirrors NoOutcomeIn: post back the exact strings shift-status returned. */
export interface NoOutcomeInput {
  ward: string;
  period_start: string;
  period_end: string;
  shift: string;
}

export interface NoneConfirmed {
  /** False when this shift was already confirmed — a no-op, not an error. */
  recorded: boolean;
  already_confirmed?: boolean;
  shadow_event_id?: number;
  episode_id: string;
  ward: string;
  shift: string;
  confirmed_at?: string | null;
  confirmed_by_role?: string | null;
  note?: string;
}

/** The once-per-shift "no deterioration events" confirmation. Resubmitting an
 *  already-confirmed shift returns 200 with `already_confirmed` — the server
 *  dedupes on (ward, date, shift), so confirming twice can never double-count. */
export async function confirmNoEvents(input: NoOutcomeInput): Promise<NoneConfirmed> {
  return apiFetch(`${SHADOW_BASE}/outcome/none`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}
