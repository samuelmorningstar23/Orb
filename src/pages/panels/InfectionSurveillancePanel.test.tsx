import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import InfectionSurveillancePanel from './InfectionSurveillancePanel'
import * as api from '../../api/infectionSurveillance'

vi.mock('../../api/client', () => ({getUser: () => ({id: 'TEST-ADMIN', role: 'admin'})}))
vi.mock('../../components/OfflinePatientPicker', () => ({default: ({onChange}: {onChange: (id: string) => void}) => <button type="button" onClick={() => onChange('PAT-TEST')}>Select synthetic patient</button>}))
vi.mock('../../api/infectionSurveillance', () => ({loadInfectionSurveillance: vi.fn(), getInfectionWindow: vi.fn(), getInfectionIsolation: vi.fn(), createInfectionRecord: vi.fn(), classifyInfectionCase: vi.fn(), voidInfectionRecord: vi.fn(), endInfectionIsolation: vi.fn(), actOnInfectionWindow: vi.fn(), getInfectionMetrics: vi.fn()}))

const definition: api.InfectionDefinition = {id: 'DEF-TEST', code: 'DEF', name: 'Synthetic definition', case_definition: 'Hospital-authored definition TEST', counting_policy: 'Hospital counting policy TEST', denominator_unit: 'device_days', device_label: 'Device TEST', evidence_reference: 'Approved source TEST'}
const window: api.InfectionWindowDetail = {id: 'WIN-TEST', definition_id: definition.id, label: 'Synthetic window', population_description: 'Explicit population TEST', start_date: '2026-09-01', end_date: '2026-09-30', source_reference: 'Source TEST', rate_multiplier: 1000, minimum_cell: 3, isolate_policy: 'first_per_patient', status: 'open', version: 1, finalization_note: null, definition, cases: [], exposures: [], susceptibility: [], offset: 0, limit: 100, has_more: false, events: []}
const metrics: api.InfectionMetrics = {window_id: window.id, definition, population: window.population_description, start_date: window.start_date, end_date: window.end_date, minimum_cell: 3, isolate_policy: 'first_per_patient', group_by: ['organism', 'antibiotic'], notice: 'Validated manual source records only', rate: {suppressed: true, numerator: null, denominator: null, multiplier: 1000, value: null}, antibiogram: [{organism: 'ORG-TEST', antibiotic: 'AB-TEST', specimen: null, standard_reference: 'Standard edition TEST', method: 'Method TEST', suppressed: true, tested: null, counts: null, susceptible_percent: null}]}
function deferred<T>() {let resolve!: (value: T) => void; const promise = new Promise<T>(res => {resolve = res}); return {promise, resolve}}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.loadInfectionSurveillance).mockResolvedValue({definitions: [definition], windows: [window], isolations: [], offset: 0, limit: 100, has_more: false, notice: 'Local only'})
  vi.mocked(api.getInfectionWindow).mockResolvedValue(window)
  vi.mocked(api.getInfectionMetrics).mockResolvedValue(metrics)
})

it('requires explicit hospital multiplier, minimum and isolate policy instead of supplying defaults', async () => {
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByText('Open an observation window'))
  expect(screen.getByLabelText('Hospital-selected rate multiplier (whole number)')).toHaveValue(null)
  expect(screen.getByLabelText('Hospital-selected minimum cell (whole number)')).toHaveValue(null)
  expect(screen.getByLabelText('Hospital-selected isolate counting policy')).toHaveValue('')
  expect(api.getInfectionMetrics).not.toHaveBeenCalled()
})

it('keeps calculations unavailable for an open observation window', async () => {
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByRole('button', {name: 'Synthetic window · open'}))
  expect(await screen.findByText(/Rates and antibiogram results remain unavailable/)).toBeInTheDocument()
  expect(api.getInfectionMetrics).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Population / denominator completeness attestation')).toBeRequired()
})

it('records exactly one observed device day with the selected patient and evidence', async () => {
  vi.mocked(api.createInfectionRecord).mockResolvedValue({id: 'EXPOSURE-TEST'})
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByRole('button', {name: 'Synthetic window · open'}))
  fireEvent.click(await screen.findByText('Record source evidence in this window'))
  fireEvent.change(screen.getByLabelText('Source record type'), {target: {value: 'exposure'}})
  const patientButtons = screen.getAllByRole('button', {name: 'Select synthetic patient'})
  fireEvent.click(patientButtons[patientButtons.length - 1])
  fireEvent.change(screen.getByLabelText('Observed / specimen date (hospital calendar)'), {target: {value: '2026-09-02'}})
  fireEvent.change(screen.getByLabelText('Distinct observed device reference'), {target: {value: 'DEVICE-TEST'}})
  fireEvent.change(screen.getByLabelText('Source evidence reference'), {target: {value: 'Daily ledger TEST'}})
  fireEvent.click(screen.getByRole('button', {name: 'Record source evidence'}))
  await waitFor(() => expect(api.createInfectionRecord).toHaveBeenCalledWith('exposure', {window_id: 'WIN-TEST', patient_id: 'PAT-TEST', observed_date: '2026-09-02', device_reference: 'DEVICE-TEST', day_count: 1, evidence_reference: 'Daily ledger TEST'}, expect.any(String)))
})

it('renders suppressed cells without revealing counts or an inferred susceptible percentage', async () => {
  const final = {...window, status: 'finalized' as const}
  vi.mocked(api.getInfectionWindow).mockResolvedValue(final)
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByRole('button', {name: 'Synthetic window · open'}))
  expect(await screen.findByText('Surveillance rate suppressed by the hospital minimum-cell policy.')).toBeInTheDocument()
  expect(screen.getAllByText('Suppressed')).toHaveLength(3)
  expect(screen.queryByText(/Recorded included cases:/)).not.toBeInTheDocument()
  expect(screen.queryByText(/null%/)).not.toBeInTheDocument()
})

it('discards an older aggregate response when its grouping changes', async () => {
  const first = deferred<api.InfectionMetrics>(), second = deferred<api.InfectionMetrics>()
  vi.mocked(api.getInfectionWindow).mockResolvedValue({...window, status: 'finalized'})
  vi.mocked(api.getInfectionMetrics).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByRole('button', {name: 'Synthetic window · open'}))
  fireEvent.change(await screen.findByLabelText('Antibiogram grouping'), {target: {value: 'organism,antibiotic,specimen'}})
  await act(async () => {second.resolve({...metrics, population: 'Current grouped population TEST'}); await second.promise})
  expect(await screen.findByText(/Current grouped population TEST/)).toBeInTheDocument()
  await act(async () => {first.resolve({...metrics, population: 'Stale old population TEST'}); await first.promise})
  expect(screen.queryByText(/Stale old population TEST/)).not.toBeInTheDocument()
})

it('shows the source-change refusal and no cached calculation', async () => {
  vi.mocked(api.getInfectionWindow).mockResolvedValue({...window, status: 'finalized'})
  vi.mocked(api.getInfectionMetrics).mockRejectedValue(new Error('Retained source records changed after finalization.'))
  render(<InfectionSurveillancePanel />)
  fireEvent.click(await screen.findByRole('button', {name: 'Synthetic window · open'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Retained source records changed')
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
})
