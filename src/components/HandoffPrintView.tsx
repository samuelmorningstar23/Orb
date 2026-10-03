import React from 'react';
import { formatClinicalDateTime } from '../lib/datetime';
import { vitalTemperature, vitalWhole } from '../lib/vitals';
import { news2Description } from '../lib/news2';
import type { News2 } from '../api/nurse';

// Types derived from usage in VigilPanel
interface Patient {
  mrn: string;
  name: string;
  age: number;
  sex: string;
  ward: string;
  bed: string;
  diagnosis: string;
}

// The chart's live risk object, i.e. vigil_service.compute_news2 output, or
// the no-vitals shape ({score: 0, level: 'stable', drivers: [...]}) that has
// no raw_news2 at all. `score` is the 0-1 normalised INDEX of the aggregate.
// It stays on the type because the object carries it, but it is never
// printed: see the risk box below.
interface Risk {
  score: number;
  level: string;
  raw_news2?: number | null;
  /** False means an unmeasured parameter scored 0: the aggregate is a minimum. */
  complete?: boolean;
  coverage_note?: string | null;
  single_param_3?: boolean;
  spo2_scale?: number;
}

/**
 * The NEWS2 sentence for the risk box, from the same function the badge uses,
 * so the paper and the screen cannot spell the score two ways. Guarded here
 * rather than trusting news2Description to cope: the no-vitals shape has no
 * raw_news2, and a missing or non-finite aggregate must print as "not
 * recorded", never as "NEWS2 undefined" or as a zero.
 */
function news2Line(risk: Risk): string {
  if (typeof risk.raw_news2 !== 'number' || !Number.isFinite(risk.raw_news2)) {
    return 'NEWS2 not recorded';
  }
  // The object is passed through as it came from the server; news2Description
  // reads raw_news2, level, single_param_3, spo2_scale and complete off it.
  return news2Description(risk as News2);
}

interface Vital {
  timestamp: string;
  heart_rate: number;
  systolic_bp: number;
  diastolic_bp: number;
  respiratory_rate: number;
  temperature: number;
  spo2: number;
}

interface Note {
  id: string;
  created_at: string;
  author_id: string;
  author_name: string;
  author_role: string;
  note_type: string;
  content: string;
}

interface Medication {
  id: string;
  name: string;
  dosage: string;
  frequency: string;
  start_date: string;
  status: string;
}

interface HandoffPrintViewProps {
  patient: Patient;
  risk: Risk;
  latestVitals: Partial<Vital>;
  notes: Note[];
  medications: Medication[];
  aiAssessment?: string;
}

