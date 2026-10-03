/**
 * Admit modal: the client-side mirror of the server's registration name rule
 * (server/services/patient_name_rule.py). A name with fewer than two letters
 * keeps the Identity step invalid and says why, in the server's own words;
 * two letters, in any script, is enough. The server remains the rule (it
 * answers 400); this only stops the clinician from finding out after the
 * round trip.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({
  admitPatient: vi.fn(),
  // The admission option lists (GET /api/admissions/options); never resolved,
  // since the name rule lives on the Identity step.
  apiFetch: vi.fn(() => new Promise(() => {})),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

const RULE = 'Patient name must contain at least two letters.'

function identityIndicator() {
  const tab = screen.getByRole('button', { name: 'Identity' })
  return tab.querySelector('.admit-modal__tab-indicator') as HTMLElement
}

function setup() {
  const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
  const name = container.querySelector('input.admit-input[type="text"]') as HTMLInputElement
  const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
  const sex = container.querySelector('select.admit-input') as HTMLSelectElement
  // The name field is the one that takes focus on open, so this is it.
  expect(document.activeElement).toBe(name)
  fireEvent.change(age, { target: { value: '40' } })
  fireEvent.change(sex, { target: { value: 'F' } })
  return { name }
}

describe('AdmitPatientModal name rule mirror', () => {
  it('keeps Identity invalid and explains why for a one-letter name', () => {
    const { name } = setup()
    fireEvent.change(name, { target: { value: 'S' } })
    expect(screen.getByText(RULE)).toBeInTheDocument()
    expect(identityIndicator().className).not.toContain('admit-modal__tab-indicator--complete')

    fireEvent.change(name, { target: { value: 'S.' } })
    expect(screen.getByText(RULE)).toBeInTheDocument()
    expect(identityIndicator().className).not.toContain('admit-modal__tab-indicator--complete')
  })

  it.each(['Om', 'S. Kumar', 'Meenakshi Varadarajan', 'राम'])(
    'accepts a real name: %s', (value) => {
      const { name } = setup()
      fireEvent.change(name, { target: { value } })
      expect(screen.queryByText(RULE)).not.toBeInTheDocument()
      expect(identityIndicator().className).toContain('admit-modal__tab-indicator--complete')
    },
  )

  it('says nothing while the field is still empty', () => {
    setup()
    expect(screen.queryByText(RULE)).not.toBeInTheDocument()
    expect(identityIndicator().className).not.toContain('admit-modal__tab-indicator--complete')
  })
})
