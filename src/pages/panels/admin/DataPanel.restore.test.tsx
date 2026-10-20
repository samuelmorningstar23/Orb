/**
 * WHAT A RESTORE ROLLS BACK (multisite-15, ruling MS-R12 (a)).
 *
 * One appliance can hold several facilities of a hospital group in one
 * database, and a backup is that whole database, so restoring one puts every
 * facility back together. Data and Backups says so once more than one
 * facility is on record or ORB_MULTI_SITE is on, in the server's words when
 * it has them; a single-site install with the switch off sees the screen as
 * it was.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))
vi.mock('../../../hooks/useFocusTrap', () => ({ useFocusTrap: () => ({ current: null }) }))

import DataPanel from './DataPanel'
import { resetPlatformFlags, setPlatformFlags } from '../../../shellConfig'

const SERVER_STATEMENT =
  'This appliance holds 2 facilities in one database, and a backup holds all of them. '
  + 'Restoring one puts every facility back to the moment it was taken, not just one: '
  + 'anything recorded at any facility after that moment is lost and must be entered '
  + 'again. One facility cannot be restored on its own.'

function serve(backups: unknown) {
  apiFetch.mockImplementation((path: string) => {
    if (path === '/admin/backups') return backups instanceof Error ? Promise.reject(backups) : Promise.resolve(backups)
    if (path === '/admin/compliance/erasure') return Promise.resolve({ requests: [] })
    return Promise.reject(new Error(`unexpected ${path}`))
  })
}

beforeEach(() => {
  apiFetch.mockReset()
  resetPlatformFlags()
})

afterEach(() => resetPlatformFlags())

describe('restore statement on Data and Backups', () => {
  it('says nothing new on a single-site install with the switch off', async () => {
    serve({
      backups: [],
      restore_scope: {
        whole_appliance: true,
        facilities_on_record: 1,
        statement: "A backup holds this appliance's whole database.",
      },
    })
    render(<DataPanel />)
    await screen.findByText('No backups yet')
    expect(screen.queryByRole('note', { name: 'What a restore rolls back' })).toBeNull()
  })

  it("shows the server's statement once two facilities are on record", async () => {
    serve({
      backups: [],
      restore_scope: {
        whole_appliance: true,
        facilities_on_record: 2,
        statement: SERVER_STATEMENT,
        facilities: [
          { id: 1, code: 'MAIN', name: 'Quenthorpe Group Hospital' },
          { id: 9511, code: 'SAT1', name: 'Velmira Satellite Hospital' },
        ],
      },
    })
    render(<DataPanel />)
    const note = await screen.findByRole('note', { name: 'What a restore rolls back' })
    expect(note.textContent).toContain('A restore rolls back every facility.')
    expect(note.textContent).toContain('This appliance holds 2 facilities in one database')
    expect(note.textContent).not.toContain(String.fromCharCode(0x2014))
  })

  it('still says it when the list is fenced off and only the platform flags know', async () => {
    setPlatformFlags({ multi_site: false, multi_facility_data: true })
    serve(new Error('This screen is not available yet when more than one facility is on record'))
    render(<DataPanel />)
    const note = await screen.findByRole('note', { name: 'What a restore rolls back' })
    expect(note.textContent).toContain('One facility cannot be restored on its own.')
    expect(note.textContent).toContain('A backup holds every facility on this appliance')
  })

  it('says it while ORB_MULTI_SITE is on, before a second facility exists', async () => {
    setPlatformFlags({ multi_site: true, multi_facility_data: false })
    serve({
      backups: [],
      restore_scope: {
        whole_appliance: true,
        facilities_on_record: 1,
        statement: "A backup holds this appliance's whole database.",
      },
    })
    render(<DataPanel />)
    await waitFor(() =>
      expect(screen.getByRole('note', { name: 'What a restore rolls back' }).textContent)
        .toContain('A backup holds every facility on this appliance'))
  })
})
