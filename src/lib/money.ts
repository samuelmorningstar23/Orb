/**
 * Orb — money primitives for the browser. THE MIRROR of server/services/money.py.
 *
 * Every rupee the UI parses, adds, taxes or prints goes through this file, and
 * every rule in it is the same rule the server enforces. The two files are a
 * PAIR: if you change a rounding rule, a GST split or a format here, the server
 * module changes in the same commit. A client that rounds differently from the
 * server produces a screen that disagrees with the bill it just created, and the
 * clerk believes the screen.
 *
 * THE FOUR RULES (identical to the Python side, restated because this file is
 * read on its own):
 *
 * 1. MONEY IS AN INTEGER OF PAISE. JavaScript has one number type and it is a
 *    double, so "integer paise" is a discipline this module enforces rather than
 *    a type the language gives us: `assertPaise` rejects any non-integer and
 *    anything outside `Number.MAX_SAFE_INTEGER` (±₹90,07,19,92,54,740.99 — far
 *    past a hospital's annual revenue, but the check is free and the failure it
 *    prevents is silent). Never `parseFloat` a rupee string; `toPaise` parses
 *    the digits as text.
 *
 * 2. ONE ROUNDING RULE: HALF-UP, AWAY FROM ZERO, AT THE PAISE BOUNDARY.
 *    `divRoundHalfUp` is the only division here. Not `Math.round` — that rounds
 *    -2.5 to -2 (toward +∞), so a refund would not mirror its charge.
 *
 * 3. ROUNDING HAPPENS EARLY AND ONCE PER LINE. An invoice is the integer SUM of
 *    already-rounded lines, never a re-derivation from the subtotal.
 *
 * 4. NOT CONFIGURED IS NOT ZERO. `formatInrOrUnset(null)` is words.
 *
 * GST: healthcare services by a clinical establishment are EXEMPT in India;
 * pharmacy, consumables and implants are not. `computeGst` returns zeros for an
 * exempt item whatever rate is attached to it. Intra-state → CGST + SGST at half
 * the rate each, rounded independently; inter-state → IGST at the full rate.
 */

/** 1 rupee = 100 paise. */
export const PAISE_PER_RUPEE = 100

/**
 * Quantities are integers of THOUSANDTHS of a unit, for the same reason money is
 * integer paise: 0.5 vials and 1.5 hours of OT time are real, and neither may
 * arrive as a float. 1 unit = 1000.
 */
export const QUANTITY_SCALE = 1000

/** Rates and percentages are integer BASIS POINTS. 5% = 500, 18% = 1800. */
export const BASIS_POINTS = 10_000

/** Thrown for anything that cannot be money. Mirrors Python's MoneyError. */
export class MoneyError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'MoneyError'
  }
}

/* ── GST treatment ────────────────────────────────────────────────────────── */

export type GstTreatment = 'exempt' | 'taxable' | 'nil_rated' | 'zero_rated'

/** Healthcare services by a clinical establishment. Never taxed. */
export const GST_EXEMPT: GstTreatment = 'exempt'
/** Pharmacy, consumables, implants, vendor purchases. */
export const GST_TAXABLE: GstTreatment = 'taxable'
/** In the schedule, at 0%. Same arithmetic as exempt, different return box. */
export const GST_NIL_RATED: GstTreatment = 'nil_rated'
/** Exports / SEZ. Same arithmetic, different return box. */
export const GST_ZERO_RATED: GstTreatment = 'zero_rated'

export const GST_TREATMENTS: readonly GstTreatment[] = [
  GST_EXEMPT, GST_TAXABLE, GST_NIL_RATED, GST_ZERO_RATED,
]

/* ── Type gate ────────────────────────────────────────────────────────────── */

/**
 * The gate every money value passes through.
 *
 * Rejects non-integers (0.5 paise is not a thing), NaN/Infinity, and anything
 * beyond `Number.MAX_SAFE_INTEGER`, where a double silently stops counting by
 * ones. A `1.0000000000000002` that arrived from a stray `* 1.18` fails here
 * rather than being stored.
 */
export function assertPaise(value: unknown, name = 'money value (paise)'): number {
  if (typeof value !== 'number') {
    throw new MoneyError(`${name} must be a number (got ${typeof value})`)
  }
  if (!Number.isFinite(value)) {
    throw new MoneyError(`${name} must be finite (got ${value})`)
  }
  if (!Number.isInteger(value)) {
    throw new MoneyError(
      `${name} must be a whole number of paise (got ${value}): money is never fractional here`,
    )
  }
  if (!Number.isSafeInteger(value)) {
    throw new MoneyError(
      `${name} is beyond Number.MAX_SAFE_INTEGER (${value}); past this point a ` +
      'double stops counting by ones and the arithmetic is silently wrong',
    )
  }
  return value
}

