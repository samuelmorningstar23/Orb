import { formatClinicalDate, formatClinicalDateTime } from '../lib/datetime';
import { vitalTemperature, vitalWhole } from '../lib/vitals';

const NOT_RECORDED = 'Not recorded';
const NOT_DOCUMENTED = 'Not documented';

export interface RecordPatient {
  mrn?: string | null;
  name: string;
  age?: number | string | null;
  sex?: string | null;
  ward?: string | null;
  bed?: string | null;
  diagnosis?: string | null;
  admission_date?: string | null;
  attending_doctor_name?: string | null;
  admission_id?: string | null;
}

export interface RecordAllergy { substance: string; reaction?: string | null; severity?: string | null }

export interface RecordMedication {
  id?: string | number;
  drug_name?: string | null;
  dosage?: string | null;
  route?: string | null;
  frequency?: string | null;
  status?: string | null;
  unverified?: boolean;
}

export interface RecordVitals {
  timestamp?: string | null;
  heart_rate?: number | null;
  systolic_bp?: number | null;
  diastolic_bp?: number | null;
  spo2?: number | null;
  temperature?: number | null;
  respiratory_rate?: number | null;
  /** The sheet's pain score, 0 to 10 (migration 87). Printed with the set. */
  pain_score?: number | null;
}

/** A stored date or timestamp in the clinical form, or "Not recorded". A
 *  date-only value is a calendar day, not midnight UTC. */
function recordedDate(v: string | null | undefined, withTime = true): string {
  if (!v) return NOT_RECORDED;
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    const d = new Date(`${v}T00:00:00`);
    return isNaN(d.getTime()) ? v : formatClinicalDate(d);
  }
  return (withTime ? formatClinicalDateTime(v) : formatClinicalDate(v)) || v;
}

function allergyLine(a: RecordAllergy): string {
  const detail = [a.reaction, a.severity].filter(x => x && x !== 'unknown').join(', ');
  return detail ? `${a.substance} (${detail})` : a.substance;
}

/** The latest recorded set at chart precision, with when it was taken. */
function vitalsLine(v: RecordVitals | null | undefined): string {
  if (!v) return 'No vitals recorded';
  return [
    `HR ${vitalWhole(v.heart_rate, '--')} bpm`,
    `BP ${vitalWhole(v.systolic_bp, '--')}/${vitalWhole(v.diastolic_bp, '--')} mmHg`,
    `SpO2 ${vitalWhole(v.spo2, '--')}%`,
    `Temp ${vitalTemperature(v.temperature, '--')}°C`,
    `RR ${vitalWhole(v.respiratory_rate, '--')}/min`,
    `Pain ${vitalWhole(v.pain_score, '--')}/10`,
  ].join(', ') + `. Recorded ${recordedDate(v.timestamp ?? null)}.`;
}

function medicationLine(m: RecordMedication): string {
  const prn = m.status === 'prn' ? ' (as needed)' : '';
  return `${m.drug_name || 'Unnamed medicine'}: ${m.dosage || 'dose not recorded'}, `
    + `${m.route || 'route not recorded'}, ${m.frequency || 'frequency not recorded'}${prn}.`;
}

/* THE CHART'S "PRINT RECORD": a factual inpatient record, for a patient who is
 * still admitted.
 *
 * It used to print the DISCHARGE SUMMARY template, dated today, for a patient
 * nobody had discharged, with that template's standing clinical prose. This
 * prints what the chart holds (identity, allergies, problems, active medicines,
 * the latest observation set and the signed notes) and carries no discharge
 * date.
 *
 * The root carries `discharge-print-layout` because src/print.css (owned
 * elsewhere) keys its print-only reveal and page styles on that class; it is a
 * styling hook, not a statement about what this document is. */

export interface RecordProblem {
  problem: string;
  icd10_code?: string | null;
  onset_date?: string | null;
}

export interface RecordNote {
  title: string;
  author?: string | null;
  at?: string | null;
  text: string;
}

interface InpatientRecordPrintViewProps {
  patient: RecordPatient;
  allergies: RecordAllergy[] | null;
  problems: RecordProblem[];
  comorbidities?: string[];
  /** Active and as-needed orders, with the unverified flag the chart carries. */
  medications: RecordMedication[];
  /** False on an Orb Ward Record computer, whose drug chart stays on paper:
   *  the record has no medicines section at all, not even an empty one. */
  showMedicines?: boolean;
  latestVitals?: RecordVitals | null;
  notes: RecordNote[];
  siteName?: string | null;
  printedAt?: Date;
}

