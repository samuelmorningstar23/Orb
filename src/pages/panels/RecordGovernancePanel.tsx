import { useCallback, useState, type FormEvent } from 'react'
import OfflinePatientPicker from '../../components/OfflinePatientPicker'
import { getUser } from '../../api/client'
import { createGovernance, getGovernance, listGovernance, transitionGovernance, type GovernanceRecord, type RecordKind } from '../../api/recordGovernance'
import { useOperations } from '../../hooks/useLocalOperations'
import { hasPermission } from '../../lib/roles'
import './RecordGovernancePanel.css'

const labels: Record<RecordKind, string> = {hold: 'Legal holds', file: 'File custody', release: 'Release requests'}
const description = (row: GovernanceRecord) => row.file_number || row.purpose || row.reason || row.id
function actions(kind: RecordKind, row: GovernanceRecord): string[] {
  if (kind === 'hold') return row.released_at ? [] : ['release']
  if (kind === 'release') return row.status === 'requested' ? ['approved', 'denied', 'cancelled'] : row.status === 'approved' ? ['released', 'cancelled'] : []
  return ({archived: ['checked_out', 'in_transit', 'missing'], checked_out: ['archived', 'in_transit', 'missing'], in_transit: ['archived', 'checked_out', 'missing'], missing: ['archived']} as Record<string, string[]>)[row.state || ''] || []
}
const human = (value: string) => value.replaceAll('_', ' ')

