/**
 * The client half of the money contract.
 *
 * TWO JOBS, and the second is the important one:
 *
 * 1. The rules hold here — no float reaches a stored value, exempt items are
 *    never taxed, Indian grouping, refunds mirror charges, invoice = sum of
 *    rounded lines.
 * 2. THE CLIENT AGREES WITH THE SERVER. Every case in PARITY_CASES is asserted
 *    against a value produced by server/services/money.py and pasted here.
 *    A client that rounds differently from the server draws a screen that
 *    disagrees with the bill it just created, and the clerk believes the screen.
 *    If one of those goes red, the two files have drifted — fix them together.
 */
import { describe, expect, it } from 'vitest'
import {
  BASIS_POINTS,
  GST_EXEMPT,
  GST_NIL_RATED,
  GST_TAXABLE,
  GST_ZERO_RATED,
  MoneyError,
  QUANTITY_SCALE,
  applyBasisPoints,
  assertPaise,
  computeGst,
  computeInvoice,
  computeLine,
  divRoundHalfUp,
  formatIndianCompact,
  formatInr,
  formatInrOrUnset,
  roundToRupee,
  rupees,
  splitShares,
  toPaise,
} from './money'

/* ══ 1. No float ever enters a stored value ═════════════════════════════════ */

describe('no float ever becomes money', () => {
  it.each([0.5, 1.0000000000000002, -2.5, NaN, Infinity, -Infinity])(
    'assertPaise rejects %p', bad => {
      expect(() => assertPaise(bad)).toThrow(MoneyError)
    },
  )

  it('rejects anything past MAX_SAFE_INTEGER, where a double stops counting by ones', () => {
    expect(() => assertPaise(Number.MAX_SAFE_INTEGER + 2)).toThrow(MoneyError)
    expect(assertPaise(Number.MAX_SAFE_INTEGER)).toBe(Number.MAX_SAFE_INTEGER)
  })

  it('rejects a non-number outright', () => {
    expect(() => assertPaise('100' as unknown as number)).toThrow(MoneyError)
    expect(() => assertPaise(null as unknown as number)).toThrow(MoneyError)
  })

  it('refuses to parse a fractional JS number as rupees', () => {
    // 12.34 is not exactly representable; trusting it is the whole defect.
    expect(() => toPaise(12.34)).toThrow(MoneyError)
    expect(toPaise(12)).toBe(1200)
  })

  it('every field a line produces is an integer', () => {
    const line = computeLine({
      unitRatePaise: toPaise('1234.57'), quantityMilli: 3500,
      discountBp: 750, gstRateBp: 1800, gstTreatment: GST_TAXABLE,
    })
    for (const [key, value] of Object.entries(line)) {
      if (typeof value === 'number') {
        expect(Number.isInteger(value), `${key} = ${value}`).toBe(true)
      }
    }
  })
})

/* ══ 2. Parsing and Indian formatting ═══════════════════════════════════════ */

describe('parsing', () => {
  it.each([
    ['0', 0], ['1', 100], ['1.5', 150], ['1.50', 150], ['1234.56', 123456],
    ['₹1,23,456.78', 12345678], ['Rs. 1,234', 123400], ['-45.05', -4505],
    ['(45.05)', -4505], ['  12.3  ', 1230], ['10.5000', 1050],
  ])('toPaise(%s) === %i', (text, expected) => {
    expect(toPaise(text as string)).toBe(expected)
  })

  it('sub-paise precision is an error, not a silent round', () => {
    expect(() => toPaise('10.005')).toThrow(MoneyError)
  })

  it.each(['', 'abc', '1.2.3', '₹', '5%', '1e5', '--5'])('rejects %p', text => {
    expect(() => toPaise(text)).toThrow(MoneyError)
  })
})

