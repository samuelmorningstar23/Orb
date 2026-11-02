import { useEffect, useId, useRef, useState } from 'react'
import { apiFetch } from '../api/client'

interface PatientIdentity { id: string; name: string; mrn: string; status: string }
interface Props { value: string; onChange: (id: string) => void; label?: string; includeRegistered?: boolean }

export default function OfflinePatientPicker({ value, onChange, label = 'Patient', includeRegistered = false }: Props) {
  const id = useId()
  const [query, setQuery] = useState(''), [matches, setMatches] = useState<PatientIdentity[]>([])
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [selected, setSelected] = useState<PatientIdentity | null>(null)
  const generation = useRef(0)
  useEffect(() => {
    let active = true
    const request = generation.current
    if (query.trim().length < 3) return
    const timer = window.setTimeout(() => {
      void apiFetch<{patients: PatientIdentity[]}>(`/local-workflows/patients?q=${encodeURIComponent(query.trim())}&include_registered=${includeRegistered}`)
        .then(result => {if (active && request === generation.current) {setMatches(result.patients); setError('')}})
        .catch((reason: unknown) => {if (active && request === generation.current) {
          setMatches([]); setError(reason instanceof Error ? reason.message : 'Patient search failed.')
          if ([401, 403, 404].includes((reason as {status?: number})?.status || 0)) {++generation.current; setSelected(null); setQuery(''); setBusy(false); onChange('')}
        }})
        .finally(() => {if (active && request === generation.current) setBusy(false)})
    }, 250)
    return () => {active = false; window.clearTimeout(timer)}
  }, [query, includeRegistered, onChange])
  function clear() {++generation.current; setSelected(null); setQuery(''); setMatches([]); setError(''); setBusy(false); onChange('')}
  return <fieldset><legend>{label}</legend><label htmlFor={id}>Search name, hospital number or record ID</label>
    <input id={id} value={query} autoComplete="off" maxLength={120} onChange={event => {
      ++generation.current; setQuery(event.target.value); setMatches([]); setError(''); setBusy(event.target.value.trim().length >= 3)
    }} />
    {busy && <p role="status">Searching…</p>}{error && <p role="alert">{error}</p>}
    {query.trim().length >= 3 && !busy && !error && matches.length === 0 && <p>No matching records.</p>}
    {matches.length > 0 && <ul>{matches.map(patient => <li key={patient.id}><button type="button" onClick={() => {
      ++generation.current; setSelected(patient); onChange(patient.id); setQuery(''); setMatches([]); setError(''); setBusy(false)
    }}>{patient.name} · {patient.mrn}</button></li>)}</ul>}
    {value && <p>Selected record: {selected?.id === value ? `${selected.name} · ${selected.mrn}` : value} <button type="button" onClick={clear}>Clear selection</button></p>}
  </fieldset>
}
