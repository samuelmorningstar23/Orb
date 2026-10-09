"""Orb Hospital OS — SQLite Database Layer"""
import sqlite3
from db_backend import DBOperationalError, DBProgrammingError
import json
import logging
import threading
from pathlib import Path
from typing import Optional
from config import DB_PATH
from roles import role_check_sql

logger = logging.getLogger("orb.database")

# Bump this whenever the additive-migration sequence in init_db() changes meaningfully.
# Recorded in the schema_version table so we have an auditable record of which schema a
# given orb.db is at (rather than re-running every migration block blindly each boot).
# v5: medication_administrations (eMAR) table; universal '1234' PIN seeding removed.
# v6: users.role CHECK admits 'cashier' (existing DBs rebuilt by migration 33;
#     this SCHEMA renders the same list from roles.STORABLE_ROLES for fresh DBs).
SCHEMA_VERSION = 6

# Users rows that are automation identities, not people: 'system' (Sage's audit
# identity, seeded below) and the bots that post to Relay. Their password_hash
# is a sentinel no login can match, so they are safe exactly as long as nobody
# gives them a real one: one admin password reset on bot_vigil (role 'doctor',
# no department) would mint a hospital-wide doctor login. User Management hides
# them and refuses every change to them; anything seeding a new bot adds it here.
# 'system_lab' (lis-22) is the laboratory interface's identity: an external
# laboratory's critical result is posted to the case room under it. It is
# created inactive (is_active 0) by services/lab_critical_service.py and never
# signs in.
SERVICE_ACCOUNT_IDS: frozenset[str] = frozenset({
    "system", "bot_vigil", "bot_lab", "bot_lens", "bot_scribe", "bot_bridge", "system_lab",
})


