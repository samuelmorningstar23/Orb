/**
 * THE ALLERGY IS REPEATED ON THE LAST SCREEN BEFORE THE DRUG GOES IN.
 *
 * Seen on the running ward, 2026-08-14. Suresh Reddy, GM-104:
 *   * a CHARTED SEVERE CEFTRIAXONE ANAPHYLAXIS — urticaria, angio-oedema,
 *     hypotension, 2023;
 *   * an ACTIVE Ceftriaxone 2g IV OD order, 5h17m late on the due-now list;
 *   * a red allergy banner on the medicine-round sheet;
 *   * and NOTHING in the Administer modal (observed: allergyInModal false).
 *
 * The round sheet's banner is on a screen the nurse has already left. This
 * modal is the last one before the dose is given.
 *
 * TWO SEPARATE THINGS ARE ASSERTED HERE, and the separation is the point:
 *
 *   1. THE DISPLAY — the allergen, its severity and its reaction as charted
 *      are on the screen, before the confirmation field, not after the press.
 *   2. THE CLINICAL GATE IS UNTOUCHED — the interlock lives in
 *      server/services/allergy_interlock.py and is the authority on whether a
 *      dose may proceed. These cases pin that this modal still sends exactly
 *      the same request it always did, and still refuses to record anything
 *      the server refused. A UI change must not be able to alter a clinical
 *      gate quietly, so the gate is asserted in its own describe block rather
 *      than implied by the display cases passing.
 */
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

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

import AdministerModal, { type AdministerAllergy } from './AdministerModal'
import { allergenMatchesDrug } from './administerSafety'

/** Suresh Reddy's late dose. */
const CEFTRIAXONE = { id: 811, drug_name: 'Ceftriaxone', dosage: '2 g', route: 'IV', frequency: 'OD' }

/** His allergy, exactly as `patient_allergies` holds it. */
const CEFTRIAXONE_ANAPHYLAXIS: AdministerAllergy = {
  substance: 'Ceftriaxone',
  reaction: 'urticaria, angio-oedema, hypotension (2023)',
  severity: 'severe',
}

const PENICILLIN: AdministerAllergy = {
  substance: 'Penicillin', reaction: 'rash', severity: 'moderate',
}

function open(over: {
  med?: typeof CEFTRIAXONE
  allergies?: AdministerAllergy[] | null
} = {}) {
  const onRecorded = vi.fn()
  render(
    <AdministerModal
      med={over.med ?? CEFTRIAXONE}
      patientName="Suresh Reddy"
      patientBed="GM-104"
      allergies={over.allergies === undefined ? [CEFTRIAXONE_ANAPHYLAXIS] : over.allergies}
      onClose={() => {}}
      onRecorded={onRecorded}
    />,
  )
  return { onRecorded }
}

const givenBtn = () => screen.getByRole('button', { name: /^Given$/ })
const confirmField = () => screen.getByRole('textbox')

/** The allergy region — an `alert` when it must stop the nurse. */
const alertRegion = () => screen.getAllByRole('alert')[0]

beforeEach(() => { administerMedication.mockReset() })

/* ══════════════════════════════════════════════════════════════════════════
   1. THE DISPLAY
   ══════════════════════════════════════════════════════════════════════════ */
