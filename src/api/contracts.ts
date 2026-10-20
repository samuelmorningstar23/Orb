/** Public API wire contracts. Type-only imports keep transport independent. */
import type { News2, ObsSchedule } from './nurse';


/** Shape of the user object persisted to localStorage. Structurally compatible
 *  with App's `User` type (role kept as `string | null` so the union from App is
 *  assignable here without importing it and creating a circular module ref). */
export interface StoredUser {
  id: string;
  name: string;
  role: string | null;
  department?: string;
  registrationNumber?: string;
  /** How this session was authenticated. 'pin' means a shared ward tablet, which
   *  the app auto-signs-out far sooner — a tablet left on a trolley is the
   *  threat model. Not a credential and not trusted for authorization: the
   *  server enforces the short lifetime on the token itself. */
  authMethod?: 'password' | 'pin';
}


/** The structured body of a 403. The RBAC layer sends `permission_denied`
 *  with the permissions involved; other refusals (`mfa_enrollment_required`,
 *  `allergy_override_not_permitted`, ...) carry their own fields. A plain
 *  string detail arrives here as `{ message }`. */
export interface ApiPermissionDetail {
  error?: string;
  message?: string;
  required?: string[];
  missing?: string[];
  [key: string]: unknown;
}


/** Thrown by apiFetch and handleResponseError on a 403. */
export interface ApiPermissionError extends Error {
  status: 403;
  permissions: ApiPermissionDetail;
  /** The MFA self-enrolment ticket, when the server issued one in the
   *  MFA_ENROLL_TICKET_HEADER response header (apiFetch only). */
  mfaEnrollTicket?: string;
}



// ─── Offline Support ───
export interface OfflineRequest {
  id: string;
  url: string;
  method: string;
  /** The request body as the caller passed it (replayed verbatim if a
   *  string, JSON-encoded otherwise). */
  body: unknown;
  timestamp: number;
  /** Idempotency-Key sent with the replay so the server can dedupe a request it
   *  already processed (e.g. the response was lost in transit and we retry). */
  idempotencyKey?: string;
  /** WHO queued this write. A replay goes out on whatever session is signed in
   *  when the connection returns, so without this the previous nurse's note is
   *  filed under the next nurse's name — silently, and long after either of them
   *  could notice. Entries without it were queued by an older build and are
   *  refused rather than attributed to a guess. */
  userId?: string;
}


// ─── Auth ───
export interface LoginResponse {
  token: string;
  user: {
    id: string;
    name: string;
    role: string;
    department?: string;
    registration_number?: string;
    /** Where this sign-in works (server UserOut.facility): App's
     *  UserFacility, null when the server could not say. */
    facility?: { id: number; code: string | null; name: string | null } | null;
  };
}


// ─── Vigil ───
export interface Patient {
  id: string;
  name: string;
  mrn: string;
  bed: string;
  ward: string;
  admission_date: string;
  diagnosis: string;
  age: number;
  sex: string;
  /** NEWS2 of the patient's latest vitals row, scored by the server; null when
   *  nothing is recorded. The board shows this, never `risk_score`. */
  news2?: News2 | null;
  /** When that vitals row was observed. A live tick older than this is stale. */
  news2_at?: string | null;
  /** The same moment as `news2`: its 0-1 INDEX (not a probability), band and
   *  drivers, kept for the round queue and the patient picker. null when
   *  nothing is recorded. */
  risk_score: number | null;
  risk_level: string | null;
  risk_drivers: string[];
  attending_doctor_name?: string;
  admission_department?: string;
}


/** One stored vitals row as the chart and the sparkline receive it. `source`
 *  is the row's provenance ('simulated' for the simulator), null on a
 *  database that predates the column. */
export interface ChartVitalsRow {
  timestamp: string;
  heart_rate: number | null;
  systolic_bp: number | null;
  diastolic_bp: number | null;
  spo2: number | null;
  temperature: number | null;
  respiratory_rate: number | null;
  source: string | null;
}


/** NEWS2 of the chart's newest vitals row (vigil_service.risk_for_row): the
 *  full score, or the no-vitals stub (score 0, stable, no breakdown) when
 *  nothing is recorded. The CfC overlay adds the `cfc` fields when a trained
 *  checkpoint is loaded. */
export type PatientRisk = Pick<News2, 'score' | 'level' | 'drivers' | 'scoring_model'>
  & Partial<Omit<News2, 'score' | 'level' | 'drivers' | 'scoring_model'>>
  & {
    cfc?: Record<string, { risk_by_horizon: unknown; peak_risk: number }>;
    cfc_primary_risk?: number;
    cfc_seq_len?: number;
  };


/** Recent-vs-earlier vitals direction (vigil_service.get_vitals_trend). */
export interface VitalsTrend {
  trend: 'deteriorating' | 'improving' | 'stable' | 'insufficient_data';
  /** Why there is no trend, on 'insufficient_data' only. */
  reason?: string;
  hr_delta?: number | null;
  sbp_delta?: number | null;
  spo2_delta?: number | null;
}


/** A `patients` row as the chart serves it, plus what the route joins on.
 *  Every column of the row is sent (`p.*`), so columns added by later
 *  migrations arrive too; the named ones are what the chart reads. */
export interface ChartPatientRecord {
  id: string;
  name: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  admission_date: string | null;
  diagnosis: string | null;
  status: string;
  age: number | null;
  date_of_birth?: string | null;
  sex: string | null;
  attending_doctor_id: string | null;
  attending_doctor_name: string | null;
  on_scale_2?: number | null;
  comorbidities: string[];
  /** The admission this chart belongs to: the active one, else the newest. */
  admission_id: string | null;
  [column: string]: unknown;
}


/** A `clinical_documents` row (a note, a ward round, a lab report). */
export interface ClinicalDocumentRow {
  id: number;
  patient_id: string;
  doc_type: string;
  title: string;
  content: string;
  author_id: string | null;
  created_at: string;
  /** 'simulated' for a simulator's result. */
  source?: string | null;
  idempotency_key?: string | null;
}


/** A `scribe_notes` row. The JSON columns arrive as stored TEXT here; the
 *  verification route is the one that parses them. */
