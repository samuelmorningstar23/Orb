/**
 * User Management and the HIS roles (migration 48).
 *
 * Each of the five new roles exists only while its feature is on, and the
 * server refuses to create one whose feature is off. So the role picker offers
 * a HIS role only while its feature is on, and with every HIS feature off (every
 * site today) it offers exactly the five roles it always did. Editing a user
 * who already holds a HIS role must never silently swap that role for the first
 * option in the list, whatever the switch says now.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import UsersPanel from './UsersPanel'
import {
  resetClinicalSwitches, resetFeatureFlags, resetModuleFlags, setClinicalSwitches, setFeatureFlags, setModuleFlags,
} from '../../../shellConfig'

const ADMIN = { id: 'ADM001', name: 'Asha Admin', role: 'admin', department: null, is_active: true, totp_enabled: true, is_self: true }
const CLERK = { id: 'WC001', name: 'Wanda Clerk', role: 'ward_clerk', department: 'Cardiology', is_active: true, totp_enabled: false, is_self: false }
const PATHOLOGIST = { id: 'PATH01', name: 'Priya Path', role: 'pathologist', department: null, is_active: true, totp_enabled: false, is_self: false }

function serve(users: object[]) {
  apiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (init?.method) return Promise.resolve({})
    if (path === '/admin/users') return Promise.resolve({ users, departments: ['Cardiology', 'General Medicine'] })
    return Promise.reject(new Error(`unexpected ${path}`))
  })
}

async function openCreate() {
  render(<UsersPanel />)
  await screen.findByText('Asha Admin')
  fireEvent.click(screen.getByRole('button', { name: /create user/i }))
  const dialog = screen.getByRole('dialog')
  const roleSelect = within(dialog).getByRole('combobox', { name: 'Role' }) as HTMLSelectElement
  return { dialog, roleSelect }
}

const optionLabels = (select: HTMLSelectElement) => Array.from(select.options).map(o => o.textContent)
const optionValues = (select: HTMLSelectElement) => Array.from(select.options).map(o => o.value)

beforeEach(() => {
  apiFetch.mockReset()
  resetModuleFlags()
  resetFeatureFlags()
  resetClinicalSwitches()
})

afterEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
  resetClinicalSwitches()
})

describe('the role picker offers a HIS role only while its feature is on', () => {
  it('with every HIS feature off, offers exactly the five roles it always did', async () => {
    serve([ADMIN])
    const { roleSelect } = await openCreate()
    expect(optionLabels(roleSelect)).toEqual(['Doctor', 'Nurse', 'Administrator', 'Pharmacist', 'Cashier'])
  })

  it('offers the ward clerk once Beds is on', async () => {
    serve([ADMIN])
    setModuleFlags({ adt: true })
    const { roleSelect } = await openCreate()
    expect(optionValues(roleSelect)).toContain('ward_clerk')
    expect(optionLabels(roleSelect)).toContain('Ward clerk')
    expect(optionValues(roleSelect)).not.toContain('receptionist')
    expect(optionValues(roleSelect)).not.toContain('pathologist')
  })

  it('offers the three lab roles once the Laboratory is on', async () => {
    serve([ADMIN])
    setModuleFlags({ lab: true })
    const { roleSelect } = await openCreate()
    expect(optionLabels(roleSelect)).toEqual(expect.arrayContaining(['Lab technician', 'Pathologist', 'Phlebotomist']))
    expect(optionValues(roleSelect)).not.toContain('ward_clerk')
  })

  it('offers the receptionist once registration is on', async () => {
    serve([ADMIN])
    setFeatureFlags({ registration: true })
    const { roleSelect } = await openCreate()
    expect(optionLabels(roleSelect)).toContain('Receptionist')
  })

  it('offers the ward clerk on an Orb Ward Record computer with Beds off, and no other HIS role', async () => {
    serve([ADMIN])
    setClinicalSwitches({ ward_record: true })
    const { roleSelect } = await openCreate()
    expect(optionValues(roleSelect)).toEqual(['doctor', 'nurse', 'admin', 'pharmacist', 'cashier', 'ward_clerk'])
  })

  it('follows the switch live, when the health answer lands after the panel opened', async () => {
    serve([ADMIN])
    const { roleSelect } = await openCreate()
    expect(optionValues(roleSelect)).not.toContain('ward_clerk')
    act(() => { setModuleFlags({ adt: true }) })
    expect(optionValues(roleSelect)).toContain('ward_clerk')
  })
})

describe('the ward clerk needs a department', () => {
  it('refuses a ward clerk with no department, then creates one with it', async () => {
    serve([ADMIN])
    setModuleFlags({ adt: true })
    const { dialog, roleSelect } = await openCreate()
    fireEvent.change(roleSelect, { target: { value: 'ward_clerk' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. DR-0142'), { target: { value: 'WC002' } })
    fireEvent.change(within(dialog).getByPlaceholderText('Dr. Jane Doe'), { target: { value: 'New Clerk' } })
    fireEvent.change(within(dialog).getByPlaceholderText('At least 8 characters'), { target: { value: 'password1' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('Department is required for a ward clerk')
    expect(apiFetch).not.toHaveBeenCalledWith('/admin/users', expect.objectContaining({ method: 'POST' }))

    fireEvent.change(within(dialog).getByRole('combobox', { name: /department/i }), { target: { value: 'Cardiology' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admin/users', expect.objectContaining({ method: 'POST' })))
    const body = JSON.parse((apiFetch.mock.calls.find(c => c[1]?.method === 'POST')![1] as RequestInit).body as string)
    expect(body).toMatchObject({ role: 'ward_clerk', department: 'Cardiology' })
  })

  it('a lab role needs no department', async () => {
    serve([ADMIN])
    setModuleFlags({ lab: true })
    const { dialog, roleSelect } = await openCreate()
    fireEvent.change(roleSelect, { target: { value: 'phlebotomist' } })
    fireEvent.change(within(dialog).getByPlaceholderText('e.g. DR-0142'), { target: { value: 'PHL01' } })
    fireEvent.change(within(dialog).getByPlaceholderText('Dr. Jane Doe'), { target: { value: 'New Phleb' } })
    fireEvent.change(within(dialog).getByPlaceholderText('At least 8 characters'), { target: { value: 'password1' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admin/users', expect.objectContaining({ method: 'POST' })))
    const body = JSON.parse((apiFetch.mock.calls.find(c => c[1]?.method === 'POST')![1] as RequestInit).body as string)
    expect(body).toMatchObject({ role: 'phlebotomist' })
    expect(body.department).toBeUndefined()
  })
})

describe('existing HIS accounts', () => {
  it('are labelled by their own role, in an existing colour, never the patient fallback', async () => {
    serve([ADMIN, CLERK, PATHOLOGIST])
    render(<UsersPanel />)
    const clerkRow = (await screen.findByText('Wanda Clerk')).closest('tr') as HTMLElement
    const clerkBadge = within(clerkRow).getByText('Ward clerk')
    expect(clerkBadge.style.color).toBe('var(--status-info-text)')
    const pathRow = screen.getByText('Priya Path').closest('tr') as HTMLElement
    const pathBadge = within(pathRow).getByText('Pathologist')
    expect(pathBadge.style.color).toBe('var(--status-ok-text)')
    expect(pathBadge.style.color).not.toBe('var(--text-muted)')
  })

  it('editing a ward clerk while Beds is off keeps their role selected, and offers no other HIS role', async () => {
    serve([ADMIN, CLERK])
    render(<UsersPanel />)
    const row = (await screen.findByText('Wanda Clerk')).closest('tr') as HTMLElement
    fireEvent.click(within(row).getByRole('button', { name: /change role/i }))
    const dialog = screen.getByRole('dialog')
    const roleSelect = within(dialog).getByRole('combobox', { name: 'Role' }) as HTMLSelectElement
    expect(roleSelect.value).toBe('ward_clerk')
    expect(optionValues(roleSelect)).toEqual(['doctor', 'nurse', 'admin', 'pharmacist', 'cashier', 'ward_clerk'])
  })
})