function assertInt(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || !Number.isSafeInteger(value)) {
    throw new MoneyError(`${name} must be a safe integer (got ${String(value)})`)
  }
  return value
}

/* ── The one rounding rule ────────────────────────────────────────────────── */

/**
 * Integer division rounding halves AWAY FROM ZERO. The only division in this file.
 *
 * 2.5 → 3, -2.5 → -3. `Math.round(-2.5)` is -2, which would make a refund fail
 * to mirror the charge that created it — hence the explicit implementation.
 * Computed as `(2n + d) / 2d` on absolute values, floored, so no intermediate
 * fraction is ever kept.
 */
export function divRoundHalfUp(numerator: number, denominator: number): number {
  assertInt(numerator, 'numerator')
  assertInt(denominator, 'denominator')
  if (denominator === 0) throw new MoneyError('division by zero in money arithmetic')
  const negative = (numerator < 0) !== (denominator < 0)
  const n = Math.abs(numerator)
  const d = Math.abs(denominator)
  const magnitude = Math.floor((2 * n + d) / (2 * d))
  return negative ? -magnitude : magnitude
}

/** `paise * bp / 10000`, rounded by the one rule. Tax, discounts and co-pays. */
export function applyBasisPoints(paise: number, basisPoints: number): number {
  return divRoundHalfUp(assertPaise(paise) * assertInt(basisPoints, 'basisPoints'), BASIS_POINTS)
}

/** Nearest whole rupee, in paise. Same half-up rule. */
export function roundToRupee(paise: number): number {
  return divRoundHalfUp(assertPaise(paise), PAISE_PER_RUPEE) * PAISE_PER_RUPEE
}

/* ── Parsing ──────────────────────────────────────────────────────────────── */

const AMOUNT_RE = /^([-+]?)(\d+)(?:\.(\d+))?$/

/**
 * Parse a human/rupee amount into integer paise. THE ONLY ENTRY POINT.
 *
 * Accepts `"1234.50"`, `"₹1,23,456.78"`, `"-45"`, `"(45.05)"` (accounting
 * notation for a credit) and a plain `number` of RUPEES — but only a whole one,
 * because `toPaise(12.34)` would mean trusting a double to hold 12.34, which is
 * the exact thing this module exists to prevent. Pass the string.
 *
 * Never uses `parseFloat`: the integer and fraction parts are read as text and
 * combined with integer arithmetic.
 */
export function toPaise(value: string | number): number {
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) {
      throw new MoneyError(
        `cannot read ${value} as money: a fractional JS number cannot represent ` +
        'rupees exactly. Pass the amount as a string, e.g. "12.34".',
      )
    }
    return assertPaise(value * PAISE_PER_RUPEE)
  }
  let cleaned = String(value)
    .trim()
    .replace(/₹/g, '')
    .replace(/Rs\.?/gi, '')
    .replace(/,/g, '')
    .replace(/\s/g, '')
  if (cleaned.startsWith('(') && cleaned.endsWith(')')) cleaned = `-${cleaned.slice(1, -1)}`

  const match = AMOUNT_RE.exec(cleaned)
  if (!match) throw new MoneyError(`cannot read ${JSON.stringify(value)} as a rupee amount`)
  const [, sign, whole, fracRaw] = match
  let frac = fracRaw ?? ''
  if (frac.length > 2) {
    if (frac.slice(2).replace(/0/g, '') !== '') {
      throw new MoneyError(
        `${JSON.stringify(value)} is finer than one paise; round it at the source ` +
        'rather than letting Orb guess',
      )
    }
    frac = frac.slice(0, 2)
  }
  frac = (frac + '00').slice(0, 2)
  const paise = Number(whole) * PAISE_PER_RUPEE + Number(frac)
  const signed = sign === '-' ? -paise : paise
  return assertPaise(signed)
}

/** Alias — reads better at an import boundary. */
export const parseInr = toPaise

/** `rupees("1234.50") === 123450`. Sugar for fixtures and constants. */
export const rupees = toPaise

/* ── Formatting ───────────────────────────────────────────────────────────── */

