/**
 * Pick a patient by NAME, bed or MRN — not by internal id.
 *
 * WHY THIS EXISTS. Orders and Revenue Integrity both opened on a bare text
 * field labelled "Patient ID", placeholdered `e.g. PAT001`. Typing exactly the
 * example the product suggested produced `HTTP 404 /orders/patient/PAT001` and
 * an empty screen: real ids in this database look like `PAT-A73E5E98`. So the
 * panel documented an id format it does not accept, and asked a doctor —
 * standing one click away from the ward board that holds every one of these
 * patients — to recall an opaque internal identifier from memory.
 *
 * A picker is the fix, not a better placeholder. The clinician searches the
 * name they already know; the internal id never has to be seen or typed. The
 * raw field is kept as a disclosed fallback (some sites do paste an MRN from
 * another system), and it now validates against the loaded list instead of
 * firing a 404 into an empty screen.
 *
 * Scoping is the SERVER's job: this lists exactly what /api/vigil/patients
 * returns for the signed-in clinician, which is already department-scoped. It
 * shows nothing the caller could not already read.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { getPatients, type Patient } from '../../api/client'
import { useDataChanged } from '../../hooks/useDataChanged'
import { sexShort } from '../../lib/demographics'
import './PatientPicker.css'

interface Props {
  value: string
  onChange: (patientId: string) => void
  /** Shown above the control. */
  label?: string
  /** Rendered under the control when nothing is selected. */
  hint?: string
}

