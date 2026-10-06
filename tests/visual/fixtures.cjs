/**
 * Orb visual-audit harness — deterministic mock API fixtures.
 *
 * Contract sources (scratchpad maps + code):
 *   - map-backend-api.md          exact response envelopes per endpoint
 *   - map-clinician-ui.md         which fields each clinician panel reads
 *   - map-admin-pharm-patient.md  admin / pharmacist / patient surfaces
 *   - src/api/client.ts:169-195   SSE framing (`data: {...}\n\n` events)
 *
 * Interface:
 *   fixtureFor(method, pathname, searchParams) ->
 *     null                                    (no fixture; driver 404s)
 *     { status?, contentType?, body }         (objects JSON.stringify'd by driver)
 *
 * All timestamps are fixed in May 2026 — no Date.now(), no Math.random().
 */
'use strict';

// ─── Time helpers (fixed "today" = Saturday 2026-05-23) ───
const D = '2026-05-23';
const t = (hm) => `${D}T${hm}:00`;                 // t('09:15') -> today 09:15
const onDay = (day, hm) => `2026-05-${day}T${hm}:00`; // onDay('20','14:30')

// ─── SSE helper — each frame becomes `data: <json|string>\n\n` (client.ts:169-195) ───
function sse(frames) {
  const body = frames
    .map((f) => `data: ${typeof f === 'string' ? f : JSON.stringify(f)}\n\n`)
    .join('');
  return { contentType: 'text/event-stream', body };
}
const ok = (body) => ({ body });

// ═══════════════════════════════════════════════════════════════════════════
// AUTH — map-backend-api.md:224 (login), :18-22 (/me shape)
// ═══════════════════════════════════════════════════════════════════════════
const AUTH_USER = {
  id: 'DOC001',
  name: 'Dr. Meera Sharma',
  role: 'doctor',
  department: 'General Medicine',
  registration_number: null,
};

// ═══════════════════════════════════════════════════════════════════════════
// VIGIL patient list — bare array, map-backend-api.md:27-31.
// Covers risk_level critical / elevated / stable / null. PAT007 must have
// department + admission_department 'General Surgery' (VigilPanel.tsx:74,158
// reads admission_department for the emergency-surgery row). Names follow
// server/scripts/seed_clinical_data.py style.
// ═══════════════════════════════════════════════════════════════════════════
function vp(o) {
  return Object.assign(
    { attending_doctor_name: 'Dr. Meera Sharma', department: 'General Medicine' },
    o,
    { admission_department: o.admission_department || o.department || 'General Medicine' },
  );
}
const VIGIL_PATIENTS = [
  vp({ id: 'PAT002', name: 'Kavitha Menon', mrn: 'MRN-CC002', bed: 'CCU-02', ward: 'ICU',
    department: 'Cardiology', admission_date: onDay('20', '14:30'),
    diagnosis: 'AF with decompensated heart failure', age: 58, sex: 'F',
    risk_score: 0.86, risk_level: 'critical',
    risk_drivers: ['HR 132', 'SpO2 89%', 'RR 28'], attending_doctor_name: 'Dr. Arjun Mehta' }),
  vp({ id: 'PAT003', name: 'Rajesh Iyer', mrn: 'MRN-GM002', bed: 'GM-102', ward: 'W1',
    admission_date: onDay('19', '10:05'), diagnosis: 'COPD exacerbation', age: 67, sex: 'M',
    risk_score: 0.78, risk_level: 'critical',
    risk_drivers: ['SpO2 88% on 2L', 'RR 26', 'Accessory muscle use'] }),
  vp({ id: 'PAT001', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'W1',
    admission_date: onDay('21', '08:00'), diagnosis: 'CAP with early sepsis', age: 55, sex: 'M',
    risk_score: 0.62, risk_level: 'elevated',
    risk_drivers: ['RR 26', 'Temp 38.6°C', 'SpO2 92% on air'] }),
  vp({ id: 'PAT007', name: 'Mohan Das', mrn: 'MRN-GS001', bed: 'SG-201', ward: 'W2',
    department: 'General Surgery', admission_department: 'General Surgery',
    admission_date: t('05:40'), diagnosis: 'Acute appendicitis with perforation', age: 46, sex: 'M',
    risk_score: 0.58, risk_level: 'elevated',
    risk_drivers: ['HR 110', 'Temp 38.6°C', 'Guarding RIF'],
    attending_doctor_name: 'Dr. Vikram Rathore' }),
  vp({ id: 'PAT004', name: 'Ananya Kapoor', mrn: 'MRN-GM001', bed: 'GM-101', ward: 'W1',
    admission_date: onDay('22', '19:20'), diagnosis: 'Diabetic ketoacidosis', age: 34, sex: 'F',
    risk_score: 0.55, risk_level: 'elevated', risk_drivers: ['HR 118', 'RR 28'] }),
  vp({ id: 'PAT006', name: 'Lakshmi Devi', mrn: 'MRN-GM005', bed: 'GM-105', ward: 'W3',
    admission_date: onDay('18', '16:45'), diagnosis: 'UTI with delirium', age: 80, sex: 'F',
    risk_score: 0.31, risk_level: 'stable', risk_drivers: ['Temp 38.4°C'] }),
  vp({ id: 'PAT005', name: 'Priya Nair', mrn: 'MRN-GM003', bed: 'GM-103', ward: 'W3',
    admission_date: onDay('21', '22:10'), diagnosis: 'Dengue fever with thrombocytopenia',
    age: 28, sex: 'F', risk_score: 0.25, risk_level: 'stable', risk_drivers: [] }),
  vp({ id: 'PAT008', name: 'Farhan Sheikh', mrn: 'MRN-OR001', bed: 'OR-12', ward: 'W2',
    department: 'Orthopedics', admission_date: t('07:55'),
    diagnosis: 'Femur shaft fracture, pre-op', age: 41, sex: 'M',
    risk_score: null, risk_level: null, risk_drivers: [],
    attending_doctor_name: 'Dr. Sanjay Rao' }),
];
const patientById = (id) => VIGIL_PATIENTS.find((p) => p.id === id) || VIGIL_PATIENTS[2];

// ─── 24 vitals points spanning 6h (03:30 → 09:15, 15-min cadence), trending
//     abnormal toward the end. Shape: map-backend-api.md:41,51. ───
const VITALS_24 = (() => {
  const HR = [88, 89, 91, 90, 92, 94, 93, 95, 97, 99, 98, 101, 103, 105, 104, 107, 109, 111, 110, 113, 115, 117, 116, 118];
  const SBP = [128, 126, 127, 124, 125, 122, 123, 120, 121, 118, 119, 116, 117, 114, 115, 112, 113, 110, 111, 108, 109, 106, 107, 104];
  const DBP = [82, 81, 82, 80, 81, 79, 80, 78, 79, 77, 78, 76, 77, 75, 76, 74, 75, 73, 74, 72, 73, 71, 72, 70];
  const SPO2 = [97, 97, 96, 97, 96, 96, 95, 96, 95, 95, 94, 95, 94, 94, 93, 94, 93, 93, 92, 93, 92, 92, 91, 92];
  const TEMP = [37.1, 37.2, 37.2, 37.3, 37.4, 37.4, 37.5, 37.6, 37.6, 37.7, 37.8, 37.8, 37.9, 38.0, 38.0, 38.1, 38.2, 38.2, 38.3, 38.4, 38.4, 38.5, 38.6, 38.6];
  const RR = [16, 16, 17, 17, 18, 18, 18, 19, 19, 20, 20, 21, 21, 22, 22, 22, 23, 23, 24, 24, 25, 25, 26, 26];
  const out = [];
  for (let i = 0; i < 24; i++) {
    const total = 210 + i * 15; // minutes since midnight; 210 = 03:30
    const hh = String(Math.floor(total / 60)).padStart(2, '0');
    const mm = String(total % 60).padStart(2, '0');
    out.push({
      timestamp: `${D}T${hh}:${mm}:00`,
      heart_rate: HR[i], systolic_bp: SBP[i], diastolic_bp: DBP[i],
      spo2: SPO2[i], temperature: TEMP[i], respiratory_rate: RR[i],
    });
  }
  return out;
})();

// ─── 12 hourly risk_history points (2026-05-22 22:00 → today 09:00) ───
const RISK_HISTORY = (() => {
  const scores = [3, 3, 4, 4, 4, 5, 5, 5, 5, 6, 6, 6];
  const levels = (s) => (s >= 7 ? 'critical' : s >= 5 ? 'elevated' : 'stable');
  const out = [];
  for (let i = 0; i < 12; i++) {
    const hour = 22 + i; // 22,23 on May 22 then 0..9 on May 23
    const ts = hour < 24
      ? `2026-05-22T${String(hour).padStart(2, '0')}:00:00`
      : `${D}T${String(hour - 24).padStart(2, '0')}:00:00`;
    out.push({
      score: scores[i], level: levels(scores[i]),
      drivers: scores[i] >= 5 ? ['RR trending up', 'Low-grade fever'] : [],
      timestamp: ts,
    });
  }
  return out;
})();

const LENS_ANALYSIS_ROW = {
  id: 301, patient_id: 'PAT001', author_id: 'DOC001',
  image_path: 'uploads/lens_mock_cxr.jpg', image_type: 'xray',
  clinical_context: 'Fever and productive cough, day 2 of admission',
  analysis: 'Impression: right lower zone consolidation consistent with pneumonia. No effusion. Cardiac silhouette normal.',
  findings_json: null, created_at: t('07:10'),
};

const SCRIBE_NOTE_DRAFT = {
  id: 501, patient_id: 'PAT001', author_id: 'DOC001',
  raw_transcript:
    '55 year old gentleman day two of community acquired pneumonia, still febrile at 38.6, respiratory rate mid twenties, sats 92 on room air, continue piptaz, add paracetamol PRN, repeat bloods in the morning.',
  structured_note:
    'S: Day 2 CAP. Persistent fever, mildly worse work of breathing.\nO: T 38.6°C, RR 26, SpO2 92% RA, HR 118. Right basal crackles.\nA: CAP with early sepsis — not yet improving on piperacillin-tazobactam.\nP: Continue IV antibiotics, paracetamol PRN, repeat CBC/CRP in AM, escalate if NEWS2 ≥ 7.',
  note_format: 'soap', audio_path: null,
  // JSON STRINGS in the vigil patient-detail envelope (map-backend-api.md:43,300)
  verification_json: JSON.stringify({
    status: 'complete', overall: 'pass',
    claims: [
      { text: 'Temperature 38.6°C documented', verdict: 'supported' },
      { text: 'SpO2 92% on room air', verdict: 'supported' },
    ],
    unsupported_count: 0,
  }),
  pipeline_meta_json: JSON.stringify({
    asr_model: 'whisper-small', correction_model: 'orb-correct-v1',
    draft_model: 'orb-draft-v1', duration_ms: 5120,
  }),
  created_at: t('08:20'), status: 'draft', version: 1, signed_by: null, ai_generated: 1,
};

const DETAIL_MEDICATIONS = [
  { id: 11, patient_id: 'PAT001', drug_name: 'Piperacillin-Tazobactam', dosage: '4.5g',
    route: 'IV', frequency: 'Q6H', status: 'active', prescribed_by: 'DOC001',
    notes: null, started_at: onDay('21', '09:00'), unverified: false },
  { id: 12, patient_id: 'PAT001', drug_name: 'Azithromycin', dosage: '500mg',
    route: 'PO', frequency: 'OD', status: 'active', prescribed_by: 'DOC001',
    notes: 'Extracted from admission note by Scribe', started_at: onDay('21', '10:15'),
    unverified: true },
  { id: 13, patient_id: 'PAT001', drug_name: 'Paracetamol', dosage: '1g',
    route: 'PO', frequency: 'PRN', status: 'active', prescribed_by: 'DOC001',
    notes: 'For fever > 38°C', started_at: onDay('21', '09:05'), unverified: false },
];

// ─── Vigil patient detail — map-backend-api.md:35-49. PAT001 is canonical;
//     any other id gets the same rich envelope re-keyed to that patient. ───
function vigilPatientDetail(id) {
  const p = patientById(id);
  return {
    patient: {
      id: p.id, user_id: `USR-${p.id}`, name: p.name, mrn: p.mrn, bed: p.bed,
      ward: p.ward, admission_date: p.admission_date, diagnosis: p.diagnosis,
      status: 'admitted', age: p.age, sex: p.sex, date_of_birth: '1971-02-14',
      phone: '+91-98450-11223', emergency_contact_name: 'Radhika Reddy',
      emergency_contact_phone: '+91-98450-33445', address: '14 Lakeview Road, Bengaluru',
      attending_doctor_id: 'DOC001', attending_doctor_name: p.attending_doctor_name,
      created_at: p.admission_date,
    },
    risk: {
      score: 0.62, level: 'elevated', raw_news2: 6,
      drivers: ['RR 26 — worsening respiratory effort', 'SpO2 92% on room air', 'Temp 38.6°C'],
      scoring_model: 'NEWS2',
    },
    trend: { trend: 'worsening', hr_delta: 6.5, sbp_delta: -8.0, spo2_delta: -1.2 },
    vitals: VITALS_24,
    notes: [
      { id: 41, patient_id: p.id, doc_type: 'Progress Note', title: 'Morning ward round',
        content: 'Day 2 of CAP. Fever persists; chest signs unchanged. Continue current antibiotics and reassess this evening.',
        author_id: 'DOC001', created_at: t('08:05') },
      { id: 40, patient_id: p.id, doc_type: 'Admission Note', title: 'Admission assessment',
        content: 'Admitted with fever, productive cough and confusion. CURB-65 = 2. Started on IV piperacillin-tazobactam.',
        author_id: 'DOC001', created_at: onDay('21', '08:30') },
    ],
    scribe_notes: [Object.assign({}, SCRIBE_NOTE_DRAFT, { patient_id: p.id })],
    risk_history: RISK_HISTORY,
    lens_analyses: [Object.assign({}, LENS_ANALYSIS_ROW, { patient_id: p.id })],
    medications: DETAIL_MEDICATIONS.map((m) => Object.assign({}, m, { patient_id: p.id })),
  };
}

// ─── Discharged patients — bare array of patients columns + discharged_at/summary
//     (map-backend-api.md:33) ───
const DISCHARGED = [
  { id: 'PAT101', user_id: 'USR-PAT101', name: 'Nikhil Menon', mrn: 'MRN-GM090',
    bed: 'GM-110', ward: 'W1', admission_date: onDay('15', '09:00'),
    diagnosis: 'Acute gastroenteritis', status: 'discharged', age: 39,
    date_of_birth: '1987-01-05', sex: 'M', phone: '+91-98860-55667',
    emergency_contact_name: 'Divya Menon', emergency_contact_phone: '+91-98860-77889',
    address: '3 Rose Garden Street, Mysuru', attending_doctor_id: 'DOC001',
    created_at: onDay('15', '09:00'), discharged_at: onDay('18', '12:30'),
    summary: 'Rehydrated, tolerating oral intake. Discharged with ORS advice and OPD review in one week.' },
  { id: 'PAT102', user_id: 'USR-PAT102', name: 'Rohini Kulkarni', mrn: 'MRN-CC050',
    bed: 'CCU-05', ward: 'ICU', admission_date: onDay('12', '17:40'),
    diagnosis: 'NSTEMI, medically managed', status: 'discharged', age: 61,
    date_of_birth: '1964-11-21', sex: 'F', phone: '+91-99010-22334',
    emergency_contact_name: 'Ashok Kulkarni', emergency_contact_phone: '+91-99010-44556',
    address: '78 MG Road, Pune', attending_doctor_id: 'DOC002',
    created_at: onDay('12', '17:40'), discharged_at: onDay('19', '11:00'),
    summary: 'Chest pain settled on dual antiplatelets and statin. Cardiology OPD in 2 weeks.' },
];

// ─── Medication reconciliation — map-backend-api.md:57-60; all buckets non-empty ───
const MED_RECONCILIATION = {
  continued: [
    { name: 'Piperacillin-Tazobactam', dosage: '4.5g', route: 'IV', frequency: 'Q6H',
      source: 'clinician', prn: false },
    { name: 'Paracetamol', dosage: '1g', route: 'PO', frequency: 'PRN',
      source: 'clinician', prn: true },
  ],
  stopped: [
    { name: 'Noradrenaline', dosage: '0.1mcg/kg/min', route: 'IV', frequency: 'Infusion',
      source: 'clinician', prn: false },
  ],
  unverified: [
    { name: 'Azithromycin', dosage: '500mg', route: 'PO', frequency: 'OD',
      source: 'llm_extracted', prn: false, needs_verification: true },
  ],
  counts: { continued: 2, stopped: 1, unverified: 1 },
};

// ─── Discharge timeline events — {timestamp,title,detail} (DischargePrintView.tsx:161-166) ───
const DISCHARGE_TIMELINE = [
  { timestamp: onDay('21', '08:30'), title: 'Admission',
    detail: 'Admitted via ED with fever, cough and confusion. IV antibiotics started.' },
  { timestamp: onDay('22', '09:10'), title: 'Clinical review',
    detail: 'Fever trending down, inflammatory markers improving. Continue IV therapy.' },
  { timestamp: t('09:00'), title: 'Discharge decision',
    detail: 'Afebrile 24h, mobilising independently. Switched to oral antibiotics for discharge.' },
];

