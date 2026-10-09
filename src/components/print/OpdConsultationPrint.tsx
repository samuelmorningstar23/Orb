/**
 * The outpatient consultation print and the investigation slip (opd-11).
 *
 * Prints exactly what GET /api/opd/visits/{visit_id}/print returned, which is
 * what the record holds: the patient's name, UHID, age and sex; the signed
 * note (complaint, history, examination, the vitals as typed with the time
 * they were measured, diagnoses, plan, advice, follow-up, referral, addenda)
 * or the investigations ordered on the visit; and the doctor's name with
 * their registration number. Nothing is written for the print: a section the
 * note leaves empty is left out, and a missing registration number says so.
 *
 * No date of birth, phone, address or allergy list, no score, band or colour
 * (rulings OPD-S3 and OPD-S6). Reading the print is audited by the server.
 *
 * Portalled by the caller into document.body; ./opdPrint.css shows it alone
 * on an A4 page. It opens the print dialog once and calls onAfterPrint on
 * `afterprint`.
 */
import type { VisitPrint } from '../../api/opd'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import { opdVitalsLine } from '../../pages/panels/opd/opdVitals'
import './opdPrint.css'
import { usePrintOnce } from './usePrintOnce'

interface OpdConsultationPrintProps {
  print: VisitPrint
  onAfterPrint: () => void
  /** Tests only: render without opening the print dialog. */
  autoPrint?: boolean
}

const SEX_WORD: Record<string, string> = { F: 'Female', M: 'Male', O: 'Other' }
const PRIORITY_WORD: Record<string, string> = { routine: 'Routine', urgent: 'Urgent', stat: 'STAT' }
const TYPE_WORD: Record<string, string> = { lab: 'Laboratory', imaging: 'Imaging' }

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="opd-print-sheet__section">
      <h2 className="opd-print-sheet__label">{label}</h2>
      {children}
    </section>
  )
}

function Text({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value || !value.trim()) return null
  return (
    <Section label={label}>
      <p className="opd-print-sheet__text">{value}</p>
    </Section>
  )
}

export default function OpdConsultationPrint({ print, onAfterPrint, autoPrint = true }: OpdConsultationPrintProps) {
  usePrintOnce(onAfterPrint, autoPrint)
  const { patient, visit, consultation: note } = print
  const consultation = print.kind === 'consultation'
  const title = consultation ? 'Outpatient consultation' : 'Investigation request'
  const vitals = note?.vitals ? opdVitalsLine(note.vitals.values) : ''
  const followUp = [note?.follow_up_date ? formatClinicalDateKey(note.follow_up_date) : null,
    note?.follow_up_note || null].filter(Boolean).join('. ')

  return (
    <div className="opd-print-layout opd-print-layout--sheet" data-testid="opd-consultation-print">
      <header className="opd-print-sheet__head">
        <div>
          <h1 className="opd-print-sheet__title">{title}</h1>
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

      {consultation && note && (
        <>
          <Text label="Complaint" value={note.chief_complaint} />
          <Text label="History" value={note.history} />
          <Text label="Examination" value={note.examination} />
          {vitals && (
            <Section label="Vitals">
              <p className="opd-print-sheet__text">
                {note.vitals?.measured_at ? `Measured ${formatClinicalDateTime(note.vitals.measured_at)}: ` : ''}
                {vitals}
              </p>
            </Section>
          )}
          {note.diagnoses.length > 0 && (
            <Section label="Diagnosis">
              <ul className="opd-print-sheet__list">
                {note.diagnoses.map((d, i) => (
                  <li key={`${d.text}-${i}`}>
                    {d.text}{d.code ? ` (${[d.code_system, d.code].filter(Boolean).join(' ')})` : ''}
                  </li>
                ))}
              </ul>
            </Section>
          )}
          <Text label="Plan" value={note.plan} />
          <Text label="Advice" value={note.advice} />
          <Text label="Follow-up" value={followUp} />
          <Text label="Referred to" value={note.referral_department} />
        </>
      )}

      {print.investigations.length > 0 && (
        <Section label={consultation ? 'Investigations ordered' : 'Tests requested'}>
          <table className="opd-print-sheet__table">
            <thead>
              <tr>
                <th>Test</th>
                <th>Kind</th>
                <th>Priority</th>
                <th>Ordered</th>
              </tr>
            </thead>
            <tbody>
              {print.investigations.map((o, i) => (
                <tr key={`${o.description}-${i}`}>
                  <td>{o.description}</td>
                  <td>{TYPE_WORD[o.order_type] ?? o.order_type}</td>
                  <td>{PRIORITY_WORD[o.priority] ?? o.priority}</td>
                  <td>{formatClinicalDateTime(o.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}

      {consultation && note && note.addenda.length > 0 && (
        <Section label="Addenda">
          <ul className="opd-print-sheet__list">
            {note.addenda.map((a, i) => (
              <li key={`${a.created_at}-${i}`}>
                {formatClinicalDateTime(a.created_at)}{a.author_name ? `, ${a.author_name}` : ''}: {a.body}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <footer className="opd-print-sheet__foot">
        {consultation && note?.signed_at && (
          <p className="opd-print-sheet__text">Signed {formatClinicalDateTime(note.signed_at)}</p>
        )}
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
