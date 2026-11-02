import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RecordGovernancePanel from './RecordGovernancePanel'
import RadiationSafetyPanel from './RadiationSafetyPanel'
import * as governance from '../../api/recordGovernance'
import { apiFetch } from '../../api/client'

vi.mock('../../api/client', () => ({apiFetch: vi.fn(), getUser: () => ({id: 'TEST-ADMIN', role: 'admin'})}))
vi.mock('../../api/recordGovernance', () => ({listGovernance: vi.fn(), getGovernance: vi.fn(), createGovernance: vi.fn(), transitionGovernance: vi.fn()}))
vi.mock('../../components/OfflinePatientPicker', () => ({default: ({onChange}: {onChange: (id: string) => void}) => <><button type="button" onClick={() => onChange('PAT-A')}>Select patient A</button><button type="button" onClick={() => onChange('PAT-B')}>Select patient B</button></>}))
function deferred<T>() {let resolve!: (value: T) => void; const promise = new Promise<T>(res => {resolve = res}); return {promise, resolve}}
const hold = (id: string, patient = 'PAT-A'): governance.GovernanceRecord => ({id, patient_id: patient, version: 1, created_at: '2026-10-01T00:00:00Z', reason: `Synthetic ${id}`, authority_reference: 'Paper TEST', events: []})
const asset = {id: 1, asset_tag: 'ASSET-1', name: 'Synthetic equipment', serial_number: 'SERIAL-1'}
const evidence = (id: string) => ({id, record_kind: 'licence', reference: `Synthetic ${id}`, authority: 'Source TEST', valid_from: '2026-01-01', valid_to: '2027-01-01', scope_description: `Scope of ${id}`, officer_name: null, qualification_reference: null, date_status: 'within_recorded_dates', state: 'recorded', version: 1, asset})
const register = {records: [evidence('RAD-A'), evidence('RAD-B')], total: 2, assets: [asset], asset_total: 51, asset_offset: 0, asset_limit: 50, asset_next_offset: 50}
beforeEach(() => {vi.resetAllMocks(); vi.mocked(governance.listGovernance).mockResolvedValue({records: [hold('HOLD-A'), hold('HOLD-B')], total: 2}); vi.mocked(apiFetch).mockResolvedValue(register)})