describe('the Administer modal repeats the charted allergy', () => {
  it('names the allergen, the severity AND the reaction as charted', () => {
    open()
    const region = alertRegion()
    expect(within(region).getByText('Ceftriaxone')).toBeInTheDocument()
    expect(within(region).getByText('severe')).toBeInTheDocument()
    expect(
      within(region).getByText(/urticaria, angio-oedema, hypotension \(2023\)/),
    ).toBeInTheDocument()
  })

  it('states it BEFORE the confirmation field, not after the press', () => {
    open()
    const region = alertRegion()
    const field = confirmField()
    // Node.compareDocumentPosition: FOLLOWING means the field comes after the
    // allergy, i.e. the nurse reads the allergy on the way to typing.
    expect(region.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says the dose will be screened and can be refused, before anything is typed', () => {
    open()
    expect(within(alertRegion()).getByText(/screens this dose against the live allergy list/i))
      .toBeInTheDocument()
    // Nothing has been sent yet — this is standing information, not a response.
    expect(administerMedication).not.toHaveBeenCalled()
  })

  it('carries role="alert" so it is announced, not just coloured', () => {
    open()
    expect(alertRegion()).toHaveTextContent(/Charted allergy to Ceftriaxone/i)
  })

  it('still lists allergies that are NOT this drug, under the one that is', () => {
    open({ allergies: [PENICILLIN, CEFTRIAXONE_ANAPHYLAXIS] })
    const region = alertRegion()
    expect(within(region).getByText(/Also charted/i)).toBeInTheDocument()
    expect(within(region).getByText('Penicillin')).toBeInTheDocument()
  })

  it('shows every charted allergy even when none of them names this drug', () => {
    open({ med: { ...CEFTRIAXONE, drug_name: 'Paracetamol' }, allergies: [PENICILLIN] })
    expect(screen.getByText('Penicillin')).toBeInTheDocument()
    expect(screen.getByText(/rash/)).toBeInTheDocument()
    // …and does not claim the dose is clear: the server screens class and
    // cross-reactivity, which this screen does not attempt.
    expect(screen.getByText(/still screens this dose against the whole list/i)).toBeInTheDocument()
  })

  it('an UNREADABLE list is never rendered as an empty one', () => {
    open({ allergies: null })
    const region = alertRegion()
    expect(within(region).getByText(/Allergy list unavailable/i)).toBeInTheDocument()
    expect(within(region).getByText(/not.*the same as/i)).toBeInTheDocument()
    expect(screen.queryByText(/NKDA/)).not.toBeInTheDocument()
    expect(screen.queryByText(/^No allergies recorded$/)).not.toBeInTheDocument()
  })

  it('an EMPTY list says none recorded, never NKDA, and asks the nurse to confirm', () => {
    // NKDA is a clinician's assertion; an empty list is also every patient
    // nobody asked (every UI admission before 2026-10-01 charted none).
    open({ allergies: [] })
    expect(screen.getByText(/^No allergies recorded$/)).toBeInTheDocument()
    expect(screen.getByText(/Confirm with the patient/)).toBeInTheDocument()
    expect(screen.getByText(/never asked/i)).toBeInTheDocument()
    expect(screen.queryByText(/NKDA/)).not.toBeInTheDocument()
  })

  it('shows a charted allergy whose severity was never graded, as ungraded', () => {
    open({ allergies: [{ substance: 'Ceftriaxone', reaction: 'collapse', severity: null }] })
    expect(screen.getByText(/severity not recorded/i)).toBeInTheDocument()
  })

  it("surfaces the interlock's OWN verdict when the server refuses the dose", async () => {
    // Exactly the 428 body emar_routes builds from allergy_interlock.summarize.
    const refusal = Object.assign(
      new Error('allergy interlock: Ceftriaxone conflicts with a documented allergy (Ceftriaxone) — a typed reason is required to administer'),
      {
        status: 428,
        detail: {
          error: 'allergy_acknowledgement_required',
          message: 'allergy interlock: Ceftriaxone conflicts with a documented allergy (Ceftriaxone) — a typed reason is required to administer',
          allergy: {
            decision: 'block',
            screen_failed: false,
            alerts: [{
              substance: 'Ceftriaxone',
              reaction: 'urticaria, angio-oedema, hypotension (2023)',
              match: 'direct',
              decision: 'block',
              effective_severity: 'severe',
              message: 'Patient is allergic to Ceftriaxone (documented reaction: urticaria, angio-oedema, hypotension (2023)).',
            }],
          },
        },
      },
    )
    administerMedication.mockRejectedValue(refusal)
    const { onRecorded } = open()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    await act(async () => { fireEvent.click(givenBtn()) })

    expect(screen.getByText(/Orb refused this dose/i)).toBeInTheDocument()
    expect(screen.getByText(/direct match/i)).toBeInTheDocument()
    expect(onRecorded).not.toHaveBeenCalled()
  })

  it('says a failed screen is a screen that did not happen, not a clean one', async () => {
    administerMedication.mockRejectedValue(Object.assign(new Error('unavailable'), {
      status: 428,
      detail: {
        error: 'allergy_screen_unavailable',
        allergy: {
          decision: 'block', screen_failed: true, alerts: [],
          message: 'Allergy data could not be read — this dose has NOT been screened. Do not assume the patient has no allergies.',
        },
      },
    }))
    open({ allergies: [] })
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    await act(async () => { fireEvent.click(givenBtn()) })
    expect(screen.getByText(/This dose was NOT screened/i)).toBeInTheDocument()
    expect(screen.getByText(/Do not assume the patient has no allergies/i)).toBeInTheDocument()
  })
})

/* The emphasis rule is deliberately NARROWER than the server's matcher (name
   overlap only). Narrower is safe in this direction: it can fail to shout, and
   the allergy is still listed below — it can never quieten one the server
   would stop. These pin that it does not go WIDER, which is where the
   documented alert-fatigue hazard lives. */
describe('which charted allergy is lifted to the top', () => {
  it.each([
    ['Ceftriaxone', 'Ceftriaxone', true, 'the same substance'],
    ['ceftriaxone', 'Ceftriaxone 2g Injection', true, 'the charted name inside the product name'],
    ['Penicillin', 'Amoxicillin', false, 'a class relative — the SERVER catches this one, not this screen'],
    ['Pen', 'Meropenem', false, 'a 3-character allergen must never substring-match a formulary'],
    ['', 'Ceftriaxone', false, 'an empty allergen'],
    ['Ceftriaxone', '', false, 'an empty drug name'],
  ])('%s vs %s → %s (%s)', (substance, drug, want) => {
    expect(allergenMatchesDrug(substance, drug)).toBe(want)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   2. THE CLINICAL GATE, ASSERTED SEPARATELY
   ══════════════════════════════════════════════════════════════════════════ */
describe('the interlock is unchanged by this screen', () => {
  it('sends the same request it always did — no new field, no override', async () => {
    administerMedication.mockResolvedValue({ status: 'ok', administration: { id: 9 } })
    open()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    await act(async () => { fireEvent.click(givenBtn()) })

    expect(administerMedication).toHaveBeenCalledTimes(1)
    const [medId, payload] = administerMedication.mock.calls[0]
    expect(medId).toBe(811)
    expect(payload).toEqual({
      status: 'given',
      dose_given: '2 g',
      route_given: 'IV',
      confirmation_method: 'typed',
    })
    // No client-side acknowledgement of anything. The screen displays; the
    // server decides.
    expect(payload).not.toHaveProperty('allergy_override_reason')
    expect(payload).not.toHaveProperty('allergy_acknowledged')
  })

  it('does not block the dose itself — a charted allergy still reaches the server', async () => {
    // THE GATE IS SERVER-SIDE ON PURPOSE. If this modal started refusing the
    // request locally, the interlock would stop being the thing that decides,
    // and the audited override ledger would stop seeing the attempt.
    administerMedication.mockResolvedValue({ status: 'ok', administration: { id: 9 } })
    open()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    expect(givenBtn()).not.toBeDisabled()
    await act(async () => { fireEvent.click(givenBtn()) })
    expect(administerMedication).toHaveBeenCalledTimes(1)
  })

  it('keeps the type-to-confirm interlock: Given stays disabled until the name matches', () => {
    open()
    expect(givenBtn()).toBeDisabled()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriax' } })
    expect(givenBtn()).toBeDisabled()
    fireEvent.change(confirmField(), { target: { value: 'ceftriaxone' } })
    expect(givenBtn()).not.toBeDisabled()
  })

  it('records nothing when the server refuses, whatever this screen showed', async () => {
    administerMedication.mockRejectedValue(Object.assign(new Error('refused'), {
      status: 428,
      detail: { error: 'allergy_acknowledgement_required', allergy: { decision: 'block', alerts: [] } },
    }))
    const { onRecorded } = open()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    await act(async () => { fireEvent.click(givenBtn()) })
    expect(onRecorded).not.toHaveBeenCalled()
  })

  it('leaves "Not given" reachable — withholding a dose must always be easy', () => {
    open()
    expect(screen.getByRole('button', { name: /^Not given$/ })).not.toBeDisabled()
  })
})