describe('Indian formatting', () => {
  it.each([
    [0, '₹0.00'],
    [5, '₹0.05'],
    [100, '₹1.00'],
    [123456, '₹1,234.56'],
    [12345678, '₹1,23,456.78'],        // lakh grouping, NOT ₹123,456.78
    [1234567890, '₹1,23,45,678.90'],   // crore grouping
    [12345678901234, '₹1,23,45,67,89,012.34'],
    [-4505, '-₹45.05'],                // minus leads the symbol, as on a receipt
  ])('formatInr(%i) === %s', (paise, expected) => {
    expect(formatInr(paise)).toBe(expected)
  })

  it('round-trips through the parser', () => {
    for (const paise of [0, 1, 99, 100, 123456789, -55555, 1000000000]) {
      expect(toPaise(formatInr(paise))).toBe(paise)
    }
  })

  it('respects the symbol/decimals switches', () => {
    expect(formatInr(12345678, { symbol: false })).toBe('1,23,456.78')
    expect(formatInr(12345678, { decimals: false })).toBe('₹1,23,456')
  })

  it.each([
    [999900, '₹9,999'],
    [10000000, '₹1.00 L'],
    [123456700, '₹12.35 L'],
    [1000000000, '₹1.00 Cr'],
    [12345678900, '₹12.35 Cr'],
    [-1000000000, '-₹1.00 Cr'],
  ])('formatIndianCompact(%i) === %s', (paise, expected) => {
    expect(formatIndianCompact(paise)).toBe(expected)
  })

  it('an unset amount is words, never ₹0.00', () => {
    // THE FAILURE: an unpriced item rendering as ₹0.00, which reads as "free".
    expect(formatInrOrUnset(null)).toBe('Not configured')
    expect(formatInrOrUnset(undefined)).toBe('Not configured')
    expect(formatInrOrUnset(null, 'Not known')).toBe('Not known')
    expect(formatInrOrUnset(0)).toBe('₹0.00')
    expect(formatInrOrUnset(null)).not.toBe(formatInrOrUnset(0))
  })
})

/* ══ 3. Rounding ════════════════════════════════════════════════════════════ */

describe('the one rounding rule: half-up, away from zero', () => {
  it.each([
    [5, 2, 3], [-5, 2, -3], [7, 2, 4], [-7, 2, -4], [3, 2, 2], [1, 3, 0], [0, 7, 0],
  ])('divRoundHalfUp(%i, %i) === %i', (n, d, expected) => {
    expect(divRoundHalfUp(n, d)).toBe(expected)
  })

  it('is NOT Math.round, which rounds -2.5 toward +infinity', () => {
    expect(Math.round(-2.5)).toBe(-2)      // the wrong answer, pinned
    expect(divRoundHalfUp(-5, 2)).toBe(-3) // the right one
  })

  it.each([
    [100, 1800, 18], [1, 5000, 1], [5, 5000, 3], [-5, 5000, -3], [100, 0, 0],
  ])('applyBasisPoints(%i, %i) === %i', (paise, bp, expected) => {
    expect(applyBasisPoints(paise, bp)).toBe(expected)
  })

  it.each([[0, 0], [49, 0], [50, 100], [149, 100], [150, 200], [-50, -100], [-150, -200]])(
    'roundToRupee(%i) === %i', (paise, expected) => {
      expect(roundToRupee(paise)).toBe(expected)
    },
  )

  it('a refund is the exact mirror of its charge', () => {
    for (const rate of [1, 3, 5, 7, 33, 12345, 99999]) {
      const charge = computeLine({
        unitRatePaise: rate, quantityMilli: 1500,
        gstRateBp: 500, gstTreatment: GST_TAXABLE,
      })
      const refund = computeLine({
        unitRatePaise: -rate, quantityMilli: 1500,
        gstRateBp: 500, gstTreatment: GST_TAXABLE,
      })
      expect(refund.totalPaise).toBe(-charge.totalPaise)
      expect(refund.taxPaise).toBe(-charge.taxPaise)
    }
  })
})

/* ══ 4. GST ═════════════════════════════════════════════════════════════════ */