export default function PatientPicker({ value, onChange, label = 'Patient', hint }: Props) {
  const [patients, setPatients] = useState<Patient[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [manual, setManual] = useState(false)
  // What was typed in the fallback field. Kept apart from `value` because a
  // typed MRN resolves to the patient's internal id before it is handed on.
  const [manualText, setManualText] = useState('')
  /* Which option the keyboard is on. -1 = none highlighted, which is the state
     the list opens in so that Enter on a freshly-typed query does not commit a
     patient the clinician never looked at. */
  const [active, setActive] = useState(-1)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)

  const load = () => {
    setLoading(true)
    setLoadError(null)
    getPatients()
      .then(list => setPatients(Array.isArray(list) ? list : []))
      .catch(e => setLoadError(e instanceof Error ? e.message : 'Patient list unavailable.'))
      .finally(() => setLoading(false))
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(load, [])

  // A patient admitted elsewhere in the shell joins the list without a page
  // reload. Quiet: the list on screen stays until the new one arrives, and a
  // failed refresh keeps it, since it is still valid.
  useDataChanged(() => {
    getPatients()
      .then(list => {
        setPatients(Array.isArray(list) ? list : [])
        setLoadError(null)
        // Rows may move under a keyboard highlight; Enter must not commit a
        // patient the clinician did not look at.
        setActive(-1)
      })
      .catch(() => { /* keep the list already shown */ })
  })

  // Click-away closes the list. Without it the dropdown sits over the orders
  // beneath it and the only way out is to pick something.
  useEffect(() => {
    if (!open) return
    const onDocDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    return () => document.removeEventListener('mousedown', onDocDown)
  }, [open])

  const selected = useMemo(
    () => patients.find(p => p.id === value) || null,
    [patients, value],
  )

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return patients
    return patients.filter(p =>
      p.name?.toLowerCase().includes(q)
      || p.bed?.toLowerCase().includes(q)
      || p.mrn?.toLowerCase().includes(q)
      || p.ward?.toLowerCase().includes(q)
      || p.diagnosis?.toLowerCase().includes(q),
    )
  }, [patients, query])

  /** A typed MRN that matches nothing we can see: say so BEFORE the request. */
  const unknownTypedId = manual && manualText !== '' && !selected && patients.length > 0

  // Clinicians know the MRN, never the internal id, so the fallback field takes
  // an MRN and resolves it against the loaded list. A pasted internal id still
  // works, and anything unmatched is passed through as before.
  const onManualChange = (raw: string) => {
    const t = raw.trim()
    setManualText(t)
    const hit = patients.find(p => p.id === t || (!!p.mrn && p.mrn.toLowerCase() === t.toLowerCase()))
    onChange(hit ? hit.id : t)
  }
  const openManual = () => { setManualText(selected?.mrn || ''); setManual(true) }

  /* ── Keyboard ───────────────────────────────────────────────────────────────
   *
   * This control announced itself as `role="combobox"` with `aria-expanded` and
   * `aria-controls` and then answered no key at all: ArrowDown did nothing,
   * Enter did nothing, Escape did nothing. It was mouse-only while claiming
   * otherwise, which is worse than an honest text field — a screen reader tells
   * the user there is a list to walk, and walking it does nothing.
   *
   * `aria-activedescendant` (rather than moving DOM focus into the list) is the
   * right pattern here: focus stays in the input so the clinician can keep
   * typing to narrow, which is how a ward tablet's on-screen keyboard and a
   * barcode wedge both behave.
   */
  const pick = (id: string) => { onChange(id); setQuery(''); setOpen(false); setActive(-1) }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      if (open) { e.preventDefault(); setOpen(false); setActive(-1) }
      return
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) { setOpen(true); setActive(0); return }
      if (matches.length === 0) return
      const step = e.key === 'ArrowDown' ? 1 : -1
      const next = active < 0
        ? (step === 1 ? 0 : matches.length - 1)
        : (active + step + matches.length) % matches.length
      setActive(next)
      // Keep the highlight in view when the list is longer than its box.
      listRef.current?.querySelectorAll('.patient-picker__option')[next]
        ?.scrollIntoView({ block: 'nearest' })
      return
    }
    if (e.key === 'Enter') {
      // Only commit a HIGHLIGHTED option. Enter with nothing highlighted must
      // not silently pick the first match — that is how the wrong chart opens.
      if (open && active >= 0 && matches[active]) {
        e.preventDefault()
        pick(matches[active].id)
      }
      return
    }
    if (e.key === 'Home' && open && matches.length) { e.preventDefault(); setActive(0) }
    if (e.key === 'End' && open && matches.length) { e.preventDefault(); setActive(matches.length - 1) }
  }

  return (
    <div className="patient-picker" ref={boxRef}>
      <label className="patient-picker__label" htmlFor="patient-picker-input">{label}</label>

      {!manual ? (
        <>
          <div className="patient-picker__control">
            <input
              id="patient-picker-input"
              className="patient-picker__input"
              type="text"
              role="combobox"
              aria-expanded={open}
              aria-controls="patient-picker-list"
              aria-autocomplete="list"
              aria-activedescendant={
                open && active >= 0 && matches[active]
                  ? `patient-picker-opt-${matches[active].id}`
                  : undefined
              }
              autoComplete="off"
              value={open ? query : (selected ? `${selected.name} · ${selected.bed || 'no bed'}` : query)}
              onChange={e => { setQuery(e.target.value); setOpen(true); setActive(-1) }}
              onFocus={() => { setQuery(''); setOpen(true); setActive(-1) }}
              onKeyDown={onKeyDown}
              placeholder={loading ? 'Loading patients…' : 'Search by name, bed or MRN'}
              disabled={loading && patients.length === 0}
            />
            {selected && (
              <button
                type="button"
                className="patient-picker__clear"
                onClick={() => { onChange(''); setQuery(''); setOpen(false) }}
                aria-label={`Clear selected patient ${selected.name}`}
              >
                Clear
              </button>
            )}
          </div>

          {open && (
            <ul className="patient-picker__list" id="patient-picker-list" role="listbox" ref={listRef}>
              {loading && <li className="patient-picker__msg">Loading patients…</li>}
              {!loading && matches.length === 0 && (
                <li className="patient-picker__msg">
                  No patient matches “{query}” on your ward list.
                </li>
              )}
              {matches.map((p, i) => (
                <li key={p.id}>
                  <button
                    type="button"
                    id={`patient-picker-opt-${p.id}`}
                    role="option"
                    aria-selected={p.id === value}
                    tabIndex={-1}
                    className={
                      'patient-picker__option'
                      + (p.id === value ? ' patient-picker__option--on' : '')
                      + (i === active ? ' patient-picker__option--active' : '')
                    }
                    onMouseEnter={() => setActive(i)}
                    onClick={() => pick(p.id)}
                  >
                    <span className="patient-picker__opt-name">{p.name}</span>
                    <span className="patient-picker__opt-meta">
                      {[p.bed, p.ward, p.mrn,
                        p.age ? `${p.age}${sexShort(p.sex, '')}` : null]
                        .filter(Boolean).join(' · ')}
                    </span>
                    {p.diagnosis && (
                      <span className="patient-picker__opt-dx">{p.diagnosis}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <div className="patient-picker__control">
          <input
            id="patient-picker-input"
            className="patient-picker__input"
            value={manualText}
            onChange={e => onManualChange(e.target.value)}
            placeholder="Enter the patient's MRN"
            spellCheck={false}
          />
        </div>
      )}

      <div className="patient-picker__foot">
        {loadError ? (
          <span className="patient-picker__error" role="alert">
            Patient list could not be loaded ({loadError}).{' '}
            <button type="button" className="patient-picker__link" onClick={load}>Retry</button>
            {' · '}
            <button type="button" className="patient-picker__link" onClick={openManual}>
              enter an MRN instead
            </button>
          </span>
        ) : unknownTypedId ? (
          <span className="patient-picker__error" role="alert">
            No patient with that MRN is on your ward list. Check the MRN, or search by name.
          </span>
        ) : selected ? (
          <span className="patient-picker__note">
            {selected.name} · {selected.bed || 'no bed'} · {selected.mrn}
          </span>
        ) : (
          <span className="patient-picker__note">{hint || 'Search your ward list by name, bed or MRN.'}</span>
        )}
        {!loadError && (
          <button
            type="button"
            className="patient-picker__link"
            onClick={() => { if (manual) setManual(false); else openManual(); setOpen(false) }}
          >
            {manual ? 'Search by name' : 'Enter MRN'}
          </button>
        )}
      </div>
    </div>
  )
}
