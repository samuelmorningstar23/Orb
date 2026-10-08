/**
 * The patient index search (opd-6a): the first step of registering a visit.
 *
 * Hospital-wide and minimal (ruling OPD-N4): each card shows the UHID, name,
 * sex, age, the last 4 digits of the phone, the last visit date and whether
 * the patient is an inpatient now. Never a date of birth, a full phone number
 * or an address. Choosing a card does NOT attach anything to the record: the
 * caller then asks the patient to confirm who they are (IdentityConfirmDialog).
 *
 * Every search is audited on the server with the ids it showed, so this
 * searches when the desk asks (the button or Enter), not on every keystroke.
 */
import { useState } from 'react'

import {
  isFeatureDisabled,
  searchPatientIndex,
  type IndexCard,
} from '../../../api/registration'
import { formatClinicalDateKey } from '../../../lib/datetime'
import { cardMeta } from './indexCard'
import './RegisterPatientView.css'

interface PatientIndexSearchProps {
  /** The desk picked this record; the caller confirms identity next. */
  onChoose: (card: IndexCard) => void
  /** Offered when nobody matches, or always under the results; absent for an
   *  account that cannot register a new patient. */
  onRegisterNew?: (typed: string) => void
  /** The server's minimum (registration config); 3 until it answers. */
  minChars?: number
  onFeatureOff?: () => void
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'The search did not run. Try again.'
}

export default function PatientIndexSearch({
  onChoose,
  onRegisterNew,
  minChars = 3,
  onFeatureOff,
}: PatientIndexSearchProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<IndexCard[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const typed = query.trim()
  const ready = typed.length >= minChars && !busy

  const search = async (event?: React.FormEvent) => {
    event?.preventDefault()
    if (!ready) return
    setBusy(true)
    setError('')
    try {
      const found = await searchPatientIndex(typed)
      setResults(Array.isArray(found.results) ? found.results : [])
      setTruncated(found.truncated === true)
    } catch (err) {
      if (isFeatureDisabled(err)) {
        onFeatureOff?.()
        return
      }
      setResults(null)
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rpv-card" aria-label="Find a patient">
      <h3 className="rpv-card__title">Find the patient</h3>
      <form className="rpv-search" onSubmit={search} role="search">
        <div className="rpv-field">
          <label htmlFor="rpv-index-query">UHID, full phone number or name</label>
          <input
            id="rpv-index-query"
            type="search"
            value={query}
            autoComplete="off"
            onChange={e => setQuery(e.target.value)}
            placeholder={`At least ${minChars} letters of the name`}
          />
        </div>
        <button type="submit" className="rpv-btn rpv-btn--primary" disabled={!ready}>
          {busy ? 'Searching…' : 'Search'}
        </button>
      </form>
      <p className="rpv-hint">
        Search before registering anyone new: the same person registered twice has their
        allergies and history split across two records.
      </p>

      {error && <p className="rpv-error" role="alert">{error}</p>}

      {results !== null && results.length === 0 && (
        <p className="rpv-empty">
          No patient matches &ldquo;{typed}&rdquo;. Check the spelling, try the UHID or the full
          phone number, or register a new patient.
        </p>
      )}

      {results !== null && results.length > 0 && (
        <ul className="rpv-results" aria-label="Matching patients">
          {results.map(card => (
            <li key={card.patient_id} className="rpv-result">
              <div className="rpv-result__who">
                <span className="rpv-result__name">{card.name}</span>
                <span className="rpv-result__meta">{cardMeta(card)}</span>
                <span className="rpv-result__meta">
                  {card.last_visit_date
                    ? `Last seen ${formatClinicalDateKey(card.last_visit_date)}`
                    : 'No earlier visit'}
                </span>
              </div>
              <div className="rpv-actions">
                {card.inpatient_now && <span className="rpv-tag">Inpatient now</span>}
                <button
                  type="button"
                  className="rpv-btn"
                  onClick={() => onChoose(card)}
                  aria-label={`Choose ${card.name}${card.uhid ? `, UHID ${card.uhid}` : ''}`}
                >
                  Choose
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {truncated && (
        <p className="rpv-hint">
          More patients match than are shown. Type more of the name, or search by UHID or phone.
        </p>
      )}

      {onRegisterNew && results !== null && (
        <div className="rpv-actions">
          <button type="button" className="rpv-btn" onClick={() => onRegisterNew(typed)}>
            Register a new patient
          </button>
        </div>
      )}
    </div>
  )
}
