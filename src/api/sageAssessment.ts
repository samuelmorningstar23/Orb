/** Sage's DRAFTED ASSESSMENT behind a critical Vigil alert — the read/verdict client.
 *
 *  Its own module rather than another block in client.ts for the same reason the
 *  server gave these their own routes: an assessment is not an action card. A
 *  card is a thing Orb could DO and its transition endpoint dispatches a
 *  downstream handoff; an assessment is a four-section document a clinician
 *  agrees or disagrees with, and `transitionAssessment` below **executes
 *  nothing**. Sharing a file with `transitionSageAction` is how, six months from
 *  now, somebody copies the wrong one.
 *
 *  THE TYPES ARE THE SERVER'S RENDERER, FIELD FOR FIELD.
 *  `sage_assessment_service.render()` is the one payload shape — the list
 *  endpoint, the single fetch and any later surface all go through it — so this
 *  file mirrors it and invents nothing. In particular:
 *
 *    - `unavailable_message` is the server's wording, kept server-side precisely
 *      so the ward, the voice lane and the handover sheet cannot tell three
 *      different stories about the same failure. **Never compose this sentence
 *      in the client.**
 *    - `evidence_note` is present only when a READY assessment retrieved no
 *      guideline. It is the sentence that stops model-only reasoning from
 *      reading as guideline-backed.
 *    - `disclaimer` is on every payload because it is the regulatory posture,
 *      not a UI choice.
 *    - `state` is the DERIVED state, not necessarily the stored one: a row left
 *      `generating` past the stale window comes back `unavailable` /
 *      `abandoned`, which is what stops a killed appliance leaving a permanent
 *      spinner on a ward screen.
 */
import { apiFetch, getChannels } from './client'

export type SageAssessmentState =
  | 'generating'
  | 'ready'
  | 'unavailable'
  | 'approved'
  | 'declined'

/** The clinician verdicts the API will accept. Neither executes anything. */
export type SageAssessmentVerdict = 'approved' | 'declined'

/** One line of section 2. `orb_can_act` is set server-side from whether a
 *  `sage_actions` CARD REALLY EXISTS, never from the model's claim about
 *  itself — so it is safe to render as a statement of fact about Orb. */
export interface SageAssessmentConsideration {
  key: string
  step: string
  why: string | null
  proposed_action_type: string | null
  proposed_target: string | null
  orb_can_act: boolean
  action_id: string | null
  action_type: string | null
  advice_only_reason: string | null
}

/* ═══════════════════════════════════════════════════════════════════════════
   THE ADVICE TIER, AFTER THE 2026-08-15 RULING

   MEASURED: the old advice tier delivered on 22% of alerts, 0% in a quiet
   control window, and when it did arrive two of its three steps restated the
   alert ("Escalate immediately", "Critical NEWS2"). It was decoration.

   IT IS NOW PATIENT-SPECIFIC ONLY. It says nothing that is true of any patient
   with those numbers. It says only what depends on THIS patient's own chart —
   a rate-controlling drug making a reassuring heart rate misleading, renal
   impairment changing the calculus on a fluid bolus, an allergy that rules out
   the obvious first-line agent, anticoagulation before a procedure, steroids
   blunting a fever, a recent lab that changes the picture.

   THE SHAPE FOLLOWS THE ARCHITECTURE, AND THAT IS THE POINT.
     STEP A is a DETERMINISTIC pre-screen over machinery Orb already has (the
     allergy interlock, the interaction knowledge base, the formulary, the
     chart). It produces candidate FACTS.
     STEP B hands the model that short list and asks only which of them matter
     here. The model never invents a fact and never proposes management. When
     STEP A produced nothing the model IS NEVER CALLED — which is why
     `no_candidates` is instant, free, and the commonest answer on a ward.

   SO SILENCE IS A FIRST-CLASS ANSWER, AND `outcome` IS WHY THIS IS AN ENUM
   RATHER THAN AN EMPTY ARRAY. "Nothing to flag" and "the check did not run"
   are OPPOSITE facts about a deteriorating patient and an empty list cannot
   tell them apart. Any surface reading this must render them differently, and
   must never present an unrecognised value as silence-by-design.
   ═══════════════════════════════════════════════════════════════════════════ */

/** Why this tier is saying what it is saying.
 *
 *   - `flags`         — at least one chart fact matters here. `flags` is short.
 *   - `no_candidates` — the deterministic pre-screen found nothing on this
 *                       chart worth asking about. THE MODEL WAS NEVER CALLED.
 *   - `no_material`   — the pre-screen found candidates; the model judged that
 *                       none of them change this deterioration.
 *   - `failed`        — the check did not complete. NOT silence. A surface that
 *                       renders this as "nothing to flag" is claiming a
 *                       negative finding Orb never made.
 */