export interface ScribeNoteRow {
  id: number;
  patient_id: string | null;
  author_id: string;
  raw_transcript: string;
  structured_note: string | null;
  note_format: 'soap' | 'discharge' | 'handoff' | 'raw' | null;
  audio_path: string | null;
  verification_json: string | null;
  pipeline_meta_json: string | null;
  created_at: string;
  status?: string;
  version?: number;
  supersedes_note_id?: number | null;
  signed_by?: string | null;
  signed_at?: string | null;
  finalized_at?: string | null;
  rejected_by?: string | null;
  rejected_at?: string | null;
  rejection_reason?: string | null;
  last_edited_by?: string | null;
  last_edited_at?: string | null;
  ai_generated?: number;
  unverified_ack_by?: string | null;
  unverified_ack_at?: string | null;
}


/** A `lens_analyses` row, with `image_url` (the authenticated /uploads path
 *  the screen loads; `image_path` is a location on the appliance's disk). */
export interface LensAnalysisRow {
  id: number;
  patient_id: string | null;
  author_id: string;
  image_path: string;
  image_url: string | null;
  image_type: string | null;
  clinical_context: string | null;
  /** The model's raw read. */
  analysis: string;
  findings_json: string | null;
  created_at: string;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  reviewed_text?: string | null;
  /** 'simulated' for a simulator's report (migration 46), so the chart can
   *  label it; null for a clinician's own. Every route sends it (SELECT *). */
  source?: string | null;
}


/** A `medications` row on the chart. `unverified` is true for an
 *  AI-extracted or agentic order no pharmacist has verified yet. */
export interface ChartMedicationRow {
  id: number;
  patient_id: string;
  drug_name: string;
  dosage: string;
  route: string;
  frequency: string;
  status: 'active' | 'discontinued' | 'prn';
  prescribed_by: string | null;
  notes: string | null;
  started_at: string;
  source?: string | null;
  verified_by?: string | null;
  verified_at?: string | null;
  unverified: boolean;
}


/** One structured lab result (services/lab_observations.chart_lab_rows),
 *  oldest first. */
export interface ChartLabObservation {
  id: number;
  observation_name: string;
  value_numeric: number | null;
  unit: string | null;
  reference_range: string | null;
  abnormal_flag: string | null;
  status: string | null;
  source: string | null;
  observed_at: string;
  lab_result_id: number | null;
}


/** GET /vigil/patients/{id}: the whole chart in one read. */
export interface PatientDetail {
  patient: ChartPatientRecord;
  /** Null while scoring is switched off on this computer (ORB_SCORING_ENABLED,
   *  the Orb Ward Record); `risk_off_reason` then says why, in words. */
  risk: PatientRisk | null;
  risk_off_reason?: string | null;
  trend: VitalsTrend;
  /** Null while scoring is switched off: no schedule from a score. */
  obs_schedule: ObsSchedule | null;
  lab_observations: ChartLabObservation[];
  problems: { problem: string; icd10_code: string | null; onset_date: string | null; source: string }[];
  site_name: string | null;
  /** Newest first: vitals[0] is the set the command bar shows. */
  vitals: ChartVitalsRow[];
  /** True when vitals[0] came from the simulator. */
  simulated_vitals: boolean;
  notes: ClinicalDocumentRow[];
  scribe_notes: ScribeNoteRow[];
  risk_history: { score: number; level: string; drivers: string[]; timestamp: string }[];
  lens_analyses: LensAnalysisRow[];
  medications: ChartMedicationRow[];
  /** False on an Orb Ward Record computer, whose drug chart stays on paper:
   *  `medications` is empty and the chart draws no medicines at all, even
   *  before its own health probe answers. Absent from an older server. */
  drug_chart?: boolean;
}


/** One care phase of a projected clinical course. */
export interface TimelinePhase {
  name: string;
  focus: string;
  /** Free text, e.g. "Day 1-3". */
  typical_days: string;
}


/** GET /vigil/patients/{id}/timeline-projection (services/timeline_service). */
export interface TimelineProjection {
  patient_name: string;
  diagnosis: string;
  age: number | null;
  sex: string | null;
  comorbidities: string[];
  expected_los_days: number | null;
  phases: TimelinePhase[];
  discharge_criteria: string[];
  rationale: string;
  /** Derived from the patient's live risk level, not from the calendar. */
  current_phase_index: number;
  /** 'llm' or 'heuristic' (or the cached row's own source). */
  source: string;
  /** The Ollama model that generated it; null for a heuristic projection. */
  engine: string | null;
}


/** GET /forecast/patient/{id} (forecast_service.predict_patient_los). The
 *  heuristic fallback says so in `prediction_source`, and `model_trained` is
 *  false while the network has no trained checkpoint. */
export interface PatientForecast {
  predicted_los_days: number;
  discharge_probability: number;
  acuity_trajectory: string;
  confidence: number;
  /** YYYY-MM-DD. */
  estimated_discharge_date: string;
  prediction_source?: 'lstm' | 'heuristic';
  model_trained?: boolean;
}


/** First observations taken at the admission desk. ACVPU and oxygen are null
 *  when not assessed, never a guess: NEWS2 then reports the set incomplete. */
export interface AdmissionVitals {
  hr?: number;
  bp_sys?: number;
  bp_dia?: number;
  spo2?: number;
  temperature?: number;
  rr?: number;
  consciousness?: string | null;
  on_oxygen?: boolean | null;
}


/** POST /admissions/admit body (admission_routes.AdmissionRequest). */
export interface AdmissionRequest {
  name: string;
  age?: number | null;
  /** Optional, but it is the second factor on the discharge link. */
  date_of_birth?: string | null;
  sex?: string | null;
  phone?: string | null;
  emergency_contact_name?: string | null;
  emergency_contact_phone?: string | null;
  address?: string | null;
  mrn?: string | null;
  admission_source: string;
  admission_type: string;
  department: string;
  ward: string;
  bed: string;
  attending_doctor_id: string;
  referring_clinician?: string | null;
  chief_complaint: string;
  initial_diagnosis?: string | null;
  triage_severity?: number | null;
  comorbidities?: string[];
  past_medical_history?: string | null;
  past_surgical_history?: string | null;
  allergies?: string[];
  current_medications?: string[];
  family_history?: string | null;
  social_history?: string | null;
  vitals?: AdmissionVitals | null;
  admitting_notes?: string | null;
  nursing_notes?: string | null;
  isolation_precautions?: string[];
  code_status?: string | null;
}


