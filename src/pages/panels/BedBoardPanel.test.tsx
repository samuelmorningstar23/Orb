/**
 * BedBoardPanel, its Board and Awaiting a bed views, and the two registries
 * that find views and tile actions (adt-4a). What they must never get wrong:
 *
 * 1. SWITCHED OFF IS NOT EMPTY. A 404 module_disabled is the shell's
 *    switched-off notice, never an empty board; and with ORB_ADT off the Beds
 *    tab is not on anybody's rail even though this file is in the build.
 * 2. THE SERVER WRITES THE WORDS. A tile prints status_words and markers as
 *    sent, styles by tone, and an unknown status or tone still renders (as
 *    neutral), so later statuses need no edit here.
 * 3. NO NAME OUTSIDE SCOPE. A tile the server sent without an occupant shows
 *    no name; there is nothing to show.
 * 4. THE BOARD POLLS every 30 seconds, and a failed refresh keeps the last
 *    board on screen and says when it was read.
 * 5. AWAITING AND UNLINKED ARE TWO LISTS. Only a bed manager sees 'Link to
 *    bed', and a link the server answers with words is made only after the
 *    words are confirmed.
 * 6. ACTIONS ARE FOUND, NOT NAMED, and filtered by permission, status and
 *    whether the occupant may be seen.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import i18n from '../../i18n'
import { AdtApiError } from '../../api/adt'
import type { BedBoard, BedTile } from '../../api/adt/board'
import {
  actionIdFromPath,
  actionsForTile,
  discoverTileActions,
  type TileActionProps,
} from '../../components/adt/actions'
import {
  DEFAULT_MODULE_FLAGS,
  getAccessibleTabs,
  hospitalModuleInstalled,
} from '../../shellConfig'
import BedBoardPanel from './BedBoardPanel'
import AwaitingView from './adt/AwaitingView'
import BoardView from './adt/BoardView'
import {
  discoverViews,
  labelFromId,
  viewIdFromPath,
  type AdtViewEntry,
  type AdtViewProps,
} from './adt/viewRegistry'

vi.mock('../../api/adt', async () => {
  const actual = await vi.importActual<typeof import('../../api/adt')>('../../api/adt')
  return { ...actual, getAdtMeta: vi.fn() }
})

vi.mock('../../api/adt/board', async () => {
  const actual = await vi.importActual<typeof import('../../api/adt/board')>('../../api/adt/board')
  return {
    ...actual,
    getBedBoard: vi.fn(),
    getAwaitingBed: vi.fn(),
    linkAdmissionsToBeds: vi.fn(),
  }
})

vi.mock('../../components/adt/actions', async () => {
  const actual = await vi.importActual<typeof import('../../components/adt/actions')>(
    '../../components/adt/actions')
  function MarkCleanAction({ tile, onDone, onClose }: TileActionProps) {
    return (
      <div>
        <p>Cleaning bed {tile.bed_label}</p>
        <button type="button" onClick={onDone}>Finish</button>
        <button type="button" onClick={onClose}>Close</button>
      </div>
    )
  }
  function NoteAction() {
    return <p>Note body</p>
  }
  return {
    ...actual,
    TILE_ACTIONS: actual.discoverTileActions({
      './MarkCleanAction.tsx': {
        default: MarkCleanAction, actionLabel: 'Mark clean', actionPermission: 'adt.bed',
        actionStatuses: 'needs_cleaning',
      },
      './NoteAction.tsx': { default: NoteAction, actionNeedsOccupant: 1, actionOrder: 5 },
    }),
  }
})

const adtApi = await import('../../api/adt')
const boardApi = await import('../../api/adt/board')
const getMeta = vi.mocked(adtApi.getAdtMeta)
const getBoard = vi.mocked(boardApi.getBedBoard)
const getAwaiting = vi.mocked(boardApi.getAwaitingBed)
const link = vi.mocked(boardApi.linkAdmissionsToBeds)

beforeAll(async () => {
  await i18n.changeLanguage('en')
})

const holdsAll = () => true
const holdsNone = () => false

function tile(overrides: Partial<BedTile>): BedTile {
  return {
    bed_id: 'BED-X', unit_code: 'GM-1/X', bed_label: 'X', ward_id: 'WRD-1', room_id: null,
    room: null, room_name: null, bed_class: null, isolation: false, temporary: false,
    status: 'available', status_words: 'Available', tone: 'ready', markers: [], free: true,
    housekeeping_state: 'vacant_ready', since: null, minutes_in_state: null, occupant: null,
    occupant_hidden: false, ...overrides,
  }
}

const BOARD: BedBoard = {
  generated_at: '2026-10-04T10:00:00.000000+00:00',
  department: 'General Medicine',
  ward_id: null,
  ward_choices: [
    { id: 'WRD-1', code: 'GM-1', name: 'General ward one', department: 'General Medicine', is_active: true },
    { id: 'WRD-2', code: 'GM-2', name: null, department: 'General Medicine', is_active: true },
  ],
  status_words: { occupied: 'Occupied', boarder: 'Boarder', needs_cleaning: 'Needs cleaning' },
  wards: [{
    id: 'WRD-1', code: 'GM-1', name: 'General ward one', department: 'General Medicine',
    ward_type: 'general', is_active: true, cleaning_tracked: true, register_complete: false,
    counts: { beds: 4, free: 0, occupied_total: 2, occupied: 1, boarder: 1, needs_cleaning: 1,
              held_for_theatre: 1 },
    tiles: [
      tile({
        bed_id: 'B1', bed_label: '1', status: 'occupied', tone: 'occupied', free: false,
        status_words: 'Occupied, bed reported out of service',
        markers: ['Bed reported out of service', 'Not linked to bed setup'],
        occupant: { admission_id: 'A1', patient_id: 'P1', name: 'Asha Rao', mrn: 'MRN-001',
                    department: 'General Medicine', boarder: false, boarder_from: null,
                    presence_status: null, linked: false },
        isolation: true, room: 'BAY-A', room_name: 'Bay A',
      }),
      tile({ bed_id: 'B2', bed_label: '2', status: 'boarder', status_words: 'Boarder', tone: 'occupied',
             free: false, occupant_hidden: true, room: 'BAY-A', room_name: 'Bay A' }),
      tile({ bed_id: 'B3', bed_label: '3', status: 'needs_cleaning', status_words: 'Needs cleaning',
             tone: 'attention', free: false, minutes_in_state: 135,
             since: '2026-10-04T07:45:00.000000+00:00' }),
      tile({ bed_id: 'B4', bed_label: '4', status: 'held_for_theatre', status_words: 'Held for theatre',
             tone: 'sparkly', free: false }),
    ],
  }],
}

function viewProps(overrides: Partial<AdtViewProps> = {}): AdtViewProps {
  return { meta: { module: 'adt', state: 'on' }, openView: vi.fn(), onModuleOff: vi.fn(),
           holds: holdsAll, ...overrides }
}

beforeEach(() => {
  getMeta.mockReset()
  getBoard.mockReset()
  getAwaiting.mockReset()
  link.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

// ═════════════════════════════════════════════════════════════════════════════
//  The panel
// ═════════════════════════════════════════════════════════════════════════════

function StubView({ onModuleOff }: AdtViewProps) {
  return <button type="button" onClick={onModuleOff}>Report the module switched off</button>
}

const STUB: AdtViewEntry[] = discoverViews({ './adt/StubView.tsx': { default: StubView } })

describe('BedBoardPanel', () => {
  it('says it is loading while the module probe is in flight', () => {
    getMeta.mockReturnValue(new Promise(() => {}))
    render(<BedBoardPanel views={STUB} holds={holdsAll} />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading the bed board')
  })

  it('shows the switched-off notice, never an empty board, on module_disabled', async () => {
    getMeta.mockRejectedValue(new AdtApiError('Beds is switched off.', 404, 'module_disabled',
      { error: 'module_disabled', module: 'adt' }))
    render(<BedBoardPanel views={STUB} holds={holdsAll} />)
    expect(await screen.findByText('Beds is switched off on this site.')).toBeInTheDocument()
    expect(screen.queryByText('Report the module switched off')).not.toBeInTheDocument()
  })

  it('a view that meets module_disabled later turns the whole panel off', async () => {
    getMeta.mockResolvedValue({ module: 'adt', state: 'on' })
    render(<BedBoardPanel views={STUB} holds={holdsAll} />)
    fireEvent.click(await screen.findByText('Report the module switched off'))
    expect(await screen.findByText('Beds is switched off on this site.')).toBeInTheDocument()
  })

  it('offers a retry when the server cannot be reached', async () => {
    getMeta.mockRejectedValueOnce(new AdtApiError('Could not reach the bed board.', 0, 'network_error'))
    getMeta.mockResolvedValueOnce({ module: 'adt', state: 'on' })
    render(<BedBoardPanel views={STUB} holds={holdsAll} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the bed board')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Report the module switched off')).toBeInTheDocument()
  })

  it('the real build finds the Board and Awaiting a bed views, in that order', async () => {
    getMeta.mockResolvedValue({ module: 'adt', state: 'on' })
    getBoard.mockResolvedValue(BOARD)
    // A bed manager: adt.read, and not vitals.transcribe, which the ward
    // clerk's Observations view (Ward Record, 6 October 2026) needs.
    render(<BedBoardPanel holds={(p: string) => p === 'adt.read'} />)
    const nav = await screen.findByRole('navigation', { name: 'Beds screens' })
    expect(within(nav).getAllByRole('button').map((b) => b.textContent))
      .toEqual(['Board', 'Awaiting a bed'])
    expect(await screen.findByText('Asha Rao')).toBeInTheDocument()
  })

  it('the real build orders every view it finds by viewOrder', async () => {
    getMeta.mockResolvedValue({ module: 'adt', state: 'on' })
    getBoard.mockResolvedValue(BOARD)
    render(<BedBoardPanel holds={holdsAll} />)
    const nav = await screen.findByRole('navigation', { name: 'Beds screens' })
    // Bed setup (adt-4b, W5-B) is order 90 and needs bed.master.
    expect(within(nav).getAllByRole('button').map((b) => b.textContent))
      .toEqual(['Board', 'Awaiting a bed', 'Observations', 'Bed setup'])
  })

  it('says so when no view is open to this role', async () => {
    getMeta.mockResolvedValue({ module: 'adt', state: 'on' })
    render(<BedBoardPanel holds={holdsNone} />)
    expect(await screen.findByText('Nothing here for your role')).toBeInTheDocument()
    expect(getBoard).not.toHaveBeenCalled()
  })

  it('installs the Beds tab, which only the server switch puts on a rail', () => {
    expect(hospitalModuleInstalled.adt).toBe(true)
    expect(getAccessibleTabs('admin', undefined, DEFAULT_MODULE_FLAGS, hospitalModuleInstalled))
      .not.toContain('adt')
    expect(getAccessibleTabs('admin', undefined, { ...DEFAULT_MODULE_FLAGS, adt: true },
      hospitalModuleInstalled)).toContain('adt')
    // The clerk's own Observations tab (9 October 2026) sits beside Beds.
    expect(getAccessibleTabs('ward_clerk', 'General Medicine', { ...DEFAULT_MODULE_FLAGS, adt: true },
      hospitalModuleInstalled)).toEqual(['adt', 'observations'])
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  The Board view
// ═════════════════════════════════════════════════════════════════════════════

function tileOf(label: string): HTMLElement {
  const all = Array.from(document.querySelectorAll<HTMLElement>('li.bb-tile'))
  const found = all.find((li) => li.querySelector('.bb-tile__label')?.textContent === label)
  if (!found) throw new Error(`no tile ${label}`)
  return found
}

describe('BoardView', () => {
  it('prints the server words and styles by tone, an unknown one as neutral', async () => {
    getBoard.mockResolvedValue(BOARD)
    render(<BoardView {...viewProps()} />)
    expect(await screen.findByText('Asha Rao')).toBeInTheDocument()
    const one = tileOf('1')
    expect(one).toHaveClass('bb-tile--occupied')
    expect(within(one).getByText('Occupied, bed reported out of service')).toBeInTheDocument()
    // A marker the status words already say is not said twice.
    expect(within(one).getAllByText(/bed reported out of service/i)).toHaveLength(1)
    expect(within(one).getByText('Not linked to bed setup')).toBeInTheDocument()
    expect(within(one).getByText('Isolation')).toBeInTheDocument()
    expect(within(one).getByText('MRN-001')).toBeInTheDocument()
    const four = tileOf('4')
    expect(four).toHaveClass('bb-tile--neutral')
    expect(within(four).getByText('Held for theatre')).toBeInTheDocument()
    expect(within(tileOf('3')).getByText('for 2 h 15 min')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Bay A' })).toBeInTheDocument()
    const counts = screen.getByRole('list', { name: 'Bed counts for GM-1' })
    expect(counts).toHaveTextContent('4 Beds')
    expect(counts).toHaveTextContent('1 Needs cleaning')
    expect(counts).toHaveTextContent('1 held for theatre')
  })

  it('names nobody the server did not name', async () => {
    getBoard.mockResolvedValue(BOARD)
    render(<BoardView {...viewProps()} />)
    await screen.findByText('Asha Rao')
    const two = tileOf('2')
    expect(two).toHaveTextContent('Boarder')
    expect(two.querySelector('.bb-tile__name')).toBeNull()
    expect(two.querySelector('.bb-tile__mrn')).toBeNull()
  })

  it('polls every 30 seconds and keeps the last board when a refresh fails', async () => {
    const ticks: { fn: () => void; ms: number }[] = []
    vi.spyOn(window, 'setInterval').mockImplementation(((fn: () => void, ms: number) => {
      ticks.push({ fn, ms })
      return ticks.length
    }) as unknown as typeof window.setInterval)
    getBoard.mockResolvedValueOnce(BOARD)
    getBoard.mockRejectedValueOnce(new AdtApiError('Could not reach the bed board.', 0, 'network_error'))
    render(<BoardView {...viewProps()} />)
    await screen.findByText('Asha Rao')
    const poll = ticks.find((t) => t.ms === 30_000)
    expect(poll).toBeDefined()
    await act(async () => { poll!.fn() })
    expect(getBoard).toHaveBeenCalledTimes(2)
    expect(await screen.findByText(/Could not refresh/)).toBeInTheDocument()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
  })

  it('asks for one ward when the picker changes', async () => {
    getBoard.mockResolvedValue(BOARD)
    render(<BoardView {...viewProps()} />)
    await screen.findByText('Asha Rao')
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'WRD-2' } })
    await waitFor(() => expect(getBoard).toHaveBeenLastCalledWith('WRD-2'))
  })

  it('turns the panel off on module_disabled', async () => {
    const onModuleOff = vi.fn()
    getBoard.mockRejectedValue(new AdtApiError('off', 404, 'module_disabled', { error: 'module_disabled' }))
    render(<BoardView {...viewProps({ onModuleOff })} />)
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })

  it('offers only the actions the role, the status and the occupant allow', async () => {
    getBoard.mockResolvedValue(BOARD)
    const { unmount } = render(<BoardView {...viewProps({ holds: (p) => p === 'adt.bed' })} />)
    await screen.findByText('Asha Rao')
    expect(within(tileOf('3')).getByRole('button', { name: 'Mark clean' })).toBeInTheDocument()
    expect(within(tileOf('1')).queryByRole('button', { name: 'Mark clean' })).toBeNull()
    // Needs an occupant the caller may see: on bed 1, never on the hidden boarder.
    expect(within(tileOf('1')).getByRole('button', { name: 'Note' })).toBeInTheDocument()
    expect(within(tileOf('2')).queryByRole('button', { name: 'Note' })).toBeNull()
    fireEvent.click(within(tileOf('3')).getByRole('button', { name: 'Mark clean' }))
    expect(screen.getByText('Cleaning bed 3')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }))
    await waitFor(() => expect(getBoard).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Cleaning bed 3')).toBeNull()
    unmount()

    render(<BoardView {...viewProps({ holds: holdsNone })} />)
    await screen.findAllByText('Asha Rao')
    expect(screen.queryByRole('button', { name: 'Mark clean' })).toBeNull()
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  The Awaiting a bed view
// ═════════════════════════════════════════════════════════════════════════════

const AWAITING = {
  ward_id: null,
  awaiting: [{
    admission_id: 'A10', patient_id: 'P10', name: 'Waiting Person', mrn: 'MRN-010',
    department: 'General Medicine', ward: 'GM-1', bed: null, ward_id: null, bed_id: null,
    master_ward_id: 'WRD-1', master_ward_code: 'GM-1', master_bed_id: null, master_bed_label: null,
    master_unit_code: null, reason: 'awaiting_bed', reason_words: 'No bed recorded yet',
    since: '2026-10-04 08:00:00',
  }],
  unlinked: [
    {
      admission_id: 'A11', patient_id: 'P11', name: 'Typed Person', mrn: 'MRN-011',
      department: 'General Medicine', ward: 'gm-1', bed: '3', ward_id: null, bed_id: null,
      master_ward_id: 'WRD-1', master_ward_code: 'GM-1', master_bed_id: 'B3', master_bed_label: '3',
      master_unit_code: 'GM-1/3', reason: 'matches_bed',
      reason_words: 'Matches a bed in setup; a bed manager can link it', since: null,
    },
    {
      admission_id: 'A12', patient_id: 'P12', name: 'Unknown Bed', mrn: 'MRN-012',
      department: 'General Medicine', ward: 'GM-1', bed: '99', ward_id: null, bed_id: null,
      master_ward_id: 'WRD-1', master_ward_code: 'GM-1', master_bed_id: null, master_bed_label: null,
      master_unit_code: null, reason: 'bed_not_in_setup',
      reason_words: 'The typed bed is not in bed setup', since: null,
    },
  ],
  counts: { awaiting: 1, unlinked: 2 },
}

describe('AwaitingView', () => {
  it('keeps awaiting and unlinked apart', async () => {
    getAwaiting.mockResolvedValue(AWAITING)
    render(<AwaitingView {...viewProps({ holds: holdsNone })} />)
    const awaiting = await screen.findByRole('region', { name: /Awaiting a bed/ })
    const unlinked = screen.getByRole('region', { name: /not linked to bed setup/ })
    expect(within(awaiting).getByText('Waiting Person')).toBeInTheDocument()
    expect(within(awaiting).queryByText('Typed Person')).toBeNull()
    expect(within(unlinked).getByText('Typed Person')).toBeInTheDocument()
    expect(within(unlinked).getByText('Unknown Bed')).toBeInTheDocument()
    expect(within(unlinked).queryByText('Waiting Person')).toBeNull()
    expect(screen.queryByRole('button', { name: /Link to bed/ })).toBeNull()
  })

  it('lets only a bed manager link, and only a bed that matches', async () => {
    getAwaiting.mockResolvedValue(AWAITING)
    render(<AwaitingView {...viewProps({ holds: (p) => p === 'bed.master' })} />)
    const buttons = await screen.findAllByRole('button', { name: /Link to bed/ })
    expect(buttons.map((b) => b.textContent)).toEqual(['Link to bed GM-1/3'])
  })

  it('makes a boarder link only after the words are confirmed', async () => {
    getAwaiting.mockResolvedValue(AWAITING)
    link.mockResolvedValueOnce({
      linked: [], count: 0,
      skipped: [{ admission_id: 'A11', code: 'boarder_acknowledgement_required',
                  message: 'Nurses on GM-1 (Cardiology) will not be able to open this chart.' }],
    })
    link.mockResolvedValueOnce({
      linked: [{ admission_id: 'A11', bed_id: 'B3', ward_id: 'WRD-1', ward: 'GM-1', bed: '3',
                 movement_id: 'MOV-1', boarder: true }],
      skipped: [], count: 1,
    })
    render(<AwaitingView {...viewProps({ holds: (p) => p === 'bed.master' })} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Link to bed GM-1/3' }))
    expect(await screen.findByText(/will not be able to open this chart/)).toBeInTheDocument()
    expect(link).toHaveBeenLastCalledWith({ admission_ids: ['A11'] })
    fireEvent.click(screen.getByRole('button', { name: 'I have read this. Link as a boarder' }))
    expect(await screen.findByText('Linked to GM-1, bed 3.')).toBeInTheDocument()
    expect(link).toHaveBeenLastCalledWith({ admission_ids: ['A11'], boarder_acknowledged: true })
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  The registries
// ═════════════════════════════════════════════════════════════════════════════

describe('the view and action registries', () => {
  it('derives ids and labels from file names', () => {
    expect(viewIdFromPath('./adt/BedSetupView.tsx')).toBe('bed-setup')
    expect(labelFromId('bed-setup')).toBe('Bed setup')
    expect(actionIdFromPath('./ChangeBedAction.tsx')).toBe('change-bed')
  })

  it('leaves out a file with no component or a repeated id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const entries = discoverTileActions({
      './AAction.tsx': { default: () => null, actionId: 'same' },
      './BAction.tsx': { default: () => null, actionId: 'same' },
      './CAction.tsx': { actionId: 'none' },
    })
    expect(entries.map((e) => e.id)).toEqual(['same'])
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('filters actions by status list, permission and occupant', () => {
    const entries = discoverTileActions({
      './AnyAction.tsx': { default: () => null, actionStatuses: '*' },
      './FreeAction.tsx': { default: () => null, actionStatuses: 'available, vacant_untracked' },
      './GatedAction.tsx': { default: () => null, actionPermission: 'bed.master' },
    })
    const free = tile({ status: 'vacant_untracked' })
    expect(actionsForTile(entries, free, holdsNone).map((e) => e.id)).toEqual(['any', 'free'])
    expect(actionsForTile(entries, free, holdsAll).map((e) => e.id)).toEqual(['any', 'free', 'gated'])
    expect(actionsForTile(entries, tile({ status: 'occupied' }), holdsAll).map((e) => e.id))
      .toEqual(['any', 'gated'])
  })
})

describe('words', () => {
  it('no rendered sentence carries an em dash', async () => {
    getBoard.mockResolvedValue(BOARD)
    getAwaiting.mockResolvedValue(AWAITING)
    const { container } = render(
      <>
        <BoardView {...viewProps()} />
        <AwaitingView {...viewProps()} />
      </>,
    )
    await screen.findByText('Asha Rao')
    await screen.findByText('Waiting Person')
    expect(container.textContent).not.toContain('\u2014')
  })
})