export function HandoffPrintView({ patient, risk, latestVitals, notes, medications, aiAssessment }: HandoffPrintViewProps) {
  const currentDate = formatClinicalDateTime(new Date());

  return (
    <div className="handoff-print-layout">
      {/* Header */}
      <div className="handoff-header">
        <div className="handoff-header-branding">
          <h1>Orb Hospital System</h1>
          <p>Official Shift Handoff Report</p>
        </div>
        <div className="handoff-header-meta">
          <p><strong>Generated:</strong> {currentDate}</p>
          <p><strong>Status:</strong> CONFIDENTIAL MEDICAL RECORD</p>
        </div>
      </div>

      <hr className="handoff-divider" />

      {/* Patient demographics & risk */}
      <div className="handoff-section">
        <div className="handoff-grid-2">
          <div>
            <h2 className="handoff-patient-name">{patient.name}</h2>
            <p className="handoff-patient-demo">
              {patient.age}y/{patient.sex} · MRN: {patient.mrn} · Ward: {patient.ward} · Bed: {patient.bed}
            </p>
            <p className="handoff-diagnosis"><strong>Diagnosis:</strong> {patient.diagnosis}</p>
          </div>
          {/* NEWS2, not a percent (fixed 2026-10-02). This box used to print
              risk.score * 100 as "45% - ELEVATED" for a NEWS2 of 9: the 0-1
              index read as a probability of deterioration it never was, and
              a patient with no observations printed "0% - STABLE", which reads
              as a clean set. A signed handover has to say the aggregate the
              chart shows, and say when it is only a minimum. */}
          <div className="handoff-risk-box">
            <h3>NEWS2</h3>
            <div className="handoff-risk-val">{news2Line(risk)}</div>
            {risk.complete === false && typeof risk.coverage_note === 'string' && risk.coverage_note.trim() !== '' && (
              <p className="handoff-risk-note" style={{ margin: '4px 0 0', maxWidth: '18rem' }}>
                <small>{risk.coverage_note}</small>
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Current Vitals */}
      <div className="handoff-section">
        <h3 className="handoff-section-title">Latest Vitals Summary</h3>
        <div className="handoff-vitals-row">
          <div className="handoff-vital">
            <span className="vital-label">HR</span>
            <span className="vital-val">{vitalWhole(latestVitals.heart_rate, '--')} <small>bpm</small></span>
          </div>
          <div className="handoff-vital">
            <span className="vital-label">SpO₂</span>
            <span className="vital-val">{vitalWhole(latestVitals.spo2, '--')} <small>%</small></span>
          </div>
          <div className="handoff-vital">
            <span className="vital-label">BP (Sys/Dia)</span>
            <span className="vital-val">
              {vitalWhole(latestVitals.systolic_bp, '--')}/{vitalWhole(latestVitals.diastolic_bp, '--')} <small>mmHg</small>
            </span>
          </div>
          <div className="handoff-vital">
            <span className="vital-label">Resp</span>
            <span className="vital-val">{vitalWhole(latestVitals.respiratory_rate, '--')} <small>/min</small></span>
          </div>
          <div className="handoff-vital">
            <span className="vital-label">Temp</span>
            <span className="vital-val">{vitalTemperature(latestVitals.temperature, '--')} <small>°C</small></span>
          </div>
        </div>
      </div>

      {/* AI Clinical Assessment */}
      {aiAssessment && (
        <div className="handoff-section">
          <h3 className="handoff-section-title">MedGemma AI Clinical Assessment</h3>
          <div style={{ 
            whiteSpace: 'pre-wrap', 
            fontSize: '0.9rem', 
            lineHeight: 1.7, 
            padding: '12px 16px',
            border: '1px solid #CCC',
            borderRadius: '4px',
            backgroundColor: '#FAFAFA'
          }}>
            {aiAssessment}
          </div>
        </div>
      )}

      {/* Active Medications */}
      <div className="handoff-section">
        <h3 className="handoff-section-title">Active Medications</h3>
        {medications.length > 0 ? (
          <table className="handoff-table">
            <thead>
              <tr>
                <th>Medication</th>
                <th>Dosage & Freq</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {medications.map(med => (
                <tr key={med.id}>
                  <td><strong>{med.name}</strong></td>
                  <td>{med.dosage} · {med.frequency}</td>
                  <td>{med.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="handoff-empty">No active medications recorded.</p>
        )}
      </div>

      {/* Recent Notes */}
      <div className="handoff-section">
        <h3 className="handoff-section-title">Recent Clinical Notes (Max 3)</h3>
        {notes.length > 0 ? (
          <div className="handoff-notes-list">
            {notes.slice(0, 3).map(note => (
              <div key={note.id} className="handoff-note">
                <div className="handoff-note-meta">
                  <strong>{note.author_name} ({note.author_role})</strong>
                  <span>{formatClinicalDateTime(note.created_at)}</span>
                  <span className="handoff-note-type">[{note.note_type}]</span>
                </div>
                <div className="handoff-note-content">{note.content}</div>
              </div>
            ))}
          </div>
        ) : (
          <p className="handoff-empty">No recent clinical notes.</p>
        )}
      </div>

      {/* Signoff / Footer */}
      <div className="handoff-footer">
        <div className="handoff-signoff">
          <p>Handing over Provider Signature:</p>
          <div className="handoff-signature-line"></div>
          <p>Receiving Provider Signature:</p>
          <div className="handoff-signature-line"></div>
        </div>
        {/* REMOVED 2026-09-07: six Unicode block characters arranged to look
            like a QR code, captioned "Scan for Digital Chart". It scanned to
            nothing. A fake affordance on a printed clinical handoff is worse
            than no affordance: a nurse points a phone at it, gets nothing, and
            learns the paperwork lies. The MRN in the header is the real handle
            onto the chart. Put a QR back here when it encodes something. */}
      </div>
    </div>
  );
}
