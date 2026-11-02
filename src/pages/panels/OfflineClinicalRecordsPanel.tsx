import { useEffect, useRef, useState, type FormEvent } from 'react'
import OfflinePatientPicker from '../../components/OfflinePatientPicker'
import {
  addClinicalAddendum, createClinicalRecord, getClinicalHistory, getClinicalMeta, getClinicalRecord, listClinicalRecords,
  listPerioperativeCases, signClinicalRecord, updateClinicalRecord,
  type CareTask, type ClinicalData, type ClinicalDomain, type ClinicalMeta, type ClinicalRecord,
  type Observation, type SurgicalCaseChoice,
} from '../../api/offlineClinicalRecords'
import './OfflineClinicalRecordsPanel.css'

type Draft = Record<string, unknown>
type FieldKind = 'text' | 'textarea' | 'datetime-local' | 'date' | 'number' | 'select'
interface FieldSpec { key: string; label: string; type?: FieldKind; options?: string[]; required?: boolean }
const TITLES: Record<ClinicalDomain, string> = {
  radiology: 'Radiology', 'care-plans': 'Care plans', emergency: 'Emergency records', perioperative: 'Perioperative records', dialysis: 'Dialysis records',
}
const text = (key: string, label: string, required = false): FieldSpec => ({ key, label, required })
const area = (key: string, label: string, required = false): FieldSpec => ({ key, label, required, type: 'textarea' })
const time = (key: string, label: string, required = false): FieldSpec => ({ key, label, required, type: 'datetime-local' })
const number = (key: string, label: string): FieldSpec => ({ key, label, type: 'number' })
const FIELDS: Record<ClinicalDomain, FieldSpec[]> = {
  radiology: [
    { key: 'modality', label: 'Modality', type: 'select', options: ['X-ray', 'CT', 'MRI', 'Ultrasound', 'Other'], required: true },
    text('body_site', 'Body site', true), text('accession', 'Accession / local study number'), text('order_id', 'Existing imaging order ID (optional)'),
    { key: 'priority', label: 'Recorded priority', type: 'select', options: ['routine', 'urgent', 'stat'] }, area('indication', 'Indication'),
    time('scheduled_at', 'Scheduled time'), time('arrived_at', 'Arrival time'), time('performed_at', 'Performed time'),
    text('performer', 'Performed by'), area('findings', 'Report findings'), area('impression', 'Report impression'),
  ],
  'care-plans': [text('problem', 'Problem / focus', true), area('goal', 'Care goal', true), time('review_at', 'Review due'),
    area('evaluation', 'Plan evaluation'), area('closure_reason', 'Closure reason')],
  emergency: [time('arrival_at', 'Arrival time', true), area('complaint', 'Presenting complaint', true),
    text('triage_category', 'Category recorded by staff'), time('triaged_at', 'Triage time'), area('triage_note', 'Triage assessment'),
    time('seen_at', 'Seen time'), text('clinician', 'Seen by'), area('assessment', 'Clinical assessment'),
    { key: 'disposition', label: 'Recorded disposition', type: 'select', options: ['admission_requested', 'discharged_from_ed', 'referred', 'left', 'other'] },
    time('disposition_at', 'Disposition time'), area('disposition_note', 'Disposition and handover note')],
  perioperative: [],
  dialysis: [text('access', 'Access', true), area('indication', 'Indication'), time('scheduled_at', 'Scheduled time'),
    time('started_at', 'Session started'), time('ended_at', 'Session finished'), number('actual_duration_minutes', 'Actual duration (minutes)'),
    number('actual_fluid_ml', 'Actual fluid removed (mL)'), area('complications', 'Complications / recorded response'), area('handover', 'Completion and handover')],
}
const PERIOP_FIELDS: Record<string, FieldSpec[]> = {
  anaesthesia: [area('assessment', 'Pre-anaesthesia assessment'), text('technique', 'Recorded technique'), area('airway', 'Airway record'),
    time('started_at', 'Anaesthesia started'), time('ended_at', 'Anaesthesia ended'), area('handover', 'Handover'), area('notes', 'Additional record')],
  pacu: [time('started_at', 'Recovery arrival'), time('ended_at', 'Recovery departure'), area('assessment', 'Recovery assessment'),
    area('handover', 'Disposition and handover'), area('notes', 'Additional record')],
  implant: [text('device_name', 'Implant / device', true), text('manufacturer', 'Manufacturer'), text('model', 'Model'), text('lot', 'Lot / batch'),
    text('serial', 'Serial number'), { key: 'expiry_date', label: 'Expiry date', type: 'date' }, text('site', 'Implant site', true),
    time('implanted_at', 'Implanted time'), area('notes', 'Traceability note')],
}
const PRESCRIPTION_FIELDS = [text('modality', 'Prescribed modality', true), number('duration_minutes', 'Prescribed duration (minutes)'),
  number('target_fluid_ml', 'Prescribed fluid target (mL)'), number('blood_flow_ml_min', 'Prescribed blood flow (mL/min)'),
  text('dialysate', 'Dialysate'), area('anticoagulation', 'Anticoagulation as prescribed'), area('instructions', 'Prescriber instructions')]