export type SagePatientSpecificOutcome =
  | 'flags'
  | 'no_candidates'
  | 'no_material'
  | 'failed'

/** ONE checkable caution.
 *
 *  `fact` is the CHART FACT the pre-screen supplied, and it is what makes the
 *  line checkable in one glance: not "consider renal function" but "on
 *  ramipril; creatinine 180 six days ago". It comes from the chart and never
 *  from the model's memory — the server drops any line naming a fact the
 *  pre-screen did not supply, and the surface drops any flag that arrives
 *  without one.
 *
 *  `caution` is the JUDGEMENT: one terse line on why that fact matters for
 *  THIS deterioration. It is a caution, never an order, and it must never be a
 *  sentence that would be true of any patient with these numbers.
 */
export interface SageAssessmentPatientFlag {
  key: string
  /** The chart fact, already written out by the server. Rendered verbatim and
   *  VISUALLY SEPARATE from the judgement, so it can be verified without
   *  reading the reasoning. */
  fact: string
  /** Where on the chart to check it — 'Medications', 'Allergies', 'Results',
   *  'Problems'. Null when the pre-screen rule did not name a section. */
  fact_source: string | null
  /** When the fact was recorded, already phrased by the server ("six days
   *  ago", "charted 12 Aug"). Never composed in the client. */
  fact_recorded: string | null
  /** The judgement. One line. */
  caution: string
  /** What in THIS deterioration it bears on ("Heart rate 78"), when the
   *  pre-screen matched a scoring parameter. */
  relates_to: string | null
}

/** The advice tier's whole output.
 *
 *  The counts are the pilot's instrument, mirrored into `sage.drafted` (see the
 *  whitelist note in server/services/shadow_service.py). `model_called` is the
 *  throughput fact the ruling predicted: on most alerts the pre-screen finds
 *  nothing, so most alerts never touch the GPU at all.
 */
export interface SageAssessmentPatientSpecific {
  outcome: SagePatientSpecificOutcome
  /** Non-empty only on `flags`. Short by construction. */
  flags: SageAssessmentPatientFlag[]
  /** Whether STEP B ran. False on `no_candidates` by definition. */
  model_called: boolean
  /** How many candidate facts STEP A produced. */
  candidates_n?: number | null
  /** Model lines the server's post-filter dropped for restating the alert. */
  dropped_generic?: number | null
  /** Model lines dropped for naming a fact the pre-screen did not supply —
   *  i.e. the model reaching into its own memory. Counted, never rendered as
   *  advice. */
  dropped_unsourced?: number | null
  /** Cost of the deterministic pre-screen. */
  screen_ms?: number | null
  /** Server-authored wording for `failed`. Render verbatim; never invent it. */
  message?: string | null
}

/** One retrieved document. `attribution` says HOW it relates to the text:
 *  `model_cited` (the model named it and it was really retrieved),
 *  `system_attributed` (the grounding check matched it), `context_only`
 *  (retrieved, not used). */
export interface SageAssessmentEvidence {
  document_id: string | null
  title: string | null
  source: string | null
  trust_tier: number | string | null
  attribution: 'model_cited' | 'system_attributed' | 'context_only' | string
}

/** ONE scored NEWS2 parameter in the Tier 1 block, with how it has MOVED.
 *
 *  Every field here is the server's. `value`/`points`/`note` are copied byte for
 *  byte from Orb's deterministic engine; `display`, the deltas, the two
 *  directions and `movement` are computed once in
 *  `sage_assessment_service._tier1_parameter` so that the ward, the prompt and
 *  any later surface cannot phrase the same movement three ways.
 *
 *  THERE ARE TWO DIRECTIONS, NEVER ONE. NEWS2 points and the raw value do not
 *  move together — a heart rate can climb 92 → 110 and score 1 at both ends —
 *  so `points_direction` and `value_direction` are separate and a renderer that
 *  collapses them into one arrow will lie about one of them.
 */
export interface SageAssessmentTier1Parameter {
  parameter: string
  value: number | string | boolean | null
  points: number
  note: string | null
  /** The value rendered once, server-side, with its unit. */
  display: string
  unit: string | null
  previous_value: number | string | boolean | null
  previous_points: number | null
  first_value: number | string | boolean | null
  first_points: number | null
  value_delta: number | null
  points_delta: number | null
  points_direction: 'worsening' | 'improving' | 'unchanged' | 'new' | 'unknown' | string
  value_direction: 'up' | 'down' | 'flat' | 'unknown' | string
  /** The server's whole sentence, e.g. "Systolic BP 65mmHg, down from 108mmHg
   *  across the last 6 observations, NEWS2 points 1 → 3". RENDER IT VERBATIM —
   *  this is the sentence the ward was missing while every chart read "Stable". */
  movement: string
}

