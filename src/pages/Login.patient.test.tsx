/**
 * Patient sign-in prompts (F144): with both fields empty the screen asked only
 * for the password. The patient ID is asked for first, and nothing is sent to
 * the server until both are filled.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../api/deviceAuth', () => ({
  getDeviceStatus: vi.fn(async () => ({ enrolled: false })),
  bindDevice: vi.fn(),
  pinLogin: vi.fn(),
}))
vi.mock('../api/client', () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  setUser: vi.fn(),
  authenticateSocket: vi.fn(),
}))
vi.mock('../components/OrbLogo', () => ({ default: () => <span>orb</span> }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))

import Login from './Login'
import { AuthContext } from '../App'

function renderPatientLogin() {
  return render(
    <AuthContext.Provider value={{ user: null, login: () => {}, logout: () => {} }}>
      <MemoryRouter initialEntries={['/login/patient']}>
        <Routes>
          <Route path="/login/:mode" element={<Login />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

// By id, not label: i18n keys are unresolved under the test runner.
const submit = async () => { await act(async () => { fireEvent.click(document.getElementById('btn-login')!) }) }

beforeEach(() => { vi.clearAllMocks() })

describe('patient sign-in prompts', () => {
  it('asks for the patient ID first when both fields are empty', async () => {
    renderPatientLogin()
    await submit()
    expect(screen.getByText('Please enter your patient ID.')).toBeInTheDocument()
    expect(screen.queryByText('Please enter your password.')).toBeNull()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('asks for the password once the ID is filled', async () => {
    renderPatientLogin()
    fireEvent.change(document.getElementById('input-registration')!, { target: { value: 'P001' } })
    await submit()
    expect(screen.getByText('Please enter your password.')).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('signs in once both are filled', async () => {
    apiFetch.mockRejectedValue(new Error('Invalid credentials'))
    renderPatientLogin()
    fireEvent.change(document.getElementById('input-registration')!, { target: { value: 'P001' } })
    fireEvent.change(document.getElementById('input-dob')!, { target: { value: 'secret-pass' } })
    await submit()
    expect(apiFetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toMatchObject({ employee_id: 'P001', password: 'secret-pass' })
  })
})
