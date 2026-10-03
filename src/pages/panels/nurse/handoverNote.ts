/* The nurse's own words inside a saved I-PASS handover note, kept apart from
   HandoverSheet.tsx so that file exports only components (fast refresh). */

/** The heading the nurse's own words are saved under, inside the handover
 *  note. One constant for the writer (HandoverSheet's save) and the reader. */
export const HANDOVER_NOTE_HEADING = 'HANDOVER NOTE: '

/** The outgoing nurse's own words from a saved handover, or null when she
 *  added none. The auto-drafted I-PASS above the heading is not hers. */
export function handoverNoteFrom(structuredNote: string | null | undefined): string | null {
  const text = structuredNote || ''
  const at = text.lastIndexOf(`\n${HANDOVER_NOTE_HEADING}`)
  if (at < 0) return null
  // Line 1 is "HANDOVER NOTE: <name>, <time>"; her note is everything after.
  const block = text.slice(at + 1)
  const firstBreak = block.indexOf('\n')
  if (firstBreak < 0) return null
  return block.slice(firstBreak + 1).trim() || null
}
