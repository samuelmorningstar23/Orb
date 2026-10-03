import { describe, expect, it } from 'vitest'
import { summaryRows } from './ambientSummary'

describe('summaryRows', () => {
  it('splits the running summary into one row per update', () => {
    expect(summaryRows('Day 3 CAP with AKI. Creatinine up to 2.4. ECG at 07:52 shows no peaked T waves.'))
      .toEqual([
        { text: 'Day 3 CAP with AKI.', correction: false },
        { text: 'Creatinine up to 2.4.', correction: false },
        { text: 'ECG at 07:52 shows no peaked T waves.', correction: false },
      ])
  })

  it('labels the server retraction note as a retraction and keeps later narrative', () => {
    const rows = summaryRows(
      'Potassium 6.8, critical. Insulin-dextrose given. RETRACTED by ward message: "the potassium of 6.8 was a haemolysed sample, repeat 4.1" Plan to recheck at 14:00.',
    )
    expect(rows).toEqual([
      { text: 'Potassium 6.8, critical.', correction: false },
      { text: 'Insulin-dextrose given.', correction: false },
      { text: 'Retracted by ward message: "the potassium of 6.8 was a haemolysed sample, repeat 4.1"', correction: true },
      { text: 'Plan to recheck at 14:00.', correction: false },
    ])
  })

  it('never marks a confirmed or negated sentence as a correction', () => {
    // These read like retractions to a keyword match and say the opposite.
    expect(summaryRows('Repeat sample not haemolysed, potassium 6.9 confirmed.'))
      .toEqual([{ text: 'Repeat sample not haemolysed, potassium 6.9 confirmed.', correction: false }])
    expect(summaryRows('Do not disregard the troponin rise.'))
      .toEqual([{ text: 'Do not disregard the troponin rise.', correction: false }])
  })

  it('leaves model-written wording unmarked: only the server note is a correction', () => {
    const rows = summaryRows('Potassium 6.8 is retracted, haemolysed sample. Repeat 4.1 is normal.')
    expect(rows.map(r => r.correction)).toEqual([false, false])
  })

  it('does not split a name after a title or an initial', () => {
    expect(summaryRows('Seen by Dr. Sharma at 08:00. Plan reviewed by Dr. A. Rao.').map(r => r.text))
      .toEqual(['Seen by Dr. Sharma at 08:00.', 'Plan reviewed by Dr. A. Rao.'])
  })

  it('returns nothing for an empty summary', () => {
    expect(summaryRows('')).toEqual([])
    expect(summaryRows(null)).toEqual([])
  })
})
