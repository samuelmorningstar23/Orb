import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OfflineClinicalRecordsPanel from './OfflineClinicalRecordsPanel'
import * as api from '../../api/offlineClinicalRecords'
import type { ClinicalDomain, ClinicalMeta, ClinicalRecord } from '../../api/offlineClinicalRecords'

vi.mock('../../api/offlineClinicalRecords', () => ({
  getClinicalMeta: vi.fn(), listClinicalRecords: vi.fn(), getClinicalRecord: vi.fn(), createClinicalRecord: vi.fn(),
  updateClinicalRecord: vi.fn(), signClinicalRecord: vi.fn(), addClinicalAddendum: vi.fn(), listPerioperativeCases: vi.fn(), getClinicalHistory: vi.fn(),
}))
vi.mock('../../components/OfflinePatientPicker', () => ({ default: ({ value, onChange, includeRegistered }: { value: string; onChange: (id: string) => void; includeRegistered: boolean }) =>
  <label>Patient <select value={value} data-registered={String(includeRegistered)} onChange={e => onChange(e.target.value)}><option value="">Choose</option><option value="p1">Synthetic patient</option></select></label>,
}))

function meta(domain: ClinicalDomain, extras: Partial<ClinicalMeta> = {}): ClinicalMeta {
  const states: Record<ClinicalDomain, string[]> = {
    radiology: ['requested', 'scheduled', 'arrived', 'performed', 'reported', 'signed', 'cancelled'],
    'care-plans': ['draft', 'active', 'completed', 'cancelled'], emergency: ['arrived', 'triaged', 'seen', 'record_closed', 'cancelled'],
    perioperative: ['draft', 'in_progress', 'completed', 'signed', 'cancelled'], dialysis: ['planned', 'in_progress', 'completed', 'signed', 'cancelled'],
  }
  return { module: domain, states: states[domain], initial_state: states[domain][0], terminal_states: ['signed', 'cancelled', ...(domain === 'care-plans' ? ['completed'] : []), ...(domain === 'emergency' ? ['record_closed'] : [])], can_write: true, can_sign: true, can_prescribe: true, documentation_only: true, ...extras }
}
function record(extras: Partial<ClinicalRecord> = {}): ClinicalRecord {
  return { id: 'r1', patient_id: 'p1', admission_id: null, surgery_id: null, facility_id: 1, department: 'Synthetic ward', title: 'Recorded study', state: 'requested', data: { modality: 'X-ray', body_site: 'Chest' }, version: 1, created_by: 'd1', updated_by: 'd1', created_at: '2026-10-11T08:00:00Z', updated_at: '2026-10-11T08:00:00Z', signed_by: null, signed_at: null, history: [], ...extras }
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.getClinicalMeta).mockImplementation(async domain => meta(domain))
  vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [], limit: 50, offset: 0 })
  vi.mocked(api.listPerioperativeCases).mockResolvedValue({ cases: [{ id: 's1', patient_id: 'p1', patient_name: 'Synthetic patient', procedure_name: 'Recorded procedure', scheduled_date: '2026-10-11' }] })
})
async function newRecord(domain: ClinicalDomain) {
  render(<OfflineClinicalRecordsPanel domain={domain} />)
  await screen.findByRole('button', { name: 'New record' })
  if (domain !== 'perioperative') fireEvent.change(screen.getByLabelText('Patient'), { target: { value: 'p1' } })
  fireEvent.click(screen.getByRole('button', { name: 'New record' }))
}