describe('GST: healthcare is exempt, pharmacy is not', () => {
  it.each([GST_EXEMPT, GST_NIL_RATED, GST_ZERO_RATED])(
    '%s is never taxed, even with 18%% on the item master', treatment => {
      const gst = computeGst(100000, { rateBp: 1800, treatment })
      expect(gst.taxPaise).toBe(0)
      expect(gst.cgstPaise + gst.sgstPaise + gst.igstPaise).toBe(0)
    },
  )

  it('a consultation is exempt and a syringe is not', () => {
    const consultation = computeLine({
      unitRatePaise: rupees('500'), gstRateBp: 1800, gstTreatment: GST_EXEMPT,
    })
    const syringe = computeLine({
      unitRatePaise: rupees('500'), gstRateBp: 1200, gstTreatment: GST_TAXABLE,
    })
    expect(consultation.taxPaise).toBe(0)
    expect(consultation.totalPaise).toBe(rupees('500'))
    expect(syringe.taxPaise).toBe(rupees('60'))
    expect(syringe.totalPaise).toBe(rupees('560'))
  })

  it('intra-state is CGST + SGST, equal halves; inter-state is IGST alone', () => {
    const intra = computeGst(100000, { rateBp: 1800, treatment: GST_TAXABLE })
    expect(intra.cgstPaise).toBe(9000)
    expect(intra.sgstPaise).toBe(9000)
    expect(intra.igstPaise).toBe(0)

    const inter = computeGst(100000, { rateBp: 1800, treatment: GST_TAXABLE, interstate: true })
    expect(inter.igstPaise).toBe(18000)
    expect(inter.cgstPaise).toBe(0)
    expect(inter.taxPaise).toBe(18000)
  })

  it('the one-paise gap between the two splits is deliberate', () => {
    // ₹1.00 at 5%: 2.5p + 2.5p each rounded half-up to 3p = 6p intra-state;
    // one 5p levy inter-state. CGST and SGST are separate levies.
    expect(computeGst(100, { rateBp: 500, treatment: GST_TAXABLE }).taxPaise).toBe(6)
    expect(computeGst(100, { rateBp: 500, treatment: GST_TAXABLE, interstate: true }).taxPaise).toBe(5)
  })

  it('an unknown treatment is rejected, never defaulted to taxable or exempt', () => {
    expect(() => computeGst(100, {
      rateBp: 1800, treatment: 'maybe' as never,
    })).toThrow(MoneyError)
  })
})

/* ══ 5. Lines and invoices ══════════════════════════════════════════════════ */

describe('line and invoice arithmetic', () => {
  it('quantity is thousandths, so half units need no float', () => {
    expect(computeLine({ unitRatePaise: rupees('100'), quantityMilli: 500 }).grossPaise)
      .toBe(rupees('50'))
    expect(computeLine({ unitRatePaise: rupees('100'), quantityMilli: 2250 }).grossPaise)
      .toBe(rupees('225'))
    expect(QUANTITY_SCALE).toBe(1000)
    expect(BASIS_POINTS).toBe(10_000)
  })

  it('tax is computed on the post-discount value', () => {
    const line = computeLine({
      unitRatePaise: rupees('1000'), discountBp: 1000,
      gstRateBp: 1800, gstTreatment: GST_TAXABLE,
    })
    expect(line.discountPaise).toBe(rupees('100'))
    expect(line.taxableValuePaise).toBe(rupees('900'))
    expect(line.taxPaise).toBe(rupees('162'))
    expect(line.totalPaise).toBe(rupees('1062'))
  })

  it('a discount cannot invert a line, and a negative discount is refused', () => {
    const capped = computeLine({ unitRatePaise: rupees('100'), discountPaise: rupees('500') })
    expect(capped.discountPaise).toBe(rupees('100'))
    expect(capped.totalPaise).toBe(0)
    expect(() => computeLine({ unitRatePaise: rupees('100'), discountPaise: -500 }))
      .toThrow(MoneyError)
  })

  it('an invoice is the sum of ROUNDED lines, not a rounded sum', () => {
    // Three ₹1.01 lines at 18%: each tax rounds to 18p, so the foot is 54p.
    // Taxing the ₹3.03 subtotal in one go would give 55p and would disagree
    // with the printed lines.
    const lines = [1, 2, 3].map(() => computeLine({
      unitRatePaise: 101, gstRateBp: 1800, gstTreatment: GST_TAXABLE,
    }))
    expect(lines.map(l => l.taxPaise)).toEqual([18, 18, 18])
    const totals = computeInvoice(lines, { roundOffToRupee: false })
    expect(totals.taxPaise).toBe(54)
    expect(totals.totalPaise).toBe(357)
    expect(applyBasisPoints(303, 1800)).toBe(55)
  })

  it('every foot field is the exact sum of its lines', () => {
    const lines = [
      computeLine({ unitRatePaise: rupees('123.45'), quantityMilli: 1500, gstRateBp: 1800, gstTreatment: GST_TAXABLE }),
      computeLine({ unitRatePaise: rupees('99.99'), discountBp: 500, gstRateBp: 500, gstTreatment: GST_TAXABLE }),
      computeLine({ unitRatePaise: rupees('2500'), gstTreatment: GST_EXEMPT }),
    ]
    const totals = computeInvoice(lines, { roundOffToRupee: false })
    expect(totals.totalPaise).toBe(lines.reduce((s, l) => s + l.totalPaise, 0))
    expect(totals.taxPaise).toBe(lines.reduce((s, l) => s + l.taxPaise, 0))
    expect(totals.taxableValuePaise + totals.taxPaise).toBe(totals.totalPaise)
  })

  it('the round-off is explicit and reconciles', () => {
    const totals = computeInvoice(
      [computeLine({ unitRatePaise: 12345, gstRateBp: 1800, gstTreatment: GST_TAXABLE })],
    )
    expect(totals.totalPaise).toBe(14567)
    expect(totals.payablePaise).toBe(14600)
    expect(totals.roundingAdjustmentPaise).toBe(33)
    expect(totals.totalPaise + totals.roundingAdjustmentPaise).toBe(totals.payablePaise)
  })

  it('an empty invoice is zero, not an error', () => {
    const totals = computeInvoice([])
    expect(totals.totalPaise).toBe(0)
    expect(totals.payablePaise).toBe(0)
  })

  it('a credit note is negative all the way down', () => {
    const totals = computeInvoice(
      [computeLine({ unitRatePaise: -12345, gstRateBp: 1800, gstTreatment: GST_TAXABLE })],
    )
    expect(totals.totalPaise).toBe(-14567)
    expect(totals.payablePaise).toBe(-14600)
    expect(totals.roundingAdjustmentPaise).toBe(-33)
  })
})

