/**
 * Beds, Bed setup view (adt-4b). What it must never get wrong:
 *
 * 1. ONLY A BED MANAGER SEES IT. The view is gated on bed.master, so a role
 *    holding only adt.read never gets the tab.
 * 2. THE PREVIEW COMES BEFORE THE SAVE. Bulk registration shows every unit
 *    code ('<ward>/<bed>') before anything is sent, and registers exactly the
 *    beds previewed.
 * 3. ONE CLASH SAVES NOTHING. A 409 on one bed says, in the server's words,
 *    that nothing was saved, and the bed list is not changed.
 * 4. REGISTER COMPLETE SAYS WHY NOT. A refusal shows the server's sentence
 *    and the admissions and beds it names.
 * 5. A DOUBLED BED IS NEVER LINKED from here: it is listed with no link.
 * 6. CODES AND LABELS. A ward code or bed label the server will not change
 *    is refused in the server's words; a display name change sends only the
 *    name. Departments come from cover and the site configuration, and a ward
 *    nobody covers is flagged.
 * 7. No em dashes in anything this view or its client prints.
 */
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AdtApiError } from '../../../api/adt'
import type { BedMaster, Reconciliation } from '../../../api/adt/master'
import { previewBeds } from '../../../api/adt/master'
import { discoverViews, visibleViews } from './viewRegistry'

const api = vi.hoisted(() => ({
  getBedMaster: vi.fn(),
  createWard: vi.fn(),
  updateWard: vi.fn(),
  createRoom: vi.fn(),
  updateRoom: vi.fn(),
  registerBeds: vi.fn(),
  updateBed: vi.fn(),
  retireBed: vi.fn(),
  createBedClass: vi.fn(),
  updateBedClass: vi.fn(),
  importSiteConfigWards: vi.fn(),
  getReconciliation: vi.fn(),
  markRegisterComplete: vi.fn(),
}))
const linkAdmissionsToBeds = vi.hoisted(() => vi.fn())

vi.mock('../../../api/adt/master', async () => {
  const actual = await vi.importActual<typeof import('../../../api/adt/master')>(
    '../../../api/adt/master')
  return { ...actual, ...api }
})
vi.mock('../../../api/adt/board', async () => {
  const actual = await vi.importActual<typeof import('../../../api/adt/board')>(
    '../../../api/adt/board')
  return { ...actual, linkAdmissionsToBeds }
})

import SetupView, * as setupModule from './SetupView'

function master(over: Partial<BedMaster> = {}): BedMaster {
  return {
    facility_code: 'MAIN',
    wards: [
      {
        id: 'WRD-GM', facility_code: 'MAIN', code: 'GM-1', name: 'General ward one',
        department: 'General Medicine', ward_type: 'general', default_bed_class: null,
        cleaning_tracked: true, register_complete: false, register_complete_at: null,
        register_complete_by: null, ledger_complete_from: null, is_active: true,
        has_cover: true, cover_note: null, beds: 2, retired_beds: 0, rooms: 0,
      },
      {
        id: 'WRD-ON', facility_code: 'MAIN', code: 'ONC-1', name: 'ONC-1',
        department: 'Oncology', ward_type: 'general', default_bed_class: null,
        cleaning_tracked: true, register_complete: false, register_complete_at: null,
        register_complete_by: null, ledger_complete_from: null, is_active: true,
        has_cover: false, cover_note: 'Admissions refused until a doctor or nurse covers Oncology.',
        beds: 0, retired_beds: 0, rooms: 0,
      },
    ],
    rooms: [],
    beds: [
      {
        id: 'BED-3', unit_code: 'GM-1/3', ward_id: 'WRD-GM', ward: 'GM-1', room_id: null,
        room: null, bed_label: '3', bed_class: null, is_isolation_room: false,
        is_temporary: false, is_active: true, retired_at: null, state: 'vacant_ready',
        state_since: null, notes: null,
      },
      {
        id: 'BED-4', unit_code: 'GM-1/4', ward_id: 'WRD-GM', ward: 'GM-1', room_id: null,
        room: null, bed_label: '4', bed_class: null, is_isolation_room: false,
        is_temporary: false, is_active: true, retired_at: null, state: 'occupied',
        state_since: null, notes: null,
      },
    ],
    bed_classes: [],
    housekeeping_beds_not_in_setup: [],
    departments: { covering: ['General Medicine', 'Cardiology'], declared: ['Oncology', 'cardiology'] },
    ward_types: ['general', 'icu', 'daycare'],
    room_types: ['bay', 'semi_private', 'isolation'],
    max_beds_per_registration: 200,
    ...over,
  }
}

