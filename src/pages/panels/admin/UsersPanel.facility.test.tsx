/**
 * User Management and facilities (multisite-6, W9-H).
 *
 * Samuel ruled on 3 October 2026 that one Orb can serve the facilities of a
 * hospital group, that the multi-site switch only lets a site grow, and that
 * group-wide access is a named grant held only by an administrator (MS-R6 (a)).
 * On every site today (one facility, switch off) User Management must look and
 * post exactly as before. With the switch on it shows where each account
 * works, offers a home facility to staff without a department only, narrows
 * the department list to the chosen facility, and lets a grant holder give
 * or remove access to all facilities.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()
vi.mock('../../../api/client', () => ({
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))

import UsersPanel from './UsersPanel'
import { resetPlatformFlags, setPlatformFlags } from '../../../shellConfig'

const MAIN = { id: 1, code: 'MAIN', name: 'City Hospital' }
const SAT = { id: 2, code: 'SAT1', name: 'Riverside Satellite' }

const ADMIN = { id: 'ADM001', name: 'Asha Admin', role: 'admin', department: null, is_active: true, totp_enabled: true, is_self: true }
const NURSE = { id: 'N100', name: 'Nina Nurse', role: 'nurse', department: 'Cardiology', is_active: true, totp_enabled: false, is_self: false }
const PHARM = { id: 'PH200', name: 'Pavan Pharm', role: 'pharmacist', department: null, is_active: true, totp_enabled: false, is_self: false }
const ADMIN2 = { id: 'ADM002', name: 'Bela Admin', role: 'admin', department: null, is_active: true, totp_enabled: true, is_self: false }

/** The list as the server sends it with one facility and the switch off. */
const PLAIN = { users: [ADMIN, NURSE, PHARM], departments: ['Cardiology', 'General Medicine'] }

/** The list as the server sends it with facility keys (switch on, or two facilities). */
function withFacilities(over: Record<string, unknown> = {}) {
  return {
    users: [
      { ...ADMIN, facility: MAIN, enterprise_access: true, enterprise_grant_id: 7 },
      { ...NURSE, facility: SAT, enterprise_access: false, enterprise_grant_id: null },
      { ...PHARM, facility: MAIN, enterprise_access: false, enterprise_grant_id: null },
      { ...ADMIN2, facility: SAT, enterprise_access: false, enterprise_grant_id: null },
    ],
    departments: ['Cardiology', 'General Medicine'],
    facilities: [MAIN, SAT],
    department_facilities: { 'General Medicine': 1, Cardiology: 2, Oncology: 2 },
    own_facility_id: 1,
    grants_manageable: true,
    ...over,
  }
}

function serve(list: object) {
  apiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (init?.method) return Promise.resolve({})
    if (path === '/admin/users') return Promise.resolve(list)
    return Promise.reject(new Error(`unexpected ${path}`))
  })
}

const postCall = (path: string) => apiFetch.mock.calls.find(c => c[0] === path && (c[1] as RequestInit | undefined)?.method === 'POST')
const postBody = (path: string) => JSON.parse(((postCall(path)![1]) as RequestInit).body as string)
const datalistValues = (id: string) =>
  Array.from(document.querySelectorAll(`#${id}-options option`)).map(o => (o as HTMLOptionElement).value)

async function openCreate() {
  render(<UsersPanel />)
  await screen.findByText('Asha Admin')
  fireEvent.click(screen.getByRole('button', { name: /create user/i }))
  return screen.getByRole('dialog')
}

function fillCreate(dialog: HTMLElement, { id, role, department }: { id: string, role: string, department?: string }) {
  fireEvent.change(within(dialog).getByRole('combobox', { name: 'Role' }), { target: { value: role } })
  fireEvent.change(within(dialog).getByPlaceholderText('e.g. DR-0142'), { target: { value: id } })
  fireEvent.change(within(dialog).getByPlaceholderText('Dr. Jane Doe'), { target: { value: 'New Person' } })
  fireEvent.change(within(dialog).getByPlaceholderText('At least 8 characters'), { target: { value: 'password1' } })
  if (department !== undefined) {
    fireEvent.change(within(dialog).getByRole('combobox', { name: /department/i }), { target: { value: department } })
  }
}

beforeEach(() => {
  apiFetch.mockReset()
  resetPlatformFlags()
})

afterEach(() => {
  resetPlatformFlags()
})

describe('one facility, multi-site switch off (every site today)', () => {
  it('shows no facility column, no facility picker and no facility buttons', async () => {
    serve(PLAIN)
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    expect(screen.queryByRole('columnheader', { name: 'Facility' })).toBeNull()
    expect(screen.queryByRole('button', { name: /change facility/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /all facilities/i })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /create user/i }))
    expect(within(screen.getByRole('dialog')).queryByRole('combobox', { name: 'Facility' })).toBeNull()
  })

  it('posts a new account exactly as before, with no facility', async () => {
    serve(PLAIN)
    const dialog = await openCreate()
    fillCreate(dialog, { id: 'PH300', role: 'pharmacist' })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users')).toBeTruthy())
    expect(Object.keys(postBody('/admin/users')).sort()).toEqual(['id', 'name', 'password', 'role'])
  })
})

