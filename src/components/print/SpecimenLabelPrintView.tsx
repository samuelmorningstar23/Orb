import { useEffect, useMemo } from 'react'

import { formatAccession, type SpecimenLabel } from '../../api/lab/specimens'
import {
  code128Bars,
  code128TotalModules,
  encodeCode128,
  type Code128Encoding,
} from '../../lib/code128'

/**
 * Specimen labels (lis-8, ruling LIS-R15): one label per tube, printed by the
 * browser on a thermal label printer, 50 x 25 mm unless the site says
 * otherwise.
 *
 * Each label carries what the bench and the bedside check: the patient's
 * name and UHID, the accession as a Code 128 barcode and in digits a person
 * can read, the container and the short names of the tests in the tube. STAT
 * is printed in words. A reprint says so, so a second label in circulation is
 * never mistaken for the first.
 *
 * Print-first, portaled by the caller into document.body; src/print.css hides
 * the layout on screen and gives it its own page size (the named page
 * `specimen-label`). Owns its print lifecycle like MedListPrintView: it opens
 * the print dialog once committed and calls onAfterPrint on `afterprint`.
 *
 * The labels it receives were released by the server only after the two
 * identifiers matched, one patient at a time; this view prints exactly those.
 */

interface SpecimenLabelPrintViewProps {
  labels: SpecimenLabel[]
  onAfterPrint: () => void
  /** Label size in millimetres. The default is the 50 x 25 mm label of ruling
   *  LIS-R15; another size adds its own page rule. */
  widthMm?: number
  heightMm?: number
  /** Tests only: render without opening the print dialog. */
  autoPrint?: boolean
}

const DEFAULT_WIDTH_MM = 50
const DEFAULT_HEIGHT_MM = 25

function Barcode({ encoding, accession }: { encoding: Code128Encoding; accession: string }) {
  const total = code128TotalModules(encoding)
  return (
    <svg
      className="specimen-label__barcode"
      viewBox={`0 0 ${total} 40`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Barcode for accession ${accession}`}
      data-modules={total}
    >
      {code128Bars(encoding).map(bar => (
        <rect key={bar.x} x={bar.x} y={0} width={bar.width} height={40} fill="currentColor" />
      ))}
    </svg>
  )
}

function Label({ label }: { label: SpecimenLabel }) {
  const encoding = useMemo(() => {
    try {
      return encodeCode128(label.specimen_id)
    } catch {
      return null
    }
  }, [label.specimen_id])
  const where = [label.patient.ward, label.patient.bed ? `bed ${label.patient.bed}` : null]
    .filter(Boolean).join(', ')
  const container = [label.container, label.specimen_display].filter(Boolean).join(', ')
  return (
    <section className="specimen-label" aria-label={`Label for ${label.patient.name}`}>
      <div className="specimen-label__top">
        <strong className="specimen-label__name">{label.patient.name}</strong>
        {label.priority === 'stat' && <span className="specimen-label__stat">STAT</span>}
        {label.priority === 'urgent' && <span className="specimen-label__stat">Urgent</span>}
      </div>
      <div className="specimen-label__line">
        UHID {label.patient.mrn || 'not recorded'}{where ? ` · ${where}` : ''}
      </div>
      {encoding ? (
        <Barcode encoding={encoding} accession={label.specimen_id} />
      ) : (
        <div className="specimen-label__line">Barcode could not be drawn. Use the number.</div>
      )}
      <div className="specimen-label__accession">{formatAccession(label.specimen_id)}</div>
      <div className="specimen-label__line">{container}</div>
      <div className="specimen-label__tests">{label.tests.join(', ')}</div>
      {label.print_number > 1 && (
        <div className="specimen-label__line">Reprint {label.print_number}</div>
      )}
    </section>
  )
}

export default function SpecimenLabelPrintView({
  labels,
  onAfterPrint,
  widthMm = DEFAULT_WIDTH_MM,
  heightMm = DEFAULT_HEIGHT_MM,
  autoPrint = true,
}: SpecimenLabelPrintViewProps) {
  useEffect(() => {
    if (!autoPrint) return undefined
    const handleAfterPrint = () => onAfterPrint()
    window.addEventListener('afterprint', handleAfterPrint)
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

  const customSize = widthMm !== DEFAULT_WIDTH_MM || heightMm !== DEFAULT_HEIGHT_MM
  return (
    <div className="specimen-label-print-layout">
      {customSize && (
        <style>{`@page specimen-label { size: ${widthMm}mm ${heightMm}mm; margin: 0; }
@media print { .specimen-label { width: ${widthMm}mm; height: ${heightMm}mm; } }`}</style>
      )}
      {labels.map(label => <Label key={label.specimen_id} label={label} />)}
    </div>
  )
}