/**
 * `1234567` → `'12,34,567'`. Last three digits, then PAIRS — the lakh/crore
 * grouping. `Number.toLocaleString()` with the wrong locale gives western
 * thousands grouping, which is what makes an Indian invoice look imported; and
 * `'en-IN'` support has historically varied by engine, so this is done by hand.
 */
function groupIndian(digits: string): string {
  if (digits.length <= 3) return digits
  let head = digits.slice(0, -3)
  const tail = digits.slice(-3)
  const parts: string[] = []
  while (head.length > 2) {
    parts.unshift(head.slice(-2))
    head = head.slice(0, -2)
  }
  if (head) parts.unshift(head)
  return [...parts, tail].join(',')
}

export interface FormatOptions {
  /** Include the ₹ symbol. Default true. */
  readonly symbol?: boolean
  /** Include the paise. Default true. */
  readonly decimals?: boolean
}

/**
 * Render paise as an Indian rupee string: `formatInr(12345678)` → `'₹1,23,456.78'`.
 *
 * The minus leads the symbol (`'-₹5.00'`), which is how every Indian accounting
 * package prints a credit and how a refund must read on a receipt.
 *
 * NOTE FOR CALLERS: money is always rendered with tabular-lining figures
 * (`font-variant-numeric: tabular-nums lining-nums`) so columns of amounts line
 * up. That is a CSS obligation on the component, not something a string can do.
 */
export function formatInr(paise: number, options: FormatOptions = {}): string {
  const { symbol = true, decimals = true } = options
  const p = assertPaise(paise)
  const negative = p < 0
  const abs = Math.abs(p)
  const whole = Math.floor(abs / PAISE_PER_RUPEE)
  const frac = abs % PAISE_PER_RUPEE
  let body = groupIndian(String(whole))
  if (decimals) body = `${body}.${String(frac).padStart(2, '0')}`
  if (symbol) body = `₹${body}`
  return negative ? `-${body}` : body
}

/**
 * `null` is not zero. A tariff nobody has entered reads as "Not configured",
 * because "₹0.00" is a price — a free item — and somebody will bill it.
 */
export function formatInrOrUnset(
  paise: number | null | undefined,
  unset = 'Not configured',
): string {
  if (paise === null || paise === undefined) return unset
  return formatInr(paise)
}

/**
 * Dashboard scale: `'₹1.23 Cr'`, `'₹4.50 L'`, `'₹9,999'`. Headline figures only —
 * never a bill line, never anything a clerk reconciles.
 */
export function formatIndianCompact(paise: number, options: FormatOptions = {}): string {
  const { symbol = true } = options
  const p = assertPaise(paise)
  const negative = p < 0
  const abs = Math.abs(p)
  const crore = 100 * 100_000 * PAISE_PER_RUPEE
  const lakh = 100_000 * PAISE_PER_RUPEE
  let body: string
  if (abs >= crore) {
    const hundredths = divRoundHalfUp(abs * 100, crore)
    body = `${Math.floor(hundredths / 100)}.${String(hundredths % 100).padStart(2, '0')} Cr`
  } else if (abs >= lakh) {
    const hundredths = divRoundHalfUp(abs * 100, lakh)
    body = `${Math.floor(hundredths / 100)}.${String(hundredths % 100).padStart(2, '0')} L`
  } else {
    body = groupIndian(String(Math.floor(abs / PAISE_PER_RUPEE)))
  }
  if (symbol) body = `₹${body}`
  return negative ? `-${body}` : body
}

/* ── GST ──────────────────────────────────────────────────────────────────── */

export interface GstBreakup {
  readonly taxableValuePaise: number
  readonly cgstPaise: number
  readonly sgstPaise: number
  readonly igstPaise: number
  readonly taxPaise: number
  readonly rateBp: number
  readonly treatment: GstTreatment
  readonly interstate: boolean
}

export interface GstOptions {
  readonly rateBp?: number
  readonly treatment?: GstTreatment
  readonly interstate?: boolean
}

/**
 * GST on one taxable value.
 *
 * EXEMPT / NIL-RATED / ZERO-RATED → all zeros, WHATEVER `rateBp` says. That is
 * the India-specific rule this module exists to encode: a consultation, a bed
 * day and a procedure carry no GST, so a stray 18% left on a service master must
 * not be able to tax them.
 *
 * INTRA-STATE → CGST at half the rate + SGST at half the rate, each rounded
 * independently (they are two separate levies, not one number cut in two).
 * ₹1.00 at 5% is therefore 3p + 3p = 6p intra-state and 5p of IGST inter-state.
 * The one-paise difference is real and deliberate.
 */
