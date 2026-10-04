/**
 * Four invariants of the Surgical Suite:
 *
 *   1. PROVENANCE. The Tier-3 prep package with ai_package_status 'generated'
 *      is a deterministic template — surgery_escalation_service.py performs NO
 *      model call on that path. The badge must say so ('Standard template'),
 *      and must never claim 'AI Generated'. Fabricated AI provenance on a
 *      clinical surface is the defect this file exists to keep dead.
 *
 *   2. LAYOUT (DEMO_READINESS §10.4). A week-view day with zero surgeries is
 *      marked with the --empty modifier so it does not hold the full 480px
 *      card height as blank space.
 *
 *   3. TODAY. The week view marks the current day with a badge and a card
 *      modifier, both driven by `is_today` from GET /api/surgery/schedule. The
 *      route never sent the field, so both were unreachable: every card in the
 *      week looked alike and a surgeon had to read dates to find the day they
 *      were operating. The flag is decided server-side in the HOSPITAL's
 *      timezone and must be rendered as sent — recomputing it from `date` here
 *      would put the badge on the wrong card for anyone whose browser is in a
 *      different zone from the ward.
 *
 *   4. A FAILED PACKAGE STILL OPENS THE CASE. A failed prep package used to
 *      raise a "Feature under development" modal instead of the case, hiding
 *      the patient and the WHO Surgical Safety Checklist from the team about
 *      to operate. The case opens; the risk section says the assessment is
 *      unavailable; no success estimate is drawn when there is none.
 */
import { render, screen, waitFor, act } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getSurgerySchedule = vi.fn()
const getSurgeryDetail = vi.fn()
const getEmergencyQueue = vi.fn()
const getSurgeryCases = vi.fn()
const getSurgeryRiskAssessment = vi.fn()