function dischargeSummaryFor(id) {
  const p = patientById(id);
  return {
    summary:
      `${p.name} (${p.mrn}) was admitted on ${p.admission_date.slice(0, 10)} with ${p.diagnosis}. ` +
      'The patient responded to treatment, remained haemodynamically stable, and was discharged with oral medications and OPD follow-up in one week.',
    timeline: DISCHARGE_TIMELINE,
    qr_code_url: `/api/vigil/patients/${id}/qr`,
    discharged_at: t('09:30'),
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SAGE — insights {insights:[...]} (map-backend-api.md:93-97), history
// {messages:[...]} (:92), query/voice SSE (:253, client.ts:528-599)
// ═══════════════════════════════════════════════════════════════════════════
const SAGE_INSIGHTS = {
  insights: [
    { id: 'ins-0001', patient_id: 'PAT002', insight_type: 'deterioration_risk',
      severity: 'critical', title: 'Rapid AF with falling SpO2',
      detail: 'Heart rate 132 with SpO2 89% over the last hour. Consider urgent senior review and rate control.',
      suggested_action_type: 'escalate_review', suggested_action_target: 'PAT002',
      target_user_id: null, dedup_key: 'PAT002:deterioration', state: 'active',
      created_at: t('08:55'), updated_at: t('09:05'), patient_name: 'Kavitha Menon' },
    { id: 'ins-0002', patient_id: 'PAT001', insight_type: 'medication_gap',
      severity: 'warning', title: 'Unverified AI-charted antibiotic',
      detail: 'Azithromycin 500mg OD was extracted by Scribe and has not been pharmacist-verified.',
      suggested_action_type: 'add_medication', suggested_action_target: 'Azithromycin 500mg PO OD',
      target_user_id: null, dedup_key: 'PAT001:unverified-med', state: 'active',
      created_at: t('08:30'), updated_at: t('08:30'), patient_name: 'Suresh Reddy' },
    { id: 'ins-0003', patient_id: 'PAT005', insight_type: 'lab_followup',
      severity: 'info', title: 'Repeat platelet count due',
      detail: 'Dengue with thrombocytopenia — last platelet count was 14 hours ago; repeat is due this morning.',
      suggested_action_type: 'order_lab', suggested_action_target: 'CBC with platelets',
      target_user_id: null, dedup_key: 'PAT005:platelets', state: 'active',
      created_at: t('07:45'), updated_at: t('07:45'), patient_name: 'Priya Nair' },
  ],
};

const SAGE_HISTORY = {
  messages: [
    { role: 'user', content: 'Summarise the sickest patients on my list.' },
    { role: 'assistant', content: 'Kavitha Menon (CCU-02) is in rapid AF with SpO2 89% — highest concern. Rajesh Iyer (GM-102) remains hypoxic on 2L. Suresh Reddy (GM-104) has a rising NEWS2 of 6.' },
    { role: 'user', content: 'What was Suresh Reddy admitted with?' },
  ],
};

const SAGE_QUERY_SSE = sse([
  { content: 'Based on the last 6 hours of observations, ' },
  { content: 'Suresh Reddy shows a rising NEWS2 of 6 driven by RR 26, SpO2 92% and temperature 38.6°C. ' },
  { content: 'Recommend increasing observation frequency to hourly and a senior review if NEWS2 reaches 7.' },
  { metadata: {
    action_state: 'none',
    sources: [{ title: 'NEWS2 — Royal College of Physicians 2017', ref: 'news2-guideline' }],
    confidence: 0.82, escalation: false, display_mode: 'text', query_scope: 'patient',
    patient_context_used: true, patient_id: 'PAT001', patient_name: 'Suresh Reddy',
    timing: { retrieval_ms: 42, ttft_ms: 180, total_ms: 950 },
  } },
  { structured_response: {
    concise_answer: 'NEWS2 is 6 (elevated); increase observations to hourly.',
    clinical_assessment: 'Early sepsis physiology not yet resolving on current antibiotics.',
  } },
  { done: true },
]);

const SAGE_VOICE_SSE = sse([
  { token: 'Suresh Reddy ' },
  { token: 'is elevated risk with a NEWS2 of six. ' },
  { token: 'Observations should move to hourly.' },
  { done: true, timing: { ttft_ms: 120, total_ms: 640, tokens: 3 } },
]);

// Ambient ward brief — WardBrief type (client.ts) / sage_narrator_service.py
// _build_ward_snapshot(): risk_distribution keys and watchlist levels are the
// narrator's critical/elevated/moderate/stable vocabulary; snapshot must be
// non-null or WardBriefCard renders nothing. Names/beds match VIGIL_PATIENTS.
const SAGE_WARD_BRIEF = {
  snapshot: {
    census: 8,
    risk_distribution: { critical: 1, elevated: 1, moderate: 2, stable: 4 },
    watchlist: [
      { patient_id: 'PAT002', name: 'Kavitha Menon', bed: 'CCU-02', ward: 'ICU',
        level: 'critical', score: 9 },
      { patient_id: 'PAT001', name: 'Suresh Reddy', bed: 'GM-104', ward: 'W1',
        level: 'elevated', score: 6 },
    ],
    recent_events: [
      { topic: 'vigil', event_type: 'alert_created', patient_id: 'PAT002', timestamp: t('08:55') },
      { topic: 'labs', event_type: 'result_posted', patient_id: 'PAT001', timestamp: t('08:40') },
    ],
    active_alerts: 2,
    as_of: t('09:00'),
  },
  narrative: 'Eight admitted. Kavitha Menon (CCU-02) is the priority — rapid AF with falling SpO2, urgent senior review advised. Suresh Reddy (GM-104) is trending up on NEWS2 with an unverified antibiotic pending pharmacist review. The remaining six patients are stable.',
  narrative_engine: 'medgemma-4b',
  generated_at: t('09:00'),
};

// AI governance posture: SageAiPosture type (client.ts). There is no cloud
// speech; local_asr_available says whether on-appliance transcription can run.
const SAGE_AI_POSTURE = {
  provider_mode: 'local',
  hosted_enabled: false,
  hosted_configured: false,
  hosted_host_allowlisted: false,
  phi_egress_approved: false,
  local_model: 'medgemma-4b',
  local_asr_available: false,
};

// ═══════════════════════════════════════════════════════════════════════════
// RELAY — channels/alerts/messages are BARE ARRAYS (map-backend-api.md:68-88).
// alert / ambient_action content is a STRINGIFIED JSON payload; AlertsSection
// reads payload.is_structured/.title/.action_required/.patient_id/.surgery_id
// (AlertsSection.tsx:36-66); the in-channel alert card also reads patient_name,
// mrn, procedure_name, operating_room, scheduled_time, status, resolved_by
// (RelayPanel.tsx:398-428).
// ═══════════════════════════════════════════════════════════════════════════
const RELAY_CHANNELS = [
  { id: 'CH-PAT002', name: 'Case: Kavitha Menon', type: 'case_room', patient_id: 'PAT002',
    mrn: 'MRN-CC002', diagnosis: 'AF with decompensated heart failure', ward: 'ICU',
    bed: 'CCU-02', risk_score: 0.86, risk_level: 'critical', unread_count: 3,
    last_message: 'Sage: BNP resulted at 1840 — trending review suggested.',
    last_activity: t('09:14'), alert_count: 1, has_alert: true },
  { id: 'CH-PAT001', name: 'Case: Suresh Reddy', type: 'case_room', patient_id: 'PAT001',
    mrn: 'MRN-GM004', diagnosis: 'CAP with early sepsis', ward: 'W1', bed: 'GM-104',
    risk_score: 61.5, risk_level: 'elevated', unread_count: 0,
    last_message: 'Continue current antibiotics, reassess this evening.',
    last_activity: t('08:40'), alert_count: 0, has_alert: false },
  { id: 'DEPT-GM', name: 'General Medicine', type: 'department', patient_id: null,
    mrn: null, diagnosis: null, ward: null, bed: null, risk_score: null, risk_level: null,
    unread_count: 1, last_message: 'Bed huddle moved to 10:00 today.',
    last_activity: t('08:15'), alert_count: 0, has_alert: false },
  { id: 'DEPT-CARD', name: 'Cardiology', type: 'department', patient_id: null,
    mrn: null, diagnosis: null, ward: null, bed: null, risk_score: null, risk_level: null,
    unread_count: 0, last_message: 'Cath lab list finalised for Monday.',
    last_activity: onDay('22', '18:20'), alert_count: 0, has_alert: false },
];

const RELAY_ALERTS = [
  { id: 9101, channel_id: 'CH-PAT002', sender_id: 'orb-vigil', sender_name: 'Orb Vigil',
    sender_role: 'system', message_type: 'alert', timestamp: t('09:02'),
    content: JSON.stringify({
      is_structured: true, title: 'Critical deterioration — Kavitha Menon',
      patient_id: 'PAT002', patient_name: 'Kavitha Menon', mrn: 'MRN-CC002',
      action_required: 'HR 132 with SpO2 89%. Urgent bedside review and rate control required.',
      status: null, resolved_by: null,
      target_users: [], target_roles: ['doctor', 'nurse'], target_departments: ['Cardiology'],
    }) },
  { id: 9102, channel_id: 'DEPT-GM', sender_id: 'orb-surgery', sender_name: 'Orb Surgical Suite',
    sender_role: 'system', message_type: 'alert', timestamp: t('08:47'),
    content: JSON.stringify({
      is_structured: true, surgery_id: 'SUR-EM-01',
      title: 'Emergency laparotomy — team confirmation needed',
      patient_id: 'PAT007', patient_name: 'Mohan Das', mrn: 'MRN-GS001',
      procedure_name: 'Emergency laparoscopic appendicectomy', operating_room: 'OT-2',
      scheduled_time: '10:30',
      action_required: 'Confirm availability for emergency case in OT-2 at 10:30.',
      status: null, resolved_by: null,
      target_users: ['DOC004'], target_roles: ['doctor'], target_departments: ['General Surgery'],
    }) },
];

// 8 messages ascending by time — types text / alert (stringified) /
// ambient_action (stringified) / system, plus one is_bot ambient message.
const RELAY_MESSAGES = [
  { id: 9001, channel_id: 'CH-PAT002', sender_id: 'system', sender_name: 'Orb System',
    sender_role: 'system', content: 'Case room created for Kavitha Menon (MRN-CC002).',
    message_type: 'system', timestamp: onDay('20', '14:35'), is_bot: true, source: 'bot' },
  { id: 9002, channel_id: 'CH-PAT002', sender_id: 'DOC002', sender_name: 'Dr. Arjun Mehta',
    sender_role: 'doctor', content: 'Rate still 130s despite digoxin load. Starting IV diltiazem per protocol.',
    message_type: 'text', timestamp: t('08:12'), is_bot: false, source: 'clinician' },
  { id: 9003, channel_id: 'CH-PAT002', sender_id: 'NUR001', sender_name: 'Nurse Kavya Hegde',
    sender_role: 'nurse', content: 'Latest obs: HR 132, BP 108/70, SpO2 89% on 4L. Patient anxious but responsive.',
    message_type: 'text', timestamp: t('08:35'), is_bot: false, source: 'clinician' },
  { id: 9004, channel_id: 'CH-PAT002', sender_id: 'orb-sage', sender_name: 'Orb Sage',
    sender_role: 'intelligence', content: 'Vitals trend: heart rate has risen 14 bpm over the last hour while SpO2 drifted down 2%.',
    message_type: 'text', timestamp: t('08:50'), is_bot: true, source: 'bot' },
  { id: 9005, channel_id: 'CH-PAT002', sender_id: 'orb-sage', sender_name: 'Orb Sage',
    sender_role: 'intelligence',
    content: JSON.stringify({
      action: 'order_lab', target: 'BNP level', status: 'proposed',
      rationale: 'Team discussing decompensated heart failure — baseline BNP not on file.',
      confidence: 0.91,
    }),
    message_type: 'ambient_action', timestamp: t('08:52'), is_bot: true, source: 'bot' },
  { id: 9006, channel_id: 'CH-PAT002', sender_id: 'orb-vigil', sender_name: 'Orb Vigil',
    sender_role: 'system',
    content: RELAY_ALERTS[0].content,
    message_type: 'alert', timestamp: t('09:02'), is_bot: true, source: 'bot' },
  { id: 9007, channel_id: 'CH-PAT002', sender_id: 'DOC002', sender_name: 'Dr. Arjun Mehta',
    sender_role: 'doctor', content: 'Seen. I am at the bedside — echo tech paged, please draw the BNP with the next bloods.',
    message_type: 'text', timestamp: t('09:08'), is_bot: false, source: 'clinician' },
  { id: 9008, channel_id: 'CH-PAT002', sender_id: 'NUR001', sender_name: 'Nurse Kavya Hegde',
    sender_role: 'nurse', content: 'Bloods drawn and sent. Repeat obs in 15 minutes.',
    message_type: 'text', timestamp: t('09:14'), is_bot: false, source: 'clinician' },
];

// sage_memory object + sage_actions bare array (map-backend-api.md:81-88)
const SAGE_MEMORY = {
  room_id: 'CH-PAT002', patient_id: 'PAT002',
  patient_summary: 'Kavitha Menon, 58F, AF with decompensated heart failure, CCU-02.',
  discussion_summary: 'Team escalating rate control; diltiazem started, BNP and echo pending.',
  current_concern: 'Rapid AF with borderline oxygenation despite treatment.',
  likely_next_steps: ['Review BNP result', 'Bedside echo', 'Consider HDU-level monitoring'],
  unresolved_questions: ['Is there a reversible precipitant (infection, ischaemia)?'],
  active_intents: [
    { action: 'order_lab', target: 'BNP level', rationale: 'Baseline BNP not on file',
      confidence: 0.91, created_at: t('08:52'), updated_at: t('08:52') },
  ],
  risk_flags: [
    { detail: 'SpO2 below 90% on 4L', rationale: 'Hypoxia despite escalating oxygen',
      confidence: 0.88, created_at: t('08:50'), updated_at: t('09:05') },
  ],
  last_processed_at: t('09:10'), updated_at: t('09:10'),
};

const SAGE_ACTIONS = [
  // suggested: confidence set, handoff null
  { id: 'act-001', room_id: 'CH-PAT002', patient_id: 'PAT002',
    action_type: 'add_medication', target: 'Furosemide 40mg IV BD',
    rationale: 'Volume overload discussed by the team; no diuretic escalation charted yet.',
    confidence: 0.86, state: 'suggested', version: 1, source_msg_id: 9003,
    created_at: t('08:55'), updated_at: t('08:55'), handoff: null },
  // confirmed: carries a handoff
  { id: 'act-002', room_id: 'CH-PAT002', patient_id: 'PAT002',
    action_type: 'order_lab', target: 'BNP level',
    rationale: 'Baseline BNP required to grade decompensation.',
    confidence: 0.91, state: 'confirmed', version: 2, source_msg_id: 9005,
    created_at: t('08:52'), updated_at: t('09:09'),
    handoff: { id: 'hf-001', status: 'accepted', target_module: 'orders',
      error_summary: null, downstream_ref_id: 'ORD-2105' } },
];

const SAGE_PRE_FLIGHT = {
  action: SAGE_ACTIONS[0],
  resolved_medication: { id: 'MED005', name: 'Furosemide', generic_name: 'furosemide',
    form: 'injection', strength: '40mg' },
  reconciliation_score: 0.92,
  stock_status: { medication_id: 'MED005', in_stock: true, total_quantity: 42 },
  alternative_suggestion: null,
  is_high_risk: false,
};

// ═══════════════════════════════════════════════════════════════════════════
// FORECAST — /hospital /capacity /admission-patterns objects; /discharge-board
// and /appointments bare arrays (map-backend-api.md:115-126). Two days exceed
// 90% occupancy to exercise warn states.
// ═══════════════════════════════════════════════════════════════════════════
const FORECAST_HOSPITAL = (() => {
  const days = [
    ['2026-05-24', 'Sun May 24', 4, 2, 24, 80.0],
    ['2026-05-25', 'Mon May 25', 5, 3, 26, 86.7],
    ['2026-05-26', 'Tue May 26', 4, 2, 28, 93.3],
    ['2026-05-27', 'Wed May 27', 3, 3, 28, 93.3],
    ['2026-05-28', 'Thu May 28', 2, 4, 26, 86.7],
    ['2026-05-29', 'Fri May 29', 3, 4, 25, 83.3],
    ['2026-05-30', 'Sat May 30', 2, 3, 24, 80.0],
  ];
  const wards = [
    { ICU: 5, W1: 8, W2: 6, W3: 5 }, { ICU: 6, W1: 8, W2: 7, W3: 5 },
    { ICU: 6, W1: 8, W2: 8, W3: 6 }, { ICU: 6, W1: 8, W2: 8, W3: 6 },
    { ICU: 5, W1: 8, W2: 7, W3: 6 }, { ICU: 5, W1: 8, W2: 7, W3: 5 },
    { ICU: 5, W1: 7, W2: 7, W3: 5 },
  ];
  return {
    current_occupancy: 22, total_capacity: 30, current_occupancy_pct: 73.3,
    daily_forecasts: days.map(([date, day_label, adm, dis, occ, pct], i) => ({
      date, day_label, predicted_admissions: adm, predicted_discharges: dis,
      predicted_occupancy: occ, occupancy_pct: pct, ward_breakdown: wards[i],
      env_multiplier: i === 2 ? 1.15 : 1.0,
      env_factors: i === 2 ? ['AQI 162 — respiratory admissions elevated'] : [],
      env_icons: i === 2 ? ['smog'] : [],
    })),
    summary: {
      avg_occupancy: 26, peak_day: 'Tue May 26', peak_occupancy: 28,
      total_predicted_discharges_7d: 21, total_predicted_admissions_7d: 23,
      beds_available_in_48h: 4,
    },
  };
})();

const FORECAST_CAPACITY = {
  current: {
    ICU: { total: 6, occupied: 6, available: 0, utilization_pct: 100.0 },
    W1: { total: 8, occupied: 7, available: 1, utilization_pct: 87.5 },
    W2: { total: 8, occupied: 6, available: 2, utilization_pct: 75.0 },
    W3: { total: 8, occupied: 3, available: 5, utilization_pct: 37.5 },
  },
  projected_48h: {
    ICU: { projected_occupied: 6, projected_available: 0, projected_utilization_pct: 100.0 },
    W1: { projected_occupied: 8, projected_available: 0, projected_utilization_pct: 100.0 },
    W2: { projected_occupied: 7, projected_available: 1, projected_utilization_pct: 87.5 },
    W3: { projected_occupied: 5, projected_available: 3, projected_utilization_pct: 62.5 },
  },
  total: { capacity: 30, occupied: 22, available: 8 },
};

const FORECAST_ADMISSION_PATTERNS = {
  daily_summary: [
    { day: 'Mon', avg_admissions: 3.4, total: 14 }, { day: 'Tue', avg_admissions: 3.1, total: 12 },
    { day: 'Wed', avg_admissions: 2.9, total: 12 }, { day: 'Thu', avg_admissions: 2.6, total: 10 },
    { day: 'Fri', avg_admissions: 3.2, total: 13 }, { day: 'Sat', avg_admissions: 2.2, total: 9 },
    { day: 'Sun', avg_admissions: 2.0, total: 8 },
  ],
  total_30d: 88, avg_daily: 2.9, avg_los: 4.2,
  severity_distribution: { critical: 9, elevated: 30, stable: 49 },
  ward_distribution: { ICU: 8, W1: 30, W2: 28, W3: 22 },
  discharge_rate: 74.5,
};

const FORECAST_DISCHARGE_BOARD = [
  { patient_id: 'PAT005', name: 'Priya Nair', ward: 'W3', bed: 'GM-103',
    diagnosis: 'Dengue fever with thrombocytopenia', age: 28, sex: 'F',
    days_admitted: 1.5, predicted_remaining_days: 1.2, discharge_probability: 0.74,
    acuity_trajectory: 'improving', confidence: 0.61, estimated_discharge_date: '2026-05-24' },
  { patient_id: 'PAT006', name: 'Lakshmi Devi', ward: 'W3', bed: 'GM-105',
    diagnosis: 'UTI with delirium', age: 80, sex: 'F',
    days_admitted: 4.7, predicted_remaining_days: 1.8, discharge_probability: 0.58,
    acuity_trajectory: 'stable', confidence: 0.52, estimated_discharge_date: '2026-05-25' },
  { patient_id: 'PAT004', name: 'Ananya Kapoor', ward: 'W1', bed: 'GM-101',
    diagnosis: 'Diabetic ketoacidosis', age: 34, sex: 'F',
    days_admitted: 0.6, predicted_remaining_days: 2.4, discharge_probability: 0.36,
    acuity_trajectory: 'improving', confidence: 0.47, estimated_discharge_date: '2026-05-26' },
  { patient_id: 'PAT001', name: 'Suresh Reddy', ward: 'W1', bed: 'GM-104',
    diagnosis: 'CAP with early sepsis', age: 55, sex: 'M',
    days_admitted: 2.1, predicted_remaining_days: 3.5, discharge_probability: 0.21,
    acuity_trajectory: 'worsening', confidence: 0.45, estimated_discharge_date: '2026-05-27' },
];

const FORECAST_APPOINTMENTS = [
  { date: '2026-05-24', day_label: 'Sun May 24', time: '09:30', patient_id: 'PAT101',
    patient_name: 'Nikhil Menon', age: 39, sex: 'M', ward: 'W1', bed: 'GM-110',
    diagnosis: 'Acute gastroenteritis', type: 'follow_up', reason: 'Post-discharge review',
    acuity: 'stable', days_admitted: 3.1, active_medications: 1, is_discharged: true },
  { date: '2026-05-25', day_label: 'Mon May 25', time: '10:00', patient_id: 'PAT005',
    patient_name: 'Priya Nair', age: 28, sex: 'F', ward: 'W3', bed: 'GM-103',
    diagnosis: 'Dengue fever with thrombocytopenia', type: 'lab_review',
    reason: 'Platelet recheck', acuity: 'stable', days_admitted: 2.5,
    active_medications: 2, is_discharged: false },
  { date: '2026-05-25', day_label: 'Mon May 25', time: '11:15', patient_id: 'PAT102',
    patient_name: 'Rohini Kulkarni', age: 61, sex: 'F', ward: 'ICU', bed: 'CCU-05',
    diagnosis: 'NSTEMI, medically managed', type: 'follow_up',
    reason: 'Cardiology OPD — titrate beta-blocker', acuity: 'elevated',
    days_admitted: 6.7, active_medications: 4, is_discharged: true },
  { date: '2026-05-26', day_label: 'Tue May 26', time: '09:00', patient_id: 'PAT006',
    patient_name: 'Lakshmi Devi', age: 80, sex: 'F', ward: 'W3', bed: 'GM-105',
    diagnosis: 'UTI with delirium', type: 'assessment', reason: 'Cognitive reassessment',
    acuity: 'stable', days_admitted: 7.7, active_medications: 3, is_discharged: false },
  { date: '2026-05-27', day_label: 'Wed May 27', time: '14:30', patient_id: 'PAT001',
    patient_name: 'Suresh Reddy', age: 55, sex: 'M', ward: 'W1', bed: 'GM-104',
    diagnosis: 'CAP with early sepsis', type: 'imaging', reason: 'Repeat chest X-ray',
    acuity: 'elevated', days_admitted: 6.3, active_medications: 3, is_discharged: false },
];

function forecastPatient(id) {
  return {
    predicted_los_days: 2.5, discharge_probability: 0.42, acuity_trajectory: 'stable',
    confidence: 0.45, estimated_discharge_date: '2026-05-26', model_trained: false,
    prediction_source: 'heuristic', patient_id: id,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SURGERY — /schedule bare array of day buckets, /cases + /emergency-queue bare
// arrays, /{id} object with PARSED arrays (map-backend-api.md:128-136; brief/full
// field lists from SurgicalSuitePanel.tsx:10-66).
// ═══════════════════════════════════════════════════════════════════════════
function surgeryBrief(o) {
  return Object.assign({
    id: 'SUR-000', patient_id: 'PAT007', patient_name: 'Mohan Das', age: 46, sex: 'M',
    mrn: 'MRN-GS001', procedure_name: 'Laparoscopic appendicectomy',
    diagnosis: 'Acute appendicitis', scheduled_time: '10:30',
    estimated_duration_min: 90, operating_room: 'OT-2', status: 'scheduled',
    risk_level: 'stable', risk_score: null, success_probability: 0.95,
    surgeon_name: 'Dr. Vikram Rathore',
  }, o);
}

const SURGERY_SCHEDULE = [
  { date: '2026-05-23', day_label: 'Saturday', is_today: true, surgeries: [
    surgeryBrief({ id: 'SUR-001', patient_id: 'PAT008', patient_name: 'Farhan Sheikh',
      age: 41, mrn: 'MRN-OR001', procedure_name: 'ORIF femur shaft fracture',
      diagnosis: 'Femur shaft fracture', scheduled_time: '08:00',
      estimated_duration_min: 150, operating_room: 'OT-1', status: 'in_progress',
      risk_level: 'elevated', risk_score: 42.0, success_probability: 0.9,
      surgeon_name: 'Dr. Sanjay Rao' }),
    surgeryBrief({ id: 'SUR-002', patient_id: 'PAT007',
      procedure_name: 'Emergency laparoscopic appendicectomy',
      diagnosis: 'Acute appendicitis with perforation', scheduled_time: '10:30',
      status: 'scheduled', risk_level: 'critical', risk_score: 71.0,
      success_probability: 0.85 }),
  ] },
  { date: '2026-05-24', day_label: 'Sunday', is_today: false, surgeries: [
    surgeryBrief({ id: 'SUR-003', patient_id: 'PAT201', patient_name: 'Geeta Rani',
      age: 52, sex: 'F', mrn: 'MRN-GS014', procedure_name: 'Laparoscopic cholecystectomy',
      diagnosis: 'Symptomatic cholelithiasis', scheduled_time: '09:00',
      estimated_duration_min: 75, operating_room: 'OT-2', status: 'scheduled',
      risk_level: 'stable' }),
  ] },
  { date: '2026-05-25', day_label: 'Monday', is_today: false, surgeries: [
    surgeryBrief({ id: 'SUR-004', patient_id: 'PAT202', patient_name: 'Harish Gowda',
      age: 63, mrn: 'MRN-GS015', procedure_name: 'Inguinal hernia repair (open mesh)',
      diagnosis: 'Right inguinal hernia', scheduled_time: '11:00',
      estimated_duration_min: 60, operating_room: 'OT-3', status: 'scheduled',
      risk_level: 'stable' }),
    surgeryBrief({ id: 'SUR-005', patient_id: 'PAT203', patient_name: 'Shanta Bai',
      age: 71, sex: 'F', mrn: 'MRN-GS016', procedure_name: 'Sigmoid colectomy',
      diagnosis: 'Sigmoid adenocarcinoma', scheduled_time: '14:00',
      estimated_duration_min: 210, operating_room: 'OT-1', status: 'scheduled',
      risk_level: 'elevated', risk_score: 48.5, success_probability: 0.88 }),
  ] },
  { date: '2026-05-26', day_label: 'Tuesday', is_today: false, surgeries: [] },
  { date: '2026-05-27', day_label: 'Wednesday', is_today: false, surgeries: [
    surgeryBrief({ id: 'SUR-006', patient_id: 'PAT204', patient_name: 'Ibrahim Khan',
      age: 58, mrn: 'MRN-GS017', procedure_name: 'Thyroidectomy (total)',
      diagnosis: 'Multinodular goitre', scheduled_time: '09:30',
      estimated_duration_min: 120, operating_room: 'OT-2', status: 'scheduled',
      risk_level: 'stable' }),
  ] },
];

const SURGERY_CASES = [
  Object.assign(surgeryBrief({ id: 'SUR-001', patient_id: 'PAT008',
    patient_name: 'Farhan Sheikh', age: 41, mrn: 'MRN-OR001',
    procedure_name: 'ORIF femur shaft fracture', diagnosis: 'Femur shaft fracture',
    scheduled_time: '08:00', estimated_duration_min: 150, operating_room: 'OT-1',
    status: 'in_progress', risk_level: 'elevated', risk_score: 42.0,
    surgeon_name: 'Dr. Sanjay Rao' }), {
    team: [
      { id: 1, surgery_id: 'SUR-001', user_id: 'DOC005', role_name: 'Lead Surgeon',
        status: 'confirmed', updated_at: t('07:45') },
      { id: 2, surgery_id: 'SUR-001', user_id: 'anesthetist_1', role_name: 'Anesthetist',
        status: 'confirmed', updated_at: t('07:40') },
      { id: 3, surgery_id: 'SUR-001', user_id: 'NUR001', role_name: 'Scrub Nurse',
        status: 'confirmed', updated_at: t('07:50') },
    ],
  }),
  Object.assign(surgeryBrief({ id: 'SUR-002',
    procedure_name: 'Emergency laparoscopic appendicectomy',
    diagnosis: 'Acute appendicitis with perforation', status: 'in_progress',
    risk_level: 'critical', risk_score: 71.0, scheduled_time: '10:30' }), {
    team: [
      { id: 4, surgery_id: 'SUR-002', user_id: 'DOC004', role_name: 'Lead Surgeon',
        status: 'confirmed', updated_at: t('09:05') },
      { id: 5, surgery_id: 'SUR-002', user_id: 'anesthetist_2', role_name: 'Anesthetist',
        status: 'pending', updated_at: t('09:00') },
    ],
  }),
];

const SURGERY_EMERGENCY_QUEUE = [
  surgeryBrief({ id: 'SUR-EM-01', procedure_name: 'Emergency laparoscopic appendicectomy',
    diagnosis: 'Acute appendicitis with perforation', scheduled_time: '10:30',
    status: 'emergency_pending', risk_level: 'critical', risk_score: 71.0,
    success_probability: 0.85, created_at: t('08:45') }),
];

function surgeryDetail(id) {
  return {
    id, patient_id: 'PAT007', patient_name: 'Mohan Das', age: 46, sex: 'M',
    mrn: 'MRN-GS001', bed: 'SG-201', ward: 'W2',
    patient_diagnosis: 'Acute appendicitis with perforation',
    admission_date: t('05:40'),
    procedure_name: 'Emergency laparoscopic appendicectomy',
    diagnosis: 'Acute appendicitis with perforation',
    scheduled_date: '2026-05-23', scheduled_time: '10:30',
    estimated_duration_min: 90, operating_room: 'OT-2', status: 'scheduled',
    co_morbidities: ['Type 2 DM', 'Hypertension'],
    allergies: ['Penicillin'],
    surgical_checklist: [
      'Confirm consent signed', 'Group & save sent', 'IV antibiotics on induction',
      'DVT prophylaxis charted', 'Mark laparoscopy port sites',
    ],
    instruments_required: ['Laparoscopic tower', '5mm & 10mm trocars', 'Endo-GIA stapler', 'Suction irrigator'],
    staff_required: [
      { role: 'Lead Surgeon', name: 'Dr. Vikram Rathore' },
      { role: 'Anesthetist', name: 'Dr. Nandini Rao' },
      { role: 'Scrub Nurse', name: 'Nurse Kavya Hegde' },
    ],
    risk_assessment: 'Perforated appendix with localised peritonitis; sepsis physiology partially corrected pre-op.',
    possible_complications: ['Intra-abdominal abscess', 'Wound infection', 'Ileus', 'Conversion to open'],
    success_probability: 0.85, risk_level: 'critical', risk_score: 71.0,
    risk_drivers: ['Perforation on imaging', 'HR 110', 'Temp 38.6°C'],
    notes: JSON.stringify({ ai_package_status: 'ready' }),
    imported_context: {
      medications: ['Piperacillin-Tazobactam 4.5g IV Q6H', 'Paracetamol 1g IV PRN'],
      vitals: ['HR 110', 'BP 118/74', 'SpO2 96% RA', 'Temp 38.6°C'],
      recent_labs: ['WBC 16.2', 'CRP 148', 'Lactate 2.1'],
      scribe_notes: 'RIF guarding with rebound; CT confirms perforated appendicitis with local collection.',
    },
  };
}

// WHO checklist template + per-surgery state (map-backend-api.md:133-134)
const SURGERY_CHECKLIST_TEMPLATE = {
  version: 'who-ssc-2009-v1',
  phases: [
    { id: 'sign_in', label: 'Sign In', when: 'Before induction of anaesthesia', items: [
      { id: 'si_identity', text: 'Patient identity, site, procedure and consent confirmed' },
      { id: 'si_site_marked', text: 'Surgical site marked' },
      { id: 'si_anaesthesia_check', text: 'Anaesthesia machine and medication check complete' },
      { id: 'si_allergy', text: 'Known allergies reviewed' },
    ] },
    { id: 'time_out', label: 'Time Out', when: 'Before skin incision', items: [
      { id: 'to_team_intro', text: 'Team members introduced by name and role' },
      { id: 'to_antibiotics', text: 'Antibiotic prophylaxis given within last 60 minutes' },
      { id: 'to_imaging', text: 'Essential imaging displayed' },
    ] },
    { id: 'sign_out', label: 'Sign Out', when: 'Before patient leaves operating room', items: [
      { id: 'so_counts', text: 'Instrument, sponge and needle counts correct' },
      { id: 'so_specimen', text: 'Specimen labelled' },
      { id: 'so_recovery', text: 'Key recovery concerns reviewed' },
    ] },
  ],
};

const SURGERY_CHECKLIST_STATE = {
  version: 'who-ssc-2009-v1',
  state: {
    si_identity: { checked: true, by: 'DOC004', at: t('09:20') },
    si_site_marked: { checked: true, by: 'DOC004', at: t('09:21') },
    si_allergy: { checked: true, by: 'NUR001', at: t('09:24') },
  },
  progress: { checked: 3, total: 10 },
  completed: false,
};

const SURGERY_RISK_SSE = sse([
  { content: 'Risk assessment: perforated appendicitis with early sepsis. ' },
  { content: 'ASA III due to diabetes and active infection. ' },
  { content: 'Primary risks are intra-abdominal abscess and wound infection; recommend post-op HDU observation for 24 hours.' },
  '[DONE]',
]);

// ═══════════════════════════════════════════════════════════════════════════
// HELIX — ALL keyed wrappers (map-backend-api.md:138-145). Inventory covers
// out-of-stock (0), low (<30), and normal quantities. Orders cover the full
// status machine pending→…→delivered plus rejected.
// ═══════════════════════════════════════════════════════════════════════════
const HELIX_CATALOG = { catalog: [
  { id: 'MED001', name: 'Piperacillin-Tazobactam', generic_name: 'piperacillin/tazobactam', form: 'injection', strength: '4.5g' },
  { id: 'MED002', name: 'Metoprolol', generic_name: 'metoprolol tartrate', form: 'tablet', strength: '25mg' },
  { id: 'MED003', name: 'Ceftriaxone', generic_name: 'ceftriaxone sodium', form: 'injection', strength: '1g' },
  { id: 'MED004', name: 'Paracetamol', generic_name: 'acetaminophen', form: 'tablet', strength: '500mg' },
  { id: 'MED005', name: 'Furosemide', generic_name: 'furosemide', form: 'injection', strength: '40mg' },
  { id: 'MED006', name: 'Enoxaparin', generic_name: 'enoxaparin sodium', form: 'prefilled syringe', strength: '60mg' },
] };

const HELIX_INVENTORY = { inventory: [
  { id: 'INV001', medication_id: 'MED001', lot_number: 'LOT-PT-2311', quantity: 88, expiration_date: '2027-02-28', name: 'Piperacillin-Tazobactam', strength: '4.5g' },
  { id: 'INV002', medication_id: 'MED002', lot_number: 'LOT-MT-1082', quantity: 45, expiration_date: '2027-08-31', name: 'Metoprolol', strength: '25mg' },
  { id: 'INV003', medication_id: 'MED003', lot_number: 'LOT-CF-0917', quantity: 12, expiration_date: '2026-11-30', name: 'Ceftriaxone', strength: '1g' },      // low (<30)
  { id: 'INV004', medication_id: 'MED004', lot_number: 'LOT-PC-5541', quantity: 220, expiration_date: '2028-01-31', name: 'Paracetamol', strength: '500mg' },  // normal
  { id: 'INV005', medication_id: 'MED005', lot_number: 'LOT-FU-3120', quantity: 42, expiration_date: '2027-05-31', name: 'Furosemide', strength: '40mg' },
  { id: 'INV006', medication_id: 'MED006', lot_number: 'LOT-EN-2208', quantity: 0, expiration_date: '2026-12-31', name: 'Enoxaparin', strength: '60mg' },      // out of stock
] };

function helixOrder(o) {
  return Object.assign({
    id: 'HO-000', patient_id: 'PAT001', medication_id: 'MED001', status: 'pending',
    prescribed_by: 'DOC001', dosage: '4.5g', frequency: 'Q6H', route: 'IV',
    substitute_medication_id: null, pharmacist_notes: null, reviewed_by: null,
    reviewed_at: null, scheduled_at: null, last_transition_id: null, is_mock_event: 0,
    created_at: t('07:00'), medication_name: 'Piperacillin-Tazobactam',
    patient_name: 'Suresh Reddy', prescribed_by_name: 'Dr. Meera Sharma',
    ward: 'W1', bed: 'GM-104', substitute_name: null,
  }, o);
}

const HELIX_ORDERS = { orders: [
  helixOrder({ id: 'HO-001', status: 'pending', created_at: t('08:50') }),
  helixOrder({ id: 'HO-002', status: 'reviewing', patient_id: 'PAT002',
    patient_name: 'Kavitha Menon', ward: 'ICU', bed: 'CCU-02',
    medication_id: 'MED005', medication_name: 'Furosemide', dosage: '40mg',
    frequency: 'BD', route: 'IV', prescribed_by: 'DOC002',
    prescribed_by_name: 'Dr. Arjun Mehta', reviewed_by: 'PHA001',
    reviewed_at: t('09:02'), created_at: t('08:40') }),
  helixOrder({ id: 'HO-003', status: 'substitute_suggested', patient_id: 'PAT003',
    patient_name: 'Rajesh Iyer', bed: 'GM-102', medication_id: 'MED006',
    medication_name: 'Enoxaparin', dosage: '60mg', frequency: 'BD', route: 'SC',
    substitute_medication_id: 'MED002', substitute_name: 'Metoprolol',
    pharmacist_notes: 'Enoxaparin out of stock — suggest therapeutic alternative pending restock.',
    reviewed_by: 'PHA001', reviewed_at: t('08:55'), created_at: t('08:10') }),
  helixOrder({ id: 'HO-004', status: 'verified', patient_id: 'PAT004',
    patient_name: 'Ananya Kapoor', bed: 'GM-101', medication_id: 'MED004',
    medication_name: 'Paracetamol', dosage: '1g', frequency: 'Q6H', route: 'PO',
    reviewed_by: 'PHA001', reviewed_at: t('08:20'), created_at: t('07:45') }),
  helixOrder({ id: 'HO-005', status: 'scheduled', patient_id: 'PAT005',
    patient_name: 'Priya Nair', ward: 'W3', bed: 'GM-103', medication_id: 'MED004',
    medication_name: 'Paracetamol', dosage: '1g', frequency: 'Q6H', route: 'PO',
    reviewed_by: 'PHA001', reviewed_at: t('07:50'), scheduled_at: t('10:00'),
    created_at: t('07:15') }),
  helixOrder({ id: 'HO-006', status: 'dispensed', patient_id: 'PAT006',
    patient_name: 'Lakshmi Devi', ward: 'W3', bed: 'GM-105', medication_id: 'MED003',
    medication_name: 'Ceftriaxone', dosage: '1g', frequency: 'Q24H', route: 'IV',
    reviewed_by: 'PHA001', reviewed_at: t('06:40'), scheduled_at: t('08:00'),
    created_at: t('06:10') }),
  helixOrder({ id: 'HO-007', status: 'delivered', patient_id: 'PAT002',
    patient_name: 'Kavitha Menon', ward: 'ICU', bed: 'CCU-02', medication_id: 'MED002',
    medication_name: 'Metoprolol', dosage: '25mg', frequency: 'BD', route: 'PO',
    prescribed_by: 'DOC002', prescribed_by_name: 'Dr. Arjun Mehta',
    reviewed_by: 'PHA001', reviewed_at: onDay('22', '18:00'),
    scheduled_at: onDay('22', '20:00'), created_at: onDay('22', '17:30') }),
  helixOrder({ id: 'HO-008', status: 'rejected', patient_id: 'PAT008',
    patient_name: 'Farhan Sheikh', bed: 'OR-12', ward: 'W2', medication_id: 'MED001',
    medication_name: 'Piperacillin-Tazobactam', dosage: '9g', frequency: 'Q6H',
    route: 'IV', pharmacist_notes: 'Dose exceeds maximum — please re-prescribe at 4.5g Q6H.',
    reviewed_by: 'PHA001', reviewed_at: onDay('22', '16:20'),
    created_at: onDay('22', '15:55') }),
] };

const HELIX_ORDERS_PENDING = {
  orders: HELIX_ORDERS.orders.filter((o) =>
    ['pending', 'reviewing', 'substitute_suggested'].includes(o.status)),
};

const HELIX_STOCK_MOVEMENTS = { movements: [
  { id: 7001, medication_id: 'MED003', inventory_id: 'INV003', movement_type: 'dispense',
    quantity: -2, lot_number: 'LOT-CF-0917', actor_id: 'PHA001', reason: 'Order HO-006',
    created_at: t('08:05'), medication_name: 'Ceftriaxone' },
  { id: 7002, medication_id: 'MED006', inventory_id: 'INV006', movement_type: 'dispense',
    quantity: -4, lot_number: 'LOT-EN-2208', actor_id: 'PHA001', reason: 'Ward stock top-up',
    created_at: onDay('22', '19:30'), medication_name: 'Enoxaparin' },
  { id: 7003, medication_id: 'MED004', inventory_id: 'INV004', movement_type: 'restock',
    quantity: 100, lot_number: 'LOT-PC-5541', actor_id: 'PHA001', reason: 'Weekly delivery',
    created_at: onDay('22', '09:00'), medication_name: 'Paracetamol' },
] };

function helixTransitions(orderId) {
  return { transitions: [
    { id: 8101, order_id: orderId, from_status: null, to_status: 'pending',
      idempotency_key: `${orderId}-t1`, actor_id: 'DOC001', payload: null,
      created_at: t('07:00'), actor_name: 'Dr. Meera Sharma' },
    { id: 8102, order_id: orderId, from_status: 'pending', to_status: 'reviewing',
      idempotency_key: `${orderId}-t2`, actor_id: 'PHA001', payload: null,
      created_at: t('08:40'), actor_name: 'Pharmacist Rina D\'Souza' },
  ] };
}

// ═══════════════════════════════════════════════════════════════════════════
// PHARMACY verify queue — {pending:[...]} (map-backend-api.md:152-153). The
// component reads row.patient / row.dose (src/api/pharmacy.ts:21-33), the
// backend emits patient_name / dosage — include BOTH spellings. Row 2 uses
// 'REVIEW' values to exercise the editable inputs (PharmacyVerifyQueue.tsx:131).
// ═══════════════════════════════════════════════════════════════════════════
const PHARMACY_PENDING = { pending: [
  { id: 12, patient_id: 'PAT001', patient: 'Suresh Reddy', patient_name: 'Suresh Reddy',
    drug_name: 'Azithromycin', dose: '500mg', dosage: '500mg', route: 'PO',
    frequency: 'OD', status: 'active', source: 'llm_extracted',
    verified_by: null, verified_at: null, notes: null,
    safety_alerts: [
      { type: 'interaction', severity: 'severe',
        message: 'Severe interaction: azithromycin + digoxin — increased risk of arrhythmia.',
        with: 'Digoxin' },
    ],
    max_severity: 'severe' },
  { id: 13, patient_id: 'PAT004', patient: 'Ananya Kapoor', patient_name: 'Ananya Kapoor',
    drug_name: 'Insulin glargine', dose: 'REVIEW', dosage: 'REVIEW', route: 'REVIEW',
    frequency: 'REVIEW', status: 'active', source: 'agentic',
    verified_by: null, verified_at: null, notes: 'Dose not stated in dictation',
    safety_alerts: [], max_severity: 'none' },
  { id: 14, patient_id: 'PAT006', patient: 'Lakshmi Devi', patient_name: 'Lakshmi Devi',
    drug_name: 'Ceftriaxone', dose: '1g', dosage: '1g', route: 'IV',
    frequency: 'Q24H', status: 'active', source: 'llm_extracted',
    verified_by: null, verified_at: null, notes: null,
    safety_alerts: [], max_severity: 'none' },
] };

function pharmacyActionResult(status) {
  const medication = {
    id: 12, patient_id: 'PAT001', patient: 'Suresh Reddy', drug_name: 'Azithromycin',
    dose: '500mg', route: 'PO', frequency: 'OD', source: 'llm_extracted',
    status: status === 'verified' ? 'active' : 'discontinued',
    verified_by: 'PHA001', verified_at: t('09:20'), notes: null,
  };
  const safety = { max_severity: 'none', blocking: false, safe: true };
  return status === 'verified'
    ? { status, medication, safety_alerts: [], safety }
    : { status, medication, reason: 'Duplicate therapy', safety_alerts: [], safety };
}

// ═══════════════════════════════════════════════════════════════════════════
// ORDERS (CPOE) — keyed wrappers {orders}/{order}/{events}
// (map-backend-api.md:147-150). Statuses: draft, active, in_progress,
// resulted, cancelled.
// ═══════════════════════════════════════════════════════════════════════════
function cpoeOrder(o) {
  return Object.assign({
    id: 'ORD-2101', patient_id: 'PAT001', order_type: 'lab', code: '718-7',
    code_system: 'LOINC', description: 'Hemoglobin', priority: 'routine',
    status: 'draft', ordered_by: 'DOC001', source: 'clinician', detail_json: null,
    result_text: null, result_at: null, created_at: t('08:00'), updated_at: t('08:00'),
  }, o);
}

function cpoeOrdersFor(patientId) {
  return { orders: [
    cpoeOrder({ id: 'ORD-2101', patient_id: patientId, status: 'draft',
      description: 'Hemoglobin', created_at: t('08:58'), updated_at: t('08:58') }),
    cpoeOrder({ id: 'ORD-2102', patient_id: patientId, status: 'active',
      order_type: 'lab', code: '2160-0', description: 'Creatinine',
      priority: 'urgent', created_at: t('08:12'), updated_at: t('08:15') }),
    cpoeOrder({ id: 'ORD-2103', patient_id: patientId, status: 'in_progress',
      order_type: 'imaging', code: null, code_system: null,
      description: 'Chest X-ray PA view', priority: 'routine',
      created_at: t('07:30'), updated_at: t('08:05') }),
    cpoeOrder({ id: 'ORD-2104', patient_id: patientId, status: 'resulted',
      order_type: 'lab', code: '6690-2', description: 'WBC count',
      result_text: 'WBC 16.2 x10^9/L (high)', result_at: t('07:20'),
      created_at: onDay('22', '22:00'), updated_at: t('07:20') }),
    cpoeOrder({ id: 'ORD-2105', patient_id: patientId, status: 'cancelled',
      order_type: 'procedure', code: null, code_system: null,
      description: 'Lumbar puncture', priority: 'routine',
      created_at: onDay('22', '18:00'), updated_at: onDay('22', '19:10') }),
  ] };
}

function cpoeEvents(orderId) {
  return { events: [
    { id: 5501, order_id: orderId, prior_status: null, new_status: 'draft',
      actor_id: 'DOC001', reason: null, created_at: t('08:00') },
    { id: 5502, order_id: orderId, prior_status: 'draft', new_status: 'active',
      actor_id: 'DOC001', reason: 'Signed', created_at: t('08:05') },
  ] };
}

// ═══════════════════════════════════════════════════════════════════════════
// AUDIT — paginated envelope {logs,total,limit,offset} (map-backend-api.md:175-176).
// 25 rows spanning auth/ingestion/clinical, success+failure, alert action types
// with stringified metadata_json, and a shared correlation_id for the Trace tab.
// ═══════════════════════════════════════════════════════════════════════════
const AUDIT_LOGS = (() => {
  const base = {
    correlation_id: null, event_category: 'clinical', actor_user_id: 'DOC001',
    actor_role: 'doctor', target_type: 'patient', target_id: 'PAT001',
    patient_id: 'PAT001', encounter_id: null, source_type: 'api', source_event_id: null,
    request_path: '/api/vigil/patients/PAT001', http_method: 'GET',
    ip_address: '10.0.0.12', user_agent: 'Mozilla/5.0 (OrbVisualAudit)',
    session_id: 'sess-visual-1', outcome: 'success', action_type: 'record_view',
    summary_message: 'Viewed patient record', metadata_json: null,
  };
  const CORR = 'corr-ingest-42'; // shared by rows 4-7 → Trace tab timeline
  const specs = [
    { event_category: 'auth', actor_user_id: 'DOC001', action_type: 'login',
      target_type: 'user', target_id: 'DOC001', patient_id: null,
      request_path: '/api/auth/login', http_method: 'POST',
      summary_message: 'Login succeeded for DOC001' },
    { event_category: 'auth', actor_user_id: 'DOC003', actor_role: 'doctor',
      action_type: 'login', outcome: 'failure', target_type: 'user',
      target_id: 'DOC003', patient_id: null, request_path: '/api/auth/login',
      http_method: 'POST', summary_message: 'Login failed for DOC003 — bad password' },
    { event_category: 'auth', actor_user_id: 'NUR001', actor_role: 'nurse',
      action_type: 'logout', target_type: 'user', target_id: 'NUR001',
      patient_id: null, request_path: '/api/auth/logout', http_method: 'POST',
      summary_message: 'NUR001 signed out' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'vigil_alert_generated', correlation_id: CORR, patient_id: 'PAT002',
      target_id: 'PAT002', source_type: 'hl7v2', source_event_id: 'EVT-88231',
      request_path: '/api/ingest/hl7', http_method: 'POST',
      summary_message: 'Vigil alert generated: critical deterioration',
      metadata_json: '{"risk_level":"critical"}' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'relay_alert_generated', correlation_id: CORR, patient_id: 'PAT002',
      target_id: 'CH-PAT002', target_type: 'channel', source_type: 'hl7v2',
      source_event_id: 'EVT-88231', request_path: '/api/ingest/hl7', http_method: 'POST',
      summary_message: 'Relay alert posted to case room',
      metadata_json: '{"risk_level":"critical"}' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'ingest_event_parsed', correlation_id: CORR, patient_id: 'PAT002',
      target_id: 'EVT-88231', target_type: 'ingest_event', source_type: 'hl7v2',
      request_path: '/api/ingest/hl7', http_method: 'POST',
      summary_message: 'ORU^R01 parsed: 6 vitals inserted' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'ingest_event_received', correlation_id: CORR, patient_id: 'PAT002',
      target_id: 'EVT-88231', target_type: 'ingest_event', source_type: 'hl7v2',
      request_path: '/api/ingest/hl7', http_method: 'POST',
      summary_message: 'HL7 message received from monitor gateway' },
    { summary_message: 'Viewed patient record PAT001' },
    { action_type: 'note_created', http_method: 'POST',
      request_path: '/api/vigil/patients/PAT001/notes',
      summary_message: 'Progress note added' },
    { action_type: 'medication_added', patient_id: 'PAT003', target_id: 'PAT003',
      http_method: 'POST', request_path: '/api/vigil/patients/PAT003/medications',
      summary_message: 'Medication charted: Prednisolone 40mg PO' },
    { action_type: 'order_created', http_method: 'POST', request_path: '/api/orders',
      target_type: 'order', target_id: 'ORD-2101', summary_message: 'Lab order created: Hemoglobin' },
    { action_type: 'order_transition', http_method: 'POST',
      request_path: '/api/orders/ORD-2102/transition', target_type: 'order',
      target_id: 'ORD-2102', summary_message: 'Order ORD-2102 draft → active' },
    { event_category: 'auth', actor_user_id: 'PHA001', actor_role: 'pharmacist',
      action_type: 'login', target_type: 'user', target_id: 'PHA001', patient_id: null,
      request_path: '/api/auth/login', http_method: 'POST',
      summary_message: 'Login succeeded for PHA001' },
    { event_category: 'auth', actor_user_id: 'NUR002', actor_role: 'nurse',
      action_type: 'permission_denied', outcome: 'failure', target_type: 'route',
      target_id: '/api/admin/overview', patient_id: null,
      request_path: '/api/admin/overview',
      summary_message: 'Permission denied: admin.read required' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'vigil_alert_generated', correlation_id: 'corr-ingest-57',
      patient_id: 'PAT004', target_id: 'PAT004', source_type: 'fhir',
      source_event_id: 'EVT-88310', request_path: '/api/ingest/fhir', http_method: 'POST',
      summary_message: 'Vigil alert generated: elevated risk',
      metadata_json: '{"risk_level":"elevated"}' },
    { patient_id: 'PAT002', target_id: 'PAT002', actor_user_id: 'DOC002',
      request_path: '/api/vigil/patients/PAT002', summary_message: 'Viewed patient record PAT002' },
    { action_type: 'discharge_completed', patient_id: 'PAT101', target_id: 'PAT101',
      http_method: 'POST', request_path: '/api/vigil/patients/PAT101/discharge',
      summary_message: 'Patient discharged with summary + QR access token' },
    { event_category: 'auth', actor_user_id: 'ADM001', actor_role: 'admin',
      action_type: 'login', target_type: 'user', target_id: 'ADM001', patient_id: null,
      request_path: '/api/auth/login', http_method: 'POST',
      summary_message: 'Login succeeded for ADM001' },
    { event_category: 'ingestion', actor_user_id: 'system', actor_role: 'system',
      action_type: 'ingest_event_failed', outcome: 'failure',
      correlation_id: 'corr-ingest-58', patient_id: null, target_id: 'EVT-88355',
      target_type: 'ingest_event', source_type: 'hl7v2', request_path: '/api/ingest/hl7',
      http_method: 'POST', summary_message: 'Parse failed: MSH segment malformed' },
    { patient_id: 'PAT007', target_id: 'PAT007', actor_user_id: 'DOC004',
      request_path: '/api/vigil/patients/PAT007', summary_message: 'Viewed patient record PAT007' },
    { action_type: 'note_signed', http_method: 'POST',
      request_path: '/api/scribe/notes/501/sign', target_type: 'scribe_note',
      target_id: '501', summary_message: 'Scribe note signed' },
    { action_type: 'relay_message_sent', patient_id: 'PAT002', target_id: 'CH-PAT002',
      target_type: 'channel', actor_user_id: 'DOC002', http_method: 'POST',
      request_path: '/api/relay/channels/CH-PAT002/messages',
      summary_message: 'Message posted to case room' },
    { event_category: 'auth', action_type: 'logout', target_type: 'user',
      target_id: 'DOC001', patient_id: null, request_path: '/api/auth/logout',
      http_method: 'POST', summary_message: 'DOC001 signed out' },
    { event_category: 'auth', actor_user_id: 'NUR001', actor_role: 'nurse',
      action_type: 'login', target_type: 'user', target_id: 'NUR001', patient_id: null,
      request_path: '/api/auth/login', http_method: 'POST',
      summary_message: 'Login succeeded for NUR001' },
    { patient_id: 'PAT006', target_id: 'PAT006', summary_message: 'Viewed patient record PAT006' },
  ];
  return specs.map((s, i) => {
    const minutes = 554 - i * 7; // newest 09:14, descending 7-minute steps
    const hh = String(Math.floor(minutes / 60)).padStart(2, '0');
    const mm = String(minutes % 60).padStart(2, '0');
    return Object.assign({ id: 4000 - i, timestamp: `${D}T${hh}:${mm}:00` }, base, s);
  });
})();

function auditLogsResponse(params) {
  const limit = parseInt(params.get('limit') || '25', 10);
  const offset = parseInt(params.get('offset') || '0', 10);
  const filters = ['correlation_id', 'patient_id', 'action_type', 'event_category', 'outcome']
    .map((k) => [k, params.get(k)])
    .filter(([, v]) => v);
  let rows = AUDIT_LOGS;
  for (const [k, v] of filters) rows = rows.filter((r) => r[k] === v);
  return {
    logs: rows.slice(offset, offset + limit),
    total: filters.length ? rows.length : 137,
    limit, offset,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// INGEST — {events,count,ingestion_mode} (map-backend-api.md:210-211); rows
// cover parsed / failed (with parse_error) / duplicate.
// ═══════════════════════════════════════════════════════════════════════════
const INGEST_EVENTS = [
  { id: 'EVT-88360', received_at: t('09:10'), source_type: 'hl7v2',
    source_name: 'monitor-gateway-1', parse_status: 'parsed', parse_error: null,
    correlation_id: 'corr-ingest-61', vitals_inserted: 6, observations_inserted: 2,
    processed_at: t('09:10') },
  { id: 'EVT-88355', received_at: t('08:58'), source_type: 'hl7v2',
    source_name: 'monitor-gateway-1', parse_status: 'failed',
    parse_error: 'MSH segment malformed: missing field separator at position 3',
    correlation_id: 'corr-ingest-58', vitals_inserted: 0, observations_inserted: 0,
    processed_at: t('08:58') },
  { id: 'EVT-88350', received_at: t('08:45'), source_type: 'fhir',
    source_name: 'lab-lis-bridge', parse_status: 'parsed', parse_error: null,
    correlation_id: 'corr-ingest-57', vitals_inserted: 0, observations_inserted: 4,
    processed_at: t('08:45') },
  { id: 'EVT-88349', received_at: t('08:44'), source_type: 'fhir',
    source_name: 'lab-lis-bridge', parse_status: 'duplicate', parse_error: null,
    correlation_id: 'corr-ingest-56', vitals_inserted: 0, observations_inserted: 0,
    processed_at: t('08:44') },
  { id: 'EVT-88231', received_at: t('08:47'), source_type: 'hl7v2',
    source_name: 'monitor-gateway-1', parse_status: 'parsed', parse_error: null,
    correlation_id: 'corr-ingest-42', vitals_inserted: 6, observations_inserted: 0,
    processed_at: t('08:47') },
  { id: 'EVT-88210', received_at: t('07:30'), source_type: 'hl7v2',
    source_name: 'monitor-gateway-2', parse_status: 'parsed', parse_error: null,
    correlation_id: 'corr-ingest-40', vitals_inserted: 5, observations_inserted: 1,
    processed_at: t('07:30') },
];

function ingestStatusResponse(params) {
  const status = params.get('status');
  const events = status ? INGEST_EVENTS.filter((e) => e.parse_status === status) : INGEST_EVENTS;
  return { events, count: events.length, ingestion_mode: 'simulation' };
}

// ═══════════════════════════════════════════════════════════════════════════
// ADMIN — /overview bare object (map-backend-api.md:179), /models {models}
// (:180), provenance (:181), /impact/ (:182, trailing slash), /compliance/
// (:184, trailing slash; field shapes from TrustCenter.tsx:8-50).
// ═══════════════════════════════════════════════════════════════════════════
// Admin Users — GET /api/admin/users → {users:[...]} (routes/admin_routes.py)
const ADMIN_USERS = {
  users: [
    { id: 'DOC001', name: 'Dr. Meera Sharma', role: 'doctor', department: 'General Medicine', registration_number: 'MH-12345', totp_enabled: false, created_at: '2026-04-01T08:00:00', last_login: '2026-05-23T09:14:00', failed_logins_24h: 0 },
    { id: 'DOC002', name: 'Dr. Arjun Mehta', role: 'doctor', department: 'Cardiology', registration_number: 'MH-12346', totp_enabled: true, created_at: '2026-04-01T08:00:00', last_login: '2026-05-23T07:40:00', failed_logins_24h: 0 },
    { id: 'NUR001', name: 'Priya Reddy', role: 'nurse', department: 'General Medicine', registration_number: null, totp_enabled: false, created_at: '2026-04-01T08:00:00', last_login: '2026-05-23T06:15:00', failed_logins_24h: 2 },
    { id: 'PHA001', name: 'Deepa Joshi', role: 'pharmacist', department: 'Pharmacy', registration_number: null, totp_enabled: true, created_at: '2026-04-01T08:00:00', last_login: '2026-05-22T18:02:00', failed_logins_24h: 0 },
    { id: 'ADM001', name: 'Rajesh Kumar', role: 'admin', department: 'Administration', registration_number: null, totp_enabled: true, created_at: '2026-04-01T08:00:00', last_login: '2026-05-23T08:30:00', failed_logins_24h: 0 },
    { id: 'P001', name: 'Raj Patel', role: 'patient', department: null, registration_number: null, totp_enabled: false, created_at: '2026-05-20T10:00:00', last_login: '2026-05-23T05:50:00', failed_logins_24h: 0 },
  ],
};

// Admin Data & Backups — GET /api/admin/backups (routes/admin_routes.py)
const ADMIN_BACKUPS = {
  database: {
    filename: 'orb.db', size_bytes: 2490368, journal_mode: 'wal',
    encrypted_at_rest: false, uploads_encrypted: true,
    tables: { users: 13, patients: 30, admissions: 30, vitals: 4210, audit_logs: 1342, medications: 96, orders: 24, scribe_notes: 18 },
  },
  backups: [
    { filename: 'orb-backup-20260523-020000.db', size_bytes: 2481152, created_at: '2026-05-23T02:00:00' },
    { filename: 'orb-backup-20260522-020000.db', size_bytes: 2472960, created_at: '2026-05-22T02:00:00' },
    { filename: 'orb-backup-20260521-020000.db', size_bytes: 2465792, created_at: '2026-05-21T02:00:00' },
  ],
  backup_count: 3, backups_shown: 3, retention_days: 14,
};

const ADMIN_OVERVIEW = {
  ingestion_mode: 'simulation', audit_simulation_enabled: false,
  audit_total_24h: 1342,
  audit_categories: { auth: 214, clinical: 698, ingestion: 430 },
  audit_outcomes: { success: 1298, failure: 44 },
  ingestion: { total: 430, parsed: 402, failed: 18, duplicates: 10,
    total_vitals: 2210, total_observations: 640 },
  source_mix: { hl7v2: 280, fhir: 150 },
  alerts: { vigil: 6, relay: 3 },
  recent_failures: [
    { id: 'EVT-88355', received_at: t('08:58'), source_type: 'hl7v2',
      parse_error: 'MSH segment malformed: missing field separator at position 3',
      correlation_id: 'corr-ingest-58' },
  ],
  auth_events: [
    { action: 'login', outcome: 'success', count: 57 },
    { action: 'login', outcome: 'failure', count: 4 },
    { action: 'logout', outcome: 'success', count: 41 },
  ],
};

const ADMIN_MODELS = { models: [
  { model_key: 'vigil-news2-cfc', version: '1.3.0', task: 'deterioration_risk',
    training_data: '18 months de-identified ward vitals (n=42,000 episodes)',
    validated: 1, is_active: 1, checkpoint_present: 1,
    notes: 'Calibrated quarterly against NEWS2 outcomes.',
    validation_metrics: { auroc: 0.87, auprc: 0.41, sensitivity: 0.82, specificity: 0.79 },
    calibration_stats: { brier: 0.11, ece: 0.03, last_calibrated: '2026-04-30' } },
  { model_key: 'forecast-los-v2', version: '0.9.1', task: 'length_of_stay',
    training_data: 'Pilot admissions export (n=3,100) — pending governance sign-off',
    validated: 0, is_active: 0, checkpoint_present: 1,
    notes: 'Unvalidated — heuristic fallback in production.',
    validation_metrics: {}, calibration_stats: {} },
] };

const ADMIN_MODELS_PROVENANCE = { models: [
  { model_key: 'vigil-news2-cfc', version: '1.3.0', task: 'deterioration_risk',
    training_data: '18 months de-identified ward vitals (n=42,000 episodes)',
    validated: true, checkpoint_present: true, is_active: true },
  { model_key: 'forecast-los-v2', version: '0.9.1', task: 'length_of_stay',
    training_data: 'Pilot admissions export (n=3,100)',
    validated: false, checkpoint_present: true, is_active: false },
] };

const ADMIN_IMPACT = {
  generated_at: t('09:00'),
  period: { days: 30, start: '2026-04-23', end: '2026-05-23' },
  metrics: [
    { key: 'icu_days_avoided', label: 'ICU days avoided via early deterioration alerts',
      value: 9, unit: 'days', dollars: 21600, assumption: 'cost_per_icu_day', evidence_count: 6 },
    { key: 'readmissions_prevented', label: 'Readmissions prevented by discharge follow-up',
      value: 3, unit: 'readmissions', dollars: 10500, assumption: 'cost_per_readmission', evidence_count: 3 },
    { key: 'documentation_hours_saved', label: 'Clinician hours saved by Scribe',
      value: 118, unit: 'hours', dollars: 7080, assumption: 'clinician_hourly_cost', evidence_count: 118 },
    { key: 'med_errors_intercepted', label: 'Medication errors intercepted at verification',
      value: 11, unit: 'errors', dollars: 2070, assumption: 'cost_per_med_error', evidence_count: 11 },
  ],
  // ICU days and adverse drug events are separate units, so the server reports
  // them separately (it used to add them into one events_prevented figure).
  totals: { monthly_savings: 41250, annual_savings: 495000, icu_days_avoided: 9, ades_avoided: 11 },
  assumptions: {
    cost_per_icu_day: { value: 2400, unit: 'USD/day',
      citation: 'Dasta et al., Crit Care Med 2005 (inflation-adjusted, illustrative)' },
    cost_per_readmission: { value: 3500, unit: 'USD/readmission',
      citation: 'AHRQ HCUP statistical brief #248 (illustrative)' },
    clinician_hourly_cost: { value: 60, unit: 'USD/hour',
      citation: 'Facility loaded-cost estimate, finance office 2026' },
    cost_per_med_error: { value: 188, unit: 'USD/error',
      citation: 'Bates et al., JAMA 1997 (inflation-adjusted, illustrative)' },
  },
  has_data: true,
};

const ADMIN_COMPLIANCE = (() => {
  const roles = ['doctor', 'nurse', 'pharmacist', 'admin', 'patient'];
  const permissions = ['vigil.read', 'orders.write', 'helix.read', 'admin.read', 'bridge.read_own'];
  const grants = {
    doctor: ['vigil.read', 'orders.write'],
    nurse: ['vigil.read', 'orders.write'],
    pharmacist: ['helix.read'],
    admin: ['vigil.read', 'admin.read'],
    patient: ['bridge.read_own'],
  };
  const matrix = {};
  for (const r of roles) {
    matrix[r] = {};
    for (const p of permissions) matrix[r][p] = grants[r].includes(p);
  }
  return {
    generated_at: t('09:00'),
    overall_score: 82,
    controls: [
      { id: 'audit-chain', name: 'Tamper-evident audit chain', status: 'pass',
        evidence: '1,342 sealed rows verified end-to-end', remediation: null },
      { id: 'encryption-at-rest', name: 'Encryption at rest', status: 'pass',
        evidence: 'SQLCipher DB + encrypted uploads volume', remediation: null },
      { id: 'rbac', name: 'Role-based access control', status: 'pass',
        evidence: '5 roles × 5 permission groups enforced per-route', remediation: null },
      { id: 'mfa', name: 'MFA adoption', status: 'warn',
        evidence: '9/29 users enrolled (31%)',
        remediation: 'Enforce TOTP enrollment for the 3 remaining mandated doctors.' },
      { id: 'backups', name: 'Backups', status: 'warn',
        evidence: 'Last verified restore 9 days ago',
        remediation: 'Schedule weekly restore drills.' },
      { id: 'session-timeout', name: 'Idle session timeout', status: 'fail',
        evidence: 'No idle timeout configured for clinician sessions',
        remediation: 'Set ORB_SESSION_IDLE_TIMEOUT to 15 minutes.' },
    ],
    mfa: {
      required_roles: ['admin', 'doctor'],
      by_role: {
        doctor: { total: 8, enrolled: 5 },
        nurse: { total: 6, enrolled: 2 },
        pharmacist: { total: 2, enrolled: 1 },
        admin: { total: 1, enrolled: 1 },
        patient: { total: 12, enrolled: 0 },
      },
      total_users: 29, total_enrolled: 9, adoption_pct: 31,
      required_gap: ['DOC002', 'DOC005', 'DOC006'],
    },
    rbac_matrix: { roles, permissions, matrix, role_count: roles.length,
      permission_count: permissions.length },
    audit_chain: { verified: true, broken_at: null, count: 1342 },
    audit_coverage: { total_events: 1342,
      by_category: { clinical: 698, ingestion: 430, auth: 214 }, categories: 3 },
    encryption: { db_at_rest: true, uploads_at_rest: true,
      db_encryption_key_set: true, upload_encryption_key_set: false },
    residency: { host: '127.0.0.1', loopback_only: true,
      cors_origins: ['http://localhost:5173'], deployment_profile: 'on-prem', on_prem: true },
    models: { validated: 1, total: 2, unvalidated: 1 },
  };
})();

// ═══════════════════════════════════════════════════════════════════════════
// FLIGHT RECORDER — {events,count} (map-backend-api.md:205-208). All chain_ok
// true + verify {ok:true} so the tamper gate is NOT tripped by fixtures.
// ═══════════════════════════════════════════════════════════════════════════
const FLIGHT_EVENTS = (() => {
  const sources = ['audit', 'event', 'order'];
  const actions = ['record_view', 'vitals_ingested', 'order_transition', 'note_created',
    'alert_generated', 'medication_verified'];
  const actors = ['DOC001', 'system', 'PHA001', 'NUR001'];
  const patients = ['PAT001', 'PAT002', 'PAT003', 'PAT007'];
  const out = [];
  for (let i = 0; i < 20; i++) {
    const minutes = 554 - i * 11; // descending from 09:14
    const hh = String(Math.floor(minutes / 60)).padStart(2, '0');
    const mm = String(minutes % 60).padStart(2, '0');
    out.push({
      id: 6100 - i,
      ts: `${D}T${hh}:${mm}:00`,
      source: sources[i % 3],
      actor: actors[i % 4],
      action: actions[i % 6],
      target: patients[i % 4],
      detail: `${actions[i % 6].replace(/_/g, ' ')} for ${patients[i % 4]}`,
      chain_ok: true,
      patient_id: patients[i % 4],
      correlation_id: i % 5 === 0 ? 'corr-ingest-42' : `corr-fr-${6100 - i}`,
    });
  }
  return out;
})();

function flightTimelineResponse(params) {
  const pid = params.get('patient_id');
  const events = pid ? FLIGHT_EVENTS.filter((e) => e.patient_id === pid) : FLIGHT_EVENTS;
  return { events, count: events.length };
}

function flightEventDetail(source, id) {
  const row = FLIGHT_EVENTS.find((e) => String(e.id) === String(id)) || FLIGHT_EVENTS[0];
  return Object.assign({}, row, {
    source,
    prev_hash: 'a41f09c2be77d31e5a90',
    row_hash: 'f7c3d8091b2ea6540cd1',
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// COMMAND CENTER — full house snapshot (map-backend-api.md:189-199; field
// shapes CommandCenter.tsx:6-19). Registered with and without trailing slash.
// ═══════════════════════════════════════════════════════════════════════════
const COMMAND_CENTER = {
  generated_at: t('09:15'),
  census: {
    total_admitted: 22, total_capacity: 30, occupancy_pct: 73.3,
    by_ward: [
      { ward: 'ICU', occupied: 6, capacity: 6, occupancy_pct: 100.0 },
      { ward: 'W1', occupied: 7, capacity: 8, occupancy_pct: 87.5 },
      { ward: 'W2', occupied: 6, capacity: 8, occupancy_pct: 75.0 },
      { ward: 'W3', occupied: 3, capacity: 8, occupancy_pct: 37.5 },
    ],
  },
  acuity: {
    critical: 2, elevated: 3, stable: 17,
    heatmap: [
      { ward: 'ICU', critical: 2, elevated: 1, stable: 3 },
      { ward: 'W1', critical: 0, elevated: 2, stable: 5 },
      { ward: 'W2', critical: 0, elevated: 0, stable: 6 },
      { ward: 'W3', critical: 0, elevated: 0, stable: 3 },
    ],
  },
  deteriorations: [
    { patient_id: 'PAT002', name: 'Kavitha Menon', ward: 'ICU', bed: 'CCU-02',
      level: 'critical', news2: 9 },
    { patient_id: 'PAT001', name: 'Suresh Reddy', ward: 'W1', bed: 'GM-104',
      level: 'elevated', news2: 6 },
  ],
  bundles: { active: 1, compliant: 0, compliance_pct: 67 },
  forecast: { projected_admissions_24h: 4, projected_occupancy: 24,
    projected_occupancy_pct: 80.0, current_occupancy: 22, total_capacity: 30 },
  downtime_readiness: { patients_with_recent_snapshot: 20, total: 22, pct: 90.9,
    oldest_snapshot_age_min: 58.2 },
  or_today: {
    count: 2,
    cases: [
      { id: 'SUR-001', procedure_name: 'ORIF femur shaft fracture', operating_room: 'OT-1',
        scheduled_time: '08:00', status: 'in_progress', patient_name: 'Farhan Sheikh', ward: 'W2' },
      { id: 'SUR-002', procedure_name: 'Emergency laparoscopic appendicectomy',
        operating_room: 'OT-2', scheduled_time: '10:30', status: 'scheduled',
        patient_name: 'Mohan Das', ward: 'W2' },
    ],
  },
};

// ═══════════════════════════════════════════════════════════════════════════
// CAPACITY SIM — POST, both slash forms (map-backend-api.md:251; shape
// CapacitySimulator.tsx:20-45). Deterministic 48h projection at 4h steps.
// ═══════════════════════════════════════════════════════════════════════════
const CAPACITY_SIM = (() => {
  const occ = [22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 30, 29, 28];
  return {
    baseline: { census: 22, capacity: 30, admission_rate: 0.38 },
    scenario: {
      horizon_hours: 48, extra_admissions: 0, beds_delta: 0,
      admission_rate_multiplier: 1, los_multiplier: 1, discharge_boost: 0,
      nurse_patient_ratio: 4, effective_capacity: 30,
    },
    projection: occ.map((o, i) => ({
      step: i, hour_offset: i * 4, projected_occupancy: o, capacity: 30,
      overflow: Math.max(0, o - 30), occupancy_pct: Math.round((o / 30) * 1000) / 10,
    })),
    summary: {
      peak_occupancy: 31, peak_occupancy_pct: 103.3, peak_hour_offset: 36,
      hours_to_overflow: 32, beds_short_at_peak: 1, suggested_discharges: 3,
      staffing_gap_nurses: 2, nurses_required_at_peak: 8,
      projected_admissions: 18, projected_discharges: 9,
    },
    assumptions: {
      admission_rate: { value: '0.38 admissions/hour', source: 'Poisson fit on 30-day admissions' },
      length_of_stay: { value: '4.2 days mean', source: 'Orb Forecast LOS model' },
      nurse_ratio: { value: '1 nurse : 4 patients', source: 'Hospital staffing policy' },
    },
  };
})();

// ═══════════════════════════════════════════════════════════════════════════
// CODING / revenue integrity (map-backend-api.md:186-187; shapes
// RevenueIntegrityPanel.tsx:16-51)
// ═══════════════════════════════════════════════════════════════════════════
function codingFor(id) {
  return {
    patient_id: id,
    suggested_codes: [
      { code: 'J18.9', system: 'ICD-10', display: 'Pneumonia, unspecified organism',
        evidence: 'Admission note: "CAP with early sepsis"; CXR consolidation.', confidence: 0.93 },
      { code: 'A41.9', system: 'ICD-10', display: 'Sepsis, unspecified organism',
        evidence: 'Lactate 2.1, qSOFA 2, vasopressor threshold documented.', confidence: 0.71 },
      { code: '233604007', system: 'SNOMED-CT', display: 'Pneumonia (disorder)',
        evidence: 'Structured problem list entry.', confidence: 0.88 },
    ],
    cdi_flags: [
      { type: 'specificity', message: 'Pneumonia organism not specified',
        rationale: 'Sputum culture pending — coding to J15.x once resulted increases specificity.',
        est_impact_usd: 850 },
      { type: 'missing_comorbidity', message: 'Type 2 DM documented but not coded',
        rationale: 'Comorbidity affects DRG weighting and severity of illness.',
        est_impact_usd: 420 },
    ],
    reimbursement: {
      captured_usd: 3480, potential_usd: 4750, uplift_usd: 1270,
      assumptions: {
        model: 'Illustrative CMS-weighted DRG model',
        method: 'Sum of suggested-code weights × base rate',
        base_rate_usd: 1450,
        caveats: ['Illustrative only — not a payer contract', 'Excludes outlier payments'],
        citations: ['CMS IPPS FY2026 final rule (illustrative weights)'],
        code_weights: { 'J18.9': 1.2, 'A41.9': 1.9 },
      },
    },
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// BUNDLES — {bundles:[...]} (map-backend-api.md:160-162; element states from
// CareBundleTimer.tsx:66-69). One active sepsis6 bundle, elapsed 32 min, mixing
// done_on_time / pending / overdue, on_track false.
// ═══════════════════════════════════════════════════════════════════════════
function sepsisBundle(patientId) {
  return {
    id: 71, patient_id: patientId, bundle_type: 'sepsis6',
    // ~35 min before the harness's frozen clock (09:00) so the live elapsed
    // timer reads a realistic ~35m and the 30m-target antibiotic reads overdue.
    triggered_at: t('08:25'), triggered_by: 'DOC001',
    trigger_reason: 'Lactate 3.2 mmol/L with new hypotension',
    lactate_at: t('08:45'), blood_cultures_at: null, antibiotics_at: null,
    fluids_at: null, oxygen_at: t('08:33'), urine_output_at: null,
    status: 'active', completed_at: null, source: 'manual',
    elapsed_min: 35,
    elements: [
      { key: 'oxygen', label: 'High-flow oxygen', target_min: 15,
        completed_at: t('08:33'), state: 'done_on_time' },
      { key: 'lactate', label: 'Measure lactate', target_min: 60,
        completed_at: t('08:45'), state: 'done_on_time' },
      { key: 'antibiotics', label: 'IV broad-spectrum antibiotics', target_min: 30,
        completed_at: null, state: 'overdue' },
      { key: 'blood_cultures', label: 'Blood cultures before antibiotics', target_min: 60,
        completed_at: null, state: 'pending' },
      { key: 'fluids', label: 'IV fluid bolus 30ml/kg', target_min: 60,
        completed_at: null, state: 'pending' },
      { key: 'urine_output', label: 'Urine output monitoring', target_min: 240,
        completed_at: null, state: 'pending' },
    ],
    completed_count: 2, total_count: 6, completion_pct: 33.3,
    compliance_pct: 67, on_track: false,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// BRIDGE (patient role) — bare objects (map-backend-api.md:106-110). status
// 'elevated' exercises the warn badge (PatientShell badge --warn).
// ═══════════════════════════════════════════════════════════════════════════
const BRIDGE_STATUS = {
  patient_name: 'Suresh Reddy', bed: 'GM-104', ward: 'W1',
  diagnosis_simple: 'A chest infection that is being treated with intravenous antibiotics.',
  status: 'elevated',
  status_message: 'Your care team is watching your breathing closely today. Your treatment plan is on track.',
  vitals: { heart_rate: '96 bpm', blood_pressure: '118/76', oxygen: '93%', temperature: '37.9°C' },
  attending_doctor: 'Dr. Meera Sharma',
};

const BRIDGE_REPORTS = {
  patient_id: 'PAT001',
  medications: [
    { drug_name: 'Piperacillin-Tazobactam', dosage: '4.5g', route: 'IV',
      frequency: 'Every 6 hours', status: 'active', started_at: onDay('21', '09:00') },
    { drug_name: 'Azithromycin', dosage: '500mg', route: 'By mouth',
      frequency: 'Once daily', status: 'active', started_at: onDay('21', '10:15') },
    { drug_name: 'Paracetamol', dosage: '1g', route: 'By mouth',
      frequency: 'As needed for fever', status: 'active', started_at: onDay('21', '09:05') },
  ],
  clinical_documents: [
    { doc_type: 'Progress Note', title: 'Morning ward round',
      content: 'Day 2 of treatment for a chest infection. Fever persists but overall condition is stable. Antibiotics continue.',
      created_at: t('08:05') },
    { doc_type: 'Admission Note', title: 'Admission assessment',
      content: 'Admitted with fever and cough. Started on intravenous antibiotics for pneumonia.',
      created_at: onDay('21', '08:30') },
  ],
  discharge_summary: null,
  imaging_reports: [
    { image_type: 'xray', clinical_context: 'Fever and productive cough',
      analysis: 'Chest X-ray shows an area of infection in the lower right lung, consistent with pneumonia.',
      created_at: t('07:10') },
  ],
};

const BRIDGE_ASK_SSE = sse([
  { content: 'Your antibiotics are working as expected. ' },
  { content: 'The care team is monitoring your oxygen levels closely, ' },
  { content: 'and your doctor will review you again this evening.' },
  { done: true },
]);

// ═══════════════════════════════════════════════════════════════════════════
// I-PASS handoff draft (map-backend-api.md:164-166; field usage
// IpassHandoffView.tsx:125-278). illness_severity 'Watcher'.
// ═══════════════════════════════════════════════════════════════════════════
function ipassDraft(id) {
  const p = patientById(id);
  return {
    patient_id: p.id, generated_at: t('09:00'), window_hours: 12,
    source_summary: 'Auto-drafted from vitals, notes, orders and bundles over the last 12 hours.',
    sections: {
      illness_severity: {
        severity: 'Watcher', news2: 6, news2_level: 'elevated',
        drivers: ['RR 26', 'SpO2 92% on room air', 'Temp 38.6°C'],
        text: 'Elevated early-warning score, not yet critical — needs close watching overnight.',
      },
      patient_summary: {
        name: p.name, age: p.age, sex: p.sex, ward: p.ward, bed: p.bed,
        diagnosis: p.diagnosis,
        problems: ['Community-acquired pneumonia', 'Type 2 diabetes mellitus'],
        allergies: ['Penicillin (anaphylaxis)'],
        text: `${p.age}y ${p.sex}, day 2 of ${p.diagnosis}. Slow response to first-line antibiotics.`,
      },
      action_list: {
        active_medications: [
          { drug_name: 'Piperacillin-Tazobactam', dosage: '4.5g', route: 'IV', frequency: 'Q6H' },
          { drug_name: 'Azithromycin', dosage: '500mg', route: 'PO', frequency: 'OD' },
          { drug_name: 'Paracetamol', dosage: '1g', route: 'PO', frequency: 'PRN' },
        ],
        open_care_bundles: [
          { bundle_type: 'sepsis6', compliance_pct: 67,
            outstanding: ['IV antibiotics', 'Blood cultures', 'Fluid bolus'] },
        ],
        outstanding_items: [
          'Repeat CBC/CRP with 06:00 bloods',
          'Chase sputum culture result',
          'Senior review if NEWS2 reaches 7',
        ],
      },
      situation_awareness: {
        trend: 'worsening',
        trend_detail: 'HR up 14 bpm and SpO2 down 2% over the shift',
        active_deterioration: false,
        latest_vitals: { heart_rate: 118, systolic_bp: 104, diastolic_bp: 70,
          spo2: 92, respiratory_rate: 26, temperature: 38.6 },
        contingencies: [
          'If SpO2 < 90% on air: start 2L nasal cannula and inform on-call registrar',
          'If SBP < 100: 500ml crystalloid bolus and reassess in 30 minutes',
          'If NEWS2 ≥ 7: activate rapid response',
        ],
      },
      synthesis: {
        text: 'Receiver to confirm: watch respiratory trajectory overnight, chase morning bloods, escalate at NEWS2 7. Sepsis bundle antibiotics element is overdue and must be closed out first.',
      },
    },
    plain_text:
      'I-PASS HANDOFF — Suresh Reddy (MRN-GM004)\nI: Watcher — NEWS2 6.\nP: 55M, day 2 CAP with early sepsis.\nA: Pip-taz 4.5g IV Q6H; sepsis bundle antibiotics OVERDUE.\nS: Worsening resp trend; escalate at NEWS2 7.\nS: Confirm plan and morning bloods.',
  };
}

const IPASS_RECENT = { handoffs: [
  { id: 801, patient_id: 'PAT001', author_id: 'DOC001',
    raw_transcript: 'Handing over Suresh Reddy, day 2 CAP, watcher status...',
    structured_note: 'I-PASS handoff — Watcher. Continue antibiotics, escalate at NEWS2 7.',
    note_format: 'handoff', status: 'signed', created_at: onDay('22', '20:30') },
] };

// ═══════════════════════════════════════════════════════════════════════════
// NURSE SHIFT BOARD — GET /api/nurse/board
// (shape: src/api/nurse.ts ShiftBoard; server/services/nurse_shift_service.py)
//
// THE NURSE HOME HAD NEVER BEEN CAPTURED IN ANY THEME. The only nurse scenario
// in driver.cjs waited on `.patient-row`, which is Vigil's roster — and the
// nurse's landing tab is `shift` (shellConfig.tsx), so that selector never
// appeared on the screen the scenario was named after. There was no
// /api/nurse/board fixture at all, so even a corrected scenario would have
// rendered the read-failure state.
//
// Three boards, because the nurse home's states are the point of its design and
// a populated capture proves only one of them:
//   NURSE_BOARD       — a bad shift: 1 critical, obs 42 min late, a late dose.
//                       Exercises the danger/warn count chips, both status left
//                       rules, the filled NEWS2 bands and the trailing lateness.
//   NURSE_BOARD_QUIET — a good shift: nothing overdue, nothing due within 30
//                       minutes. This is the CALM EMPTY STATE, the single most
//                       important state on the screen and the one most likely
//                       to regress into looking like a failure.
//   NURSE_BOARD_EMPTY — no patients in scope: the dashed absence box, which
//                       must not look like the calm one.
// ═══════════════════════════════════════════════════════════════════════════
function nurseNews2(raw, level, drivers, single3 = false) {
  return {
    score: Math.min(1, raw / 12), level, raw_news2: raw, single_param_3: single3,
    drivers, breakdown: [], scoring_model: 'NEWS2_RCP_2017', spo2_scale: 1,
  };
}

/** One eMAR row. Due times are DERIVED, never prescriber-authored. */
function nurseMed(o) {
  return Object.assign({
    medication_id: 1, drug_name: 'Paracetamol', dosage: '1 g', route: 'PO',
    frequency: 'QDS', schedule_kind: 'scheduled', interval_minutes: 360,
    status: 'active', last_given_at: t('05:00'), given_count: 4,
    due_at: t('11:00'), due_basis: 'last_given', missed_slots: 0,
    minutes_until_due: 120, overdue: false, due_soon: false, unverified: false,
  }, o);
}

/** Built off VIGIL_PATIENTS so a bed on My Shift is the same bed on Vigil. */
function nurseCard(id, o) {
  const p = patientById(id);
  return Object.assign({
    patient_id: p.id, name: p.name, mrn: p.mrn, bed: p.bed, ward: p.ward,
    age: p.age, sex: p.sex, diagnosis: p.diagnosis,
    department: p.department, attending_doctor_name: p.attending_doctor_name,
    on_scale_2: false, allergies: [], news2: null,
    last_obs_at: t('08:15'), obs_interval_minutes: 240, obs_band: 'low',
    obs_response: 'Continue routine monitoring.',
    obs_due_at: t('12:15'), obs_minutes_until_due: 195,
    obs_overdue: false, obs_due_soon: false,
    medications: [], meds_overdue: 0, meds_due_soon: 0,
  }, o);
}

const NURSE_BOARD = {
  generated_at: t('09:00'),
  department: 'General Medicine',
  patients: [
    nurseCard('PAT003', {
      allergies: [{ substance: 'Penicillin', reaction: 'Anaphylaxis', severity: 'severe' }],
      news2: nurseNews2(9, 'critical', ['RR 26', 'SpO2 88% on 2L'], true),
      obs_interval_minutes: 30, obs_band: 'critical',
      obs_response: 'Continuous monitoring and immediate senior review.',
      last_obs_at: t('07:48'), obs_due_at: t('08:18'),
      obs_minutes_until_due: -42, obs_overdue: true,
    }),
    nurseCard('PAT001', {
      news2: nurseNews2(5, 'elevated', ['RR 26', 'Temp 38.6°C']),
      obs_interval_minutes: 60, obs_band: 'medium',
      obs_response: 'Hourly observations; urgent review by a clinician.',
      last_obs_at: t('08:20'), obs_due_at: t('09:20'),
      obs_minutes_until_due: 20, obs_due_soon: true,
      medications: [
        nurseMed({
          medication_id: 41, drug_name: 'Piperacillin-tazobactam', dosage: '4.5 g',
          route: 'IV', frequency: '8-hourly', interval_minutes: 480,
          last_given_at: onDay('22', '20:48'), given_count: 2,
          due_at: t('08:48'), missed_slots: 1,
          minutes_until_due: -12, overdue: true,
        }),
        nurseMed({ medication_id: 42, drug_name: 'Azithromycin', dosage: '500 mg', route: 'PO', frequency: 'OD' }),
      ],
      meds_overdue: 1,
    }),
    nurseCard('PAT004', {
      news2: nurseNews2(4, 'elevated', ['HR 118']),
      obs_interval_minutes: 60, obs_band: 'medium',
      obs_response: 'Hourly observations.',
      last_obs_at: t('08:40'), obs_due_at: t('09:40'), obs_minutes_until_due: 40,
      medications: [nurseMed({ medication_id: 43, drug_name: 'Insulin (variable rate)', dosage: '50 units/50 mL', route: 'IV', schedule_kind: 'continuous', due_at: null, minutes_until_due: null })],
    }),
    nurseCard('PAT006', { news2: nurseNews2(1, 'stable', []) }),
    nurseCard('PAT005', { news2: nurseNews2(0, 'stable', []) }),
  ],
  totals: { patients: 5, critical: 1, elevated: 2, obs_overdue: 1, meds_overdue: 1 },
};

const NURSE_BOARD_QUIET = {
  generated_at: t('09:00'),
  department: 'General Medicine',
  patients: NURSE_BOARD.patients.map((p, i) => Object.assign({}, p, {
    news2: nurseNews2(i === 0 ? 2 : 1, 'stable', []),
    obs_interval_minutes: 240, obs_band: 'low',
    obs_response: 'Continue routine monitoring.',
    last_obs_at: t('08:30'),
    // The nearest is PAT003 at 10:30 — the bed and clock time the calm state's
    // second line has to name for "nothing" to become "nothing YET".
    obs_due_at: t(i === 0 ? '10:30' : '12:30'),
    obs_minutes_until_due: i === 0 ? 90 : 210,
    obs_overdue: false, obs_due_soon: false,
    medications: p.medications.map((m) => Object.assign({}, m, {
      due_at: t('13:00'), minutes_until_due: 240, missed_slots: 0,
      overdue: false, due_soon: false,
    })),
    meds_overdue: 0, meds_due_soon: 0,
  })),
  totals: { patients: 5, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
};

const NURSE_BOARD_EMPTY = {
  generated_at: t('09:00'),
  department: 'General Medicine',
  patients: [],
  totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
};

// Current + previous shift for the charge nurse's End of shift tab. Ward and
// period strings are echoed back verbatim by the POST, so they are real here.
const NURSE_SHIFT_STATUS = {
  generated_at: t('09:00'),
  status_available: true,
  max_backdate_hours: 72,
  event_types: ['icu_transfer', 'cardiac_arrest', 'rapid_response', 'unexpected_death', 'emergency_surgery'],
  sources: ['nurse_recognised', 'doctor_recognised', 'orb_alert', 'family_escalation'],
  recognition: ['bedside_nurse', 'charge_nurse', 'resident', 'consultant', 'rapid_response_team'],
  shifts: [
    {
      shift: 'day', period_start: t('07:00'), period_end: t('15:00'),
      label: '07:00–15:00', is_current: true,
      wards: [{ ward: 'General Medicine', confirmed: false, confirmed_at: null,
        confirmed_by_role: null, events_recorded: 0, status: 'outstanding' }],
    },
    {
      shift: 'night', period_start: onDay('22', '23:00'), period_end: t('07:00'),
      label: '23:00–07:00', is_current: false,
      wards: [{ ward: 'General Medicine', confirmed: true, confirmed_at: t('07:12'),
        confirmed_by_role: 'nurse', events_recorded: 0, status: 'confirmed' }],
    },
  ],
};

// ═══════════════════════════════════════════════════════════════════════════
// DOWNTIME — snapshot envelope (map-backend-api.md:168-170; exact fields
// src/api/downtime.ts:96-118 read by DowntimeMode.tsx)
// ═══════════════════════════════════════════════════════════════════════════
function downtimeEnvelope(patientId, envId, createdAt) {
  const p = patientById(patientId);
  return {
    id: envId, patient_id: p.id,
    summary: `${p.name} — ${p.diagnosis}. 3 active medications, NEWS2 6 (elevated).`,
    created_by: 'auto', created_at: createdAt,
    snapshot: {
      schema_version: 1, captured_at: createdAt, patient_id: p.id,
      demographics: {
        id: p.id, name: p.name, mrn: p.mrn, bed: p.bed, ward: p.ward,
        admission_date: p.admission_date, diagnosis: p.diagnosis, status: 'admitted',
        age: p.age, date_of_birth: '1971-02-14', sex: p.sex, phone: '+91-98450-11223',
        emergency_contact_name: 'Radhika Reddy', emergency_contact_phone: '+91-98450-33445',
        address: '14 Lakeview Road, Bengaluru', attending_doctor_id: 'DOC001',
        attending_doctor_name: p.attending_doctor_name,
      },
      allergies: [
        { substance: 'Penicillin', substance_class: 'beta-lactam', reaction: 'Anaphylaxis',
          severity: 'severe', status: 'active', source: 'clinician' },
        { substance: 'Sulfa drugs', substance_class: 'sulfonamide', reaction: 'Rash',
          severity: 'moderate', status: 'active', source: 'clinician' },
      ],
      problems: [
        { problem: 'Community-acquired pneumonia', icd10_code: 'J18.9',
          snomed_code: '233604007', status: 'active', onset_date: '2026-05-21', source: 'clinician' },
        { problem: 'Type 2 diabetes mellitus', icd10_code: 'E11.9',
          snomed_code: '44054006', status: 'active', onset_date: '2019-03-01', source: 'clinician' },
      ],
      medications: DETAIL_MEDICATIONS.map((m) => ({
        drug_name: m.drug_name, dosage: m.dosage, route: m.route,
        frequency: m.frequency, status: m.status, source: 'clinician', notes: m.notes,
      })),
      latest_vitals: {
        timestamp: t('09:15'), heart_rate: 118, systolic_bp: 104, diastolic_bp: 70,
        spo2: 92, temperature: 38.6, respiratory_rate: 26,
        consciousness: 'Alert', on_oxygen: 0,
      },
      news2: { raw_news2: 6, score: 6, level: 'elevated',
        drivers: ['RR 26', 'SpO2 92%', 'Temp 38.6°C'], scoring_model: 'NEWS2', spo2_scale: 1 },
      recent_notes: [
        { id: 41, note_format: 'soap', structured_note: 'Day 2 CAP — fever persists, continue IV antibiotics.',
          author_id: 'DOC001', signed_by: null, signed_at: null, created_at: t('08:05') },
      ],
      recent_risk_scores: RISK_HISTORY.slice(-4).map((r) => ({
        timestamp: r.timestamp, score: r.score, level: r.level, drivers: r.drivers,
      })),
      counts: { allergies: 2, problems: 2, medications: 3, notes: 1 },
    },
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// PULSE dashboard (map-backend-api.md:201-203; field usage PulseWidget.tsx:
// 238-381). AQI 162 'Unhealthy'; one 'high' severity weather alert.
// ═══════════════════════════════════════════════════════════════════════════
const PULSE_DASHBOARD = {
  weather: {
    current: { temperature: 34.2, feels_like: 38.1, humidity: 62,
      weather_code: 95, description: 'Thunderstorm' },
    forecast: [
      { day_label: 'Sat May 23', weather_code: 95, temp_max: 35, temp_min: 26 },
      { day_label: 'Sun May 24', weather_code: 80, temp_max: 33, temp_min: 25 },
      { day_label: 'Mon May 25', weather_code: 3, temp_max: 34, temp_min: 26 },
      { day_label: 'Tue May 26', weather_code: 1, temp_max: 36, temp_min: 27 },
      { day_label: 'Wed May 27', weather_code: 2, temp_max: 36, temp_min: 27 },
    ],
    clinical_flags: ['Heat stress risk for elderly patients', 'Storm may delay inter-facility transfers'],
  },
  air_quality: {
    aqi: 162, category: 'Unhealthy', color: 'red',
    pollutants: { pm2_5: 78.4, pm10: 142.0, o3: 31.2, no2: 44.8 },
    clinical_flags: [
      'Expect increased COPD/asthma exacerbations over 24-48h',
      'Advise high-risk discharged patients to limit outdoor activity',
    ],
  },
  flu_surveillance: {
    source: 'CDC FluView (Seasonal Estimate)', week: 21, year: 2026,
    ili_percentage: 3.8, total_patients: 12480, activity_level: 'moderate',
    clinical_flags: ['ILI trending up 0.4% week-over-week'],
    last_updated: t('06:00'),
  },
  drug_recalls: [
    { product: 'Metformin HCl ER 750mg tablets', reason: 'NDMA impurity above acceptable intake limit',
      classification: 'Class II', recall_date: '2026-05-18', firm: 'Generic Pharma Ltd' },
    { product: 'Sodium chloride 0.9% injection 500ml', reason: 'Particulate matter observed in single lot',
      classification: 'Class I', recall_date: '2026-05-20', firm: 'InfuCare Labs' },
  ],
  active_alerts: [
    { type: 'weather', severity: 'high', icon: 'storm',
      title: 'Severe thunderstorm warning until 18:00',
      message: 'Damaging winds and heavy rain expected. Possible power fluctuation — verify generator readiness and downtime snapshots.',
      affected_conditions: ['Oxygen-dependent patients', 'Scheduled transfers'],
      recommended_actions: ['Run downtime capture-all', 'Confirm generator fuel level', 'Hold non-urgent transfers'] },
  ],
  alert_count: 1,
  last_refresh: t('09:12'),
  data_direction: 'INBOUND_ONLY — zero patient data transmitted',
};

// ═══════════════════════════════════════════════════════════════════════════
// CLINICAL RECORDS — {allergies}/{problems}/medication-safety
// (map-backend-api.md:155-158)
// ═══════════════════════════════════════════════════════════════════════════
function allergiesFor(id) {
  return { allergies: [
    { id: 1, patient_id: id, substance: 'Penicillin', substance_class: 'beta-lactam',
      reaction: 'Anaphylaxis', severity: 'severe', status: 'active',
      source: 'clinician', recorded_by: 'DOC001', recorded_at: onDay('21', '08:35') },
    { id: 2, patient_id: id, substance: 'Sulfa drugs', substance_class: 'sulfonamide',
      reaction: 'Rash', severity: 'moderate', status: 'active',
      source: 'clinician', recorded_by: 'DOC001', recorded_at: onDay('21', '08:36') },
  ] };
}

function problemsFor(id) {
  return { problems: [
    { id: 1, patient_id: id, problem: 'Community-acquired pneumonia', icd10_code: 'J18.9',
      snomed_code: '233604007', status: 'active', onset_date: '2026-05-21',
      source: 'clinician', recorded_by: 'DOC001', recorded_at: onDay('21', '08:40'), resolved_at: null },
    { id: 2, patient_id: id, problem: 'Type 2 diabetes mellitus', icd10_code: 'E11.9',
      snomed_code: '44054006', status: 'active', onset_date: '2019-03-01',
      source: 'clinician', recorded_by: 'DOC001', recorded_at: onDay('21', '08:41'), resolved_at: null },
  ] };
}

function medicationSafetyFor(params) {
  return {
    drug: params.get('drug') || 'paracetamol',
    allergy_alerts: [], interaction_alerts: [],
    max_severity: 'none', blocking: false, safe: true,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// SCRIBE misc + SSE (map-backend-api.md:99-104, :243; terminator
// {done,id,...} per client.ts:686-690)
// ═══════════════════════════════════════════════════════════════════════════
const SCRIBE_CONFIG = {
  asr: { model: 'whisper-small', provider: 'local', batch_size: 8 },
  correction: { enabled: true, model: 'orb-correct-v1', provider: 'local' },
  draft: { enabled: true, model: 'orb-draft-v1', provider: 'local' },
  verify: { enabled: true, model: 'orb-verify-v1', provider: 'local' },
};

const SCRIBE_DAEMON_STATUS = {
  status: 'running', daemon_enabled: true, pid: 4242,
  uptime_s: 5400, last_heartbeat: t('09:14'),
};

const SCRIBE_STRUCTURE_SSE = sse([
  { content: 'S: Day 2 CAP. Persistent fever, mildly worse work of breathing.\n' },
  { content: 'O: T 38.6°C, RR 26, SpO2 92% RA, HR 118. Right basal crackles.\n' },
  { content: 'A: CAP with early sepsis — not yet improving on current antibiotics.\n' },
  { content: 'P: Continue IV antibiotics, repeat CBC/CRP in AM, escalate if NEWS2 ≥ 7.' },
  { done: true, id: 501, format: 'soap', status: 'draft',
    pipeline: { asr_model: 'whisper-small', draft_model: 'orb-draft-v1', duration_ms: 5120 },
    verification: { status: 'not_requested' } },
]);

// Lens SSE — LensPanel appends raw event text directly (client.ts:933-938)
const LENS_ANALYZE_SSE = sse([
  'Impression: right lower zone consolidation consistent with pneumonia. ',
  'No pleural effusion or pneumothorax. Cardiac silhouette within normal limits. ',
  'Recommend clinical correlation and repeat film in 48 hours if not improving.',
  '[DONE]',
]);

const VIGIL_ANALYSIS_SSE = sse([
  { content: 'Clinical narrative: 55-year-old male, day 2 of community-acquired pneumonia with early sepsis. ' },
  { content: 'The overnight trend shows rising respiratory rate and drifting oxygen saturation, ' },
  { content: 'consistent with an incomplete response to first-line antibiotics. ' },
  { content: 'Priorities: close the overdue sepsis-bundle antibiotics element, repeat inflammatory markers this morning, and set an explicit escalation threshold at NEWS2 7.' },
  '[DONE]',
]);

// ═══════════════════════════════════════════════════════════════════════════
// DISCHARGE ACCESS (public) — map-backend-api.md:254-255
// ═══════════════════════════════════════════════════════════════════════════
const DISCHARGE_ACCESS_SUMMARY = {
  status: 'success',
  summary:
    'Suresh Reddy (MRN-GM004) was admitted on 2026-05-21 with community-acquired pneumonia and early sepsis. He responded to intravenous antibiotics, remained stable, and was discharged on oral antibiotics with OPD review in one week.',
  timeline: DISCHARGE_TIMELINE,
  patient: {
    mrn: 'MRN-GM004', name: 'Suresh Reddy', age: 55, sex: 'M', ward: 'W1',
    bed: 'GM-104', diagnosis: 'CAP with early sepsis',
    admission_date: onDay('21', '08:00'), attending_doctor_name: 'Dr. Meera Sharma',
  },
  medications: [
    { drug_name: 'Amoxicillin-Clavulanate', dosage: '625mg', route: 'PO',
      frequency: 'TDS', status: 'active', started_at: t('09:30') },
    { drug_name: 'Paracetamol', dosage: '1g', route: 'PO',
      frequency: 'PRN', status: 'active', started_at: onDay('21', '09:05') },
  ],
};

// ═══════════════════════════════════════════════════════════════════════════
// TRUNK WAVE-1/2 SURFACES — MFA enrollment, command-palette patient search,
// real patient appointments, unified Patient Story, eMAR administrations and
// the drug-safety 428 gate. Ported from the trunk harness so trunk-only
// features stay covered. Shapes match src/api/client.ts + PatientShell /
// CommandPalette / MfaEnrollmentModal / PatientStory / VigilPanel usage.
// ═══════════════════════════════════════════════════════════════════════════

// POST /api/auth/mfa/enroll/begin (MfaEnrollmentModal.tsx).
const MFA_ENROLL_BEGIN = {
  secret: 'JBSWY3DPEHPK3PXP',
  otpauth_uri: 'otpauth://totp/Orb%20Hospital%20OS:DOC001?secret=JBSWY3DPEHPK3PXP&issuer=Orb%20Hospital%20OS',
};

// GET /api/search/patients?q= — CommandPalette expects a BARE array.
const SEARCH_PATIENTS = VIGIL_PATIENTS.slice(0, 4).map((p) => ({
  id: p.id, name: p.name, mrn: p.mrn, bed: p.bed, ward: p.ward,
  risk_level: p.risk_level, diagnosis: p.diagnosis,
}));

// PatientShell: GET /appointments/mine → { appointments: [...] };
// POST /appointments returns the booked row itself (confirm screen reads it).
const APPOINTMENTS_MINE = [
  { id: 'apt-01', patient_id: 'P001', scheduled_date: '2026-05-28', scheduled_time: '10:30',
    appointment_type: 'Follow-up', reason: 'Post-discharge review', status: 'booked', source: 'booked' },
  { id: 'apt-02', patient_id: 'P001', scheduled_date: '2026-06-04', scheduled_time: '09:00',
    appointment_type: 'Lab draw', reason: 'Repeat CRP', status: 'booked', source: 'booked' },
];
const APPOINTMENT_BOOKED = {
  id: 'apt-90', patient_id: 'P001', scheduled_date: '2026-05-29', scheduled_time: '11:00',
  appointment_type: 'Follow-up', reason: 'Post-discharge review', status: 'booked', source: 'booked',
};

// GET /api/vigil/patients/:id/story — one event per type so every PatientStory
// filter chip renders (admission/medication/imaging/note/scribe/order/alert/
// administration), newest first.
const PATIENT_STORY_EVENTS = [
  { timestamp: t('09:02'), type: 'administration', title: 'Administered',
    detail: 'Piperacillin-Tazobactam 4.5g IV given', actor: 'NUR001' },
  { timestamp: t('09:00'), type: 'alert', title: 'Critical alert (NEWS2 band: critical)',
    detail: 'RR 26, Temp 38.6°C, SpO2 92% on air', actor: null },
  { timestamp: t('08:15'), type: 'order', title: 'Order — lab (active)',
    detail: 'Lactate, plasma — STAT', actor: 'DOC001' },
  { timestamp: t('07:10'), type: 'scribe', title: 'Scribe SOAP',
    detail: 'S: Feels feverish, breathless. O: RR 26, Temp 38.6, SpO2 92%. A: CAP with early sepsis. P: Continue Pip-Taz, serial lactate.', actor: 'DOC001' },
  { timestamp: t('06:30'), type: 'note', title: 'PROGRESS note',
    detail: 'Overnight fever spikes with rising respiratory rate. Escalated onto the sepsis pathway.', actor: 'DOC001' },
  { timestamp: t('06:05'), type: 'imaging', title: 'Imaging — xray',
    detail: 'Right lower-zone consolidation consistent with pneumonia.', actor: null },
  { timestamp: onDay('21', '09:00'), type: 'medication', title: 'Medication started',
    detail: 'Piperacillin-Tazobactam 4.5g IV Q6H', actor: 'DOC001' },
  { timestamp: onDay('21', '08:00'), type: 'admission', title: 'Admitted',
    detail: 'W1 · Bed GM-104 · CAP with early sepsis', actor: null },
];

// GET /api/emar/patients/:id/administrations — medication_id MUST match
// DETAIL_MEDICATIONS ids (11/12/13) so the "Given HH:MM" chips land on real
// med rows in the Medications modal.
const EMAR_ADMINISTRATIONS = [
  { id: 9101, medication_id: 11, patient_id: 'PAT001', drug_name: 'Piperacillin-Tazobactam',
    status: 'given', dose_given: '4.5g', route_given: 'IV', not_given_reason: null,
    administered_by: 'NUR001', administered_by_name: 'Priya Reddy',
    administered_at: t('08:05'), confirmation_method: 'typed', notes: null },
  { id: 9102, medication_id: 12, patient_id: 'PAT001', drug_name: 'Azithromycin',
    status: 'not_given', dose_given: null, route_given: null,
    not_given_reason: 'Held — clinical decision (QTc review)',
    administered_by: 'NUR001', administered_by_name: 'Priya Reddy',
    administered_at: t('06:30'), confirmation_method: 'typed', notes: null },
];

// Drug-safety gate: the med-add POST answers 428 so the blocking
// SafetyAlertModal renders (VigilPanel.tsx / OrdersPanel.tsx open it on
// exactly this envelope).
const MED_SAFETY_428 = {
  status: 428,
  body: { detail: {
    error: 'drug_safety_acknowledgement_required',
    message: 'Severe drug-safety alerts must be acknowledged with a reason before this order proceeds.',
    safety: {
      drug: 'Amoxicillin',
      allergy_alerts: [
        { type: 'allergy', severity: 'severe', substance: 'Penicillin', reaction: 'anaphylaxis',
          message: 'Patient is allergic to Penicillin (documented reaction: anaphylaxis).', match: 'class' },
      ],
      interaction_alerts: [
        { type: 'interaction', severity: 'moderate', with: 'Methotrexate',
          message: 'Amoxicillin + Methotrexate: reduced renal clearance of methotrexate — toxicity risk.' },
      ],
      max_severity: 'severe', blocking: true, safe: false,
      interaction_screened: true, coverage_note: null,
    },
  } },
};

// ═══════════════════════════════════════════════════════════════════════════
// ROUTER
// ═══════════════════════════════════════════════════════════════════════════

// GET resolution. Trailing slashes are normalized away before this is called,
// so '/api/admin/impact/' and '/api/admin/impact' (also compliance,
// command-center, capacity-sim, orders) resolve identically.
function getFixture(path, params) {
  switch (path) {
    // misc / auth
    case '/api/health':
      return ok({ status: 'ok', service: 'orb-hospital-os', version: '0.1.0' });
    // Local-inference warmth, polled by the header AiStatusChip. Both tiers
    // reported warm so the audit captures the chip's normal (green) state.
    case '/api/health/models':
      return ok({
        ollama_reachable: true,
        models: [
          { model: 'medgemma:27b', role: 'primary', warm: true, detail: { size_vram: 17540778556 } },
          { model: 'medgemma:4b', role: 'ambient', warm: true, detail: { size_vram: 3031370300 } },
        ],
      });
    case '/api/auth/me': return ok(AUTH_USER);

    // vigil collections
    case '/api/vigil/patients': return ok(VIGIL_PATIENTS);
    case '/api/vigil/discharged': return ok(DISCHARGED);

    // nurse — My Shift. Scenarios swap in NURSE_BOARD_QUIET / _EMPTY, or a
    // 500, via the driver's routeOverride; this is the busy-shift default.
    case '/api/nurse/board': return ok(NURSE_BOARD);
    case '/api/admin/shadow/outcome/shift-status': return ok(NURSE_SHIFT_STATUS);

    // sage
    case '/api/sage/insights': return ok(SAGE_INSIGHTS);
    case '/api/sage/history': return ok(SAGE_HISTORY);
    case '/api/sage/ward-brief': return ok(SAGE_WARD_BRIEF);
    case '/api/sage/ai-posture': return ok(SAGE_AI_POSTURE);

    // relay collections
    case '/api/relay/channels': return ok(RELAY_CHANNELS);
    case '/api/relay/alerts': return ok(RELAY_ALERTS);

    // scribe
    case '/api/scribe/notes': return ok([SCRIBE_NOTE_DRAFT]);
    case '/api/scribe/config': return ok(SCRIBE_CONFIG);
    case '/api/scribe/daemon/status': return ok(SCRIBE_DAEMON_STATUS);

    // lens
    case '/api/lens/analyses': return ok([LENS_ANALYSIS_ROW]);

    // forecast
    case '/api/forecast/hospital': return ok(FORECAST_HOSPITAL);
    case '/api/forecast/capacity': return ok(FORECAST_CAPACITY);
    case '/api/forecast/admission-patterns': return ok(FORECAST_ADMISSION_PATTERNS);
    case '/api/forecast/discharge-board': return ok(FORECAST_DISCHARGE_BOARD);
    case '/api/forecast/appointments': return ok(FORECAST_APPOINTMENTS);

    // surgery fixed paths (must resolve before /api/surgery/{id})
    case '/api/surgery/schedule': return ok(SURGERY_SCHEDULE);
    case '/api/surgery/cases': return ok(SURGERY_CASES);
    case '/api/surgery/emergency-queue': return ok(SURGERY_EMERGENCY_QUEUE);
    case '/api/surgery/checklist/template': return ok(SURGERY_CHECKLIST_TEMPLATE);

    // helix
    case '/api/helix/catalog': return ok(HELIX_CATALOG);
    case '/api/helix/inventory': return ok(HELIX_INVENTORY);
    case '/api/helix/orders': return ok(HELIX_ORDERS);
    case '/api/helix/orders/pending': return ok(HELIX_ORDERS_PENDING);
    case '/api/helix/stock-movements': return ok(HELIX_STOCK_MOVEMENTS);

    // pharmacy
    case '/api/pharmacy/pending': return ok(PHARMACY_PENDING);

    // audit / ingest
    case '/api/audit/logs': return ok(auditLogsResponse(params));
    case '/api/ingest/status': return ok(ingestStatusResponse(params));

    // admin (impact/compliance are trailing-slash-only on the real server;
    // both forms are accepted here via normalization)
    case '/api/admin/overview': return ok(ADMIN_OVERVIEW);
    case '/api/admin/users': return ok(ADMIN_USERS);
    case '/api/admin/backups': return ok(ADMIN_BACKUPS);
    case '/api/admin/models': return ok(ADMIN_MODELS);
    case '/api/admin/models/provenance': return ok(ADMIN_MODELS_PROVENANCE);
    case '/api/admin/impact': return ok(ADMIN_IMPACT);
    case '/api/admin/impact/assumptions': return ok({ assumptions: ADMIN_IMPACT.assumptions });
    case '/api/admin/compliance': return ok(ADMIN_COMPLIANCE);

    // flight recorder
    case '/api/flight-recorder/timeline': return ok(flightTimelineResponse(params));
    case '/api/flight-recorder/verify': return ok({ ok: true, broken_at: null, count: 20 });

    // command center (with/without trailing slash via normalization)
    case '/api/command-center': return ok(COMMAND_CENTER);

    // bridge (patient role)
    case '/api/bridge/status': return ok(BRIDGE_STATUS);
    case '/api/bridge/vitals': return ok({ vitals: BRIDGE_STATUS.vitals });
    case '/api/bridge/reports': return ok(BRIDGE_REPORTS);
    case '/api/bridge/medications': return ok({ medications: BRIDGE_REPORTS.medications });
    case '/api/bridge/documents': return ok({ documents: BRIDGE_REPORTS.clinical_documents });
    case '/api/bridge/discharge':
      return ok({ discharge_summary: null, message: 'No discharge summary available yet.' });

    // pulse
    case '/api/pulse/dashboard': return ok(PULSE_DASHBOARD);
    case '/api/pulse/weather': return ok(PULSE_DASHBOARD.weather);
    case '/api/pulse/aqi': return ok(PULSE_DASHBOARD.air_quality);
    case '/api/pulse/flu': return ok(PULSE_DASHBOARD.flu_surveillance);
    case '/api/pulse/drug-recalls': return ok(PULSE_DASHBOARD.drug_recalls);
    case '/api/pulse/audit-log': return ok([]);

    // orders terminology (query-dependent)
    case '/api/orders/terminology/validate': {
      const code = params.get('code') || '';
      const codeSystem = params.get('code_system') || 'LOINC';
      return code === '718-7'
        ? ok({ valid: true, code_system: codeSystem, code, display: 'Hemoglobin' })
        : ok({ valid: false, code_system: codeSystem, code, display: null });
    }

    // bundles
    case '/api/bundles/active': return ok({ bundles: [sepsisBundle('PAT001')] });

    // trunk wave-1/2 surfaces
    case '/api/appointments/mine': return ok({ appointments: APPOINTMENTS_MINE });
    case '/api/appointments': return ok({ appointments: APPOINTMENTS_MINE });
    case '/api/search/patients': return ok(SEARCH_PATIENTS);
  }

  let m;

  // ── vigil parameterized ──
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/vitals$/))) return ok(VITALS_24);
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/notes$/)))
    return ok(vigilPatientDetail(m[1]).notes);
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/analysis$/))) return VIGIL_ANALYSIS_SSE;
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/medication-reconciliation$/)))
    return ok(MED_RECONCILIATION);
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/discharge-summary$/)))
    return ok(dischargeSummaryFor(m[1]));
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/qr$/)))
    return { status: 404, body: { detail: 'no qr in mock' } };
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/story$/)))
    return ok({ patient_id: m[1], events: PATIENT_STORY_EVENTS });
  if ((m = path.match(/^\/api\/emar\/patients\/([^/]+)\/administrations$/)))
    return ok({ administrations: EMAR_ADMINISTRATIONS });
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)$/)))
    return ok(vigilPatientDetail(m[1]));

  // ── relay parameterized ──
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/messages$/))) return ok(RELAY_MESSAGES);
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/sage_memory$/))) return ok(SAGE_MEMORY);
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/sage_actions\/([^/]+)\/pre_flight$/)))
    return ok(SAGE_PRE_FLIGHT);
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/sage_actions$/))) return ok(SAGE_ACTIONS);

  // ── surgery parameterized (after fixed subpaths above) ──
  if ((m = path.match(/^\/api\/surgery\/([^/]+)\/checklist$/))) return ok(SURGERY_CHECKLIST_STATE);
  if ((m = path.match(/^\/api\/surgery\/([^/]+)\/risk$/))) return SURGERY_RISK_SSE;
  if ((m = path.match(/^\/api\/surgery\/([^/]+)$/))) return ok(surgeryDetail(m[1]));

  // ── helix parameterized ──
  if ((m = path.match(/^\/api\/helix\/orders\/([^/]+)\/transitions$/)))
    return ok(helixTransitions(m[1]));

  // ── orders CPOE parameterized ──
  if ((m = path.match(/^\/api\/orders\/patient\/([^/]+)$/))) return ok(cpoeOrdersFor(m[1]));
  if ((m = path.match(/^\/api\/orders\/([^/]+)\/events$/))) return ok(cpoeEvents(m[1]));
  if ((m = path.match(/^\/api\/orders\/([^/]+)$/))) return ok({ order: cpoeOrder({ id: m[1] }) });

  // ── scribe notes detail — verification_json/pipeline_meta_json PARSED here
  //    (map-backend-api.md:101,300), unlike the list/detail envelopes ──
  if ((m = path.match(/^\/api\/scribe\/notes\/([^/]+)\/verification$/)))
    return ok({
      note_id: Number(m[1]) || 501,
      verification: JSON.parse(SCRIBE_NOTE_DRAFT.verification_json),
      pipeline: JSON.parse(SCRIBE_NOTE_DRAFT.pipeline_meta_json),
    });
  if ((m = path.match(/^\/api\/scribe\/notes\/([^/]+)$/)))
    return ok(Object.assign({}, SCRIBE_NOTE_DRAFT, {
      verification_json: JSON.parse(SCRIBE_NOTE_DRAFT.verification_json),
      pipeline_meta_json: JSON.parse(SCRIBE_NOTE_DRAFT.pipeline_meta_json),
    }));

  // ── forecast / coding / bundles / ipass / downtime per-patient ──
  if ((m = path.match(/^\/api\/forecast\/patient\/([^/]+)$/))) return ok(forecastPatient(m[1]));
  if ((m = path.match(/^\/api\/coding\/patient\/([^/]+)$/))) return ok(codingFor(m[1]));
  if ((m = path.match(/^\/api\/bundles\/patient\/([^/]+)$/)))
    return ok({ bundles: [sepsisBundle(m[1])] });
  if ((m = path.match(/^\/api\/ipass\/([^/]+)\/draft$/))) return ok(ipassDraft(m[1]));
  if ((m = path.match(/^\/api\/ipass\/([^/]+)\/recent$/))) return ok(IPASS_RECENT);
  if ((m = path.match(/^\/api\/downtime\/([^/]+)\/latest$/)))
    return ok(downtimeEnvelope(m[1], 71, t('08:17')));
  if ((m = path.match(/^\/api\/downtime\/([^/]+)\/history$/)))
    return ok({ snapshots: [
      downtimeEnvelope(m[1], 71, t('08:17')),
      downtimeEnvelope(m[1], 70, onDay('22', '20:17')),
    ] });

  // ── clinical records ──
  if ((m = path.match(/^\/api\/patients\/([^/]+)\/allergies$/))) return ok(allergiesFor(m[1]));
  if ((m = path.match(/^\/api\/patients\/([^/]+)\/problems$/))) return ok(problemsFor(m[1]));
  if ((m = path.match(/^\/api\/patients\/([^/]+)\/medication-safety$/)))
    return ok(medicationSafetyFor(params));

  // ── discharge access (grant-authorized summary) ──
  if ((m = path.match(/^\/api\/discharge-access\/([^/]+)\/summary$/)))
    return ok(DISCHARGE_ACCESS_SUMMARY);

  // ── flight recorder event detail ──
  if ((m = path.match(/^\/api\/flight-recorder\/event\/([^/]+)\/([^/]+)$/)))
    return ok(flightEventDetail(m[1], m[2]));

  return null; // unknown GET → driver 404s
}

