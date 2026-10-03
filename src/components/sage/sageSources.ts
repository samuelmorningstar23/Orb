/* The wording under a Sage answer about its sources, kept apart from
   SageMessage.tsx so that file exports only components (fast refresh). */

/** One document on a turn's `sources`, as sage_routes sends it. */
export interface SageSource {
  document_id: string
  title?: string | null
  source?: string | null
  source_type?: string | null
  trust_tier?: number | null
  is_synthetic?: boolean
  category?: string | null
  fusion_score?: number
  cited?: boolean
  attribution?: string
}

/** The sources header, from the turn's `citation_basis`. */
export function sourcesHeading(citationBasis?: string): string {
  if (citationBasis === 'model_cited') return 'Sources Sage cited';
  if (citationBasis === 'system_attributed') return 'Sources Orb matched to this answer';
  return 'Documents Sage read';
}

// Scopes the server answers from Orb's own records (the department census,
// the bed and admission counts, the list of matching patients) with no
// guideline search at all. Mirrors the record-built half of
// sage_service.DETERMINISTIC_SCOPES; the refusal scopes send no citation_basis.
const RECORD_SCOPES = new Set(['BROAD', 'AGGREGATE_STATS', 'DISAMBIGUATE']);

/**
 * The one line under an answer that rests on no retrieved document
 * (`citation_basis` 'none'). Without it, a model-only answer looked exactly
 * like a grounded one.
 */
export function noSourcesNote(
  citationBasis?: string,
  patientContextUsed?: boolean,
  queryScope?: string,
  confidenceBasis?: string,
): string | null {
  if (citationBasis !== 'none') return null;
  // A ward census or count answer is built from the records Orb read, so
  // "the model alone" would be false on every one of them, and a label that is
  // wrong on a whole class of answers teaches clinicians to ignore it.
  if ((queryScope && RECORD_SCOPES.has(queryScope)) || confidenceBasis === 'deterministic_scope') {
    return 'Built from the hospital records Orb read. No guideline or hospital policy was used.';
  }
  return patientContextUsed
    ? 'No guideline or hospital policy was found for this. The answer rests on the chart and the model alone.'
    : 'No guideline or hospital policy was found for this. The answer rests on the model alone.';
}