function initialData(domain: ClinicalDomain): Draft {
  if (domain === 'radiology') return { modality: '', body_site: '', priority: 'routine' }
  if (domain === 'care-plans') return { problem: '', goal: '', tasks: [] }
  if (domain === 'emergency') return { arrival_at: new Date().toISOString(), complaint: '', observations: [] }
  if (domain === 'perioperative') return { record_type: 'anaesthesia', observations: [] }
  return { access: '', prescription: null, observations: [] }
}
function localTime(value: unknown) {
  if (typeof value !== 'string' || !value) return ''
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return ''
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}
function message(reason: unknown) { return reason instanceof Error ? reason.message : 'The record could not be saved.' }

function Fields({ specs, data, onChange, disabled }: { specs: FieldSpec[]; data: Draft; onChange: (next: Draft) => void; disabled: boolean }) {
  return <div className="ocr-fields">{specs.map(field => {
    const value = field.type === 'datetime-local' ? localTime(data[field.key]) : String(data[field.key] ?? '')
    const change = (raw: string) => {
      let next: unknown = raw
      if (field.type === 'number') next = raw === '' ? null : Number(raw)
      else if (field.type === 'datetime-local') next = raw ? new Date(raw).toISOString() : null
      else if (!raw && !field.required && field.type !== 'textarea') next = null
      onChange({ ...data, [field.key]: next })
    }
    return <label key={field.key}>{field.label}
      {field.type === 'textarea'
        ? <textarea value={value} disabled={disabled} required={field.required} maxLength={8000} onChange={event => change(event.target.value)} />
        : field.type === 'select'
          ? <select value={value} disabled={disabled} required={field.required} onChange={event => change(event.target.value)}>
            <option value="">Not recorded</option>{field.options?.map(option => <option key={option} value={option}>{option.replaceAll('_', ' ')}</option>)}
          </select>
          : <input type={field.type ?? 'text'} step={field.type === 'number' ? 'any' : undefined} value={value} disabled={disabled}
            required={field.required} maxLength={field.type ? undefined : 200} onChange={event => change(event.target.value)} />}
    </label>
  })}</div>
}