describe('Local documentation panels', () => {
  it.each([
    ['radiology', 'Body site'], ['care-plans', 'Care goal'], ['emergency', 'Presenting complaint'],
    ['perioperative', 'Pre-anaesthesia assessment'], ['dialysis', 'Actual duration (minutes)'],
  ] as const)('exposes the %s workflow fields', async (domain, field) => {
    await newRecord(domain)
    expect(screen.getByLabelText(field)).toBeEnabled()
    expect(screen.getByLabelText('Patient')).toHaveAttribute('data-registered', String(domain === 'emergency'))
  })

  it('records due time, responsible staff and an intervention outcome in a care plan', async () => {
    await newRecord('care-plans')
    fireEvent.change(screen.getByLabelText('Record title'), { target: { value: 'Mobility plan' } })
    fireEvent.change(screen.getByLabelText('Problem / focus'), { target: { value: 'Mobility' } })
    fireEvent.change(screen.getByLabelText('Care goal'), { target: { value: 'Staff goal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add intervention' }))
    fireEvent.change(screen.getByLabelText('Intervention'), { target: { value: 'Assist activity' } })
    fireEvent.change(screen.getByLabelText('Responsible staff'), { target: { value: 'Assigned nurse' } })
    fireEvent.change(screen.getByLabelText('Due time'), { target: { value: '2026-10-11T09:00' } })
    fireEvent.change(screen.getByLabelText('Intervention outcome'), { target: { value: 'done' } })
    fireEvent.change(screen.getByLabelText('Outcome time'), { target: { value: '2026-10-11T09:30' } })
    fireEvent.change(screen.getByLabelText('Intervention evaluation'), { target: { value: 'Assistance recorded' } })
    const saved = record({ title: 'Mobility plan', state: 'draft', data: { problem: 'Mobility', goal: 'Staff goal', tasks: [] } })
    vi.mocked(api.createClinicalRecord).mockResolvedValue(saved)
    vi.mocked(api.getClinicalRecord).mockResolvedValue(saved)
    fireEvent.click(screen.getByRole('button', { name: 'Create record' }))
    await waitFor(() => expect(api.createClinicalRecord).toHaveBeenCalledTimes(1))
    expect(api.createClinicalRecord).toHaveBeenCalledWith('care-plans', expect.objectContaining({ patient_id: 'p1', data: expect.objectContaining({ tasks: [expect.objectContaining({ owner: 'Assigned nurse', state: 'done', evaluation: 'Assistance recorded', due_at: expect.stringContaining('T'), completed_at: expect.stringContaining('T') })] }) }), expect.any(String))
    await screen.findByText('Record saved.')
  })

  it('uses the scoped surgical case picker before choosing a ward patient', async () => {
    await newRecord('perioperative')
    fireEvent.change(screen.getByLabelText('Surgical case'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'implant' } })
    expect(screen.getByLabelText('Patient')).toHaveValue('p1')
    expect(screen.getByLabelText('Lot / batch')).toBeEnabled()
    expect(screen.getByLabelText('Serial number')).toBeEnabled()
    expect(screen.queryByLabelText('Airway record')).not.toBeInTheDocument()
  })

  it('lets a nurse record actual dialysis values without editing the prescription', async () => {
    vi.mocked(api.getClinicalMeta).mockResolvedValue(meta('dialysis', { can_prescribe: false, can_sign: false }))
    const item = record({ title: 'Session', state: 'in_progress', data: { access: 'Recorded access', prescription: { modality: 'Staff prescription', duration_minutes: 180 }, actual_duration_minutes: 90 } })
    vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [item], limit: 50, offset: 0 })
    vi.mocked(api.getClinicalRecord).mockResolvedValue(item)
    render(<OfflineClinicalRecordsPanel domain="dialysis" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Session' }))
    expect(await screen.findByLabelText('Prescribed duration (minutes)')).toBeDisabled()
    expect(screen.getByLabelText('Actual duration (minutes)')).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Sign final record' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove unsaved prescription' })).not.toBeInTheDocument()
  })

  it('keeps a signed original readonly and appends a reasoned addendum', async () => {
    const item = record({ state: 'signed', version: 6, signed_by: 'd1', signed_at: '2026-10-11T09:00:00Z' })
    vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [item], limit: 50, offset: 0 })
    vi.mocked(api.getClinicalRecord).mockResolvedValue(item)
    vi.mocked(api.addClinicalAddendum).mockResolvedValue({ ...item, version: 7 })
    render(<OfflineClinicalRecordsPanel domain="radiology" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Recorded study' }))
    expect(await screen.findByLabelText('Report findings')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save revision' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Addendum'), { target: { value: 'Staff clarification' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Late entry' } })
    fireEvent.click(screen.getByRole('button', { name: 'Append addendum' }))
    await waitFor(() => expect(api.addClinicalAddendum).toHaveBeenCalledWith('radiology', 'r1', 6, 'Staff clarification', 'Late entry'))
  })

  it('worklist refresh preserves the loaded workflow stage', async () => {
    const item = record({ state: 'reported', version: 5 })
    vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [item], limit: 50, offset: 0 })
    vi.mocked(api.getClinicalRecord).mockResolvedValue(item)
    render(<OfflineClinicalRecordsPanel domain="radiology" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Recorded study' }))
    expect(await screen.findByLabelText('Workflow stage')).toHaveValue('reported')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh worklist' }))
    await waitFor(() => expect(api.getClinicalMeta).toHaveBeenCalledTimes(2))
    expect(screen.getByLabelText('Workflow stage')).toHaveValue('reported')
  })

  it('shows a failed save without claiming success or advancing the version', async () => {
    const item = record({ state: 'reported', version: 5 })
    vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [item], limit: 50, offset: 0 })
    vi.mocked(api.getClinicalRecord).mockResolvedValue(item)
    vi.mocked(api.updateClinicalRecord).mockRejectedValue(new Error('Record changed; reload before saving'))
    render(<OfflineClinicalRecordsPanel domain="radiology" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Recorded study' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save revision' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Record changed; reload before saving')
    expect(screen.queryByText('Record saved.')).not.toBeInTheDocument()
    expect(screen.getByText('Revision 5')).toBeInTheDocument()
  })
})

it('loads older saved revisions without replacing the signed original', async () => {
  const item = record({ state: 'signed', version: 102, history_next_offset: 100, history: [{ version: 102, kind: 'signed', actor_id: 'd1', prior_state: 'reported', new_state: 'signed', recorded_at: '2026-10-11T09:00:00Z', reason: null, snapshot: {} }] })
  vi.mocked(api.listClinicalRecords).mockResolvedValue({ records: [item], limit: 50, offset: 0 })
  vi.mocked(api.getClinicalRecord).mockResolvedValue(item)
  vi.mocked(api.getClinicalHistory).mockResolvedValue({ history: [{ version: 1, kind: 'created', actor_id: 'd1', prior_state: null, new_state: 'requested', recorded_at: '2026-10-11T08:00:00Z', reason: null, snapshot: { title: 'Original saved title', data: { modality: 'X-ray', body_site: 'Chest' } } }], history_next_offset: null })
  render(<OfflineClinicalRecordsPanel domain="radiology" />)
  fireEvent.click(await screen.findByRole('button', { name: 'Recorded study' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Load older revisions' }))
  expect(await screen.findByText('Original saved title')).toBeInTheDocument()
  expect(api.getClinicalHistory).toHaveBeenCalledWith('radiology', 'r1', 100)
  expect(screen.getByLabelText('Workflow stage')).toHaveValue('signed')
  expect(screen.queryByRole('button', { name: 'Load older revisions' })).not.toBeInTheDocument()
})
