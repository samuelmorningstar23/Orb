import { useCallback, useState, type FormEvent } from 'react'
import { apiFetch, getUser } from '../../api/client'
import { useOperations } from '../../hooks/useLocalOperations'
import { hasPermission } from '../../lib/roles'
import './RecordGovernancePanel.css'

interface Asset {id: number; asset_tag: string; name: string; serial_number: string | null}
interface Evidence {id: string; record_kind: 'licence' | 'officer'; reference: string; authority: string; valid_from: string; valid_to: string; scope_description: string; officer_name: string | null; qualification_reference: string | null; date_status: string; state: string; version: number; asset: Asset}
interface Event {action: string; reason: string; occurred_at: string; actor_id: string}
interface Register {records: Evidence[]; total: number; assets: Asset[]; asset_total: number; asset_offset: number; asset_limit: number; asset_next_offset: number | null}
const human = (value: string) => value.replaceAll('_', ' ')
const getEvidence = async (id: string) => {
  const result = await apiFetch<{record: Evidence; events: Event[]}>(`/radiation-safety/${encodeURIComponent(id)}`)
  return {...result, id: result.record.id}
}

export default function RadiationSafetyPanel() {
  const [kind, setKind] = useState('licence'), [notice, setNotice] = useState('')
  const [assetQuery, setAssetQuery] = useState(''), [assetSearch, setAssetSearch] = useState(''), [assetOffset, setAssetOffset] = useState(0), [assetId, setAssetId] = useState('')
  const loader = useCallback((offset: number) => apiFetch<Register>(`/radiation-safety?limit=50&offset=${offset}&asset_query=${encodeURIComponent(assetSearch)}&asset_offset=${assetOffset}&asset_limit=50`), [assetSearch, assetOffset])
  const model = useOperations(loader, getEvidence)
  const {data, selected, busy, loading} = model
  const canWrite = hasPermission('radiation_safety.write', getUser()?.role)
  function pageAssets(offset: number) {setAssetId(''); setAssetOffset(offset); model.page(model.offset)}
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget, data = new FormData(form), value = (name: string) => String(data.get(name) || '').trim()
    setNotice('')
    const saved = await model.run(() => apiFetch('/radiation-safety', {method: 'POST', body: JSON.stringify({asset_id: Number(assetId), record_kind: kind,
      reference: value('reference'), authority: value('authority'), valid_from: value('valid_from'), valid_to: value('valid_to'), scope_description: value('scope_description'),
      ...(kind === 'officer' ? {officer_name: value('officer_name'), qualification_reference: value('qualification_reference')} : {})})}))
    if (saved) {form.reset(); setAssetId(''); setNotice('Document evidence recorded.')}
  }
  async function withdraw(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected) return
    const reason = String(new FormData(event.currentTarget).get('reason') || '')
    setNotice('')
    if (await model.run(() => apiFetch(`/radiation-safety/${encodeURIComponent(selected.id)}/withdraw`, {method: 'POST', body: JSON.stringify({version: selected.record.version, reason})}))) setNotice('Withdrawal recorded.')
  }
  return <section className="record-governance"><h1>Radiation safety documents</h1>
    <p>Record equipment licences and safety officer appointment evidence. Check the source documents and their scope before equipment use; date labels reflect the dates entered here.</p>
    {model.error && <p role="alert">{model.error}</p>}{notice && <p role="status">{notice}</p>}{loading && <p role="status">Loading register…</p>}
    {data && <div className="record-governance-grid"><div><h2>Recorded evidence</h2><ul>{data.records.map(row => <li key={row.id}><button type="button" disabled={busy} onClick={() => void model.select(row.id)}>{row.asset.asset_tag} · {row.reference} · {human(row.date_status)}</button></li>)}</ul>
      {!loading && data.records.length === 0 && <p>No equipment documents recorded.</p>}<p>{data.total} records</p>
      <button type="button" disabled={model.offset === 0 || busy || loading} onClick={() => model.page(model.offset - 50)}>Previous</button><button type="button" disabled={model.offset + 50 >= data.total || busy || loading} onClick={() => model.page(model.offset + 50)}>Next</button>
      <button type="button" disabled={busy || loading} onClick={() => void model.refresh()}>Refresh register</button>
      {canWrite && <><h2>Find registered equipment</h2><form onSubmit={event => {event.preventDefault(); setAssetSearch(assetQuery.trim()); pageAssets(0)}}><fieldset disabled={busy || loading}>
        <label>Search equipment tag, name or serial number<input value={assetQuery} onChange={event => setAssetQuery(event.target.value)} maxLength={200} /></label><button type="submit">Search equipment</button></fieldset></form>
        <p>{data.asset_total} matching equipment records · page {Math.floor(data.asset_offset / data.asset_limit) + 1}</p>
        <button type="button" disabled={busy || loading || data.asset_offset === 0} onClick={() => pageAssets(Math.max(0, data.asset_offset - data.asset_limit))}>Previous equipment page</button>
        <button type="button" disabled={busy || loading || data.asset_next_offset == null} onClick={() => pageAssets(data.asset_next_offset!)}>Next equipment page</button>
        <h2>Record document evidence</h2><form onSubmit={event => void save(event)}><fieldset disabled={busy}>
          <label>Equipment<select name="asset_id" required value={assetId} onChange={event => setAssetId(event.target.value)}><option value="" disabled>Choose registered equipment</option>{data.assets.map(asset => <option key={asset.id} value={asset.id}>{asset.asset_tag} · {asset.name} · {asset.serial_number || 'Serial not recorded'}</option>)}</select></label>
          <label>Document type<select value={kind} onChange={event => setKind(event.target.value)}><option value="licence">Equipment licence</option><option value="officer">Safety officer appointment</option></select></label>
          <label>Document reference<input name="reference" required maxLength={300} /></label><label>Issuing authority<input name="authority" required maxLength={300} /></label>
          <label>Valid from<input type="date" name="valid_from" required /></label><label>Valid through<input type="date" name="valid_to" required /></label>
          {kind === 'officer' && <><label>Officer name<input name="officer_name" required maxLength={200} /></label><label>Qualification or approval reference<input name="qualification_reference" required maxLength={300} /></label></>}
          <label>Scope as recorded on the source document<textarea name="scope_description" required maxLength={4000} /></label><button type="submit" disabled={!assetId}>Record evidence</button>
        </fieldset></form></>}
    </div>{selected && <article><h2>{selected.record.reference}</h2><p>{selected.record.asset.asset_tag} · {selected.record.authority}</p><p>{selected.record.valid_from} through {selected.record.valid_to} · {human(selected.record.date_status)}</p><p>{selected.record.scope_description}</p>
      {selected.record.officer_name && <p>Officer: {selected.record.officer_name} · {selected.record.qualification_reference}</p>}
      {canWrite && selected.record.state === 'recorded' && <form key={`${selected.id}:${selected.record.version}`} onSubmit={event => void withdraw(event)}><fieldset disabled={busy}><label>Reason for withdrawal<textarea name="reason" required maxLength={4000} /></label><button type="submit">Withdraw recorded evidence</button></fieldset></form>}
      <h3>History</h3><ol>{selected.events.map((event,index) => <li key={index}>{human(event.action)} · {event.occurred_at} · {event.actor_id}<p>{event.reason}</p></li>)}</ol>
    </article>}</div>}
  </section>
}
