/**
 * Specimen labels (lis-8). A label is what the bench reads to decide whose
 * blood is in the tube: it must carry the name, the UHID, the accession both
 * as a barcode that decodes to the same number and in digits a person can
 * read, the container, and the short names of the tests.
 */
import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SpecimenLabel } from '../../api/lab/specimens'
import { CODE128_PATTERNS, QUIET_ZONE_MODULES, encodeCode128 } from '../../lib/code128'
import SpecimenLabelPrintView from './SpecimenLabelPrintView'

const LABEL: SpecimenLabel = {
  specimen_id: '261004000127',
  patient: { name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A', ward: 'GM-1' },
  specimen_display: 'Serum',
  container: 'Gold top (SST)',
  container_colour_name: 'gold',
  priority: 'stat',
  tests: ['Sodium', 'Potassium', 'Creatinine'],
  print_number: 1,
}

let print: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  print = vi.spyOn(window, 'print').mockImplementation(() => {})
})
afterEach(() => {
  print.mockRestore()
})

function bars(svg: SVGElement) {
  return Array.from(svg.querySelectorAll('rect')).map(r => ({
    x: Number(r.getAttribute('x')),
    width: Number(r.getAttribute('width')),
  }))
}

/** Rebuild the bar and space widths from the drawn rectangles and read them
 *  back into symbol values, as a scanner would. */
function decodeDrawn(svg: SVGElement): number[] {
  const drawn = bars(svg)
  const widths: number[] = []
  drawn.forEach((bar, i) => {
    if (i > 0) {
      const prev = drawn[i - 1]
      widths.push(bar.x - (prev.x + prev.width))
    }
    widths.push(bar.width)
  })
  const lookup = new Map(CODE128_PATTERNS.map((p, value) => [p, value]))
  const values: number[] = []
  let i = 0
  while (i < widths.length) {
    const take = widths.length - i === 7 ? 7 : 6
    values.push(lookup.get(widths.slice(i, i + take).join('')) ?? -1)
    i += take
  }
  return values
}

describe('SpecimenLabelPrintView', () => {
  it('shows the name, UHID, readable accession, container and short names', () => {
    render(<SpecimenLabelPrintView labels={[LABEL]} onAfterPrint={() => {}} autoPrint={false} />)
    const label = screen.getByRole('region', { name: 'Label for Asha Menon' })
    expect(within(label).getByText('Asha Menon')).toBeTruthy()
    expect(within(label).getByText(/UHID MRN-0042/).textContent).toBe('UHID MRN-0042 · GM-1, bed 4A')
    expect(within(label).getByText('261004 00012 7')).toBeTruthy()
    expect(within(label).getByText('Gold top (SST), Serum')).toBeTruthy()
    expect(within(label).getByText('Sodium, Potassium, Creatinine')).toBeTruthy()
    expect(within(label).getByText('STAT')).toBeTruthy()
    expect(within(label).queryByText(/Reprint/)).toBeNull()
  })

  it('draws a barcode that decodes to the accession', () => {
    render(<SpecimenLabelPrintView labels={[LABEL]} onAfterPrint={() => {}} autoPrint={false} />)
    const svg = screen.getByRole('img', { name: 'Barcode for accession 261004000127' })
    const encoding = encodeCode128('261004000127')
    expect(decodeDrawn(svg as unknown as SVGElement)).toEqual(encoding.codes)
    expect(bars(svg as unknown as SVGElement)[0].x).toBe(QUIET_ZONE_MODULES)
  })

  it('prints one label per tube and marks a reprint', () => {
    const second = { ...LABEL, specimen_id: '261004000135', priority: 'routine' as const,
      print_number: 2, tests: ['CBC'] }
    const { container } = render(
      <SpecimenLabelPrintView labels={[LABEL, second]} onAfterPrint={() => {}} autoPrint={false} />)
    expect(container.querySelectorAll('.specimen-label')).toHaveLength(2)
    expect(screen.getByText('Reprint 2')).toBeTruthy()
    expect(screen.getAllByText('STAT')).toHaveLength(1)
  })

  it('opens the print dialog once and hands back on afterprint', async () => {
    const done = vi.fn()
    render(<SpecimenLabelPrintView labels={[LABEL]} onAfterPrint={done} />)
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(print).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event('afterprint'))
    expect(done).toHaveBeenCalledTimes(1)
  })

  it('adds a page rule only for a label size other than 50 x 25 mm', () => {
    const { container, rerender } = render(
      <SpecimenLabelPrintView labels={[LABEL]} onAfterPrint={() => {}} autoPrint={false} />)
    expect(container.querySelector('style')).toBeNull()
    rerender(<SpecimenLabelPrintView labels={[LABEL]} onAfterPrint={() => {}} autoPrint={false}
      widthMm={38} heightMm={25} />)
    expect(container.querySelector('style')?.textContent).toContain('size: 38mm 25mm')
  })
})