/** What a successful admission returns (admission_service.admit_patient). */
export interface AdmissionResult {
  success: boolean;
  patient_id: string;
  admission_id: string;
  channel_id: string;
  mrn: string;
  /** The patient's Bridge login and one-time password. Returned exactly once:
   *  only the hash is stored, so it can never be retrieved again. */
  patient_login_id: string;
  temporary_password: string;
  /** False when no date of birth was recorded, so no discharge link can open. */
  date_of_birth_recorded: boolean;
  /** Null, or the sentence saying this bed was already held. */
  bed_warning: string | null;
}


// ─── Relay ───
export interface Channel {
  id: string;
  name: string;
  type: string;
  patient_id?: string;
  mrn?: string;
  diagnosis?: string;
  ward?: string;
  bed?: string;
  risk_score?: number;
  risk_level?: 'critical' | 'elevated' | 'stable';
  unread_count: number;
  last_message?: string;
  last_activity?: string;
  alert_count?: number;
  has_alert: boolean;
}


export interface Message {
  id: number;
  channel_id: string;
  sender_id: string;
  sender_name: string;
  sender_role: string;
  content: string;
  message_type: string;
  timestamp: string;
  // Provenance: true for automated/AI bot posts (simulation vitals/labs, Lens AI,
  // Vigil alerts). The UI badges these so bot chatter is never mistaken for a
  // real clinician message. `source` is 'clinician' | 'bot' | 'simulated' | ...
  // ('simulated': a simulator's lab or imaging result, badged as one).
  is_bot?: boolean;
  source?: string;
  /** Wrong-room warning attached to THIS send, when the detector fired.
   *  Present only on the POST response — the durable copy comes from
   *  getRoomFitFlags(). See services/room_fit_service.py. */
  room_fit?: RoomFitFlag;
  /** CLIENT-ONLY correlation token. Set on an optimistic bubble so the socket
   *  echo of the same send can be matched to it before the POST response comes
   *  back with the real id. Never sent to or returned by the server. */
  client_key?: string;
}


/** A message the wrong-room detector flagged as possibly belonging to another
 *  patient. Room-visible and durable: whoever reads this thread next week needs
 *  to know a foreign clinical fact may have landed in it. */
export interface RoomFitFlag {
  id?: number;
  /** Present on the POST-response copy; `id` on the durable copy. */
  flag_id?: number;
  message_id: number;
  channel_id?: string;
  band: 'flag' | 'review' | 'clear';
  stage: 'deterministic' | 'model';
  score?: number;
  suspected_patient_id?: string | null;
  suspected_patient_name?: string | null;
  suggested_channel_id?: string | null;
  suggested_channel_name?: string | null;
  reasons?: string[];
  markers?: string[];
  suppressors?: string[];
  matched_surface?: string | null;
  ambiguous?: boolean;
  resolution: 'open' | 'confirmed_correct' | 'redirected' | 'auto_cleared';
  resolved_by?: string | null;
  resolved_by_name?: string | null;
  resolved_at?: string | null;
  resolve_reason?: string | null;
  redirected_message_id?: number | null;
  created_at?: string;
}


export interface SageAmbientMemory {
  room_id: string;
  patient_id: string;
  patient_summary: string;
  discussion_summary: string;
  current_concern: string;
  likely_next_steps: string[];
  unresolved_questions: string[];
  active_intents: {
    action: string;
    target: string;
    rationale?: string;
    confidence: number;
    created_at: string;
    updated_at: string;
  }[];
  risk_flags: {
    detail: string;
    rationale?: string;
    confidence: number;
    created_at: string;
    updated_at: string;
  }[];
  last_processed_at: string;
  updated_at: string;
}


/** One alert from the drug-safety screen that ran when the card was confirmed. */
export interface SageHandoffAlert {
  type: 'allergy' | 'interaction' | string;
  severity: string;
  match?: string | null;
  message: string;
}


/** The drug-safety screen behind a medication handoff (Helix lane only). */
export interface SageHandoffSafety {
  blocking: boolean;
  screen_failed: boolean;
  alerts: SageHandoffAlert[];
  coverage_note?: string | null;
}


export interface SageHandoff {
  id: string;
  /** 'blocked': the drug-safety screen stopped the order; nothing was charted. */
  status: 'pending' | 'accepted' | 'failed' | 'unsupported' | 'blocked';
  target_module: string;
  error_summary?: string;
  downstream_ref_id?: string;
  safety?: SageHandoffSafety | null;
}


export interface SageAction {
  id: string;
  room_id: string;
  patient_id: string;
  action_type: string;
  target: string;
  rationale?: string;
  confidence: number;
  state: 'suggested' | 'acknowledged' | 'dismissed' | 'confirmed' | 'failed' | 'superseded';
  version: number;
  source_msg_id?: number;
  created_at: string;
  updated_at: string;
  handoff?: SageHandoff;
}


// ── Sage Proactive (surveillance insights) ──
export interface SageInsight {
  id: string;
  patient_id: string;
  patient_name?: string;
  insight_type: string;
  severity: 'critical' | 'warning' | 'info';
  title: string;
  detail?: string;
  suggested_action_type?: string | null;
  suggested_action_target?: string | null;
  state: string;
  created_at: string;
}


// ─── Sage ───
/** One retrieved guideline document on a Sage answer. `cited` means the
 *  model named it; `attribution` says how it came to be shown. */
export interface SageSourceRef {
  document_id?: string;
  title?: string;
  source?: string;
  source_type?: string;
  /** The document's trust tier, an integer: 1-2 the hospital's own policy and
   *  formulary, 3+ external guidance. Null when the source carries none. */
  trust_tier?: number | null;
  is_synthetic?: boolean;
  category?: string;
  fusion_score?: number;
  cited?: boolean;
  attribution?: 'model_cited' | 'system_attributed' | 'context_only';
}


