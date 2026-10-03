/**
 * The portal names no nurse nobody assigned.
 *
 * /api/bridge/care-team used to name the first nurse in the department, else
 * the first nurse in the hospital, and this card printed her as the patient's
 * nurse. Orb records no nurse-to-patient assignment, so the server now sends
 * the ward's nursing team, and the card must say that rather than read it as a
 * person.
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getCareTeam = vi.fn()
vi.mock('../../api/bridgeClient', () => ({
  getCareTeam: () => getCareTeam(),
}))

const TEAM = {
  care_team: [
    { name: 'Dr Meera Raman', role: 'Attending Physician', department: 'General Medicine' },
    { name: 'Nursing team', role: 'Nurse', department: 'General Medicine', ward: 'GM-1', is_team: true },
  ],
  unit: { department: 'General Medicine', ward: 'GM-1', bed: '4' },
}

import { CareTeamCard } from './CareTeamCard'

beforeEach(() => { getCareTeam.mockReset(); getCareTeam.mockResolvedValue(TEAM) })

describe('CareTeamCard', () => {
  it('keeps each part of the care unit on one line, so a bed id never splits', async () => {
    getCareTeam.mockResolvedValue({ ...TEAM, unit: { department: 'Surgery', ward: 'SW-1', bed: 'SW-102' } })
    render(<CareTeamCard />)
    const bed = await screen.findByText('Bed SW-102')
    expect(bed).toHaveStyle({ whiteSpace: 'nowrap' })
    expect(screen.getByText('Ward SW-1')).toHaveStyle({ whiteSpace: 'nowrap' })
    expect(bed.parentElement?.parentElement?.textContent).toBe('Care unit: Surgery · Ward SW-1 · Bed SW-102')
  })

  it('leaves out a missing department rather than printing a dash', async () => {
    getCareTeam.mockResolvedValue({ ...TEAM, unit: { department: null, ward: 'GM-1', bed: '4' } })
    render(<CareTeamCard />)
    const bed = await screen.findByText('Bed 4')
    expect(bed.parentElement?.parentElement?.textContent).toBe('Care unit: Ward GM-1 · Bed 4')
  })

  it('shows the ward nursing team, not a named nurse', async () => {
    render(<CareTeamCard />)
    expect(await screen.findByText('Nursing team')).toBeInTheDocument()
    expect(screen.getByText('Ward GM-1 · General Medicine')).toBeInTheDocument()
    expect(screen.getByText('Attending Physician · General Medicine')).toBeInTheDocument()
  })
})
