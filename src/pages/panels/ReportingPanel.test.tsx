import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ReportingPanel from './ReportingPanel'
import * as api from '../../api/reporting'
import type { ReportCatalog, ReportResult } from '../../api/reporting'

vi.mock('../../api/reporting', () => ({
  deleteReportDefinition: vi.fn(), exportReport: vi.fn(), getReportCatalog: vi.fn(), getReportDefinitions: vi.fn(), runReport: vi.fn(), saveReportDefinition: vi.fn(),
}))
const catalog: ReportCatalog = {
  datasets: [{ id: 'patients', label: 'Patient cohort', fields: [{ id: 'patient_id', kind: 'text', groupable: false }, { id: 'patient_name', kind: 'text', groupable: false }, { id: 'sex', kind: 'text', groupable: true }], default_columns: ['patient_id', 'patient_name'], measures: ['count'], date_required: false, cohort_problem_filter: true }],
  templates: [{ name: 'Count visible patients', query: { dataset: 'patients', mode: 'summary', measures: ['count'] } }],
  limits: { filters: 10, columns: 12, groups: 2, rows: 500, days: 366 }, notes: [],
}
const result: ReportResult = { dataset: 'patients', mode: 'rows', columns: ['patient_name'], rows: [{ patient_name: 'Synthetic patient result' }], truncated: false, limit: 200, period: null, scope: { department: null, facilities: null } }
async function ready() { render(<ReportingPanel />); await screen.findByLabelText('Dataset') }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.getReportCatalog).mockResolvedValue(catalog); vi.mocked(api.getReportDefinitions).mockResolvedValue([]); vi.mocked(api.runReport).mockResolvedValue(result); vi.mocked(api.exportReport).mockResolvedValue(undefined) })
afterEach(cleanup)

describe('Reports & cohorts', () => {
  it('states module failures as unavailable, without an empty census', async () => {
    vi.mocked(api.getReportCatalog).mockRejectedValue(new Error('Reporting is disabled at this site.'))
    render(<ReportingPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('disabled')
    expect(screen.queryByText('No records match these filters.')).not.toBeInTheDocument()
  })
  it('clears the previous patient result immediately when the query changes', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
    await screen.findByText('Synthetic patient result')
    fireEvent.change(screen.getByLabelText('Maximum rows'), { target: { value: '100' } })
    expect(screen.queryByText('Synthetic patient result')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled()
  })
  it('ignores a pending response after the query has changed', async () => {
    let finish!: (value: ReportResult) => void
    vi.mocked(api.runReport).mockReturnValue(new Promise(resolve => { finish = resolve }))
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
    fireEvent.change(screen.getByLabelText('Maximum rows'), { target: { value: '50' } })
    await act(async () => { finish(result) })
    expect(screen.queryByText('Synthetic patient result')).not.toBeInTheDocument()
  })
  it('discloses truncation and prevents a partial export', async () => {
    vi.mocked(api.runReport).mockResolvedValue({ ...result, truncated: true })
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
    expect(await screen.findByText(/This preview is truncated/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled()
  })
  it('exports the selected validated query through the session-bound API', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
    await screen.findByText('Synthetic patient result'); fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(api.exportReport).toHaveBeenCalledWith({ dataset: 'patients', mode: 'rows', limit: 200 }))
  })
  it('removes an earlier result after a server permission refusal', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Preview report' })); await screen.findByText('Synthetic patient result')
    vi.mocked(api.runReport).mockRejectedValue(new Error('Permission denied'))
    fireEvent.click(screen.getByRole('button', { name: 'Preview report' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Permission denied')
    expect(screen.queryByText('Synthetic patient result')).not.toBeInTheDocument()
  })
  it('loads template queries and saves no result payload', async () => {
    await ready(); fireEvent.change(screen.getByLabelText('Start from a template'), { target: { value: '0' } })
    expect(screen.getByLabelText('Report view')).toHaveValue('summary')
    fireEvent.change(screen.getByLabelText('Definition name'), { target: { value: 'Synthetic reusable count' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save definition' }))
    await waitFor(() => expect(api.saveReportDefinition).toHaveBeenCalledWith('Synthetic reusable count', catalog.templates[0].query, undefined))
  })
})
