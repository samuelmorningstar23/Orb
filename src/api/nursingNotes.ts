/**
 * The per-shift nursing note (founding ward list item 4, 21 October 2026).
 *
 * A nurse writes free text about a patient on her own ward; the server files
 * it under the shift running now with her name and the time, audits it on
 * the hash chain, and shows it on the chart timeline, the handover and the
 * inpatient record print. Under the Orb Ward Record profile every route
 * answers 404: nursing notes stay on paper there.
 */
import { apiFetch } from './client';

export interface NursingNote {
  id: number;
  patient_id: string;
  author_id: string;
  /** The author's name as on the users row; falls back to the id on screen. */
  author_name: string | null;
  /** The hospital-local day the shift began (YYYY-MM-DD). */
  shift_date: string;
  /** The ward shift the note is filed under, e.g. "07:00 to 15:00". */
  shift_label: string;
  content: string;
  /** UTC, in the one stored spelling. Format with formatClinicalDateTime. */
  written_at: string;
}

/** The longest note the server accepts (nursing_note_service.MAX_CHARACTERS). */
export const NURSING_NOTE_MAX_CHARACTERS = 4000;

export async function listNursingNotes(patientId: string): Promise<{ patient_id: string; notes: NursingNote[] }> {
  return apiFetch(`/patients/${encodeURIComponent(patientId)}/nursing-notes`);
}

export async function writeNursingNote(patientId: string, content: string): Promise<NursingNote> {
  return apiFetch(`/patients/${encodeURIComponent(patientId)}/nursing-notes`, {
    method: 'POST',
    body: JSON.stringify({ content }),
  });
}