export default function RecordGovernancePanel() {
  const [kind, setKind] = useState<RecordKind>('hold'), [patient, setPatient] = useState(''), [notice, setNotice] = useState('')
  const owner = `${kind}:${patient}`
  const loader = useCallback(async (offset: number) => ({...patient ? await listGovernance(kind, patient, offset) : {records: [], total: 0}, owner}), [kind, patient, owner])
  const getter = useCallback(async (id: string) => {
    const detail = await getGovernance(kind, id)
    if (detail.patient_id !== patient) throw new Error('The record does not belong to the selected patient.')
    return {...detail, owner}
  }, [kind, patient, owner])
  const model = useOperations(loader, getter)
  const data = model.data?.owner === owner ? model.data : null
  const selected = model.selected?.owner === owner ? model.selected : null
  const busy = model.busy, loading = model.loading
  const canWrite = hasPermission('record_governance.write', getUser()?.role)
  const canTransition = hasPermission(kind === 'file' ? 'record_governance.write' : 'record_governance.release', getUser()?.role)
  function reset() {model.page(0); setNotice('')}
  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget, data = new FormData(form), value = (name: string) => String(data.get(name) || '').trim()
    const saved = await model.run(() => createGovernance(kind, {patient_id: patient, ...(kind === 'hold'
      ? {reason: value('reason'), authority_reference: value('authority_reference'), categories: []}
      : kind === 'file' ? {file_number: value('file_number'), medium: value('medium') as 'paper' | 'digital_reference', location: value('location'), custodian: value('custodian')}
        : {purpose: value('purpose'), recipient: value('recipient'), authority_reference: value('authority_reference'), documents: value('documents').split('\n').map(v => v.trim()).filter(Boolean)})}))
    if (saved) {form.reset(); setNotice('Record saved.')}
  }
  async function transition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected) return
    const form = event.currentTarget, data = new FormData(form), value = (name: string) => String(data.get(name) || '').trim()
    const saved = await model.run(() => transitionGovernance(kind, selected.id, {version: selected.version, action: value('action'), reason: value('reason'),
      ...(kind === 'file' ? {location: value('location'), custodian: value('custodian')} : {reference: value('reference')})}))
    if (saved) {form.reset(); setNotice('Change saved.')}
  }
  return <section className="record-governance"><h1>Medical records</h1><p>Track file custody, documented legal holds and authorised record handovers.</p>
    <nav aria-label="Record registers">{(Object.keys(labels) as RecordKind[]).map(id => <button type="button" key={id} disabled={busy} aria-pressed={kind === id} onClick={() => {reset(); setKind(id)}}>{labels[id]}</button>)}</nav>
    <OfflinePatientPicker value={patient} onChange={id => {reset(); setPatient(id)}} />
    {model.error && <p role="alert">{model.error}</p>}{notice && <p role="status">{notice}</p>}{loading && <p role="status">Loading records…</p>}
    {patient && data && <div className="record-governance-grid"><div><h2>{labels[kind]}</h2>
      {!loading && data.records.length === 0 && <p>No records in this register for the selected patient.</p>}
      <ul>{data.records.map(row => <li key={row.id}><button type="button" disabled={busy} onClick={() => void model.select(row.id)}>{description(row)} · {human(row.state || row.status || (row.released_at ? 'released' : 'active'))}</button></li>)}</ul>
      <p>{data.total} records</p><button type="button" disabled={model.offset === 0 || loading || busy} onClick={() => model.page(model.offset - 25)}>Previous</button>
      <button type="button" disabled={model.offset + 25 >= data.total || loading || busy} onClick={() => model.page(model.offset + 25)}>Next</button>
      <button type="button" disabled={loading || busy} onClick={() => void model.refresh()}>Refresh register</button>
      {canWrite && <><h2>Create a record</h2><form key={owner} onSubmit={event => void create(event)}><fieldset disabled={busy}>
        {kind === 'hold' && <><p>This hold protects all retention categories until an authorised release is recorded.</p><label>Hold reason<textarea name="reason" required maxLength={4000} /></label></>}
        {kind === 'file' && <><label>File number<input name="file_number" required maxLength={120} /></label><label>Medium<select name="medium"><option value="paper">Paper</option><option value="digital_reference">Digital reference</option></select></label><label>Location<input name="location" required maxLength={200} /></label><label>Custodian<input name="custodian" required maxLength={200} /></label></>}
        {kind === 'release' && <><label>Purpose<textarea name="purpose" required maxLength={4000} /></label><label>Recipient<input name="recipient" required maxLength={300} /></label><label>Document references, one per line<textarea name="documents" required /></label></>}
        {kind !== 'file' && <label>{kind === 'hold' ? 'Hold authority reference' : 'Consent or authority reference'}<input name="authority_reference" required maxLength={300} /></label>}<button type="submit">Save record</button>
      </fieldset></form></>}
    </div>{selected && <article><h2>{description(selected)}</h2><p>Record {selected.id} · revision {selected.version}</p>
      {selected.authority_reference && <p>Authority: {selected.authority_reference}</p>}{selected.location && <p>Location: {selected.location} · custodian: {selected.custodian}</p>}{selected.recipient && <p>Recipient: {selected.recipient}</p>}
      {canTransition && actions(kind, selected).length > 0 && <form key={`${selected.id}:${selected.version}`} onSubmit={event => void transition(event)}><fieldset disabled={busy}>
        <label>Record action<select name="action">{actions(kind, selected).map(action => <option key={action} value={action}>{human(action)}</option>)}</select></label><label>Reason<textarea name="reason" required maxLength={4000} /></label>
        {kind === 'file' ? <><label>New location<input name="location" required maxLength={200} defaultValue={selected.location} /></label><label>New custodian<input name="custodian" required maxLength={200} defaultValue={selected.custodian} /></label></>
          : <label>{kind === 'hold' ? 'Release authority reference' : 'Recipient verification and handover reference'}<input name="reference" required maxLength={300} /></label>}<button type="submit">Record action</button>
      </fieldset></form>}
      <h3>History</h3><ol>{selected.events?.map(event => <li key={event.id}>{human(event.action)} · {event.occurred_at} · {event.actor_id}<p>{event.reason}</p></li>)}</ol><button type="button" disabled={loading || busy} onClick={() => void model.select(selected.id)}>Refresh history</button>
    </article>}</div>}
  </section>
}
