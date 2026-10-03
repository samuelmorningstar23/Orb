/**
 * PIN-on-device sign-in, from the shared ward tablet's point of view.
 *
 * The properties tested here are the ones that decide whether staff share a
 * login or not — which is to say, whether the audit trail means anything:
 *
 *   1. On an ENROLLED tablet the PIN pad is what you see first. If the fast path
 *      is still "type your whole password", nothing changes on the ward.
 *   2. On any other browser it is not offered at all, so a shoulder-surfed PIN
 *      has nowhere to be typed.
 *   3. The password form is ALWAYS one tap away on the tablet — that is what
 *      makes a PIN lockout survivable rather than a mid-shift lockout from the
 *      record.
 *   4. A PIN session is marked as such in the app's own session state, because
 *      that is what drives the two-minute auto-sign-out instead of the
 *      fifteen-minute lock a personal workstation gets.
 *   5. The raw device secret never lands in localStorage — it is exchanged once
 *      for an HttpOnly cookie.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getDeviceStatus = vi.fn()
const bindDevice = vi.fn()
const pinLogin = vi.fn()
const apiFetch = vi.fn()

vi.mock('../api/deviceAuth', () => ({
  getDeviceStatus: (...a: unknown[]) => getDeviceStatus(...a),
  bindDevice: (...a: unknown[]) => bindDevice(...a),
  pinLogin: (...a: unknown[]) => pinLogin(...a),
}))

vi.mock('../api/client', () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  setUser: vi.fn(),
  authenticateSocket: vi.fn(),
}))

vi.mock('../components/OrbLogo', () => ({ default: () => <span>orb</span> }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))

import Login from './Login'
import { AuthContext, type User } from '../App'

const PIN_USER = {
  token: 'jwt',
  user: { id: 'NUR001', name: 'Priya', role: 'nurse', department: 'General Medicine' },
}

function renderLogin(onLogin: (u: User) => void = () => {}) {
  return render(
    <AuthContext.Provider value={{ user: null, login: onLogin, logout: () => {} }}>
      <MemoryRouter initialEntries={['/login/clinician']}>
        <Routes>
          <Route path="/login/:mode" element={<Login />} />
          <Route path="/dashboard" element={<div>dashboard</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  getDeviceStatus.mockResolvedValue({ enrolled: false })
})

describe('PIN-on-device sign-in', () => {
  it('leads with the PIN pad on an enrolled tablet', async () => {
    getDeviceStatus.mockResolvedValue({
      enrolled: true, device_id: 'dev_1', label: 'Ward GM-1 trolley',
    })
    renderLogin()

    // The tablet names itself, so a nurse can see she is on the ward device.
    expect(await screen.findByText('Ward GM-1 trolley')).toBeInTheDocument()
    expect(screen.getByLabelText('PIN')).toBeInTheDocument()
    // ...and the password field is NOT the thing in front of her.
    expect(screen.queryByPlaceholderText('DOC001')).not.toBeInTheDocument()
  })

  it('does not offer a PIN on a browser that is not an enrolled tablet', async () => {
    getDeviceStatus.mockResolvedValue({ enrolled: false })
    renderLogin()
    await waitFor(() => expect(getDeviceStatus).toHaveBeenCalled())
    expect(screen.queryByLabelText('PIN')).not.toBeInTheDocument()
    // A stolen PIN has nowhere to go here — only the password form exists.
    expect(screen.getByPlaceholderText('DOC001')).toBeInTheDocument()
  })

  it('keeps the password fallback one tap away, so a PIN lock is not a lockout', async () => {
    getDeviceStatus.mockResolvedValue({ enrolled: true, device_id: 'dev_1', label: 'Ward GM-1' })
    renderLogin()

    await screen.findByLabelText('PIN')
    fireEvent.click(screen.getByRole('button', { name: /use my password instead/i }))
    expect(screen.getByPlaceholderText('DOC001')).toBeInTheDocument()
    expect(screen.queryByLabelText('PIN')).not.toBeInTheDocument()
  })

  it('marks the resulting session as a shared-device session', async () => {
    getDeviceStatus.mockResolvedValue({ enrolled: true, device_id: 'dev_1', label: 'Ward GM-1' })
    pinLogin.mockResolvedValue(PIN_USER)
    const captured: User[] = []
    renderLogin(u => captured.push(u))

    const pinField = await screen.findByLabelText('PIN')
    fireEvent.change(screen.getByPlaceholderText('NUR001'), { target: { value: 'NUR001' } })
    fireEvent.change(pinField, { target: { value: '4417' } })
    await act(async () => {
      // By id, not label: i18n keys are unresolved under the test runner.
      fireEvent.click(document.getElementById('btn-login')!)
    })

    await waitFor(() => expect(captured).toHaveLength(1))
    expect(pinLogin).toHaveBeenCalledWith('NUR001', '4417')
    // The session names the PERSON...
    expect(captured[0].id).toBe('NUR001')
    // ...and is flagged as a tablet session, which is what shortens the
    // auto-sign-out from fifteen minutes to two.
    expect(captured[0].authMethod).toBe('pin')
  })

  it('leaves a way out of tablet setup when the device check resolves late', async () => {
    // The status fetch is async: a tap on "set up this tablet" can land before it
    // answers, and then answer "already enrolled". Gating the way back on
    // `!enrolled` alone stranded the user in the setup form with only a page
    // reload to escape.
    let resolveStatus: (s: unknown) => void = () => {}
    getDeviceStatus.mockReturnValue(new Promise(r => { resolveStatus = r }))
    renderLogin()

    fireEvent.click(screen.getByRole('button', { name: /set up this tablet for PIN sign-in/i }))
    expect(await screen.findByLabelText(/tablet setup code/i)).toBeInTheDocument()

    await act(async () => {
      resolveStatus({ enrolled: true, device_id: 'dev_1', label: 'Ward GM-1' })
    })

    // Still a way back to sign-in — and taking it lands on the PIN pad.
    const back = screen.getByRole('button', { name: /back to sign in/i })
    fireEvent.click(back)
    expect(await screen.findByLabelText('PIN')).toBeInTheDocument()
  })

  it('rejects non-digits in the PIN field before anything is sent', async () => {
    getDeviceStatus.mockResolvedValue({ enrolled: true, device_id: 'dev_1', label: 'Ward GM-1' })
    renderLogin()

    const pinField = await screen.findByLabelText('PIN')
    fireEvent.change(pinField, { target: { value: 'a1b2c3' } })
    expect((pinField as HTMLInputElement).value).toBe('123')
  })

  it('never stores the raw device secret in the browser', async () => {
    getDeviceStatus.mockResolvedValue({ enrolled: false })
    bindDevice.mockResolvedValue({ enrolled: true, device_id: 'dev_9', label: 'Ward GM-2' })
    const secret = 'S'.repeat(43)
    renderLogin()

    await waitFor(() => expect(getDeviceStatus).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /set up this tablet for PIN sign-in/i }))
    fireEvent.change(await screen.findByLabelText(/tablet setup code/i), {
      target: { value: secret },
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^set up this tablet$/i }))
    })

    await waitFor(() => expect(bindDevice).toHaveBeenCalledWith(secret))
    // The server exchanged it for an HttpOnly cookie; nothing readable is kept.
    const stored = Object.keys(localStorage).map(k => localStorage.getItem(k) || '').join('|')
    expect(stored).not.toContain(secret)
    // And the tablet flips straight into PIN mode.
    expect(await screen.findByLabelText('PIN')).toBeInTheDocument()
  })
})
