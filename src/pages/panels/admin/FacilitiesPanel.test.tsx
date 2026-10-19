/**
 * Facilities (multisite-2): facility 1's identity card is always shown, an
 * edit sends only what changed and shows the server's field refusals beside
 * the fields, and the create check appears only with the multi-site switch on
 * (platform flags from the health probe, never a second fetch).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetPlatformFlags, setPlatformFlags } from '../../../shellConfig'

const api = vi.hoisted(() => ({
  listFacilities: vi.fn(),
  updateFacility: vi.fn(),
  getCreateCheck: vi.fn(),
}))

vi.mock('../../../api/facilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/facilities')>()
  return { ...actual, ...api }
})

import FacilitiesPanel from './FacilitiesPanel'
import type { Facility } from '../../../api/facilities'

function facility(overrides: Partial<Facility> = {}): Facility {
  return {
    id: 1, uuid: 'u-1', code: 'MAIN', is_active: true, is_main: true,
    display_name: 'Sunrise General', site_name: 'Sunrise General',
    name: null, short_name: null, legal_name: null,
    address_line1: null, address_line2: null, city: null, district: null, state: null,
    pincode: null, gstin: null, pan: null, state_code: null, phone: null, email: null,
    hfr_id: null, abdm_hip_id: null, nabh_accreditation_no: null, nabh_valid_until: null,
    clinical_establishment_reg_no: null, timezone: null,
    address: null, supplier_name: 'Sunrise General', state_code_name: null,
    invoice_ready: false, missing_for_invoice: ['Address', 'GSTIN'],
    can_edit: true, created_at: '2026-10-09 10:00:00', created_by: null,
    updated_at: null, updated_by: null, audit_epoch_id: null,
    ...overrides,
  }
}

const second = facility({
  id: 2, uuid: 'u-2', code: 'SAT1', is_main: false, display_name: 'Satellite Clinic',
  name: 'Satellite Clinic', site_name: null, can_edit: false,
})

beforeEach(() => {
  api.listFacilities.mockResolvedValue({
    facilities: [facility(), second], multi_site: false, multi_facility_data: false,
  })
  api.getCreateCheck.mockResolvedValue({
    allowed: false,
    reasons: [
      { code: 'no_group_grant', message: 'Only an administrator with group-wide access can add a facility.' },
      { code: 'screens_not_ready', message: "Orb's screens are not yet ready to keep two facilities' patients apart." },
    ],
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  resetPlatformFlags()
})

describe('FacilitiesPanel', () => {
  it('always shows facility 1 and nothing about adding one with the switch off', async () => {
    render(<FacilitiesPanel />)
    const card = await screen.findByTestId('facility-card-1')
    expect(within(card).getByRole('heading', { name: 'Sunrise General' })).toBeTruthy()
    expect(within(card).getByText('Sunrise General (from the site settings file)')).toBeTruthy()
    expect(screen.getByTestId('facility-not-ready').textContent).toContain('Address, GSTIN')
    expect(screen.queryByTestId('facility-card-2')).toBeNull()
    expect(screen.queryByText('Adding a facility')).toBeNull()
    expect(api.getCreateCheck).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/—/)
  })

  it('sends only the changed fields and shows the saved identity', async () => {
    api.updateFacility.mockResolvedValue(facility({
      gstin: '27AAPFU0939F1ZV', state_code: '27', state_code_name: 'Maharashtra',
      legal_name: 'Sunrise Hospitals Pvt Ltd', updated_at: '2026-10-10T06:30:00Z',
    }))
    render(<FacilitiesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    fireEvent.change(screen.getByLabelText('GSTIN'), { target: { value: '27aapfu0939f1zv' } })
    fireEvent.change(screen.getByLabelText('Legal name'), { target: { value: ' Sunrise Hospitals Pvt Ltd ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save details' }))
    await waitFor(() => expect(api.updateFacility).toHaveBeenCalledTimes(1))
    expect(api.updateFacility).toHaveBeenCalledWith(1, {
      gstin: '27AAPFU0939F1ZV', legal_name: 'Sunrise Hospitals Pvt Ltd',
    })
    expect(await screen.findByRole('status')).toHaveTextContent('numbered from now on')
    expect(screen.getByText('27AAPFU0939F1ZV')).toBeTruthy()
    expect(screen.getByText('27, Maharashtra')).toBeTruthy()
  })

  it('clears a field by sending null', async () => {
    api.listFacilities.mockResolvedValue({
      facilities: [facility({ hfr_id: 'IN2710000123' })], multi_site: false, multi_facility_data: false,
    })
    api.updateFacility.mockResolvedValue(facility())
    render(<FacilitiesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    fireEvent.change(screen.getByLabelText('Health facility registry ID'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save details' }))
    await waitFor(() => expect(api.updateFacility).toHaveBeenCalledWith(1, { hfr_id: null }))
  })

  it('shows each refusal beside its field', async () => {
    const err = Object.assign(new Error('Some details need correcting before they can be saved.'), {
      status: 400,
      detail: {
        error: 'invalid_identity',
        errors: [{ field: 'state_code', code: 'state_mismatch',
                   message: 'The GST state code is 29 but the GSTIN starts with 27. They must be the same.' }],
      },
    })
    api.updateFacility.mockRejectedValue(err)
    render(<FacilitiesPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Edit details' }))
    fireEvent.change(screen.getByLabelText('GST state code'), { target: { value: '29' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save details' }))
    const input = screen.getByLabelText('GST state code')
    await waitFor(() => expect(input).toHaveAttribute('aria-invalid', 'true'))
    const describedBy = input.getAttribute('aria-describedby') ?? ''
    const message = describedBy.split(' ').map(id => document.getElementById(id)?.textContent).join(' ')
    expect(message).toContain('GSTIN starts with 27')
    expect(screen.getByRole('alert')).toHaveTextContent('need correcting')
  })

  it('offers no edit to an account that may not edit', async () => {
    api.listFacilities.mockResolvedValue({
      facilities: [facility({ can_edit: false })], multi_site: false, multi_facility_data: false,
    })
    render(<FacilitiesPanel />)
    await screen.findByTestId('facility-card-1')
    expect(screen.queryByRole('button', { name: 'Edit details' })).toBeNull()
  })

  it('lists every facility and why one cannot be added with the switch on', async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: false }) })
    render(<FacilitiesPanel />)
    expect(await screen.findByTestId('facility-card-2')).toBeTruthy()
    const reasons = await screen.findByTestId('facility-create-reasons')
    expect(within(reasons).getAllByRole('listitem')).toHaveLength(2)
    expect(reasons.textContent).toContain('group-wide access')
    expect(api.getCreateCheck).toHaveBeenCalledTimes(1)
    expect(document.body.textContent).not.toMatch(/—/)
  })

  it('says when the details could not be loaded and tries again', async () => {
    api.listFacilities.mockRejectedValueOnce(new Error('Network down'))
    render(<FacilitiesPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Network down')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('facility-card-1')).toBeTruthy()
  })
})