_SCHEMA_TEMPLATE = """
-- Users (clinicians + patients)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (__USERS_ROLE_CHECK__)),
    department TEXT,
    registration_number TEXT,
    password_hash TEXT NOT NULL,
    pin_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Patients
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    name TEXT NOT NULL,
    mrn TEXT UNIQUE NOT NULL,
    bed TEXT,
    ward TEXT,
    admission_date TIMESTAMP,
    diagnosis TEXT,
    status TEXT DEFAULT 'admitted' CHECK (status IN ('admitted', 'discharged', 'transferred', 'registered')),
    age INTEGER,
    date_of_birth DATE,
    sex TEXT,
    phone TEXT,
    emergency_contact_name TEXT,
    emergency_contact_phone TEXT,
    address TEXT,
    attending_doctor_id TEXT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Admissions
CREATE TABLE IF NOT EXISTS admissions (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    admitted_by TEXT NOT NULL REFERENCES users(id),
    admission_source TEXT,
    admission_type TEXT,
    department TEXT,
    ward TEXT,
    bed TEXT,
    attending_doctor_id TEXT REFERENCES users(id),
    referring_clinician TEXT,
    chief_complaint TEXT,
    initial_diagnosis TEXT,
    triage_severity INTEGER,
    admitting_notes TEXT,
    nursing_notes TEXT,
    isolation_precautions TEXT,
    code_status TEXT DEFAULT 'full_code',
    initial_vitals_json TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discharged', 'transferred')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Patient Profiles (Longitudinal Data)
CREATE TABLE IF NOT EXISTS patient_profiles (
    patient_id TEXT PRIMARY KEY REFERENCES patients(id),
    comorbidities TEXT,
    past_medical_history TEXT,
    past_surgical_history TEXT,
    allergies TEXT,
    current_medications TEXT,
    family_history TEXT,
    social_history TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Vitals (time-series)
CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    timestamp TIMESTAMP NOT NULL,
    heart_rate REAL,
    systolic_bp REAL,
    diastolic_bp REAL,
    spo2 REAL,
    temperature REAL,
    respiratory_rate REAL,
    consciousness TEXT DEFAULT 'alert'
);

-- Risk scores
CREATE TABLE IF NOT EXISTS risk_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score REAL NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('critical', 'elevated', 'stable')),
    drivers TEXT  -- JSON array of top risk drivers
);

-- Channels (Relay)
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('case_room', 'department', 'direct')),
    patient_id TEXT REFERENCES patients(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Messages (Relay)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    sender_id TEXT NOT NULL REFERENCES users(id),
    sender_name TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'alert', 'system', 'ambient_action', 'surgical_alert')),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    read_by TEXT DEFAULT '[]'  -- JSON array of user IDs
);

-- Sage conversations
CREATE TABLE IF NOT EXISTS sage_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    sources TEXT,  -- JSON array of source references
    -- Which Sage thread this turn belongs to. "New Chat" mints a new one, and
    -- prompt history is scoped to it — see migration 16. NULL = pre-threads.
    conversation_id TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Clinical documents (for RAG)
CREATE TABLE IF NOT EXISTS clinical_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    author_id TEXT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Scribe notes
CREATE TABLE IF NOT EXISTS scribe_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    raw_transcript TEXT NOT NULL,
    structured_note TEXT,  -- JSON structured output
    note_format TEXT CHECK (note_format IN ('soap', 'discharge', 'handoff', 'raw')),
    audio_path TEXT,
    verification_json TEXT,      -- JSON verification report (Stage 4)
    pipeline_meta_json TEXT,     -- JSON pipeline timing + model info
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Discharge summaries
CREATE TABLE IF NOT EXISTS discharge_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL,
    timeline_json TEXT,  -- JSON array of chronological events
    qr_code_path TEXT,
    discharged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Discharge access tokens
CREATE TABLE IF NOT EXISTS discharge_access_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    expires_at TIMESTAMP NOT NULL,
    failed_attempts INTEGER DEFAULT 0,
    locked_until TIMESTAMP NULL,
    verified_at TIMESTAMP NULL,
    last_accessed_at TIMESTAMP NULL,
    revoked_at TIMESTAMP NULL,
    created_by_user_id TEXT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lens analyses (medical image interpretation)
CREATE TABLE IF NOT EXISTS lens_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),
    author_id TEXT NOT NULL REFERENCES users(id),
    image_path TEXT NOT NULL,
    image_type TEXT,  -- 'xray', 'ecg', 'ct', 'mri', 'photo', 'other'
    clinical_context TEXT,
    analysis TEXT NOT NULL,
    findings_json TEXT,  -- JSON structured findings
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Medications / Prescriptions
CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    drug_name TEXT NOT NULL,
    dosage TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT 'Oral',
    frequency TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'discontinued', 'prn')),
    prescribed_by TEXT REFERENCES users(id),
    notes TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Medication administrations (eMAR). One row per bedside administration
-- attempt of a charted medication — closes the loop CPOE → pharmacist
-- verification → administration. 'given' rows record the five rights as
-- confirmed at the bedside (dose/route actually given + how identity was
-- confirmed); 'not_given' rows must carry a reason (refused / held / NPO /
-- unavailable...). Append-only by design: corrections are new rows, never
-- UPDATEs, so the administration record stays a faithful history.
CREATE TABLE IF NOT EXISTS medication_administrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id INTEGER NOT NULL REFERENCES medications(id),
    patient_id TEXT NOT NULL REFERENCES patients(id),
    administered_by TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('given', 'not_given')),
    not_given_reason TEXT,
    dose_given TEXT,
    route_given TEXT,
    scheduled_for TIMESTAMP,
    administered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    confirmation_method TEXT DEFAULT 'typed' CHECK (confirmation_method IN ('typed', 'barcode')),
    idempotency_key TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_med_admin_patient ON medication_administrations(patient_id, administered_at DESC);
CREATE INDEX IF NOT EXISTS idx_med_admin_medication ON medication_administrations(medication_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_med_admin_idem
    ON medication_administrations(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- Forecast predictions (cached per-patient LSTM output)
CREATE TABLE IF NOT EXISTS forecast_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    predicted_los_days REAL NOT NULL,
    discharge_probability REAL NOT NULL,
    acuity_trajectory TEXT NOT NULL CHECK (acuity_trajectory IN ('improving', 'stable', 'worsening')),
    confidence REAL NOT NULL,
    feature_snapshot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Hospital-level daily forecasts
CREATE TABLE IF NOT EXISTS hospital_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_date DATE NOT NULL,
    hour INTEGER DEFAULT 0,
    predicted_admissions REAL,
    predicted_discharges REAL,
    predicted_occupancy REAL,
    ward_breakdown TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Admission history (last 30 days for Poisson model fitting)
CREATE TABLE IF NOT EXISTS admission_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admission_date DATE NOT NULL,
    patient_name TEXT NOT NULL,
    age INTEGER,
    sex TEXT,
    ward TEXT,
    diagnosis TEXT,
    severity TEXT CHECK (severity IN ('critical', 'elevated', 'stable')),
    length_of_stay_days REAL,
    discharged INTEGER DEFAULT 0,
    discharge_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Surgeries
CREATE TABLE IF NOT EXISTS surgeries (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    surgeon_id TEXT NOT NULL REFERENCES users(id),
    procedure_name TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    scheduled_date DATE NOT NULL,
    scheduled_time TEXT NOT NULL,
    estimated_duration_min INTEGER,
    operating_room TEXT,
    status TEXT DEFAULT 'scheduled' CHECK (status IN ('scheduled','in_progress','completed','cancelled','emergency_pending','emergency_context_generating','emergency_ready','emergency_acknowledged','emergency_staged')),
    co_morbidities TEXT,
    surgical_checklist TEXT,
    instruments_required TEXT,
    staff_required TEXT,
    risk_assessment TEXT,
    possible_complications TEXT,
    success_probability REAL,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Surgery Team Assignments
CREATE TABLE IF NOT EXISTS surgery_team_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surgery_id TEXT NOT NULL REFERENCES surgeries(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role_name TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'alerted', 'confirmed', 'declined')),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Appointments (real bookings — patient portal "Schedule Follow-Up" + clinician views)
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    booked_by_user_id TEXT REFERENCES users(id),
    scheduled_date TEXT NOT NULL,          -- YYYY-MM-DD
    scheduled_time TEXT NOT NULL,          -- HH:MM (24h)
    appointment_type TEXT NOT NULL DEFAULT 'Follow-up',
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled', 'completed')),
    idempotency_key TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Surgery Escalation (Prep Requests)
CREATE TABLE IF NOT EXISTS surgery_prep_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    channel_id TEXT REFERENCES channels(id),
    requested_by TEXT NOT NULL,
    requested_by_name TEXT NOT NULL,
    procedure_name TEXT,
    indication TEXT,
    urgency TEXT DEFAULT 'emergency',
    clinical_context TEXT,
    state TEXT DEFAULT 'drafted' CHECK (state IN ('drafted', 'resolved')),
    idempotency_key TEXT UNIQUE,
    surgery_id TEXT REFERENCES surgeries(id),
    resolved_by TEXT,
    resolved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Ingestion events (raw provenance / dead-letter table)
CREATE TABLE IF NOT EXISTS ingestion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source_type TEXT NOT NULL,                       -- hl7, json, csv_replay, simulated
    source_name TEXT,                                -- system / sender / replay file
    external_message_id TEXT,                        -- sender-provided dedup key
    patient_identifier_raw TEXT,                     -- raw patient ID from payload
    encounter_identifier_raw TEXT,
    raw_payload TEXT,                                -- full original payload
    payload_format TEXT,                             -- hl7_oru, json_webhook, csv
    parse_status TEXT DEFAULT 'received'
        CHECK (parse_status IN ('received','parsed','partially_parsed','failed','duplicate')),
    parse_error TEXT,
    fingerprint TEXT,                                -- SHA-256 for dedup
    correlation_id TEXT,
    processed_at TIMESTAMP,
    adapter_name TEXT,
    vitals_inserted INTEGER DEFAULT 0,
    observations_inserted INTEGER DEFAULT 0
);

-- Observations (structured lab / OBX / non-vital results)
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT REFERENCES patients(id),          -- nullable until resolved
    encounter_id TEXT,                                -- nullable until resolved
    source TEXT NOT NULL DEFAULT 'unknown',           -- ingested_hl7, ingested_json, csv_replay, simulated
    source_event_id INTEGER REFERENCES ingestion_events(id),
    observation_code TEXT,                            -- LOINC or local code
    observation_code_system TEXT,                     -- LOINC, LOCAL, etc.
    observation_name TEXT NOT NULL,
    value_text TEXT,
    value_numeric REAL,
    unit TEXT,
    reference_range TEXT,
    abnormal_flag TEXT,
    status TEXT DEFAULT 'final',
    observed_at TIMESTAMP,
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    panel_name TEXT,
    obr_group_id TEXT,
    raw_segment_ref TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit logs (healthcare-grade immutable logging)
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    correlation_id TEXT,
    event_category TEXT NOT NULL,
    actor_user_id TEXT NULL,
    actor_role TEXT NULL,
    target_type TEXT NULL,
    target_id TEXT NULL,
    patient_id TEXT NULL,
    encounter_id TEXT NULL,
    source_type TEXT NULL,
    source_event_id INTEGER NULL,
    request_path TEXT NULL,
    http_method TEXT NULL,
    ip_address TEXT NULL,
    user_agent TEXT NULL,
    session_id TEXT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'partial_success', 'denied')),
    action_type TEXT NOT NULL,
    summary_message TEXT,
    metadata_json TEXT,
    -- Tamper-evidence: per-row SHA-256 hash chain. row_hash = SHA256(prev_hash || canonical(row)).
    -- Any altered or deleted row breaks the chain and is detectable on verification.
    prev_hash TEXT,
    row_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_correlation_id ON audit_logs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_audit_action_type ON audit_logs(action_type);
CREATE INDEX IF NOT EXISTS idx_audit_patient_id ON audit_logs(patient_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_category_timestamp ON audit_logs(event_category, timestamp);

-- Append-only enforcement: block UPDATE and DELETE on audit_logs at the DB layer so the
-- app DB user cannot silently mutate or remove audit records (HIPAA §164.312(b)/(c)).
-- This makes the "append-only / immutable" claim real rather than aspirational.
CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_update
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_logs_no_delete
BEFORE DELETE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE is forbidden');
END;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_vitals_patient ON vitals(patient_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_risk_patient ON risk_scores(patient_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_docs_patient ON clinical_documents(patient_id);
CREATE INDEX IF NOT EXISTS idx_scribe_patient ON scribe_notes(patient_id);
CREATE INDEX IF NOT EXISTS idx_sage_user ON sage_conversations(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_discharge_patient ON discharge_summaries(patient_id);
CREATE INDEX IF NOT EXISTS idx_lens_patient ON lens_analyses(patient_id);
CREATE INDEX IF NOT EXISTS idx_meds_patient ON medications(patient_id);
CREATE INDEX IF NOT EXISTS idx_forecast_patient ON forecast_predictions(patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hospital_forecast ON hospital_forecasts(forecast_date);
CREATE INDEX IF NOT EXISTS idx_admission_history ON admission_history(admission_date);
CREATE INDEX IF NOT EXISTS idx_surgeries_surgeon ON surgeries(surgeon_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_surgeries_patient ON surgeries(patient_id);
CREATE INDEX IF NOT EXISTS idx_surgeries_date ON surgeries(scheduled_date);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_fp ON ingestion_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_ingestion_events_status ON ingestion_events(parse_status, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_patient ON observations(patient_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_source ON observations(source_event_id);

-- Sage Ambient: Room Memory (one row per case room, bounded)
CREATE TABLE IF NOT EXISTS sage_room_memory (
    room_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    patient_summary TEXT,
    discussion_summary TEXT,
    current_concern TEXT,
    likely_next_steps TEXT,
    unresolved_questions TEXT,
    active_intents TEXT,
    risk_flags TEXT,
    participants TEXT,
    last_processed_msg_id INTEGER,
    last_processed_at TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    llm_call_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sage Ambient: Observations (append-only, clinically meaningful only)
CREATE TABLE IF NOT EXISTS sage_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    source_message_id INTEGER,
    observation_type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_obs_type ON sage_observations(observation_type, created_at DESC);

-- Sage Ambient: Durable Actions (Stage 3)
CREATE TABLE IF NOT EXISTS sage_actions (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    rationale TEXT,
    confidence REAL,
    state TEXT DEFAULT 'suggested' CHECK (state IN ('suggested', 'acknowledged', 'dismissed', 'confirmed', 'failed', 'superseded')),
    version INTEGER DEFAULT 1,
    source_msg_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_actions_room ON sage_actions(room_id, created_at DESC);

-- Sage Proactive: surveillance insights ("Sage noticed X") pushed to the attending.
CREATE TABLE IF NOT EXISTS sage_insights (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    insight_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('critical','warning','info')),
    title TEXT NOT NULL,
    detail TEXT,
    suggested_action_type TEXT,
    suggested_action_target TEXT,
    target_user_id TEXT,
    dedup_key TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','dismissed','acted','auto_resolved')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sage_insights_active ON sage_insights(state, target_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sage_insights_dedup ON sage_insights(dedup_key, state, created_at DESC);

-- Sage Ambient: Action Audit Log
CREATE TABLE IF NOT EXISTS sage_action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES sage_actions(id),
    prior_state TEXT,
    new_state TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(id),
    actor_name TEXT NOT NULL,
    actor_role TEXT,
    reason TEXT,
    source TEXT DEFAULT 'ui' CHECK (source IN ('ui', 'system')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_action_events_action ON sage_action_events(action_id, created_at DESC);

-- Sage Ambient: Downstream Handoffs (Stage 4)
CREATE TABLE IF NOT EXISTS sage_handoffs (
    id TEXT PRIMARY KEY,
    source_action_id TEXT NOT NULL REFERENCES sage_actions(id),
    room_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    target_module TEXT NOT NULL,
    handoff_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'failed', 'unsupported', 'blocked')),
    downstream_ref_id TEXT,
    error_summary TEXT,
    created_by TEXT NOT NULL REFERENCES users(id),
    last_attempted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    safety_json TEXT                         -- drug-safety screen summary shown on the card (migration 39)
);

CREATE INDEX IF NOT EXISTS idx_sage_handoffs_action ON sage_handoffs(source_action_id, created_at DESC);

-- Sage Ambient: Handoff Audit Log
CREATE TABLE IF NOT EXISTS sage_handoff_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL REFERENCES sage_handoffs(id),
    prior_status TEXT,
    new_status TEXT NOT NULL,
    actor_id TEXT,
    source TEXT DEFAULT 'system' CHECK (source IN ('system', 'user', 'module_callback')),
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sage_handoff_events_handoff ON sage_handoff_events(handoff_id, created_at DESC);

-- Persistent Log of all State Transitions (Hospital Nervous System)
CREATE TABLE IF NOT EXISTS event_log (
    event_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    source_node TEXT NOT NULL,     -- 'vigil', 'relay', 'sage', 'scribe'
    event_type TEXT NOT NULL,      -- e.g., 'patient_deteriorated', 'note_signed'
    patient_id TEXT,               -- Nullable for system-wide events
    payload TEXT NOT NULL,         -- JSON Payload
    timestamp TEXT NOT NULL,       -- ISO-8601 UTC
    correlation_id TEXT NOT NULL   -- Traces a single clinical workflow across multiple nodes
);

CREATE INDEX IF NOT EXISTS idx_event_log_topic_timestamp ON event_log(topic, timestamp);
CREATE INDEX IF NOT EXISTS idx_event_log_patient ON event_log(patient_id);
CREATE INDEX IF NOT EXISTS idx_event_log_correlation ON event_log(correlation_id);

-- Per-(event, subscriber) delivery ledger for at-least-once event delivery.
-- The event_log row is the durable WAL; THIS table tracks whether each
-- subscriber actually processed each event, so a crash / subscriber exception
-- no longer silently drops an ambient workflow — replay_undelivered() re-runs
-- any row still 'pending' on startup and on a periodic poll.
CREATE TABLE IF NOT EXISTS event_deliveries (
    event_id TEXT NOT NULL,
    subscriber TEXT NOT NULL,          -- stable subscriber id (module.qualname)
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (event_id, subscriber)
);
CREATE INDEX IF NOT EXISTS idx_event_deliveries_pending ON event_deliveries(status, attempts, created_at);

-- ── Persistent per-entity world model (temporal state) ──
-- An evolving, versioned snapshot of each patient and the ward, maintained from
-- the event stream, so Sage reasons over a MAINTAINED model instead of
-- re-deriving from raw SQL every turn (enables trajectory / cross-time
-- reasoning). `state_json` may be application-layer AES-GCM encrypted (see
-- services/field_crypto.py) — it is PHI-derived. `version` monotonically
-- increases per entity.
--
-- RETENTION: this table is write-heavy and TAIL-READ ONLY — `_load_latest`
-- (services/world_model_service.py) is the sole production SELECT and it is
-- `ORDER BY version DESC LIMIT 1`. Nothing reads version N-1. It accumulated
-- 58,188 rows / 60 MB of state_json in its first week, so `_persist` now
-- self-prunes to the newest ORB_WORLD_MODEL_KEEP_VERSIONS rows per
-- (entity_type, entity_id). Do NOT add a reader of older versions without
-- raising that bound first — an earlier comment here claimed a `revision_of`
-- column "links successive versions for audit"; no such column was ever
-- created and no such audit consumer was ever written.
CREATE TABLE IF NOT EXISTS world_model_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,         -- 'patient' | 'ward'
    entity_id TEXT NOT NULL,           -- patient_id, or ward name, or '__hospital__'
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,          -- JSON (possibly encrypted envelope)
    encrypted INTEGER NOT NULL DEFAULT 0,
    summary TEXT,                      -- short PHI-free-ish label for audit/debug
    updated_by TEXT,                   -- event topic / reason that produced this version
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_model_latest ON world_model_state(entity_type, entity_id, version DESC);

-- ── Insight feedback ledger (acceptance-rate learning) ──
-- Every time a clinician acts on / dismisses a proactive insight, we record the
-- outcome keyed by the insight PATTERN (insight_type[:severity]) so the proactive
-- engine can suppress patterns clinicians consistently reject. This is the
-- labelled dataset the app already generates being put to use.
CREATE TABLE IF NOT EXISTS insight_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,             -- e.g. 'stewardship' or 'deterioration:warning'
    insight_id TEXT,
    patient_id TEXT,
    outcome TEXT NOT NULL,             -- 'acted' | 'dismissed'
    actor_user_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_insight_feedback_pattern ON insight_feedback(pattern, outcome, created_at);

-- Schema version / migration ledger (auditable record of applied schema versions)
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);

-- Sage Preloaded Decision Cache Table
CREATE TABLE IF NOT EXISTS sage_decision_cache (
    patient_id TEXT PRIMARY KEY,
    mews_score REAL NOT NULL,
    compiled_context TEXT NOT NULL, -- Pre-assembled JSON containing Channel A, B, and C data
    preloaded_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE CASCADE
);

-- Structured allergies (the free-text patient_profiles.allergies is retained for
-- narrative; this table is the coded source of truth used for order-time allergy
-- checking in Helix). One row per (patient, substance).
CREATE TABLE IF NOT EXISTS patient_allergies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    substance TEXT NOT NULL,                 -- normalized allergen name (lowercased on write)
    substance_class TEXT,                    -- drug class if known (e.g. 'penicillins', 'nsaids')
    reaction TEXT,                           -- e.g. 'anaphylaxis', 'rash'
    severity TEXT DEFAULT 'unknown' CHECK (severity IN ('mild','moderate','severe','unknown')),
    status TEXT DEFAULT 'active' CHECK (status IN ('active','inactive','resolved')),
    source TEXT NOT NULL DEFAULT 'clinician',-- clinician | llm_extracted | ingested_fhir
    recorded_by TEXT REFERENCES users(id),
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(patient_id, substance)
);
CREATE INDEX IF NOT EXISTS idx_allergies_patient ON patient_allergies(patient_id, status);

-- Problem list (active/resolved clinical problems, coded).
CREATE TABLE IF NOT EXISTS problem_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    problem TEXT NOT NULL,
    icd10_code TEXT,
    snomed_code TEXT,
    status TEXT DEFAULT 'active' CHECK (status IN ('active','resolved','inactive')),
    onset_date DATE,
    source TEXT NOT NULL DEFAULT 'clinician',-- clinician | llm_extracted | ingested_fhir
    recorded_by TEXT REFERENCES users(id),
    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_problems_patient ON problem_list(patient_id, status);

-- Sepsis / deterioration bundle timers. A Vigil critical alert (or manual start)
-- opens a bundle; each element records completion so the dashboard can show
-- compliance against the 1-hour / 3-hour targets.
CREATE TABLE IF NOT EXISTS care_bundles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    bundle_type TEXT NOT NULL DEFAULT 'sepsis6',  -- sepsis6 | deterioration
    triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    triggered_by TEXT,                            -- user id or 'vigil_auto'
    trigger_reason TEXT,                          -- e.g. 'NEWS2 >= 7'
    -- element completion timestamps (NULL = outstanding)
    lactate_at TIMESTAMP,
    cultures_at TIMESTAMP,
    antibiotics_at TIMESTAMP,
    fluids_at TIMESTAMP,
    oxygen_at TIMESTAMP,
    urine_output_at TIMESTAMP,
    status TEXT DEFAULT 'active' CHECK (status IN ('active','completed','cancelled','expired')),
    completed_at TIMESTAMP,
    source TEXT NOT NULL DEFAULT 'clinician'
);
CREATE INDEX IF NOT EXISTS idx_bundles_patient ON care_bundles(patient_id, status);

-- Patient downtime snapshots. A durable, local, read-only copy of the essential
-- clinical picture (demographics, allergies, problems, active meds, latest vitals,
-- recent notes) so the ward keeps a safe view when the EHR/network/LLM is down —
-- a capability cloud-only systems structurally cannot offer.
CREATE TABLE IF NOT EXISTS patient_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    snapshot_json TEXT NOT NULL,          -- full serialized snapshot
    summary TEXT,                         -- short human-readable summary line
    created_by TEXT,                      -- user id or 'auto'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_snapshots_patient ON patient_snapshots(patient_id, created_at DESC);

-- Model registry / governance. Every ML/AI model surface (Vigil CfC, Forecast
-- LSTM, MedGemma prompts) can register a version so the UI can show provenance,
-- training-data lineage, validation status and calibration — the honesty layer.
CREATE TABLE IF NOT EXISTS model_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL,                  -- 'vigil_cfc' | 'forecast_lstm' | 'sage_llm' ...
    version TEXT NOT NULL,
    task TEXT,                                -- human description
    training_data TEXT,                       -- lineage, e.g. 'synthetic v1' / 'MIMIC-IV train'
    validated INTEGER DEFAULT 0,              -- 0/1: externally validated on held-out real data
    validation_metrics TEXT,                  -- JSON (AUROC, sensitivity, etc.)
    calibration_stats TEXT,                   -- JSON per-site calibration
    is_active INTEGER DEFAULT 0,
    checkpoint_present INTEGER DEFAULT 0,     -- is a trained checkpoint actually on disk?
    notes TEXT,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_key, version)
);

-- ── Orders / CPOE-lite ──────────────────────────────────────────────────────
-- Computerized provider order entry for labs, imaging, medications and other
-- orders, with a guarded status machine mirroring Helix's discipline. Codes are
-- validated against `terminology` on write.
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    order_type TEXT NOT NULL CHECK (order_type IN ('lab','imaging','medication','procedure','nursing','other')),
    code TEXT,                                -- LOINC/SNOMED/local code
    code_system TEXT,                         -- LOINC | SNOMED | ICD10 | LOCAL
    description TEXT NOT NULL,
    priority TEXT DEFAULT 'routine' CHECK (priority IN ('stat','urgent','routine')),
    status TEXT DEFAULT 'draft' CHECK (status IN ('draft','active','in_progress','completed','resulted','cancelled')),
    ordered_by TEXT REFERENCES users(id),
    source TEXT NOT NULL DEFAULT 'clinician', -- clinician | llm_extracted | agentic
    detail_json TEXT,                         -- order-type-specific payload (dose, specimen, etc.)
    result_text TEXT,
    result_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_orders_patient ON orders(patient_id, status);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(id),
    prior_status TEXT,
    new_status TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── Terminology (coding validation) ─────────────────────────────────────────
-- A local subset of standard terminologies used to VALIDATE codes on write
-- (SNOMED CT requires a licence; ICD-10 and LOINC are freely redistributable).
-- Seeded with a starter set; scripts/load_terminology.py bulk-loads the real
-- reference files. Unknown codes are flagged, not silently accepted.
CREATE TABLE IF NOT EXISTS terminology (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_system TEXT NOT NULL,                -- LOINC | ICD10 | SNOMED
    code TEXT NOT NULL,
    display TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    UNIQUE(code_system, code)
);
CREATE INDEX IF NOT EXISTS idx_terminology_lookup ON terminology(code_system, code);

-- ── Drug interactions (data-backed) ─────────────────────────────────────────
-- Replaces the hardcoded interaction list in drug_safety_service with a table
-- (class- or ingredient-pair based) that scripts/load_interactions.py can bulk
-- load from an open source (e.g. DDInter). Symmetric lookups are the caller's job.
CREATE TABLE IF NOT EXISTS drug_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_a TEXT NOT NULL,                     -- drug class or ingredient (lowercased)
    term_b TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'class' CHECK (match_type IN ('class','ingredient')),
    severity TEXT NOT NULL DEFAULT 'moderate' CHECK (severity IN ('mild','moderate','severe')),
    mechanism TEXT,
    source TEXT DEFAULT 'builtin',
    UNIQUE(term_a, term_b, match_type)
);
CREATE INDEX IF NOT EXISTS idx_interactions_a ON drug_interactions(term_a);
CREATE INDEX IF NOT EXISTS idx_interactions_b ON drug_interactions(term_b);
"""

