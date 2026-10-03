import { formatClinicalDateTime, parseUTC } from '../../lib/datetime'

/* How a medication's notes print, kept apart from MedListPrintView.tsx so
   that file exports only components (fast refresh). */

/** A pharmacist's rejection stamp, as pharmacy_verification_service writes it:
 *  "[REJECTED by <name> @ <UTC ISO>] reason". Printed with the time in local
 *  clinical form, not as a raw UTC string. */
const REJECTION_STAMP = /\[REJECTED by (.+?) @ ([^\]]+)\]/g

export function readableMedNotes(notes: string | null | undefined): string {
  if (!notes) return ''
  return notes.replace(REJECTION_STAMP, (whole, who: string, at: string) => {
    const when = formatClinicalDateTime(parseUTC(at))
    return when ? `Rejected by ${who}, ${when}:` : whole
  })
}
