/**
 * The laboratory Setup screen (lis-4). What it must never get wrong:
 *
 * 1. NOT CONFIGURED IS LOUD. Orb ships no reference range and no critical
 *    limit (LIS-R1, LIS-R2): the screen says how many numeric laboratory tests
 *    have none and what that means for a result, and a test's row says "Not
 *    configured", never a blank. An empty catalogue is said as empty.
 * 2. ONLY A PATHOLOGIST CHANGES IT. A technician (or an administrator) reads
 *    the catalogue with no control that changes it; a pathologist gets the
 *    forms. While the module drains, it is read only for everyone.
 * 3. AN APPROVAL IS DELIBERATE. It needs an approval note and the approver's
 *    confirmation; a critical limit also names who it was agreed with; a value
 *    must be exact at the test's reporting precision (trailing zeros are fine).
 *    Nothing is sent until the form is right, and the server's refusal is
 *    shown in its own words.
 * 4. RETIRING NEEDS A REASON.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { LabTest } from '../../../api/lab/master'
import SetupView, * as setupModule from './SetupView'
import { aboutAge, bandText, gapLines, precisionProblem } from './labSetupModel'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/master', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/master')>(
    '../../../api/lab/master')
  return {
    ...actual,
    listLabTests: vi.fn(),
    listLabReferenceRanges: vi.fn(),
    listLabCriticalLimits: vi.fn(),
    listLabSpecimenTypes: vi.fn(),
    listLabSections: vi.fn(),
    listLabRejectionReasons: vi.fn(),
    getLabTest: vi.fn(),
    approveLabReferenceRange: vi.fn(),
    approveLabCriticalLimit: vi.fn(),
    retireLabReferenceRange: vi.fn(),
    retireLabCriticalLimit: vi.fn(),
    createLabTest: vi.fn(),
    updateLabTest: vi.fn(),
    setLabPanelComponents: vi.fn(),
    saveLabSpecimenType: vi.fn(),
    saveLabSection: vi.fn(),
    saveLabRejectionReason: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/master'))

const POTASSIUM: LabTest = {
  id: 'LOINC:2823-3', code: '2823-3', code_system: 'LOINC', name: 'Potassium',
  short_name: 'Potassium', kind: 'analyte', performed_at: 'lab', section_id: 'biochemistry',
  specimen_type_code: 'serum', result_type: 'numeric', unit: 'mmol/L', decimal_places: 1,
  orderable: true, active: true, version: 1, updated_by: 'system',
  updated_at: '2026-10-04T06:00:00.000000+00:00',
}
const SODIUM: LabTest = { ...POTASSIUM, id: 'LOINC:2951-2', code: '2951-2', name: 'Sodium',
  short_name: 'Sodium', decimal_places: 0 }
const KETONES: LabTest = { ...POTASSIUM, id: 'LOCAL:ORB-LAB-BLOOD-KETONES',
  code: 'ORB-LAB-BLOOD-KETONES', code_system: 'LOCAL', name: 'Capillary blood ketones',
  short_name: 'Ketones', performed_at: 'ward_poct', section_id: null,
  specimen_type_code: 'capillary_blood' }
const CBC: LabTest = { ...POTASSIUM, id: 'LOINC:58410-2', code: '58410-2',
  name: 'Complete blood count', short_name: 'CBC', kind: 'panel', result_type: 'text',
  specimen_type_code: 'edta_whole_blood', section_id: 'haematology' }

const SODIUM_RANGE = {
  id: 7, test_id: SODIUM.id, sex: 'any' as const, age_min_days: 0, age_max_days: null,
  low_milli: 135000, high_milli: 145000, display_text: '135 to 145 mmol/L',
  effective_from: '2026-10-04T06:00:00.000000+00:00', effective_to: null,
  approved_by: 'P001', approved_at: '2026-10-04T06:00:00.000000+00:00',
  method_note: 'Analyser A, manufacturer insert',
}

function signInAs(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

function catalogue({ gaps = { numeric_tests: 3, without_reference_range: 2, without_critical_limit: 3 } } = {}) {
  api.listLabTests.mockResolvedValue({ tests: [POTASSIUM, SODIUM, KETONES, CBC], gaps })
  api.listLabReferenceRanges.mockResolvedValue({ ranges: [SODIUM_RANGE] })
  api.listLabCriticalLimits.mockResolvedValue({ critical_limits: [] })
  api.listLabSpecimenTypes.mockResolvedValue({ specimen_types: [
    { code: 'serum', display: 'Serum', container: 'Plain or gel tube', container_colour_name: 'Red or gold top',
      min_volume_ul: null, stability_minutes: null, handling_note: null, active: true },
    { code: 'edta_whole_blood', display: 'EDTA whole blood', container: 'EDTA tube',
      container_colour_name: 'Lavender top', min_volume_ul: null, stability_minutes: null,
      handling_note: null, active: true },
  ] })
  api.listLabSections.mockResolvedValue({ sections: [
    { id: 'biochemistry', name: 'Biochemistry', site_id: null, active: true, sort_order: 20 },
    { id: 'haematology', name: 'Haematology', site_id: null, active: true, sort_order: 10 },
  ] })
  api.listLabRejectionReasons.mockResolvedValue({ rejection_reasons: [
    { code: 'haemolysed', display: 'Haemolysed', incident_prompt: false, active: true },
    { code: 'mislabelled', display: 'Mislabelled', incident_prompt: true, active: true },
  ] })
  api.getLabTest.mockImplementation(async (id: string) => {
    if (id === SODIUM.id) return { ...SODIUM, reference_ranges: [SODIUM_RANGE], critical_limits: [] }
    if (id === CBC.id) return { ...CBC, components: [
      { test_id: 'LOINC:718-7', name: 'Haemoglobin', short_name: 'Haemoglobin', sort_order: 10 }] }
    const t = [POTASSIUM, KETONES].find((x) => x.id === id)!
    return { ...t, reference_ranges: [], critical_limits: [] }
  })
}

function renderView(state: 'on' | 'draining' = 'on') {
  const onModuleOff = vi.fn()
  render(<SetupView meta={{ module: 'lab', state }} state={state} openView={vi.fn()}
    onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openTest(name: string) {
  fireEvent.click(await screen.findByRole('button', { name: `Open ${name}` }))
  return screen.findByRole('region', { name })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

afterEach(() => {
  localStorage.clear()
})

describe('the view contract', () => {
  it('is the setup tab, after the work screens, open to anyone holding lab.read', () => {
    expect(setupModule.viewId).toBe('setup')
    expect(setupModule.viewOrder).toBe(90)
    expect(setupModule.viewPermission).toBe('lab.read')
    const [entry] = discoverViews({ './lab/SetupView.tsx': setupModule })
    expect(entry).toMatchObject({ id: 'setup', label: 'Setup', permission: 'lab.read',
      whenDraining: 'show' })
  })
})

describe('not configured is loud', () => {
  it('says how many numeric tests have no approved range or limit, and what it means', async () => {
    signInAs('lab_technician')
    catalogue()
    renderView()
    expect(await screen.findByText(/2 of 3 numeric laboratory tests have no approved reference range/))
      .toBeInTheDocument()
    expect(screen.getByText(/carry no high or low flag/)).toBeInTheDocument()
    expect(screen.getByText(/3 of 3 numeric laboratory tests have no approved critical limit/))
      .toBeInTheDocument()
    expect(screen.getByText(/No critical result will be raised/)).toBeInTheDocument()
    const potassium = screen.getByText('Potassium', { selector: 'span' }).closest('tr')!
    expect(within(potassium).getAllByText('Not configured')).toHaveLength(2)
    const sodium = screen.getByText('Sodium', { selector: 'span' }).closest('tr')!
    expect(within(sodium).getByText('Approved')).toBeInTheDocument()
    expect(within(sodium).getByText('Not configured')).toBeInTheDocument()
    const ketones = screen.getByText('Capillary blood ketones', { selector: 'span' }).closest('tr')!
    expect(within(ketones).getByText('At the bedside')).toBeInTheDocument()
  })

  it('says an empty catalogue is empty, never that every test is configured', async () => {
    signInAs('pathologist')
    catalogue({ gaps: { numeric_tests: 0, without_reference_range: 0, without_critical_limit: 0 } })
    renderView()
    expect(await screen.findByText(/no numeric laboratory tests in the catalogue yet/))
      .toBeInTheDocument()
    expect(screen.queryByText(/Every numeric laboratory test/)).not.toBeInTheDocument()
  })

  it('a test with no range says so in its own editor', async () => {
    signInAs('lab_technician')
    catalogue()
    renderView()
    const detail = await openTest('Potassium')
    expect(await within(detail).findByText(/Reference range not configured/)).toBeInTheDocument()
    expect(within(detail).getByText(/Critical limit not configured/)).toBeInTheDocument()
  })
})

describe('who can change the catalogue', () => {
  it('a technician reads it with no control that changes it', async () => {
    signInAs('lab_technician')
    catalogue()
    renderView()
    expect(await screen.findByText(/Only a pathologist can change it/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add a test' })).not.toBeInTheDocument()
    const detail = await openTest('Sodium')
    expect(await within(detail).findByText('135 to 145 mmol/L')).toBeInTheDocument()
    expect(within(detail).getByText(/P001/)).toBeInTheDocument()
    expect(within(detail).getByText('Analyser A, manufacturer insert')).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Change test' })).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Retire' })).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Approve range' })).not.toBeInTheDocument()
    expect(within(detail).getByText('Only a pathologist can approve or retire a range.'))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Specimen types' }))
    expect(screen.queryByRole('button', { name: /Change Serum/ })).not.toBeInTheDocument()
  })

  it('an administrator is read only too', async () => {
    signInAs('admin')
    catalogue()
    renderView()
    expect(await screen.findByText(/Only a pathologist can change it/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add a test' })).not.toBeInTheDocument()
  })

  it('a pathologist gets the forms', async () => {
    signInAs('pathologist')
    catalogue()
    renderView()
    expect(await screen.findByRole('button', { name: 'Add a test' })).toBeInTheDocument()
    expect(screen.queryByText(/Only a pathologist can change it/)).not.toBeInTheDocument()
    const detail = await openTest('Potassium')
    expect(await within(detail).findByRole('button', { name: 'Approve range' })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Approve limit' })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Change test' })).toBeInTheDocument()
  })

  it('while the module drains the catalogue is read only, even for a pathologist', async () => {
    signInAs('pathologist')
    catalogue()
    renderView('draining')
    expect(await screen.findByText(/so the catalogue is read only/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add a test' })).not.toBeInTheDocument()
    const detail = await openTest('Potassium')
    await within(detail).findByText(/Reference range not configured/)
    expect(within(detail).queryByRole('button', { name: 'Approve range' })).not.toBeInTheDocument()
  })

  it('a switched-off module answer hands the panel its notice', async () => {
    signInAs('pathologist')
    catalogue()
    api.listLabTests.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})

function rangeForm(detail: HTMLElement) {
  return within(detail).getByRole('form', { name: 'Approve a reference range' })
}

function limitForm(detail: HTMLElement) {
  return within(detail).getByRole('form', { name: 'Approve a critical limit' })
}

describe('an approval is deliberate', () => {
  it('needs an approval note and the confirmation before anything is sent', async () => {
    signInAs('pathologist')
    catalogue()
    api.approveLabReferenceRange.mockResolvedValue({ ...SODIUM_RANGE, id: 9, test_id: POTASSIUM.id,
      display_text: '3.5 to 5.1 mmol/L' })
    renderView()
    const detail = await openTest('Potassium')
    await within(detail).findByRole('button', { name: 'Approve range' })
    const form = rangeForm(detail)
    fireEvent.change(within(form).getByLabelText(/Lower value/), { target: { value: '3.5' } })
    fireEvent.change(within(form).getByLabelText(/Upper value/), { target: { value: '5.1' } })
    fireEvent.click(within(form).getByLabelText(/I approve this reference range/))
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/Write the approval note/)
    expect(api.approveLabReferenceRange).not.toHaveBeenCalled()

    fireEvent.change(within(form).getByLabelText(/Approval note/), { target: { value: 'Analyser A' } })
    fireEvent.click(within(form).getByLabelText(/I approve this reference range/)) // untick
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/Tick the box/)
    expect(api.approveLabReferenceRange).not.toHaveBeenCalled()

    fireEvent.click(within(form).getByLabelText(/I approve this reference range/))
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    await waitFor(() => expect(api.approveLabReferenceRange).toHaveBeenCalledTimes(1))
    expect(api.approveLabReferenceRange).toHaveBeenCalledWith({
      test_id: POTASSIUM.id, sex: 'any', age_min_days: 0, age_max_days: null,
      low: '3.5', high: '5.1', method_note: 'Analyser A',
    })
    expect(await within(form).findByRole('status')).toHaveTextContent('Approved: 3.5 to 5.1 mmol/L')
  })

  it('checks the value against the test reporting precision, trailing zeros allowed', async () => {
    signInAs('pathologist')
    catalogue()
    api.approveLabReferenceRange.mockResolvedValue({ ...SODIUM_RANGE, display_text: '3.5 to 5.1 mmol/L' })
    renderView()
    const detail = await openTest('Potassium')
    await within(detail).findByRole('button', { name: 'Approve range' })
    const form = rangeForm(detail)
    fireEvent.change(within(form).getByLabelText(/Lower value/), { target: { value: '3.55' } })
    fireEvent.change(within(form).getByLabelText(/Upper value/), { target: { value: '5.1' } })
    fireEvent.change(within(form).getByLabelText(/Approval note/), { target: { value: 'Analyser A' } })
    fireEvent.click(within(form).getByLabelText(/I approve this reference range/))
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    expect(await within(form).findByRole('alert'))
      .toHaveTextContent('Lower value: This test reports 1 decimal place.')
    expect(api.approveLabReferenceRange).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText(/Lower value/), { target: { value: '3.50' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    await waitFor(() => expect(api.approveLabReferenceRange).toHaveBeenCalledTimes(1))
    expect(api.approveLabReferenceRange.mock.calls[0][0].low).toBe('3.50')
  })

  it('a critical limit names the clinicians it was agreed with', async () => {
    signInAs('pathologist')
    catalogue()
    api.approveLabCriticalLimit.mockResolvedValue({
      ...SODIUM_RANGE, id: 3, test_id: POTASSIUM.id, critical_low_milli: 2500,
      critical_high_milli: 6500, agreed_with: 'Dr Rao, Dr Iyer',
      display_text: 'Critical below 2.5 or above 6.5 mmol/L' })
    renderView()
    const detail = await openTest('Potassium')
    await within(detail).findByRole('button', { name: 'Approve limit' })
    const form = limitForm(detail)
    fireEvent.change(within(form).getByLabelText(/Critical below/), { target: { value: '2.5' } })
    fireEvent.change(within(form).getByLabelText(/Critical above/), { target: { value: '6.5' } })
    fireEvent.change(within(form).getByLabelText(/Approval note/), { target: { value: 'NABH policy 4' } })
    fireEvent.click(within(form).getByLabelText(/I approve this critical limit/))
    fireEvent.click(within(form).getByRole('button', { name: 'Approve limit' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/Name the clinicians/)
    expect(api.approveLabCriticalLimit).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText(/Agreed with/), { target: { value: 'Dr Rao, Dr Iyer' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Approve limit' }))
    await waitFor(() => expect(api.approveLabCriticalLimit).toHaveBeenCalledTimes(1))
    expect(api.approveLabCriticalLimit).toHaveBeenCalledWith({
      test_id: POTASSIUM.id, sex: 'any', age_min_days: 0, age_max_days: null,
      critical_low: '2.5', critical_high: '6.5', agreed_with: 'Dr Rao, Dr Iyer',
      method_note: 'NABH policy 4',
    })
  })

  it('shows the server refusal in its own words', async () => {
    signInAs('pathologist')
    catalogue()
    api.approveLabReferenceRange.mockRejectedValue(new LabApiError(
      'This overlaps a current range for any patients aged 0 to any days.', 409, 'overlap'))
    renderView()
    const detail = await openTest('Potassium')
    await within(detail).findByRole('button', { name: 'Approve range' })
    const form = rangeForm(detail)
    fireEvent.change(within(form).getByLabelText(/Lower value/), { target: { value: '3.5' } })
    fireEvent.change(within(form).getByLabelText(/Approval note/), { target: { value: 'x' } })
    fireEvent.click(within(form).getByLabelText(/I approve this reference range/))
    fireEvent.click(within(form).getByRole('button', { name: 'Approve range' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent(/overlaps a current range/)
  })
})

describe('retiring', () => {
  it('needs a reason before it is sent', async () => {
    signInAs('pathologist')
    catalogue()
    api.retireLabReferenceRange.mockResolvedValue({ ...SODIUM_RANGE, effective_to: '2026-10-04T07:00:00+00:00' })
    renderView()
    const detail = await openTest('Sodium')
    fireEvent.click(await within(detail).findByRole('button', { name: 'Retire' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Retire range' }))
    expect(await within(detail).findByRole('alert')).toHaveTextContent(/Write why/)
    expect(api.retireLabReferenceRange).not.toHaveBeenCalled()
    fireEvent.change(within(detail).getByLabelText(/Why is this range being retired/),
      { target: { value: 'New analyser' } })
    fireEvent.click(within(detail).getByRole('button', { name: 'Retire range' }))
    await waitFor(() => expect(api.retireLabReferenceRange).toHaveBeenCalledWith(7, 'New analyser'))
  })
})

describe('tests, panels and the other lists', () => {
  it('a panel lists its tests and a bedside test says it stays on the ward', async () => {
    signInAs('lab_technician')
    catalogue()
    renderView()
    const panel = await openTest('Complete blood count')
    expect(await within(panel).findByText('Haemoglobin')).toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: 'Approve range' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    const bedside = await openTest('Capillary blood ketones')
    expect(await within(bedside).findByText(/stay on the ward and never reach/)).toBeInTheDocument()
  })

  it('a pathologist adds a test through the form, and the code is required', async () => {
    signInAs('pathologist')
    catalogue()
    api.createLabTest.mockResolvedValue({ ...POTASSIUM, id: 'LOCAL:MG', code: 'MG', name: 'Magnesium' })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a test' }))
    const form = screen.getByRole('form', { name: 'Add a test' })
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Magnesium' } })
    fireEvent.change(within(form).getByLabelText(/Short name/), { target: { value: 'Magnesium' } })
    fireEvent.change(within(form).getByLabelText('Section'), { target: { value: 'biochemistry' } })
    fireEvent.change(within(form).getByLabelText('Specimen type'), { target: { value: 'serum' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Add test' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Give the test its code.')
    fireEvent.change(within(form).getByLabelText('Code'), { target: { value: 'MG' } })
    fireEvent.change(within(form).getByLabelText(/Decimal places/), { target: { value: '2' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Add test' }))
    await waitFor(() => expect(api.createLabTest).toHaveBeenCalledTimes(1))
    expect(api.createLabTest.mock.calls[0][0]).toMatchObject({
      code_system: 'LOCAL', code: 'MG', kind: 'analyte', name: 'Magnesium',
      short_name: 'Magnesium', performed_at: 'lab', section_id: 'biochemistry',
      specimen_type_code: 'serum', result_type: 'numeric', decimal_places: 2,
    })
  })

  it('a change sends only what changed, and nothing when nothing did', async () => {
    signInAs('pathologist')
    catalogue()
    api.updateLabTest.mockResolvedValue({ ...POTASSIUM, name: 'Serum potassium', version: 2 })
    renderView()
    const detail = await openTest('Potassium')
    fireEvent.click(await within(detail).findByRole('button', { name: 'Change test' }))
    const form = within(detail).getByRole('form', { name: 'Change Potassium' })
    fireEvent.click(within(form).getByRole('button', { name: 'Save changes' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Nothing has changed.')
    expect(api.updateLabTest).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Serum potassium' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.updateLabTest).toHaveBeenCalledWith(POTASSIUM.id,
      { name: 'Serum potassium' }))
  })

  it('rejection reasons say which ones prompt an incident report', async () => {
    signInAs('pathologist')
    catalogue()
    api.saveLabRejectionReason.mockResolvedValue({ code: 'haemolysed', display: 'Haemolysed',
      incident_prompt: 1, active: 1 })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Rejection reasons' }))
    const row = screen.getByText('Mislabelled').closest('tr')!
    expect(within(row).getByText('Yes')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Change Haemolysed' }))
    fireEvent.click(screen.getByLabelText(/Prompt for an incident report/))
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.saveLabRejectionReason).toHaveBeenCalledWith('haemolysed', {
      display: 'Haemolysed', incident_prompt: true, active: true }))
  })

  it('a write refused because the module drains is said in words', async () => {
    signInAs('pathologist')
    catalogue()
    api.saveLabSection.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    const { onModuleOff } = renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Sections' }))
    fireEvent.click(screen.getByRole('button', { name: 'Change Biochemistry' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/catalogue cannot be changed/)
    expect(onModuleOff).not.toHaveBeenCalled()
  })
})

describe('the words and checks', () => {
  it('precision follows the server rule', () => {
    expect(precisionProblem('', 1)).toBeNull()
    expect(precisionProblem('3.5', 1)).toBeNull()
    expect(precisionProblem('3.50', 1)).toBeNull()
    expect(precisionProblem('-2', 0)).toBeNull()
    expect(precisionProblem('3.55', 1)).toBe('This test reports 1 decimal place.')
    expect(precisionProblem('3.5', 0)).toBe('This test reports whole numbers only.')
    expect(precisionProblem('3.555', 2)).toBe('This test reports 2 decimal places.')
    expect(precisionProblem('abc', 1)).toBe('Type a number, like 4.5.')
    expect(precisionProblem('1e3', 1)).toBe('Type a number, like 4.5.')
  })

  it('bands and ages read in words', () => {
    expect(bandText('any', 0, null)).toBe('Any sex, every age')
    expect(bandText('male', 0, 28)).toBe('Male, 0 to under 28 days (up to 28 days)')
    expect(bandText('female', 6570, null)).toBe('Female, 6570 days and older (about 18 years)')
    expect(aboutAge(365)).toBe('about 12 months')
    expect(aboutAge(1)).toBe('1 day')
  })

  it('gap lines warn only when something is missing', () => {
    const lines = gapLines({ numeric_tests: 2, without_reference_range: 0, without_critical_limit: 1 })
    expect(lines.map((l) => l.warn)).toEqual([false, true])
    expect(lines[0].sentence).toBe('Every numeric laboratory test has an approved reference range.')
    expect(lines[1].sentence).toMatch(/^1 of 2 numeric laboratory tests has no approved critical limit/)
  })
})
