/**
 * Helix stock rules, shared by the Formulary and the Patient Board.
 *
 * The two screens used to answer "how much is on the shelf" differently: the
 * board summed every lot (expired ones included), looked the drug up by name
 * and called a medicine low under 100, while the formulary counted only
 * dispensable lots and called it low under 30. One drug could read "In Stock"
 * on one tab and "Low" on the other. Both now read from here.
 */

export interface StockLot {
  medication_id: string
  quantity: number
  expiration_date: string | null
  /** The server's dispense rule for this lot: 1 when it has a recorded expiry
   *  on or after today. Absent from an older server, then judged here. */
  in_date?: number | boolean
}

/** Below this many packs a medicine reads as low. Sized to the seeded
 *  inventory scale (lots hold up to about 200). */
export const LOW_STOCK_THRESHOLD = 30

export type LotState = 'expired' | 'undated' | 'empty' | 'soon' | 'ok'

/** The YYYY-MM-DD part of a stored expiry, or null. */
export function expiryKey(raw: string | null | undefined): string | null {
  const m = /^(\d{4}-\d{2}-\d{2})/.exec((raw || '').trim())
  return m ? m[1] : null
}

export function lotState(lot: StockLot, today: string, soonCutoff: string): LotState {
  const key = expiryKey(lot.expiration_date)
  if (!key) return 'undated'
  const inDate = lot.in_date === undefined ? key >= today : !!lot.in_date
  if (!inDate) return 'expired'
  if (lot.quantity <= 0) return 'empty'
  if (key <= soonCutoff) return 'soon'
  return 'ok'
}

/** What a dispense could actually take from this lot: in date, with packs left. */
export function isDispensable(lot: StockLot, today: string): boolean {
  const state = lotState(lot, today, today)
  return state === 'ok' || state === 'soon'
}

/** Dispensable packs of one medicine across all its lots. */
export function availableStock(lots: StockLot[], medicationId: string, today: string): number {
  return lots
    .filter(lot => lot.medication_id === medicationId && isDispensable(lot, today))
    .reduce((sum, lot) => sum + lot.quantity, 0)
}

/* "Units" read as international units on heparin and insulin, where a unit is
   a dose. Stock is counted in packs, so it is named by the pack. */
const PACK_NOUNS: Record<string, [string, string]> = {
  tablet: ['tablet', 'tablets'],
  capsule: ['capsule', 'capsules'],
  vial: ['vial', 'vials'],
  ampoule: ['ampoule', 'ampoules'],
  ampule: ['ampoule', 'ampoules'],
  bottle: ['bottle', 'bottles'],
  syrup: ['bottle', 'bottles'],
  suspension: ['bottle', 'bottles'],
  drops: ['bottle', 'bottles'],
  sachet: ['sachet', 'sachets'],
  tube: ['tube', 'tubes'],
  cream: ['tube', 'tubes'],
  ointment: ['tube', 'tubes'],
  gel: ['tube', 'tubes'],
  bag: ['bag', 'bags'],
  inhaler: ['inhaler', 'inhalers'],
  patch: ['patch', 'patches'],
  pen: ['pen', 'pens'],
  syringe: ['syringe', 'syringes'],
  suppository: ['suppository', 'suppositories'],
}

/** "tablets", "vials", or "packs" for a form Orb has no noun for. */
export function packUnit(form: string | null | undefined, qty: number): string {
  const nouns = PACK_NOUNS[(form || '').trim().toLowerCase()] ?? ['pack', 'packs']
  return qty === 1 ? nouns[0] : nouns[1]
}

/** "1,20,000 tablets": Indian digit grouping, named by the pack. */
export function formatPackCount(qty: number, form: string | null | undefined): string {
  return `${qty.toLocaleString('en-IN')} ${packUnit(form, qty)}`
}
