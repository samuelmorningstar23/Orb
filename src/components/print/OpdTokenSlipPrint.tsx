/**
 * The OPD token slip (opd-6b): the slip the desk hands the patient after the
 * visit is registered, on an 80 mm receipt roll.
 *
 * It carries the visit the server returned and nothing else: the token in
 * large figures, the clinic, the doctor's name (or that the next free doctor
 * will see them), the room when one is set, the day and time registered, and
 * the patient's name and UHID so the doctor can check the slip against the
 * patient. No date of birth, phone or address.
 *
 * Portalled by the caller into document.body; ./opdPrint.css gives it its own
 * named page. It opens the print dialog once and calls onAfterPrint on
 * `afterprint`.
 */
import type { OpdVisit } from '../../api/frontdesk'
import { formatClinicalDateKey, formatClinicalTime } from '../../lib/datetime'
import './opdPrint.css'
import { usePrintOnce } from './usePrintOnce'

export type TokenSlipVisit = Pick<OpdVisit,
  'token_number' | 'department' | 'doctor_name' | 'visit_date' | 'registered_at'
  | 'patient_name' | 'patient_mrn'> & { room?: string | null }

interface OpdTokenSlipPrintProps {
  visit: TokenSlipVisit
  onAfterPrint: () => void
  /** Tests only: render without opening the print dialog. */
  autoPrint?: boolean
}

export default function OpdTokenSlipPrint({ visit, onAfterPrint, autoPrint = true }: OpdTokenSlipPrintProps) {
  usePrintOnce(onAfterPrint, autoPrint)
  return (
    <div className="opd-print-layout opd-print-layout--slip" data-testid="opd-token-slip-print">
      <section className="opd-token-slip" aria-label={`Token slip ${visit.token_number}`}>
        <div>Outpatient token</div>
        <div className="opd-token-slip__token">{visit.token_number}</div>
        <div><strong>{visit.department}</strong></div>
        <div>{visit.doctor_name ? `Doctor: ${visit.doctor_name}` : 'The next free doctor will see you'}</div>
        {visit.room && <div>Room {visit.room}</div>}
        <hr className="opd-token-slip__rule" />
        <div>{visit.patient_name ?? 'Name not recorded'}</div>
        <div>{visit.patient_mrn ? `UHID ${visit.patient_mrn}` : 'UHID not recorded'}</div>
        <div>
          {formatClinicalDateKey(visit.visit_date)}
          {visit.registered_at ? `, registered ${formatClinicalTime(visit.registered_at)}` : ''}
        </div>
      </section>
    </div>
  )
}
