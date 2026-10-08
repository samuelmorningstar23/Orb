/**
 * The Worklists view and its entry grid (lis-10). What they must never get
 * wrong:
 *
 *   · The bench keeps the server's order (STAT first, then the oldest
 *     receipt) and groups tests under their sample.
 *   · A value with more decimal places than the test reports is refused on
 *     the screen, in words, and never sent; a value is sent as typed.
 *   · The flag shown is the server's, in words; no range says so.
 *   · A coded test offers only its own values.
 *   · A test cancelled after receipt says who cancelled it and when.
 *   · A change says what its row showed; one refused because someone else
 *     changed the result first reloads the bench and draws the row again.
 *   · A failed load is never an empty bench.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { Worklist, WorklistItem } from '../../../api/lab/results'
import { discoverViews } from './viewRegistry'
import WorklistView, * as worklistModule from './WorklistView'

vi.mock('../../../api/lab/results', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/results')>(
    '../../../api/lab/results')
  return {
    ...actual,
    listWorklists: vi.fn(),
    getWorklist: vi.fn(),
    enterLabResult: vi.fn(),
    editLabResult: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/results'))

const ASHA = { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A' }
const RAVI = { id: 'P-RAVI', name: 'Ravi Kumar', mrn: 'MRN-0077', ward: 'CARD-1', bed: '2B' }

function item(over: Partial<WorklistItem> = {}): WorklistItem {
  return {
    item_id: 1,
    order_id: 'O-1',
    specimen_id: '261006000127',
    priority: 'routine',
    status: 'received',
    patient: ASHA,
    test: { id: 'LOINC:2823-3', name: 'Potassium', short_name: 'K', result_type: 'numeric',
      unit: 'mmol/L', decimal_places: 1, coded_values: [] },
    received_at: '2026-10-06T04:00:00.000000+00:00',
    collected_at: '2026-10-06T03:30:00.000000+00:00',
    tat_minutes: null,
    range: { id: 7, text: '3.5 to 5.1 mmol/L', configured: true },
    result: null,
    ...over,
  }
}

function bench(items: WorklistItem[], over: Partial<Worklist> = {}): Worklist {
  return { section: { id: 'biochemistry', name: 'Biochemistry' }, items, cancelled: [],
    scope: { department: null, hospital_wide: true }, ...over }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

beforeEach(() => {
  api.listWorklists.mockResolvedValue({ sections: [
    { id: 'haematology', name: 'Haematology', waiting: 0, entered: 0, stat_waiting: 0 },
    { id: 'biochemistry', name: 'Biochemistry', waiting: 2, entered: 0, stat_waiting: 1 },
  ] })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('WorklistView', () => {
  it('declares itself to the panel as the worklists tab', () => {
    const [entry] = discoverViews({ './lab/WorklistView.tsx': worklistModule })
    expect(entry).toMatchObject({ id: 'worklists', label: 'Worklists', order: 30,
      permission: 'lab.result', whenDraining: 'show' })
  })

  it('opens the first section with work and keeps the server\'s order, STAT first', async () => {
    api.getWorklist.mockResolvedValue(bench([
      item({ item_id: 9, specimen_id: '261006000135', priority: 'stat', patient: RAVI }),
      item(),
    ]))
    render(<WorklistView {...props} />)
    await waitFor(() => expect(api.getWorklist).toHaveBeenCalledWith('biochemistry'))
    const table = await screen.findByRole('table', { name: 'Result entry' })
    // One header row per sample, then its tests.
    expect(table.querySelectorAll('tr.lab-bench-group')).toHaveLength(2)
    const rows = within(table).getAllByRole('row').map(r => r.textContent ?? '')
    const ravi = rows.findIndex(t => t.includes('Ravi Kumar'))
    const asha = rows.findIndex(t => t.includes('Asha Menon'))
    expect(ravi).toBeGreaterThan(-1)
    expect(ravi).toBeLessThan(asha)
    expect(rows[ravi]).toContain('STAT')
  })

  it('refuses too many decimal places on the screen and sends nothing', async () => {
    api.getWorklist.mockResolvedValue(bench([item()]))
    render(<WorklistView {...props} />)
    const box = await screen.findByLabelText('Result, Potassium for Asha Menon')
    fireEvent.change(box, { target: { value: '4.56' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Potassium is reported in 1 decimal place')
    expect(api.enterLabResult).not.toHaveBeenCalled()
  })

  it('sends the value as typed and shows the server\'s flag in words', async () => {
    api.getWorklist.mockResolvedValue(bench([item()]))
    api.enterLabResult.mockResolvedValue({ result: {
      id: 41, order_item_id: 1, test_id: 'LOINC:2823-3', status: 'entered', version: 1,
      value_text: '5.2', value_milli: 5200, decimal_places: 1, comparator: null,
      unit: 'mmol/L', abnormal_flag: 'H', reference_range_id: 7,
      reference_range_text: '3.5 to 5.1 mmol/L', comment: null, entered_by_name: 'Tech B',
      entered_at: '2026-10-06T05:00:00.000000+00:00', flag_text: 'High' } })
    render(<WorklistView {...props} />)
    const box = await screen.findByLabelText('Result, Potassium for Asha Menon')
    fireEvent.change(box, { target: { value: '5.20' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.enterLabResult).toHaveBeenCalledWith(1, '5.20', undefined))
    expect(await screen.findByText('High')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save change' })).toBeTruthy()
  })

  it('says when no range is configured', async () => {
    api.getWorklist.mockResolvedValue(bench([item({
      range: { id: null, text: 'Reference range not configured: date of birth not recorded',
        configured: false } })]))
    render(<WorklistView {...props} />)
    expect(await screen.findByText(
      'Reference range not configured: date of birth not recorded')).toBeTruthy()
  })

  it('offers a coded test only its own values', async () => {
    api.getWorklist.mockResolvedValue(bench([item({ test: { id: 'LOCAL:HBS',
      name: 'HBsAg', short_name: 'HBsAg', result_type: 'coded', unit: '', decimal_places: 0,
      coded_values: ['Reactive', 'Non-reactive'] }, range: { id: null, text: null, configured: false } })]))
    render(<WorklistView {...props} />)
    const select = await screen.findByLabelText('Result, HBsAg for Asha Menon') as HTMLSelectElement
    expect([...select.options].map(o => o.textContent)).toEqual(['Choose', 'Reactive', 'Non-reactive'])
  })

  it('says who cancelled a test after receipt, and when', async () => {
    api.getWorklist.mockResolvedValue(bench([], { cancelled: [{ item_id: 3, order_id: 'O-3',
      specimen_id: '261006000127', test_name: 'Potassium', patient: { id: 'P-ASHA',
        name: 'Asha Menon', mrn: 'MRN-0042' }, cancelled_by_name: 'Nurse Priya',
      cancelled_at: '2026-10-06T05:00:00.000000+00:00', reason: 'Going to theatre' }] }))
    render(<WorklistView {...props} />)
    expect(await screen.findByText('Cancelled after the sample was received')).toBeTruthy()
    expect(screen.getByText(/Potassium for Asha Menon \(261006 00012 7\): cancelled by Nurse Priya at/))
      .toBeTruthy()
    expect(screen.getByText(/Nothing is waiting on the biochemistry bench/)).toBeTruthy()
  })

  it('reloads and shows the other person\'s result when they entered it first', async () => {
    const theirs = {
      id: 52, status: 'entered', version: 1, value_text: '4.8', comparator: null,
      unit: 'mmol/L', abnormal_flag: '', reference_range_id: 7,
      reference_range_text: '3.5 to 5.1 mmol/L', comment: null, entered_by_name: 'Tech C',
      entered_at: '2026-10-06T05:00:00.000000+00:00', flag_text: 'Within range',
    }
    api.getWorklist
      .mockResolvedValueOnce(bench([item()]))
      .mockResolvedValueOnce(bench([item({ status: 'resulted', result: theirs })]))
    api.enterLabResult.mockRejectedValue(new LabApiError(
      'A result is already entered for this test. Edit it instead.', 409, 'result_exists'))
    render(<WorklistView {...props} />)
    const box = await screen.findByLabelText('Result, Potassium for Asha Menon')
    fireEvent.change(box, { target: { value: '4.2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(
      'Potassium for Asha Menon: A result is already entered for this test. Edit it instead. '
      + 'The worklist has been reloaded.')).toBeTruthy()
    await waitFor(() => expect(api.getWorklist).toHaveBeenCalledTimes(2))
    // The row is drawn again from the server's copy, ready to be changed.
    await waitFor(() => expect(
      (screen.getByLabelText('Result, Potassium for Asha Menon') as HTMLInputElement).value)
      .toBe('4.8'))
    expect(screen.getByText('Within range')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save change' })).toBeTruthy()
  })

  const MINE = {
    id: 41, status: 'entered', version: 1, value_text: '4.5', comparator: null,
    unit: 'mmol/L', abnormal_flag: '', reference_range_id: 7,
    reference_range_text: '3.5 to 5.1 mmol/L', comment: null, entered_by_name: 'Tech A',
    entered_at: '2026-10-06T05:00:00.000000+00:00', flag_text: 'Within range',
  }

  it('sends what the row showed with a change, and reloads when someone changed it first',
    async () => {
      const theirs = { ...MINE, value_text: '5.5', abnormal_flag: 'H', flag_text: 'High' }
      api.getWorklist
        .mockResolvedValueOnce(bench([item({ status: 'resulted', result: MINE })]))
        .mockResolvedValueOnce(bench([item({ status: 'resulted', result: theirs })]))
      const message = 'This result has changed since your screen loaded: it now reads 5.5 '
        + '(last saved by Tech B). Nothing was saved. Check it and make your change again.'
      api.editLabResult.mockRejectedValue(new LabApiError(message, 409, 'result_changed',
        { error: 'result_changed', message, current: theirs }))
      render(<WorklistView {...props} />)
      // Only the comment is changed; the row still shows 4.5.
      const note = await screen.findByLabelText('Comment, Potassium for Asha Menon')
      fireEvent.change(note, { target: { value: 'lipaemic' } })
      fireEvent.click(screen.getByRole('button', { name: 'Save change' }))
      await waitFor(() => expect(api.editLabResult).toHaveBeenCalledWith(
        41, '4.5', { value_text: '4.5', comment: null, abnormal_flag: '' },
        { comment: 'lipaemic' }))
      expect(await screen.findByText(`Potassium for Asha Menon: ${message} `
        + 'The worklist has been reloaded.')).toBeTruthy()
      await waitFor(() => expect(api.getWorklist).toHaveBeenCalledTimes(2))
      // The row is drawn again from the other person's result, and the
      // comment typed against the old one is not kept.
      await waitFor(() => expect(
        (screen.getByLabelText('Result, Potassium for Asha Menon') as HTMLInputElement).value)
        .toBe('5.5'))
      expect((screen.getByLabelText('Comment, Potassium for Asha Menon') as HTMLInputElement)
        .value).toBe('')
      expect(screen.getByText('High')).toBeTruthy()
      expect(api.editLabResult).toHaveBeenCalledTimes(1)
    })

  it('draws a row again when only its flag changed on the server', async () => {
    const reflagged = { ...MINE, abnormal_flag: 'H', flag_text: 'High' }
    api.getWorklist
      .mockResolvedValueOnce(bench([item({ status: 'resulted', result: MINE })]))
      .mockResolvedValueOnce(bench([item({ status: 'resulted', result: reflagged })]))
    api.editLabResult.mockRejectedValue(new LabApiError(
      'This result has changed since your screen loaded.', 409, 'result_changed'))
    render(<WorklistView {...props} />)
    const box = await screen.findByLabelText('Result, Potassium for Asha Menon')
    expect(screen.getByText('Within range')).toBeTruthy()
    fireEvent.change(box, { target: { value: '4.6' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save change' }))
    await waitFor(() => expect(api.getWorklist).toHaveBeenCalledTimes(2))
    // The next change would say it saw the High flag, not the old one.
    expect(await screen.findByText('High')).toBeTruthy()
    expect(screen.queryByText('Within range')).toBeNull()
    expect((screen.getByLabelText('Result, Potassium for Asha Menon') as HTMLInputElement).value)
      .toBe('4.5')
  })

  it('never shows a failed load as an empty bench', async () => {
    api.getWorklist.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    render(<WorklistView {...props} />)
    expect((await screen.findByRole('alert')).textContent).toContain('not an empty bench')
    expect(screen.queryByText(/Nothing is waiting/)).toBeNull()
  })
})
