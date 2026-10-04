import type { DischargeTimelineEntry } from '../api/client';
import { formatClinicalDate, formatClinicalDateTime } from '../lib/datetime';

/* THE PRINTED DISCHARGE SUMMARY RENDERS RECORDED DATA ONLY.
 *
 * Until 2026-10-03 this printed the same prose for every patient: five "No H/o"
 * negatives, a normal examination, "Conservatively Managed", "responded well to
 * medical therapy... hemodynamically stable", "FOR 5 DAYS" and a default "Oral"
 * on every drug, an invented IP number, a named fallback signatory and "Follow
 * up in OPD after 1 week". None of it came from the chart, and it reached
 * patients through the discharge link. A section with nothing recorded now says
 * so; it is never filled in. */

interface Patient {
  mrn?: string | null;
  name: string;
  age?: number | null;
  sex?: string | null;
  ward?: string | null;
  bed?: string | null;
  diagnosis?: string | null;
  admission_date?: string | null;
  attending_doctor_name?: string | null;
  admission_id?: string | null;
}

interface Medication {
  id?: string | number;
  name?: string;
  drug_name?: string;
  dosage?: string | null;
  frequency?: string | null;
  status?: string | null;
  route?: string | null;
  duration?: string | null;
}

export interface PrintAllergy {
  substance: string;
  reaction?: string | null;
  severity?: string | null;
}

interface DischargePrintViewProps {
  patient: Patient;
  /** The discharge medicines: continued, verified orders only. The caller
   *  filters; this view prints what it is given and invents nothing. */
  medications: Medication[];
  /** The stored summary text. Null when it has not been released to the
   *  reader (the patient sees nothing until a clinician has reviewed it). */
  summary?: string | null;
  summaryStatus?: 'draft' | 'reviewed' | null;
  /** Shown in place of the text when it is withheld. */
  summaryMessage?: string | null;
  reviewedByName?: string | null;
  reviewedAt?: string | null;
  dischargedAt?: string | null;
  /** undefined = not supplied, null = the list could not be read. An unread
   *  allergy list is never printed as "no allergies". */
  allergies?: PrintAllergy[] | null;
  comorbidities?: string[];
  /** Medicines left off because a pharmacist has not verified them yet. */
  unverifiedMedicationCount?: number;
  siteName?: string | null;
  timeline?: DischargeTimelineEntry[];
}

const NOT_RECORDED = 'Not recorded';
const NOT_DOCUMENTED = 'Not documented';

function allergyLine(a: PrintAllergy): string {
  const detail = [a.reaction, a.severity && a.severity !== 'unknown' ? a.severity : null]
    .filter(Boolean)
    .join(', ');
  return detail ? `${a.substance} (${detail})` : a.substance;
}

function medicationLine(m: Medication): string {
  const parts = [m.dosage, m.route, m.frequency].filter((p) => p && String(p).trim());
  const prn = m.status === 'prn' ? ' (as needed)' : '';
  return `${parts.join(', ')}${prn}`;
}

