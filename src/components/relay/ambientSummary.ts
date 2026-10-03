/**
 * The Sage room summary as rows a clinician can scan.
 *
 * The server keeps the summary as one running string: each classified update is
 * appended with a space, and a retraction is appended as
 *   RETRACTED by ward message: "<the clinician's words>"
 * Shown as one paragraph, a withdrawn value ("potassium 6.8, critical") read as
 * current, with the correction trailing several sentences later. Each sentence
 * is its own row here, and the server's retraction note is its own labelled row.
 *
 * Only that note is marked. The server writes it only when it actually retired
 * a flag; guessing from the model's wording marked "Repeat sample not
 * haemolysed, potassium 6.9 confirmed" as withdrawn, the opposite of the truth.
 */

export interface SummaryRow {
  text: string
  /** The server's retraction note: a ward message that withdrew an earlier value. */
  correction: boolean
}

const RETRACTION_NOTE = 'RETRACTED by ward message:'

// Short forms that end in a full stop mid-sentence: "Seen by Dr. Sharma",
// "e.g. paracetamol", and an initial as in "Dr. A. Sharma". Not splitting
// there costs a longer row; splitting cuts a name in half.
const ABBREVIATION = /(?:^|[\s(])(?:dr|mr|mrs|ms|prof|sr|st|no|vs|approx|e\.g|i\.e)$/i
const INITIAL = /(?:^|\s)[A-Z]$/

// Split after . ! or ? when the next sentence starts with a capital or digit.
// A loop rather than a lookbehind: older ward-tablet Safari cannot parse one.
function sentences(text: string): string[] {
  const out: string[] = []
  const boundary = /[.!?]\s+(?=["'(]?[A-Z0-9])/g
  let start = 0
  for (let m = boundary.exec(text); m; m = boundary.exec(text)) {
    const before = text.slice(start, m.index)
    if (text[m.index] === '.' && (ABBREVIATION.test(before) || INITIAL.test(before))) continue
    out.push(text.slice(start, m.index + 1))
    start = m.index + m[0].length
  }
  out.push(text.slice(start))
  return out.map(s => s.trim()).filter(Boolean)
}

export function summaryRows(summary: string | null | undefined): SummaryRow[] {
  const text = (summary || '').trim()
  if (!text) return []
  const rows: SummaryRow[] = []
  const pushNarrative = (chunk: string) => {
    for (const s of sentences(chunk)) rows.push({ text: s, correction: false })
  }

  const [head, ...notes] = text.split(RETRACTION_NOTE)
  pushNarrative(head)
  for (const note of notes) {
    const body = note.trim()
    // The quoted excerpt ends at a closing quote followed by a space or the end;
    // anything after it is later narrative.
    const m = /^"([\s\S]*?)"(?=\s|$)/.exec(body)
    const excerpt = (m ? m[1] : body).trim()
    if (excerpt) rows.push({ text: `Retracted by ward message: "${excerpt}"`, correction: true })
    if (m) pushNarrative(body.slice(m[0].length))
  }
  return rows
}