export function computeGst(taxableValuePaise: number, options: GstOptions = {}): GstBreakup {
  const { rateBp = 0, treatment = GST_EXEMPT, interstate = false } = options
  const value = assertPaise(taxableValuePaise)
  const rate = assertInt(rateBp, 'rateBp')
  if (rate < 0) throw new MoneyError(`rateBp must not be negative (got ${rate})`)
  if (!GST_TREATMENTS.includes(treatment)) {
    throw new MoneyError(
      `unknown GST treatment ${JSON.stringify(treatment)}; expected one of ${GST_TREATMENTS.join(', ')}`,
    )
  }
  const zero: GstBreakup = {
    taxableValuePaise: value, cgstPaise: 0, sgstPaise: 0, igstPaise: 0,
    taxPaise: 0, rateBp: rate, treatment, interstate,
  }
  if (treatment !== GST_TAXABLE || rate === 0) return zero

  if (interstate) {
    const igst = applyBasisPoints(value, rate)
    return { ...zero, igstPaise: igst, taxPaise: igst }
  }
  const half = rate % 2 === 0
    ? applyBasisPoints(value, rate / 2)
    // An odd basis-point rate cannot be halved in basis points; halve the value
    // side of the fraction instead, still with integers only.
    : divRoundHalfUp(value * rate, 2 * BASIS_POINTS)
  return { ...zero, cgstPaise: half, sgstPaise: half, taxPaise: half * 2 }
}

/* ── Lines ────────────────────────────────────────────────────────────────── */

export interface LineTotals {
  readonly grossPaise: number
  readonly discountPaise: number
  readonly taxableValuePaise: number
  readonly cgstPaise: number
  readonly sgstPaise: number
  readonly igstPaise: number
  readonly taxPaise: number
  readonly totalPaise: number
  readonly gstRateBp: number
  readonly gstTreatment: GstTreatment
}

export interface LineInput {
  readonly unitRatePaise: number
  /** Thousandths of a unit. Default 1000 (= one unit). */
  readonly quantityMilli?: number
  readonly discountPaise?: number
  readonly discountBp?: number
  readonly gstRateBp?: number
  readonly gstTreatment?: GstTreatment
  readonly interstate?: boolean
}

/**
 * The line arithmetic, in the FIXED order the server uses. Do not reorder it.
 *
 *   1. gross    = round(unitRate * quantityMilli / 1000)
 *   2. discount = round(gross * discountBp / 10000) + explicit discountPaise,
 *                 capped at |gross| so a line can never invert its own sign
 *   3. taxable  = gross - discount     ← tax is on the POST-discount value
 *   4. tax      = computeGst(taxable)  ← rounded inside, per component
 *   5. total    = taxable + tax        ← pure integer addition
 *
 * Rounding occurs at steps 1, 2 and 4 and NOWHERE ELSE. A negative
 * `unitRatePaise` (a credit-note line, a refund) flows through unchanged: every
 * step is sign-symmetric.
 */
export function computeLine(input: LineInput): LineTotals {
  const {
    unitRatePaise,
    quantityMilli = QUANTITY_SCALE,
    discountPaise = 0,
    discountBp = 0,
    gstRateBp = 0,
    gstTreatment = GST_EXEMPT,
    interstate = false,
  } = input

  const rate = assertPaise(unitRatePaise, 'unitRatePaise')
  const qty = assertInt(quantityMilli, 'quantityMilli')
  const explicit = assertPaise(discountPaise, 'discountPaise')
  const discBp = assertInt(discountBp, 'discountBp')
  if (discBp < 0) throw new MoneyError(`discountBp must not be negative (got ${discBp})`)
  if (explicit < 0) {
    throw new MoneyError(
      `discountPaise must not be negative (got ${explicit}); a negative discount ` +
      'is a surcharge and must be its own line',
    )
  }

  const gross = divRoundHalfUp(rate * qty, QUANTITY_SCALE)
  let discount = applyBasisPoints(Math.abs(gross), discBp) + explicit
  // A discount larger than the line is a data error, not a negative bill. Cap
  // rather than throw: the approval workflow already decided the amount, and a
  // hard failure here would block a bill over a rounding edge. The cap is
  // visible in the returned discountPaise.
  if (discount > Math.abs(gross)) discount = Math.abs(gross)
  // `discount !== 0` guards against JavaScript's negative zero: `-0` is not
  // `Object.is`-equal to `0`, so it would fail a parity assertion against the
  // Python side (which has no such value) and could serialise as "-0" in JSON.
  const signedDiscount = gross < 0 && discount !== 0 ? -discount : discount
  const taxable = gross - signedDiscount

  const gst = computeGst(taxable, { rateBp: gstRateBp, treatment: gstTreatment, interstate })
  return {
    grossPaise: gross,
    discountPaise: signedDiscount,
    taxableValuePaise: taxable,
    cgstPaise: gst.cgstPaise,
    sgstPaise: gst.sgstPaise,
    igstPaise: gst.igstPaise,
    taxPaise: gst.taxPaise,
    totalPaise: taxable + gst.taxPaise,
    gstRateBp: gst.rateBp,
    gstTreatment: gst.treatment,
  }
}