function Observations({ value, change, disabled }: { value: Observation[]; change: (next: Observation[]) => void; disabled: boolean }) {
  return <fieldset><legend>Recorded observations</legend>
    {value.length === 0 && <p>No observations recorded.</p>}
    {value.map((observation, index) => <div className="ocr-entry" key={index}>
      <Fields specs={[time('at', 'Observation time', true), text('blood_pressure', 'Blood pressure as recorded'), number('pulse', 'Pulse / min'),
        number('respiratory_rate', 'Respiratory rate / min'), number('spo2', 'SpO₂ (%)'), number('temperature_c', 'Temperature (°C)'), area('note', 'Observation note')]}
      data={{ ...observation }} disabled={disabled} onChange={next => change(value.map((old, i) => i === index ? next as unknown as Observation : old))} />
      {!disabled && <button type="button" onClick={() => change(value.filter((_, i) => i !== index))}>Remove unsaved observation {index + 1}</button>}
    </div>)}
    {!disabled && value.length < 100 && <button type="button" onClick={() => change([...value, { at: new Date().toISOString() }])}>Add observation</button>}
  </fieldset>
}

function Tasks({ value, change, disabled }: { value: CareTask[]; change: (next: CareTask[]) => void; disabled: boolean }) {
  return <fieldset><legend>Interventions and evaluation</legend>
    {value.length === 0 && <p>No interventions recorded.</p>}
    {value.map((task, index) => <div className="ocr-entry" key={task.id}>
      <Fields specs={[text('description', 'Intervention', true), text('owner', 'Responsible staff', true), time('due_at', 'Due time'),
        { key: 'state', label: 'Intervention outcome', type: 'select', options: ['planned', 'done', 'not_done'], required: true },
        time('completed_at', 'Outcome time'), area('evaluation', 'Intervention evaluation')]}
      data={{ ...task }} disabled={disabled} onChange={next => change(value.map((old, i) => i === index ? next as unknown as CareTask : old))} />
      {!disabled && <button type="button" onClick={() => change(value.filter((_, i) => i !== index))}>Remove unsaved intervention {index + 1}</button>}
    </div>)}
    {!disabled && value.length < 40 && <button type="button" onClick={() => change([...value, { id: crypto.randomUUID(), description: '', owner: '', state: 'planned' }])}>Add intervention</button>}
  </fieldset>
}

