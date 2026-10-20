/**
 * GroupMatchCard (multisite-17): the front desk's search of the whole group.
 *
 *  * all four facts and a reason are needed before a search can run;
 *  * a match shows who the patient is and nothing clinical;
 *  * no match says so in words, and the desk can still register a new patient;
 *  * registering the visit sends the match's ticket and what the patient
 *    stated (for the identity check), never a value the record holds.
 *
 * Driven through a mocked apiFetch, so the real src/api/groupMatch.ts builds
 * the request bodies.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GroupMatchCard from './GroupMatchCard'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
}))

const DOCTORS = [
  { id: 'DOC-7', name: 'Dr Vellanki', department: 'Satellite Medicine' },
  { id: 'DOC-8', name: 'Dr Orrin', department: 'Satellite Surgery' },
]

const MATCH = {
  id: 'PAT-GM1',
  name: 'Zerelda Quonset',
  age: 47,
  sex: 'F',
  group_mrn: 'UHID-000123',
  registered_facility: { id: 1, code: 'MAIN' },
  ticket: '1999999999.abc',
}

function fill() {
  fireEvent.change(screen.getByLabelText('Full name'), { target: { value: 'Zerelda Quonset' } })
  fireEvent.change(screen.getByLabelText('Age in years'), { target: { value: '47' } })
  fireEvent.change(screen.getByLabelText('Sex'), { target: { value: 'F' } })
  fireEvent.change(screen.getByLabelText('Phone number'), { target: { value: '98450 12345' } })
  fireEvent.change(screen.getByLabelText('Why you are searching the whole group'), {
    target: { value: 'Patient says she was treated at the main hospital' },
  })
}

beforeEach(() => {
  apiFetch.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GroupMatchCard', () => {
  it('needs all four details and a reason before it searches', () => {
    render(<GroupMatchCard clinics={['Satellite Medicine']} doctors={DOCTORS} onRegistered={vi.fn()} />)
    const button = screen.getByRole('button', { name: 'Search all facilities' })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Full name'), { target: { value: 'Zerelda Quonset' } })
    fireEvent.change(screen.getByLabelText('Age in years'), { target: { value: '47' } })
    fireEvent.change(screen.getByLabelText('Sex'), { target: { value: 'F' } })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Phone number'), { target: { value: '98450 12345' } })
    expect(button).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Why you are searching the whole group'), {
      target: { value: 'Treated at the main hospital' },
    })
    expect(button).toBeEnabled()
  })

  it('shows who matches and nothing clinical, then registers the visit with the ticket', async () => {
    const onRegistered = vi.fn()
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/frontdesk/group-match') {
        return { matches: [MATCH], truncated: false, searched_at: '2026-10-20T10:00:00+00:00',
          searches_left: 19, ticket_seconds: 900 }
      }
      if (path === '/frontdesk/opd/visits') {
        return { id: 'OPD-77', patient_id: 'PAT-GM1', department: 'Satellite Medicine', token_number: 4 }
      }
      throw new Error(`unrouted ${path}`)
    })
    render(<GroupMatchCard clinics={['Satellite Medicine', 'Satellite Surgery']} doctors={DOCTORS}
      onRegistered={onRegistered} />)
    fill()
    fireEvent.click(screen.getByRole('button', { name: 'Search all facilities' }))

    expect(await screen.findByText('Zerelda Quonset')).toBeInTheDocument()
    expect(screen.getByText(/UHID-000123/)).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'MAIN' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'Female' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '47' })).toBeInTheDocument()

    const [, searchOptions] = apiFetch.mock.calls[0]
    expect(JSON.parse(String(searchOptions.body))).toEqual({
      name: 'Zerelda Quonset', age_years: 47, date_of_birth: null, sex: 'F',
      phone: '98450 12345', reason: 'Patient says she was treated at the main hospital',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Register a visit here' }))
    const register = screen.getByRole('button', { name: 'Register and issue token' })
    expect(register).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Clinic'), { target: { value: 'Satellite Medicine' } })
    // Only the chosen clinic's doctors are offered.
    expect(screen.queryByRole('option', { name: 'Dr Orrin' })).toBeNull()
    fireEvent.change(screen.getByLabelText('Consulting doctor'), { target: { value: 'DOC-7' } })
    fireEvent.click(register)

    await waitFor(() => expect(onRegistered).toHaveBeenCalled())
    expect(screen.getByText(/Token 4/)).toBeInTheDocument()
    const [visitPath, visitOptions] = apiFetch.mock.calls[1]
    expect(visitPath).toBe('/frontdesk/opd/visits')
    const body = JSON.parse(String(visitOptions.body))
    expect(body.patient_id).toBe('PAT-GM1')
    expect(body.department).toBe('Satellite Medicine')
    expect(body.doctor_id).toBe('DOC-7')
    expect(body.group_match_ticket).toBe('1999999999.abc')
    expect(body.identity_check).toEqual({
      name: 'Zerelda Quonset', age_years: 47, date_of_birth: null, phone_last4: '2345',
    })
  })

  it('says so in words when nobody in the group matches', async () => {
    apiFetch.mockResolvedValue({ matches: [], truncated: false, searched_at: 'x',
      searches_left: 18, ticket_seconds: 900 })
    render(<GroupMatchCard clinics={['Satellite Medicine']} doctors={DOCTORS} onRegistered={vi.fn()} />)
    fill()
    fireEvent.click(screen.getByRole('button', { name: 'Search all facilities' }))
    expect(await screen.findByText(/No patient in the group matches all four details/))
      .toBeInTheDocument()
    expect(screen.getByText(/register them as a new patient/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Register a visit here' })).toBeNull()
  })

  it("shows the server's refusal in its own words", async () => {
    apiFetch.mockRejectedValue(new Error('You have searched the whole group 20 times in the last hour.'))
    render(<GroupMatchCard clinics={['Satellite Medicine']} doctors={DOCTORS} onRegistered={vi.fn()} />)
    fill()
    fireEvent.click(screen.getByRole('button', { name: 'Search all facilities' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('20 times in the last hour')
  })

  it('sends a date of birth instead of an age when one is given', async () => {
    apiFetch.mockResolvedValue({ matches: [], truncated: false, searched_at: 'x',
      searches_left: 17, ticket_seconds: 900 })
    render(<GroupMatchCard clinics={['Satellite Medicine']} doctors={DOCTORS} onRegistered={vi.fn()} />)
    fill()
    fireEvent.change(screen.getByLabelText('Or date of birth'), { target: { value: '1979-03-14' } })
    expect(screen.getByLabelText('Age in years')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Search all facilities' }))
    await screen.findByText(/No patient in the group matches/)
    const body = JSON.parse(String(apiFetch.mock.calls[0][1].body))
    expect(body.date_of_birth).toBe('1979-03-14')
    expect(body.age_years).toBeNull()
  })
})