describe('medical record request ownership', () => {
  it('keeps the newer selected hold when an earlier detail arrives last', async () => {
    const first = deferred<governance.GovernanceRecord>(), second = deferred<governance.GovernanceRecord>()
    vi.mocked(governance.getGovernance).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    render(<RecordGovernancePanel />); fireEvent.click(screen.getByRole('button', {name: 'Select patient A'}))
    fireEvent.click(await screen.findByRole('button', {name: 'Synthetic HOLD-A · active'}))
    fireEvent.click(screen.getByRole('button', {name: 'Synthetic HOLD-B · active'}))
    await act(async () => {second.resolve(hold('HOLD-B')); await second.promise})
    expect(screen.getByRole('heading', {name: 'Synthetic HOLD-B'})).toBeInTheDocument()
    await act(async () => {first.resolve(hold('HOLD-A')); await first.promise})
    expect(screen.queryByRole('heading', {name: 'Synthetic HOLD-A'})).not.toBeInTheDocument()
  })

  it('removes prior details and register rows after a denied refresh', async () => {
    vi.mocked(governance.getGovernance).mockResolvedValue(hold('HOLD-A'))
    render(<RecordGovernancePanel />); fireEvent.click(screen.getByRole('button', {name: 'Select patient A'}))
    fireEvent.click(await screen.findByRole('button', {name: 'Synthetic HOLD-A · active'}))
    await screen.findByRole('heading', {name: 'Synthetic HOLD-A'})
    vi.mocked(governance.listGovernance).mockRejectedValue(Object.assign(new Error('Record access refused'), {status: 403}))
    fireEvent.click(screen.getByRole('button', {name: 'Refresh register'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('Record access refused')
    expect(screen.queryByText(/Synthetic HOLD-A/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', {name: 'Save record'})).not.toBeInTheDocument()
  })

  it('does not display a completed old-patient mutation in the newly selected patient', async () => {
    const pending = deferred<governance.GovernanceRecord>()
    vi.mocked(governance.getGovernance).mockResolvedValue(hold('HOLD-A'))
    vi.mocked(governance.transitionGovernance).mockReturnValue(pending.promise)
    render(<RecordGovernancePanel />); fireEvent.click(screen.getByRole('button', {name: 'Select patient A'}))
    fireEvent.click(await screen.findByRole('button', {name: 'Synthetic HOLD-A · active'}))
    fireEvent.change(await screen.findByLabelText('Reason'), {target: {value: 'Authorized handover TEST'}})
    fireEvent.change(screen.getByLabelText('Release authority reference'), {target: {value: 'Signed reference TEST'}})
    fireEvent.click(screen.getByRole('button', {name: 'Record action'}))
    vi.mocked(governance.listGovernance).mockResolvedValue({records: [hold('NEW-PATIENT', 'PAT-B')], total: 1})
    fireEvent.click(screen.getByRole('button', {name: 'Select patient B'}))
    await screen.findByRole('button', {name: 'Synthetic NEW-PATIENT · active'})
    await act(async () => {pending.resolve({...hold('HOLD-A'), released_at: '2026-10-01T00:00:00Z'}); await pending.promise})
    await waitFor(() => expect(screen.queryByText('Change saved.')).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', {name: 'Synthetic HOLD-A'})).not.toBeInTheDocument()
    expect(screen.getByRole('button', {name: 'Synthetic NEW-PATIENT · active'})).toBeInTheDocument()
  })

  it('reselecting the current register refreshes it instead of leaving the screen empty', async () => {
    render(<RecordGovernancePanel />); fireEvent.click(screen.getByRole('button', {name: 'Select patient A'}))
    await screen.findByRole('button', {name: 'Synthetic HOLD-A · active'})
    fireEvent.click(screen.getByRole('button', {name: 'Legal holds'}))
    expect(await screen.findByRole('button', {name: 'Synthetic HOLD-A · active'})).toBeInTheDocument()
  })
})

describe('radiation document request ownership and equipment paging', () => {
  it('keeps the newer selected document when an earlier detail arrives last', async () => {
    const first = deferred<unknown>(), second = deferred<unknown>()
    vi.mocked(apiFetch).mockImplementation(path => path.includes('?') ? Promise.resolve(register) : path.endsWith('RAD-A') ? first.promise : second.promise)
    render(<RadiationSafetyPanel />)
    fireEvent.click(await screen.findByRole('button', {name: /ASSET-1 · Synthetic RAD-A/}))
    fireEvent.click(screen.getByRole('button', {name: /ASSET-1 · Synthetic RAD-B/}))
    await act(async () => {second.resolve({record: evidence('RAD-B'), events: []}); await second.promise})
    expect(screen.getByRole('heading', {name: 'Synthetic RAD-B'})).toBeInTheDocument()
    await act(async () => {first.resolve({record: evidence('RAD-A'), events: []}); await first.promise})
    expect(screen.queryByRole('heading', {name: 'Synthetic RAD-A'})).not.toBeInTheDocument()
  })

  it('clears the held document, equipment catalog and register after authoritative refusal', async () => {
    vi.mocked(apiFetch).mockImplementation(path => path.includes('?') ? Promise.resolve(register) : Promise.resolve({record: evidence('RAD-A'), events: []}))
    render(<RadiationSafetyPanel />)
    fireEvent.click(await screen.findByRole('button', {name: /ASSET-1 · Synthetic RAD-A/}))
    await screen.findByRole('heading', {name: 'Synthetic RAD-A'})
    vi.mocked(apiFetch).mockRejectedValue(Object.assign(new Error('Equipment access refused'), {status: 403}))
    fireEvent.click(screen.getByRole('button', {name: 'Refresh register'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('Equipment access refused')
    expect(screen.queryByText(/Synthetic RAD-A/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Equipment')).not.toBeInTheDocument()
  })

  it('loads later equipment pages and resets the selected equipment when searching', async () => {
    vi.mocked(apiFetch).mockImplementation(path => Promise.resolve(path.includes('asset_offset=50') ? {...register, assets: [{...asset, id: 999, asset_tag: 'LATE-999'}], asset_offset: 50, asset_next_offset: null} : register))
    render(<RadiationSafetyPanel />)
    fireEvent.click(await screen.findByRole('button', {name: 'Next equipment page'}))
    expect(await screen.findByRole('option', {name: /LATE-999/})).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Equipment'), {target: {value: '999'}})
    expect(screen.getByLabelText('Equipment')).toHaveValue('999')
    fireEvent.change(screen.getByLabelText('Search equipment tag, name or serial number'), {target: {value: 'IMAGING-501'}})
    fireEvent.click(screen.getByRole('button', {name: 'Search equipment'}))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining('asset_query=IMAGING-501&asset_offset=0&asset_limit=50')))
    expect(await screen.findByLabelText('Equipment')).toHaveValue('')
  })
})
