/** Turn a machine enum into something a clinician or a CIO can read.
 *
 *  Orb's server enums are snake_case (`phi_access`, `ai_inference`,
 *  `insufficient_data`, `api_mutation`). Several screens printed them straight,
 *  or with a naive capitalise-the-first-letter, which produced `Phi_access` and
 *  `Ai_inference` in the audit table — the most CIO-facing surface in the
 *  product — and `insufficient_data` in an I-PASS handover *next to its own
 *  human translation*.
 *
 *  Rendering is a UI concern, so this lives in the UI: the wire format stays
 *  exactly as it is, and nothing downstream of an API is renamed.
 */

/** Tokens that must keep their conventional casing. */
const ACRONYMS: Record<string, string> = {
  phi: 'PHI',
  ai: 'AI',
  api: 'API',
  ui: 'UI',
  id: 'ID',
  mrn: 'MRN',
  ehr: 'EHR',
  hl7: 'HL7',
  asr: 'ASR',
  llm: 'LLM',
  mfa: 'MFA',
  news2: 'NEWS2',
  ipass: 'I-PASS',
  qr: 'QR',
  sms: 'SMS',
  db: 'DB',
  // Hospital stores and facilities words: "ppe" and "grn 1" were printed raw
  // on the procurement screens.
  ppe: 'PPE',
  grn: 'GRN',
  uom: 'UOM',
  gst: 'GST',
  amc: 'AMC',
  cmc: 'CMC',
  icu: 'ICU',
  ot: 'OT',
  opd: 'OPD',
  ipd: 'IPD',
  abha: 'ABHA',
  abdm: 'ABDM',
  nabh: 'NABH',
}

/** "1 item", "3 items": a count with a real plural, never "item(s)". */
export function plural(count: number, singular: string, pluralWord = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralWord}`
}

/** `phi_access` → `PHI access`; `insufficient_data` → `Insufficient data`. */
export function humaniseEnum(value: string | null | undefined, fallback = '—'): string {
  if (!value) return fallback
  const words = String(value).trim().split(/[_\-\s]+/).filter(Boolean)
  if (words.length === 0) return fallback
  return words
    .map((w, i) => {
      const acronym = ACRONYMS[w.toLowerCase()]
      if (acronym) return acronym
      const lower = w.toLowerCase()
      // Only the first word is capitalised — sentence case, not Title Case,
      // which is what the rest of the product uses for inline labels.
      return i === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower
    })
    .join(' ')
}

/** Reasons the server's fail-closed sign gate refuses a one-click signature.
 *
 *  They arrive as diagnostics, not prose: the route builds them with
 *  `f"verified={v.get('verified')}"` and `f"{bucket}: {type}"`, so a clinician
 *  standing at the Sign & Finalize button was shown the literal string
 *  `verified=None` and asked to type SIGN. `None` is a Python word; a nurse or
 *  a doctor reads it as a value someone forgot to fill in, not as "this note
 *  was never checked". The wire format is a diagnostic and stays exactly as it
 *  is — the audit chain records these strings verbatim — so the translation
 *  happens here, at the point of render.
 */
export function humaniseSignGateReason(raw: string | null | undefined): string {
  const text = String(raw ?? '').trim()
  if (!text) return ''

  // `verified=None` / `verified=False` — the verdict field itself.
  const verdict = /^verified\s*=\s*(.*)$/i.exec(text)
  if (verdict) {
    const value = verdict[1].trim().toLowerCase()
    return value === 'false'
      // The check compares the note with its source TEXT, dictated or typed;
      // a typed note has no audio to be checked against.
      ? 'The automated check did not confirm this note against what was dictated or typed.'
      : 'No verification verdict was recorded for this note.'
  }

  // `warning: unsupported_medication` / `omission: high` — bucket and type.
  const bucket = /^(warning|omission)\s*:\s*(.+)$/i.exec(text)
  if (bucket) {
    return `${humaniseEnum(bucket[1])}: ${humaniseEnum(bucket[2]).toLowerCase()}`
  }

  // Anything else is passed through untouched UNLESS it still looks machine
  // shaped — the gate already writes some reasons as sentences and those must
  // not be re-cased into nonsense.
  return /[_=]/.test(text) ? humaniseEnum(text, text) : text
}
