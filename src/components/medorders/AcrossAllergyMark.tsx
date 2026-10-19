import { givenAcrossAllergy, type AdministrationRecord } from '../../api/client'
import { formatClinicalDateTime } from '../../lib/datetime'
import './MedOrderList.css'

/* ═══════════════════════════════════════════════════════════════════════════
   A DOSE GIVEN ACROSS A CHARTED ALLERGY, MARKED (W9-D)

   NEVER SILENT. The chart's old medication table marked the last dose given
   across a charted allergy with an 'Across charted allergy' chip and the
   written reason; the order list and each order's history keep marking it,
   so a colleague reading the chart later never has to go to the audit log to
   find out that the interlock was cleared, or why.

   The decision is givenAcrossAllergy() from api/client and nothing else: one
   derivation for every surface that shows an administration. This renders
   nothing for any other record (a clean dose, a dose not given, a record the
   chart has not read).

   Danger text with a danger border on the chip, no fill and no opacity. The
   reason is printed as well as on hover, and the dose's time is said, so a
   mark beside a later 'not given' line is never read as that line's.
   ═══════════════════════════════════════════════════════════════════════════ */

interface Props {
  record: AdministrationRecord | null | undefined
}

export default function AcrossAllergyMark({ record }: Props) {
  if (!record || !givenAcrossAllergy(record)) return null
  const override = record.allergy_check?.override
  const reason = (override?.reason ?? '').trim()
  const when = formatClinicalDateTime(record.administered_at)
  return (
    <span className="medorder-allergy-mark">
      <span
        className="medorder-chip medorder-chip--danger"
        title={`Given despite a charted allergy: ${reason || 'reason recorded in the audit trail'}`}
      >
        Across charted allergy
      </span>
      <span>
        Dose given despite a charted allergy{when ? ` at ${when}` : ''}.
        {' '}{override?.carried ? 'Reason carried from an earlier dose' : 'Reason'}:
        {' '}{reason || 'recorded in the audit trail'}
      </span>
    </span>
  )
}