# The one schema fragment with an external authority: the users.role list lives
# in roles.STORABLE_ROLES (see that module's contract), rendered here so a fresh
# DB and one rebuilt by migration 33 carry identical CHECK text.
SCHEMA = _SCHEMA_TEMPLATE.replace("__USERS_ROLE_CHECK__", role_check_sql())


def _open_raw_connection(pooled: bool = False):
    """Open the underlying SQLite connection.

    ``pooled=True`` hands back a close-neutered subclass (see
    _pooled_connection_class) so legacy direct .close() calls cannot destroy a
    shared pooled handle mid-frame.

    When ORB_DB_ENCRYPTION_KEY is set AND a SQLCipher driver (pysqlcipher3 /
    sqlcipher3) is importable, the DB is opened encrypted at rest and the key is
    applied via `PRAGMA key` before any other statement. Absent the key, the
    stdlib sqlite3 driver opens the file in plaintext. A key WITHOUT a driver is
    refused (RuntimeError): it used to fall back to plaintext with one warning,
    so a box configured for encryption wrote PHI to disk in the clear.

    VALIDATED 2026-07-30 on Apple Silicon / Python 3.12 with sqlcipher3-wheels:
    SQLCipher coexists with the sqlite-vec loadable extension (vec0 vector search)
    AND FTS5 AND WAL mode — the earlier concern about an extension conflict does
    not hold on this build. Encryption stays opt-in (it is a native dependency and
    needs a key), but enabling it is a supported configuration. Note the file must
    also be MIGRATED: a pre-existing plaintext orb.db cannot be opened by
    SQLCipher — run scripts/encrypt_db.py --migrate.
    """
    from config import DB_ENCRYPTION_KEY
    if DB_ENCRYPTION_KEY:
        driver = None
        try:
            from pysqlcipher3 import dbapi2 as driver  # type: ignore
        except Exception:
            try:
                import sqlcipher3 as driver  # type: ignore
            except Exception:
                driver = None
        if driver is not None:
            factory = _pooled_connection_class(driver.Connection) if pooled else driver.Connection
            conn = driver.connect(str(DB_PATH), timeout=30, factory=factory)
            # Key MUST be applied first, before touching any other table/pragma.
            conn.execute(f"PRAGMA key = \"x'{_sqlcipher_key_hex(DB_ENCRYPTION_KEY)}'\"")
            return conn
        _refuse_plaintext_with_key()
    factory = _pooled_connection_class(sqlite3.Connection) if pooled else sqlite3.Connection
    return sqlite3.connect(str(DB_PATH), timeout=30, factory=factory)


