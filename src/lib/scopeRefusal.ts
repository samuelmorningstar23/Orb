/**
 * The wording every surface must use for a patient-scope refusal.
 *
 * The API answers an unknown patient id and an out-of-ward patient id
 * identically ("Patient not found", 404) so that a refusal never confirms
 * whether the record exists. A screen that prints that body verbatim throws
 * that care away twice over: it tells a clinician the patient does not exist
 * when the record may be sitting in another department, and it turns a
 * deliberately ambiguous answer into an existence oracle in the one direction
 * the server was careful about.
 *
 * So the rewording lives here rather than in any one page: every surface that
 * can receive the refusal — downtime snapshots, I-PASS handoff drafts — says
 * the same careful thing (DEMO_READINESS §15.1).
 *
 * Returns null for anything that is NOT that refusal, so callers keep their own
 * message for real errors: `refusalMessage(e.message) ?? e.message`.
 */
export function refusalMessage(message: string | undefined | null): string | null {
  if (!message || !/^patient not found$/i.test(message.trim())) return null;
  return (
    'This patient record is not available to you. It may not exist, or it may be ' +
    'outside your department. If you need it clinically, contact the team caring ' +
    'for the patient.'
  );
}
