/**
 * The UHID card (opd-6b): the patient's hospital number on a CR80 card,
 * 86 x 54 mm, printed by the browser.
 *
 * It carries what GET /api/registration/patients/{id}/card returned, which
 * the server audits as a card print: the name, the UHID in digits a person
 * can read and as a Code 128 barcode (src/lib/code128, drawn as SVG, no new
 * dependency), age and sex, and the day the patient was registered. Never a
 * date of birth, phone or address.
 *
 * Portalled by the caller into document.body; ./opdPrint.css gives it its own
 * named page. It opens the print dialog once and calls onAfterPrint on
 * `afterprint`.
 */
import { useMemo } from 'react'

import type { UhidCard } from '../../api/registration'
import { formatClinicalDateKey } from '../../lib/datetime'
import { code128Bars, code128TotalModules, encodeCode128 } from '../../lib/code128'
import './opdPrint.css'
import { usePrintOnce } from './usePrintOnce'

interface UhidCardPrintProps {
  card: UhidCard
  onAfterPrint: () => void
  /** Tests only: render without opening the print dialog. */
  autoPrint?: boolean
}

const SEX_WORD: Record<string, string> = { F: 'Female', M: 'Male', O: 'Other' }

export default function UhidCardPrint({ card, onAfterPrint, autoPrint = true }: UhidCardPrintProps) {
  usePrintOnce(onAfterPrint, autoPrint)
  const encoding = useMemo(() => {
    if (!card.uhid) return null
    try {
      return encodeCode128(card.uhid)
    } catch {
      return null
    }
  }, [card.uhid])
  const ageSex = [card.age != null ? `${card.age} y` : null,
    card.sex ? (SEX_WORD[card.sex] ?? card.sex) : null].filter(Boolean).join(', ')

  return (
    <div className="opd-print-layout opd-print-layout--card" data-testid="uhid-card-print">
      <section className="uhid-card" aria-label={`UHID card for ${card.name}`}>
        <div className="uhid-card__name">{card.name}</div>
        {ageSex && <div>{ageSex}</div>}
        {encoding ? (
          <svg
            className="uhid-card__barcode"
            viewBox={`0 0 ${code128TotalModules(encoding)} 40`}
            preserveAspectRatio="none"
            role="img"
            aria-label={`Barcode for UHID ${card.uhid}`}
          >
            {code128Bars(encoding).map(bar => (
              <rect key={bar.x} x={bar.x} y={0} width={bar.width} height={40} fill="currentColor" />
            ))}
          </svg>
        ) : (
          <div>{card.uhid ? 'Barcode could not be drawn. Use the number.' : 'No UHID on file.'}</div>
        )}
        <div className="uhid-card__number">{card.uhid ? `UHID ${card.uhid}` : 'UHID not recorded'}</div>
        {card.registered_on && <div>Registered {formatClinicalDateKey(card.registered_on)}</div>}
      </section>
    </div>
  )
}
