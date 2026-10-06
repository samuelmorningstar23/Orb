/**
 * THE WARD RECORD SECTION SETS EACH WARD'S INTERVAL AND ASKS FOR THE FILE.
 *
 * It shows every ward with its interval and grace or "Not set", saves a
 * change with who set it in writing, asks for the rounds file for a whole
 * calendar month, and shows the server's refusal (a ward with no interval)
 * in words rather than a blank.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getWardSettings = vi.fn()
const saveWardSetting = vi.fn()
const fetchRoundsFile = vi.fn()
const saveTextFile = vi.fn()

vi.mock('../../../api/wardRecord', async () => {
  const actual = await vi.importActual<typeof import('../../../api/wardRecord')>('../../../api/wardRecord')
  return {
    ...actual,
    getWardSettings: (...a: unknown[]) => getWardSettings(...a),
    saveWardSetting: (...a: unknown[]) => saveWardSetting(...a),
    fetchRoundsFile: (...a: unknown[]) => fetchRoundsFile(...a),
    saveTextFile: (...a: unknown[]) => saveTextFile(...a),
  }
})

import WardRecordSection from './WardRecordSection'
import { monthRange } from '../../../api/wardRecord'

const LIMITS = { interval_minutes: [30, 1440], grace_minutes: [0, 240], default_grace_minutes: 30 }

beforeEach(() => {
  getWardSettings.mockReset()
  saveWardSetting.mockReset()
  fetchRoundsFile.mockReset()
  saveTextFile.mockReset()
  getWardSettings.mockResolvedValue({
    wards: [
      {
        ward: 'GM-1',
        setting: {
          ward: 'GM-1', interval_minutes: 240, grace_minutes: 30,
          set_by: 'Sister Thomas', set_at: '2026-10-01T04:30:00+00:00', note: null,
        },
      },
      { ward: 'GM-2', setting: null },
    ],
    limits: LIMITS,
  })
})

describe('WardRecordSection', () => {
  it('shows each ward with its interval and grace, or Not set', async () => {
    render(<WardRecordSection />)
    expect(await screen.findByText('every 4 hours')).toBeInTheDocument()
    expect(screen.getByText('30 minutes')).toBeInTheDocument()
    expect(screen.getByText('Not set')).toBeInTheDocument()
    expect(screen.getByText(/Sister Thomas/)).toBeInTheDocument()
  })

  it('saves an interval with who set it in writing', async () => {
    saveWardSetting.mockResolvedValue({})
    render(<WardRecordSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Change the interval for GM-2' }))
    fireEvent.change(screen.getByLabelText('Observations due every (minutes)'), { target: { value: '360' } })
    fireEvent.change(screen.getByLabelText('Set in writing by'), { target: { value: 'Sister Thomas' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save interval' }))
    await waitFor(() => expect(saveWardSetting).toHaveBeenCalledWith('GM-2', {
      interval_minutes: 360, grace_minutes: 30, set_by: 'Sister Thomas', note: null,
    }))
    await waitFor(() => expect(getWardSettings).toHaveBeenCalledTimes(2))
  })

  it('will not save without the name of who set it', async () => {
    render(<WardRecordSection />)
    fireEvent.click(await screen.findByRole('button', { name: 'Change the interval for GM-2' }))
    fireEvent.change(screen.getByLabelText('Observations due every (minutes)'), { target: { value: '240' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save interval' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Say who set this in writing.')
    expect(saveWardSetting).not.toHaveBeenCalled()
  })

  it('downloads the rounds file for a whole month in either format', async () => {
    fetchRoundsFile.mockResolvedValue({
      fileName: 'rounds_GM_1_2026-09-01_2026-09-30.csv', text: 'Ward Record rounds file\n',
      reference: 'WR-20261006-ABCD1234', sha256: 'f'.repeat(64),
    })
    render(<WardRecordSection />)
    await screen.findByText('every 4 hours')
    fireEvent.change(screen.getByLabelText('Month'), { target: { value: '2026-09' } })
    fireEvent.click(screen.getByRole('button', { name: 'Download the rounds file (CSV)' }))
    await waitFor(() => expect(fetchRoundsFile).toHaveBeenCalledWith({
      ward: 'GM-1', start: '2026-09-01', end: '2026-09-30', format: 'csv',
      includePatientNumbers: false,
    }))
    expect(saveTextFile).toHaveBeenCalledWith('rounds_GM_1_2026-09-01_2026-09-30.csv',
      'Ward Record rounds file\n', 'text/csv;charset=utf-8')
    expect(await screen.findByRole('status')).toHaveTextContent('WR-20261006-ABCD1234')

    fireEvent.click(screen.getByRole('button', { name: 'Download the rounds file (Markdown)' }))
    await waitFor(() => expect(fetchRoundsFile).toHaveBeenLastCalledWith(
      expect.objectContaining({ format: 'md' })))
  })

  it('shows the refusal for a ward with no interval in words', async () => {
    fetchRoundsFile.mockRejectedValue(new Error("Set the ward's observation interval first"))
    render(<WardRecordSection />)
    await screen.findByText('every 4 hours')
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'GM-2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Download the rounds file (CSV)' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Set the ward's observation interval first")
    expect(saveTextFile).not.toHaveBeenCalled()
  })

  it('turns a month into its first and last day', () => {
    expect(monthRange('2026-02')).toEqual({ start: '2026-02-01', end: '2026-02-28' })
    expect(monthRange('2028-02')).toEqual({ start: '2028-02-01', end: '2028-02-29' })
    expect(monthRange('2026-13')).toBeNull()
  })
})
