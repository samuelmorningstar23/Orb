import { useEffect, useRef, useState } from 'react'
import { deleteReportDefinition, exportReport, getReportCatalog, getReportDefinitions, runReport, saveReportDefinition, type ReportCatalog, type ReportDefinition, type ReportFilter, type ReportQuery, type ReportResult } from '../../api/reporting'
import { formatInr } from '../../lib/money'
import './ReportingFinance.css'

const words = (value: string) => value.replaceAll('_', ' ')
const message = (value: unknown) => value instanceof Error ? value.message : 'The report could not be completed. Please retry.'

export default function ReportingPanel() {
  const [catalog, setCatalog] = useState<ReportCatalog | null>(null)
  const [saved, setSaved] = useState<ReportDefinition[]>([])
  const [query, setQuery] = useState<ReportQuery>({ dataset: 'patients', mode: 'rows', limit: 200 })
  const [result, setResult] = useState<ReportResult | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [editing, setEditing] = useState<ReportDefinition | undefined>()
  const serial = useRef(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    let active = true
    Promise.all([getReportCatalog(), getReportDefinitions()]).then(([c, definitions]) => {
      if (active) { setCatalog(c); setSaved(definitions); if (c.datasets[0]) setQuery({ dataset: c.datasets[0].id, mode: 'rows', limit: 200 }) }
    }).catch(e => { if (active) setError(message(e)) })
    return () => { active = false; mounted.current = false }
  }, [])
  const dataset = catalog?.datasets.find(d => d.id === query.dataset)
  const change = (next: ReportQuery) => {
    serial.current++; setQuery(next); setResult(null); setError(''); setNotice(''); setBusy(false)
  }
  async function perform<T>(task: () => Promise<T>, apply: (value: T) => void) {
    const ticket = ++serial.current
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await task()
      if (mounted.current && ticket === serial.current) apply(value)
    } catch (e) {
      if (mounted.current && ticket === serial.current) { setResult(null); setSaved([]); setError(message(e)) }
    } finally { if (mounted.current && ticket === serial.current) setBusy(false) }
  }
  const columns = query.columns ?? dataset?.default_columns ?? []
  const filters = query.filters ?? []
  const toggle = (values: string[], key: string) => values.includes(key) ? values.filter(v => v !== key) : [...values, key]
  function filterValue(field: string, op: ReportFilter['op'], raw: string): ReportFilter['value'] {
    if (op === 'is_null') return raw === 'true'
    const numeric = dataset?.fields.find(f => f.id === field)?.kind === 'integer'
    const convert = (v: string) => numeric ? Number(v) : v
    return op === 'in' ? raw.split(',').map(v => convert(v.trim())) : convert(raw)
  }
  const setFilter = (index: number, update: Partial<ReportFilter>) => change({ ...query, filters: filters.map((f, i) => i === index ? { ...f, ...update } : f) })
  return <section className="rf-workbench" aria-labelledby="reporting-title">
    <header><div className="rf-eyebrow">Local administration</div><h1 id="reporting-title">Reports & cohorts</h1><p>Build a report from the records you can access. Preview it here or save a CSV to this computer.</p></header>
    {error && <p className="rf-error" role="alert">{error}</p>}
    {notice && <p className="rf-notice" role="status">{notice}</p>}
    {!catalog ? <p role="status">{error ? 'Reporting is unavailable.' : 'Loading report catalog…'}</p> : <>
      <div className="rf-grid">
        <label>Start from a template<select value="" disabled={busy} onChange={e => { const template = catalog.templates[Number(e.target.value)]; if (template) { change(template.query); setEditing(undefined); setName(template.name) } }}><option value="">Choose a template</option>{catalog.templates.map((t, i) => <option key={t.name} value={i}>{t.name}</option>)}</select></label>
        <label>Your saved definitions<select value={editing?.id ?? ''} disabled={busy} onChange={e => { const definition = saved.find(d => d.id === e.target.value); if (definition) { change(definition.query); setEditing(definition); setName(definition.name) } else { setEditing(undefined); setName('') } }}><option value="">Choose a saved report</option>{saved.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
      </div>
      <form className="rf-card" onSubmit={e => { e.preventDefault(); void perform(() => runReport(query), setResult) }}>
        <div className="rf-grid">
          <label>Dataset<select value={query.dataset} onChange={e => { change({ dataset: e.target.value, mode: 'rows', limit: 200 }); setEditing(undefined) }}>{catalog.datasets.map(d => <option key={d.id} value={d.id}>{d.label}</option>)}</select></label>
          <label>Report view<select value={query.mode ?? 'rows'} onChange={e => change({ ...query, mode: e.target.value as 'rows' | 'summary' })}><option value="rows">Individual records</option><option value="summary">Grouped summary</option></select></label>
          <label>From date<input type="date" value={query.from_day ?? ''} onChange={e => change({ ...query, from_day: e.target.value || undefined })} /></label>
          <label>Through date<input type="date" value={query.to_day ?? ''} onChange={e => change({ ...query, to_day: e.target.value || undefined })} /></label>
          <label>Maximum rows<input type="number" min="1" max="500" step="1" value={query.limit ?? 200} onChange={e => change({ ...query, limit: Number(e.target.value) })} /></label>
        </div>
        <p className="rf-help">Dates follow the hospital calendar. {dataset?.date_required ? 'Leaving both blank uses the last 30 days.' : 'Leaving both blank searches the whole visible cohort.'} A period can span up to 366 days.</p>
        {query.mode === 'summary' ? <div className="rf-grid">
          <fieldset><legend>Group by (up to two)</legend>{dataset?.fields.filter(f => f.groupable).map(f => <label className="rf-check" key={f.id}><input type="checkbox" checked={(query.group_by ?? []).includes(f.id)} onChange={() => change({ ...query, group_by: toggle(query.group_by ?? [], f.id) })} />{words(f.id)}</label>)}</fieldset>
          <fieldset><legend>Measures</legend>{dataset?.measures.map(m => <label className="rf-check" key={m}><input type="checkbox" checked={(query.measures ?? ['count']).includes(m)} onChange={() => change({ ...query, measures: toggle(query.measures ?? ['count'], m) })} />{words(m)}</label>)}</fieldset>
        </div> : <fieldset><legend>Columns (up to twelve)</legend><div className="rf-options">{dataset?.fields.map(f => <label className="rf-check" key={f.id}><input type="checkbox" checked={columns.includes(f.id)} onChange={() => change({ ...query, columns: toggle(columns, f.id) })} />{words(f.id)}</label>)}</div></fieldset>}
        <fieldset><legend>Filters</legend>{filters.map((filter, index) => <div className="rf-filter" key={index}>
          <label>Field<select value={filter.field} onChange={e => setFilter(index, { field: e.target.value, value: dataset?.fields.find(f => f.id === e.target.value)?.kind === 'integer' ? 0 : '' })}>{dataset?.fields.map(f => <option key={f.id} value={f.id}>{words(f.id)}</option>)}{dataset?.cohort_problem_filter && <option value="problem_icd10">Recorded active ICD-10 problem code</option>}</select></label>
          <label>Operator<select value={filter.op} onChange={e => { const op = e.target.value as ReportFilter['op']; setFilter(index, { op, value: op === 'is_null' ? true : op === 'in' ? [] : '' }) }}>{(['eq', 'ne', 'contains', 'in', 'gte', 'lte', 'is_null'] as const).map(op => <option key={op} value={op}>{({ eq: 'Equals', ne: 'Does not equal', contains: 'Contains', in: 'One of', gte: 'At least', lte: 'At most', is_null: 'Is missing' })[op]}</option>)}</select></label>
          <label>Value{filter.op === 'is_null' ? <select value={String(filter.value)} onChange={e => setFilter(index, { value: e.target.value === 'true' })}><option value="true">Missing</option><option value="false">Present</option></select> : <input value={Array.isArray(filter.value) ? filter.value.join(', ') : String(filter.value ?? '')} placeholder={filter.op === 'in' ? 'Comma separated values' : ''} onChange={e => setFilter(index, { value: filterValue(filter.field, filter.op, e.target.value) })} />}</label>
          <button type="button" onClick={() => change({ ...query, filters: filters.filter((_, i) => i !== index) })} aria-label={`Remove filter ${index + 1}`}>Remove</button>
        </div>)}<button type="button" disabled={filters.length >= 10} onClick={() => change({ ...query, filters: [...filters, { field: dataset?.fields[0]?.id ?? '', op: 'eq', value: '' }] })}>Add filter</button></fieldset>
        <div className="rf-actions"><button type="submit" className="rf-primary" disabled={busy}>Preview report</button><button type="button" disabled={busy || !result || result.truncated} onClick={() => void perform(() => exportReport(query), () => setNotice('CSV saved through your browser.'))}>Export CSV</button><span role="status">{busy ? 'Working…' : ''}</span></div>
      </form>
      <form className="rf-card" onSubmit={e => { e.preventDefault(); void perform(async () => { await saveReportDefinition(name, query, editing); return getReportDefinitions() }, definitions => { setSaved(definitions); setEditing(undefined); setNotice('Report definition saved. It will rerun with your current access.') }) }}>
        <label>Definition name<input value={name} maxLength={100} required onChange={e => setName(e.target.value)} /></label><p className="rf-help">Use a non-identifying name. Definitions store the query, never its results. Filters containing patient identifiers or free-text diagnoses cannot be saved.</p>
        <div className="rf-actions"><button disabled={busy || !name.trim()}>{editing ? 'Update saved definition' : 'Save definition'}</button>{editing && <button type="button" disabled={busy} onClick={() => void perform(async () => { await deleteReportDefinition(editing.id); return getReportDefinitions() }, definitions => { setSaved(definitions); setEditing(undefined); setName(''); setNotice('Definition deleted.') })}>Delete definition</button>}</div>
      </form>
      {result && <section className="rf-card" aria-label="Report results"><h2>Report preview</h2><p>{result.rows.length} rows · {result.scope.department ?? 'All permitted departments'}{result.period && ` · ${result.period.from_day} to ${result.period.to_day}`}</p>{result.truncated && <p className="rf-notice">This preview is truncated. Narrow the filters before exporting.</p>}{result.rows.length ? <div className="rf-scroll"><table><thead><tr>{result.columns.map(c => <th key={c}>{words(c)}</th>)}</tr></thead><tbody>{result.rows.map((row, i) => <tr key={i}>{result.columns.map(c => <td key={c}>{row[c] == null ? '—' : c.endsWith('_paise') && typeof row[c] === 'number' ? formatInr(row[c]) : String(row[c])}</td>)}</tr>)}</tbody></table></div> : <p>No records match these filters.</p>}</section>}
      <ul className="rf-help">{catalog.notes.map(n => <li key={n}>{n}</li>)}</ul>
    </>}
  </section>
}
