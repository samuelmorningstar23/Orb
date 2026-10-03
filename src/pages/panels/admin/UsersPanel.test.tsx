/**
 * User Management offered Reset password, Role and Deactivate on every row,
 * including the admin's own, and let a nurse be created with no department
 * (which reads every ward). A refused action reported itself as a toast
 * behind the still-open dialog.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import UsersPanel from './UsersPanel'

const USERS = [
  { id: 'ADM001', name: 'Asha Admin', role: 'admin', department: null, is_active: true, totp_enabled: true, is_self: true },
  { id: 'NUR009', name: 'Nina Nurse', role: 'nurse', department: 'Cardiology', is_active: true, totp_enabled: false, is_self: false },
]
const PATIENT = { id: 'P1A2B3', name: 'Pat Portal', role: 'patient', department: 'Ward 3', is_active: true, totp_enabled: false, is_self: false }

function routeGet(path: string) {
  if (path === '/admin/users') return Promise.resolve({ users: USERS, departments: ['Cardiology', 'General Medicine'] })
  if (path === '/admin/users?include_patients=true') {
    return Promise.resolve({ users: [...USERS, PATIENT], departments: ['Cardiology', 'General Medicine'] })
  }
  return Promise.reject(new Error(`unexpected ${path}`))
}

describe('UsersPanel', () => {
  beforeEach(() => {
    apiFetch.mockReset()
    apiFetch.mockImplementation((path: string, init?: RequestInit) =>
      init?.method ? Promise.resolve({}) : routeGet(path))
  })

  it('labels roles and hides Deactivate on the admin\'s own row', async () => {
    render(<UsersPanel />)
    const ownRow = (await screen.findByText('Asha Admin')).closest('tr') as HTMLElement
    const nurseRow = screen.getByText('Nina Nurse').closest('tr') as HTMLElement
    expect(within(ownRow).getByText('Administrator')).toBeInTheDocument()
    expect(within(ownRow).queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(within(nurseRow).getByText('Nurse')).toBeInTheDocument()
    expect(within(nurseRow).getByRole('button', { name: 'Deactivate' })).toBeInTheDocument()
  })

  it('lists patient portal accounts only on request, without a Role action', async () => {
    render(<UsersPanel />)
    await screen.findByText('Nina Nurse')
    expect(screen.queryByText('Pat Portal')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Show patient portal accounts'))
    const row = (await screen.findByText('Pat Portal')).closest('tr') as HTMLElement
    expect(within(row).getByText('Patient portal')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /change role/i })).not.toBeInTheDocument()
    expect(apiFetch).toHaveBeenCalledWith('/admin/users?include_patients=true')
  })

  it('shows a refused deactivation inside the dialog', async () => {
    apiFetch.mockImplementation((path: string, init?: RequestInit) =>
      init?.method ? Promise.reject(new Error('This is a system account used by Orb\'s automation.')) : routeGet(path))
    render(<UsersPanel />)
    const row = (await screen.findByText('Nina Nurse')).closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: 'Deactivate' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Deactivate' }))
    await waitFor(() =>
      expect(within(dialog).getByRole('alert')).toHaveTextContent('This is a system account'))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('will not create a nurse without a department', async () => {
    render(<UsersPanel />)
    await screen.findByText('Nina Nurse')
    fireEvent.click(screen.getByRole('button', { name: /create user/i }))
    const dialog = screen.getByRole('dialog')
    const roleSelect = within(dialog).getByRole('combobox', { name: 'Role' }) as HTMLSelectElement
    expect(Array.from(roleSelect.options).map((o) => o.textContent)).not.toContain('Patient portal')
    fireEvent.change(roleSelect, { target: { value: 'nurse' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. DR-0142'), { target: { value: 'NUR010' } })
    fireEvent.change(within(dialog).getByPlaceholderText('Dr. Jane Doe'), { target: { value: 'New Nurse' } })
    fireEvent.change(within(dialog).getByPlaceholderText('At least 8 characters'), { target: { value: 'password1' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Department is required for clinical roles')
    expect(apiFetch).not.toHaveBeenCalledWith('/admin/users', expect.objectContaining({ method: 'POST' }))

    fireEvent.change(within(dialog).getByRole('combobox', { name: /department/i }), { target: { value: 'Cardiology' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admin/users', expect.objectContaining({ method: 'POST' })))
    const body = JSON.parse((apiFetch.mock.calls.find((c) => c[1]?.method === 'POST')![1] as RequestInit).body as string)
    expect(body).toMatchObject({ role: 'nurse', department: 'Cardiology' })
  })
})
