/**
 * DietaryPanel — the four things this screen must never get wrong.
 *
 * 1. NIL BY MOUTH IS UNMISSABLE, IN WORDS, WITH ITS CLINICIAN AND ITS END TIME.
 *    Not a coloured row inside the diet table — a block ABOVE it, carrying the
 *    literal words "NIL BY MOUTH", who ordered it and until when. An NPO patient
 *    rendered as one row among thirty is an NPO patient who gets handed
 *    breakfast at 07:00.
 *
 * 2. AN EXPIRED NPO WINDOW SAYS "REVIEW NOW", AND THE PATIENT IS STILL NPO. The
 *    screen must never present a passed window as a resumed diet.
 *
 * 3. AN UNREADABLE ALLERGY LIST IS NOT "NO KNOWN ALLERGIES". Those two states
 *    look identical and mean opposite things; rendering the first as the second
 *    is the most dangerous sentence this panel could print.
 *
 * 4. A DISABLED MODULE IS NOT AN EMPTY WARD. A site running its own kitchen
 *    system 404s these routes; that must read as "not available at this site",
 *    never as a ward where nobody has a diet order.
 *
 * PROVEN ABLE TO FAIL (2026-08-12): each assertion was watched go red with its
 * defect reintroduced — the NPO block rendered inside the diet table instead of
 * above it, `npoWindowLabel` returning a raw negative duration instead of the
 * review wording, the `allergies_unavailable` branch removed from <Allergies> so
 * an empty list fell through to "No allergies recorded", and the
 * `isModuleDisabled` handling deleted — then restored.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DietaryPanel from './DietaryPanel'
import { DietaryApiError, mealServiceForHour, npoWindowLabel, sumLines } from '../../api/dietary'

vi.mock('../../api/dietary', async () => {
  const actual = await vi.importActual<typeof import('../../api/dietary')>('../../api/dietary')
  return {
    ...actual,
    getMeta: vi.fn(),
    getSummary: vi.fn(),
    getWards: vi.fn(),
    getWardView: vi.fn(),
    getKitchenBoard: vi.fn(),
    getMealPlans: vi.fn(),
    getIndents: vi.fn(),
    getStaleOrders: vi.fn(),
    saveDietOrder: vi.fn(),
    discontinueDietOrder: vi.fn(),
    buildMealPlan: vi.fn(),
    lockMealPlan: vi.fn(),
    raiseIndent: vi.fn(),
    issueIndent: vi.fn(),
    cancelIndent: vi.fn(),
  }
})

const api = await import('../../api/dietary')

const META = {
  diet_types: ['normal', 'diabetic', 'renal', 'cardiac', 'soft', 'liquid', 'npo'],
  diet_type_labels: {
    normal: 'Normal / full diet', diabetic: 'Diabetic', renal: 'Renal',
    cardiac: 'Cardiac / low salt', soft: 'Soft diet', liquid: 'Liquid diet',
    npo: 'Nil by mouth (NPO)',
  },
  textures: ['regular', 'soft', 'minced', 'pureed', 'liquid', 'thickened'],
  texture_labels: { regular: 'Regular', soft: 'Soft', pureed: 'Pureed' },
  meal_services: ['breakfast', 'lunch', 'dinner'],
  meal_service_labels: { breakfast: 'Breakfast', lunch: 'Lunch', dinner: 'Dinner' },
  npo_diet_type: 'npo',
  npo_label: 'NIL BY MOUTH',
  npo_expiry_notice:
    'This NPO window has passed. The diet has NOT resumed automatically — a '
    + 'clinician must review and write the next order.',
  npo_max_window_hours: 336,
  npo_long_window_hours: 48,
  allergy_source: 'patient_allergies',
  allergy_unavailable_message:
    'Allergies could not be read — this diet has NOT been checked against them. '
    + 'Do not assume the patient has no allergies.',
  allergies_are_read_not_copied: true,
}

const SUMMARY = {
  active_orders: 2, meals: 1, npo_active: 1, npo_needs_review: 0,
  npo_long_window: 0, stale_order_count: 0, admitted_without_order: 0,
  ward_count: 1, npo_label: 'NIL BY MOUTH',
  npo_expiry_notice: META.npo_expiry_notice,
}

function order(over: Record<string, unknown> = {}) {
  return {
    id: 1, patient_id: 'P1', patient_name: 'Asha Rao', patient_mrn: 'MRN-1',
    admission_id: 'A1', current_admission_id: 'A1',
    ward: 'W1', bed: '4', department: 'Medicine',
    diet_type: 'diabetic', diet_label: 'Diabetic',
    texture: 'regular', texture_label: 'Regular',
    restriction_notes: null, notes: null, status: 'active',
    is_npo: false, npo_label: null, npo_reason: null, npo_until: null,
    npo_until_iso: null, npo_hours_remaining: null, npo_expired: false,
    npo_end_unknown: false, npo_long_window: false, npo_notice: null,
    needs_review: false,
    ordered_by: 'D001', ordered_by_name: 'Dr. Mehta', ordered_by_display: 'Dr. Mehta',
    ordered_at: '2026-08-12 06:00:00', ordered_at_iso: '2026-08-12T06:00:00+00:00',
    supersedes_order_id: null, discontinued_by: null, discontinued_at: null,
    discontinue_reason: null,
    allergies: [], allergies_unavailable: false, allergies_message: null,
    allergy_source: 'patient_allergies',
    ...over,
  }
}

function npoOrder(over: Record<string, unknown> = {}) {
  return order({
    id: 2, patient_id: 'P2', patient_name: 'Vikram Shah', bed: '7',
    diet_type: 'npo', diet_label: 'Nil by mouth (NPO)',
    is_npo: true, npo_label: 'NIL BY MOUTH',
    npo_reason: 'for OT at 08:00',
    npo_until: '2026-08-12 08:00:00', npo_until_iso: '2026-08-12T08:00:00+00:00',
    npo_hours_remaining: 5.2, npo_expired: false,
    ordered_by: 'D009', ordered_by_name: 'Dr. Iyer', ordered_by_display: 'Dr. Iyer',
    ...over,
  })
}

function wardView(over: Record<string, unknown> = {}) {
  const rows = (over.rows as unknown[]) ?? [order(), npoOrder()]
  const npoRows = rows.filter((r) => (r as { is_npo: boolean }).is_npo)
  return {
    ward: 'W1', department: 'Medicine',
    rows,
    npo_rows: npoRows,
    npo_count: npoRows.length,
    npo_needs_review: npoRows.filter((r) => (r as { needs_review: boolean }).needs_review),
    npo_needs_review_count: npoRows.filter(
      (r) => (r as { needs_review: boolean }).needs_review,
    ).length,
    meal_count: rows.length - npoRows.length,
    order_count: rows.length,
    unordered: [], unordered_count: 0,
    counts: [],
    allergies_unavailable: false, allergies_message: null,
    allergy_source: 'patient_allergies',
    npo_label: 'NIL BY MOUTH',
    npo_expiry_notice: META.npo_expiry_notice,
    ...over,
  }
}

beforeEach(() => {
  vi.mocked(api.getMeta).mockResolvedValue(META as never)
  vi.mocked(api.getSummary).mockResolvedValue(SUMMARY as never)
  vi.mocked(api.getWards).mockResolvedValue([
    { ward: 'W1', admitted: 2, with_orders: 2, without_orders: 0, npo: 1 },
  ] as never)
  vi.mocked(api.getWardView).mockResolvedValue(wardView() as never)
  vi.mocked(api.getKitchenBoard).mockResolvedValue({
    service_date: '2026-08-12', meal_services: ['lunch'], wards: [],
    hospital_total_meals: 0, hospital_npo_count: 0, plans: [], contains_phi: false,
  } as never)
  vi.mocked(api.getMealPlans).mockResolvedValue([] as never)
  vi.mocked(api.getIndents).mockResolvedValue([] as never)
  vi.mocked(api.getStaleOrders).mockResolvedValue([] as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

/* ══════════════════════════════════════════════════════════════════════════
   1. NIL BY MOUTH
   ══════════════════════════════════════════════════════════════════════════ */

