/**
 * Sage Provenance incident-review page — read-only, evidence-at-the-time.
 *
 * What this file defends:
 *   A  the listing renders the incident-review columns: time, user, dept,
 *      patient-or-'—', model, safety-floor TRI-STATE, retrieval count.
 *   B  the tri-state is honest: a legacy row ('unknown') is labelled as
 *      legacy/unknown — never rendered as "Did not fire", which is the exact
 *      re-badging the backend refuses to do.
 *   C  a patient-less turn shows '—' for patient and still carries its
 *      department (the scope it is read under).
 *   D  opening a turn fetches the REPLAY endpoint (the audited read) and shows
 *      the stored question and answer; Back returns to the list without
 *      refetching the replay.
 */
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

const apiFetch = vi.fn()
vi.mock('../../../api/client', () => ({
  apiFetch: (path: string) => apiFetch(path),
}))

import ProvenanceReview from './ProvenanceReview'

const baseTurn = {
  lane: 'chat',
  outcome: 'answered',
  actor_role: 'doctor',
  hospital_wide: false,
  patient_context_used: true,
  query_scope: 'PATIENT_SPECIFIC',
  provider: 'ollama',
  thinking: true,
  safety_floor: false,
  safety_floor_unsatisfied: false,
  citation_basis: 'model_cited',
  confidence: 'high',
  escalation: false,
  refusal_class: null,
  retrieval_ms: 800,
  ttft_ms: 2100,
  total_ms: 6400,
}

const TURNS = [
  {
    ...baseTurn,
    turn_id: 'turnfired0001',
    occurred_at: '2026-08-10T09:15:00+00:00',
    actor_user_id: 'DOC-7',
    department: 'General Medicine',
    patient_id: 'P-101',
    model_served: 'medgemma:27b',
    safety_floor: true,
    safety_floor_state: 'fired',
    retrieved_doc_count: 3,
  },
  {
    // Patient-less ward question: no patient anchor, still owned by a dept.
    ...baseTurn,
    turn_id: 'turnnopat0002',
    occurred_at: '2026-08-10T08:40:00+00:00',
    actor_user_id: 'NUR-3',
    department: 'Cardiology',
    patient_id: null,
    patient_context_used: false,
    model_served: 'voice-4b',
    thinking: null,
    safety_floor_state: 'not_evaluated',
    retrieved_doc_count: 0,
  },
  {
    // A row recorded before the tri-state column existed.
    ...baseTurn,
    turn_id: 'turnlegacy003',
    occurred_at: '2026-08-01T11:00:00+00:00',
    actor_user_id: 'DOC-7',
    department: 'General Medicine',
    patient_id: 'P-102',
    model_served: 'medgemma:27b',
    safety_floor_state: 'unknown',
    retrieved_doc_count: 2,
  },
]

const REPLAY = {
  turn_id: 'turnfired0001',
  occurred_at: '2026-08-10T09:15:00+00:00',
  lane: 'chat',
  outcome: 'answered',
  actor: { user_id: 'DOC-7', role: 'doctor' },
  scope: {
    department: 'General Medicine', hospital_wide: false, patient_id: 'P-101',
    patient_context_used: true, query_scope: 'PATIENT_SPECIFIC',
  },
  question: 'What antibiotic cover does this patient need?',
  patient_context: { patient_id: 'P-101', medications: 'Co-amoxiclav 1.2 g IV TDS' },
  guidelines: [
    { document_id: 'EXT-SEPSIS-004', title: 'Sepsis bundle', source: 'Local Policy', passage: 'Administer the bundle within one hour.' },
  ],
  model: {
    requested: 'medgemma:27b', served: 'medgemma:27b', provider: 'ollama',
    thinking: true, thinking_requested: true, thinking_tier: 'careful',
    thinking_reason: 'safety_floor', safety_floor: true,
    safety_floor_rules: ['dosing'], safety_floor_unsatisfied: false,
    safety_floor_state: 'fired',
  },
  answer: {
    text: 'Continue co-amoxiclav; review at 48 h.',
    cited_document_ids: ['EXT-SEPSIS-004'], citation_basis: 'model_cited',
    confidence: 'high', escalation: false, action_status: null, refusal_class: null,
  },
  raw_model_output: 'NOACT| Continue co-amoxiclav; review at 48 h.',
  retrieved_document_ids: ['EXT-SEPSIS-004'],
  timing: { retrieval_ms: 800, ttft_ms: 2100, total_ms: 6400 },
  integrity: { prompt_sha256: 'ab'.repeat(32), schema_v: 2, missing_blobs: [] },
}

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/sage/provenance?')) {
      return { turns: TURNS, count: TURNS.length, scope: { department: null, hospital_wide: true } }
    }
    if (path === '/sage/provenance/turnfired0001') return REPLAY
    throw new Error(`unexpected apiFetch: ${path}`)
  })
})

