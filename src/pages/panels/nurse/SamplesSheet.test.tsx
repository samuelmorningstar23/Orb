/**
 * The Samples view (lis-8): bedside collection.
 *
 *   · Nothing prints until the typed UHID and name both match; a mismatch says
 *     which identifier failed and prints nothing.
 *   · Only after the check does a tube offer "Mark collected", and the
 *     collection sends the same identity.
 *   · A failed load is never an empty list; an empty list names its scope.
 *   · The laboratory's list groups every ward.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { CollectionList, CollectionSpecimen } from '../../../api/lab/specimens'

const getCollectionList = vi.fn()
const printSpecimenLabels = vi.fn()
const collectSpecimen = vi.fn()

vi.mock('../../../api/lab/specimens', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/specimens')>(
    '../../../api/lab/specimens')
  return {
    ...actual,
    getCollectionList: (...a: unknown[]) => getCollectionList(...a),
    printSpecimenLabels: (...a: unknown[]) => printSpecimenLabels(...a),
    collectSpecimen: (...a: unknown[]) => collectSpecimen(...a),
  }
})

vi.mock('../../../components/print/SpecimenLabelPrintView', () => ({
  default: ({ labels }: { labels: { specimen_id: string }[] }) => (
    <div data-testid="label-print">{labels.map(l => l.specimen_id).join(',')}</div>
  ),
}))

// For the Samples tab in My shift.
const getShiftBoard = vi.fn()
vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

import { clearOfflineCache } from '../../../lib/offline/phiCache'
import { resetModuleFlags, setModuleFlags } from '../../../shellConfig'
import NursePanel from './NursePanel'
import SamplesSheet from './SamplesSheet'

const ASHA = { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A', ward: 'GM-1',
  department: 'General Medicine' }
const RAVI = { id: 'P-RAVI', name: 'Ravi Kumar', mrn: 'MRN-0077', bed: '2B', ward: 'CARD-1',
  department: 'Cardiology' }

function tube(over: Partial<CollectionSpecimen> = {}): CollectionSpecimen {
  return {
    id: '261004000127',
    patient: ASHA,
    specimen_type_code: 'serum',
    specimen_display: 'Serum',
    container: 'Gold top (SST)',
    container_colour_name: 'gold',
    priority: 'routine',
    status: 'planned',
    planned_at: '2026-10-04T08:00:00+00:00',
    label_print_count: 0,
    label_printed_at: null,
    recollection_needed: false,
    rejection_code: null,
    rejection_note: null,
    tests: [{ item_id: 1, order_id: 'O-1', test_id: 'LOINC:2823-3', status: 'awaiting_collection',
      name: 'Potassium', short_name: 'Potassium' }],
    ...over,
  }
}

function list(specimens: CollectionSpecimen[], dept: string | null = 'General Medicine'): CollectionList {
  return { specimens, scope: { department: dept, hospital_wide: dept === null }, backdate_minutes: 0 }
}

beforeEach(() => {
  getCollectionList.mockReset()
  printSpecimenLabels.mockReset()
  collectSpecimen.mockReset()
})
afterEach(() => {
  vi.clearAllMocks()
})

async function openIdentity(name = 'Asha Menon') {
  const card = (await screen.findByText(name)).closest('li') as HTMLElement
  fireEvent.click(within(card).getByRole('button', { name: 'Check identity and print labels' }))
  return card
}

function typeIdentity(card: HTMLElement, uhid: string, name: string) {
  fireEvent.change(within(card).getByLabelText('UHID from the wristband'), { target: { value: uhid } })
  fireEvent.change(within(card).getByLabelText('Name as the patient says it'), { target: { value: name } })
}

describe('SamplesSheet', () => {
  it('lists the ward\'s samples by patient, STAT first, with the label state', async () => {
    getCollectionList.mockResolvedValue(list([
      tube({ id: '261004000135', priority: 'stat', tests: [{ item_id: 2, order_id: 'O-2',
        test_id: 'LOINC:2160-0', status: 'awaiting_collection', name: 'Creatinine', short_name: 'Creatinine' }] }),
      tube(),
    ]))
    render(<SamplesSheet />)
    const card = (await screen.findByText('Asha Menon')).closest('li') as HTMLElement
    expect(within(card).getByText(/Bed 4A · UHID MRN-0042/)).toBeTruthy()
    expect(within(card).getByText('STAT')).toBeTruthy()
    expect(within(card).getAllByText(/Label not printed/)).toHaveLength(2)
    expect(within(card).getByText(/261004 00013 5/)).toBeTruthy()
    expect(screen.getByText(/2 samples on General Medicine/)).toBeTruthy()
    // No collection before the identity check.
    expect(within(card).queryByRole('button', { name: 'Mark collected' })).toBeNull()
  })

  it('prints nothing until both identifiers are typed, then prints and offers collection', async () => {
    // The first read, then the read after the print, which shows the label on the tube.
    getCollectionList.mockResolvedValueOnce(list([tube()])).mockResolvedValue(
      list([tube({ label_print_count: 1, label_printed_at: '2026-10-04T08:10:00+00:00' })]))
    printSpecimenLabels.mockResolvedValue({
      labels: [{ specimen_id: '261004000127', patient: { name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A', ward: 'GM-1' },
        specimen_display: 'Serum', container: 'Gold top (SST)', container_colour_name: 'gold',
        priority: 'routine', tests: ['Potassium'], print_number: 1 }],
      printed_at: '2026-10-04T08:10:00+00:00',
    })
    collectSpecimen.mockResolvedValue({ specimen: { id: '261004000127', status: 'collected',
      collected_at: '2026-10-04T08:12:00+00:00' }, items_collected: 1, orders: [] })
    render(<SamplesSheet />)
    const card = await openIdentity()
    const printBtn = within(card).getByRole('button', { name: 'Print label' })
    expect((printBtn as HTMLButtonElement).disabled).toBe(true)
    typeIdentity(card, 'MRN-0042', '')
    expect((printBtn as HTMLButtonElement).disabled).toBe(true)
    typeIdentity(card, ' MRN-0042 ', 'Asha Menon')
    fireEvent.click(printBtn)
    await waitFor(() => expect(printSpecimenLabels).toHaveBeenCalledTimes(1))
    expect(printSpecimenLabels).toHaveBeenCalledWith(['261004000127'],
      { uhid: 'MRN-0042', name: 'Asha Menon' })
    expect((await screen.findByTestId('label-print')).textContent).toBe('261004000127')

    const collect = await within(card).findByRole('button', { name: 'Mark collected' })
    expect((collect as HTMLButtonElement).disabled).toBe(false)
    await waitFor(() => expect(getCollectionList).toHaveBeenCalledTimes(2))
    fireEvent.click(collect)
    await waitFor(() => expect(collectSpecimen).toHaveBeenCalledWith('261004000127',
      { uhid: 'MRN-0042', name: 'Asha Menon' }))
    expect(await screen.findByText(/recorded as collected for Asha Menon/)).toBeTruthy()
  })

  it('says which identifier did not match and prints nothing', async () => {
    getCollectionList.mockResolvedValue(list([tube()]))
    printSpecimenLabels.mockRejectedValue(new LabApiError('refused', 409, 'identity_not_confirmed',
      { error: 'identity_not_confirmed', checks: { uhid: 'no_match', name: 'match' } }))
    render(<SamplesSheet />)
    const card = await openIdentity()
    typeIdentity(card, 'MRN-9999', 'Asha Menon')
    fireEvent.click(within(card).getByRole('button', { name: 'Print label' }))
    const alert = await within(card).findByRole('alert')
    expect(alert.textContent).toContain('The UHID does not match this patient\'s record.')
    expect(alert.textContent).not.toContain('MRN-0042')
    expect(screen.queryByTestId('label-print')).toBeNull()
    expect(within(card).queryByRole('button', { name: 'Mark collected' })).toBeNull()
  })

  it('lets labels already on the tubes go straight to collection, the identity still sent', async () => {
    getCollectionList.mockResolvedValue(list([tube({ label_print_count: 1,
      label_printed_at: '2026-10-04T07:00:00+00:00' })]))
    collectSpecimen.mockRejectedValue(new LabApiError('refused', 409, 'identity_not_confirmed',
      { error: 'identity_not_confirmed', checks: { uhid: 'match', name: 'no_match' } }))
    render(<SamplesSheet />)
    const card = await openIdentity()
    typeIdentity(card, 'MRN-0042', 'Asha Mennon')
    fireEvent.click(within(card).getByRole('button', { name: 'Labels are already on the tubes' }))
    expect(printSpecimenLabels).not.toHaveBeenCalled()
    fireEvent.click(within(card).getByRole('button', { name: 'Mark collected' }))
    const alert = await within(card).findByRole('alert')
    expect(alert.textContent).toContain('The name does not match this patient\'s record.')
    // A refused identity is forgotten: the check starts again.
    expect(within(card).queryByRole('button', { name: 'Mark collected' })).toBeNull()
  })

  it('says new collections have stopped while the module drains', async () => {
    getCollectionList.mockResolvedValue(list([tube()]))
    printSpecimenLabels.mockRejectedValue(new LabApiError('off', 404, 'module_disabled',
      { error: 'module_disabled', module: 'lab' }))
    render(<SamplesSheet />)
    const card = await openIdentity()
    typeIdentity(card, 'MRN-0042', 'Asha Menon')
    fireEvent.click(within(card).getByRole('button', { name: 'Print label' }))
    expect((await within(card).findByRole('alert')).textContent).toContain(
      'New collections have stopped')
  })

  it('shows a rejected sample as recollection needed, with no collection button', async () => {
    getCollectionList.mockResolvedValue(list([tube({ status: 'rejected', recollection_needed: true,
      rejection_note: 'Haemolysed' })]))
    render(<SamplesSheet />)
    expect(await screen.findByText(/Recollection needed: the laboratory rejected this sample \(Haemolysed\)/))
      .toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Check identity and print labels' })).toBeNull()
  })

  it('names the laboratory\'s reason and marks the new tube a recollection (lis-9)', async () => {
    getCollectionList.mockResolvedValue(list([
      tube({ id: '261006000135', status: 'rejected', recollection_needed: true,
        rejection_code: 'clotted', rejection_display: 'Clotted', rejection_note: 'Small clot' }),
      tube({ id: '261006000143', recollect_of: '261006000127' }),
    ]))
    render(<SamplesSheet />)
    expect(await screen.findByText(/the laboratory rejected this sample \(Clotted: Small clot\)/))
      .toBeTruthy()
    expect(screen.getByText(/Recollection of 261006 00012 7, which the laboratory rejected/))
      .toBeTruthy()
    // The new tube is collected like any other.
    expect(screen.getByRole('button', { name: 'Check identity and print labels' })).toBeTruthy()
  })

  it('never shows a failed load as an empty list', async () => {
    getCollectionList.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    render(<SamplesSheet />)
    expect((await screen.findByRole('alert')).textContent).toContain('not an empty list')
    expect(screen.queryByText(/No samples waiting/)).toBeNull()
  })

  it('names the scope of an empty list', async () => {
    getCollectionList.mockResolvedValue(list([]))
    render(<SamplesSheet />)
    expect(await screen.findByText(/No samples waiting to be collected on General Medicine/)).toBeTruthy()
  })

  it('says so when the laboratory module is switched off', async () => {
    getCollectionList.mockRejectedValue(new LabApiError('off', 404, 'module_disabled',
      { error: 'module_disabled', module: 'lab' }))
    render(<SamplesSheet />)
    expect(await screen.findByText(/laboratory module is switched off on this site/)).toBeTruthy()
  })

  it('groups the laboratory\'s list by ward', async () => {
    getCollectionList.mockResolvedValue(list([
      tube(),
      tube({ id: '261004000143', patient: RAVI }),
    ], null))
    render(<SamplesSheet variant="laboratory" />)
    expect(await screen.findByRole('heading', { name: 'GM-1' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'CARD-1' })).toBeTruthy()
    expect(screen.getByText(/2 samples on every ward/)).toBeTruthy()
    const cardiology = screen.getByRole('region', { name: 'CARD-1' })
    expect(within(cardiology).getByText('Ravi Kumar')).toBeTruthy()
    expect(within(cardiology).queryByText('Asha Menon')).toBeNull()
  })
})

describe('the Samples tab in My shift', () => {
  const BOARD = {
    department: 'General Medicine',
    generated_at: '2026-10-04T08:00:00+00:00',
    patients: [],
    totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
  }
  const signIn = (role: string) =>
    localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR-S', name: 'Nurse S', role }))

  beforeEach(() => {
    getShiftBoard.mockReset()
    getShiftBoard.mockResolvedValue(BOARD)
  })
  afterEach(() => {
    localStorage.clear()
    resetModuleFlags()
    clearOfflineCache()
  })

  it('is absent while the laboratory module is off, so the strip is unchanged', async () => {
    signIn('nurse')
    render(<NursePanel />)
    await screen.findByRole('tab', { name: /Due now/ })
    expect(screen.queryByRole('tab', { name: 'Samples' })).toBeNull()
    expect(getCollectionList).not.toHaveBeenCalled()
  })

  it('appears for a nurse once the module is on and opens her samples list', async () => {
    signIn('nurse')
    setModuleFlags({ lab: true })
    getCollectionList.mockResolvedValue(list([tube()]))
    render(<NursePanel />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Samples' }))
    expect(await screen.findByText('Asha Menon')).toBeTruthy()
    expect(getCollectionList).toHaveBeenCalledTimes(1)
  })

  it('is absent for an account that cannot collect', async () => {
    signIn('admin')
    setModuleFlags({ lab: true })
    render(<NursePanel />)
    await screen.findByRole('tab', { name: /Due now/ })
    expect(screen.queryByRole('tab', { name: 'Samples' })).toBeNull()
  })
})