def _sqlcipher_key_hex(key: str) -> str:
    """Derive a 32-byte raw key (hex) from operator-supplied key material.

    Using an explicit raw key (PRAGMA key = "x'<64 hex>'") skips SQLCipher's
    per-open KDF and accepts any-length input, mirroring the uploads key.
    """
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _refuse_plaintext_with_key():
    """A key with no driver is a misconfigured appliance, never a plaintext one.

    Fail closed on both the live and the backup path: the operator asked for
    encryption, so writing PHI (or a backup of it) in the clear is the one
    outcome that must not happen quietly.
    """
    raise RuntimeError(
        "ORB_DB_ENCRYPTION_KEY is set but no SQLCipher driver (sqlcipher3 or "
        "pysqlcipher3) is installed. Refusing to open the database in plaintext. "
        "Install the driver pinned in requirements.txt and start Orb again."
    )


def encryption_at_rest_status() -> dict:
    """What the live connection proves about encryption, not what config says.

    `PRAGMA cipher_version` answers only on a SQLCipher connection. A key in
    .env says encryption was intended; a cipher version on the open connection
    says it is happening. Never raises: a failed probe reports inactive.
    """
    from config import DB_ENCRYPTION_KEY
    key_set = bool(DB_ENCRYPTION_KEY)
    conn = None
    try:
        # A fresh raw handle, not a pooled one: the probe must take the same
        # driver-selection path a new connection takes, not reuse an old answer.
        conn = _open_raw_connection()
        row = conn.execute("PRAGMA cipher_version").fetchone()
    except Exception as exc:
        return {"key_set": key_set, "cipher_version": None, "active": False,
                "error": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()
    version = (row[0] if row else None) or None
    return {"key_set": key_set, "cipher_version": version,
            "active": bool(key_set and version)}


def verify_encryption_at_rest() -> None:
    """Boot check: with a key configured, the open connection must be SQLCipher.

    Raises RuntimeError when it is not, so the appliance stops at start-up
    instead of serving from a database that is not encrypted.
    """
    status = encryption_at_rest_status()
    if status["key_set"] and not status["active"]:
        raise RuntimeError(
            "ORB_DB_ENCRYPTION_KEY is set but the database connection reports no "
            "SQLCipher cipher version"
            + (f" ({status['error']})" if status.get("error") else "")
            + ". Refusing to start on a database that is not encrypted."
        )


def _row_factory_for(conn):
    """The Row class belonging to whichever driver opened `conn`.

    stdlib `sqlite3.Row` is a C type that validates its argument is a
    `sqlite3.Cursor` and raises `TypeError: Row() argument 1 must be
    sqlite3.Cursor, not sqlcipher3.dbapi2.Cursor` for a SQLCipher cursor. Hard-
    coding sqlite3.Row therefore broke EVERY query the moment encryption at rest
    was actually switched on — the reason this path had never run in anger.

    Walks the MRO (not just type(conn).__module__): pooled connections are a
    subclass defined in THIS module, so the direct module check would misread
    a SQLCipher pooled handle as stdlib and reintroduce the Row bug.
    """
    modules = [(c.__module__ or "") for c in type(conn).__mro__]
    if any(m.startswith("sqlcipher3") for m in modules):
        import sqlcipher3.dbapi2 as _drv  # type: ignore
        return _drv.Row
    if any(m.startswith("pysqlcipher3") for m in modules):
        from pysqlcipher3 import dbapi2 as _drv  # type: ignore
        return _drv.Row
    return sqlite3.Row


# ── Pooled-connection classes ────────────────────────────────────────────────
# A pooled handle is shared by every db_session/get_db call on its thread, but
# plenty of legacy call sites still do `db = get_db() ... finally: db.close()`.
# With pooling ON, that close destroyed the shared handle out from under any
# enclosing db_session, whose rollback then died with "Cannot operate on a
# closed database" (the ORB_DB_POOL test failures, incl. the medication-safety
# gate). The pool therefore hands out a subclass whose close() is a no-op —
# ONLY the pool retires its handles, via pool_close().
_pooled_conn_classes: dict = {}


def _pooled_connection_class(base):
    cls = _pooled_conn_classes.get(base)
    if cls is None:
        class _PooledConnection(base):  # type: ignore[misc, valid-type]
            def close(self):
                """Lifecycle no-op + STATE RESET.

                Callers using the get_db() → mutate → close() idiom assume
                close() disposes of any connection-level state they flipped —
                several sites set isolation_level=None for a manual BEGIN
                IMMEDIATE (audit_service, who_checklist). On a SHARED pooled
                handle that state leaked to every later frame on the thread:
                autocommit mode made db_session's rollback a silent no-op,
                which let an eMAR administration survive its failed audit
                write. "Close" therefore restores the connection defaults;
                only pool_close() actually closes.
                """
                try:
                    if self.in_transaction:
                        self.rollback()
                    self.isolation_level = getattr(self, "_orb_default_isolation", "")
                except Exception:
                    logger.debug("pooled close(): state reset failed", exc_info=True)

            def pool_close(self):
                base.close(self)

        _PooledConnection.__name__ = f"Pooled{base.__name__}"
        _PooledConnection.__qualname__ = _PooledConnection.__name__
        _pooled_conn_classes[base] = _PooledConnection
        cls = _PooledConnection
    return cls


def _build_connection(pooled: bool = False) -> sqlite3.Connection:
    """Open + fully configure a SQLite connection (row_factory + WAL/PRAGMA tuning)."""
    conn = _open_raw_connection(pooled=pooled)
    if pooled:
        # Remember the driver's default so the neutered close() can restore
        # connection-level state flipped by manual-BEGIN call sites.
        conn._orb_default_isolation = conn.isolation_level  # type: ignore[attr-defined]
    conn.row_factory = _row_factory_for(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA page_size = 16384")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA mmap_size = 30000000000")
    conn.execute("PRAGMA cache_size = -256000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def get_db() -> sqlite3.Connection:
    """Get a database connection with row_factory.

    With ORB_DB_POOL enabled, returns a per-thread pooled connection (PRAGMAs
    already applied) tagged so db_session() keeps it open for reuse; otherwise
    opens a fresh connection per call (the default, thread-safe model the tests
    exercise). See db_backend.get_pooled.
    """
    from db_backend import get_pooled
    pooled = get_pooled(lambda: _build_connection(pooled=True), str(DB_PATH))
    if pooled is not None:
        return pooled
    return _build_connection()


from contextlib import contextmanager

# Per-thread db_session nesting depth for POOLED connections. Nested sessions
# on one thread share one pooled handle, so the inner exit must not commit or
# roll back the OUTER transaction — it gets SAVEPOINT semantics instead (inner
# failure rolls back only inner work, matching the isolation the per-call model
# provided via separate connections, without its nested-writer self-deadlock).
_session_nesting = threading.local()


@contextmanager
def db_session(immediate: bool = False):
    """Get a database session with automatic cleanup.

    Commits on success, rolls back on error. A pooled connection (ORB_DB_POOL)
    is left OPEN for reuse instead of closed — only per-call connections are
    closed here. Nested use on a pooled connection is safe: inner sessions run
    inside a SAVEPOINT; only the outermost commits/rolls back the transaction.

    ── READ THIS BEFORE WRITING A read-then-decide-then-write GUARD ──────────
    By DEFAULT this block is NOT a serialisable transaction, and the name makes
    it easy to assume otherwise. Python's sqlite3 in legacy mode opens a
    transaction only before DML (INSERT/UPDATE/DELETE) — a SELECT executes in
    AUTOCOMMIT. So in

        with db_session() as db:
            row = db.execute("SELECT status FROM medications ...").fetchone()
            if row["status"] != "active": raise ...
            db.execute("INSERT INTO medication_administrations ...")

    the SELECT is NOT inside the transaction the INSERT later takes. Another
    connection may change that row in between; the guard then decided on state
    that no longer holds, and the write still commits. Demonstrated against
    emar_service.record_administration 2026-08-06: a dose committed against an
    order discontinued mid-call, and its permanent ``allergy_check_json``
    recorded "allergies_on_file": 0 for a patient who by commit time had a
    charted SEVERE anaphylaxis allergy to that very drug. The window is normally
    sub-millisecond, so this is a race, not an exploit — but on an eMAR the
    record it leaves behind is the evidence someone reads after an incident.

    ``immediate=True`` closes it: the session takes ``BEGIN IMMEDIATE`` up
    front, so it holds the write lock before its first read and no other writer
    can interleave. Use it for any guard whose CORRECTNESS depends on what it
    read — eMAR administration, order state transitions, stock decrements — and
    NOT for ordinary reads or single-statement writes, which would then queue
    behind each other on a single-writer database for no benefit.

    Cost and constraints, so this is chosen with open eyes:
      * it serialises writers for the whole block, so keep the block short and
        never do LLM/network work inside one;
      * it must be the OUTERMOST session — a nested frame is a SAVEPOINT inside
        someone else's already-DEFERRED transaction and cannot be upgraded, so
        asking there raises rather than silently not delivering the guarantee;
      * on the UNPOOLED path (ORB_DB_POOL=0) a nested ``db_session()`` opens a
        SECOND connection, which will contend with this one's write lock for the
        full busy_timeout. Pooled mode (the default) shares the handle and is
        the mode to use with this flag.
    """
    # Nesting depth counted for EVERY session, pooled or not. `_session_nesting
    # .depth` alone cannot answer "am I inside another session?" on the UNPOOLED
    # path, because there each frame gets its OWN connection — so an inner
    # immediate session would sail past the guard, issue BEGIN IMMEDIATE against
    # the outer frame's write lock, stall for the full 30 s busy_timeout and then
    # die with "database is locked". Verified with ORB_DB_POOL=0 before this
    # counter existed.
    outer_depth = getattr(_session_nesting, "any_depth", 0)
    if immediate and outer_depth > 0:
        raise RuntimeError(
            "db_session(immediate=True) must be the OUTERMOST session: a nested "
            "frame runs as a SAVEPOINT inside an already-DEFERRED transaction "
            "(pooled) or on a second connection that would deadlock against it "
            "(unpooled), and neither can be upgraded to a write lock. Move the "
            "flag to the outermost session, or pass the existing connection down."
        )

    conn = get_db()
    from db_backend import discard_pooled, is_pooled
    pooled = is_pooled(conn)
    depth = getattr(_session_nesting, "depth", 0) if pooled else 0

    if immediate and getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "db_session(immediate=True) cannot upgrade a connection that is "
            "already inside a transaction."
        )

    _session_nesting.any_depth = outer_depth + 1
    try:
        if immediate:
            prev_isolation = conn.isolation_level
            conn.isolation_level = None  # we own BEGIN/COMMIT from here
            if pooled:
                _session_nesting.depth = 1
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:  # pragma: no cover — nothing left to roll back
                        logger.debug("immediate session rollback failed", exc_info=True)
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                # Restore FIRST: on a pooled handle this connection is shared
                # thread-wide, and a leaked isolation_level=None puts the whole
                # thread into autocommit — which silently turns every later
                # db_session rollback into a no-op (see db_backend.py; an eMAR
                # administration once survived its own failed audit write that way).
                conn.isolation_level = prev_isolation
                if pooled:
                    _session_nesting.depth = 0
                else:
                    conn.close()
            return

        if pooled and depth > 0:
            sp = f"orb_session_{depth}"
            conn.execute(f"SAVEPOINT {sp}")
            _session_nesting.depth = depth + 1
            try:
                yield conn
            except Exception:
                conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                conn.execute(f"RELEASE SAVEPOINT {sp}")
                raise
            else:
                conn.execute(f"RELEASE SAVEPOINT {sp}")
            finally:
                _session_nesting.depth = depth
            return

        if pooled:
            _session_nesting.depth = 1
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except DBProgrammingError:
                # Handle died mid-frame (should be impossible now that pooled
                # close() is neutered, but self-heal the pool rather than mask
                # the original error with a secondary one).
                if pooled:
                    discard_pooled(conn)
            raise
        else:
            conn.commit()
        finally:
            if pooled:
                _session_nesting.depth = 0
            else:
                conn.close()
    finally:
        _session_nesting.any_depth = outer_depth


# ── Off-loop DB execution for async contexts ─────────────────────────────────
# SQLite work is synchronous, and since encryption-at-rest every page I/O also
# pays HMAC-SHA512 + AES — a `with db_session()` on the event loop now blocks
# the whole API for the duration (the 2026-07-30 wedge: event_bus writes on the
# loop stalled /api/health for minutes; kill -USR1 named it). Async code must
# run DB work through run_db() instead.
#
# Sizing: a SMALL private pool, not one worker and not the asyncio default
# executor. WAL lets readers run concurrently, and writers already serialize on
# SQLite's own lock with busy_timeout=30s — hand-serializing every caller
# behind one thread would convoy interactive reads behind background writes.
# Private, so a background storm cannot drain the shared default executor.
# (event_bus keeps its own single-worker executor: its delivery bookkeeping
# relies on ordered execution.)
import asyncio as _asyncio
import atexit as _atexit
from concurrent.futures import ThreadPoolExecutor as _TPE
from functools import partial as _partial

_DB_EXECUTOR = _TPE(max_workers=4, thread_name_prefix="orb-db")
_atexit.register(_DB_EXECUTOR.shutdown, wait=False)


async def run_db(fn, *args, **kwargs):
    """Run blocking DB work (a sync function that opens its own db_session)
    off the event loop. The function must be PURE SYNC — it must not touch
    asyncio, and it must open/close its own session so the transaction begins
    and ends inside the worker thread."""
    # Fail loudly if someone hands this an `async def`. A coroutine function
    # called in a worker thread does not run — it just returns a coroutine
    # object, which run_db then hands back as a perfectly ordinary "result"
    # that nobody awaits. The work silently never happens, with no exception
    # and no log line.
    #
    # This is not hypothetical: nine call sites pass `log_audit_event` through
    # here. If that function ever became async, every one of those tamper-evident
    # audit rows would stop being written and the only symptom would be an
    # audit log that is quietly missing entries — the exact failure a hash-chained
    # audit trail exists to make impossible.
    if _asyncio.iscoroutinefunction(fn):
        raise TypeError(
            f"run_db() requires a synchronous callable, got coroutine function "
            f"{getattr(fn, '__qualname__', fn)!r}. Await it directly instead: "
            f"passing it here would silently skip the work."
        )
    loop = _asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, _partial(fn, *args, **kwargs))


