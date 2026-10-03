/**
 * THE PRESCRIBER-ONLY ALLERGY OVERRIDE, at the last screen before the drug goes
 * in.
 *
 * WHAT THIS SCREEN IS FOR. The interlock blocks correctly — a nurse pressing
 * "Given" on Suresh Reddy's Ceftriaxone 2g IV is refused with a 428 — but until
 * 2026-08-15 that 428 was a DEAD END from every screen: no payload field, no
 * modal, no way through for anyone. Ceftriaxone could not be given to that
 * patient through Orb by any person, ever. Hospitals do give a drug across a
 * charted allergy under supervision (desensitisation, a de-labelled allergy, a
 * risk/benefit call in an emergency), so the founder ruled: prescriber only,
 * with a written reason.
 *
 * The invariants, in the order they matter:
 *
 *   1. THE NURSE IS NEVER SHOWN THE CONTROL. She is the last independent check
 *      on the prescriber's decision. Load-bearing, and asserted against the
 *      exact refusal the server sends.
 *   2. NOT BEFORE THE SERVER HAS SPOKEN. The first press meets the interlock
 *      head-on — no reason field, no reason in the payload. A client that always
 *      sent a reason would turn the block into the default path.
 *   3. IT IS NOT A CLICK-THROUGH. The allergen, severity and charted reaction
 *      are restated at the moment of overriding; the reason is required; the
 *      type-to-confirm is re-armed because the earlier confirmation was for a
 *      lighter act.
 *   4. THE SERVER IS STILL THE AUTHORITY. Nothing here decides anything — the
 *      client-side length check is a courtesy, the permission mirror only
 *      decides what to OFFER, and a refusal records nothing.
 */
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const administerMedication = vi.fn()
let signedIn: { id: string; name: string; role: string | null } | null = null

/* The permission mirror (src/lib/roles.ts) reads the session through
   `getUser`, so signing a user in here drives the REAL permission table — the
   thing under test. Mocking `hasPermission` instead would assert only that the
   modal calls a function. */
vi.mock('../../api/client', () => ({
  administerMedication: (...a: unknown[]) => administerMedication(...a),
  getUser: () => signedIn,
}))

import AdministerModal from './AdministerModal'
import { overrideAudience } from './administerSafety'

const CEFTRIAXONE = { id: 811, drug_name: 'Ceftriaxone', dosage: '2 g', route: 'IV', frequency: 'OD' }
const CHARTED = {
  substance: 'Ceftriaxone',
  reaction: 'urticaria, angio-oedema, hypotension (2023)',
  severity: 'severe',
}

/** Exactly the 428 body emar_routes builds from allergy_interlock.summarize. */
const REFUSAL_428 = Object.assign(
  new Error('allergy interlock: Ceftriaxone conflicts with a documented allergy (Ceftriaxone) — a prescriber must authorise this dose with a written clinical reason before it can be administered'),
  {
    status: 428,
    detail: {
      error: 'allergy_acknowledgement_required',
      message: 'allergy interlock: Ceftriaxone conflicts with a documented allergy (Ceftriaxone)',
      allergy: {
        decision: 'block',
        screen_failed: false,
        override_requires_permission: 'medication.prescribe',
        alerts: [{
          substance: 'Ceftriaxone',
          reaction: 'urticaria, angio-oedema, hypotension (2023)',
          match: 'direct',
          decision: 'block',
          effective_severity: 'severe',
          message: 'Patient is allergic to Ceftriaxone.',
        }],
      },
    },
  },
)

/** The 403 the server sends when a non-prescriber supplies a reason anyway. */
const REFUSAL_403 = Object.assign(
  new Error('Ceftriaxone conflicts with a documented allergy (ceftriaxone) and only a prescriber may authorise giving it. This dose has NOT been recorded — the prescriber must enter the reason themselves.'),
  {
    status: 403,
    detail: {
      error: 'allergy_override_not_permitted',
      required_permission: 'medication.prescribe',
      message: 'only a prescriber may authorise giving it',
      allergy: REFUSAL_428.detail.allergy,
    },
  },
)

const GOOD_REASON =
  'Allergy de-labelled by immunology 12/08 after negative skin test and graded oral challenge; giving under 1:1 observation.'

