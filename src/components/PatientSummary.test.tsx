import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PatientSummary } from './PatientSummary'

const { fetchSummary } = vi.hoisted(() => ({ fetchSummary: vi.fn() }))
vi.mock('../api/client', () => ({ apiFetch: fetchSummary }))

afterEach(() => { cleanup(); fetchSummary.mockReset(); vi.useRealTimers() })

function ready(patientId: string, text = 'Synthetic draft') {
  return {
    patient_id: patientId, status: 'ready', source_captured_at: '2026-10-21T08:00:00Z',
    summary: { overview: text, important_findings: ['A recorded finding'], outstanding_items: [], data_gaps: ['Selected records only'] },
  }
}

describe('Patient summary snapshots', () => {
  it('labels the snapshot as a selected-record AI draft with an as-of time', async () => {
    fetchSummary.mockResolvedValue(ready('p1'))
    render(<PatientSummary patientId="p1" />)
    expect(await screen.findByText('Synthetic draft')).toBeInTheDocument()
    expect(screen.getByText(/AI draft/)).toBeInTheDocument()
    expect(screen.getByText(/Records captured/)).toBeInTheDocument()
    expect(screen.getByText(/not the full patient history/)).toBeInTheDocument()
  })

  it('keeps an observationally outdated draft explicitly separate from current chart data', async () => {
    fetchSummary.mockResolvedValue({ ...ready('p1'), status: 'outdated', reason: 'new_observations' })
    render(<PatientSummary patientId="p1" />)
    expect(await screen.findByText('Synthetic draft')).toBeInTheDocument()
    expect(screen.getByText(/New observations have arrived/)).toBeInTheDocument()
  })

  it('withholds expired or clinically invalidated prose even if included in a malformed response', async () => {
    fetchSummary.mockResolvedValue({ ...ready('p1', 'Unsafe old draft'), status: 'unavailable', reason: 'source_changed' })
    render(<PatientSummary patientId="p1" />)
    expect(await screen.findByText(/The chart changed/)).toBeInTheDocument()
    expect(screen.queryByText('Unsafe old draft')).not.toBeInTheDocument()
  })

  it('never flashes another patient’s draft while a new patient is loading', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    fetchSummary.mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValueOnce(ready('p2', 'Second patient'))
    const view = render(<PatientSummary patientId="p1" />)
    view.rerender(<PatientSummary patientId="p2" />)
    expect(await screen.findByText('Second patient')).toBeInTheDocument()
    await act(async () => { resolveFirst(ready('p1', 'First patient')); await Promise.resolve() })
    expect(screen.queryByText('First patient')).not.toBeInTheDocument()
  })

  it('rejects a response carrying a different patient id', async () => {
    fetchSummary.mockResolvedValue(ready('wrong', 'Wrong patient'))
    render(<PatientSummary patientId="p1" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be checked/)
    expect(screen.queryByText('Wrong patient')).not.toBeInTheDocument()
  })

  it('hides the panel when the deployment has not enabled summary generation', async () => {
    fetchSummary.mockResolvedValue({ patient_id: 'p1', status: 'unavailable', reason: 'feature_disabled', summary: null })
    render(<PatientSummary patientId="p1" />)
    await waitFor(() => expect(screen.queryByLabelText('Patient summary')).not.toBeInTheDocument())
  })

  it('hides a draft at its source-age deadline without waiting for the next poll', async () => {
    vi.useFakeTimers()
    fetchSummary.mockResolvedValue({ ...ready('p1'), refresh_after_seconds: 0.05 })
    render(<PatientSummary patientId="p1" />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('Synthetic draft')).toBeInTheDocument()
    await act(async () => { await vi.advanceTimersByTimeAsync(51) })
    expect(screen.queryByText('Synthetic draft')).not.toBeInTheDocument()
    expect(screen.getByText(/previous draft has expired/)).toBeInTheDocument()
  })
})