/** The model route a Sage turn took (`route_info` on the server). The
 *  provider layer adds keys over time, so only the read ones are named. */
export interface SageProviderInfo {
  lane?: string;
  provider?: string;
  model?: string;
  preferred_model?: string | null;
  downgraded?: boolean;
  thinking_tier?: string | null;
  thinking_requested?: boolean;
  [key: string]: unknown;
}


/** Timing on a Sage answer, in ms. Null means not measured. */
export interface SageStreamTiming {
  retrieval_ms?: number;
  ttft_ms?: number | null;
  first_content_ms?: number | null;
  total_ms?: number | null;
}


/** The parsed sections of a full Sage answer (the `structured_response`
 *  event). Every section is the model's text, sanitised server-side. */
export interface SageStructuredResponse {
  concise_answer?: string;
  patient_facts_summary?: string;
  local_policy_guidance?: string;
  external_guidance?: string;
  clinical_assessment?: string;
  caveats?: string;
  escalation?: string;
  display_mode?: string;
  action_intent?: string;
  cited_sources?: string[];
}


/** What `querySage` hands its `onMetadata` callback: the stream's `metadata`
 *  event, or (as a separate call) the `structured_response` event wrapped in
 *  `_structured_response`. Fields are optional because the refusal and
 *  out-of-scope paths send a shorter event. */
export interface SageStreamMetadata {
  action_state?: Record<string, unknown> | null;
  sources?: SageSourceRef[];
  citation_basis?: string;
  cited_count?: number;
  attributed_count?: number;
  attributed_sources?: string[];
  confidence?: string;
  confidence_basis?: string;
  escalation?: boolean;
  display_mode?: string;
  query_scope?: string;
  patient_context_used?: boolean;
  patient_id?: string | null;
  patient_name?: string | null;
  patient_source?: string | null;
  data_completeness?: { missing?: string[]; [key: string]: unknown } | null;
  local_policy_found?: boolean;
  provider?: SageProviderInfo | null;
  timing?: SageStreamTiming;
  _structured_response?: SageStructuredResponse;
}


// ─── Sage AI governance / ambient intelligence ───

export interface SageAiPosture {
  provider_mode: string;
  hosted_enabled: boolean;
  hosted_configured: boolean;
  hosted_host_allowlisted: boolean;
  phi_egress_approved: boolean;
  local_model: string;
  /** Whether ON-APPLIANCE transcription can run. A capability fact, and every
   *  dictation control needs it so it can say what it can do BEFORE the
   *  clinician speaks, rather than failing after thirty seconds of dictation.
   *  It is the only speech recognition there is: Orb is strictly offline. */
  local_asr_available?: boolean;
}


export interface WardBrief {
  snapshot: {
    /** Null for a department-scoped caller: a hospital-wide count describes
     *  wards they cannot see. */
    census: number | null;
    risk_distribution: Record<string, number>;
    watchlist: Array<{ patient_id: string; name: string; bed: string; ward: string; level: string; score: number }>;
    recent_events: Array<{ topic: string; event_type: string; patient_id: string | null; timestamp: string }>;
    /** Null for a scoped caller, for the same reason as census. */
    active_alerts: number | null;
    as_of: string;
    /** The caller's department when the brief is scoped to it. */
    scope?: string;
  } | null;
  narrative: string | null;
  narrative_engine: string | null;
  generated_at: string | null;
}


/** One stored Sage turn. `metadata` carries only what a reloaded thread
 *  needs (the speed footer and the patient label), and is absent on a turn
 *  stored before timing was recorded or whose timing was never measured. */
export interface SageHistoryMessage {
  role: string;
  content: string;
  conversation_id: string | null;
  timestamp: string;
  metadata?: {
    timing?: SageVoiceTiming;
    provider?: { thinking_tier: string };
    patient_name?: string;
  };
}


// ─── Sage Voice Agent ───
/** Timing on a voice answer (sage_routes._answer_timing). Null means not
 *  measured: the refusal paths never ran the model. */
export interface SageVoiceTiming {
  ttft_ms: number | null;
  total_ms: number | null;
  tokens: number | null;
}


/** One issue the verifier found: a claim the transcript does not support. */
export interface ScribeVerificationWarning {
  type?: string;
  section?: string;
  text: string;
  severity?: 'low' | 'medium' | 'high' | string;
}


/** The note verifier's report (scribe_routes._run_verification_async).
 *  `status` is 'not_requested', 'pending', 'complete' or 'failed'; the
 *  findings are present once it is complete, `error` once it failed. */
export interface ScribeVerification {
  status?: string;
  verified?: boolean;
  confidence?: number;
  /** 'unverifiable' when the verifier ran but could not reach a verdict. */
  verification_status?: string;
  warnings?: ScribeVerificationWarning[];
  omissions?: { text: string; severity?: string }[];
  sections_verified?: string[];
  error?: string;
  started_at?: string;
  completed_at?: string;
  failed_at?: string;
  elapsed_s?: number;
}


/** Stage timings and models behind a note (scribe_service.build_pipeline_meta). */
export interface ScribePipelineMeta {
  /** Seconds per stage, keyed by stage name. */
  timings?: Record<string, number>;
  models?: { asr?: string; correction?: string | null; draft?: string; verify?: string | null };
  stages_enabled?: { correction?: boolean; verify?: boolean };
}


/** The active Scribe pipeline (admin only): models and enabled stages. */
export interface ScribeConfig {
  asr: { model: string; provider: string; batch_size: number };
  correction: { enabled: boolean; model: string; provider: string };
  draft: { model: string; provider: string };
  verify: { enabled: boolean; model: string; provider: string };
}


export interface ScribeAsrStatus {
  available: boolean;
  provider: string;
  model: string;
  reason: string | null;
  runbook: string;
}


// ─── Bridge ───
/** The patient's status in plain words (bridge_service.get_patient_status_simple).
 *  A login with no patient record gets only `status: 'unknown'` and a
 *  `message`. Vitals are charted text ("72 bpm"), never raw numbers. */