function signIn(role: string | null) {
  signedIn = role === null ? null : { id: 'U1', name: 'Test User', role }
}

function open() {
  const onRecorded = vi.fn()
  render(
    <AdministerModal
      med={CEFTRIAXONE}
      patientName="Suresh Reddy"
      patientBed="GM-104"
      allergies={[CHARTED]}
      onClose={() => {}}
      onRecorded={onRecorded}
    />,
  )
  return { onRecorded }
}

const confirmField = () => screen.getByLabelText(/confirm drug name/i)
const reasonField = () => screen.queryByLabelText(/why is it safe to give this drug/i)
const giveBtn = () => screen.getByRole('button', { name: /^Given$|despite the allergy/i })

/** Press Given once, with the drug name confirmed, and let the server refuse. */
async function pressGivenAndBeRefused(refusal: unknown = REFUSAL_428) {
  administerMedication.mockRejectedValueOnce(refusal)
  fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
  await act(async () => { fireEvent.click(giveBtn()) })
}

beforeEach(() => {
  administerMedication.mockReset()
  signIn('nurse')
})

/* ══════════════════════════════════════════════════════════════════════════
   1. THE NURSE IS NEVER SHOWN THE CONTROL — the load-bearing case
   ══════════════════════════════════════════════════════════════════════════ */