export function DischargePrintView({
  patient,
  medications,
  summary,
  summaryStatus,
  summaryMessage,
  reviewedByName,
  reviewedAt,
  dischargedAt,
  allergies,
  comorbidities,
  unverifiedMedicationCount,
  siteName,
  timeline,
}: DischargePrintViewProps) {
  const admitted = formatClinicalDateTime(patient.admission_date) || NOT_RECORDED;
  const discharged = formatClinicalDate(dischargedAt) || NOT_RECORDED;
  const ageSex = [patient.age != null ? `${patient.age}Y` : null, patient.sex || null]
    .filter(Boolean)
    .join(' / ') || NOT_RECORDED;
  const wardBed = [patient.ward, patient.bed ? `Bed ${patient.bed}` : null]
    .filter(Boolean)
    .join(', ') || NOT_RECORDED;
  const isDraft = summaryStatus === 'draft';

  return (
    <div className="discharge-print-layout">
      {/* HEADER: the site's configured name, or nothing at all. */}
      <div className="discharge-title-wrapper">
        {siteName && <p style={{ textAlign: 'center', fontWeight: 600, margin: 0 }}>{siteName}</p>}
        <h1 className="discharge-title" style={{ textDecoration: 'underline', marginBottom: '8px', textAlign: 'center' }}>DISCHARGE SUMMARY</h1>
      </div>

      <table className="discharge-patient-table">
        <tbody>
          <tr>
            <td className="w-1-3"><strong>Name</strong>: {patient.name}</td>
            <td className="w-1-3"><strong>Age / sex</strong>: {ageSex}</td>
            <td className="w-1-3"><strong>Ward</strong>: {wardBed}</td>
          </tr>
          <tr>
            <td className="w-1-3"><strong>MRN</strong>: {patient.mrn || NOT_RECORDED}</td>
            <td className="w-1-3"><strong>Admission ID</strong>: {patient.admission_id || NOT_RECORDED}</td>
            <td className="w-1-3"><strong>Date of discharge</strong>: {discharged}</td>
          </tr>
          <tr>
            <td className="w-1-3" colSpan={2}><strong>Date of admission</strong>: {admitted}</td>
            <td className="w-1-3"><strong>Attending doctor</strong>: {patient.attending_doctor_name || NOT_RECORDED}</td>
          </tr>
        </tbody>
      </table>

      <div className="discharge-section">
        <p><strong>DIAGNOSIS:</strong> {patient.diagnosis || NOT_DOCUMENTED}</p>
      </div>

      <div className="discharge-section">
        <p><strong>ALLERGIES:</strong>{' '}
          {allergies === null
            ? 'The allergy list could not be read. Check with the hospital.'
            : allergies === undefined
              ? NOT_DOCUMENTED
              : allergies.length === 0
                ? 'No allergies recorded'
                : allergies.map(allergyLine).join('; ')}
        </p>
        <p><strong>COMORBIDITIES:</strong>{' '}
          {comorbidities && comorbidities.length > 0 ? comorbidities.join(', ') : 'None recorded'}
        </p>
      </div>

      <div className="discharge-section">
        <p><strong>SUMMARY OF THE STAY:</strong></p>
        {summary ? (
          <>
            {isDraft && (
              <p
                data-testid="discharge-draft-label"
                style={{ fontWeight: 700, border: '1px solid currentColor', padding: '4px 8px', display: 'inline-block' }}
              >
                AI-drafted, not reviewed by a clinician
              </p>
            )}
            <p style={{ whiteSpace: 'pre-wrap' }}>{summary}</p>
            {summaryStatus === 'reviewed' && (
              <p><em>Reviewed by {reviewedByName || NOT_RECORDED}{reviewedAt ? ` on ${formatClinicalDateTime(reviewedAt)}` : ''}</em></p>
            )}
          </>
        ) : (
          <p>{summaryMessage || NOT_DOCUMENTED}</p>
        )}
      </div>

      <div className="discharge-section">
        <p><strong>DISCHARGE MEDICINES:</strong></p>
        {medications.length > 0 ? (
          <ol className="discharge-meds-list" style={{ paddingLeft: '24px', margin: '8px 0', lineHeight: '1.6' }}>
            {medications.map((med, idx) => (
              <li key={med.id ?? idx}>
                <strong>{med.drug_name || med.name}</strong>
                {medicationLine(med) ? `: ${medicationLine(med)}` : ''}
                {`. Duration: ${med.duration || 'not recorded'}`}
              </li>
            ))}
          </ol>
        ) : (
          <p>No discharge medicines recorded.</p>
        )}
        {!!unverifiedMedicationCount && unverifiedMedicationCount > 0 && (
          <p>
            {unverifiedMedicationCount} medicine{unverifiedMedicationCount === 1 ? ' is' : 's are'} awaiting
            pharmacist verification and {unverifiedMedicationCount === 1 ? 'is' : 'are'} not listed. Ask the hospital before taking {unverifiedMedicationCount === 1 ? 'it' : 'them'}.
          </p>
        )}
      </div>

      {timeline && timeline.length > 0 && (
        <div className="discharge-section" style={{ marginTop: '24px' }}>
          <p style={{ textDecoration: 'underline', fontWeight: 'bold' }}>HOSPITAL COURSE TIMELINE</p>
          <table style={{ width: '100%', borderCollapse: 'collapse', marginTop: '8px', border: '1px solid #111', fontSize: '10pt' }}>
            <thead>
              <tr>
                <th style={{ border: '1px solid #111', padding: '6px 10px', textAlign: 'left', width: '20%' }}>Date/Time</th>
                <th style={{ border: '1px solid #111', padding: '6px 10px', textAlign: 'left', width: '25%' }}>Event type</th>
                <th style={{ border: '1px solid #111', padding: '6px 10px', textAlign: 'left' }}>Details</th>
              </tr>
            </thead>
            <tbody>
              {timeline.map((event, i) => (
                <tr key={i}>
                  <td style={{ border: '1px solid #111', padding: '6px 10px' }}>{event.timestamp ? formatClinicalDateTime(event.timestamp) : ''}</td>
                  <td style={{ border: '1px solid #111', padding: '6px 10px' }}><strong>{event.title}</strong></td>
                  <td style={{ border: '1px solid #111', padding: '6px 10px' }}>{event.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="discharge-footer">
        <div className="discharge-signature-block">
          <p><strong>{summaryStatus === 'reviewed' ? 'REVIEWED BY' : 'ATTENDING DOCTOR'}</strong></p>
          <p style={{ marginTop: '30px', fontWeight: 600 }}>
            {(summaryStatus === 'reviewed' ? reviewedByName : patient.attending_doctor_name) || NOT_RECORDED}
          </p>
        </div>
      </div>
    </div>
  );
}