function entry(id: string, over: Record<string, unknown> = {}) {
  return {
    admission_id: id, patient_id: `P-${id}`, department: 'General Medicine', ward: 'GM-1',
    bed: '3', ward_id: null, bed_id: null, master_ward_id: 'WRD-GM', master_ward_code: 'GM-1',
    master_bed_id: 'BED-3', master_bed_label: '3', master_unit_code: 'GM-1/3', reason: null,
    ...over,
  }
}

const onModuleOff = vi.fn()

async function renderView() {
  await act(async () => {
    render(
      <SetupView meta={{}} openView={() => {}} onModuleOff={onModuleOff}
        holds={() => true} />,
    )
  })
}

async function openGeneralWard() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Open ward GM-1' })) })
  return screen.getByRole('region', { name: 'Ward GM-1' })
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
  linkAdmissionsToBeds.mockReset()
  onModuleOff.mockReset()
  api.getBedMaster.mockResolvedValue(master())
})

afterEach(cleanup)

describe('Bed setup view', () => {
  it('is shown only to a role holding bed.master', () => {
    expect(setupModule.viewId).toBe('setup')
    expect(setupModule.viewLabel).toBe('Bed setup')
    expect(setupModule.viewOrder).toBe(90)
    expect(setupModule.viewPermission).toBe('bed.master')
    const views = discoverViews({ './adt/SetupView.tsx': setupModule })
    expect(views.map((v) => v.id)).toEqual(['setup'])
    const readerOnly = (p: string) => p === 'adt.read'
    expect(visibleViews(views, readerOnly)).toEqual([])
    const manager = (p: string) => p === 'adt.read' || p === 'bed.master'
    expect(visibleViews(views, manager).map((v) => v.id)).toEqual(['setup'])
  })

  it('hands a switched-off module to the panel', async () => {
    api.getBedMaster.mockRejectedValue(new AdtApiError('off', 404, 'module_disabled'))
    await renderView()
    expect(onModuleOff).toHaveBeenCalled()
  })

  it('lists wards with the server cover note and offers covered and declared departments', async () => {
    await renderView()
    expect(screen.getByText('Admissions refused until a doctor or nurse covers Oncology.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Set up a ward' }))
    const form = screen.getByRole('form', { name: 'Set up a ward' })
    const department = within(form).getByLabelText('Department') as HTMLSelectElement
    const names = Array.from(department.options).map((o) => o.textContent)
    expect(names).toEqual([
      'Choose...', 'General Medicine', 'Cardiology', 'Oncology (no doctor or nurse covers it yet)',
    ])
    const types = Array.from((within(form).getByLabelText('Ward type') as HTMLSelectElement).options)
      .map((o) => o.textContent)
    expect(types).toEqual(['General', 'ICU', 'Day care'])
  })

  it('sets up a ward with the department chosen and opens it', async () => {
    api.createWard.mockResolvedValue({ ...master().wards[0], id: 'WRD-NEW', code: 'CA-2', department: 'Cardiology' })
    api.getBedMaster.mockResolvedValueOnce(master()).mockResolvedValue(master({
      wards: [...master().wards, { ...master().wards[0], id: 'WRD-NEW', code: 'CA-2', department: 'Cardiology' }],
    }))
    await renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Set up a ward' }))
    const form = screen.getByRole('form', { name: 'Set up a ward' })
    fireEvent.change(within(form).getByLabelText('Ward code'), { target: { value: 'CA-2' } })
    fireEvent.change(within(form).getByLabelText('Department'), { target: { value: 'Cardiology' } })
    await act(async () => { fireEvent.click(within(form).getByRole('button', { name: 'Save the ward' })) })
    expect(api.createWard).toHaveBeenCalledWith(expect.objectContaining({
      code: 'CA-2', department: 'Cardiology', ward_type: 'general', cleaning_tracked: true }))
    expect(await screen.findByRole('region', { name: 'Ward CA-2' })).toBeInTheDocument()
  })

  it('shows every unit code before saving, and registers exactly the beds previewed', async () => {
    api.registerBeds.mockResolvedValue({ ward_id: 'WRD-GM', ward_code: 'GM-1', registered: [{}, {}, {}],
      adopted: [], count: 3 })
    await renderView()
    const ward = await openGeneralWard()
    const form = within(ward).getByRole('form', { name: 'Register beds on GM-1' })
    fireEvent.change(within(form).getByLabelText('From'), { target: { value: '1' } })
    fireEvent.change(within(form).getByLabelText('To'), { target: { value: '3' } })
    fireEvent.change(within(form).getByLabelText('Digits (leading zeros)'), { target: { value: '2' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Preview' }))
    const preview = within(form).getByRole('region', { name: 'Beds to register' })
    expect(within(preview).getAllByRole('listitem').map((li) => li.textContent))
      .toEqual(['GM-1/01', 'GM-1/02', 'GM-1/03'])
    expect(api.registerBeds).not.toHaveBeenCalled()

    await act(async () => { fireEvent.click(within(preview).getByRole('button', { name: 'Register 3 beds' })) })
    expect(api.registerBeds).toHaveBeenCalledWith(expect.objectContaining({
      ward_id: 'WRD-GM', labels: ['01', '02', '03'] }))
    expect(await screen.findByText('3 beds registered on GM-1.')).toBeInTheDocument()
  })

  it('clears the preview when what would be registered changes', async () => {
    await renderView()
    const ward = await openGeneralWard()
    const form = within(ward).getByRole('form', { name: 'Register beds on GM-1' })
    fireEvent.click(within(form).getByRole('radio', { name: 'A list of labels' }))
    fireEvent.change(within(form).getByLabelText(/Bed labels/), { target: { value: '12, 12A\n14' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Preview' }))
    expect(within(form).getByRole('region', { name: 'Beds to register' })).toHaveTextContent('GM-1/12A')
    fireEvent.change(within(form).getByLabelText(/Bed labels/), { target: { value: '12, 12A\n15' } })
    expect(within(form).queryByRole('region', { name: 'Beds to register' })).toBeNull()
  })

  it('a clash on one bed says nothing was saved and leaves the beds as they were', async () => {
    api.registerBeds.mockRejectedValue(new AdtApiError(
      'GM-1 already has beds 3', 409, 'labels_taken',
      { error: 'labels_taken', message: 'GM-1 already has beds 3', labels: ['3'] }))
    await renderView()
    const ward = await openGeneralWard()
    const form = within(ward).getByRole('form', { name: 'Register beds on GM-1' })
    fireEvent.change(within(form).getByLabelText('From'), { target: { value: '1' } })
    fireEvent.change(within(form).getByLabelText('To'), { target: { value: '4' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Preview' }))
    const loads = api.getBedMaster.mock.calls.length
    await act(async () => { fireEvent.click(within(form).getByRole('button', { name: 'Register 4 beds' })) })
    expect(within(form).getByRole('alert')).toHaveTextContent('GM-1 already has beds 3. Nothing was saved.')
    expect(api.getBedMaster.mock.calls.length).toBe(loads)
    const beds = within(ward).getByRole('list', { name: 'Beds on GM-1' })
    expect(within(beds).getAllByRole('listitem')).toHaveLength(2)
  })

  it('does not repeat nothing was saved when the server already said it', async () => {
    api.registerBeds.mockRejectedValue(new AdtApiError(
      'These bed labels appear more than once: 2. Nothing was saved.', 409, 'labels_repeated'))
    await renderView()
    const ward = await openGeneralWard()
    const form = within(ward).getByRole('form', { name: 'Register beds on GM-1' })
    fireEvent.change(within(form).getByLabelText('To'), { target: { value: '2' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Preview' }))
    await act(async () => { fireEvent.click(within(form).getByRole('button', { name: 'Register 2 beds' })) })
    expect(within(form).getByRole('alert').textContent)
      .toBe('These bed labels appear more than once: 2. Nothing was saved.')
  })

  it('register complete shows the server refusal and the admissions and beds it names', async () => {
    const words = 'GM-1 cannot be marked complete: 1 admissions are not linked to a bed in setup and '
      + '1 beds are claimed by more than one admission. Link or correct them first.'
    api.markRegisterComplete.mockRejectedValue(new AdtApiError(words, 409, 'register_incomplete', {
      error: 'register_incomplete', message: words, unlinked: ['ADM-UN1'], doubled: ['BED-4'] }))
    await renderView()
    const ward = await openGeneralWard()
    await act(async () => {
      fireEvent.click(within(ward).getByRole('button', { name: 'Mark the register complete' }))
    })
    const alert = within(ward).getByRole('alert')
    expect(alert).toHaveTextContent(words)
    expect(alert).toHaveTextContent('Admissions not linked to a bed: ADM-UN1')
    expect(alert).toHaveTextContent('Beds claimed by more than one admission: GM-1/4')
  })

  it('lists a doubled bed with no link and links a matched admission', async () => {
    const report: Reconciliation = {
      ward_id: 'WRD-GM', ward_code: 'GM-1', linked: [],
      matched: [entry('ADM-M1')],
      doubled: [{ bed_id: 'BED-4', unit_code: 'GM-1/4', bed_label: '4', ward_id: 'WRD-GM',
        admissions: [entry('ADM-D1', { bed: '4', master_unit_code: 'GM-1/4', reason: 'doubled_bed' }),
          entry('ADM-D2', { bed: '4', master_unit_code: 'GM-1/4', reason: 'doubled_bed' })] }],
      unlinked: [entry('ADM-U1', { ward: 'Old ward', master_unit_code: null, reason: 'ward_not_in_setup' })],
      awaiting: [],
      mirror_mismatches: [],
      counts: { linked: 0, matched: 1, doubled_beds: 1, unlinked: 1, awaiting: 0 },
    }
    api.getReconciliation.mockResolvedValue(report)
    linkAdmissionsToBeds.mockResolvedValue({ linked: [{ admission_id: 'ADM-M1', bed_id: 'BED-3',
      ward_id: 'WRD-GM', ward: 'GM-1', bed: '3', movement_id: 'MOV-1', boarder: false }],
    skipped: [], count: 1 })
    await renderView()
    const ward = await openGeneralWard()
    await act(async () => {
      fireEvent.click(within(ward).getByRole('button', { name: 'Check admissions on this ward' }))
    })
    expect(api.getReconciliation).toHaveBeenCalledWith('WRD-GM')
    const doubled = within(ward).getByRole('region', { name: 'Beds claimed twice' })
    expect(within(doubled).getByText('Admission ADM-D1')).toBeInTheDocument()
    expect(within(doubled).getByText('Admission ADM-D2')).toBeInTheDocument()
    expect(within(doubled).queryAllByRole('button')).toHaveLength(0)
    const unlinked = within(ward).getByRole('region', { name: 'Not in bed setup' })
    expect(within(unlinked).getByText('The typed ward is not in bed setup')).toBeInTheDocument()
    expect(within(unlinked).queryAllByRole('button')).toHaveLength(0)

    const matched = within(ward).getByRole('region', { name: 'Can be linked' })
    await act(async () => {
      fireEvent.click(within(matched).getByRole('button', { name: 'Link to bed GM-1/3' }))
    })
    expect(linkAdmissionsToBeds).toHaveBeenCalledWith({ admission_ids: ['ADM-M1'] })
  })

  it('asks for the boarder words before linking a boarder', async () => {
    api.getReconciliation.mockResolvedValue({
      ward_id: 'WRD-GM', ward_code: 'GM-1', linked: [], matched: [entry('ADM-B1')], doubled: [],
      unlinked: [], awaiting: [], mirror_mismatches: [], counts: {},
    })
    const boarderWords = 'Nurses on GM-1 (General Medicine) will not be able to open this chart. The Cardiology team keeps it.'
    linkAdmissionsToBeds
      .mockResolvedValueOnce({ linked: [], count: 0,
        skipped: [{ admission_id: 'ADM-B1', code: 'boarder_acknowledgement_required', message: boarderWords }] })
      .mockResolvedValueOnce({ linked: [{ admission_id: 'ADM-B1' }], skipped: [], count: 1 })
    await renderView()
    const ward = await openGeneralWard()
    await act(async () => {
      fireEvent.click(within(ward).getByRole('button', { name: 'Check admissions on this ward' }))
    })
    await act(async () => {
      fireEvent.click(within(ward).getByRole('button', { name: 'Link to bed GM-1/3' }))
    })
    expect(within(ward).getByText(boarderWords)).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(within(ward).getByRole('button', { name: 'I have read this. Link as a boarder' }))
    })
    expect(linkAdmissionsToBeds).toHaveBeenLastCalledWith({ admission_ids: ['ADM-B1'], boarder_acknowledged: true })
  })

  it('a ward code in use is refused in the server words; a display name change sends only the name', async () => {
    const words = 'The ward code GM-1 is already in use (2 beds), so it cannot be changed. Change the display name instead, or set up a new ward.'
    api.updateWard.mockRejectedValueOnce(new AdtApiError(words, 409, 'code_in_use'))
    api.updateWard.mockResolvedValueOnce({ ...master().wards[0], name: 'Medicine one' })
    await renderView()
    const ward = await openGeneralWard()
    const details = within(ward).getByRole('form', { name: 'Ward GM-1 details' })
    fireEvent.change(within(details).getByLabelText('Ward code'), { target: { value: 'GM-9' } })
    await act(async () => { fireEvent.click(within(details).getByRole('button', { name: 'Save changes' })) })
    expect(api.updateWard).toHaveBeenLastCalledWith('WRD-GM', { code: 'GM-9' })
    expect(within(details).getByRole('alert').textContent).toBe(words)

    fireEvent.change(within(details).getByLabelText('Ward code'), { target: { value: 'GM-1' } })
    fireEvent.change(within(details).getByLabelText('Display name'), { target: { value: 'Medicine one' } })
    await act(async () => { fireEvent.click(within(details).getByRole('button', { name: 'Save changes' })) })
    expect(api.updateWard).toHaveBeenLastCalledWith('WRD-GM', { name: 'Medicine one' })
  })

  it('a bed label in use is refused in the server words', async () => {
    const words = 'Bed 3 on GM-1 is already in use (1 admissions), so its label cannot be changed.'
    api.updateBed.mockRejectedValue(new AdtApiError(words, 409, 'label_in_use'))
    await renderView()
    const ward = await openGeneralWard()
    const beds = within(ward).getByRole('list', { name: 'Beds on GM-1' })
    fireEvent.click(within(beds).getByRole('button', { name: 'Change label of bed 3' }))
    fireEvent.change(within(beds).getByLabelText('Label of bed 3'), { target: { value: '3B' } })
    await act(async () => { fireEvent.click(within(beds).getByRole('button', { name: 'Save label of bed 3' })) })
    expect(api.updateBed).toHaveBeenCalledWith('BED-3', { bed_label: '3B' })
    await waitFor(() => expect(within(beds).getByRole('alert').textContent).toBe(words))
  })

  it('a bed with a patient in it is not retired, in the server words', async () => {
    const words = 'Bed 4 on GM-1 has a patient in it, so it cannot be retired. Move the patient first.'
    api.retireBed.mockRejectedValue(new AdtApiError(words, 409, 'bed_occupied'))
    await renderView()
    const ward = await openGeneralWard()
    const beds = within(ward).getByRole('list', { name: 'Beds on GM-1' })
    fireEvent.click(within(beds).getByRole('button', { name: 'Retire bed 4' }))
    const confirm = within(beds).getByRole('group', { name: 'Retire bed 4' })
    await act(async () => { fireEvent.click(within(confirm).getByRole('button', { name: 'Retire bed 4' })) })
    expect(api.retireBed).toHaveBeenCalledWith('BED-4', undefined)
    expect(within(beds).getByRole('alert').textContent).toBe(words)
  })

  it('imports the site configuration wards and says what it did', async () => {
    api.importSiteConfigWards.mockResolvedValue({
      created: [master().wards[0]], existing: [],
      conflicts: [{ code: 'ICU', ward_id: 'WRD-ICU', department_in_setup: 'Critical Care',
        department_in_file: 'Anaesthesia' }],
      skipped: [], file_present: true, message: null,
    })
    await renderView()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Import wards from the site configuration' }))
    })
    expect(screen.getByText('1 ward set up, 0 already set up.')).toBeInTheDocument()
    expect(screen.getByText(/Ward ICU is set up under Critical Care, but the site configuration puts it under Anaesthesia/))
      .toBeInTheDocument()
  })
})

describe('previewBeds', () => {
  const base = { wardCode: 'GM-1', labelsText: '', prefix: '', start: '1', end: '', pad: '0', max: 200 }

  it('expands a range with a prefix and padding as the server does', () => {
    const out = previewBeds({ ...base, mode: 'range', prefix: 'B ', start: '8', end: '10', pad: '2' })
    expect(out.problem).toBeNull()
    expect(out.unitCodes).toEqual(['GM-1/B08', 'GM-1/B09', 'GM-1/B10'])
  })

  it('refuses repeated labels, a slash and too many beds before anything is sent', () => {
    expect(previewBeds({ ...base, mode: 'labels', labelsText: '1, 2, 1' }).problem)
      .toBe('These bed labels appear more than once: 1.')
    expect(previewBeds({ ...base, mode: 'labels', labelsText: '1/2' }).problem).toMatch(/cannot contain/)
    expect(previewBeds({ ...base, mode: 'range', end: '500' }).problem)
      .toBe('At most 200 beds can be registered at once.')
    expect(previewBeds({ ...base, mode: 'range', start: '5', end: '2' }).problem).toMatch(/at or above/)
  })
})

/** Built from its code point, so this file holds no em dash itself. */
const EM_DASH = String.fromCharCode(0x2014)

describe('words on screen', () => {
  it('has no em dash in the view, its styles or its client', () => {
    const here = dirname(fileURLToPath(import.meta.url))
    const files = [
      join(here, 'SetupView.tsx'),
      join(here, 'SetupView.css'),
      join(here, '..', '..', '..', 'api', 'adt', 'master.ts'),
    ]
    for (const file of files) {
      expect(readFileSync(file, 'utf8').includes(EM_DASH), file).toBe(false)
    }
  })
})