/* ── Invoices ─────────────────────────────────────────────────────────────── */

export interface InvoiceTotals {
  readonly subtotalPaise: number
  readonly discountPaise: number
  readonly taxableValuePaise: number
  readonly cgstPaise: number
  readonly sgstPaise: number
  readonly igstPaise: number
  readonly taxPaise: number
  readonly totalPaise: number
  readonly roundingAdjustmentPaise: number
  readonly payablePaise: number
}

/**
 * Foot an invoice from already-computed lines.
 *
 * SUM OF ROUNDED, NOT ROUNDED SUM. Each line was rounded when it was computed
 * and is printed that way; the foot is the integer sum of exactly those printed
 * numbers, so a clerk adding the column by hand gets the same answer the system
 * did. Re-deriving tax from the subtotal is a second, different rounding.
 *
 * `roundOffToRupee` (default true) is the customary Indian invoice round-off:
 * the payable is the nearest rupee and the difference is recorded EXPLICITLY as
 * `roundingAdjustmentPaise` — the "Round Off" line — rather than smeared into a
 * tax figure. Turn it off for payer/claim documents, settled to the paise.
 */
export function computeInvoice(
  lines: readonly LineTotals[],
  options: { readonly roundOffToRupee?: boolean } = {},
): InvoiceTotals {
  const { roundOffToRupee = true } = options
  let subtotal = 0, discount = 0, taxable = 0
  let cgst = 0, sgst = 0, igst = 0, tax = 0, total = 0
  for (const line of lines) {
    subtotal += line.grossPaise
    discount += line.discountPaise
    taxable += line.taxableValuePaise
    cgst += line.cgstPaise
    sgst += line.sgstPaise
    igst += line.igstPaise
    tax += line.taxPaise
    total += line.totalPaise
  }
  assertPaise(total, 'invoice total')

  const payable = roundOffToRupee ? roundToRupee(total) : total
  return {
    subtotalPaise: subtotal,
    discountPaise: discount,
    taxableValuePaise: taxable,
    cgstPaise: cgst,
    sgstPaise: sgst,
    igstPaise: igst,
    taxPaise: tax,
    totalPaise: total,
    roundingAdjustmentPaise: payable - total,
    payablePaise: payable,
  }
}

/* ── Allocation ───────────────────────────────────────────────────────────── */

/**
 * Split a total across parties so the parts sum EXACTLY to the total.
 *
 * Largest-remainder: floor each share, then hand the leftover paise one at a
 * time to the largest remainders (ties to the earlier party, so the result is
 * deterministic and reproducible in an audit). A naive per-party
 * `round(total * w / sum)` loses or invents paise, and the paise it loses turn
 * into a balance nobody can clear.
 */
export function splitShares(totalPaise: number, weights: readonly number[]): number[] {
  const total = assertPaise(totalPaise)
  const ws = weights.map((w, i) => assertInt(w, `weight[${i}]`))
  if (ws.some(w => w < 0)) throw new MoneyError('split weights must not be negative')
  const weightSum = ws.reduce((a, b) => a + b, 0)
  if (weightSum === 0) throw new MoneyError('split weights must not all be zero')

  const negative = total < 0
  const magnitude = Math.abs(total)
  const base = ws.map(w => Math.floor((magnitude * w) / weightSum))
  const remainders = ws.map(w => (magnitude * w) % weightSum)
  const leftover = magnitude - base.reduce((a, b) => a + b, 0)
  const order = ws
    .map((_, i) => i)
    .sort((a, b) => (remainders[b] - remainders[a]) || (a - b))
  for (let k = 0; k < leftover; k += 1) base[order[k]] += 1
  // `b === 0 ? 0 : -b` avoids JavaScript's negative zero — see computeLine.
  return negative ? base.map(b => (b === 0 ? 0 : -b)) : base
}
