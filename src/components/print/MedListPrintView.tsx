import { useEffect } from 'react'
import { formatClinicalDateTime } from '../../lib/datetime'
import { readableMedNotes } from './medNotes'

/**
 * Medication list print view: active and stopped drugs in separate sections,
 * the allergy band, and AI-extracted orders labelled as unverified.
 *
 * Print-first, portaled by the caller into document.body (see VigilPanel), so
 * the shared @media print rules in src/print.css strip the dark app shell and
 * reveal only this .medlist-print-layout subtree.
 *
 * Unlike the Handoff/Discharge views (whose parent fires window.print via a
 * double-rAF effect), this component owns its own print lifecycle: it fires
 * window.print() once committed and resets the caller state through
 * onAfterPrint, wired to the browser's afterprint event. This mirrors the
 * MedListPrintView contract documented in VigilPanel and avoids a double
 * print dialog.
 */

interface Med {
  drug_name: string
  dosage: string
  route: string
  frequency: string
  status: string
  notes?: string | null
  /** True for an AI-extracted or agentic order no pharmacist has verified. */
  unverified?: boolean
}

interface PrintAllergy {
  substance: string
  reaction?: string | null
  severity?: string | null
}

interface MedListPrintViewProps {
  patientName: string
  mrn?: string
  bed?: string
  medications: Med[]
  /** The charted allergy list. `null` means it could not be read, which is
   *  printed as such, never as "none". */
  allergies?: PrintAllergy[] | null
  /** The hospital's name from site configuration. Nothing is printed rather
   *  than an invented one. */
  siteName?: string | null
  onAfterPrint: () => void
}

/** Statuses that are on the chart and may be given. Everything else (stopped,
 *  discontinued, rejected) prints under "Stopped", never as an active order. */
const ACTIVE_STATUSES = new Set(['active', 'prn'])

export const UNVERIFIED_LABEL = 'AI-extracted, awaiting pharmacist verification'

function MedTable({ meds }: { meds: Med[] }) {
  return (
    <table className="medlist-table">
      <thead>
        <tr>
          <th>Medication</th>
          <th>Dosage</th>
          <th>Route</th>
          <th>Frequency</th>
          <th>Status</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {meds.map((med, idx) => (
          <tr key={`${med.drug_name}-${idx}`}>
            <td>
              <strong>{med.drug_name}</strong>
              {/* Never printed like a prescription: nobody has checked it. */}
              {med.unverified && <div><em>{UNVERIFIED_LABEL}</em></div>}
            </td>
            <td>{med.dosage}</td>
            <td>{med.route}</td>
            <td>{med.frequency}</td>
            <td>{med.status}</td>
            <td>{readableMedNotes(med.notes) || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function MedListPrintView({
  patientName,
  mrn,
  bed,
  medications,
  allergies = null,
  siteName,
  onAfterPrint,
}: MedListPrintViewProps) {
  const generatedAt = formatClinicalDateTime(new Date())
  const active = medications.filter(m => ACTIVE_STATUSES.has((m.status || '').toLowerCase()))
  const stopped = medications.filter(m => !ACTIVE_STATUSES.has((m.status || '').toLowerCase()))

  useEffect(() => {
    // afterprint fires whether the user prints or cancels the dialog; use it to
    // tear the portal back down exactly once.
    const handleAfterPrint = () => onAfterPrint()
    window.addEventListener('afterprint', handleAfterPrint)

    // Wait for the portal content to commit + paint before opening the dialog.
    let cancelled = false
    const rafId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return
        window.print()
      })
    })

    return () => {
      cancelled = true
      cancelAnimationFrame(rafId)
      window.removeEventListener('afterprint', handleAfterPrint)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="medlist-print-layout">
      {/* Header */}
      <div className="medlist-header">
        <div className="medlist-header-branding">
          {siteName ? <h1>{siteName}</h1> : null}
          <p>Medication list</p>
        </div>
        <div className="medlist-header-meta">
          <p><strong>Generated:</strong> {generatedAt}</p>
          <p><strong>Status:</strong> CONFIDENTIAL MEDICAL RECORD</p>
        </div>
      </div>

      <hr className="medlist-divider" />

      {/* Patient identity band */}
      <div className="medlist-section">
        <h2 className="medlist-patient-name">{patientName}</h2>
        <p className="medlist-patient-demo">
          {mrn ? <>MRN: {mrn}</> : null}
          {mrn && bed ? ' · ' : null}
          {bed ? <>Bed: {bed}</> : null}
        </p>
      </div>

      {/* Allergy band: on every printed medication list, before the drugs. */}
      <div className="medlist-section">
        <h3 className="medlist-section-title">Allergies</h3>
        {allergies === null ? (
          <p className="medlist-empty">
            The allergy list could not be read. Check the chart before giving any drug.
          </p>
        ) : allergies.length === 0 ? (
          <p className="medlist-empty">No allergies recorded. Confirm with the patient.</p>
        ) : (
          <p>
            {allergies.map((a, i) => (
              <span key={`${a.substance}-${i}`}>
                {i > 0 ? '; ' : ''}
                <strong>{a.substance}</strong>
                {a.severity ? ` (${a.severity})` : ''}
                {a.reaction ? `: ${a.reaction}` : ''}
              </span>
            ))}
          </p>
        )}
      </div>

      <div className="medlist-section">
        <h3 className="medlist-section-title">Active medications</h3>
        {active.length > 0
          ? <MedTable meds={active} />
          : <p className="medlist-empty">No active medications recorded.</p>}
      </div>

      {/* Stopped drugs used to print under "Active Medications". */}
      {stopped.length > 0 && (
        <div className="medlist-section">
          <h3 className="medlist-section-title">Stopped medications (do not give)</h3>
          <MedTable meds={stopped} />
        </div>
      )}

      {/* Signature line */}
      <div className="medlist-footer">
        <div className="medlist-signoff">
          <p>Reviewed / Administered by:</p>
          <div className="medlist-signature-line"></div>
          <p>Date / Time:</p>
          <div className="medlist-signature-line"></div>
        </div>
      </div>
    </div>
  )
}
