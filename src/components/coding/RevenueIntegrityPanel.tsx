import { useCallback, useEffect, useState } from 'react'
import styles from './RevenueIntegrity.module.css'
import { apiFetch } from '../../api/client'
import PatientPicker from '../common/PatientPicker'

/**
 * Revenue Integrity — AI medical-coding & CDI (clinical documentation
 * improvement) panel. For a selected patient it shows:
 *   • suggested ICD-10 / SNOMED codes (code chips + evidence + confidence),
 *   • CDI documentation-gap flags with an illustrative ₹ impact,
 *   • documentation queries: a medicine that treats a condition nobody has
 *     written down, which is a question for the clinician and never a code,
 *   • a headline "Reimbursement: ₹captured → ₹potential (+₹uplift)".
 *
 * The reimbursement figures are ILLUSTRATIVE (see the backend assumption
 * table) — labelled as such, never presented as real payer rates.
 */

interface SuggestedCode {
  code: string
  system: string
  display: string
  evidence: string
  confidence: number
  /** The sentence the term was found in (240 characters at most). */
  evidence_sentence?: string | null
  /** Set when that sentence came from a note: which note and who wrote it. */
  evidence_note_id?: number | null
  evidence_note_type?: string | null
  evidence_author?: string | null
}

/** A condition only a medicine implies. Not a code: a question to ask. */
interface DocumentationQuery {
  condition: string
  medication: string
  code: string
  system: string
  display: string
  message: string
}

interface CdiFlag {
  type: string
  message: string
  rationale: string
  est_impact_inr: number
}

interface Reimbursement {
  captured_inr: number
  potential_inr: number
  uplift_inr: number
  /** The sharper-code flags' share of the uplift (already in it). */
  specificity_uplift_inr?: number
  assumptions: {
    model: string
    method: string
    base_rate_inr: number
    nonspecific_specificity_uplift_inr?: number
    caveats: string[]
    citations: string[]
    code_weights?: Record<string, number>
  }
}

interface CodingResult {
  patient_id: string
  suggested_codes: SuggestedCode[]
  /** Absent from an older server. */
  documentation_queries?: DocumentationQuery[]
  cdi_flags: CdiFlag[]
  reimbursement: Reimbursement
}

interface Props {
  patientId?: string
}