describe('nil by mouth', () => {
  it('renders NPO in its own block ABOVE the diet table, not as a row inside it', async () => {
    render(<DietaryPanel />)

    const npoHeading = await screen.findByRole('heading', { name: /NIL BY MOUTH: 1 patient/i })
    const npoSection = npoHeading.closest('section')!
    expect(within(npoSection).getByText('Vikram Shah', { exact: false })).toBeTruthy()

    const dietHeading = await screen.findByRole('heading', { name: /Diets on this ward/i })
    const dietSection = dietHeading.closest('section')!
    // The NPO patient must NOT appear in the ordinary diet table — that is the
    // exact failure this whole layout exists to prevent.
    expect(within(dietSection).queryByText(/Vikram Shah/)).toBeNull()
    expect(within(dietSection).getByText('Asha Rao')).toBeTruthy()

    // And the NPO block comes FIRST in document order.
    expect(npoSection.compareDocumentPosition(dietSection))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  it('names the ordering clinician and the end time on every NPO card', async () => {
    render(<DietaryPanel />)
    const heading = await screen.findByRole('heading', { name: /NIL BY MOUTH: 1 patient/i })
    const section = heading.closest('section')!

    // The words, not only a colour.
    expect(within(section).getAllByText('NIL BY MOUTH').length).toBeGreaterThan(0)
    // WHO said this patient cannot eat.
    expect(within(section).getByText('Dr. Iyer')).toBeTruthy()
    expect(within(section).getByText(/Ordered by/i)).toBeTruthy()
    // UNTIL WHEN, and WHY.
    expect(within(section).getByText(/Ends in 5\.2 h/)).toBeTruthy()
    expect(within(section).getByText('for OT at 08:00')).toBeTruthy()
  })

  it('an expired NPO window says "review now" and the patient stays NPO', async () => {
    const expired = npoOrder({
      npo_expired: true, needs_review: true, npo_hours_remaining: -3.2,
      npo_notice: META.npo_expiry_notice,
    })
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({ rows: [order(), expired] }) as never,
    )

    render(<DietaryPanel />)
    const heading = await screen.findByRole('heading', { name: /NIL BY MOUTH: 1 patient/i })
    const section = heading.closest('section')!

    expect(within(section).getByText(/Window passed: review now/i)).toBeTruthy()
    expect(within(section).getByText(/NOT resumed automatically/i)).toBeTruthy()
    // Still NPO: not moved into the fed list.
    const dietSection = screen.getByRole('heading', { name: /Diets on this ward/i })
      .closest('section')!
    expect(within(dietSection).queryByText(/Vikram Shah/)).toBeNull()
    // And never a raw negative duration for somebody to interpret.
    expect(within(section).queryByText(/-3\.2/)).toBeNull()
  })

  it('npoWindowLabel never renders a negative duration', () => {
    expect(npoWindowLabel({
      npo_expired: true, npo_end_unknown: false, npo_hours_remaining: -3.2,
    })).toBe('Window passed: review now')
    expect(npoWindowLabel({
      npo_expired: false, npo_end_unknown: true, npo_hours_remaining: null,
    })).toBe('End time unreadable: review now')
    expect(npoWindowLabel({
      npo_expired: false, npo_end_unknown: false, npo_hours_remaining: 0.5,
    })).toBe('Ends in 30 min')
    expect(npoWindowLabel({
      npo_expired: false, npo_end_unknown: false, npo_hours_remaining: 6,
    })).toBe('Ends in 6.0 h')
  })

  it('the NPO behaviour notice is on screen before any network answer', async () => {
    // The assertion runs with every fetch STILL PENDING — that is the point.
    render(<DietaryPanel />)
    expect(screen.getByText(/does NOT resume the diet automatically/i)).toBeTruthy()
    // Then let the pending promises settle inside act, so this test does not
    // leak un-acted state updates into the next one's output.
    await waitFor(() => expect(api.getSummary).toHaveBeenCalled())
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   2. ALLERGIES
   ══════════════════════════════════════════════════════════════════════════ */

describe('allergies', () => {
  it('an unreadable allergy list is NOT rendered as "no allergies recorded"', async () => {
    const blind = order({
      allergies: [], allergies_unavailable: true,
      allergies_message: META.allergy_unavailable_message,
    })
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({
        rows: [blind],
        allergies_unavailable: true,
        allergies_message: META.allergy_unavailable_message,
      }) as never,
    )

    render(<DietaryPanel />)

    await waitFor(() => {
      expect(screen.getAllByText('ALLERGIES UNKNOWN').length).toBeGreaterThan(0)
    })
    expect(screen.getAllByText(/Do not assume the patient has no allergies/i).length)
      .toBeGreaterThan(0)
    expect(screen.queryByText('No allergies recorded')).toBeNull()
  })

  it('renders a recorded allergy with its substance and reaction', async () => {
    const allergic = order({
      allergies: [
        { id: 9, substance: 'peanut', substance_class: null, reaction: 'anaphylaxis',
          severity: 'severe' },
      ],
    })
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({ rows: [allergic] }) as never,
    )

    render(<DietaryPanel />)
    await waitFor(() => expect(screen.getAllByText('ALLERGIES').length).toBeGreaterThan(0))
    expect(screen.getByText(/peanut/)).toBeTruthy()
    expect(screen.getByText(/anaphylaxis/)).toBeTruthy()
  })

  it('an empty list reads as "no allergies recorded" only when the read succeeded', async () => {
    vi.mocked(api.getWardView).mockResolvedValue(wardView({ rows: [order()] }) as never)
    render(<DietaryPanel />)
    await waitFor(() => expect(screen.getAllByText('No allergies recorded').length).toBe(1))
    expect(screen.queryByText('ALLERGIES UNKNOWN')).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   3. THE COUNT
   ══════════════════════════════════════════════════════════════════════════ */

describe('the kitchen count', () => {
  it('sumLines agrees with a plan total, and a disagreement is shown not hidden', async () => {
    const lines = [
      { id: 1, meal_plan_id: 5, line_no: 1, diet_type: 'diabetic', diet_label: 'Diabetic',
        texture: 'regular', texture_label: 'Regular', meal_count: 6 },
      { id: 2, meal_plan_id: 5, line_no: 2, diet_type: 'renal', diet_label: 'Renal',
        texture: 'regular', texture_label: 'Regular', meal_count: 3 },
    ]
    expect(sumLines(lines)).toBe(9)

    // A plan whose stored total does NOT equal its lines. The panel must refuse
    // to pick one of the two numbers.
    vi.mocked(api.getMealPlans).mockResolvedValue([{
      id: 5, service_date: '2026-08-12', meal_service: 'lunch', meal_service_label: 'Lunch',
      ward: 'W1', status: 'draft', total_meals: 11, npo_count: 0, order_count: 9,
      lines, lines_total: 9, totals_agree: false,
      generated_by: 'D001', generated_at: null, locked_by: null, locked_at: null,
      notes: null, indent: null,
    }] as never)

    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plans & indents' }))

    await waitFor(() => {
      expect(screen.getByText(/This count does not add up/i)).toBeTruthy()
    })
    expect(screen.getByText(/the lines total 9 but the plan says 11/i)).toBeTruthy()
    expect(screen.getByText(/Do not cook against it/i)).toBeTruthy()
  })

  it('the kitchen view says out loud that it carries no patient identifiers', async () => {
    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Kitchen' }))
    await waitFor(() => {
      expect(screen.getByText(/no patient names, beds or allergies/i)).toBeTruthy()
    })
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   4. THE MODULE SWITCH
   ══════════════════════════════════════════════════════════════════════════ */

describe('the module switch', () => {
  it('a disabled module reads as "not available", never as an empty ward', async () => {
    vi.mocked(api.getSummary).mockRejectedValue(
      new DietaryApiError(
        'The diet & kitchen module is not enabled for this deployment.',
        404, 'module_disabled',
      ),
    )
    vi.mocked(api.getWards).mockRejectedValue(
      new DietaryApiError('off', 404, 'module_disabled'),
    )

    render(<DietaryPanel />)

    await waitFor(() => {
      expect(screen.getByText(/not enabled for this deployment/i)).toBeTruthy()
    })
    expect(screen.getByText('Not available at this site.')).toBeTruthy()
    // No ward furniture at all — an empty diet list is a statement about a ward.
    expect(screen.queryByRole('heading', { name: /Diets on this ward/i })).toBeNull()
  })

  it('an ordinary failure is an error, not a disabled module', async () => {
    vi.mocked(api.getSummary).mockRejectedValue(new Error('network down'))
    render(<DietaryPanel />)
    await waitFor(() => expect(screen.getByText('network down')).toBeTruthy())
    expect(screen.queryByText('Not available at this site.')).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   5. WRITING AN ORDER
   ══════════════════════════════════════════════════════════════════════════ */

describe('writing a diet order', () => {
  it('refuses an NPO order with no end time, at the field', async () => {
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({
        rows: [],
        unordered: [{
          patient_id: 'P9', patient_name: 'Meera Nair', patient_mrn: 'MRN-9',
          ward: 'W1', bed: '2', department: 'Medicine',
        }],
        unordered_count: 1,
      }) as never,
    )

    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Write diet order' }))

    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Diet type'), { target: { value: 'npo' } })
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'for OT' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /Write diet order/i }))

    await waitFor(() => {
      expect(within(dialog).getByText(/needs an end time/i)).toBeTruthy()
    })
    expect(api.saveDietOrder).not.toHaveBeenCalled()
  })

  it('sends a complete NPO order through', async () => {
    vi.mocked(api.saveDietOrder).mockResolvedValue(npoOrder() as never)
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({
        rows: [],
        unordered: [{
          patient_id: 'P9', patient_name: 'Meera Nair', patient_mrn: 'MRN-9',
          ward: 'W1', bed: '2', department: 'Medicine',
        }],
        unordered_count: 1,
      }) as never,
    )

    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Write diet order' }))
    const dialog = await screen.findByRole('dialog')

    fireEvent.change(within(dialog).getByLabelText('Diet type'), { target: { value: 'npo' } })
    fireEvent.change(within(dialog).getByLabelText('Reason'), {
      target: { value: 'for OT 08:00' },
    })
    // Regex, not an exact string: the label element also contains the field's
    // hint sentence, and getByLabelText matches the label's WHOLE text.
    fireEvent.change(within(dialog).getByLabelText(/Nil by mouth until/), {
      target: { value: '2026-08-13T08:00' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /Write diet order/i }))

    await waitFor(() => expect(api.saveDietOrder).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.saveDietOrder).mock.calls[0][0]).toMatchObject({
      patient_id: 'P9',
      diet_type: 'npo',
      npo_reason: 'for OT 08:00',
      // Sent as the local string the clinician typed. Converting to UTC in the
      // browser would be a second timezone opinion on top of the server's.
      npo_until: '2026-08-13T08:00',
    })
  })

  it('closes the order drawer on Escape (shared focus trap)', async () => {
    vi.mocked(api.getWardView).mockResolvedValue(
      wardView({
        rows: [],
        unordered: [{
          patient_id: 'P9', patient_name: 'Meera Nair', patient_mrn: 'MRN-9',
          ward: 'W1', bed: '2', department: 'Medicine',
        }],
        unordered_count: 1,
      }) as never,
    )
    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Write diet order' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.keyDown(within(dialog).getByLabelText('Diet type'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   6. SCOPE, FILTERS, LOADING AND WHAT THIS ACCOUNT MAY DO
   ══════════════════════════════════════════════════════════════════════════ */

describe('scope, filters and capabilities', () => {
  it('captions the tiles with the wards they count', async () => {
    vi.mocked(api.getSummary).mockResolvedValue({
      ...SUMMARY,
      scope: { department: 'General Medicine', hospital_wide: false, wards: ['GM-1'] },
    } as never)
    render(<DietaryPanel />)
    expect(await screen.findByText(/Your wards: GM-1\. The Kitchen tab counts every ward\./))
      .toBeTruthy()
  })

  it('explains an empty ward scope instead of showing an empty ward', async () => {
    vi.mocked(api.getSummary).mockResolvedValue({
      ...SUMMARY, active_orders: 0, meals: 0, npo_active: 0,
      scope: { department: 'Anesthesiology', hospital_wide: false, wards: [] },
    } as never)
    vi.mocked(api.getWards).mockResolvedValue([] as never)
    vi.mocked(api.getWardView).mockResolvedValue(wardView({ rows: [] }) as never)

    render(<DietaryPanel />)
    expect(await screen.findByText('No ward to show')).toBeTruthy()
    expect(screen.getAllByText(/nobody is admitted under Anesthesiology/i).length)
      .toBeGreaterThan(0)
    // Not the reassuring sentences about a ward that was never looked at.
    expect(screen.queryByText(/is being fed/i)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Plans & indents' }))
    expect(await screen.findByText('No ward to count')).toBeTruthy()
    expect(api.getMealPlans).not.toHaveBeenCalled()
  })

  it('lists plans and indents for the ward, the service and the hospital\'s day', async () => {
    vi.mocked(api.getMeta).mockResolvedValue({
      ...META, service_date: '2026-10-03', current_meal_service: 'dinner',
    } as never)
    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plans & indents' }))

    const today = { ward: 'W1', meal_service: 'dinner', service_date: '2026-10-03' }
    await waitFor(() => expect(api.getMealPlans).toHaveBeenLastCalledWith(today))
    expect(api.getIndents).toHaveBeenLastCalledWith(today)

    fireEvent.click(screen.getByRole('button', { name: 'Show earlier days' }))
    const anyDay = { ward: 'W1', meal_service: 'dinner', service_date: undefined }
    await waitFor(() => expect(api.getMealPlans).toHaveBeenLastCalledWith(anyDay))
    expect(api.getIndents).toHaveBeenLastCalledWith(anyDay)
  })

  it('mealServiceForHour matches the server: breakfast to 10:00, lunch to 15:00', () => {
    expect([2, 9, 10, 14, 15, 23].map(mealServiceForHour))
      .toEqual(['breakfast', 'breakfast', 'lunch', 'lunch', 'dinner', 'dinner'])
  })

  it('opens the kitchen on the service the hospital is counting for now', async () => {
    vi.mocked(api.getMeta).mockResolvedValue({
      ...META, service_date: '2026-10-03', current_meal_service: 'breakfast',
    } as never)
    render(<DietaryPanel />)
    await waitFor(() => expect(api.getMeta).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: 'Kitchen' }))
    await waitFor(() => expect(api.getKitchenBoard).toHaveBeenLastCalledWith({
      meal_service: 'breakfast',
    }))
  })

  it('says Loading, not "No meal plans yet", before the plans have arrived', async () => {
    vi.mocked(api.getMealPlans).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getIndents).mockReturnValue(new Promise(() => {}) as never)
    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plans & indents' }))
    expect(await screen.findByText('Loading meal plans…')).toBeTruthy()
    expect(screen.getByText('Loading kitchen indents…')).toBeTruthy()
    expect(screen.queryByText(/No meal plans yet|No count for/)).toBeNull()
    expect(screen.queryByText('No indents raised')).toBeNull()
  })

  it('an account that cannot write diet orders is not offered Change or Write', async () => {
    vi.mocked(api.getMeta).mockResolvedValue({
      ...META, capabilities: { write_orders: false, run_kitchen: true },
    } as never)
    vi.mocked(api.getWardView).mockResolvedValue(wardView({
      unordered: [{
        patient_id: 'P9', patient_name: 'Meera Nair', patient_mrn: 'MRN-9',
        ward: 'W1', bed: '2', department: 'Medicine',
      }],
      unordered_count: 1,
    }) as never)

    render(<DietaryPanel />)
    expect(await screen.findByText(/This account can read them\./)).toBeTruthy()
    expect(screen.getByText('Asha Rao')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Write diet order' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Review \/ change diet/ })).toBeNull()
  })

  it('an account that cannot run the kitchen is not offered Issue or Cancel', async () => {
    vi.mocked(api.getMeta).mockResolvedValue({
      ...META, capabilities: { write_orders: false, run_kitchen: false },
    } as never)
    vi.mocked(api.getIndents).mockResolvedValue([{
      id: 4, indent_number: null, meal_plan_id: 5, ward: 'W1', meal_service: 'lunch',
      meal_service_label: 'Lunch', service_date: '2026-10-03', status: 'draft',
      total_meals: 9, lines: [], number_series_is_configured: true,
    }] as never)

    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Plans & indents' }))
    expect(await screen.findByText(/Draft #4/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Issue' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Build count/ })).toBeNull()
    expect(screen.getByText(/cannot build, lock, issue or cancel/)).toBeTruthy()
  })

  it('keeps the order drawer open, with what was typed, when the server refuses', async () => {
    vi.mocked(api.saveDietOrder).mockRejectedValue(
      new DietaryApiError('Patient is not on a ward in your scope.', 404, 'not_found'),
    )
    vi.mocked(api.getWardView).mockResolvedValue(wardView({
      rows: [],
      unordered: [{
        patient_id: 'P9', patient_name: 'Meera Nair', patient_mrn: 'MRN-9',
        ward: 'W1', bed: '2', department: 'Medicine',
      }],
      unordered_count: 1,
    }) as never)

    render(<DietaryPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Write diet order' }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Notes for the kitchen'), {
      target: { value: 'no onion' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /Write diet order/i }))

    await waitFor(() => expect(api.saveDietOrder).toHaveBeenCalledTimes(1))
    expect(await within(dialog).findByText('Patient is not on a ward in your scope.'))
      .toBeTruthy()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect((within(dialog).getByLabelText('Notes for the kitchen') as HTMLInputElement).value)
      .toBe('no onion')
  })
})
