/**
 * The laboratory Analysers screen (lis-23). What it must never get wrong:
 *
 * 1. THE KEY IS SHOWN ONCE. After it is issued it appears in one panel, is
 *    gone once the person says it is copied, and is never written to browser
 *    storage. Replacing a key needs a reason and says the old one stops.
 * 2. WHO DOES WHAT. A technician reads with no control that changes anything.
 *    The administrator registers analysers, issues keys and switches them on
 *    or off, but cannot map codes. A pathologist maps codes but cannot touch
 *    the registry or keys.
 * 3. STRICT ON THE SCREEN TOO. An analyser with no key cannot be switched
 *    on; a mapping needs a note; the factor is exact to three decimal places;
 *    retiring a code needs a reason. Nothing is sent until the form is right.
 * 4. HONEST. Until Orb receives results from analysers, the screen says so.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import { calibrationState, factorProblem, type Analyser } from '../../../api/lab/analysers'
import AnalysersView, * as analysersModule from './AnalysersView'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/analysers', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/analysers')>(
    '../../../api/lab/analysers')
  return {
    ...actual,
    listAnalysers: vi.fn(),
    getAnalyser: vi.fn(),
    registerAnalyser: vi.fn(),
    changeAnalyser: vi.fn(),
    setAnalyserSwitchedOn: vi.fn(),
    issueAnalyserKey: vi.fn(),
    saveAnalyserMapping: vi.fn(),
    retireAnalyserMapping: vi.fn(),
  }
})

vi.mock('../../../api/lab/master', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/master')>(
    '../../../api/lab/master')
  return { ...actual, listLabSections: vi.fn(), listLabTests: vi.fn() }
})

const api = vi.mocked(await import('../../../api/lab/analysers'))
const master = vi.mocked(await import('../../../api/lab/master'))

const KEY = 'k3y-shown-once-0123456789abcdefghijklmnopqrstuv'

const XN: Analyser = {
  id: 'XN-1', name: 'Haematology analyser', make: 'Make', model: 'XN', serial: 'S1',
  section_id: 'haematology', section_name: 'Haematology', protocol: 'hl7v2',
  protocol_label: 'HL7 version 2', transport: 'http',
  transport_label: 'Sends results to Orb over the network (HTTP)', supported: true,
  host: '192.168.10.21', port: null, assumed_timezone: null, biomedical_asset_id: null,
  biomedical: null, enabled: false, status_reason: null, last_message_at: null,
  key: { issued: false, issued_at: null, issued_by: null, issued_by_name: null },
  mapped_codes: 0, version: 1, created_at: '2026-10-06T06:00:00.000000+00:00', created_by: 'A1',
  updated_at: '2026-10-06T06:00:00.000000+00:00', updated_by: 'A1', updated_by_name: 'Admin',
  map: [],
}

const HB_MAP = {
  instrument_id: 'XN-1', instrument_code: 'HGB', test_id: 'LOINC:718-7', test_name: 'Haemoglobin',
  test_short_name: 'Haemoglobin', test_unit: 'g/dL', test_result_type: 'numeric', test_active: true,
  test_performed_at: 'lab', test_found: true, factor_milli: 1000, factor: '1',
  instrument_unit: 'g/dL', active: true, note: 'Host manual rev 4', approved_by: 'P1',
  approved_by_name: 'Pathologist', approved_at: '2026-10-06T07:00:00.000000+00:00',
  retired_by: null, retired_by_name: null, retired_at: null, retire_reason: null, version: 1,
}

const OPTIONS = {
  protocols: [{ value: 'hl7v2', label: 'HL7 version 2', supported: true },
    { value: 'astm', label: 'ASTM', supported: false }],
  transports: [{ value: 'http', label: 'Sends results to Orb over the network (HTTP)', supported: true },
    { value: 'mllp_listen', label: "Connects to Orb's analyser port (MLLP)", supported: true },
    { value: 'tcp_client', label: 'Orb connects to the analyser (TCP)', supported: false }],
  hospital_timezone: 'Asia/Kolkata',
  listener: { enabled: false, port: 2576 },
  http_intake_built: false,
}

const TESTS = [
  { id: 'LOINC:718-7', code: '718-7', code_system: 'LOINC', name: 'Haemoglobin',
    short_name: 'Haemoglobin', kind: 'analyte', performed_at: 'lab', section_id: 'haematology',
    specimen_type_code: 'edta_whole_blood', result_type: 'numeric', unit: 'g/dL',
    decimal_places: 1, orderable: true, active: true, version: 1, updated_by: 'system',
    updated_at: '2026-10-04T06:00:00.000000+00:00' },
  { id: 'LOCAL:ORB-LAB-BLOOD-CULTURE', code: 'ORB-LAB-BLOOD-CULTURE', code_system: 'LOCAL',
    name: 'Blood culture', short_name: 'Blood culture', kind: 'analyte', performed_at: 'lab',
    section_id: 'microbiology', specimen_type_code: 'blood_culture', result_type: 'text',
    unit: null, decimal_places: 0, orderable: true, active: true, version: 1,
    updated_by: 'system', updated_at: '2026-10-04T06:00:00.000000+00:00' },
  { id: 'LOCAL:ORB-LAB-BLOOD-KETONES', code: 'ORB-LAB-BLOOD-KETONES', code_system: 'LOCAL',
    name: 'Capillary blood ketones', short_name: 'Ketones', kind: 'analyte',
    performed_at: 'ward_poct', section_id: null, specimen_type_code: 'capillary_blood',
    result_type: 'numeric', unit: 'mmol/L', decimal_places: 1, orderable: true, active: true,
    version: 1, updated_by: 'system', updated_at: '2026-10-04T06:00:00.000000+00:00' },
] as const

function signInAs(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

function registry(analyser: Analyser = XN, options: Partial<typeof OPTIONS> = {}) {
  api.listAnalysers.mockResolvedValue({ analysers: [analyser], options: { ...OPTIONS, ...options } })
  api.getAnalyser.mockResolvedValue(analyser)
  master.listLabSections.mockResolvedValue({ sections: [
    { id: 'haematology', name: 'Haematology', site_id: null, active: true, sort_order: 10 }] })
  master.listLabTests.mockResolvedValue({
    tests: TESTS as unknown as never[],
    gaps: { numeric_tests: 2, without_reference_range: 2, without_critical_limit: 2 },
  })
}

function renderView(state: 'on' | 'draining' = 'on') {
  const onModuleOff = vi.fn()
  render(<AnalysersView meta={{ module: 'lab', state }} state={state} openView={vi.fn()}
    onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

async function openAnalyser(name = 'Haematology analyser') {
  fireEvent.click(await screen.findByRole('button', { name: `Open ${name}` }))
  return screen.findByRole('region', { name })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
})

afterEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

describe('the view contract', () => {
  it('is the analysers tab, last, open to lab.read and hidden while draining', () => {
    expect(analysersModule.viewId).toBe('analysers')
    expect(analysersModule.viewOrder).toBe(95)
    const [entry] = discoverViews({ './lab/AnalysersView.tsx': analysersModule })
    expect(entry).toMatchObject({ id: 'analysers', label: 'Analysers', permission: 'lab.read',
      whenDraining: 'hide' })
  })

  it('hands a switched-off module to the panel', async () => {
    signInAs('admin')
    registry()
    api.listAnalysers.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})

describe('honest about intake', () => {
  it('says Orb does not receive analyser results yet, and stops saying it once it does', async () => {
    signInAs('lab_technician')
    registry()
    renderView()
    expect(await screen.findByText(/Orb does not receive results from analysers yet/))
      .toBeInTheDocument()
  })

  it('drops the sentence when intake exists', async () => {
    signInAs('lab_technician')
    registry(XN, { http_intake_built: true })
    renderView()
    await screen.findByRole('button', { name: 'Open Haematology analyser' })
    expect(screen.queryByText(/Orb does not receive results from analysers yet/)).not.toBeInTheDocument()
  })
})

describe('who does what', () => {
  it('a technician reads with no control that changes anything', async () => {
    signInAs('lab_technician')
    registry({ ...XN, map: [HB_MAP], mapped_codes: 1 })
    renderView()
    expect(await screen.findByText(/You can read the analysers here/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Register an analyser' })).not.toBeInTheDocument()
    const detail = await openAnalyser()
    expect(await within(detail).findByText('Haemoglobin')).toBeInTheDocument()
    for (const name of ['Change details', 'Issue a key', 'Switch on', 'Map a code']) {
      expect(within(detail).queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(within(detail).queryByRole('button', { name: /Retire code/ })).not.toBeInTheDocument()
    expect(within(detail).getByText('Only a pathologist can change which test a code is.'))
      .toBeInTheDocument()
  })

  it('the administrator manages the registry and keys but cannot map codes', async () => {
    signInAs('admin')
    registry()
    renderView()
    expect(await screen.findByRole('button', { name: 'Register an analyser' })).toBeInTheDocument()
    const detail = await openAnalyser()
    expect(await within(detail).findByRole('button', { name: 'Issue a key' })).toBeInTheDocument()
    expect(within(detail).getByRole('button', { name: 'Change details' })).toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Map a code' })).not.toBeInTheDocument()
  })

  it('a pathologist maps codes but never sees the registry or key controls', async () => {
    signInAs('pathologist')
    registry()
    renderView()
    const detail = await openAnalyser()
    expect(await within(detail).findByRole('button', { name: 'Map a code' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Register an analyser' })).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Issue a key' })).not.toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Change details' })).not.toBeInTheDocument()
  })
})

describe('the key is shown once', () => {
  it('appears after it is issued, is never stored, and is gone once copied', async () => {
    signInAs('admin')
    registry()
    const issuedAnalyser = { ...XN, key: { issued: true, issued_at: '2026-10-06T08:00:00.000000+00:00',
      issued_by: 'A1', issued_by_name: 'Admin' } }
    api.issueAnalyserKey.mockResolvedValue({ instrument: issuedAnalyser, key: KEY,
      issued_at: '2026-10-06T08:00:00.000000+00:00', replaced: false })
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Issue a key' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Issue the key' }))
    await waitFor(() => expect(api.issueAnalyserKey).toHaveBeenCalledWith('XN-1', undefined))
    const panel = await screen.findByRole('region', { name: /copy it now/ })
    expect(within(panel).getByDisplayValue(KEY)).toBeInTheDocument()
    expect(within(panel).getByText(/Orb shows this key once/)).toBeInTheDocument()
    const stored = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })
    expect(stored).not.toContain(KEY)
    fireEvent.click(within(panel).getByRole('button', { name: 'I have copied it' }))
    expect(screen.queryByDisplayValue(KEY)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /copy it now/ })).not.toBeInTheDocument()
  })

  it('replacing a key needs a reason and says the old key stops', async () => {
    signInAs('admin')
    const issued = { ...XN, key: { issued: true, issued_at: '2026-10-06T08:00:00.000000+00:00',
      issued_by: 'A1', issued_by_name: 'Admin' } }
    registry(issued)
    api.issueAnalyserKey.mockResolvedValue({ instrument: issued, key: KEY,
      issued_at: '2026-10-06T09:00:00.000000+00:00', replaced: true })
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Replace the key' }))
    expect(within(detail).getByText(/The current key stops working at once/)).toBeInTheDocument()
    const form = within(detail).getByText(/The current key stops working at once/).closest('form')!
    fireEvent.click(within(form).getByRole('button', { name: 'Replace the key' }))
    expect(await within(detail).findByText('Say why the key is being replaced.')).toBeInTheDocument()
    expect(api.issueAnalyserKey).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText('Why it is being replaced'),
      { target: { value: 'Service engineer visit' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Replace the key' }))
    await waitFor(() => expect(api.issueAnalyserKey).toHaveBeenCalledWith('XN-1', 'Service engineer visit'))
    const panel = await screen.findByRole('region', { name: /New key for Haematology analyser/ })
    expect(within(panel).getByText(/The previous key has stopped working/)).toBeInTheDocument()
  })
})

describe('switching on', () => {
  it('an analyser with no key cannot be switched on', async () => {
    signInAs('admin')
    registry()
    renderView()
    const detail = await openAnalyser()
    expect(await within(detail).findByText('Issue this analyser a key before switching it on.'))
      .toBeInTheDocument()
    expect(within(detail).queryByRole('button', { name: 'Switch on' })).not.toBeInTheDocument()
  })

  it('switching on needs a reason', async () => {
    signInAs('admin')
    const keyed = { ...XN, key: { issued: true, issued_at: '2026-10-06T08:00:00.000000+00:00',
      issued_by: 'A1', issued_by_name: 'Admin' } }
    registry(keyed)
    api.setAnalyserSwitchedOn.mockResolvedValue({ ...keyed, enabled: true })
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Switch on' }))
    const form = within(detail).getByLabelText('Reason').closest('form')!
    fireEvent.click(within(form).getByRole('button', { name: 'Switch on' }))
    expect(await within(form).findByText('Say why it is being switched on.')).toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Commissioned' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Switch on' }))
    await waitFor(() => expect(api.setAnalyserSwitchedOn).toHaveBeenCalledWith('XN-1', true, 'Commissioned'))
  })
})

describe('registering', () => {
  it('checks the id, sends it in capitals, and offers only what Orb supports', async () => {
    signInAs('admin')
    registry()
    api.registerAnalyser.mockResolvedValue({ ...XN, id: 'AU-480', name: 'Chemistry analyser' })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Register an analyser' }))
    const form = screen.getByRole('form', { name: 'Register an analyser' })
    expect(within(form).getByRole('option', { name: 'ASTM (not supported yet)' })).toBeDisabled()
    fireEvent.change(within(form).getByLabelText(/Analyser id/), { target: { value: 'bad id!' } })
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Chemistry analyser' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register' }))
    expect(await within(form).findByText(/The analyser id is 2 to 32 letters/)).toBeInTheDocument()
    expect(api.registerAnalyser).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText(/Analyser id/), { target: { value: 'au-480' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register' }))
    await waitFor(() => expect(api.registerAnalyser).toHaveBeenCalled())
    expect(api.registerAnalyser.mock.calls[0][0]).toMatchObject({ id: 'AU-480', name: 'Chemistry analyser',
      protocol: 'hl7v2', transport: 'http' })
  })

  it('shows the server refusal in its own words', async () => {
    signInAs('admin')
    registry()
    api.registerAnalyser.mockRejectedValue(new LabApiError(
      "This address is on the internet. Orb only talks to machines on the hospital's own network.",
      422, 'host_not_local'))
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Register an analyser' }))
    const form = screen.getByRole('form', { name: 'Register an analyser' })
    fireEvent.change(within(form).getByLabelText(/Analyser id/), { target: { value: 'AU-480' } })
    fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Chemistry' } })
    fireEvent.change(within(form).getByLabelText('Address on the hospital network'),
      { target: { value: '8.8.8.8' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register' }))
    expect(await within(form).findByText(/This address is on the internet/)).toBeInTheDocument()
  })
})

describe('the code map', () => {
  it('needs a note and an exact factor, and offers only laboratory tests', async () => {
    signInAs('pathologist')
    registry()
    api.saveAnalyserMapping.mockResolvedValue(HB_MAP)
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Map a code' }))
    const form = within(detail).getByRole('form', { name: 'Map a code' })
    expect(within(form).queryByRole('option', { name: /Capillary blood ketones/ })).not.toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText(/Analyser code/), { target: { value: 'HGB' } })
    fireEvent.change(within(form).getByLabelText('Orb test'), { target: { value: 'LOINC:718-7' } })
    fireEvent.change(within(form).getByLabelText(/Multiply/), { target: { value: '0.0555' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await within(form).findByText(/at most three decimal places/)).toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText(/Multiply/), { target: { value: '1' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await within(form).findByText(/Add a note/)).toBeInTheDocument()
    expect(api.saveAnalyserMapping).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText('Note'), { target: { value: 'Host manual rev 4' } })
    fireEvent.change(within(form).getByLabelText(/Unit the analyser sends/), { target: { value: 'g/dL' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveAnalyserMapping).toHaveBeenCalledWith('XN-1', {
      instrument_code: 'HGB', test_id: 'LOINC:718-7', factor: '1', instrument_unit: 'g/dL',
      note: 'Host manual rev 4' }))
  })

  it('a text test takes no factor and no unit', async () => {
    signInAs('pathologist')
    registry()
    api.saveAnalyserMapping.mockResolvedValue(HB_MAP)
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Map a code' }))
    const form = within(detail).getByRole('form', { name: 'Map a code' })
    fireEvent.change(within(form).getByLabelText(/Analyser code/), { target: { value: 'BC' } })
    fireEvent.change(within(form).getByLabelText('Orb test'),
      { target: { value: 'LOCAL:ORB-LAB-BLOOD-CULTURE' } })
    expect(within(form).queryByLabelText(/Multiply/)).not.toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText('Note'), { target: { value: 'Middleware code list' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.saveAnalyserMapping).toHaveBeenCalledWith('XN-1', expect.objectContaining({
      factor: '1', instrument_unit: null })))
  })

  it('retiring a code needs a reason', async () => {
    signInAs('pathologist')
    registry({ ...XN, map: [HB_MAP], mapped_codes: 1 })
    api.retireAnalyserMapping.mockResolvedValue({ ...HB_MAP, active: false })
    renderView()
    const detail = await openAnalyser()
    fireEvent.click(await within(detail).findByRole('button', { name: 'Retire code HGB' }))
    const form = within(detail).getByRole('form', { name: 'Retire code HGB' })
    fireEvent.click(within(form).getByRole('button', { name: 'Retire' }))
    expect(await within(form).findByText('Say why this code is being retired.')).toBeInTheDocument()
    expect(api.retireAnalyserMapping).not.toHaveBeenCalled()
    fireEvent.change(within(form).getByLabelText('Reason'), { target: { value: 'Firmware renamed it' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Retire' }))
    await waitFor(() => expect(api.retireAnalyserMapping)
      .toHaveBeenCalledWith('XN-1', 'HGB', 'Firmware renamed it'))
  })
})

describe('words', () => {
  it('a factor is exact to three decimal places and above zero', () => {
    expect(factorProblem('1')).toBeNull()
    expect(factorProblem('0.001')).toBeNull()
    expect(factorProblem('')).toBeNull()
    expect(factorProblem('0.0555')).toMatch(/three decimal places/)
    expect(factorProblem('0')).toMatch(/above zero/)
    expect(factorProblem('-1')).toMatch(/three decimal places/)
    expect(factorProblem('1e3')).toMatch(/three decimal places/)
  })

  it('calibration reads from the biomedical register', () => {
    expect(calibrationState(null)).toBe('not_linked')
    expect(calibrationState({ id: 1, found: false })).toBe('not_found')
    expect(calibrationState({ id: 1, found: true, calibration: null })).toBe('no_schedule')
    expect(calibrationState({ id: 1, found: true, calibration: {
      last_done_date: null, next_due_date: '2026-01-01', overdue: true } })).toBe('overdue')
    expect(calibrationState({ id: 1, found: true, calibration: {
      last_done_date: null, next_due_date: '2027-01-01', overdue: false } })).toBe('due')
  })
})