export interface BridgeStatus {
  status: string;
  message?: string;
  patient_name?: string;
  bed?: string | null;
  ward?: string | null;
  diagnosis_simple?: string;
  status_message?: string;
  vitals?: {
    heart_rate: string | null;
    blood_pressure: string | null;
    oxygen: string | null;
    temperature: string | null;
  } | null;
  attending_doctor?: string | null;
}


/** A medication as the patient portal lists it. */
export interface BridgeMedication {
  drug_name: string;
  dosage: string;
  route: string;
  frequency: string;
  status: string;
  started_at: string;
}


/** A clinical document as the portal shows it. `type_label` is the type in
 *  the patient's words; `source` is 'simulated' for a simulator's result. */
export interface BridgeDocument {
  doc_type: string;
  title: string;
  content: string;
  created_at: string;
  source?: string | null;
  type_label: string;
}


/** A discharge summary as the patient may see it: `summary` stays null until
 *  a clinician has reviewed the draft, and `message` says why. */
export interface BridgeDischargeView {
  summary: string | null;
  status: string;
  message: string | null;
  discharged_at: string;
}


/** GET /bridge/reports: everything on record for the signed-in patient. */
export interface BridgeReports {
  patient_id: string;
  medications: BridgeMedication[];
  /** False on an Orb Ward Record computer: no Medications button is drawn. */
  drug_chart?: boolean;
  clinical_documents: BridgeDocument[];
  labs: BridgeDocument[];
  discharge_summary: BridgeDischargeView | null;
  /** Clinician-reviewed reads only; `analysis` is the reviewed text. */
  imaging_reports: {
    image_type: string | null;
    clinical_context: string | null;
    analysis: string | null;
    reviewed_at: string | null;
    created_at: string;
  }[];
}


/** One entry on a discharge timeline (discharge_service.build_patient_timeline). */
export interface DischargeTimelineEntry {
  timestamp: string | null;
  type: string;
  title: string;
  detail: string;
}


// ─── Drug-Safety Gate (safety-ack protocol) ───
// Every medication-writing endpoint (Vigil med-add, CPOE med orders) runs the
// order-time allergy + interaction screen. Sub-severe results come back as
// `safety` on the success response; severe results 428 until re-submitted with
// an acknowledged SafetyAck (the override is audited server-side).

export interface SafetyAlert {
  severity: string;
  message: string;
  type?: string;
}


export interface MedicationSafety {
  drug?: string;
  allergy_alerts: SafetyAlert[];
  interaction_alerts: SafetyAlert[];
  max_severity: string;
  blocking: boolean;
  /** False when the drug maps to no known class — interactions NOT screened. */
  interaction_screened: boolean;
  coverage_note: string | null;
  /** The dose guard's verdict on the ordered dose, when the lane knew the dose
   *  (Vigil add-medication, Helix approve, Relay). A 'block' makes `blocking`
   *  true and clears only with a prescriber's written reason. */
  dose_check?: (DoseCheckSummary & { override_requires_permission?: string | null }) | null;
  /** Why the server refused the written reason on the last attempt, in its
   *  own words (for example a reason too short to be one). */
  ack_refused?: string | null;
  /** Set by the server on a severe allergy block: the permission that may
   *  clear it (medication.prescribe). */
  override_requires_permission?: string | null;
}


/** Clinician acknowledgement re-submitted after reviewing severe alerts. */
export interface SafetyAck {
  acknowledged: true;
  reason: string;
}


/** Thrown by apiFetch on a 428 drug_safety_acknowledgement_required response. */
export interface SafetyAckRequiredError extends Error {
  code: 'safety_ack_required';
  status: 428;
  safety: MedicationSafety;
}


/** What addClinicalNote resolves to: the server's `{status, id}` when online,
 *  or, when the note was queued offline, the note itself under an `offline-`
 *  id. */
export type AddClinicalNoteResult =
  | { status: string; id: number }
  | { id: string; patient_id: string; content: string; doc_type: string; title: string; created_at: string };


/** A medication order from the chart (POST /vigil/patients/{id}/medications).
 *  The server defaults `route` to 'Oral' and `status` to 'active'; the table
 *  accepts 'active', 'discontinued' or 'prn'. */
export interface MedicationOrderInput {
  drug_name: string;
  dosage: string;
  route?: string;
  frequency: string;
  status?: string;
}


// ─── Orders / CPOE ───
export interface CreateOrderPayload {
  patient_id: string;
  order_type: string;
  description: string;
  code?: string | null;
  code_system?: string | null;
  priority?: string;
}


// ─── eMAR (bedside medication administration) ───
/** One alert from the server's allergy interlock, as
 *  `allergy_interlock.summarize()` shapes it. */
export interface AllergyAlert {
  substance?: string;
  reaction?: string | null;
  match?: string;
  decision?: string;
  effective_severity?: string;
  severity_basis?: string;
  message?: string;
}


/** WHAT THE INTERLOCK SAW AND DECIDED FOR THIS DOSE, stored on the
 *  administration row itself and served back with the history.
 *
 *  `null` on a record means the dose was never screened (a 'not_given' row, or
 *  one charted before the interlock existed) — which is NOT the same statement
 *  as "screened and clean", and the UI must not render them alike.
 *
 *  `override` is present only when the dose was given ACROSS a block. That is
 *  the field the chart surfaces render a marker from: a drug given despite a
 *  charted allergy has to be visible to whoever reads the record next, without
 *  them going to the audit log to find out. */
export interface AllergyCheck {
  decision?: 'allow' | 'warn' | 'block' | string;
  screen_failed?: boolean;
  allergies_on_file?: number | null;
  alerts?: AllergyAlert[];
  override?: {
    reason?: string;
    /** True when this dose inherited a reason typed for an earlier dose of the
     *  same order against the same allergy picture, rather than a fresh
     *  decision made now. */
    carried?: boolean;
    by?: string | null;
    at?: string | null;
  } | null;
  /** The permission needed to clear this block — `medication.prescribe` for a
   *  charted allergy, null when nothing needs clearing or when the screen
   *  merely failed. Sent by the server so no client hard-codes "ask a doctor". */
  override_requires_permission?: string | null;
  message?: string | null;
}


/** One finding from the dose guard, as drug_safety_service.summarize_dose_check
 *  shapes it. `decision` is 'block' (the dose was refused) or 'warn'. */
