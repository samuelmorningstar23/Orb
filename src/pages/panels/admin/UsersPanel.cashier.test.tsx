/**
 * A cashier account had no badge colour of its own and took the patient
 * badge, the fallback, so a counter account read like a portal login in the
 * user list.
 */
import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import UsersPanel from './UsersPanel'

const CASHIER = {
  id: 'CSH001', name: 'Chitra Cashier', role: 'cashier', department: null,
  is_active: true, totp_enabled: false, is_self: false,
}

describe('UsersPanel, cashier accounts', () => {
  beforeEach(() => {
    apiFetch.mockReset()
    apiFetch.mockResolvedValue({ users: [CASHIER], departments: [] })
  })

  it('labels a cashier as Cashier in its own colour, not the patient fallback', async () => {
    render(<UsersPanel />)
    const row = (await screen.findByText('Chitra Cashier')).closest('tr') as HTMLElement
    const badge = within(row).getByText('Cashier')
    expect(badge.style.color).toBe('var(--status-info-text)')
    expect(badge.style.color).not.toBe('var(--text-muted)')
  })
})