describe('a nurse is refused the override and is never shown the control', () => {
  it('shows NO reason field after the interlock refuses her dose', async () => {
    signIn('nurse')
    open()
    await pressGivenAndBeRefused()

    // The refusal itself is on screen…
    expect(screen.getByText(/Orb refused this dose/i)).toBeInTheDocument()
    // …and there is nothing to type into. Not a disabled field: no field.
    expect(reasonField()).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /despite the allergy/i })).not.toBeInTheDocument()
  })

  it('tells her WHO can authorise it, and what to do instead', async () => {
    signIn('nurse')
    open()
    await pressGivenAndBeRefused()

    expect(screen.getByText(/Only the prescriber can authorise this dose/i)).toBeInTheDocument()
    // The alternative action, named — this is what stops the workaround.
    expect(screen.getByText(/record it as/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Not given$/ })).not.toBeDisabled()
  })

  it('is decided by the PERMISSION, not by the role name', async () => {
    // A role the mirror does not know, and the pharmacist and admin accounts,
    // are all treated exactly as the nurse is — the check is
    // hasPermission('medication.prescribe'), so nothing rides on the string.
    for (const role of ['nurse', 'pharmacist', 'admin', 'surgeon', '', null]) {
      administerMedication.mockReset()
      signIn(role)
      const { unmount } = render(
        <AdministerModal
          med={CEFTRIAXONE} patientName="Suresh Reddy" allergies={[CHARTED]}
          onClose={() => {}} onRecorded={() => {}}
        />,
      )
      await pressGivenAndBeRefused()
      expect(reasonField()).not.toBeInTheDocument()
      unmount()
    }
  })

  it('records nothing when the server answers her attempt with a 403', async () => {
    // Reachable only from a stale client, since she is offered no control — but
    // the screen must still explain rather than showing a bare "forbidden".
    signIn('nurse')
    const { onRecorded } = open()
    await pressGivenAndBeRefused(REFUSAL_403)

    expect(onRecorded).not.toHaveBeenCalled()
    expect(screen.getByText(/Only the prescriber can authorise this dose/i)).toBeInTheDocument()
    expect(reasonField()).not.toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   2. THE PRESCRIBER — after the server has spoken, and not before
   ══════════════════════════════════════════════════════════════════════════ */
describe('the prescriber is offered the override, but only after a refusal', () => {
  it('offers NOTHING on the first press, and sends no reason', async () => {
    signIn('doctor')
    administerMedication.mockResolvedValue({ status: 'ok', administration: { id: 9 } })
    open()

    // Before anything is sent: no override anywhere on the screen.
    expect(reasonField()).not.toBeInTheDocument()

    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    await act(async () => { fireEvent.click(giveBtn()) })

    const [, payload] = administerMedication.mock.calls[0]
    expect(payload).not.toHaveProperty('allergy_override_reason')
  })

  it('restates the allergen, the charted severity and the charted reaction', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()

    const panel = screen.getByRole('group', { name: /despite the charted allergy/i })
    expect(within(panel).getAllByText('Ceftriaxone').length).toBeGreaterThan(0)
    expect(within(panel).getByText(/severe as charted/i)).toBeInTheDocument()
    expect(within(panel).getByText(/urticaria, angio-oedema, hypotension \(2023\)/)).toBeInTheDocument()
    // Asked for as a clinical justification, not as a formality.
    expect(reasonField()).toBeInTheDocument()
  })

  it('re-arms the type-to-confirm: the earlier confirmation does not carry over', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()

    expect(confirmField()).toHaveValue('')
    fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
    // Reason written, name not re-typed → still refused by the button.
    expect(giveBtn()).toBeDisabled()

    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    expect(giveBtn()).not.toBeDisabled()
  })

  it('will not send a reason too short to be one', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })

    for (const tooShort of ['', '   ', 'ok', 'per doc']) {
      fireEvent.change(reasonField()!, { target: { value: tooShort } })
      expect(giveBtn()).toBeDisabled()
    }
    fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
    expect(giveBtn()).not.toBeDisabled()
  })

  it('sends the written reason, on the SAME idempotency key as the refused attempt', async () => {
    signIn('doctor')
    const { onRecorded } = open()
    await pressGivenAndBeRefused()
    // Queued AFTER the refusal: `mockRejectedValueOnce` and
    // `mockResolvedValueOnce` share one FIFO queue, so priming the success
    // first would make the FIRST press succeed and quietly test nothing.
    administerMedication.mockResolvedValueOnce({ status: 'ok', administration: { id: 12 } })

    fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
    fireEvent.change(confirmField(), { target: { value: 'ceftriaxone' } })
    await act(async () => { fireEvent.click(giveBtn()) })

    expect(administerMedication).toHaveBeenCalledTimes(2)
    const [firstMed, firstPayload, firstKey] = administerMedication.mock.calls[0]
    const [secondMed, secondPayload, secondKey] = administerMedication.mock.calls[1]
    expect(firstMed).toBe(811)
    expect(secondMed).toBe(811)
    expect(firstPayload).not.toHaveProperty('allergy_override_reason')
    expect(secondPayload).toMatchObject({
      status: 'given',
      dose_given: '2 g',
      route_given: 'IV',
      confirmation_method: 'typed',
      allergy_override_reason: GOOD_REASON,
    })
    // ONE DOSE, ONE KEY — the retry that carries the override is the same dose,
    // so a lost response cannot become a second charted administration.
    expect(secondKey).toBe(firstKey)
    expect(onRecorded).toHaveBeenCalledWith({ id: 12 })
  })

  it('keeps the written reason when a DIFFERENT gate refuses the same dose', async () => {
    // The dose guard shares the 428 and fires after the allergy is cleared.
    // Wiping a paragraph the prescriber has just written, because a second gate
    // spoke, is how a clinician learns to type the shortest thing that passes.
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()
    fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })

    administerMedication.mockRejectedValueOnce(Object.assign(new Error('dose'), {
      status: 428,
      detail: { error: 'dose_acknowledgement_required', dose: { decision: 'block' } },
    }))
    await act(async () => { fireEvent.click(giveBtn()) })

    expect(reasonField()).toHaveValue(GOOD_REASON)
    // …and the allergy verdict is still on screen, not blanked by the second
    // failure. The hazard did not stop being true.
    expect(screen.getByText(/Orb refused this dose/i)).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   3. THE DELIBERATE SEAM: an unreadable allergy list is not the same thing
   ══════════════════════════════════════════════════════════════════════════ */
