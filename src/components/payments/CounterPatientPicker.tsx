/**
 * Find the patient at the cash counter by name, MRN, bed or ID.
 *
 * WHY NOT common/PatientPicker. The counter took a typed "Patient ID" and the
 * server issued a receipt to whatever was typed. The ward picker cannot replace
 * it here: it lists /api/vigil/patients, which a cashier (no chart permission)
 * cannot read, and it lists admitted patients only, while the final bill is
 * settled at discharge and a refund is paid weeks later. This searches the
 * payments desk's own endpoint, which answers with who and where (name, MRN,
 * bed, ward, status) and nothing clinical, from the caller's own ward when the
 * account has one.
 *
 * Keyboard: the PatientPicker contract. Focus stays in the input,
 * aria-activedescendant walks the list, and Enter commits only a HIGHLIGHTED
 * option, never the first match the clerk did not look at.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { mrnText, searchCounterPatients, type CounterPatient } from '../../api/payments'
import '../common/PatientPicker.css'

const STATUS_LABEL: Record<string, string> = {
  admitted: 'Admitted',
  discharged: 'Discharged',
  transferred: 'Transferred',
}

/** "MRN-GM001 · bed GM-101 · GM-1 · Admitted": enough to tell two people
 *  with the same name apart at the counter. */
function describeCounterPatient(p: CounterPatient): string {
  return [
    p.mrn ? mrnText(p.mrn) : null,
    p.bed ? `bed ${p.bed}` : null,
    p.ward,
    STATUS_LABEL[p.status] ?? p.status,
  ].filter(Boolean).join(' · ')
}

interface Props {
  value: CounterPatient | null
  onChange: (patient: CounterPatient | null) => void
  label?: string
}

export default function CounterPatientPicker({ value, onChange, label = 'Patient' }: Props) {
  const uid = useId()
  const inputId = `${uid}-input`
  const listId = `${uid}-list`
  const optionId = (p: CounterPatient) => `${uid}-opt-${p.id}`

  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  // -1 = nothing highlighted, the state the list opens in, so Enter on a
  // freshly typed name does not commit a patient nobody looked at.
  const [active, setActive] = useState(-1)
  // The answer remembers which text it was for, so a slow reply to "ra" never
  // fills the list under "ravi".
  const [result, setResult] = useState<
    { q: string; patients?: CounterPatient[]; error?: string } | null
  >(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)

  const trimmed = query.trim()
  const searchable = trimmed.length >= 2

  useEffect(() => {
    if (!searchable) return
    let cancelled = false
    const timer = setTimeout(() => {
      searchCounterPatients(trimmed)
        .then(patients => { if (!cancelled) setResult({ q: trimmed, patients }) })
        .catch(e => {
          if (!cancelled) {
            setResult({ q: trimmed, error: e instanceof Error ? e.message : 'The search did not run.' })
          }
        })
    }, 250)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [trimmed, searchable])

  // Click-away closes the list, or it sits over the form beneath it.
  useEffect(() => {
    if (!open) return
    const onDocDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    return () => document.removeEventListener('mousedown', onDocDown)
  }, [open])

  const current = searchable && result && result.q === trimmed ? result : null
  const matches = current?.patients ?? []

  const pick = (p: CounterPatient) => {
    onChange(p)
    setQuery('')
    setOpen(false)
    setActive(-1)
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      if (open) { e.preventDefault(); setOpen(false); setActive(-1) }
      return
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) { setOpen(true); return }
      if (matches.length === 0) return
      const step = e.key === 'ArrowDown' ? 1 : -1
      const next = active < 0
        ? (step === 1 ? 0 : matches.length - 1)
        : (active + step + matches.length) % matches.length
      setActive(next)
      listRef.current?.querySelectorAll('.patient-picker__option')[next]
        ?.scrollIntoView?.({ block: 'nearest' })
      return
    }
    if (e.key === 'Enter') {
      // Only a HIGHLIGHTED option. Enter with nothing highlighted must not
      // pick the first match: that is how money lands on the wrong patient.
      if (open && active >= 0 && matches[active]) {
        e.preventDefault()
        pick(matches[active])
      }
    }
  }

  if (value) {
    return (
      <div className="pay-field" data-testid="counter-patient">
        <span className="pay-field__label">{label}</span>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
          <strong>{value.name}</strong>
          <span className="pay-list__sub">{describeCounterPatient(value)}</span>
        </div>
        <button
          type="button"
          className="pay-btn pay-btn--small"
          onClick={() => onChange(null)}
          aria-label={`Change patient, ${value.name} is selected`}
        >
          Change patient
        </button>
      </div>
    )
  }

  return (
    <div className="pay-field patient-picker" ref={boxRef}>
      <label className="pay-field__label" htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        className="pay-field__input"
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={
          open && active >= 0 && matches[active] ? optionId(matches[active]) : undefined
        }
        autoComplete="off"
        spellCheck={false}
        value={query}
        onChange={e => { setQuery(e.target.value); setOpen(true); setActive(-1) }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder="Search by name, MRN or bed"
      />
      {open && (
        <ul className="patient-picker__list" id={listId} role="listbox" ref={listRef}>
          {!searchable ? (
            <li className="patient-picker__msg">
              Type at least two letters of the name, or the MRN or bed.
            </li>
          ) : current?.error ? (
            <li className="patient-picker__msg patient-picker__error" role="alert">
              The patient search did not run ({current.error}).
            </li>
          ) : !current ? (
            <li className="patient-picker__msg">Searching…</li>
          ) : matches.length === 0 ? (
            <li className="patient-picker__msg">
              No registered patient matches “{trimmed}”.
            </li>
          ) : matches.map((p, i) => (
            <li key={p.id}>
              <button
                type="button"
                id={optionId(p)}
                role="option"
                aria-selected={i === active}
                tabIndex={-1}
                className={
                  'patient-picker__option'
                  + (i === active ? ' patient-picker__option--active' : '')
                }
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(p)}
              >
                <span className="patient-picker__opt-name">{p.name}</span>
                <span className="patient-picker__opt-meta">{describeCounterPatient(p)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