// Rupees with Indian digit grouping (₹12,34,567 — lakh/crore, not thousands).
// The coding model's base rate and code weights are rupee-denominated
// placeholders in coding_service.py; this must agree with them.
const inr = (n: number) =>
  n.toLocaleString('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 })

const prettyType = (t: string) =>
  t.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

export default function RevenueIntegrityPanel({ patientId: patientIdProp }: Props) {
  const [patientId, setPatientId] = useState(patientIdProp || '')
  const [query, setQuery] = useState(patientIdProp || '')
  const [result, setResult] = useState<CodingResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /* Adjusted during render, not in an effect: an effect lands after the commit,
     so the frame that received a new patient still showed — and loaded — the
     previous one's coding suggestions. */
  const [syncedProp, setSyncedProp] = useState(patientIdProp)
  if (patientIdProp !== syncedProp) {
    setSyncedProp(patientIdProp)
    if (patientIdProp) { setPatientId(patientIdProp); setQuery(patientIdProp) }
  }

  const load = useCallback(async (pid: string) => {
    if (!pid) { setResult(null); return }
    setLoading(true)
    setError(null)
    try {
      const res = await apiFetch<CodingResult>(`/coding/patient/${encodeURIComponent(pid)}`)
      setResult(res)
    } catch (err) {
      setError((err as Error).message || 'Failed to load coding suggestions')
      setResult(null)
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { if (patientId) load(patientId) }, [patientId, load])

  const runQuery = () => setPatientId(query.trim())

  const reimb = result?.reimbursement

  return (
    <div className={styles.container}>
      {/* The shared page header (ClinicianShell.css), so Revenue, Command and
          the homes stop each styling the same thing their own way. */}
      <header className="orb-home__header">
        <div>
          <h2 className="orb-home__title">Revenue Integrity</h2>
          <p className="orb-home__context">AI medical coding and clinical documentation improvement</p>
        </div>
        {/* Same fix as Orders: pick the patient, never recall an internal id.
            The old `e.g. PAT001` placeholder 404'd on the very value it
            suggested and left the screen blank. */}
        {!patientIdProp && (
          <div className={styles.patientRow}>
            <PatientPicker
              value={query}
              onChange={setQuery}
              label="Patient"
              hint="Pick a patient to analyse coding & CDI opportunities."
            />
            <button className={styles.goButton} onClick={runQuery} disabled={!query.trim()}>
              Analyse
            </button>
          </div>
        )}
      </header>

      {error && <div className={styles.errorText}>{error}</div>}

      {loading ? (
        <div className={styles.loading}>Analysing documentation…</div>
      ) : !result ? (
        <div className={styles.emptyState}>
          <span className={styles.emptyIcon} aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              {/* Indian rupee (lucide indian-rupee), not a dollar sign. */}
              <path d="M6 3h12" /><path d="M6 8h12" /><path d="m6 13 8.5 8" />
              <path d="M6 13h3" /><path d="M9 13c6.667 0 6.667-10 0-10" />
            </svg>
          </span>
          <div className={styles.emptyTitle}>
            {patientId ? 'Nothing to suggest' : 'No patient selected'}
          </div>
          <div className={styles.emptyDetail}>
            {patientId
              ? 'The documentation on this chart already supports the codes on it. There is no additional capture to suggest.'
              : 'Pick a patient and press Analyse. Orb reads the notes already on the chart and shows the codes they support and the documentation gaps that block them.'}
          </div>
        </div>
      ) : (
        <>
          {/* Headline reimbursement banner */}
          {reimb && (
            <div className={styles.headline}>
              <div className={styles.headlineFlow}>
                <div className={styles.moneyBlock}>
                  <span className={styles.moneyLabel}>Captured today</span>
                  <span className={`${styles.moneyValue} ${styles.captured}`}>{inr(reimb.captured_inr)}</span>
                </div>
                <span className={styles.arrow}>→</span>
                <div className={styles.moneyBlock}>
                  <span className={styles.moneyLabel}>Potential with CDI</span>
                  <span className={`${styles.moneyValue} ${styles.potential}`}>{inr(reimb.potential_inr)}</span>
                </div>
              </div>
              <div className={styles.upliftPill}>
                <span className={styles.upliftLabel}>Capture opportunity</span>
                <span className={styles.upliftValue}>+{inr(reimb.uplift_inr)}</span>
                {!!reimb.specificity_uplift_inr && (
                  <span className={styles.upliftLabel}>
                    includes {inr(reimb.specificity_uplift_inr)} for sharper codes
                  </span>
                )}
              </div>
            </div>
          )}
          {reimb && (
            <div className={styles.illustrative}>
              Illustrative estimate: {reimb.assumptions.model}. Base rate {inr(reimb.assumptions.base_rate_inr)}.
              Not real payer rates; a coder/CDI specialist must confirm before billing.
            </div>
          )}

          <div className={styles.grid}>
            {/* Suggested codes */}
            <div className={styles.section}>
              <div className={styles.sectionTitle}>
                Suggested codes
                <span className={styles.count}>{result.suggested_codes.length}</span>
              </div>
              {result.suggested_codes.length === 0 ? (
                <div className={styles.emptyState}>No codes suggested from current documentation.</div>
              ) : (
                result.suggested_codes.map(c => (
                  <div key={`${c.system}-${c.code}`} className={styles.card}>
                    <div className={styles.codeHead}>
                      <span className={styles.codeChip}>{c.code}</span>
                      <span className={styles.systemChip}>{c.system}</span>
                      <span className={styles.codeDisplay}>{c.display}</span>
                    </div>
                    {c.evidence_sentence && (
                      <blockquote className={styles.evidenceSentence}>
                        <span>&ldquo;{c.evidence_sentence}&rdquo;</span>
                        {c.evidence_note_id != null && (
                          <span className={styles.evidenceSource}>
                            {c.evidence_author ? `Note by ${c.evidence_author}` : 'From a note'}
                          </span>
                        )}
                      </blockquote>
                    )}
                    <div className={styles.codeMeta}>
                      <span className={styles.evidence}>{c.evidence}</span>
                      <span className={styles.confBar} title={`Confidence ${(c.confidence * 100).toFixed(0)}%`}>
                        {(c.confidence * 100).toFixed(0)}%
                        <span className={styles.confTrack}>
                          <span className={styles.confFill} style={{ width: `${c.confidence * 100}%` }} />
                        </span>
                      </span>
                    </div>
                  </div>
                ))
              )}
            </div>

            {/* CDI flags */}
            <div className={styles.section}>
              <div className={styles.sectionTitle}>
                CDI gap flags
                <span className={styles.count}>{result.cdi_flags.length}</span>
              </div>
              {result.cdi_flags.length === 0 ? (
                <div className={styles.emptyState}>No documentation gaps detected.</div>
              ) : (
                result.cdi_flags.map((f, i) => (
                  <div key={i} className={styles.flag}>
                    <div className={styles.flagTop}>
                      <span className={styles.flagType}>{prettyType(f.type)}</span>
                      <span className={styles.flagImpact}>+{inr(f.est_impact_inr)}</span>
                    </div>
                    <div className={styles.flagMessage}>{f.message}</div>
                    <div className={styles.flagRationale}>{f.rationale}</div>
                  </div>
                ))
              )}
            </div>
          </div>

          {/* Documentation queries: never codes, never counted as revenue. */}
          <div className={styles.section}>
            <div className={styles.sectionTitle}>
              Documentation queries
              <span className={styles.count}>{(result.documentation_queries ?? []).length}</span>
            </div>
            {(result.documentation_queries ?? []).length === 0 ? (
              <div className={styles.emptyState}>No medicine points to a condition that is not written down.</div>
            ) : (
              (result.documentation_queries ?? []).map(q => (
                <div key={`${q.system}-${q.code}`} className={styles.query}>
                  <div className={styles.codeHead}>
                    <span className={styles.codeDisplay}>{q.condition}</span>
                    <span className={styles.systemChip}>Would support {q.code}</span>
                  </div>
                  <div className={styles.flagMessage}>Treated with {q.medication}</div>
                  <div className={styles.queryAsk}>{q.message}</div>
                </div>
              ))
            )}
          </div>

          {/* Assumptions disclosure */}
          {reimb && (
            <details className={styles.assumptions}>
              <summary>Reimbursement model & assumptions (editable, illustrative)</summary>
              <ul>
                <li>{reimb.assumptions.method}</li>
                {reimb.assumptions.caveats.map((c, i) => <li key={`cav-${i}`}>{c}</li>)}
                {reimb.assumptions.citations.map((c, i) => <li key={`cit-${i}`}>{c}</li>)}
              </ul>
            </details>
          )}
        </>
      )}
    </div>
  )
}
