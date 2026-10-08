/**
 * BedPicker and the placement rules it shares with the admission form
 * (adt-5). What they must never get wrong:
 *
 * 1. AN OCCUPIED BED IS NEVER CHOSEN (ruling ADT-S1 (a)). It cannot be picked;
 *    'Add an extra bed beside it' is offered instead and sent as extra_bed.
 * 2. THE PICKER ASKS FOR WHAT THE SERVER WILL ASK FOR (ruling ADT-S2 (c)): a
 *    reason for a bed not recorded clean or out of service, and for a bed
 *    awaiting a terminal clean an authoriser who acts, with a reason: the
 *    sender themself, or someone who types their own signing PIN. Only
 *    people of the ward's or the treating department (or none) are offered;
 *    an out-of-service bed awaiting a terminal clean asks for both. The form
 *    cannot be sent until they are given.
 * 3. A BOARDER IS SAID IN WORDS and must be confirmed.
 * 4. 'NO BED YET' admits awaiting a bed; a ward not in bed setup can be typed.
 * 5. THE SERVER'S WORDS are printed as sent: status words, and a refusal.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  EMPTY_BED_CHOICE,
  TYPED_WARD,
  authorisersFor,
  bedChoiceMissing,
  bedChoicePayload,
  bedChoiceSummary,
  type AdmissionMaster,
  type BedChoice,
  type MasterBedChoice,
} from '../../api/adt/moves'
import BedPicker from './BedPicker'

function bed(over: Partial<MasterBedChoice>): MasterBedChoice {
  return {
    bed_id: 'BED-1', bed_label: '1', unit_code: 'GM-1/1', room: null, bed_class: null,
    isolation: false, temporary: false, status: 'available', status_words: 'Available',
    tone: 'ready', markers: [], occupied: false, selectable: true, needs: null,
    terminal_clean: false, ...over,
  }
}

const MASTER: AdmissionMaster = {
  backtime_hours: 72,
  self_authoriser: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  authorisers: [
    { id: 'N001', name: 'Nurse Test', role: 'nurse', department: 'General Medicine' },
    { id: 'N900', name: 'Nurse Ortho', role: 'nurse', department: 'Orthopaedics' },
    { id: 'A001', name: 'Admin Wide', role: 'admin', department: null },
  ],
  wards: [
    {
      id: 'WRD-GM', code: 'GM-1', name: 'General ward one', department: 'General Medicine',
      ward_type: 'general', cleaning_tracked: true, register_complete: false,
      beds: [
        bed({ bed_id: 'BED-1', bed_label: '1' }),
        bed({ bed_id: 'BED-12', bed_label: '12', unit_code: 'GM-1/12', status: 'occupied',
          status_words: 'Occupied', tone: 'occupied', occupied: true, selectable: false }),
        bed({ bed_id: 'BED-3', bed_label: '3', status: 'needs_cleaning',
          status_words: 'Needs cleaning', tone: 'attention', needs: 'not_clean_reason' }),
        bed({ bed_id: 'BED-4', bed_label: '4', status: 'needs_cleaning',
          status_words: 'Needs cleaning', tone: 'attention', needs: 'terminal_clean_authorisation',
          terminal_clean: true, isolation: true }),
        bed({ bed_id: 'BED-5', bed_label: '5', status: 'out_of_service',
          status_words: 'Out of service', tone: 'off', needs: 'out_of_service_reason',
          out_of_service: true }),
        bed({ bed_id: 'BED-6', bed_label: '6', status: 'out_of_service',
          status_words: 'Out of service', tone: 'off', needs: 'terminal_clean_authorisation',
          terminal_clean: true, isolation: true, out_of_service: true }),
      ],
    },
    {
      id: 'WRD-CA', code: 'CA-1', name: 'Cardiac ward', department: 'Cardiology',
      ward_type: 'general', cleaning_tracked: false, register_complete: true,
      beds: [bed({ bed_id: 'BED-C1', bed_label: 'C1', unit_code: 'CA-1/C1',
        status: 'vacant_untracked', status_words: 'Vacant, cleaning not tracked' })],
    },
  ],
}

function Harness({ initial = EMPTY_BED_CHOICE, department = 'General Medicine',
  onChoice, refusal = null }: {
  initial?: BedChoice
  department?: string
  onChoice?: (c: BedChoice) => void
  refusal?: { code: string; message: string } | null
}) {
  const [choice, setChoice] = useState<BedChoice>(initial)
  return (
    <BedPicker
      master={MASTER}
      department={department}
      value={choice}
      onChange={(next) => { setChoice(next); onChoice?.(next) }}
      refusal={refusal}
    />
  )
}

function pickWard(id: string) {
  fireEvent.change(screen.getByLabelText('Ward'), { target: { value: id } })
}

describe('BedPicker', () => {
  it('lists the beds of the chosen ward with the server words', () => {
    render(<Harness />)
    pickWard('WRD-GM')
    const beds = screen.getByRole('radiogroup', { name: 'Beds on GM-1' })
    expect(within(beds).getByText('Bed 1')).toBeInTheDocument()
    expect(within(beds).getAllByText('Needs cleaning')).toHaveLength(2)
    expect(within(beds).getAllByText('Isolation')).toHaveLength(2)
    expect(within(beds).getByLabelText(/No bed yet/)).toBeInTheDocument()
  })

  it('never lets an occupied bed be chosen and offers an extra bed beside it', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    const occupied = screen.getByRole('radio', { name: /Bed 12/ })
    expect(occupied).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Add an extra bed beside 12' }))
    const last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(last).toMatchObject({ bedId: 'BED-12', extraBed: true, awaiting: false })
    expect(screen.getByText(/An extra bed with its own label \(12A/)).toBeInTheDocument()
    expect(bedChoicePayload(MASTER, last)).toMatchObject({
      ward_id: 'WRD-GM', bed_id: 'BED-12', extra_bed: true, ward: 'GM-1', bed: '12',
    })
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
  })

  it('asks for a reason for a bed not recorded clean', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 3/ }))
    let last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toMatch(/not yet recorded clean/)
    fireEvent.change(screen.getByLabelText(/has not been recorded clean/), {
      target: { value: 'Ward full, wiped down' } })
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
    expect(bedChoicePayload(MASTER, last)).toMatchObject({
      bed_id: 'BED-3', not_clean_reason: 'Ward full, wiped down' })
  })

  it('needs the signing PIN of anyone else who authorises a bed awaiting a terminal clean', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 4/ }))
    expect(screen.getByText(/waiting for a terminal clean \(an isolation room, or an/))
      .toBeInTheDocument()
    let last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toMatch(/Choose who authorises/)
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'N001' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Only isolation bed' } })
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    // A name alone is not an authorisation.
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toMatch(/their own signing PIN/)
    const pin = screen.getByLabelText(/Their signing PIN/)
    expect(pin).toHaveAttribute('type', 'password')
    fireEvent.change(pin, { target: { value: '2468' } })
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
    expect(bedChoicePayload(MASTER, last)).toMatchObject({
      terminal_clean_authorised_by: 'N001', terminal_clean_reason: 'Only isolation bed',
      terminal_clean_authoriser_pin: '2468' })
    // Choosing someone else clears the PIN typed for the first person.
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'A001' } })
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(last.terminalAuthoriserPin).toBe('')
  })

  it('lets the sender authorise it themself with no PIN', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 4/ }))
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'D001' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Only isolation bed' } })
    expect(screen.queryByLabelText(/Their signing PIN/)).toBeNull()
    const last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
    const payload = bedChoicePayload(MASTER, last)
    expect(payload).toMatchObject({ terminal_clean_authorised_by: 'D001' })
    expect(payload).not.toHaveProperty('terminal_clean_authoriser_pin')
    expect(screen.getByRole('option', { name: /You \(Dr\. Test\)/ })).toBeInTheDocument()
  })

  it('offers only authorisers of the ward or the treating department, or of none', () => {
    render(<Harness />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 4/ }))
    const offered = within(screen.getByLabelText('Authorised by'))
      .getAllByRole('option').map((o) => o.getAttribute('value'))
    expect(offered).toEqual(['', 'D001', 'N001', 'A001'])
    expect(authorisersFor(MASTER, MASTER.wards[0], 'Orthopaedics').map((a) => a.id))
      .toEqual(['D001', 'N001', 'N900', 'A001'])
  })

  it('asks an out-of-service bed awaiting a terminal clean for both', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 6/ }))
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'D001' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Only isolation room' } })
    let last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toMatch(/out of service/)
    fireEvent.change(screen.getByLabelText(/is out of service/), { target: { value: 'Suction here' } })
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
    expect(bedChoicePayload(MASTER, last)).toMatchObject({
      out_of_service_reason: 'Suction here', terminal_clean_authorised_by: 'D001',
      terminal_clean_reason: 'Only isolation room' })
  })

  it('asks for a reason for a bed out of service', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 5/ }))
    fireEvent.change(screen.getByLabelText(/is out of service/), { target: { value: 'Suction here' } })
    const last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoicePayload(MASTER, last)).toMatchObject({ out_of_service_reason: 'Suction here' })
  })

  it('says who will not open the chart of a boarder and needs it confirmed', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-CA')
    expect(screen.getByText(
      /Nurses on CA-1 \(Cardiology\) will not be able to open this chart\. The General Medicine team keeps it\./,
    )).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: /Bed C1/ }))
    let last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toMatch(/who will be able to open/)
    fireEvent.click(screen.getByRole('checkbox', { name: /I have read this/ }))
    last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
    expect(bedChoicePayload(MASTER, last)).toMatchObject({ boarder_acknowledged: true })
  })

  it('admits awaiting a bed with No bed yet', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByLabelText(/No bed yet/))
    const last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoicePayload(MASTER, last)).toEqual({
      ward: 'GM-1', bed: '', ward_id: 'WRD-GM', awaiting_bed: true })
    expect(bedChoiceSummary(MASTER, last)).toBe('GM-1, no bed yet')
  })

  it('lets a ward not in bed setup be typed, kept unlinked', () => {
    const onChoice = vi.fn()
    render(<Harness onChoice={onChoice} />)
    pickWard(TYPED_WARD)
    fireEvent.change(screen.getByLabelText('Ward as written'), { target: { value: 'Annexe' } })
    fireEvent.change(screen.getByLabelText('Bed as written'), { target: { value: '4' } })
    expect(screen.getByText(/not in bed setup, so the admission will not be linked/))
      .toBeInTheDocument()
    const last = onChoice.mock.calls.at(-1)?.[0] as BedChoice
    expect(bedChoicePayload(MASTER, last)).toEqual({ ward: 'Annexe', bed: '4' })
    expect(bedChoiceMissing(MASTER, last, 'General Medicine')).toBeNull()
  })

  it('needs a ward before anything else', () => {
    expect(bedChoiceMissing(MASTER, EMPTY_BED_CHOICE, 'General Medicine')).toBe('Choose a ward.')
    render(<Harness />)
    pickWard('WRD-GM')
    expect(bedChoiceMissing(MASTER, { ...EMPTY_BED_CHOICE, wardId: 'WRD-GM' }, 'General Medicine'))
      .toBe('Choose a bed, or No bed yet.')
  })

  it('prints a refusal in the server words', () => {
    render(<Harness refusal={{ code: 'bed_occupied',
      message: 'Bed 1 on GM-1 already holds a patient. Choose another bed, or add an extra bed with its own label.' }} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Bed 1 on GM-1 already holds a patient.')
  })

  it('writes no em dash in anything it says', () => {
    const { container } = render(<Harness />)
    pickWard('WRD-GM')
    fireEvent.click(screen.getByRole('radio', { name: /Bed 4/ }))
    expect(container.textContent ?? '').not.toContain(String.fromCharCode(0x2014))
  })
})