/** TIER 1 — the deterministic driver/trend block.
 *
 *  No model, 33-240 ms, computed before generation starts and emitted on EVERY
 *  payload whatever `state` says, because no generation failure can retract it.
 *  `available: false` is a real, renderable answer (no observation, nothing
 *  measured, or the computation itself failed) and carries the server's
 *  `message` for it.
 */
export interface SageAssessmentTier1 {
  available: boolean
  /** Present when `available` is false. */
  reason?: string | null
  /** Server-authored wording for an unavailable block. Render verbatim. */
  message?: string | null
  source?: string
  scoring_model?: string
  spo2_scale?: number
  /** Orb's NEWS2 for the observation this block decomposes. Deterministic —
   *  displayed, never recomputed. */
  news2?: number | null
  /** The RCP BAND for this observation, which is NOT the alert's level: the CfC
   *  overlay may escalate the displayed level and never de-escalates it. */
  news2_band?: string | null
  single_param_3?: boolean
  complete?: boolean
  missing_parameters?: string[]
  coverage_note?: string | null
  observed_at?: string | null
  observations_used?: number
  window_from?: string | null
  window_to?: string | null
  parameters?: SageAssessmentTier1Parameter[]
  /** Measured, scoring zero. Kept apart from `parameters` so "steady" is not
   *  mistaken for "driving the score". */
  non_scoring?: SageAssessmentTier1Parameter[]
  most_abnormal?: SageAssessmentTier1Parameter | null
  /** Everything level with `most_abnormal`. The tie is reported, not hidden. */
  most_abnormal_tied_with?: string[]
  trend?: 'worsening' | 'improving' | 'unchanged' | 'insufficient_history' | string
  news2_delta?: number | null
  computed_at?: string
  /** One deterministic sentence: the score, where it came from, where it is
   *  going. Composed server-side; render verbatim. */
  headline?: string
}

export interface SageAssessment {
  id: string
  patient_id: string
  channel_id: string | null
  message_id: number | null
  lane: string | null
  depth?: string | null
  state: SageAssessmentState
  /** The state of the MODEL'S half only. `state` still drives approve/decline;
   *  this exists so a surface can say "drivers now, advice pending" without
   *  deriving one from the other and eventually deriving it wrong at a bedside. */
  advice_state?: SageAssessmentState
  version: number
  unavailable_reason: string | null
  /** Server-authored ward-facing wording. Render verbatim; never invent it. */
  unavailable_message: string | null
  fallback_text: string | null
  /** ── TIER 1 — always present, never model-derived. ──
   *  The deterministic driver/trend block. Emitted whatever `state` says. */
  tier1?: SageAssessmentTier1
  /** `tier1.available` restated by the server so a surface never has to guess. */
  has_tier1?: boolean
  /** Whether the MODEL's next steps landed. "No advice yet" and "no assessment"
   *  are different sentences. */
  has_advice?: boolean
  /** Deterministic. Computed by Orb's NEWS2 engine, pinned into the prompt as
   *  authoritative and never recalculated by the model — so it is displayed,
   *  never derived from anything else on this payload. */
  news2: number | null
  news2_level: string | null
  news2_drivers: string[]
  /** Section 1 — the interpretation. */
  whats_happening: string | null
  /** ── THE ADVICE TIER ──
   *  Either a short list of patient-specific flags, each carrying the chart
   *  fact that produced it, or an explicit "nothing to flag" / "did not run"
   *  state. Optional because a row written before 2026-08-15 has no such
   *  block at all, and an absent block is neither of those answers: a surface
   *  must render nothing rather than claim a finding Orb never made. */
  patient_specific?: SageAssessmentPatientSpecific | null
  /** Section 2. */
  considerations: SageAssessmentConsideration[]
  /** Set only when the list of steps was cut short by the generation budget. A
   *  truncated list must not be able to read as a finished one. */
  advice_note?: string | null
  /** Section 3. */
  evidence: SageAssessmentEvidence[]
  citation_basis: string | null
  /** Present ONLY on a ready assessment with no retrieved guideline. */
  evidence_note: string | null
  /** Section 4. */
  watch_items: string[]
  confidence: string | null
  model: string | null
  generated_ms: number | null
  ttft_ms: number | null
  /** When the first READABLE suggestion reached the surface — the honest answer
   *  to "how fast is it", which `ttft_ms` is not. */
  first_step_ms?: number | null
  created_at: string
  updated_at: string
  disclaimer: string
}

export interface AssessmentTransitionResult {
  status: string
  assessment: SageAssessment
}

/** Drafted assessments for a case room, newest first.
 *  `messageId` narrows to the draft for one specific Relay alert. */