afterEach(() => cleanup())

describe('ProvenanceReview listing', () => {
  it('renders the incident-review columns for every recorded turn', async () => {
    render(<ProvenanceReview />)
    await waitFor(() => expect(screen.getByText('turnfired0001')).toBeInTheDocument())

    // A: user, dept, model, retrieval count
    expect(screen.getAllByText('DOC-7').length).toBeGreaterThan(0)
    expect(screen.getAllByText('General Medicine').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/medgemma:27b \+thinking/).length).toBeGreaterThan(0)
    expect(screen.getByText('3 docs')).toBeInTheDocument()

    // The safety-floor tri-state, spelled out — three different truths.
    expect(screen.getByText('Fired')).toBeInTheDocument()
    expect(screen.getByText('Not evaluated')).toBeInTheDocument()
  })

  it('labels a legacy row unknown — never as a verdict', async () => {
    render(<ProvenanceReview />)
    await waitFor(() => expect(screen.getByText('turnlegacy003')).toBeInTheDocument())

    // B: the legacy row is honest about being unknown...
    expect(screen.getByText('Unknown (legacy)')).toBeInTheDocument()
    // ...and is not silently re-badged as an evaluation that passed.
    expect(screen.queryByText('Did not fire')).not.toBeInTheDocument()
  })

  it("shows '—' for a patient-less turn while keeping its owning department", async () => {
    render(<ProvenanceReview />)
    await waitFor(() => expect(screen.getByText('turnnopat0002')).toBeInTheDocument())

    // C: no patient anchor, but the scope it is read under is visible.
    expect(screen.getByText('Cardiology')).toBeInTheDocument()
    const row = screen.getByText('turnnopat0002').closest('tr')!
    expect(row).toHaveTextContent('—')
    expect(row).toHaveTextContent('Cardiology')
  })
})

describe('ProvenanceReview detail', () => {
  it('opens a turn via the audited replay endpoint and shows the stored evidence', async () => {
    render(<ProvenanceReview />)
    await waitFor(() => expect(screen.getByText('turnfired0001')).toBeInTheDocument())

    fireEvent.click(screen.getByText('turnfired0001'))
    await waitFor(() =>
      expect(screen.getByText('What antibiotic cover does this patient need?')).toBeInTheDocument())

    // D: the replay endpoint (the read that writes the phi_access audit row)
    expect(apiFetch).toHaveBeenCalledWith('/sage/provenance/turnfired0001')
    // Stored evidence, not a live re-read.
    expect(screen.getByText('Continue co-amoxiclav; review at 48 h.')).toBeInTheDocument()
    expect(screen.getByText('NOACT| Continue co-amoxiclav; review at 48 h.')).toBeInTheDocument()
    expect(screen.getByText('Sepsis bundle')).toBeInTheDocument()
    expect(screen.getByText('none, record is complete')).toBeInTheDocument()

    // Back returns to the listing without re-fetching the replay.
    const replayCalls = apiFetch.mock.calls.filter(c => c[0] === '/sage/provenance/turnfired0001').length
    fireEvent.click(screen.getByText('All turns'))
    await waitFor(() => expect(screen.getByText('turnnopat0002')).toBeInTheDocument())
    expect(apiFetch.mock.calls.filter(c => c[0] === '/sage/provenance/turnfired0001').length)
      .toBe(replayCalls)
  })
})