export default function OfflineClinicalRecordsPanel({ domain }: { domain: ClinicalDomain }) {
  const [meta, setMeta] = useState<ClinicalMeta | null>(null)
  const [patient, setPatient] = useState('')
  const [records, setRecords] = useState<ClinicalRecord[]>([])
  const [record, setRecord] = useState<ClinicalRecord | null>(null)
  const [data, setData] = useState<Draft>(() => initialData(domain))
  const [title, setTitle] = useState('')
  const [state, setState] = useState('')
  const [surgery, setSurgery] = useState('')
  const [cases, setCases] = useState<SurgicalCaseChoice[]>([])
  const [caseQuery, setCaseQuery] = useState('')
  const [caseOffset, setCaseOffset] = useState(0)
  const [reason, setReason] = useState('')
  const [addendum, setAddendum] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [offset, setOffset] = useState(0)
  const [editing, setEditing] = useState(false)
  const createKey = useRef(crypto.randomUUID())
  const generation = useRef(0)
  const terminal = !!record && !!meta?.terminal_states.includes(record.state)
  const disabled = busy || !meta?.can_write || terminal

  useEffect(() => {
    let live = true
    void getClinicalMeta(domain).then(result => { if (live) { setMeta(result); setError('') } })
      .catch((failure: unknown) => { if (live) setError(message(failure)) })
    return () => { live = false }
  }, [domain, refresh])
  useEffect(() => {
    let live = true
    if (domain === 'perioperative') void listPerioperativeCases(caseQuery, caseOffset).then(result => { if (live) setCases(result.cases) })
      .catch((failure: unknown) => { if (live) setError(message(failure)) })
    return () => { live = false }
  }, [domain, refresh, caseQuery, caseOffset])
  useEffect(() => {
    let live = true
    if (meta) void listClinicalRecords(domain, patient || undefined, offset).then(result => { if (live) setRecords(result.records) })
      .catch((failure: unknown) => { if (live) setError(message(failure)) })
    return () => { live = false }
  }, [domain, meta, patient, refresh, offset])

  function reset(nextPatient = patient) {
    generation.current += 1
    setPatient(nextPatient); setOffset(0); setRecord(null); setData(initialData(domain)); setTitle(''); setState(meta?.initial_state ?? '')
    setSurgery(''); setReason(''); setAddendum(''); setNotice(''); setError(''); setEditing(false); createKey.current = crypto.randomUUID()
  }
  async function open(id: string) {
    const current = ++generation.current
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await getClinicalRecord(domain, id)
      if (current !== generation.current) return
      setRecord(result); setPatient(result.patient_id); setOffset(0); setData({ ...result.data }); setTitle(result.title)
      setState(result.state); setSurgery(result.surgery_id ?? ''); setReason(''); setAddendum(''); setEditing(true)
    } catch (failure) { if (current === generation.current) setError(message(failure)) }
    finally { if (current === generation.current) setBusy(false) }
  }
  async function mutation(work: () => Promise<ClinicalRecord>) {
    const current = generation.current
    setBusy(true); setError(''); setNotice('')
    try {
      const saved = await work()
      if (current !== generation.current) return
      const result = await getClinicalRecord(domain, saved.id)
      if (current !== generation.current) return
      setRecord(result); setData({ ...result.data }); setTitle(result.title); setState(result.state)
      setReason(''); setAddendum(''); setNotice('Record saved.'); setOffset(0); setRefresh(n => n + 1)
    } catch (failure) { if (current === generation.current) setError(message(failure)) }
    finally { if (current === generation.current) setBusy(false) }
  }
  function save(event: FormEvent) {
    event.preventDefault()
    const payload = data as unknown as ClinicalData
    void mutation(() => record
      ? updateClinicalRecord(domain, record.id, { version: record.version, title, state, data: payload, reason })
      : createClinicalRecord(domain, { patient_id: patient, surgery_id: surgery || null, title, data: payload }, createKey.current))
  }
  async function olderHistory() {
    if (!record || record.history_next_offset == null) return
    const current = generation.current
    setBusy(true); setError('')
    try {
      const result = await getClinicalHistory(domain, record.id, record.history_next_offset)
      if (current === generation.current) setRecord(previous => previous ? { ...previous, history: [...(previous.history ?? []), ...result.history], history_next_offset: result.history_next_offset } : previous)
    } catch (failure) { if (current === generation.current) setError(message(failure)) }
    finally { if (current === generation.current) setBusy(false) }
  }
  const periopType = String(data.record_type ?? 'anaesthesia')
  const shownFields = domain === 'perioperative' ? PERIOP_FIELDS[periopType] ?? [] : FIELDS[domain]
  const observations = Array.isArray(data.observations) ? data.observations as Observation[] : []
  const tasks = Array.isArray(data.tasks) ? data.tasks as CareTask[] : []
  const prescription = data.prescription && typeof data.prescription === 'object' ? data.prescription as Draft : null

  return <section className="ocr-panel" aria-label={TITLES[domain]}>
    <header><h1>{TITLES[domain]}</h1><p>Records entered by staff and kept on this hospital computer.</p></header>
    {domain === 'emergency' && <p className="ocr-notice">Record the category and disposition assessed by the treating team. Use the admission or discharge workflow separately when needed.</p>}
    {domain === 'dialysis' && <p className="ocr-notice">Record the clinician’s prescription and what happened during the session. This screen does not operate a dialysis machine.</p>}
    {error && <div role="alert">{error} <button type="button" onClick={() => setRefresh(n => n + 1)}>Retry / reload</button></div>}
    {notice && <p role="status">{notice}</p>}
    {!meta && !error && <p role="status">Loading documentation module…</p>}
    {meta && <>
      <OfflinePatientPicker value={patient} onChange={id => { reset(id); setBusy(false) }} includeRegistered={domain === 'emergency'} />
      <div className="ocr-actions"><button type="button" disabled={busy || (!patient && domain !== 'perioperative') || !meta.can_write} onClick={() => { reset(); setEditing(true) }}>New record</button>
        <button type="button" disabled={busy} onClick={() => { setOffset(0); setRefresh(n => n + 1) }}>Refresh worklist</button></div>
      <section aria-label="Documentation worklist"><h2>{patient ? 'This patient’s records' : 'Accessible records'}</h2>
        {records.length === 0 ? <p>No records found.</p> : <ul className="ocr-worklist">{records.map(item => <li key={item.id}>
          <button type="button" disabled={busy} onClick={() => void open(item.id)}>{item.title}</button>
          <span>{item.patient_name ?? item.patient_id} · {item.state.replaceAll('_', ' ')} · {new Date(item.updated_at).toLocaleString()}</span>
        </li>)}</ul>}
        {(offset > 0 || records.length === 50) && <div className="ocr-actions">
          <button type="button" disabled={busy || offset === 0} onClick={() => setOffset(n => Math.max(0, n - 50))}>Previous records</button>
          <span>Records {offset + (records.length ? 1 : 0)}–{offset + records.length}</span>
          <button type="button" disabled={busy || records.length < 50} onClick={() => setOffset(n => n + 50)}>Next records</button>
        </div>}
      </section>
      {editing && <form onSubmit={save} className="ocr-editor">
        <h2>{record ? 'Recorded documentation' : 'New documentation'}</h2>
        {record && <p>Revision {record.version}{record.signed_at ? ` · Signed by ${record.signed_by} at ${new Date(record.signed_at).toLocaleString()}` : ''}</p>}
        <label>Record title<input value={title} maxLength={200} required disabled={disabled} onChange={event => setTitle(event.target.value)} /></label>
        {domain === 'perioperative' && <>
          {!record && <><label>Find surgical case<input maxLength={200} value={caseQuery} disabled={busy} onChange={event => { setCaseQuery(event.target.value); setCaseOffset(0) }} /></label>
            <div className="ocr-actions"><button type="button" disabled={busy || caseOffset === 0} onClick={() => setCaseOffset(n => Math.max(0, n - 100))}>Previous cases</button>
              <button type="button" disabled={busy || cases.length < 100} onClick={() => setCaseOffset(n => n + 100)}>Next cases</button></div></>}
          <label>Surgical case<select value={surgery} required disabled={disabled || !!record} onChange={event => {
            const selected = cases.find(item => item.id === event.target.value)
            setSurgery(event.target.value); if (selected) setPatient(selected.patient_id)
          }}><option value="">Select an existing case</option>{cases.map(item => <option key={item.id} value={item.id}>{item.patient_name} · {item.procedure_name} · {item.scheduled_date}</option>)}
          {record && !cases.some(item => item.id === surgery) && <option value={surgery}>{surgery}</option>}</select></label>
          <Fields specs={[{ key: 'record_type', label: 'Record type', type: 'select', options: ['anaesthesia', 'pacu', 'implant'], required: true }]}
            data={data} onChange={setData} disabled={disabled || !!record} />
        </>}
        <Fields specs={shownFields} data={data} onChange={setData} disabled={disabled} />
        {domain === 'care-plans' && <Tasks value={tasks} change={next => setData({ ...data, tasks: next })} disabled={disabled} />}
        {['emergency', 'dialysis'].includes(domain) || (domain === 'perioperative' && periopType !== 'implant')
          ? <Observations value={observations} change={next => setData({ ...data, observations: next })} disabled={disabled} /> : null}
        {domain === 'dialysis' && <fieldset><legend>Clinician’s prescription</legend>
          {!prescription && <p>No prescription recorded.</p>}
          {prescription && <Fields specs={PRESCRIPTION_FIELDS} data={prescription} onChange={next => setData({ ...data, prescription: next })} disabled={disabled || !meta.can_prescribe} />}
          {!disabled && meta.can_prescribe && <button type="button" onClick={() => setData({ ...data, prescription: prescription ? null : { modality: '', duration_minutes: null } })}>{prescription ? 'Remove unsaved prescription' : 'Record prescription'}</button>}
        </fieldset>}
        {record && <label>Workflow stage<select value={state} disabled={disabled} onChange={event => setState(event.target.value)}>
          {meta.states.filter(item => item !== 'signed' || item === record.state).map(item => <option key={item} value={item}>{item.replaceAll('_', ' ')}</option>)}
        </select></label>}
        {!disabled && <label>Change / cancellation reason<input value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} /></label>}
        <div className="ocr-actions">{!disabled && <button type="submit" disabled={!patient || !title.trim()}>{busy ? 'Saving…' : record ? 'Save revision' : 'Create record'}</button>}
          {record && meta.can_sign && !terminal && <button type="button" disabled={busy || record.state !== (domain === 'radiology' ? 'reported' : 'completed')}
            onClick={() => void mutation(() => signClinicalRecord(domain, record.id, record.version))}>Sign final record</button>}
          <button type="button" disabled={busy} onClick={() => reset()}>Close record</button></div>
      </form>}
      {record && terminal && meta.can_write && <form onSubmit={event => { event.preventDefault(); void mutation(() => addClinicalAddendum(domain, record.id, record.version, addendum, reason)) }}>
        <h2>Addendum to final record</h2><label>Addendum<textarea required maxLength={8000} value={addendum} onChange={event => setAddendum(event.target.value)} disabled={busy} /></label>
        <label>Reason<input required maxLength={1000} value={reason} onChange={event => setReason(event.target.value)} disabled={busy} /></label>
        <button disabled={busy || !addendum.trim() || !reason.trim()} type="submit">Append addendum</button>
      </form>}
      {record?.history && <section aria-label="Record history"><h2>Record history</h2><ol>{record.history.map(item => <li key={item.version}>
        Revision {item.version} · {item.kind} · {item.actor_id} · {new Date(item.recorded_at).toLocaleString()}
        {item.reason && <p>{item.reason}</p>}{item.snapshot.text && <p>{item.snapshot.text}</p>}
        {item.snapshot.data && <details><summary>View this saved revision</summary>
          <p>{item.snapshot.title}</p><SavedValues value={item.snapshot.data as unknown as Draft} /></details>}
      </li>)}</ol>{record.history_next_offset != null && <button type="button" disabled={busy} onClick={() => void olderHistory()}>Load older revisions</button>}</section>}
    </>}
  </section>
}

function SavedValues({ value }: { value: Draft }) {
  const labels = Object.fromEntries([...Object.values(FIELDS).flat(), ...Object.values(PERIOP_FIELDS).flat(), ...PRESCRIPTION_FIELDS,
    text('at', 'Observation time'), text('blood_pressure', 'Blood pressure'), text('pulse', 'Pulse / min'),
    text('respiratory_rate', 'Respiratory rate / min'), text('spo2', 'SpO₂ (%)'), text('temperature_c', 'Temperature (°C)'),
    text('description', 'Intervention'), text('owner', 'Responsible staff'), text('due_at', 'Due time'), text('completed_at', 'Outcome time'),
  ].map(field => [field.key, field.label]))
  return <dl>{Object.entries(value).filter(([key, entry]) => key !== 'id' && entry !== null && entry !== undefined && entry !== '').map(([key, entry]) =>
    <div key={key}><dt>{labels[key] ?? key.replaceAll('_', ' ')}</dt><dd>
      {Array.isArray(entry) ? entry.map((child, i) => <SavedValues key={i} value={child as Draft} />)
        : typeof entry === 'object' ? <SavedValues value={entry as Draft} /> : String(entry)}
    </dd></div>)}</dl>
}
