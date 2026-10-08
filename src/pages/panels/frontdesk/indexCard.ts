/**
 * How the Register view words a patient index card (opd-6a). Minimal fields
 * only (ruling OPD-N4): never a date of birth, a full phone number or an
 * address, because the card is all a hospital-wide searcher may see.
 */
import type { IndexCard } from '../../../api/registration'

/** "47 y", or "about 47 y (stated)" for an age the patient told us. */
export function ageText(card: Pick<IndexCard, 'age' | 'age_stated'>): string | null {
  if (card.age === null || card.age === undefined) return null
  return card.age_stated ? `about ${card.age} y (stated)` : `${card.age} y`
}

const SEX_LABEL: Record<string, string> = { M: 'Male', F: 'Female', O: 'Other' }

/** "UHID X · Female · 47 y · phone ends 1234". */
export function cardMeta(card: IndexCard): string {
  return [
    card.uhid ? `UHID ${card.uhid}` : 'No UHID',
    card.sex ? (SEX_LABEL[card.sex] ?? card.sex) : null,
    ageText(card),
    card.phone_last4 ? `phone ends ${card.phone_last4}` : null,
  ].filter(Boolean).join(' · ')
}