/* ══ 6. Large values ════════════════════════════════════════════════════════ */

describe('lakh and crore scale', () => {
  it('a ₹1 crore line is exact end to end', () => {
    const oneCrore = toPaise('1,00,00,000')
    expect(oneCrore).toBe(1000000000)
    const line = computeLine({ unitRatePaise: oneCrore, gstRateBp: 1800, gstTreatment: GST_TAXABLE })
    expect(line.taxPaise).toBe(180000000)
    expect(line.totalPaise).toBe(1180000000)
    expect(formatInr(line.totalPaise)).toBe('₹1,18,00,000.00')
    expect(formatIndianCompact(line.totalPaise)).toBe('₹1.18 Cr')
  })

  it('a thousand near-crore lines still reconcile', () => {
    const lines = Array.from({ length: 1000 }, () => computeLine({
      unitRatePaise: toPaise('99,99,999.99'), gstRateBp: 1200, gstTreatment: GST_TAXABLE,
    }))
    const totals = computeInvoice(lines, { roundOffToRupee: false })
    expect(totals.totalPaise).toBe(lines.reduce((s, l) => s + l.totalPaise, 0))
    expect(totals.totalPaise).toBe(1000 * 1119999999)
  })
})

/* ══ 7. Splitting ═══════════════════════════════════════════════════════════ */

describe('splitShares never loses or invents a paise', () => {
  it.each([
    [10001, [80, 20]], [100, [1, 1, 1]], [1, [1, 1]], [7, [1, 2, 4]],
    [999999, [1, 1, 1, 1, 1, 1, 1]], [0, [1, 1]],
  ])('split(%i, %j) sums back to the total', (total, weights) => {
    expect(splitShares(total as number, weights as number[]).reduce((a, b) => a + b, 0))
      .toBe(total)
  })

  it('splits a refund the same way it split the charge', () => {
    expect(splitShares(-10001, [80, 20]).reduce((a, b) => a + b, 0)).toBe(-10001)
    expect(splitShares(-100, [1, 1])).toEqual([-50, -50])
  })

  it('largest-remainder is deterministic, ties to the earlier party', () => {
    expect(splitShares(100, [1, 1, 1])).toEqual([34, 33, 33])
  })

  it('all-zero weights are rejected', () => {
    expect(() => splitShares(100, [0, 0])).toThrow(MoneyError)
  })
})

/* ══ 8. PARITY WITH THE SERVER ══════════════════════════════════════════════ */