def _open_raw_connection_at(path, apply_key: bool):
    """Open a raw connection to an arbitrary path, optionally applying the
    SQLCipher key. Mirrors _open_raw_connection() but for a caller-supplied path
    (used by consistent_backup so the backup file is encrypted iff the live DB
    is)."""
    from config import DB_ENCRYPTION_KEY
    if apply_key and DB_ENCRYPTION_KEY:
        driver = None
        try:
            from pysqlcipher3 import dbapi2 as driver  # type: ignore
        except Exception:
            try:
                import sqlcipher3 as driver  # type: ignore
            except Exception:
                driver = None
        if driver is not None:
            conn = driver.connect(str(path), timeout=30)
            conn.execute(f"PRAGMA key = \"x'{_sqlcipher_key_hex(DB_ENCRYPTION_KEY)}'\"")
            return conn
        # The backup copy of an encrypted DB must never land in plaintext.
        _refuse_plaintext_with_key()
    return sqlite3.connect(str(path), timeout=30)


def consistent_backup(dest_path) -> None:
    """Take a transactionally-consistent online backup of the live DB to
    dest_path, then integrity-check it.

    Uses the SQLite online backup API through codec-aware connections, so it
    works whether the live DB is plaintext OR SQLCipher-encrypted at rest (the
    stdlib sqlite3 driver cannot read an encrypted file — 'file is not a
    database'). When encryption is active the backup is written encrypted too,
    with the same key, so it never leaks plaintext PHI to disk.

    Raises RuntimeError if the fresh backup fails PRAGMA integrity_check.
    """
    from config import DB_ENCRYPTION_KEY
    encrypted = bool(DB_ENCRYPTION_KEY)
    src = _open_raw_connection()  # codec-aware for the live DB
    dst = _open_raw_connection_at(dest_path, apply_key=encrypted)
    try:
        with dst:
            src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        src.close()
        dst.close()
    if not ok:
        raise RuntimeError("backup failed integrity check")


def _harden_db_file_permissions():
    """Restrict the SQLite DB file and its WAL/SHM sidecars to user-only (0o600).

    An OS-level layer under the real control: encryption at rest is SQLCipher
    (see ENCRYPTION AT REST below), and these permissions also cover a deployment
    without a key.
    """
    import os
    from config import DB_PATH
    # The WAL/SHM sidecars also contain PHI and must be locked down too — the
    # previous implementation only chmod'd the main .db file.
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = f"{DB_PATH}{suffix}"
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except Exception as e:
                # Log loudly (not a silent print) — failure to lock PHI down is a
                # security-relevant event, but is non-fatal so we do not crash boot.
                logger.warning("Could not set 0o600 permissions on %s: %s", path, e)


# ──────────────────────────────────────────────────────────────────────────────
# ENCRYPTION AT REST — IMPLEMENTED 2026-07-30 (HIPAA addressable / GDPR Art.32).
# Set ORB_DB_ENCRYPTION_KEY and install a SQLCipher driver (sqlcipher3-wheels is
# pinned in requirements.txt); _open_raw_connection() then opens orb.db encrypted
# and the -wal/-shm sidecars are encrypted with it. An existing plaintext DB must
# be migrated once: scripts/encrypt_db.py --migrate (backs up, verifies row counts
# + audit-chain linkage + vector/FTS search, then swaps).
#
# HONEST LIMITATION: with the key in .env on the same disk, this protects DB
# COPIES (backups, exports, a stolen disk image read on another machine) but not
# a running/stolen appliance whose .env an attacker can also read. Pair it with
# FileVault, and move the key to the OS keychain for a stronger posture.
#
# FAILS CLOSED since 2026-10-03: a key with no driver is refused on the live and
# backup paths, and init_db() checks the open connection's cipher version.
#
# Historical note: it was once deferred over a feared conflict with sqlite-vec
# and the WAL/mmap PRAGMAs; the 2026-07-30 validation showed sqlcipher3-wheels
# coexists with both. What remains open is the key's home (.env today; the OS
# keychain and the escrow ceremony are the follow-ups).
# ──────────────────────────────────────────────────────────────────────────────