export interface DoseCheckFinding {
  kind?: string;
  decision?: 'allow' | 'warn' | 'block' | string;
  rule?: string | null;
  message?: string | null;
  threshold?: number | null;
  unit?: string | null;
}


/** THE DOSE GUARD'S VERDICT. Rides on a charted dose as
 *  `administration.dose_check` (a WARN the server let through) and on a refused
 *  one as `detail.dose` of the 428 `dose_acknowledgement_required`. */
export interface DoseCheckSummary {
  decision?: 'allow' | 'warn' | 'block' | string;
  unavailable?: boolean;
  unavailable_reason?: string | null;
  findings?: DoseCheckFinding[];
  message?: string | null;
}


export interface AdministrationRecord {
  id: number;
  medication_id: number;
  patient_id: string;
  drug_name: string;
  status: 'given' | 'not_given';
  dose_given: string | null;
  route_given: string | null;
  not_given_reason: string | null;
  administered_by: string;
  administered_by_name: string | null;
  administered_at: string;
  confirmation_method?: string;
  notes?: string | null;
  allergy_check?: AllergyCheck | null;
  dose_check?: DoseCheckSummary | null;
}


export interface AdministerPayload {
  status: 'given' | 'not_given';
  dose_given?: string;
  route_given?: string;
  not_given_reason?: string;
  confirmation_method: 'typed';
  notes?: string;
  /** PRESCRIBER-ONLY, and only after the server has already refused this dose
   *  with a 428. The written clinical justification for giving a drug the
   *  patient has a documented allergy to — a de-labelled allergy, a
   *  desensitisation step, a risk/benefit call under supervision.
   *
   *  The server is the authority on all of it: it enforces the minimum length
   *  and the junk-reason filter (`allergy_interlock.validate_override_reason`),
   *  and it refuses the field outright with a 403 unless the caller holds
   *  `medication.prescribe`. Anything this client checks before sending is a
   *  courtesy to the typist, never a control. */
  allergy_override_reason?: string;
}


// ─── Unified patient story (timeline) ───
/* Five types were added on 2026-08-08 when the story engine grew the streams it
   had been missing: lab/vitals OBSERVATIONS from the HL7 ingestion path, care
   BUNDLES, the PROBLEM list, ALLERGIES, and ORDER STATUS TRANSITIONS. Anything
   rendering a per-type label or colour must cover the whole union — an unknown
   type used to be a crash, not a fallback (PatientStory indexed TYPE_META
   directly). */
export type StoryType =
  | 'admission' | 'alert' | 'vitals' | 'note' | 'scribe' | 'imaging'
  | 'medication' | 'order' | 'administration' | 'discharge'
  | 'observation' | 'bundle' | 'problem' | 'allergy' | 'order_event'
  /* Outpatient visits and signed outpatient notes (opd-10), on the story only
     while the server's 'opd' feature is on. */
  | 'opd_visit' | 'opd_consultation';


export interface StoryEvent {
  timestamp: string | null;
  type: StoryType;
  title: string;
  detail: string;
  actor: string | null;
}


/** The story plus THIS clinician's "I have read up to here" watermark. */
export interface StoryFeed {
  events: StoryEvent[];
  /** ISO-8601 UTC, or null when this clinician has never reviewed this chart.
   *  null means "no marker at all" — deliberately NOT "everything is new". */
  last_reviewed_at: string | null;
  /** Events after the watermark, counted server-side over every row. */
  new_count: number;
}


// ─── Discharge ───
/** A completed discharge. The drafted summary comes back for the discharging
 *  doctor to read; the patient sees it only once it is marked reviewed. */
export interface DischargeResult {
  status: 'success';
  summary_id: number;
  discharged_at: string;
  review_status: string;
  /** The secure patient link; null when no link was issued. */
  summary_url: string | null;
  /** The authenticated API path of the QR image, or null when none was made. */
  qr_code_url: string | null;
  secure_link_issued: boolean;
  /** Why no link was issued, when one was not. */
  secure_link_reason: string | null;
  summary: string;
  /** 'unavailable' when the drafting assistant was down and only the recorded
   *  facts were stored. */
  drafted_by: 'assistant' | 'unavailable';
  timeline: DischargeTimelineEntry[];
}


/** The latest discharge summary, clinician-facing, so a draft is returned
 *  too (discharge_service.load_discharge_summary_view). */
export interface DischargeSummaryView {
  summary_id: number;
  summary: string;
  review_status: string;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  timeline: DischargeTimelineEntry[];
  qr_code_url: string | null;
  discharged_at: string;
}


/** One deterioration escalation chain, as the discharge screen lists it. */
export interface EscalationChain {
  id: string;
  patient_id: string;
  status: 'open' | 'acknowledged' | 'resolved' | string;
  raised_at: string;
  peak_news2?: number | null;
  current_tier?: number | null;
  tier_label?: string | null;
}


/** One row of the discharged-patient list (ward-scoped, paginated). */
export interface DischargedPatient {
  id: string;
  name: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  diagnosis: string | null;
  age: number | null;
  sex: string | null;
  admission_date: string | null;
  discharged_at: string | null;
  summary: string | null;
  /** The latest stay, which a final bill is raised against. */
  admission_id: string | null;
  final_bill_raised: boolean;
  has_pending_charges: boolean;
}


// ─── Surgery ───
/** The `surgeries` columns every surgical list sends (`s.*`), plus the
 *  patient fields the lists join on. JSON columns arrive as stored TEXT here;
 *  only the detail route parses them. */
export interface SurgeryRow {
  id: string;
  patient_id: string;
  surgeon_id: string;
  procedure_name: string;
  diagnosis: string;
  /** YYYY-MM-DD, the hospital's local calendar day. */
  scheduled_date: string;
  scheduled_time: string;
  estimated_duration_min: number;
  operating_room: string;
  status: string;
  co_morbidities: string | null;
  surgical_checklist: string | null;
  instruments_required: string | null;
  staff_required: string | null;
  risk_assessment: string | null;
  possible_complications: string | null;
  /** A FRACTION (0-1), not a percentage. */
  success_probability: number | null;
  notes: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  patient_name: string;
  mrn: string;
  age: number;
  sex: string;
}