export function InpatientRecordPrintView({
  patient, allergies, problems, comorbidities = [], medications, showMedicines = true,
  latestVitals, notes, siteName, printedAt = new Date(),
}: InpatientRecordPrintViewProps) {
  return (
    <div className="discharge-print-layout inpatient-record-print" data-testid="inpatient-record-print">
      <div className="discharge-title-wrapper">
        {siteName && <p style={{ textAlign: 'center', margin: '0 0 4px', fontWeight: 600 }}>{siteName}</p>}
        <h1 className="discharge-title" style={{ textDecoration: 'underline', marginBottom: '8px', textAlign: 'center' }}>INPATIENT RECORD</h1>
        <p style={{ textAlign: 'center', margin: 0 }}>Printed {formatClinicalDateTime(printedAt)}. The patient is currently admitted.</p>
      </div>

      <table className="discharge-patient-table">
        <tbody>
          <tr>
            <td className="w-1-3"><strong>Name</strong>: {patient.name}</td>
            <td className="w-1-3"><strong>Age / sex</strong>: {patient.age ?? NOT_RECORDED}{patient.age != null ? 'Y' : ''} / {patient.sex || NOT_RECORDED}</td>
            <td className="w-1-3"><strong>Ward</strong>: {[patient.ward, patient.bed ? `Bed ${patient.bed}` : null].filter(Boolean).join(', ') || NOT_RECORDED}</td>
          </tr>
          <tr>
            <td className="w-1-3"><strong>MRN</strong>: {patient.mrn || NOT_RECORDED}</td>
            <td className="w-1-3"><strong>Admission ID</strong>: {patient.admission_id || NOT_RECORDED}</td>
            <td className="w-1-3"><strong>Attending doctor</strong>: {patient.attending_doctor_name || NOT_RECORDED}</td>
          </tr>
          <tr>
            <td className="w-1-3" colSpan={3}><strong>Admitted</strong>: {recordedDate(patient.admission_date)}</td>
          </tr>
        </tbody>
      </table>

      <div className="discharge-section">
        <p><strong>DIAGNOSIS:</strong> {patient.diagnosis || NOT_DOCUMENTED}</p>
        <p><strong>ALLERGIES:</strong>{' '}
          {allergies === null
            ? 'The allergy list could not be read. Check the chart.'
            : allergies.length === 0 ? 'No allergies recorded' : allergies.map(allergyLine).join('; ')}
        </p>
        <p><strong>ACTIVE PROBLEMS:</strong>{' '}
          {problems.length === 0 ? 'None recorded' : problems.map(p => [
            p.problem,
            p.icd10_code ? `(${p.icd10_code})` : null,
            p.onset_date ? `since ${recordedDate(p.onset_date, false)}` : null,
          ].filter(Boolean).join(' ')).join('; ')}
        </p>
        <p><strong>COMORBIDITIES:</strong> {comorbidities.length ? comorbidities.join(', ') : 'None recorded'}</p>
      </div>

      <div className="discharge-section">
        <p><strong>LATEST RECORDED VITALS:</strong> {vitalsLine(latestVitals)}</p>
      </div>

      {showMedicines && (
        <div className="discharge-section">
          <p><strong>ACTIVE MEDICINES:</strong></p>
          {medications.length > 0 ? (
            <ol className="discharge-meds-list" style={{ paddingLeft: '24px', margin: '8px 0', lineHeight: '1.6' }}>
              {medications.map((m, i) => (
                <li key={m.id ?? i}>
                  {medicationLine(m)}
                  {m.unverified ? ' Awaiting pharmacist verification.' : ''}
                </li>
              ))}
            </ol>
          ) : <p>No active medicines recorded.</p>}
        </div>
      )}

      <div className="discharge-section">
        <p><strong>SIGNED NOTES:</strong></p>
        {notes.length === 0 ? <p>No signed notes recorded.</p> : notes.map((n, i) => (
          <div key={i} style={{ marginBottom: '10px' }}>
            <p><strong>{n.title}</strong>, {recordedDate(n.at ?? null)}{n.author ? `, ${n.author}` : ''}</p>
            <p style={{ whiteSpace: 'pre-wrap' }}>{n.text}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