def _atomic_table_rebuild(conn, *, table: str, create_new_sql: str,
                          shared_cols: str, post_sql: list,
                          before_commit=None) -> None:
    """Rebuild `table` via the SQLite create-copy-drop-rename dance as a single
    ALL-OR-NOTHING unit. Either the rebuilt table is in place with every row, or
    the original is untouched. There is no third outcome.

    WHY THIS EXISTS — DO NOT GO BACK TO `executescript`.
    `Connection.executescript()` implicitly COMMITs whatever transaction is
    pending and then runs its statements in AUTOCOMMIT. So the old form —
    executescript("CREATE new; INSERT SELECT; DROP old; ALTER RENAME;") — is
    four separately-committed statements, not one. Reproduced: force the RENAME
    to fail after the DROP and the surviving tables are ['messages_new'] — the
    live `messages` table is GONE, db_session's rollback is a no-op because
    there is nothing left to roll back, and on the next boot
    `executescript(SCHEMA)` recreates an EMPTY `messages`, the
    `_messages_type_check_missing` guard reads the fresh DDL, sees both tokens
    and returns False forever. Every Relay message in the hospital's history
    then sits orphaned in `messages_new` where nothing will ever read it, with
    no error anywhere.

    Nor is it enough to lean on init_db's enclosing db_session: Python's sqlite3
    only opens an implicit transaction before DML (INSERT/UPDATE/DELETE), never
    before DDL, so a rebuild reached with nothing pending would run in
    autocommit and have exactly the same window. This function therefore takes
    explicit ownership of the transaction rather than inheriting one.

    ORDERING RULE: `PRAGMA foreign_keys` is a NO-OP inside an open transaction
    (the same trap that made the old INSERT-probe misfire on every boot). It
    must be issued while in autocommit, BEFORE the BEGIN — and it is needed,
    because `messages` is an FK parent (message_room_fit_flags.
    redirected_message_id), so DROP TABLE under enforced FKs would fail.

    `before_commit`, when given, is called with the connection after the
    rename and `post_sql`, inside the rebuild's own transaction. Raising from
    it rolls the whole rebuild back, so a caller can prove the rebuilt table
    (for example, that no FK child row lost its parent: PRAGMA
    foreign_key_check works inside a transaction) before anything is
    committed. Migration 52, the patients rebuild, uses it.
    """
    new_table = f"{table}_new"
    # The caller writes the CREATE by hand (the canonical schema, with the
    # column-level REFERENCES that PRAGMA table_info cannot reproduce), so the
    # one thing that can silently drift is the scratch table's NAME — and a
    # mismatch would DROP the live table and then rename something else.
    if new_table not in create_new_sql:
        raise RuntimeError(
            f"rebuild of {table!r} expects its CREATE to define {new_table!r}"
        )
    # This function COMMITs the caller's pending work before taking its own
    # transaction, which is correct from init_db's OUTERMOST db_session frame
    # (depth 1) and only there: inside a nested pooled frame the enclosing
    # session is a SAVEPOINT, and committing would silently release it and
    # publish an outer frame's half-finished work. Fail loudly rather than
    # corrupt it — this path only runs on legacy DBs at boot, so a hard error
    # is strictly better than a silent one.
    if getattr(_session_nesting, "depth", 0) > 1:
        raise RuntimeError(
            f"{table} rebuild attempted inside a nested db_session (depth="
            f"{_session_nesting.depth}); destructive DDL must own its transaction"
        )
    # Settle anything the caller had open: the rebuild must own its transaction,
    # not inherit one, and must not drag unrelated work into its rollback.
    if conn.in_transaction:
        conn.commit()
    # Autocommit-only pragmas — see ORDERING RULE above.
    conn.execute("PRAGMA legacy_alter_table = ON")
    conn.execute("PRAGMA foreign_keys = OFF")
    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # we own BEGIN/COMMIT from here
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f'DROP TABLE IF EXISTS "{new_table}"')
            conn.execute(create_new_sql)
            conn.execute(
                f'INSERT INTO "{new_table}" ({shared_cols}) '
                f'SELECT {shared_cols} FROM "{table}"'
            )
            # Prove the copy before anything destructive. Cheap, and it turns a
            # silent partial copy into a clean abort.
            src = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            dst = conn.execute(f'SELECT COUNT(*) FROM "{new_table}"').fetchone()[0]
            if src != dst:
                raise RuntimeError(
                    f"{table} rebuild aborted: copied {dst} of {src} rows"
                )
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{new_table}" RENAME TO "{table}"')
            for stmt in post_sql:
                conn.execute(stmt)
            if before_commit is not None:
                before_commit(conn)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                logger.debug("%s rebuild: rollback failed", table, exc_info=True)
            raise
    finally:
        # Restore before returning. On a POOLED handle close() is a neutered
        # no-op and the connection is shared thread-wide, so a leaked
        # isolation_level=None would put the whole thread in autocommit and
        # turn every later db_session rollback into a silent no-op.
        conn.isolation_level = prev_isolation
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")