// POST / PATCH / PUT / DELETE resolution. Specific fixtures first; a generic
// {status:'success'} catch-all for any other /api mutation LAST.
function mutationFixture(method, path, params) {
  let m;

  // ── SSE flows (map-backend-api.md:294; terminators per client.ts consumers) ──
  if (path === '/api/sage/query') return SAGE_QUERY_SSE;
  if (path === '/api/sage/voice') return SAGE_VOICE_SSE;
  if (path === '/api/scribe/structure') return SCRIBE_STRUCTURE_SSE;
  if (path === '/api/lens/analyze') return LENS_ANALYZE_SSE;
  if (path === '/api/bridge/ask') return BRIDGE_ASK_SSE;
  if ((m = path.match(/^\/api\/surgery\/([^/]+)\/risk$/))) return SURGERY_RISK_SSE;
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/analysis$/))) return VIGIL_ANALYSIS_SSE;

  // ── auth ──
  if (path === '/api/auth/login') return ok({ token: 'visual-test-token', user: AUTH_USER });
  if (path === '/api/auth/logout') return ok({ status: 'logged_out' });
  if (path === '/api/auth/mfa/enroll/begin') return ok(MFA_ENROLL_BEGIN);

  // ── patient appointments (real booking) + eMAR administer ──
  if (path === '/api/appointments') return ok(APPOINTMENT_BOOKED);
  if ((m = path.match(/^\/api\/emar\/medications\/([^/]+)\/administer$/)))
    return ok({ status: 'success',
      administration: Object.assign({}, EMAR_ADMINISTRATIONS[0], { medication_id: Number(m[1]) || 11 }) });

  // ── admin data & backups ──
  if (path === '/api/admin/backups')
    return ok({ status: 'success', backup: { filename: 'orb-backup-20260523-091500.db', size_bytes: 2490368, created_at: '2026-05-23T09:15:00' } });

  // ── admissions ──
  if (path === '/api/admissions/admit')
    return ok({ success: true, patient_id: 'PAT099', admission_id: 'ADM-2026-099',
      channel_id: 'CH-PAT099', mrn: 'MRN-GM099', patient_login_id: 'P099',
      temporary_password: 'welcome-1234' });

  // ── vigil writes ──
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/notes$/)))
    return ok({ status: 'success', id: 9001 });
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/medications$/)))
    return MED_SAFETY_428; // drug-safety gate → blocking SafetyAlertModal
  if ((m = path.match(/^\/api\/vigil\/patients\/([^/]+)\/discharge$/)))
    return ok({ status: 'success',
      summary_url: '/discharge/mock-discharge-token',
      qr_path: `/api/vigil/patients/${m[1]}/qr` });
  if (path === '/api/vigil/patients')
    return ok({ id: 'PAT099', user_id: 'USR-PAT099', channel_id: 'CH-PAT099',
      temporary_password: 'welcome-1234', message: 'Patient created' });

  // ── relay writes ──
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/messages$/)))
    return ok({ id: 9099, channel_id: m[1], sender_id: 'DOC001',
      sender_name: 'Dr. Meera Sharma', sender_role: 'doctor',
      content: 'Noted — will review shortly.', message_type: 'text',
      timestamp: t('09:16'), is_bot: false, source: 'clinician' });
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/read$/)))
    return ok({ status: 'ok', marked_read: 3 });
  if ((m = path.match(/^\/api\/relay\/channels\/([^/]+)\/sage_actions\/([^/]+)\/transition$/)))
    return ok({ status: 'ok',
      action: Object.assign({}, SAGE_ACTIONS[0], { id: m[2], state: 'confirmed', version: 2 }),
      handoff: null });
  if ((m = path.match(/^\/api\/relay\/actions\/([^/]+)\/execute$/)))
    return ok({ status: 'executed', action: 'add_medication', patient_id: 'PAT002' });
  if ((m = path.match(/^\/api\/relay\/alerts\/([^/]+)\/resolve$/)))
    return ok({ status: 'resolved' });

  // ── orders CPOE writes (path already slash-normalized) ──
  if (path === '/api/orders')
    return ok({ status: 'success', order: cpoeOrder({ id: 'ORD-2199', status: 'draft' }) });
  if ((m = path.match(/^\/api\/orders\/([^/]+)\/transition$/)))
    return ok({ status: 'success', order: cpoeOrder({ id: m[1], status: 'active' }) });
  if ((m = path.match(/^\/api\/orders\/([^/]+)\/result$/)))
    return ok({ status: 'success',
      order: cpoeOrder({ id: m[1], status: 'resulted',
        result_text: 'Hb 11.8 g/dL', result_at: t('09:20') }) });

  // ── helix writes ──
  if (path === '/api/helix/orders/approve')
    return ok({ status: 'success', order: helixOrder({ id: 'HO-099', status: 'pending' }) });
  if ((m = path.match(/^\/api\/helix\/orders\/([^/]+)\/transition$/)))
    return ok({ ok: true,
      order: helixOrder({ id: m[1], status: 'reviewing', reviewed_by: 'PHA001',
        reviewed_at: t('09:20') }),
      transition: { id: 8199, order_id: m[1], from_status: 'pending',
        to_status: 'reviewing', idempotency_key: `${m[1]}-mock`, actor_id: 'PHA001',
        payload: null, created_at: t('09:20'), actor_name: 'Pharmacist Rina D\'Souza' },
      already_applied: false });

  // ── pharmacy verify/reject ──
  if ((m = path.match(/^\/api\/pharmacy\/([^/]+)\/verify$/)))
    return ok(pharmacyActionResult('verified'));
  if ((m = path.match(/^\/api\/pharmacy\/([^/]+)\/reject$/)))
    return ok(pharmacyActionResult('rejected'));

  // ── scribe lifecycle ──
  if ((m = path.match(/^\/api\/scribe\/notes\/([^/]+)\/sign$/)))
    return ok({ ok: true, note_id: Number(m[1]) || 501, status: 'signed',
      superseded_note_id: null });
  if ((m = path.match(/^\/api\/scribe\/notes\/([^/]+)\/reject$/)))
    return ok({ ok: true, note_id: Number(m[1]) || 501, status: 'rejected' });
  if (path === '/api/scribe/transcribe')
    return ok({ transcript: 'Fifty five year old gentleman, day two of pneumonia, still febrile.',
      audio_path: 'uploads/mock_recording.webm' });

  // ── bundles (return the decorated bundle, per bundle_routes.py:27-46) ──
  if ((m = path.match(/^\/api\/bundles\/patient\/([^/]+)\/start$/)))
    return ok(sepsisBundle(m[1]));
  if ((m = path.match(/^\/api\/bundles\/([^/]+)\/complete\/([^/]+)$/)))
    return ok(sepsisBundle('PAT001'));
  if ((m = path.match(/^\/api\/bundles\/([^/]+)\/cancel$/)))
    return ok(Object.assign({}, sepsisBundle('PAT001'), { status: 'cancelled' }));

  // ── surgery writes ──
  if ((m = path.match(/^\/api\/surgery\/([^/]+)\/checklist$/)))
    return ok(SURGERY_CHECKLIST_STATE);

  // ── capacity sim (both slash forms via normalization) ──
  if (path === '/api/capacity-sim') return ok(CAPACITY_SIM);

  // ── downtime capture ──
  if ((m = path.match(/^\/api\/downtime\/capture\/([^/]+)$/)))
    return ok(downtimeEnvelope(m[1], 72, t('09:17')));
  if (path === '/api/downtime/capture-all')
    return ok({ count: 22, total_admitted: 22, failed: [], captured_at: t('09:17') });

  // ── discharge access verify (public) ──
  if (path === '/api/discharge-access/verify')
    return ok({ status: 'success', access_grant: 'mock-grant', patient_id: 'PAT001' });

  // ── lens assign ──
  if ((m = path.match(/^\/api\/lens\/analyses\/([^/]+)\/assign$/)))
    return ok({ status: 'assigned', patient_name: 'Suresh Reddy' });

  // Catch-all: any other mutation under /api succeeds generically.
  if (path.startsWith('/api/')) return { status: 200, body: { status: 'success' } };
  return null;
}

/**
 * fixtureFor(method, pathname, searchParams) — see module header.
 * Trailing slashes are stripped so '/api/admin/impact/' ≡ '/api/admin/impact',
 * '/api/command-center/' ≡ '/api/command-center', '/api/capacity-sim/' ≡
 * '/api/capacity-sim', '/api/orders/' ≡ '/api/orders', etc.
 */
function fixtureFor(method, pathname, searchParams) {
  const params = searchParams || new URLSearchParams();
  const path = pathname.length > 1 && pathname.endsWith('/')
    ? pathname.slice(0, -1)
    : pathname;

  if (method === 'GET') return getFixture(path, params);
  return mutationFixture(method, path, params);
}

/* The nurse boards are exported by name as well as served by fixtureFor,
   because the state-specific My Shift scenarios swap them in through the
   driver's routeOverride — the calm empty state and the "no patients in scope"
   state are the two most regression-prone things on that screen and cannot be
   reached from the default board. */
module.exports = { fixtureFor, NURSE_BOARD, NURSE_BOARD_QUIET, NURSE_BOARD_EMPTY };
