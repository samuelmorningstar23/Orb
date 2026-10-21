/**
 * The nurses' shift notes on the printed inpatient record.
 *
 * Print record lists the charted documents and the signed Scribe notes oldest
 * first (VigilPanel.signedNotes); this merges the nursing notes into that
 * list in time order, each titled with its shift and author, so a day's
 * record reads in the order it was written. Display only.
 */
import { parseUTC } from './datetime'

export interface RecordNoteLike {
  title: string
  author?: string | null
  at?: string | null
  text: string
}

export interface NursingNoteLike {
  author_id?: string | null
  author_name?: string | null
  shift_label?: string | null
  content?: unknown
  written_at?: string | null
}

function timeOf(at: string | null | undefined): number {
  return at ? parseUTC(at).getTime() : 0
}

/** One printed entry per nursing note with text; a note the record cannot
 *  read (no content) is left out rather than printed blank. */
export function nursingNoteRecordEntries(notes: readonly NursingNoteLike[] | null | undefined): RecordNoteLike[] {
  const out: RecordNoteLike[] = []
  for (const n of notes ?? []) {
    if (!n || typeof n.content !== 'string' || !n.content.trim()) continue
    out.push({
      title: `Nursing note (${n.shift_label || 'shift not recorded'})`,
      author: n.author_name || n.author_id || null,
      at: n.written_at ?? null,
      text: n.content,
    })
  }
  return out
}

/** The record's notes with the nursing notes merged in, oldest first. */
export function withNursingNotes<T extends RecordNoteLike>(
  recordNotes: readonly T[],
  nursingNotes: readonly NursingNoteLike[] | null | undefined,
): RecordNoteLike[] {
  const merged: RecordNoteLike[] = [...recordNotes, ...nursingNoteRecordEntries(nursingNotes)]
  return merged
    .map((n, i) => ({ n, i, t: timeOf(n.at) }))
    .sort((a, b) => a.t - b.t || a.i - b.i)
    .map(x => x.n)
}
