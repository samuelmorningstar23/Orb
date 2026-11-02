import { useOperations, useRequestKey } from '../../hooks/useLocalOperations'
import { useState, type FormEvent } from 'react'
import { getUser } from '../../api/client'
import { actOnFeedback, getFeedback, loadFeedback, recordFeedback, type FeedbackCase } from '../../api/feedback'
import OfflinePatientPicker from '../../components/OfflinePatientPicker'
import { hasPermission } from '../../lib/roles'
import { Field, History, Pages, Select, Shell } from './localOperationsUi'

const NOTICE = 'Recorded on this appliance only. No message is sent. Omitting a reporter name does not make the authenticated request untraceable.'

function RecordForm({ busy, run }: { busy: boolean; run: (op: () => Promise<unknown>) => Promise<boolean> }) {
  const [kind, setKind] = useState('complaint'), [channel, setChannel] = useState('in_person')
  const [subject, setSubject] = useState(''), [statement, setStatement] = useState('')
  const [patient, setPatient] = useState(''), [reporter, setReporter] = useState('')
  const [contact, setContact] = useState(''), [rating, setRating] = useState('')
  const request = useRequestKey()
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (await run(() => recordFeedback({ kind: kind as 'complaint' | 'feedback', channel, subject, statement,
      patient_id: patient || null, reporter_name: reporter || null, contact_reference: contact || null,
      rating: rating ? Number(rating) : null }, request.key.current))) {
      setSubject(''); setStatement(''); setPatient(''); setReporter(''); setContact(''); setRating(''); request.renew()
    }
  }
  return <details><summary>Record feedback or a complaint</summary><form onSubmit={submit} onChange={request.renew}>
    <fieldset disabled={busy}><Select label="Type" value={kind} onChange={setKind} options={[{ value: 'complaint', label: 'Complaint' }, { value: 'feedback', label: 'Feedback' }]} />
      <Select label="Received through" value={channel} onChange={setChannel} options={['in_person', 'telephone', 'written', 'other'].map(value => ({ value, label: value.replaceAll('_', ' ') }))} />
      <OfflinePatientPicker value={patient} onChange={id => { setPatient(id); request.renew() }} label="Patient link (optional)" />
      <Field label="Subject" value={subject} onChange={setSubject} maxLength={200} />
      <Field label="Original statement" value={statement} onChange={setStatement} area maxLength={10000} />
      <Field label="Reporter name (optional)" value={reporter} onChange={setReporter} required={false} maxLength={200} />
      <Field label="Contact reference (optional; no message will be sent)" value={contact} onChange={setContact} required={false} maxLength={300} />
      <Select label="Reported rating (optional)" value={rating} onChange={setRating} required={false} options={[1, 2, 3, 4, 5].map(value => ({ value: String(value), label: `${value} of 5` }))} />
      <button type="submit">Save local record</button></fieldset></form></details>
}

function Work({ record, busy, run }: { record: FeedbackCase; busy: boolean; run: (op: () => Promise<unknown>) => Promise<boolean> }) {
  const [action, setAction] = useState(''), [note, setNote] = useState(''), [owner, setOwner] = useState('')
  const actions = record.status === 'closed' ? ['reopen'] : record.status === 'void' ? []
    : ['note', 'assign', ...(record.status === 'recorded' ? ['investigate'] : []),
      ...(['recorded', 'investigating'].includes(record.status) ? ['resolve'] : ['close', 'reopen']), 'void']
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (await run(() => actOnFeedback(record.id, { action, version: record.version, note, ...(action === 'assign' ? { owner_id: owner } : {}) }))) {
      setNote(''); setAction(''); setOwner('')
    }
  }
  return <section><h2>{record.subject}</h2><dl>
    <dt>Status</dt><dd>{record.status}</dd><dt>Patient record</dt><dd>{record.patient_id || 'Not linked'}</dd>
    <dt>Owner</dt><dd>{record.owner_id || 'Unassigned'}</dd><dt>Reporter</dt><dd>{record.reporter_name || 'Not recorded'}</dd>
    <dt>Contact reference</dt><dd>{record.contact_reference || 'Not recorded'}</dd>
    <dt>Reported rating</dt><dd>{record.rating === null ? 'Not recorded' : `${record.rating} of 5`}</dd></dl>
    <h3>Original statement</h3><p>{record.statement}</p>{record.resolution && <><h3>Recorded resolution</h3><p>{record.resolution}</p></>}
    {hasPermission('feedback.manage', getUser()?.role) && actions.length > 0 && <form onSubmit={submit}><fieldset disabled={busy}>
      <Select label="Case action" value={action} onChange={setAction} options={actions.map(value => ({ value, label: value.replaceAll('_', ' ') }))} />
      {action === 'assign' && <Field label="Active owner account ID" value={owner} onChange={setOwner} maxLength={100} />}
      <Field label="Response or reason" value={note} onChange={setNote} area maxLength={10000} />
      <button type="submit" disabled={!action}>Record action</button></fieldset></form>}
    <History events={record.events} /></section>
}

export default function FeedbackPanel() {
  const model = useOperations(loadFeedback, getFeedback)
  return <Shell title="Feedback & complaints" notice={NOTICE} {...model}>
    {hasPermission('feedback.write', getUser()?.role) && <RecordForm busy={model.busy} run={model.run} />}
    {model.data && <div className="local-ops-grid"><section><h2>Register</h2>
      {!model.data.cases.length && <p>No feedback or complaints recorded.</p>}
      <ul className="local-ops-list">{model.data.cases.map(record => <li key={record.id}><button type="button" disabled={model.busy}
        aria-pressed={model.selected?.id === record.id} onClick={() => void model.select(record.id)}>{record.subject} · {record.status}</button></li>)}</ul>
    </section>{model.selected ? <Work key={model.selected.id} record={model.selected} busy={model.busy} run={model.run} /> : <section><p>Select a record to read its statement and history.</p></section>}</div>}
    {model.data && <Pages offset={model.offset} more={model.data.has_more} busy={model.busy || model.loading} page={model.page} />}
  </Shell>
}