/** A case on the week view, with the patient's latest risk band. */
export interface SurgeryScheduleCase extends SurgeryRow {
  surgeon_name: string | null;
  /** 'stable' when the patient has no risk row. */
  risk_level: string;
  risk_score: number | null;
}


/** One day of the week view. `is_today` is decided in the hospital's
 *  timezone. */
export interface SurgeryScheduleDay {
  date: string;
  /** The weekday name. */
  day_label: string;
  is_today: boolean;
  surgeries: SurgeryScheduleCase[];
}


/** Context imported with an emergency case (`notes.imported_context`). */
export interface SurgeryImportedContext {
  medications?: string[];
  vitals?: string[];
  recent_labs?: string[];
  scribe_notes?: string;
  [key: string]: unknown;
}


/** GET /surgery/{id}: one case in full, with its JSON columns parsed.
 *
 *  `bed`, `ward` and `admission_date` are the patient's, null when not
 *  recorded (the route did not select them at all until 4 October 2026, so the
 *  case header read "Bed undefined"); `risk_drivers` is the latest risk row's
 *  drivers, parsed. `surgical_checklist` is always a list of prep steps: the WHO
 *  checklist has its own column since migration 76 (it used to be stored here
 *  as an object, which crashed the panel), and the route sends [] for anything
 *  that is not a list. */
export interface SurgeryDetail {
  id: string;
  patient_id: string;
  surgeon_id: string;
  surgeon_name: string | null;
  patient_name: string;
  age: number;
  sex: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  patient_diagnosis: string;
  admission_date: string | null;
  procedure_name: string;
  diagnosis: string;
  scheduled_date: string;
  scheduled_time: string;
  estimated_duration_min: number;
  operating_room: string;
  status: string;
  co_morbidities: string[];
  /** The raw patient_profiles column the route falls back to. */
  comorbidities: string | null;
  allergies: string[];
  surgical_checklist: string[];
  instruments_required: string[];
  staff_required: { role: string; name: string }[];
  risk_assessment: string | null;
  possible_complications: string[];
  success_probability: number | null;
  /** 'stable' when the patient has no risk row. */
  risk_level: string;
  risk_score: number | null;
  risk_drivers: string[];
  notes: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  /** Minutes from start to completion; null when not measured. */
  turnaround_min: number | null;
  imported_context?: SurgeryImportedContext;
}


// ─── WHO Surgical Safety Checklist ───
/** The static WHO 2009 checklist: three phases of items. */
export interface WhoChecklistTemplate {
  version: string;
  phases: {
    id: string;
    label: string;
    when: string;
    items: { id: string; text: string }[];
  }[];
}


/** A case's checklist: who attested each ticked item, and when. Unticking
 *  deletes the item's key. */
export interface WhoChecklistState {
  version: string;
  state: Record<string, { checked: boolean; by: string; at: string }>;
  progress: { checked: number; total: number };
  completed: boolean;
}


/** An emergency case awaiting the theatre team. */
export interface EmergencySurgeryCase extends SurgeryRow {
  bed: string | null;
  surgeon_name: string | null;
}


// ─── Surgery Cases ───
/** A `surgery_team_assignments` row. */
export interface SurgeryTeamAssignment {
  id: number;
  surgery_id: string;
  user_id: string;
  role_name: string;
  status: 'pending' | 'alerted' | 'confirmed' | 'declined';
  updated_at: string;
}


/** An active (not completed, not cancelled) case with its team. */
export interface SurgeryCase extends SurgeryRow {
  team: SurgeryTeamAssignment[];
}


// ─── Admin / Governance ───
export interface AuditFilters {
  correlation_id?: string;
  patient_id?: string;
  action_type?: string;
  event_category?: string;
  outcome?: string;
  start_date?: string;
  end_date?: string;
  limit?: number;
  offset?: number;
}


export interface AuditLog {
  id: number;
  timestamp: string;
  correlation_id: string;
  event_category: string;
  action_type: string;
  outcome: string;
  actor_user_id: string | null;
  actor_role: string | null;
  /** The actor's name, read from users when the log is read (never stored in
   *  the chain). Null when the user no longer exists. */
  actor_name?: string | null;
  patient_id: string | null;
  source_type: string | null;
  summary_message: string | null;
  metadata_json: string | null;
  target_type: string | null;
  target_id: string | null;
  request_path: string | null;
  ip_address: string | null;
}


export interface AuditLogsResponse {
  logs: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}


/** Alerts fired in the last 24 h, from the shadow event log. `total: null`
 *  means UNKNOWN (the instrument is off or unreadable) and must never be
 *  shown as 0; `unknown_reason` says why. */
export interface AdminAlertCounts {
  total: number | null;
  known: boolean;
  unknown_reason: string | null;
  by_lane: Record<string, number>;
  by_level: Record<string, number>;
  source: string;
  /** The ingest lane's own audit rows, which are not a hospital-wide count. */
  audit_trail: { vigil: number; relay: number; lane: string; note: string };
}


/** Clinical rows written in the last 24 h. `total: null` means the figure is
 *  not known; `counted` is then a floor, rendered as "at least N". */
export interface AdminRecordCounts {
  total: number | null;
  counted: number;
  vitals_counted: number;
  observations_counted: number;
  by_source: Record<string, number>;
  complete: boolean;
  incomplete_reason: string | null;
  source: string;
}


/** GET /admin/overview: the governance snapshot. Counts are for 24 hours. */
export interface AdminOverviewData {
  ingestion_mode: string;
  audit_simulation_enabled: boolean;
  audit_total_24h: number;
  audit_categories: Record<string, number>;
  audit_outcomes: Record<string, number>;
  ingestion: {
    total: number;
    parsed: number;
    failed: number;
    duplicates: number;
    total_vitals: number;
    total_observations: number;
  };
  source_mix: Record<string, number>;
  alerts: AdminAlertCounts;
  records_written: AdminRecordCounts;
  recent_failures: {
    id: number;
    received_at: string;
    source_type: string;
    parse_error: string | null;
    correlation_id: string | null;
  }[];
  auth_events: { action: string; outcome: string; count: number }[];
}


