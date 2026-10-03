/** Patient demographic formatting.
 *
 *  The `sex` column is NOT a single-letter enum. Seeds, HL7 ingestion and the
 *  admit form have all written into it, so the same column holds 'Male',
 *  'male', 'M', 'F', 'Female', 'Other', '' and NULL. Any component that
 *  compares it to a bare 'M' silently renders every male patient as female —
 *  which is exactly what the Vigil chart header did, one card above a
 *  MedGemma assessment that said "67-year-old male" about the same person.
 *
 *  These helpers normalise once, here, so no screen has to guess the dialect.
 */

export type SexCode = 'M' | 'F' | 'O' | ''

/** Normalise any stored sex dialect to a single code. '' means unknown. */
export function sexCode(sex: string | null | undefined): SexCode {
  if (!sex) return ''
  const s = String(sex).trim().toUpperCase()
  if (s === 'M' || s === 'MALE' || s === 'MAN' || s === 'BOY') return 'M'
  if (s === 'F' || s === 'FEMALE' || s === 'WOMAN' || s === 'GIRL') return 'F'
  if (s === 'O' || s === 'OTHER' || s === 'X' || s === 'INTERSEX') return 'O'
  if (s === 'U' || s === 'UNKNOWN' || s === 'N/A') return ''
  // Unrecognised free text — surface the first letter rather than lying.
  if (s.startsWith('M')) return 'M'
  if (s.startsWith('F')) return 'F'
  return 'O'
}

/** Compact chart form: 'M' / 'F' / 'O', or '—' when the record has no value. */
export function sexShort(sex: string | null | undefined, fallback = '—'): string {
  const c = sexCode(sex)
  return c === '' ? fallback : c
}

/** Full-word uppercase form for the chart header: MALE / FEMALE / OTHER. */
export function sexLabel(sex: string | null | undefined, fallback = 'SEX NOT RECORDED'): string {
  switch (sexCode(sex)) {
    case 'M': return 'MALE'
    case 'F': return 'FEMALE'
    case 'O': return 'OTHER'
    default: return fallback
  }
}
