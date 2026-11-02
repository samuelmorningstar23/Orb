import { useId, type ChangeEvent, type ReactNode } from 'react'
import { formatClinicalDateTime } from '../../lib/datetime'
import type { OperationEvent } from '../../api/feedback'
import './LocalOperations.css'

export function Field({ label, value, onChange, required = true, area = false, type = 'text', maxLength = 4000 }:
{ label: string; value: string; onChange: (value: string) => void; required?: boolean; area?: boolean; type?: string; maxLength?: number }) {
  const id = useId()
  const props = { id, value, required, maxLength, onChange: (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(e.target.value) }
  return <label className="local-ops-field" htmlFor={id}>{label}{area ? <textarea {...props} rows={3} /> : <input {...props} type={type} autoComplete="off" />}</label>
}

export function Select({ label, value, onChange, options, required = true }:
{ label: string; value: string; onChange: (value: string) => void; options: { value: string; label: string }[]; required?: boolean }) {
  const id = useId()
  return <label className="local-ops-field" htmlFor={id}>{label}<select id={id} value={value} required={required} onChange={e => onChange(e.target.value)}>
    <option value="">Choose…</option>{options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
  </select></label>
}

export function History({ events = [] }: { events?: OperationEvent[] }) {
  return <section><h3>Recorded history</h3>{events.length ? <ol className="local-ops-history">{events.map(event => <li key={event.id}>
    <strong>{event.action.replaceAll('_', ' ')}</strong> · {formatClinicalDateTime(event.occurred_at)} · {event.actor_id}
    <p>{event.note}</p>
  </li>)}</ol> : <p>No history loaded.</p>}</section>
}

export function Shell({ title, notice, error, loading, busy, refresh, children }:
{ title: string; notice: string; error: string; loading: boolean; busy: boolean; refresh: () => Promise<void>; children: ReactNode }) {
  return <main className="local-ops"><header className="local-ops-header"><h1>{title}</h1>
    <button type="button" disabled={loading || busy} onClick={() => void refresh()}>Refresh register</button></header>
    <p className="local-ops-notice">{notice}</p>
    {error && <p role="alert" className="local-ops-error">{error}</p>}
    {loading && <p role="status">Loading register…</p>}
    {busy && <p role="status">Saving local record…</p>}{children}</main>
}

export function Pages({ offset, more, busy, page }: { offset: number; more: boolean; busy: boolean; page: (offset: number) => void }) {
  return <nav aria-label="Register pages" className="local-ops-header"><button type="button" disabled={busy || !offset} onClick={() => page(offset - 100)}>Previous page</button>
    <span>Page {Math.floor(offset / 100) + 1}</span><button type="button" disabled={busy || !more} onClick={() => page(offset + 100)}>Next page</button></nav>
}
