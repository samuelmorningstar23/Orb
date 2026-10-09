/**
 * The printed outpatient prescription (opd-12).
 *
 * Prints exactly what GET /api/opd/visits/{visit_id}/prescriptions/print
 * returned: the patient's name, UHID, age and sex; what the allergy review
 * said and when (never an allergen: a visit-addressed answer names none, so
 * the sheet says where the list is); the signed note's diagnoses when the note
 * is signed; each live line with its dose, route, how often, duration,
 * quantity and the instructions the doctor wrote; and the doctor's name with
 * their registration number. Nothing is written for the print: an empty field
 * is left blank, and a missing registration number says so.
 *
 * No date of birth, phone, address, score, band or colour. Reading the print
 * is audited by the server, naming the lines printed.
 *
 * Portalled by the caller into document.body; ./opdPrint.css shows it alone
 * on an A4 page. It opens the print dialog once and calls onAfterPrint on
 * `afterprint`.
 */
import type { PrescriptionPrint } from '../../api/opdPrescriptions'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import './opdPrint.css'
import { usePrintOnce } from './usePrintOnce'

interface OpdPrescriptionPrintProps {
  print: PrescriptionPrint
  onAfterPrint: () => void
  /** Tests only: render without opening the print dialog. */
  autoPrint?: boolean
}

const SEX_WORD: Record<string, string> = { F: 'Female', M: 'Male', O: 'Other' }

function howOften(line: PrescriptionPrint['prescriptions'][number]): string {
  if (!line.is_prn) return line.frequency ?? ''
  const when = line.prn_indication ? `When needed for ${line.prn_indication}` : 'When needed'
  return line.frequency ? `${when} (${line.frequency})` : when
}

function allergyLine(review: PrescriptionPrint['allergy_review']): string {
  if (!review) return 'No allergy review on file.'
  const asked = `asked ${formatClinicalDateTime(review.assessed_at)}`
  if (review.status === 'allergies_recorded') {
    return `${review.status_label}, ${asked}. The allergy list is in the patient's record.`
  }
  return `${review.status_label}, ${asked}.`
}

export default function OpdPrescriptionPrint({ print, onAfterPrint, autoPrint = true }: OpdPrescriptionPrintProps) {
  usePrintOnce(onAfterPrint, autoPrint)
  const { patient, visit } = print

  return (
    <div className="opd-print-layout opd-print-layout--sheet" data-testid="opd-prescription-print">
      <header className="opd-print-sheet__head">
        <div>
          <h1 className="opd-print-sheet__title">Prescription</h1>
          <p className="opd-print-sheet__text">
            {[visit.department, visit.visit_date ? formatClinicalDateKey(visit.visit_date) : null,
              visit.token_number != null ? `Token ${visit.token_number}` : null]
              .filter(Boolean).join(' · ')}
          </p>
        </div>
        <p className="opd-print-sheet__meta">Printed {formatClinicalDateTime(print.printed_at)}</p>
      </header>

      <dl className="opd-print-sheet__patient" aria-label="Patient">
        <div>
          <dt>Name</dt>
          <dd>{patient.name || 'Not recorded'}</dd>
        </div>
        <div>
          <dt>UHID</dt>
          <dd>{patient.uhid || 'Not recorded'}</dd>
        </div>
        <div>
          <dt>Age and sex</dt>
          <dd>
            {[patient.age != null ? `${patient.age} y` : null,
              patient.sex ? (SEX_WORD[patient.sex] ?? patient.sex) : null]
              .filter(Boolean).join(', ') || 'Not recorded'}
          </dd>
        </div>
      </dl>

      <section className="opd-print-sheet__section">
        <h2 className="opd-print-sheet__label">Allergy review</h2>
        <p className="opd-print-sheet__text">{allergyLine(print.allergy_review)}</p>
      </section>

      {print.diagnoses.length > 0 && (
        <section className="opd-print-sheet__section">
          <h2 className="opd-print-sheet__label">Diagnosis</h2>
          <ul className="opd-print-sheet__list">
            {print.diagnoses.map((d, i) => <li key={`${d}-${i}`}>{d}</li>)}
          </ul>
        </section>
      )}

      <section className="opd-print-sheet__section">
        <h2 className="opd-print-sheet__label">Medicines</h2>
        <table className="opd-print-sheet__table">
          <thead>
            <tr>
              <th>Medicine</th>
              <th>Dose</th>
              <th>Route</th>
              <th>How often</th>
              <th>Duration</th>
              <th>Quantity</th>
              <th>Instructions</th>
            </tr>
          </thead>
          <tbody>
            {print.prescriptions.map((p, i) => (
              <tr key={`${p.drug_name}-${i}`}>
                <td>{p.drug_name}</td>
                <td>{p.dose ?? ''}</td>
                <td>{p.route ?? ''}</td>
                <td>{howOften(p)}</td>
                <td>{p.duration_days ? `${p.duration_days} ${p.duration_days === 1 ? 'day' : 'days'}` : ''}</td>
                <td>{p.quantity ?? ''}</td>
                <td>{p.instructions ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <footer className="opd-print-sheet__foot">
        {print.doctors.map((d, i) => (
          <div key={`${d.name}-${i}`}>
            <div className="opd-print-sheet__sign" aria-hidden="true" />
            <p className="opd-print-sheet__text"><strong>{d.name || 'Name not recorded'}</strong></p>
            <p className="opd-print-sheet__text">
              {d.registration_number
                ? `Registration number ${d.registration_number}`
                : 'Registration number not on file'}
            </p>
          </div>
        ))}
      </footer>
    </div>
  )
}