/**
 * Each expectation below was produced by server/services/money.py and pasted
 * here verbatim. These are the cases where a client/server disagreement is
 * plausible AND expensive: the .5 boundary, the CGST/SGST halving, the
 * post-discount taxable value, the invoice round-off and the credit note.
 *
 * If one of these goes red, the browser and the server no longer agree about a
 * rupee. Fix both files together; do not "correct" the expectation.
 */
const PARITY_CASES: ReadonlyArray<{
  readonly name: string
  readonly input: Parameters<typeof computeLine>[0]
  readonly expected: { gross: number; discount: number; taxable: number; cgst: number; sgst: number; igst: number; tax: number; total: number }
}> = [
  {
    name: 'exempt consultation, ₹500',
    input: { unitRatePaise: 50000, gstRateBp: 1800, gstTreatment: GST_EXEMPT },
    expected: { gross: 50000, discount: 0, taxable: 50000, cgst: 0, sgst: 0, igst: 0, tax: 0, total: 50000 },
  },
  {
    name: 'taxable consumable 12%, intra-state',
    input: { unitRatePaise: 50000, gstRateBp: 1200, gstTreatment: GST_TAXABLE },
    expected: { gross: 50000, discount: 0, taxable: 50000, cgst: 3000, sgst: 3000, igst: 0, tax: 6000, total: 56000 },
  },
  {
    name: 'the odd-paise 5% case: ₹1.00 intra-state',
    input: { unitRatePaise: 100, gstRateBp: 500, gstTreatment: GST_TAXABLE },
    expected: { gross: 100, discount: 0, taxable: 100, cgst: 3, sgst: 3, igst: 0, tax: 6, total: 106 },
  },
  {
    name: 'the odd-paise 5% case: ₹1.00 inter-state',
    input: { unitRatePaise: 100, gstRateBp: 500, gstTreatment: GST_TAXABLE, interstate: true },
    expected: { gross: 100, discount: 0, taxable: 100, cgst: 0, sgst: 0, igst: 5, tax: 5, total: 105 },
  },
  {
    name: '3.5 units of ₹1234.57 with a 7.5% discount at 18%',
    input: { unitRatePaise: 123457, quantityMilli: 3500, discountBp: 750, gstRateBp: 1800, gstTreatment: GST_TAXABLE },
    expected: { gross: 432100, discount: 32408, taxable: 399692, cgst: 35972, sgst: 35972, igst: 0, tax: 71944, total: 471636 },
  },
  {
    name: 'a credit note line at 18%',
    input: { unitRatePaise: -12345, gstRateBp: 1800, gstTreatment: GST_TAXABLE },
    expected: { gross: -12345, discount: 0, taxable: -12345, cgst: -1111, sgst: -1111, igst: 0, tax: -2222, total: -14567 },
  },
]

describe('parity with server/services/money.py', () => {
  it.each(PARITY_CASES)('$name', ({ input, expected }) => {
    const line = computeLine(input)
    expect({
      gross: line.grossPaise,
      discount: line.discountPaise,
      taxable: line.taxableValuePaise,
      cgst: line.cgstPaise,
      sgst: line.sgstPaise,
      igst: line.igstPaise,
      tax: line.taxPaise,
      total: line.totalPaise,
    }).toEqual(expected)
  })

  it('the invoice foot matches the server, round-off included', () => {
    // server: compute_invoice([...PARITY_CASES lines...]) → these numbers.
    const lines = PARITY_CASES.map(c => computeLine(c.input))
    const totals = computeInvoice(lines)
    expect(totals.totalPaise).toBe(563280)
    expect(totals.payablePaise).toBe(563300)
    expect(totals.roundingAdjustmentPaise).toBe(20)
    expect(totals.cgstPaise).toBe(37864)
    expect(totals.sgstPaise).toBe(37864)
    expect(totals.igstPaise).toBe(5)
  })

  it('splitShares matches the server, largest-remainder and all', () => {
    expect(splitShares(10001, [80, 20])).toEqual([8001, 2000])
    expect(splitShares(7, [1, 2, 4])).toEqual([1, 2, 4])
    expect(splitShares(999999, [1, 1, 1, 1, 1, 1, 1])).toEqual([
      142857, 142857, 142857, 142857, 142857, 142857, 142857,
    ])
  })
})
