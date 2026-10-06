/**
 * WHO Surgical Safety Checklist UI — invariants against the backend contract
 * (server/services/who_checklist.py):
 *
 *   1. all three phases render with their items, and every rendered state is
 *      the SERVER's state — attribution ("by", "at") comes from the returned
 *      payload, never from a local guess;
 *   2. attesting an item POSTs {item_id, checked} and renders the returned
 *      state (role/name capture is server-side from the session — the client
 *      must not send an actor);
 *   3. a failed write re-fetches and shows the server's truth, not the click;
 *   4. the UI mirrors the backend's gating exactly: the backend enforces NO
 *      phase ordering and NO post-completion lock, so no checkbox is ever
 *      disabled by phase order or by completion. If someone adds client-side
 *      gating without backend parity, these tests go red.
 */
import { render, screen, waitFor, act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getWhoChecklistTemplate = vi.fn()
const getWhoChecklist = vi.fn()
const setWhoChecklistItem = vi.fn()

vi.mock('../../api/client', () => ({
  getWhoChecklistTemplate: (...a: unknown[]) => getWhoChecklistTemplate(...a),
  getWhoChecklist: (...a: unknown[]) => getWhoChecklist(...a),
  setWhoChecklistItem: (...a: unknown[]) => setWhoChecklistItem(...a),
  getUser: () => ({ id: 'DOC004', name: 'Dr Meera Pillai', role: 'doctor' }),
}))

import WhoSurgicalChecklist from './WhoSurgicalChecklist'

// Structurally the real template (who_checklist.WHO_CHECKLIST), trimmed to two
// items per phase so fixtures stay readable. Item ids are the real ones.
const TEMPLATE = {
  version: 'who-ssc-2009',
  phases: [
    {
      id: 'sign_in', label: 'Sign In', when: 'Before induction of anaesthesia',
      items: [
        { id: 'si_identity', text: 'Patient has confirmed identity, site, procedure, and consent' },
        { id: 'si_pulse_oximeter', text: 'Pulse oximeter on patient and functioning' },
      ],
    },
    {
      id: 'time_out', label: 'Time Out', when: 'Before skin incision',
      items: [
        { id: 'to_team_intro', text: 'All team members have introduced themselves by name and role' },
        { id: 'to_antibiotic', text: 'Antibiotic prophylaxis given within the last 60 minutes / not indicated' },
      ],
    },
    {
      id: 'sign_out', label: 'Sign Out', when: 'Before patient leaves operating room',
      items: [
        { id: 'so_counts', text: 'Instrument, sponge and needle counts are correct' },
        { id: 'so_specimen', text: 'Specimen labelled (incl. patient name) / not applicable' },
      ],
    },
  ],
}

const TOTAL = 6

function state(checkedItems: Record<string, { by: string; at: string }>, completed = false) {
  return {
    version: 'who-ssc-2009',
    state: Object.fromEntries(
      Object.entries(checkedItems).map(([id, v]) => [id, { checked: true, ...v }])
    ),
    progress: { checked: Object.keys(checkedItems).length, total: TOTAL },
    completed,
  }
}

beforeEach(() => {
  getWhoChecklistTemplate.mockReset().mockResolvedValue(TEMPLATE)
  getWhoChecklist.mockReset()
  setWhoChecklistItem.mockReset()
})

describe('rendering the server contract', () => {
  it('shows all three phases, server progress, and server-side attribution', async () => {
    getWhoChecklist.mockResolvedValue(
      state({ si_identity: { by: 'NUR001', at: '2026-08-10T09:14:00+00:00' } })
    )
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    expect(await screen.findByText('Sign In')).toBeInTheDocument()
    expect(screen.getByText('Time Out')).toBeInTheDocument()
    expect(screen.getByText('Sign Out')).toBeInTheDocument()
    expect(screen.getByText('Before skin incision')).toBeInTheDocument()
    expect(screen.getByText(`1/${TOTAL}`)).toBeInTheDocument()
    // Attribution is the SERVER's actor (another user), not the viewer.
    expect(screen.getByText(/Attested by NUR001/)).toBeInTheDocument()
    // The viewer is told whose account the next tick will be recorded against.
    expect(screen.getByText(/Attesting as Dr Meera Pillai \(doctor\)/)).toBeInTheDocument()
    expect(
      screen.getByRole('checkbox', { name: /confirmed identity/i })
    ).toBeChecked()
    expect(
      screen.getByRole('checkbox', { name: /pulse oximeter/i })
    ).not.toBeChecked()
  })
})

describe('attesting an item', () => {
  it('POSTs {item_id, checked:true} with no actor and renders the returned state', async () => {
    getWhoChecklist.mockResolvedValue(state({}))
    setWhoChecklistItem.mockResolvedValue(
      state({ si_pulse_oximeter: { by: 'DOC004', at: '2026-08-10T09:20:00+00:00' } })
    )
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    const box = await screen.findByRole('checkbox', { name: /pulse oximeter/i })
    await act(async () => { box.click() })

    expect(setWhoChecklistItem).toHaveBeenCalledTimes(1)
    // Exactly (surgeryId, item_id, checked) — the actor comes from the session
    // server-side; a client-sent actor would be a spoofable safety record.
    expect(setWhoChecklistItem).toHaveBeenCalledWith('SURG1', 'si_pulse_oximeter', true)
    expect(await screen.findByText(`1/${TOTAL}`)).toBeInTheDocument()
    expect(screen.getByText(/Attested by DOC004 \(you\)/)).toBeInTheDocument()
  })

  it('un-attesting a checked item POSTs checked:false (the backend allows it)', async () => {
    getWhoChecklist.mockResolvedValue(
      state({ so_counts: { by: 'DOC004', at: '2026-08-10T11:02:00+00:00' } })
    )
    setWhoChecklistItem.mockResolvedValue(state({}))
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    const box = await screen.findByRole('checkbox', { name: /counts are correct/i })
    expect(box).toBeChecked()
    await act(async () => { box.click() })

    expect(setWhoChecklistItem).toHaveBeenCalledWith('SURG1', 'so_counts', false)
    expect(await screen.findByText(`0/${TOTAL}`)).toBeInTheDocument()
  })

  it('a failed write shows an error and re-fetches the server truth', async () => {
    getWhoChecklist.mockResolvedValue(state({}))
    setWhoChecklistItem.mockRejectedValue(new Error('Forbidden'))
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    const box = await screen.findByRole('checkbox', { name: /antibiotic/i })
    await act(async () => { box.click() })

    expect(await screen.findByRole('alert')).toHaveTextContent('Forbidden')
    // Initial load + post-failure reload: the row shows what the server holds.
    await waitFor(() => expect(getWhoChecklist).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('checkbox', { name: /antibiotic/i })).not.toBeChecked()
  })
})

describe('gating mirrors the backend exactly', () => {
  it('a Sign Out item is enabled while Sign In is incomplete — the backend enforces no phase order', async () => {
    getWhoChecklist.mockResolvedValue(state({}))
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    // who_checklist.set_item accepts any known item id at any time. A UI-only
    // phase gate would misrepresent the record's semantics AND still be
    // bypassable through the API — so the UI must not invent one.
    const signOutBox = await screen.findByRole('checkbox', { name: /specimen labelled/i })
    expect(signOutBox).toBeEnabled()
  })

  it('completion is shown as state, not enforced as a lock', async () => {
    const all: Record<string, { by: string; at: string }> = {}
    for (const phase of TEMPLATE.phases) {
      for (const item of phase.items) all[item.id] = { by: 'DOC004', at: '2026-08-10T12:00:00+00:00' }
    }
    getWhoChecklist.mockResolvedValue(state(all, true))
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)

    expect(await screen.findByRole('status')).toHaveTextContent(
      `Checklist complete, all ${TOTAL} items attested.`
    )
    // The backend still accepts checked:false after completion; a disabled
    // checkbox here would claim an immutability the record does not have.
    expect(screen.getByRole('checkbox', { name: /counts are correct/i })).toBeEnabled()
  })
})

/* 2026-10-06: the record printed "Attested by DOC004". The server now names
   the attester at read time, and the line reads like a signature. */
describe('attester names', () => {
  it('prints the attester by name with the time', async () => {
    getWhoChecklist.mockResolvedValue(state({
      si_identity: { by: 'NUR001', by_name: 'Dr Meera Sharma', at: '2026-08-10T09:12:00+00:00' } as { by: string; at: string },
    }))
    render(<WhoSurgicalChecklist surgeryId="SURG1" />)
    const time = new Date('2026-08-10T09:12:00+00:00')
      .toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
    expect(await screen.findByText(`Attested by Dr Meera Sharma, ${time}`)).toBeInTheDocument()
    expect(screen.queryByText(/NUR001/)).toBeNull()
  })
})
