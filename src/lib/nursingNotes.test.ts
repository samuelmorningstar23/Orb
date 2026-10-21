/**
 * The nurses' shift notes on the printed inpatient record (21 October 2026):
 * merged with the signed notes in time order, titled with the shift and the
 * author, and a note without text is left out rather than printed blank.
 */
import { describe, expect, it } from 'vitest'
import { nursingNoteRecordEntries, withNursingNotes } from './nursingNotes'

const NOTE = {
  author_id: 'NUR001', author_name: 'Sister Mary Thomas', shift_label: '07:00 to 15:00',
  content: 'Settled after analgesia.', written_at: '2026-10-21T04:20:00+00:00',
}

describe('nursingNoteRecordEntries', () => {
  it('titles each note with its shift and names the author', () => {
    expect(nursingNoteRecordEntries([NOTE])).toEqual([{
      title: 'Nursing note (07:00 to 15:00)', author: 'Sister Mary Thomas',
      at: '2026-10-21T04:20:00+00:00', text: 'Settled after analgesia.',
    }])
  })

  it('falls back to the author id and leaves out a note without text', () => {
    const entries = nursingNoteRecordEntries([
      { ...NOTE, author_name: null }, { ...NOTE, content: '   ' }, { ...NOTE, content: undefined },
    ])
    expect(entries).toHaveLength(1)
    expect(entries[0].author).toBe('NUR001')
  })

  it('reads an absent or empty list as no entries', () => {
    expect(nursingNoteRecordEntries(undefined)).toEqual([])
    expect(nursingNoteRecordEntries(null)).toEqual([])
    expect(nursingNoteRecordEntries([])).toEqual([])
  })
})

describe('withNursingNotes', () => {
  it('merges the notes into the record oldest first', () => {
    const record = [
      { title: 'Scribe note (soap), signed', at: '2026-10-21T06:00:00+00:00', text: 'Doctor.' },
      { title: 'Progress note', at: '2026-10-21T02:00:00+00:00', text: 'Earlier.' },
    ]
    const merged = withNursingNotes(record, [NOTE])
    expect(merged.map(n => n.title)).toEqual([
      'Progress note', 'Nursing note (07:00 to 15:00)', 'Scribe note (soap), signed',
    ])
  })

  it('leaves the record exactly as it was when there are no notes', () => {
    const record = [{ title: 'Progress note', at: '2026-10-21T02:00:00+00:00', text: 'Earlier.' }]
    expect(withNursingNotes(record, [])).toEqual(record)
    expect(withNursingNotes(record, undefined)).toEqual(record)
  })
})