describe('the multi-site switch on', () => {
  beforeEach(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })

  it('shows where each account works, and marks group-wide access', async () => {
    serve(withFacilities())
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    expect(screen.getByRole('columnheader', { name: 'Facility' })).toBeTruthy()
    const nurseRow = screen.getByText('Nina Nurse').closest('tr') as HTMLElement
    expect(within(nurseRow).getByText('Riverside Satellite')).toBeTruthy()
    const adminRow = screen.getByText('Asha Admin').closest('tr') as HTMLElement
    expect(within(adminRow).getByText('All facilities')).toBeTruthy()
    expect(within(nurseRow).queryByText('All facilities')).toBeNull()
  })

  it('offers a home facility to staff without a department and sends it', async () => {
    serve(withFacilities())
    const dialog = await openCreate()
    const facility = within(dialog).getByRole('combobox', { name: 'Facility' }) as HTMLSelectElement
    expect(facility.value).toBe('1')
    expect(Array.from(facility.options).map(o => o.textContent)).toEqual(['City Hospital', 'Riverside Satellite'])
    expect(within(dialog).getByText('Staff without a department work at this facility.')).toBeTruthy()
    fillCreate(dialog, { id: 'CA400', role: 'cashier' })
    fireEvent.change(facility, { target: { value: '2' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users')).toBeTruthy())
    expect(postBody('/admin/users')).toMatchObject({ id: 'CA400', role: 'cashier', facility_id: 2 })
  })

  it('never sends a home facility for staff with a department', async () => {
    serve(withFacilities())
    const dialog = await openCreate()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Facility' }), { target: { value: '2' } })
    fillCreate(dialog, { id: 'N500', role: 'nurse', department: 'Cardiology' })
    expect(within(dialog).getByText("Staff with a department work at that department's facility.")).toBeTruthy()
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users')).toBeTruthy())
    const body = postBody('/admin/users')
    expect(body).toMatchObject({ role: 'nurse', department: 'Cardiology' })
    expect(body.facility_id).toBeUndefined()
  })

  it('offers only the chosen facility\'s departments', async () => {
    serve(withFacilities())
    const dialog = await openCreate()
    expect(datalistValues('admin-create-dept')).toEqual(['General Medicine'])
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Facility' }), { target: { value: '2' } })
    expect(datalistValues('admin-create-dept')).toEqual(['Cardiology', 'Oncology'])
  })

  it('moves staff without a department, and offers no move to staff with one', async () => {
    serve(withFacilities())
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    const nurseRow = screen.getByText('Nina Nurse').closest('tr') as HTMLElement
    expect(within(nurseRow).queryByRole('button', { name: /change facility/i })).toBeNull()
    const pharmRow = screen.getByText('Pavan Pharm').closest('tr') as HTMLElement
    fireEvent.click(within(pharmRow).getByRole('button', { name: /change facility for pavan pharm/i }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Facility' }), { target: { value: '2' } })
    fireEvent.change(within(dialog).getByPlaceholderText('Joined the satellite pharmacy'), { target: { value: 'Covering the satellite' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users/PH200/facility')).toBeTruthy())
    expect(postBody('/admin/users/PH200/facility')).toEqual({ facility_id: 2, reason: 'Covering the satellite' })
  })

  it('lets a grant holder give and remove access to all facilities, for administrators only', async () => {
    serve(withFacilities())
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    const nurseRow = screen.getByText('Nina Nurse').closest('tr') as HTMLElement
    expect(within(nurseRow).queryByRole('button', { name: /all facilities/i })).toBeNull()

    const admin2Row = screen.getByText('Bela Admin').closest('tr') as HTMLElement
    fireEvent.click(within(admin2Row).getByRole('button', { name: /give access to all facilities/i }))
    let dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByPlaceholderText('Group IT lead'), { target: { value: 'ab' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    expect(within(dialog).getByRole('alert')).toHaveTextContent('at least 3 characters')
    fireEvent.change(within(dialog).getByPlaceholderText('Group IT lead'), { target: { value: 'Group IT lead' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users/enterprise-grants')).toBeTruthy())
    expect(postBody('/admin/users/enterprise-grants')).toEqual({ user_id: 'ADM002', reason: 'Group IT lead' })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const adminRow = screen.getByText('Asha Admin').closest('tr') as HTMLElement
    fireEvent.click(within(adminRow).getByRole('button', { name: /remove access to all facilities/i }))
    dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByPlaceholderText('Group IT lead'), { target: { value: 'Handed over' } })
    fireEvent.submit(dialog.querySelector('form') as HTMLFormElement)
    await waitFor(() => expect(postCall('/admin/users/enterprise-grants/7/revoke')).toBeTruthy())
    expect(postBody('/admin/users/enterprise-grants/7/revoke')).toEqual({ reason: 'Handed over' })
  })

  it('shows no grant control to an administrator without group-wide access', async () => {
    const list = withFacilities({ grants_manageable: false })
    ;(list.users as Array<Record<string, unknown>>).forEach(u => { delete u.enterprise_access; delete u.enterprise_grant_id })
    serve(list)
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    expect(screen.queryByRole('button', { name: /all facilities/i })).toBeNull()
    expect(screen.queryByText('All facilities')).toBeNull()
  })
})

describe('more than one facility on record, switch off', () => {
  beforeEach(() => { setPlatformFlags({ multi_site: false, multi_facility_data: true }) })

  it('labels facilities but offers no placing, no moves and no new grants', async () => {
    serve(withFacilities())
    render(<UsersPanel />)
    await screen.findByText('Asha Admin')
    expect(screen.getByRole('columnheader', { name: 'Facility' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /change facility/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /give access to all facilities/i })).toBeNull()
    // Removing access only narrows, so it stays available.
    expect(screen.getByRole('button', { name: /remove access to all facilities/i })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /create user/i }))
    expect(within(screen.getByRole('dialog')).queryByRole('combobox', { name: 'Facility' })).toBeNull()
  })
})
