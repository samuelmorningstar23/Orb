/**
 * A DOSE THAT WAS NEVER CHARTED MUST NEVER LOOK CHARTED.
 *
 * api/client.administerMedication routes through offlineMutate: with no
 * connection it QUEUES the request and hands back an optimistic
 * `status: 'queued_offline'` record, which the round sheet renders as "Given".
 * On a bedside drug round that is the worst available outcome — the nurse sees
 * her own confirmation, moves on, and the eMAR entry depends on a replay that
 * may never succeed (the order goes inactive, the tablet is reset, the queue
 * fails permanently and drops the entry).
 *
 * So the modal refuses BEFORE the queue is reachable.
 */
import { render, screen, act } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

const administerMedication = vi.fn()
/* `getUser` is mocked to NOBODY SIGNED IN, which is the honest reading for these
   cases: an unknown role holds no permissions (src/lib/roles.ts), so the modal
   offers no allergy-override control and these assertions are about the screen
   as a nurse sees it. The prescriber's half is
   AdministerModal.override.test.tsx, which signs a user in for real. */
vi.mock('../../api/client', () => ({
  administerMedication: (...a: unknown[]) => administerMedication(...a),
  getUser: () => null,
}))

import AdministerModal from './AdministerModal'

const MED = { id: 42, drug_name: 'Enoxaparin', dosage: '40 mg', route: 'SC', frequency: 'OD' }

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
  act(() => { window.dispatchEvent(new Event(value ? 'online' : 'offline')) })
}

function open(onRecorded = vi.fn()) {
  render(
    // `[]` = a list was read and it was empty (NKDA), not "unknown". These
    // cases are about the offline write guard, not the allergy repeat.
    <AdministerModal med={MED} patientName="Anita Rao" patientBed="3" allergies={[]} onClose={() => {}} onRecorded={onRecorded} />,
  )
  const input = screen.getByRole('textbox')
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
    setter.call(input, 'Enoxaparin')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  return { onRecorded }
}

beforeEach(() => { administerMedication.mockReset(); setOnline(true) })
afterEach(() => { setOnline(true) })

describe('AdministerModal while offline', () => {
  it('does not reach the offline queue at all', async () => {
    setOnline(false)
    open()
    await act(async () => { screen.getByRole('button', { name: /^Given$/ }).click() })
    expect(administerMedication).not.toHaveBeenCalled()
  })

  it('tells the nurse the dose was NOT charted, with no promise of a later sync', async () => {
    setOnline(false)
    const { onRecorded } = open()
    await act(async () => { screen.getByRole('button', { name: /^Given$/ }).click() })

    const message = (await screen.findByText(/has not been saved/i)).textContent ?? ''
    expect(message).toMatch(/offline/i)
    expect(message).not.toMatch(/queue|sync|when you.?re back/i)
    // Nothing may render as a completed administration.
    expect(onRecorded).not.toHaveBeenCalled()
  })

  it('charts normally once the connection is back', async () => {
    setOnline(false)
    const { onRecorded } = open()
    const btn = screen.getByRole('button', { name: /^Given$/ })
    await act(async () => { btn.click() })
    expect(administerMedication).not.toHaveBeenCalled()

    administerMedication.mockResolvedValue({ status: 'ok', administration: { id: 1, status: 'given' } })
    setOnline(true)
    await act(async () => { btn.click() })
    expect(administerMedication).toHaveBeenCalledTimes(1)
    expect(onRecorded).toHaveBeenCalledTimes(1)
  })
})
