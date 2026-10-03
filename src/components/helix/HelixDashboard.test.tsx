/**
 * An empty pharmacy queue beside a ward full of active medicines read as
 * "pharmacy has seen everything". The empty state says which orders arrive here.
 */
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import HelixDashboard from './HelixDashboard'

afterEach(cleanup)

describe('Helix work queue empty state', () => {
  it('names which medication orders reach the pharmacy queue and which do not', () => {
    render(<HelixDashboard orders={[]} onSelectOrder={() => {}} />)
    expect(screen.getByText('Queue clear, no pending orders.')).toBeTruthy()
    const note = screen.getByText(/Only medication requests a clinician approves from Sage/)
    expect(note.textContent).toMatch(/patient chart go to the ward medication chart/)
    expect(note.textContent).toMatch(/Orders or Order sets are not sent to pharmacy/)
  })
})