def init_db():
    """Initialize database schema."""
    # Enforce secure database file permissions (0o600 -> user-only read/write)
    _harden_db_file_permissions()
    # Before any schema work: a configured key must yield an encrypted connection.
    verify_encryption_at_rest()

    with db_session() as conn:
        conn.executescript(SCHEMA)

        # ── Migration: tamper-evidence columns on audit_logs (additive, nullable) ──
        # Existing DBs created before the hash chain get the columns added; legacy
        # rows keep NULL hashes (chain validity begins at the first hashed row).
        audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
        if "prev_hash" not in audit_cols:
            logger.info("Migrating audit_logs: adding 'prev_hash' column")
            conn.execute("ALTER TABLE audit_logs ADD COLUMN prev_hash TEXT")
        if "row_hash" not in audit_cols:
            logger.info("Migrating audit_logs: adding 'row_hash' column")
            conn.execute("ALTER TABLE audit_logs ADD COLUMN row_hash TEXT")

        # ── Migration: belt-and-braces uniqueness on the audit hash chain ──
        # Two writers chaining off the same prev_hash is a chain fork (the
        # historical read-then-write race in audit_service). The partial UNIQUE
        # index makes the DB itself reject the second writer so audit_service can
        # retry against the fresh chain head. Partial (WHERE prev_hash IS NOT NULL)
        # so the genesis row and legacy pre-hash-chain rows (NULL prev_hash) stay
        # legal. If historical racy data already contains duplicate prev_hash
        # values, CREATE UNIQUE INDEX would abort boot — check first and skip+log.
        _audit_idx_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_audit_prev_hash_unique'"
        ).fetchone()
        if not _audit_idx_exists:
            _dup = conn.execute(
                """SELECT prev_hash, COUNT(*) AS n FROM audit_logs
                   WHERE prev_hash IS NOT NULL
                   GROUP BY prev_hash HAVING COUNT(*) > 1 LIMIT 1"""
            ).fetchone()
            if _dup:
                logger.warning(
                    "Skipping UNIQUE index on audit_logs(prev_hash): existing duplicate "
                    "prev_hash values found (e.g. %s x%d), likely from the historical "
                    "audit-chain write race. Forks remain detectable via "
                    "verify_audit_chain(); reseal the chain to enable the index.",
                    _dup[0], _dup[1],
                )
            else:
                logger.info("Migrating audit_logs: adding partial UNIQUE index on prev_hash")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_prev_hash_unique "
                    "ON audit_logs(prev_hash) WHERE prev_hash IS NOT NULL"
                )

        # ── Migration: add pin_hash column to users ──
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "pin_hash" not in user_cols:
            logger.info("Migrating users table: adding 'pin_hash' column")
            conn.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")

        # (v5) Per-account login lockout: persistent failure counter + lock
        # expiry, so credential stuffing spread across many IPs against one
        # account is throttled (the per-IP limiter alone can't see it) and the
        # lock survives a process restart.
        if "failed_login_attempts" not in user_cols:
            logger.info("Migrating users table: adding 'failed_login_attempts' column")
            conn.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER DEFAULT 0")
        if "login_locked_until" not in user_cols:
            logger.info("Migrating users table: adding 'login_locked_until' column")
            conn.execute("ALTER TABLE users ADD COLUMN login_locked_until REAL")

        # (v5) The universal default PIN is gone. Every boot used to seed
        # pin_hash = SHA256('1234') for every clinician with no PIN — a single
        # well-known 4-digit code as the sole human gate on agentic medication
        # dispatch. PINs must now be provisioned per user; a NULL pin_hash means
        # the agentic co-sign flow is unavailable for that user (fail closed).
        # DBs migrating from < v5 get the well-known hash revoked ONCE (ledger-
        # gated so a deliberately re-provisioned '1234'-equivalent hash — e.g.
        # in tests — is not silently re-revoked on a later init_db call).
        already_v5 = conn.execute(
            "SELECT 1 FROM schema_version WHERE version >= 5"
        ).fetchone()
        if not already_v5:
            import hashlib
            legacy_pin_1234 = hashlib.sha256(b"1234").hexdigest()
            revoked = conn.execute(
                "UPDATE users SET pin_hash = NULL WHERE pin_hash = ?", (legacy_pin_1234,)
            ).rowcount
            if revoked:
                logger.warning(
                    "Revoked the legacy universal '1234' PIN for %d user(s); "
                    "agentic co-sign requires re-provisioning a personal PIN.", revoked
                )

        # ── Migration: ensure messages.message_type CHECK allows the newer message
        # types ('ambient_action', 'surgical_alert') on databases created before
        # they existed.
        #
        # This is gated on the table's ACTUAL definition (sqlite_master.sql), not
        # a trial INSERT. The previous implementation probed with a throwaway
        # INSERT wrapped in `PRAGMA foreign_keys = OFF` — but that PRAGMA is a
        # no-op inside an open transaction, so the probe always failed on the
        # channel/sender FK (not a CHECK failure) and fired the destructive
        # RENAME→CREATE→copy rebuild on EVERY init_db() call. Once extra columns
        # (source/is_bot) were added, the hardcoded 9-column rebuild also crashed
        # with "table messages has 9 columns but 11 values were supplied".
        # Inspecting the schema text makes both migrations true no-ops once the
        # tokens are present (the shipped SCHEMA already includes them).
        def _messages_type_check_missing(*tokens: str) -> bool:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
            ).fetchone()
            if not row or not row[0]:
                return False
            sql = row[0]
            return any(tok not in sql for tok in tokens)

        if _messages_type_check_missing("ambient_action", "surgical_alert"):
            print("  [DB] Migrating messages table: widening message_type CHECK...")
            # CRASH-SAFE, FK-PRESERVING rebuild. Two prior-implementation defects
            # are fixed here:
            #   (1) reconstructing columns from PRAGMA table_info dropped the
            #       `sender_id REFERENCES users(id)` FK (PRAGMA doesn't expose
            #       column-level REFERENCES) AND emitted invalid SQL for
            #       parenthesized expression defaults, and
            #   (2) it RENAMEd the live table (an implicit COMMIT) BEFORE the new
            #       CREATE ran, so any CREATE failure stranded the data in an
            #       *_old table with no recovery.
            # Fix: build the new table (with the KNOWN canonical schema incl. both
            # FKs) FIRST, copy the intersection of columns, and only then drop the
            # old table and rename — nothing destructive happens until the new
            # table is proven to exist and be populated.
            #
            # (3) the whole dance ran through executescript(), which COMMITs and
            #     then autocommits each statement, so "nothing destructive until
            #     the new table is proven" held for STATEMENT ORDER but not for
            #     ATOMICITY: a crash or a failed RENAME between the DROP and the
            #     RENAME destroyed the live table with no rollback. It now runs
            #     through _atomic_table_rebuild — one BEGIN IMMEDIATE, one COMMIT,
            #     with a row-count check before the DROP.
            existing = {c[1] for c in conn.execute("PRAGMA table_info(messages)").fetchall()}
            # Canonical column list == shipped SCHEMA + the source/is_bot additive
            # migration. Copy only columns the legacy table actually has.
            canonical = ["id", "channel_id", "sender_id", "sender_name", "sender_role",
                         "content", "message_type", "timestamp", "read_by", "source", "is_bot"]
            shared = ", ".join(c for c in canonical if c in existing)
            _atomic_table_rebuild(
                conn,
                table="messages",
                shared_cols=shared,
                create_new_sql="""
                CREATE TABLE messages_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL REFERENCES channels(id),
                    sender_id TEXT NOT NULL REFERENCES users(id),
                    sender_name TEXT NOT NULL,
                    sender_role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_type TEXT DEFAULT 'text' CHECK (message_type IN ('text', 'alert', 'system', 'ambient_action', 'surgical_alert')),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    read_by TEXT DEFAULT '[]',
                    source TEXT DEFAULT 'clinician',
                    is_bot INTEGER DEFAULT 0
                )""",
                post_sql=[
                    "CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, timestamp DESC)",
                ],
            )

        # ── Migration: update surgeries CHECK constraint for emergency states ──
        # Gated on the table's ACTUAL definition, not a trial INSERT — same fix as
        # the messages migration above. The old INSERT-probe wrapped in
        # `PRAGMA foreign_keys = OFF` (a no-op inside a transaction) always tripped
        # the patient/surgeon FK and fired a destructive RENAME→rebuild on EVERY
        # init_db(). Worse, `ALTER TABLE surgeries RENAME` rewrites child FK
        # references (surgery_team_assignments → surgeries_old), which were then
        # left dangling when surgeries_old was dropped — corrupting the schema so
        # later FK-checked operations failed with "no such table: surgeries_old".
        # Inspecting the schema text makes this a true no-op once the emergency
        # statuses are present (the shipped SCHEMA already includes them).
        _surg_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='surgeries'"
        ).fetchone()
        if _surg_row and _surg_row[0] and "emergency_pending" not in _surg_row[0]:
            print("  [DB] Migrating surgeries table for emergency statuses...")
            # CRASH-SAFE + COLUMN-ADAPTIVE rebuild (same discipline as messages):
            # build surgeries_new FIRST, copy the INTERSECTION of columns, then
            # drop+rename. The previous `INSERT INTO surgeries SELECT *` assumed
            # exactly 19 columns and RENAMEd the live table before the CREATE, so
            # ANY column drift stranded all surgical data in an *_old table.
            # legacy_alter_table keeps the final RENAME from rewriting child FKs
            # (surgery_team_assignments); building the new table before touching
            # the old one means a CREATE failure destroys nothing.
            _surg_existing = {c[1] for c in conn.execute("PRAGMA table_info(surgeries)").fetchall()}
            _surg_canonical = ["id", "patient_id", "surgeon_id", "procedure_name", "diagnosis",
                               "scheduled_date", "scheduled_time", "estimated_duration_min",
                               "operating_room", "status", "co_morbidities", "surgical_checklist",
                               "instruments_required", "staff_required", "risk_assessment",
                               "possible_complications", "success_probability", "notes", "created_at"]
            _surg_shared = ", ".join(c for c in _surg_canonical if c in _surg_existing)
            # Was strictly worse than the messages block: an explicit conn.commit()
            # here, then an executescript() whose DROP and RENAME each committed
            # on their own. _atomic_table_rebuild makes the drop+rename one unit.
            _atomic_table_rebuild(
                conn,
                table="surgeries",
                shared_cols=_surg_shared,
                create_new_sql="""
                CREATE TABLE surgeries_new (
                    id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL REFERENCES patients(id),
                    surgeon_id TEXT NOT NULL REFERENCES users(id),
                    procedure_name TEXT NOT NULL,
                    diagnosis TEXT NOT NULL,
                    scheduled_date DATE NOT NULL,
                    scheduled_time TEXT NOT NULL,
                    estimated_duration_min INTEGER,
                    operating_room TEXT,
                    status TEXT DEFAULT 'scheduled' CHECK (status IN ('scheduled','in_progress','completed','cancelled','emergency_pending','emergency_context_generating','emergency_ready','emergency_acknowledged','emergency_staged')),
                    co_morbidities TEXT,
                    surgical_checklist TEXT,
                    instruments_required TEXT,
                    staff_required TEXT,
                    risk_assessment TEXT,
                    possible_complications TEXT,
                    success_probability REAL,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                post_sql=[
                    "CREATE INDEX IF NOT EXISTS idx_surgeries_surgeon ON surgeries(surgeon_id, scheduled_date)",
                    "CREATE INDEX IF NOT EXISTS idx_surgeries_patient ON surgeries(patient_id)",
                    "CREATE INDEX IF NOT EXISTS idx_surgeries_date ON surgeries(scheduled_date)",
                ],
            )

        # ── Migration: add source + source_event_id columns to vitals ──
        # Safe ADD COLUMN — nullable columns, existing rows get NULL.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
        if "source" not in cols:
            print("  [DB] Migrating vitals table: adding 'source' column...")
            conn.execute("ALTER TABLE vitals ADD COLUMN source TEXT DEFAULT 'simulated'")
        if "source_event_id" not in cols:
            print("  [DB] Migrating vitals table: adding 'source_event_id' column...")
            conn.execute("ALTER TABLE vitals ADD COLUMN source_event_id INTEGER REFERENCES ingestion_events(id)")

        # ── Migration: add verification + pipeline meta to scribe_notes ──
        scribe_cols = {r[1] for r in conn.execute("PRAGMA table_info(scribe_notes)").fetchall()}
        if "verification_json" not in scribe_cols:
            print("  [DB] Migrating scribe_notes: adding 'verification_json' column...")
            conn.execute("ALTER TABLE scribe_notes ADD COLUMN verification_json TEXT")
        if "pipeline_meta_json" not in scribe_cols:
            print("  [DB] Migrating scribe_notes: adding 'pipeline_meta_json' column...")
            conn.execute("ALTER TABLE scribe_notes ADD COLUMN pipeline_meta_json TEXT")

        # ── Migration: add note lifecycle columns to scribe_notes ──
        # Re-read columns after previous migration block
        scribe_cols = {r[1] for r in conn.execute("PRAGMA table_info(scribe_notes)").fetchall()}
        lifecycle_cols = {
            "status":           "TEXT DEFAULT 'draft'",
            "version":          "INTEGER DEFAULT 1",
            "supersedes_note_id": "INTEGER",
            "signed_by":        "TEXT",
            "signed_at":        "TIMESTAMP",
            "finalized_at":     "TIMESTAMP",
            "rejected_by":      "TEXT",
            "rejected_at":      "TIMESTAMP",
            "rejection_reason": "TEXT",
            "last_edited_by":   "TEXT",
            "last_edited_at":   "TIMESTAMP",
            "ai_generated":     "INTEGER DEFAULT 1",
        }
        added_lifecycle = False
        for col_name, col_def in lifecycle_cols.items():
            if col_name not in scribe_cols:
                print(f"  [DB] Migrating scribe_notes: adding '{col_name}' column...")
                conn.execute(f"ALTER TABLE scribe_notes ADD COLUMN {col_name} {col_def}")
                added_lifecycle = True

        # Legacy row migration: existing notes (created before lifecycle) should be
        # treated as historically final / immutable. We set them to 'signed' so they
        # are NOT accidentally editable as drafts. Only truly new notes start as 'draft'.
        if added_lifecycle:
            existing_count = conn.execute(
                "SELECT COUNT(*) FROM scribe_notes WHERE status IS NULL OR status = 'draft'"
            ).fetchone()[0]
            if existing_count > 0:
                print(f"  [DB] Migrating {existing_count} legacy scribe note(s) to status='signed' (immutable)...")
                conn.execute("""
                    UPDATE scribe_notes
                    SET status = 'signed',
                        signed_by = author_id,
                        signed_at = created_at,
                        finalized_at = created_at,
                        ai_generated = 1
                    WHERE status IS NULL OR (status = 'draft' AND signed_by IS NULL AND created_at < datetime('now', '-1 minute'))
                """)

        # ── Migration: add date_of_birth to patients ──
        patient_cols = {r[1] for r in conn.execute("PRAGMA table_info(patients)").fetchall()}
        if "date_of_birth" not in patient_cols:
            print("  [DB] Migrating patients: adding 'date_of_birth' column...")
            conn.execute("ALTER TABLE patients ADD COLUMN date_of_birth DATE")

        # ── Migration: add sage_actions and sage_action_events ──
        try:
            conn.execute("SELECT 1 FROM sage_actions LIMIT 1")
        except DBOperationalError:
            print("  [DB] Migrating Sage Ambient: creating sage_actions and sage_action_events tables...")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sage_actions (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    patient_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    rationale TEXT,
                    confidence REAL,
                    state TEXT DEFAULT 'suggested' CHECK (state IN ('suggested', 'acknowledged', 'dismissed', 'confirmed', 'failed', 'superseded')),
                    version INTEGER DEFAULT 1,
                    source_msg_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sage_actions_room ON sage_actions(room_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS sage_action_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL REFERENCES sage_actions(id),
                    prior_state TEXT,
                    new_state TEXT NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES users(id),
                    actor_name TEXT NOT NULL,
                    actor_role TEXT,
                    reason TEXT,
                    source TEXT DEFAULT 'ui' CHECK (source IN ('ui', 'system')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sage_action_events_action ON sage_action_events(action_id, created_at DESC);
            """)

        # ── Migration: add admission fields to patients ──
        patient_cols = {r[1] for r in conn.execute("PRAGMA table_info(patients)").fetchall()}
        new_columns = [
            ("phone", "TEXT"),
            ("emergency_contact_name", "TEXT"),
            ("emergency_contact_phone", "TEXT"),
            ("address", "TEXT"),
        ]
        
        migrated_any = False
        for col_name, col_type in new_columns:
            if col_name not in patient_cols:
                conn.execute(f"ALTER TABLE patients ADD COLUMN {col_name} {col_type}")
                migrated_any = True
                
        if migrated_any:
            print("  [DB] Migrating patients: added new contact/identity columns...")

        # ── Migration: per-patient NEWS2 Scale 2 flag (COPD / chronic type-2
        # respiratory failure). Drives the SpO2 scoring branch in vigil_service. ──
        if "on_scale_2" not in patient_cols:
            conn.execute("ALTER TABLE patients ADD COLUMN on_scale_2 INTEGER DEFAULT 0")
            logger.info("Migrating patients: added 'on_scale_2' (NEWS2 Scale 2) column")

        # ── Migration: persist supplemental-oxygen status on the vitals row so
        # NEWS2 scores the +2 O2 contribution from recorded data, not a carry-forward. ──
        vitals_cols = {r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()}
        if "on_oxygen" not in vitals_cols:
            conn.execute("ALTER TABLE vitals ADD COLUMN on_oxygen INTEGER DEFAULT 0")
            logger.info("Migrating vitals: added 'on_oxygen' column")

        # ── Migration: provenance + verification on medications. `source`
        # distinguishes clinician-entered from AI/agentic-extracted meds; an
        # llm_extracted/agentic med is NOT active until a clinician verifies it. ──
        med_cols = {r[1] for r in conn.execute("PRAGMA table_info(medications)").fetchall()}
        if "source" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN source TEXT DEFAULT 'clinician'")
            logger.info("Migrating medications: added 'source' provenance column")
        if "verified_by" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN verified_by TEXT")
        if "verified_at" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN verified_at TIMESTAMP")

        # ── Migration: idempotency keys for offline-replay-safe writes ──
        # The client sends an Idempotency-Key header on note/medication POSTs
        # (offline replays reuse the same key). Nullable column + partial UNIQUE
        # index (NULLs exempt) lets routes dedupe replays without touching
        # legacy rows or key-less inserts.
        doc_cols = {r[1] for r in conn.execute("PRAGMA table_info(clinical_documents)").fetchall()}
        if "idempotency_key" not in doc_cols:
            conn.execute("ALTER TABLE clinical_documents ADD COLUMN idempotency_key TEXT")
            logger.info("Migrating clinical_documents: added 'idempotency_key' column")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_clinical_documents_idempotency_key
               ON clinical_documents(idempotency_key) WHERE idempotency_key IS NOT NULL"""
        )
        if "idempotency_key" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN idempotency_key TEXT")
            logger.info("Migrating medications: added 'idempotency_key' column")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_medications_idempotency_key
               ON medications(idempotency_key) WHERE idempotency_key IS NOT NULL"""
        )

        # ── Migration: MFA (TOTP) + session revocation on users ──
        # totp_secret/totp_enabled drive optional two-factor auth; token_version is
        # bumped to revoke all of a user's outstanding JWTs (logout-everywhere).
        user_cols2 = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "totp_secret" not in user_cols2:
            conn.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        if "totp_enabled" not in user_cols2:
            conn.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0")
        if "token_version" not in user_cols2:
            conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 0")
        # totp_last_step records the highest TOTP time-step already consumed, so a
        # captured code cannot be REPLAYED within its validity window (RFC 6238
        # §5.2 single-use requirement). -1 = nothing consumed yet.
        if "totp_last_step" not in user_cols2:
            conn.execute("ALTER TABLE users ADD COLUMN totp_last_step INTEGER DEFAULT -1")

        # ── Migration: user lifecycle (admin user management) ──
        # is_active=0 disables login without deleting the row (audit trails keep
        # referencing the user); appointments idempotency mirrors notes/medications.
        if "is_active" not in user_cols2:
            conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
            logger.info("Migrating users: added 'is_active' column")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_appointments_patient
               ON appointments(patient_id, scheduled_date)"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idem
               ON appointments(idempotency_key) WHERE idempotency_key IS NOT NULL"""
        )

        # ── Migration: provenance on relay messages ──
        # Distinguishes bot/automated posts from real clinician messages (parity
        # with the vitals/medications source columns).
        msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "source" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'clinician'")
        if "is_bot" not in msg_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN is_bot INTEGER DEFAULT 0")

        # ── Migration: scrub plaintext discharge access tokens ──
        # Rows created before token hashing stored the RAW url token as token_id
        # (PK) with its SHA-256 in token_hash. All lookups now go through
        # token_hash (see services/discharge_access_service.py), so overwrite the
        # plaintext token_id with the hash. One-time and idempotent: afterwards
        # every row has token_id == token_hash and the raw secret no longer
        # exists at rest. Existing patient links keep working (they resolve via
        # token_hash).
        try:
            scrubbed = conn.execute(
                "UPDATE discharge_access_tokens SET token_id = token_hash WHERE token_id != token_hash"
            )
            if scrubbed.rowcount > 0:
                print(f"  [DB] Scrubbed {scrubbed.rowcount} plaintext discharge token(s) (token_id := token_hash)")
        except Exception as e:
            logger.warning("Discharge token scrub migration failed: %s", e)

        # Seed system user for AI background logs.
        #
        # DEPARTMENT IS NULL, not 'IT' (2026-09-07). No ward is called "IT", so
        # the value could only ever have meant "scoped to a department nobody is
        # admitted to" — the same defect that made PHA001 unable to reach a
        # single patient (migration 36) and ADM001 unable to see one before that
        # (2026-08-12). It was never exploitable here because 'system' is an
        # AUDIT IDENTITY whose placeholder hash can never authenticate, so no
        # request ever derives a scope for it. It is corrected anyway: a column
        # that would be wrong the moment anything read it is a trap left armed,
        # and this is the third account in this codebase to sit on it.
        # Listed in SERVICE_ACCOUNT_IDS, so User Management cannot touch it.
        conn.execute(
            """INSERT OR IGNORE INTO users (id, name, role, department, password_hash)
               VALUES ('system', 'Orb Sage', 'admin', NULL, 'system_hash')"""
        )

        # ── Record the schema version we just (re)applied ──
        # Migration blocks above remain additive + idempotent (PRAGMA introspection
        # guarded), so re-running them is safe; this ledger gives an auditable record
        # of which schema versions have been applied to this DB.
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, note) VALUES (?, ?)",
                (SCHEMA_VERSION, "init_db additive migrations applied"),
            )
        except Exception as e:
            logger.warning("Could not record schema_version: %s", e)

        # ── Versioned migration framework ──
        # All NEW schema changes go through server/migrations.py (ordered,
        # ledgered, re-runnable) rather than another ad-hoc block above. Runs
        # inside this same transaction so a failure rolls the whole init back.
        try:
            from migrations import run_pending_migrations
            run_pending_migrations(conn)
        except Exception as e:
            logger.error("Migration framework failed during init_db: %s", e)
            raise

    # Validate the configured DB backend loudly at boot (unsupported/unwired
    # values already raised at import; this just logs the active backend + pool).
    try:
        from db_backend import validate_backend_at_startup
        validate_backend_at_startup()
    except Exception as e:  # pragma: no cover — never let logging block boot
        logger.warning("db_backend validation note: %s", e)

    # Re-apply permission hardening AFTER init: executescript with WAL may have
    # created the -wal/-shm sidecars during this call.
    _harden_db_file_permissions()


def verify_audit_chain(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Verify the audit_logs hash chain for tamper-evidence.

    Recomputes each row's hash from the stored prev_hash + canonical fields and
    confirms it matches the persisted row_hash, and that prev_hash links to the
    prior row. Rows with NULL row_hash (legacy, pre-hash-chain) are skipped and
    counted separately. Returns a summary dict; never raises on a broken chain.
    """
    from services.audit_service import compute_audit_row_hash  # local import avoids cycle

    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, timestamp, correlation_id, event_category, actor_user_id,
                      actor_role, target_type, target_id, patient_id, encounter_id,
                      source_type, source_event_id, request_path, http_method, ip_address,
                      user_agent, session_id, outcome, action_type, summary_message,
                      metadata_json, prev_hash, row_hash
               FROM audit_logs ORDER BY id ASC"""
        ).fetchall()
        checked = 0
        skipped = 0
        broken = []
        last_hash = None
        for r in rows:
            d = dict(r)
            if d["row_hash"] is None:
                skipped += 1
                last_hash = None  # chain restarts after a legacy gap
                continue
            expected = compute_audit_row_hash(d, d.get("prev_hash"))
            link_ok = (last_hash is None) or (d.get("prev_hash") == last_hash)
            if expected != d["row_hash"] or not link_ok:
                broken.append(d["id"])
            checked += 1
            last_hash = d["row_hash"]
        return {
            "ok": len(broken) == 0,
            "rows_total": len(rows),
            "rows_checked": checked,
            "rows_skipped_legacy": skipped,
            "broken_row_ids": broken,
        }
    finally:
        if own_conn:
            conn.close()


if __name__ == "__main__":
    init_db()