export async function listSageAssessments(
  channelId: string,
  messageId?: number,
): Promise<SageAssessment[]> {
  const qs = messageId == null ? '' : `?message_id=${encodeURIComponent(String(messageId))}`
  return apiFetch(`/relay/channels/${encodeURIComponent(channelId)}/sage_assessments${qs}`)
}

/** One drafted assessment. Access is checked on the assessment's CHANNEL. */
export async function getSageAssessment(assessmentId: string): Promise<SageAssessment> {
  return apiFetch(`/relay/sage_assessments/${encodeURIComponent(assessmentId)}`)
}

/** Record a clinician's verdict on a DRAFT.
 *
 *  THIS EXECUTES NOTHING, and the name says so. Approving records that a
 *  clinician read the reasoning and agrees with it; it does not place an order,
 *  start a drug or move a patient. Everything Orb could actually do is a
 *  separate `sage_actions` card with its own confirmation, its own confirm lock
 *  and its own drug-safety gate — `transitionSageAction` in client.ts.
 *
 *  `version` is the optimistic lock, matching the card convention: a stale
 *  version comes back 409 and the surface re-reads rather than overwriting
 *  somebody else's verdict.
 */
export async function transitionAssessment(
  channelId: string,
  assessmentId: string,
  state: SageAssessmentVerdict,
  version: number,
  reason?: string | null,
): Promise<AssessmentTransitionResult> {
  return apiFetch(
    `/relay/channels/${encodeURIComponent(channelId)}/sage_assessments/${encodeURIComponent(assessmentId)}/transition`,
    {
      method: 'POST',
      body: JSON.stringify({ state, version, reason: reason ?? null }),
    },
  )
}

/** The case room for a patient, or null.
 *
 *  Assessments are keyed to a Relay channel because that is where the alert
 *  message lives, so a patient-scoped surface has to resolve the room first.
 *  Same lookup `components/relay/AlertsSection.tsx` already uses — deliberately
 *  the same shape so there is one idea of "this patient's case room".
 *
 *  A patient with no case room has no reachable draft. That is reported as
 *  null (nothing to show) rather than as a failure: it is the ordinary state of
 *  a patient nobody has raised a room for.
 */
export async function findCaseRoomId(patientId: string): Promise<string | null> {
  const channels = await getChannels()
  const room = channels.find(c => c.type === 'case_room' && c.patient_id === patientId)
  return room ? room.id : null
}

/** What one patient's case room holds. */
export interface PatientAssessments {
  channelId: string | null
  /** The NEWEST row. It governs the current state and nothing else. */
  assessment: SageAssessment | null
  /** EVERY drafted assessment in the room, newest first — never just the head.
   *  See the note on `getLatestAssessmentForPatient`. */
  rows: SageAssessment[]
}

/** Every drafted assessment for a patient, with the channel they belong to.
 *
 *  Returns `{ channelId: null, assessment: null, rows: [] }` when the patient
 *  has no case room, and `{ channelId, assessment: null, rows: [] }` when the
 *  room exists but no draft has been written — two different facts, and the
 *  surface renders neither as a spinner. "No draft" is the correct, common
 *  state: assessments are written on a CRITICAL Vigil alert and nowhere else,
 *  and with the feature switched off no row is written at all.
 *
 *  ═══ WHY THE WHOLE LIST COMES BACK, NOT JUST `rows[0]` ═══
 *  A patient has ONE case room and EVERY critical alert on that patient writes
 *  another row into it. This function used to return `rows[0]` alone, and on
 *  the ward that read as a claim about the patient rather than about the newest
 *  alert: a draft that was `ready` with full content at 16:25 was replaced on
 *  screen by a 16:26 sibling that had failed with `queue_timeout`, and the chart
 *  then said "No assessment was written" 101 seconds AFTER Sage had written one.
 *  The finished assessment was still in the database, still unresolved, and
 *  unreachable — its approve/decline controls belonged to a row nothing would
 *  render.
 *
 *  So the newest row still governs the CURRENT state (that is honest — the
 *  latest alert genuinely has no assessment), and the caller is handed the
 *  siblings so an older UNRESOLVED draft cannot be buried by a newer failed one.
 *  Which siblings still need a clinician, and how they are shown, is the
 *  surface's decision: `partitionAssessments` in
 *  components/sage/SageAlertAssessment.tsx.
 */
export async function getLatestAssessmentForPatient(
  patientId: string,
): Promise<PatientAssessments> {
  const channelId = await findCaseRoomId(patientId)
  if (!channelId) return { channelId: null, assessment: null, rows: [] }
  const rows = await listSageAssessments(channelId)
  // The endpoint orders newest-first; taking [0] rather than sorting here keeps
  // one ordering rule, on the server, where the timestamps are canonical.
  return { channelId, assessment: rows.length > 0 ? rows[0] : null, rows }
}