describe('a screen that FAILED is clearable by the person at the bedside', () => {
  it('offers the reason box to a nurse when the allergy list could not be read', async () => {
    // Nothing charted is being overruled — only an outage. Restricting this to
    // a prescriber would let an infrastructure fault stop a nurse giving
    // adrenaline in a peri-arrest. The server takes the same view; this screen
    // follows it rather than deciding for itself.
    signIn('nurse')
    open()
    await pressGivenAndBeRefused(Object.assign(new Error('unavailable'), {
      status: 428,
      detail: {
        error: 'allergy_screen_unavailable',
        allergy: {
          decision: 'block', screen_failed: true, alerts: [],
          override_requires_permission: null,
          message: 'Allergy data could not be read — this dose has NOT been screened.',
        },
      },
    }))

    expect(screen.getByText(/This dose was NOT screened/i)).toBeInTheDocument()
    expect(reasonField()).toBeInTheDocument()
    expect(screen.getByText(/judgement about an unknown/i)).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   4. THE AUDIENCE RULE ITSELF — read off the server, fail closed
   ══════════════════════════════════════════════════════════════════════════ */
describe('overrideAudience', () => {
  it.each([
    [null, 'none', 'nothing was refused'],
    [{ decision: 'block', screen_failed: true }, 'anyone', 'an outage, not a charted allergy'],
    [{ decision: 'block', override_requires_permission: 'medication.prescribe' }, 'prescriber',
      'the server named the prescribing permission'],
    [{ decision: 'block' }, 'prescriber',
      'FAIL CLOSED: a server that sent no requirement still means prescriber-only'],
    [{ decision: 'block', override_requires_permission: 'something.new' }, 'none',
      'a requirement this build does not understand is not an open gate'],
  ])('%o → %s (%s)', (input, want, _why) => {
    expect(overrideAudience(input as never)).toBe(want)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   5. A REFUSED REASON SAYS WHY — found in a browser check, 2026-08-15
   ══════════════════════════════════════════════════════════════════════════
   The server refuses a junk reason with a precise sentence, and the modal
   suppressed it: the `error` block is hidden whenever a serverScreen exists
   (right, for the interlock's own verdict — printing it twice trains a nurse to
   skim), and by the time the override panel is on screen a serverScreen ALWAYS
   exists. So the prescriber typed "per doctor", pressed the button, watched the
   confirm field clear, and was told nothing. The one refusal in this flow whose
   remedy is entirely in the user's hands was the one the UI would not state.

   Both halves are asserted: the reason-refusal appears, and the FIRST refusal
   still does not print the interlock's sentence a second time. */
const JUNK_REASON_428 = Object.assign(
  new Error("an allergy override requires a clinical reason (e.g. 'allergy de-labelled by immunology 12/06, challenge tolerated')"),
  { status: 428, detail: { error: 'allergy_acknowledgement_required', allergy: REFUSAL_428.detail.allergy } },
)

describe('a refused override reason is stated, not swallowed', () => {
  it('shows the server sentence when the reason itself is refused', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()

    fireEvent.change(reasonField()!, { target: { value: 'per doctor' } })
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    administerMedication.mockRejectedValueOnce(JUNK_REASON_428)
    await act(async () => { fireEvent.click(giveBtn()) })

    // Inside the authorise panel, where the prescriber is looking — not in a
    // generic banner at the other end of a modal this tall.
    const panel = screen.getByText(/why is it safe to give this drug/i).closest('section')!
    expect(within(panel).getByText(/requires a clinical reason/i)).toBeTruthy()
  })

  it('still does not reprint the interlock verdict on the FIRST refusal', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()

    // One statement of the block, not two: the refusal panel carries it, and
    // the generic error block stays silent.
    expect(screen.queryAllByText(/conflicts with a documented allergy/i)).toHaveLength(0)
  })

  it('clears the reason-refusal when the prescriber tries again', async () => {
    signIn('doctor')
    open()
    await pressGivenAndBeRefused()

    fireEvent.change(reasonField()!, { target: { value: 'per doctor' } })
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    administerMedication.mockRejectedValueOnce(JUNK_REASON_428)
    await act(async () => { fireEvent.click(giveBtn()) })
    expect(screen.getAllByText(/requires a clinical reason/i).length).toBeGreaterThan(0)

    fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
    fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
    administerMedication.mockResolvedValueOnce({ administration: { id: 1, status: 'given' } })
    await act(async () => { fireEvent.click(giveBtn()) })
    expect(screen.queryByText(/requires a clinical reason/i)).toBeNull()
  })
})

/* During the 2026-08-15 browser check a second submission appeared to carry the
   FIRST, rejected reason. If a prescriber who corrects their wording silently
   re-sends the old text, the ledger would record a sentence nobody chose — so
   it is asserted here rather than argued about. */
it('sends the CORRECTED reason, not the rejected one', async () => {
  signIn('doctor')
  open()
  await pressGivenAndBeRefused()

  fireEvent.change(reasonField()!, { target: { value: 'per doctor' } })
  fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
  administerMedication.mockRejectedValueOnce(JUNK_REASON_428)
  await act(async () => { fireEvent.click(giveBtn()) })

  fireEvent.change(reasonField()!, { target: { value: GOOD_REASON } })
  fireEvent.change(confirmField(), { target: { value: 'Ceftriaxone' } })
  administerMedication.mockResolvedValueOnce({ administration: { id: 9, status: 'given' } })
  await act(async () => { fireEvent.click(giveBtn()) })

  const lastPayload = administerMedication.mock.calls.at(-1)![1] as Record<string, unknown>
  expect(lastPayload.allergy_override_reason).toBe(GOOD_REASON)
})

/* ══════════════════════════════════════════════════════════════════════════
   6. A DOSE-GUARD REFUSAL IS NOT A DEAD END (F056)
   ══════════════════════════════════════════════════════════════════════════
   The 428 `dose_acknowledgement_required` carries the guard's findings on
   `detail.dose`. The modal used to recognise only the allergy payload, so the
   findings were thrown away, a generic line appeared, the typed confirmation
   stayed in the box and Given stayed armed for a dose that would be refused
   again. No override box is offered (who may type one is ruling R011). */
const DOSE_REFUSAL_428 = Object.assign(
  new Error('dose guard: Paracetamol 8 g is at or above 3000 mg'),
  {
    status: 428,
    detail: {
      error: 'dose_acknowledgement_required',
      message: 'dose guard: Paracetamol 8 g is at or above 3000 mg',
      dose: {
        decision: 'block',
        findings: [{
          kind: 'magnitude', decision: 'block', rule: 'paracetamol_single',
          message: 'Paracetamol 8 g is at or above 3000 mg, no adult indication reaches this as a single dose.',
        }],
      },
    },
  },
)

describe('a dose the dose guard refuses', () => {
  const PARACETAMOL = { id: 902, drug_name: 'Paracetamol', dosage: '8 g', route: 'PO', frequency: 'QDS' }

  function openParacetamol() {
    const onRecorded = vi.fn()
    render(
      <AdministerModal
        med={PARACETAMOL} patientName="Asha Menon" allergies={[]}
        onClose={() => {}} onRecorded={onRecorded}
      />,
    )
    return { onRecorded }
  }

  async function pressGiven() {
    administerMedication.mockRejectedValueOnce(DOSE_REFUSAL_428)
    fireEvent.change(confirmField(), { target: { value: 'Paracetamol' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Given$/ })) })
  }

  it('shows the findings in the danger panel and names the next step', async () => {
    signIn('nurse')
    const { onRecorded } = openParacetamol()
    await pressGiven()

    const panel = screen.getByRole('heading', { name: /The dose check stopped this dose/ }).closest('section')!
    expect(panel.className).toMatch(/--danger/)
    expect(within(panel).getByText(/no adult indication reaches this as a single dose/)).toBeInTheDocument()
    expect(within(panel).getByText(/Contact the prescriber, or record it/)).toBeInTheDocument()
    expect(onRecorded).not.toHaveBeenCalled()
    // Said once, in the panel, not again as a bare error line.
    expect(screen.queryAllByText(/at or above 3000 mg/)).toHaveLength(1)
  })

  it('clears the confirmation and keeps Given off, with Not given one press away', async () => {
    signIn('nurse')
    openParacetamol()
    await pressGiven()

    expect(confirmField()).toHaveValue('')
    fireEvent.change(confirmField(), { target: { value: 'Paracetamol' } })
    expect(screen.getByRole('button', { name: /^Given$/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /^Not given$/ })).not.toBeDisabled()
  })

  it('offers no override box to anyone, prescriber included, until R011 is ruled', async () => {
    signIn('doctor')
    openParacetamol()
    await pressGiven()
    expect(reasonField()).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /reason/i })).not.toBeInTheDocument()
  })
})
