/**
 * The wording and the timing the AI-drafted assessment block is rendered with.
 *
 * A MODULE OF ITS OWN, not constants hanging off the component, for two
 * reasons. The trivial one is that a file exporting both a component and plain
 * values breaks fast refresh. The one that matters is that these strings are a
 * CONTRACT WITH THE SERVER, and a contract deserves a file you can point a test
 * at: `server/tests/test_assessment_relocation.py` reads `ASSESSMENT_COPY` out
 * of here and asserts every phrase is one `ipass_service._render_assessment_lines`
 * actually prints.
 */

/**
 * How long to keep quietly re-reading before giving up, in ms from mount.
 *
 * Generation is measured at 42-57 s end to end, so a surface that reads once on
 * open and never again shows a clinician nothing on the visit that ASKED for
 * the assessment and only pays out on the next one. These re-reads are silent
 * by construction: nothing is rendered until there is something to render, so
 * they promise nothing and cost one cheap DB read each.
 */
export const ASSESSMENT_RECHECK_MS: readonly number[] = [25000, 50000, 80000];

/**
 * The connective sentences, byte-identical to the server's own printed form.
 *
 * A React tree cannot render `_render_assessment_lines`' text, so the screen has
 * to write these sentences out again — which is exactly the drift the server's
 * own docstrings warn about: two surfaces inventing two stories about one fact.
 * Holding them as string constants keeps the ASCII apostrophes the server writes
 * (a curly `’` would read identically and compare unequal) and gives the server
 * test something exact to assert, so the day someone rewords the printed sheet
 * the screen fails a test instead of quietly disagreeing with the document a
 * clinician signed.
 *
 * NOTHING MEDICO-LEGAL IS IN HERE. The heading, the AI-drafted label and the
 * no-clinical-authority disclaimer are the server's constants and are printed
 * from the payload, never from this file. There must be exactly one wording of
 * each in the product and exactly one place to change it.
 */
export const ASSESSMENT_COPY = {
  reproduced: 'Reproduced here exactly as it was written; it has not been re-run.',
  snapshotLead: 'It describes that moment, not the patient\'s state now: ',
  snapshotTail: '. Check Orb\'s current score before acting on anything here.',
  whatsHappening: 'What Sage said was happening',
  steps: 'Steps Sage suggested (suggestions only, none of them done)',
  watch: 'What Sage could not see. Establish these at the bedside',
  orbCanAct:
    'Orb has a one-click card ready for this; it does nothing until a clinician approves it.',
  confidenceLead: '. Sage\'s own confidence: ',
} as const;
