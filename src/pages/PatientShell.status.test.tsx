/**
 * The portal's "Your Status" card states the level in words, with no colour.
 *
 * NO VERDICT COLOUR (ruled 2026-10-03, the same day the vitals card lost its
 * colours). The badge and the card icon were painted green / amber / red from
 * the level, so a patient read their own stable / elevated / critical in alarm
 * colours with nobody beside them. The words stay; the colour goes.
 *
 * And no stand-in: the level used to default to 'stable', so while the status
 * was loading, or when the request failed, the patient was shown a "Stable"
 * badge that no score had produced.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

const mockGetBridgeStatus = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: (...args: unknown[]) => mockGetBridgeStatus(...args),
  getBridgeReports: () => Promise.resolve({ reports: [] }),
  askBridge: vi.fn(),
  apiFetch: () => Promise.resolve({ appointments: [] }),
}))
vi.mock('../components/bridge/CareTeamCard', () => ({ CareTeamCard: () => null }))
vi.mock('../components/bridge/VitalsTrendsCard', () => ({ VitalsTrendsCard: () => null }))

import PatientShell from './PatientShell'

const LEVEL_COLOUR = /bridge__card-(badge|icon)--(ok|warn|danger)/

// Braces matter: a function RETURNED from beforeEach is run as teardown, and
// mockReset() returns the mock itself.
beforeEach(() => { mockGetBridgeStatus.mockReset() })

describe('Your Status card', () => {
  it.each([
    ['stable', 'Stable'],
    ['elevated', 'Elevated'],
    ['critical', 'Critical'],
  ])('names the %s level without colouring it', async (level, word) => {
    mockGetBridgeStatus.mockResolvedValue({
      patient_name: 'Raj Patel', status: level, status_message: 'Your care team is monitoring you.',
    })
    const { container } = render(<PatientShell />)
    await waitFor(() => expect(screen.getByText(word)).toBeInTheDocument())
    expect(container.innerHTML).not.toMatch(LEVEL_COLOUR)
  })

  it('shows no level at all when the status could not be loaded', async () => {
    mockGetBridgeStatus.mockRejectedValue(new Error('offline'))
    render(<PatientShell />)
    await waitFor(() => expect(mockGetBridgeStatus).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText('Your care team is monitoring you.')).toBeInTheDocument())
    expect(screen.queryByText('Stable')).toBeNull()
  })
})