vi.mock('../../api/client', () => ({
  getSurgerySchedule: (...a: unknown[]) => getSurgerySchedule(...a),
  getSurgeryDetail: (...a: unknown[]) => getSurgeryDetail(...a),
  getSurgeryRiskAssessment: (...a: unknown[]) => getSurgeryRiskAssessment(...a),
  prepForSurgery: vi.fn(),
  getEmergencyQueue: (...a: unknown[]) => getEmergencyQueue(...a),
  getSurgeryCases: (...a: unknown[]) => getSurgeryCases(...a),
  alertSurgicalTeam: vi.fn(),
  startSurgery: vi.fn(),
  completeSurgery: vi.fn(),
  getUser: () => ({ id: 'DOC004', name: 'Dr Meera Pillai', role: 'doctor' }),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

// The WHO checklist has its own test file; here it would only add network
// noise. A marker is enough to prove the case view mounted it.
vi.mock('../../components/surgery/WhoSurgicalChecklist', () => ({
  default: () => <div data-testid="who-checklist" />,
}))

import SurgicalSuitePanel from './SurgicalSuitePanel'

const EMERGENCY = {
  id: 'SURG-EM-1',
  patient_name: 'Ravi Sharma',
  bed: '12',
  procedure_name: 'Exploratory Laparotomy',
  diagnosis: 'Perforated viscus',
  estimated_duration_min: 120,
  status: 'emergency_ready',
}

const DETAIL = {
  id: 'SURG-EM-1',
  patient_id: 'P001',
  patient_name: 'Ravi Sharma',
  age: 54,
  sex: 'M',
  mrn: 'MRN-1',
  bed: '12',
  patient_diagnosis: 'Sepsis',
  procedure_name: 'Exploratory Laparotomy',
  diagnosis: 'Perforated viscus',
  scheduled_date: '2026-08-10',
  scheduled_time: '09:00',
  estimated_duration_min: 120,
  operating_room: 'OR-E (Emergency)',
  status: 'emergency_ready',
  co_morbidities: [],
  allergies: [],
  surgical_checklist: ['Step one'],
  instruments_required: [],
  staff_required: [],
  risk_assessment: 'Static template risk text.',
  possible_complications: [],
  success_probability: 90,
  risk_level: 'critical',
  risk_score: 8,
  risk_drivers: [],
  notes: JSON.stringify({ ai_package_status: 'generated' }),
}

beforeEach(() => {
  getSurgerySchedule.mockReset().mockResolvedValue([])
  getSurgeryDetail.mockReset().mockResolvedValue(DETAIL)
  getEmergencyQueue.mockReset().mockResolvedValue([])
  getSurgeryCases.mockReset().mockResolvedValue([])
  getSurgeryRiskAssessment.mockReset().mockResolvedValue('')
})

describe('prep package provenance badge', () => {
  it("labels the deterministic package 'Standard template', never 'AI Generated'", async () => {
    getEmergencyQueue.mockResolvedValue([EMERGENCY])
    render(<SurgicalSuitePanel />)

    const card = await screen.findByText('Exploratory Laparotomy')
    await act(async () => { card.closest('button')!.click() })

    expect(await screen.findByText('Standard template')).toBeInTheDocument()
    expect(screen.queryByText(/AI Generated/i)).toBeNull()
  })
})

describe('failed prep package (F017)', () => {
  it('opens the case and its WHO checklist, saying the assessment is unavailable', async () => {
    getEmergencyQueue.mockResolvedValue([EMERGENCY])
    getSurgeryDetail.mockResolvedValue({
      ...DETAIL,
      success_probability: null,
      risk_assessment: 'Assessment unavailable. Complete the pre-operative risk assessment clinically.',
      notes: JSON.stringify({ ai_package_status: 'failed', reason: 'Error: boom' }),
    })
    getSurgeryRiskAssessment.mockRejectedValue(new Error('model offline'))
    render(<SurgicalSuitePanel />)

    const card = await screen.findByText('Exploratory Laparotomy')
    await act(async () => { card.closest('button')!.click() })

    expect(await screen.findByTestId('who-checklist')).toBeInTheDocument()
    expect(screen.queryByText(/Feature under development/i)).toBeNull()
    expect(screen.getAllByText(/Assessment unavailable/i).length).toBeGreaterThan(0)
    // No estimate means no ring and no percentage.
    expect(document.querySelector('.suite__detail-success-gauge')).toBeNull()
  })

  it("badges the 'template' package as a standard template", async () => {
    getEmergencyQueue.mockResolvedValue([EMERGENCY])
    getSurgeryDetail.mockResolvedValue({
      ...DETAIL, notes: JSON.stringify({ ai_package_status: 'template' }),
    })
    render(<SurgicalSuitePanel />)

    const card = await screen.findByText('Exploratory Laparotomy')
    await act(async () => { card.closest('button')!.click() })

    expect(await screen.findByText('Standard template')).toBeInTheDocument()
  })
})

describe('week view empty days (§10.4)', () => {
  it('marks a zero-surgery day card with the --empty modifier', async () => {
    getSurgerySchedule.mockResolvedValue([
      { date: '2026-08-10', day_label: 'Monday', surgeries: [] },
      {
        date: '2026-08-11', day_label: 'Tuesday',
        surgeries: [{
          id: 'S2', patient_name: 'A', operating_room: 'OR-1', procedure_name: 'Proc',
          scheduled_time: '10:00', estimated_duration_min: 60, risk_level: 'stable',
        }],
      },
    ])
    const { container } = render(<SurgicalSuitePanel />)

    await waitFor(() =>
      expect(container.querySelectorAll('.suite__day-card').length).toBe(2))
    const [monday, tuesday] = Array.from(container.querySelectorAll('.suite__day-card'))
    expect(monday.className).toContain('suite__day-card--empty')
    expect(tuesday.className).not.toContain('suite__day-card--empty')
  })
})

describe('week view Today badge', () => {
  it('badges exactly the day the server marked is_today', async () => {
    // The dates are deliberately NOT the browser's today: the server decides
    // this in the hospital's zone, and the panel must not second-guess it.
    getSurgerySchedule.mockResolvedValue([
      { date: '2027-03-15', day_label: 'Monday', is_today: true, surgeries: [] },
      { date: '2027-03-16', day_label: 'Tuesday', is_today: false, surgeries: [] },
    ])
    const { container } = render(<SurgicalSuitePanel />)

    await waitFor(() =>
      expect(container.querySelectorAll('.suite__day-card').length).toBe(2))
    expect(await screen.findByText('Today')).toBeInTheDocument()
    expect(container.querySelectorAll('.suite__today-badge')).toHaveLength(1)

    const [today, tomorrow] = Array.from(container.querySelectorAll('.suite__day-card'))
    expect(today.className).toContain('suite__day-card--today')
    expect(tomorrow.className).not.toContain('suite__day-card--today')
  })

  it('badges nothing when no day in the window is today', async () => {
    getSurgerySchedule.mockResolvedValue([
      { date: '2027-03-16', day_label: 'Tuesday', is_today: false, surgeries: [] },
    ])
    const { container } = render(<SurgicalSuitePanel />)

    await waitFor(() =>
      expect(container.querySelectorAll('.suite__day-card').length).toBe(1))
    expect(container.querySelectorAll('.suite__today-badge')).toHaveLength(0)
  })
})

describe('case header bed', () => {
  // GET /surgery/{id} used to select no bed at all, so every header read
  // "Bed undefined". The route sends it now; a patient with no bed sends null,
  // and the header then leaves the bed out rather than printing an empty one.
  async function openCase() {
    getEmergencyQueue.mockResolvedValue([EMERGENCY])
    render(<SurgicalSuitePanel />)
    const card = await screen.findByText('Exploratory Laparotomy')
    await act(async () => { card.closest('button')!.click() })
    return screen.findByText(/MRN: MRN-1/)
  }

  it('shows the bed the route sends', async () => {
    const sub = await openCase()
    expect(sub.textContent).toBe('54y · M · MRN: MRN-1 · Bed 12')
  })

  it('leaves the bed out when the patient has none', async () => {
    getSurgeryDetail.mockResolvedValue({ ...DETAIL, bed: null })
    const sub = await openCase()
    expect(sub.textContent).toBe('54y · M · MRN: MRN-1')
  })

  it('leaves the bed out of an emergency row with no bed', async () => {
    getEmergencyQueue.mockResolvedValue([{ ...EMERGENCY, bed: null }])
    render(<SurgicalSuitePanel />)
    expect(await screen.findByText('Ravi Sharma')).toBeInTheDocument()
  })
})