/** One `model_registry` row with its description. Integer flags are 0/1.
 *  `task`, `training_data` and `notes` are nullable columns, typed as the
 *  Model Governance screen reads them. */
export interface ModelRegistryEntry {
  id: number;
  model_key: string;
  version: string;
  task: string;
  training_data: string;
  validated: number;
  validation_metrics: Record<string, unknown> | string | null;
  calibration_stats: Record<string, unknown> | string | null;
  is_active: number;
  checkpoint_present: number;
  notes: string;
  registered_at: string;
  display_name?: string;
  role?: string | null;
  impacts?: string[];
}


/** One ingested payload as the Ingestion Monitor lists it. */
export interface IngestionEvent {
  id: number;
  received_at: string;
  source_type: string;
  source_name: string | null;
  parse_status: 'received' | 'parsed' | 'partially_parsed' | 'failed' | 'duplicate';
  parse_error: string | null;
  correlation_id: string | null;
  vitals_inserted: number | null;
  observations_inserted: number | null;
  processed_at: string | null;
}



// ─── Discharge Access (Patient) ───
// Both calls THROW the parsed error body on failure (`{detail: {status:
// 'error', reason, ...}}`), not an Error, so the page can read `reason`.

/** A short-lived grant to read one discharge summary. */
export interface DischargeAccessGrant {
  status: 'success';
  access_grant: string;
  patient_id: string;
}


/** The discharge summary as the patient reads it through the secure link.
 *  `summary` is null until a clinician has reviewed the draft. */
export interface VerifiedDischargeSummary {
  status: 'success';
  summary: string | null;
  summary_status: 'draft' | 'reviewed';
  summary_message: string | null;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  discharged_at: string;
  timeline: DischargeTimelineEntry[];
  /** Empty when the patient row is gone. */
  patient: {
    mrn?: string;
    name?: string;
    age?: number | null;
    sex?: string | null;
    ward?: string | null;
    bed?: string | null;
    diagnosis?: string | null;
    admission_date?: string | null;
    attending_doctor_name?: string | null;
    admission_id?: string | null;
  };
  /** Null when the allergy list could not be read, which is not "none". */
  allergies: { substance: string; reaction: string | null; severity: string | null }[] | null;
  comorbidities: string[];
  /** The reconciled continued list, excluding orders still needing a
   *  pharmacist's verification. */
  medications: {
    name: string;
    drug_name: string;
    dosage: string;
    route: string;
    frequency: string;
    status: 'active' | 'prn';
  }[];
  unverified_medication_count: number;
  /** False on an Orb Ward Record computer: the page prints no medicines
   *  section. Absent from an older server. */
  drug_chart?: boolean;
  site_name: string | null;
}

// ─── Helix Pharmacy ───
/** A `helix_patient_med_orders` row as stored. */
export interface HelixOrderRow {
  id: string;
  patient_id: string | null;
  medication_id: string | null;
  status: 'pending' | 'reviewing' | 'verified' | 'rejected' | 'substitute_suggested' | 'scheduled' | 'dispensed' | 'delivered';
  prescribed_by: string | null;
  dosage: string | null;
  frequency: string | null;
  route: string | null;
  substitute_medication_id: string | null;
  pharmacist_notes: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  scheduled_at: string | null;
  last_transition_id: string | null;
  /** Set only by the ward, through receiveHelixOrder. */
  received_by: string | null;
  received_at: string | null;
  created_at: string;
}


/** A Helix order as the pharmacy lists it: the row plus the names it shows. */
export interface HelixOrderListRow extends HelixOrderRow {
  medication_name: string;
  patient_name: string;
  prescribed_by_name: string | null;
  ward: string | null;
  bed: string | null;
  substitute_name: string | null;
}


/** One step in an order's history (`helix_transitions`). `payload` is the
 *  stored JSON TEXT. */
export interface HelixTransition {
  id: string;
  order_id: string;
  from_status: string;
  to_status: string;
  idempotency_key: string | null;
  actor_id: string | null;
  payload: string | null;
  created_at: string;
}


/** A transition or receipt that was applied, or (`already_applied`) one whose
 *  idempotency key had been used, returned as first recorded. `order` is the
 *  stored row, empty if it has since gone. */
export interface HelixTransitionResult {
  ok: true;
  order: Partial<HelixOrderRow>;
  transition: HelixTransition;
  already_applied: boolean;
}


export interface HelixOrderSafety {
  order_id: string;
  medication_id: string;
  drug_name: string;
  is_substitute: boolean;
  safety: MedicationSafety & { screen_failed?: boolean };
}


// ─── Ward receipt ───
// The other end of a dispense. Separate route and separate permission from the
// pharmacy transitions above (`helix.receive`, which the pharmacist does not
// hold), because the ward attesting that a drug arrived is a different act by a
// different person than pharmacy sending it.

export interface AwaitingReceiptOrder {
  id: string;
  patient_id: string;
  patient_name: string;
  /** The drug that was dispensed: the substitute when pharmacy set one. */
  medication_name: string | null;
  /** The prescribed drug, present only when a substitute was dispensed. */
  original_medication_name?: string | null;
  strength?: string | null;
  form?: string | null;
  dosage: string | null;
  route: string | null;
  frequency: string | null;
  ward: string | null;
  bed: string | null;
  pharmacist_notes?: string | null;
  dispensed_by: string | null;
  dispensed_by_name: string | null;
  dispensed_at: string | null;
}


/** A `helix_stock_movements` ledger row, newest first. The columns from
 *  `movement_type` on were added by the pharmacy store (migration 54) and are
 *  null on older rows. */
export interface HelixStockMovement {
  id: string;
  lot_id: string | null;
  medication_id: string | null;
  order_id: string | null;
  quantity_change: number | null;
  reason: string | null;
  timestamp: string;
  movement_type: string | null;
  store_id: number | null;
  balance_after_units: number | null;
  unit_cost_paise: number | null;
  reference_type: string | null;
  reference_id: string | null;
  actor_id: string | null;
  remarks: string | null;
  idempotency_key: string | null;
  occurred_at: string | null;
  seq: number | null;
  medication_name: string | null;
  lot_number: string | null;
}
