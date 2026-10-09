/**
 * The consultation's diagnoses (opd-11; ruling OPD-N11): free text first,
 * with a code from the code list when one fits.
 *
 * The search reads GET /api/opd/terminology/search?kind=diagnosis (ICD-10,
 * and SNOMED CT disorders and findings; no patient data). Choosing a result
 * adds its wording and code; "Add as written" adds the typed words with no
 * code. The server checks every code again when the note is saved: one it
 * holds is 'validated', any other is kept exactly as typed and marked
 * 'unvalidated', never refused. Only a validated diagnosis the doctor ticks
 * joins the problem list, and only when the note is signed.
 */
import { useEffect, useId, useRef, useState } from 'react'

import { searchTerminology, type OpdDiagnosis, type TerminologyHit } from '../../../api/opd'
import './ConsultationEditor.css'

/** A diagnosis as the editor holds it before the server has checked it. */
export interface DiagnosisDraft {
  text: string
  code?: string | null
  code_system?: string | null
  code_status?: OpdDiagnosis['code_status']
  add_to_problems?: boolean
}

interface DiagnosisPickerProps {
  value: DiagnosisDraft[]
  onChange: (next: DiagnosisDraft[]) => void
  disabled?: boolean
}

const SEARCH_DEBOUNCE_MS = 250
const MAX_DIAGNOSES = 20

function statusWords(d: DiagnosisDraft): string {
  if (!d.code) return 'No code'
  if (d.code_status === 'validated') return `${d.code_system ?? ''} ${d.code}, in the code list`.trim()
  if (d.code_status === 'unvalidated') return `${d.code_system ?? ''} ${d.code}, not in the code list, kept as written`.trim()
  return `${d.code_system ?? ''} ${d.code}`.trim()
}

export default function DiagnosisPicker({ value, onChange, disabled = false }: DiagnosisPickerProps) {
  const id = useId()
  const [query, setQuery] = useState('')
  /* The answer is kept with the words it answers, so a slow answer for an
     earlier query is never shown under a later one. */
  const [answer, setAnswer] = useState<{ q: string; results: TerminologyHit[]; failed: boolean } | null>(null)
  const seq = useRef(0)
  const full = value.length >= MAX_DIAGNOSES
  const typed = query.trim()
  const current = answer && answer.q === typed ? answer : null
  const results = current?.results ?? []

  useEffect(() => {
    const mine = ++seq.current
    if (typed.length < 2) return undefined
    const timer = setTimeout(() => {
      searchTerminology(typed, 'diagnosis')
        .then(found => {
          if (mine !== seq.current) return
          setAnswer({ q: typed, results: Array.isArray(found?.results) ? found.results : [], failed: false })
        })
        .catch(() => {
          if (mine !== seq.current) return
          setAnswer({ q: typed, results: [], failed: true })
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [typed])

  const add = (d: DiagnosisDraft) => {
    if (full || !d.text.trim()) return
    onChange([...value, { ...d, text: d.text.trim() }])
    setQuery('')
  }
  const update = (index: number, patch: Partial<DiagnosisDraft>) => {
    onChange(value.map((d, i) => (i === index ? { ...d, ...patch } : d)))
  }
  const remove = (index: number) => onChange(value.filter((_, i) => i !== index))

  return (
    <div className="opd-dx" role="group" aria-label="Diagnoses">
      {value.length === 0
        ? <p className="opd-empty">No diagnosis yet.</p>
        : (
          <ul className="opd-dx__list">
            {value.map((d, i) => (
              <li key={`${i}-${d.code ?? ''}`} className="opd-dx__item">
                <div className="opd-field">
                  <label htmlFor={`${id}-dx-${i}`}>Diagnosis {i + 1}</label>
                  <input
                    id={`${id}-dx-${i}`}
                    type="text"
                    value={d.text}
                    maxLength={500}
                    disabled={disabled}
                    onChange={e => update(i, { text: e.target.value })}
                  />
                </div>
                <p className="opd-hint">{statusWords(d)}</p>
                <div className="opd-dx__item-actions">
                  <label className="opd-check">
                    <input
                      type="checkbox"
                      checked={!!d.add_to_problems}
                      disabled={disabled || !d.code}
                      onChange={e => update(i, { add_to_problems: e.target.checked })}
                    />
                    Add to the problem list when signed
                  </label>
                  <button type="button" className="opd-btn opd-btn--quiet" disabled={disabled}
                    onClick={() => remove(i)} aria-label={`Remove ${d.text || `diagnosis ${i + 1}`}`}>
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}

      {!disabled && (
        <div className="opd-dx__find">
          <div className="opd-field">
            <label htmlFor={`${id}-find`}>Add a diagnosis</label>
            <input
              id={`${id}-find`}
              type="search"
              value={query}
              maxLength={120}
              autoComplete="off"
              disabled={full}
              placeholder="Type the diagnosis or a code"
              onChange={e => setQuery(e.target.value)}
            />
          </div>
          {full && <p className="opd-hint">A note holds at most {MAX_DIAGNOSES} diagnoses.</p>}
          {query.trim().length >= 1 && !full && (
            <div className="opd-dx__choices">
              <button type="button" className="opd-btn" onClick={() => add({ text: query })}>
                Add as written, no code
              </button>
              {results.length > 0 && (
                <ul className="opd-dx__results" aria-label="Codes that match">
                  {results.map(r => (
                    <li key={`${r.code_system}-${r.code}`}>
                      <button
                        type="button"
                        className="opd-dx__result"
                        onClick={() => add({ text: r.display, code: r.code, code_system: r.code_system,
                          code_status: 'validated', add_to_problems: false })}
                      >
                        <span className="opd-dx__result-text">{r.display}</span>
                        <span className="opd-dx__result-code">{r.code_system} {r.code}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {current && !current.failed && results.length === 0 && (
                <p className="opd-hint">No code matches. Add the diagnosis as written.</p>
              )}
              {current?.failed && (
                <p className="opd-hint">
                  The code list could not be searched. You can still add the diagnosis as written.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
