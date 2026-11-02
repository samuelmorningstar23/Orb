import { useOperations, useRequestKey } from '../../hooks/useLocalOperations'
import { useState, type FormEvent } from 'react'
import { getUser } from '../../api/client'
import { actOnCssdCycle, actOnCssdPack, createCssdPack, createCssdSet, getCssdCycle, getCssdPack,
  loadCssd, startCssdCycle, type CssdCycle, type CssdPack, type CssdRegister } from '../../api/cssd'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission } from '../../lib/roles'
import { Field, History, Pages, Select, Shell } from './localOperationsUi'

const NOTICE = "Local processing and custody record. Release records an authorized operator's SOP review; Orb does not certify sterility or command equipment."
type Runner = (op: () => Promise<unknown>) => Promise<boolean>
const getRecord = (key: string): Promise<CssdCycle | CssdPack> => key.startsWith('cycle:') ? getCssdCycle(key.slice(6)) : getCssdPack(key.slice(5))
const results = ['pending', 'pass', 'fail'].map(value => ({ value, label: value }))

function SetForm({ busy, run }: { busy: boolean; run: Runner }) {
  const [code, setCode] = useState(''), [name, setName] = useState(''), [items, setItems] = useState(''), [error, setError] = useState('')
  return <details><summary>Define an instrument set</summary><form onSubmit={async event => {
    event.preventDefault(); setError('')
    const parsed = items.split('\n').filter(line => line.trim()).map(line => {
      const [name, qty, ...extra] = line.split('|').map(x => x.trim())
      return { name, quantity: /^\d+$/.test(qty || '') && !extra.length ? Number(qty) : 0 }
    })
    if (!parsed.length || parsed.some(x => !x.name || x.quantity < 1)) { setError('Enter one instrument per line: instrument name | whole-number quantity.'); return }
    if (await run(() => createCssdSet({ code, name, items: parsed }))) { setCode(''); setName(''); setItems('') }
  }}><fieldset disabled={busy}><Field label="Set code" value={code} onChange={setCode} maxLength={64} />
    <Field label="Set name" value={name} onChange={setName} maxLength={200} />
    <Field label="Instrument contents (one name | quantity per line)" value={items} onChange={setItems} area maxLength={20000} />
    {error && <p role="alert">{error}</p>}<button type="submit">Save set definition</button></fieldset></form></details>
}

function PackForm({ data, busy, run }: { data: CssdRegister; busy: boolean; run: Runner }) {
  const [setId, setSetId] = useState(''), [tag, setTag] = useState('')
  return <details><summary>Register a physical pack</summary><form onSubmit={async event => {
    event.preventDefault(); if (await run(() => createCssdPack({ set_id: setId, tag }))) { setSetId(''); setTag('') }
  }}><fieldset disabled={busy}><Select label="Set definition" value={setId} onChange={setSetId} options={data.sets.filter(x => x.active).map(x => ({ value: x.id, label: `${x.code} · ${x.name}` }))} />
    <Field label="Physical pack tag" value={tag} onChange={setTag} maxLength={100} /><button type="submit">Register received pack</button></fieldset></form></details>
}

function CycleForm({ data, busy, run }: { data: CssdRegister; busy: boolean; run: Runner }) {
  const [reference, setReference] = useState(''), [sterilizer, setSterilizer] = useState(''), [sop, setSop] = useState(''), [method, setMethod] = useState('')
  const [packs, setPacks] = useState<string[]>([])
  const request = useRequestKey()
  return <details><summary>Record a processing cycle</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    if (await run(() => startCssdCycle({ reference, sterilizer_reference: sterilizer, sop_reference: sop, method, pack_ids: packs }, request.key.current))) {
      setReference(''); setSterilizer(''); setSop(''); setMethod(''); setPacks([]); request.renew()
    }
  }}><fieldset disabled={busy}><Field label="Unique cycle reference" value={reference} onChange={setReference} maxLength={300} />
    <Field label="Sterilizer equipment reference" value={sterilizer} onChange={setSterilizer} maxLength={300} />
    <Field label="Hospital SOP reference" value={sop} onChange={setSop} maxLength={300} />
    <Field label="Method recorded by operator" value={method} onChange={setMethod} maxLength={300} />
    <fieldset><legend>Packed sets to load</legend>{data.packs.filter(x => x.status === 'packed').map(pack => <label className="local-ops-check" key={pack.id}>
      <input type="checkbox" checked={packs.includes(pack.id)} onChange={e => setPacks(previous => e.target.checked ? [...previous, pack.id] : previous.filter(id => id !== pack.id))} />{pack.tag}
    </label>)}{!data.packs.some(x => x.status === 'packed') && <p>No packs are ready. Record cleaning and packing first.</p>}</fieldset>
    <button type="submit" disabled={!packs.length}>Record cycle start</button></fieldset></form></details>
}

function PackWork({ record, busy, run }: { record: CssdPack; busy: boolean; run: Runner }) {
  const [action, setAction] = useState(''), [note, setNote] = useState(''), [destination, setDestination] = useState('')
  const [reference, setReference] = useState(''), [surgery, setSurgery] = useState('')
  const actions: Record<string, string[]> = { received: ['clean', 'retire'], cleaned: ['pack', 'retire'], packed: ['retire'],
    quarantined: ['reprocess', 'retire'], released: [...(record.available_to_issue ? ['issue'] : []), 'reprocess', 'retire'], issued: ['return'], recall_pending: ['return'] }
  return <section><h2>Pack {record.tag}</h2><p>Status: {record.status.replaceAll('_', ' ')}</p>
    {record.expires_at && <p>Operator-recorded expiry: {formatClinicalDateTime(record.expires_at)} · {record.available_to_issue ? 'Available for custody issue' : 'Unavailable for issue'}</p>}
    {hasPermission('cssd.write', getUser()?.role) && (actions[record.status] || []).length > 0 && <form onSubmit={async event => {
      event.preventDefault()
      if (await run(() => actOnCssdPack(record.id, { action, version: record.version, note,
        ...(action === 'issue' ? { destination, reference, surgery_id: surgery || null } : {}) }))) {
        setAction(''); setNote(''); setDestination(''); setReference(''); setSurgery('')
      }
    }}><fieldset disabled={busy}><Select label="Pack action" value={action} onChange={setAction} options={(actions[record.status] || []).map(value => ({ value, label: value }))} />
      {action === 'issue' && <><Field label="Receiving service / location" value={destination} onChange={setDestination} maxLength={300} />
        <Field label="Custody reference" value={reference} onChange={setReference} maxLength={300} />
        <Field label="Existing surgery ID (optional case traceability)" value={surgery} onChange={setSurgery} required={false} maxLength={100} />
        <p>Case linkage records custody only. It does not assert that the instruments were used on the patient.</p></>}
      <Field label="Processing / custody note" value={note} onChange={setNote} area /><button type="submit" disabled={!action}>Record pack action</button></fieldset></form>}
    <h3>Custody issues</h3>{record.issues?.length ? <ul>{record.issues.map(issue => <li key={issue.id}>{issue.destination} · {issue.reference}
      {issue.surgery_id && ` · Surgery ${issue.surgery_id} · Patient ${issue.patient_id}`} · {issue.returned_at ? 'Returned' : 'Awaiting return'}</li>)}</ul> : <p>No custody issues recorded.</p>}
    <History events={record.events} /></section>
}

function CycleWork({ record, busy, run }: { record: CssdCycle; busy: boolean; run: Runner }) {
  const [action, setAction] = useState(''), [note, setNote] = useState('')
  const [physical, setPhysical] = useState(record.physical_result || 'pending'), [chemical, setChemical] = useState(record.chemical_result || 'pending')
  const [biological, setBiological] = useState(record.biological_result || 'pending'), [basis, setBasis] = useState(record.biological_basis || '')
  const [measurements, setMeasurements] = useState(record.measurements || ''), [expiry, setExpiry] = useState(''), [reference, setReference] = useState('')
  const canRelease = hasPermission('cssd.release', getUser()?.role)
  const actions = record.status === 'running' ? ['complete'] : record.status === 'completed' ? ['record_checks', ...(canRelease ? ['release', 'recall'] : [])]
    : ['released', 'failed'].includes(record.status) && canRelease ? ['recall'] : []
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (await run(() => actOnCssdCycle(record.id, { action, version: record.version, note,
      ...(['complete', 'record_checks'].includes(action) ? { physical_result: physical, chemical_result: chemical,
        biological_result: biological, biological_basis: basis || null, measurements } : {}),
      ...(action === 'release' ? { expires_at: new Date(expiry).toISOString(), release_reference: reference } : {}) }))) { setAction(''); setNote('') }
  }
  return <section><h2>Cycle {record.reference}</h2><dl><dt>Status</dt><dd>{record.status}</dd><dt>Equipment</dt><dd>{record.sterilizer_reference}</dd>
    <dt>SOP</dt><dd>{record.sop_reference}</dd><dt>Recorded method</dt><dd>{record.method}</dd><dt>Physical check</dt><dd>{record.physical_result || 'Not recorded'}</dd>
    <dt>Chemical check</dt><dd>{record.chemical_result || 'Not recorded'}</dd><dt>Biological check</dt><dd>{record.biological_result?.replaceAll('_', ' ') || 'Not recorded'}</dd></dl>
    {record.biological_basis && <p>Recorded biological-check basis: {record.biological_basis}</p>}
    {record.measurements && <p>Recorded measurements: {record.measurements}</p>}
    <h3>Loaded packs</h3><ul>{record.packs?.map(pack => <li key={pack.id}>{pack.tag} · {pack.status.replaceAll('_', ' ')}</li>)}</ul>
    {hasPermission('cssd.write', getUser()?.role) && actions.length > 0 && <form onSubmit={submit}><fieldset disabled={busy}>
      <Select label="Cycle action" value={action} onChange={setAction} options={actions.map(value => ({ value, label: value === 'release' ? 'Record operator release' : value.replaceAll('_', ' ') }))} />
      {['complete', 'record_checks'].includes(action) && <><Select label="Recorded physical check" value={physical} onChange={setPhysical} options={results} />
        <Select label="Recorded chemical check" value={chemical} onChange={setChemical} options={results} />
        <Select label="Recorded biological check" value={biological} onChange={setBiological} options={[...results, { value: 'not_required', label: 'Not required by recorded hospital SOP' }]} />
        {biological === 'not_required' && <Field label="Documented hospital SOP basis for not requiring this check" value={basis} onChange={setBasis} area maxLength={1000} />}
        <Field label="Recorded cycle measurements / log reference" value={measurements} onChange={setMeasurements} area /></>}
      {action === 'release' && <><Field label="Operator-recorded expiry (hospital / browser local time)" value={expiry} onChange={setExpiry} type="datetime-local" />
        <Field label="Release authorization / SOP review reference" value={reference} onChange={setReference} maxLength={500} />
        <p>Record the hospital's authorized review and labelled expiry. Orb supplies neither cycle parameters nor a shelf-life policy.</p></>}
      <Field label="Operator review / reason" value={note} onChange={setNote} area /><button type="submit" disabled={!action}>Record cycle action</button></fieldset></form>}
    <History events={record.events} /></section>
}

export default function CssdPanel() {
  const model = useOperations(loadCssd, getRecord)
  return <Shell title="Sterile supply & instrument custody" notice={NOTICE} {...model}>
    {hasPermission('cssd.manage', getUser()?.role) && <SetForm busy={model.busy} run={model.run} />}
    {model.data && <>{hasPermission('cssd.write', getUser()?.role) && <><PackForm data={model.data} busy={model.busy} run={model.run} /><CycleForm data={model.data} busy={model.busy} run={model.run} /></>}
      <div className="local-ops-grid"><section><h2>Physical packs</h2>{!model.data.packs.length && <p>No physical packs recorded.</p>}
        <ul className="local-ops-list">{model.data.packs.map(pack => <li key={pack.id}><button type="button" disabled={model.busy} aria-pressed={model.selected?.id === pack.id}
          onClick={() => void model.select(`pack:${pack.id}`)}>{pack.tag} · {pack.status.replaceAll('_', ' ')}</button></li>)}</ul>
        <h2>Processing cycles</h2>{!model.data.cycles.length && <p>No processing cycles recorded.</p>}<ul className="local-ops-list">{model.data.cycles.map(cycle => <li key={cycle.id}>
          <button type="button" disabled={model.busy} aria-pressed={model.selected?.id === cycle.id} onClick={() => void model.select(`cycle:${cycle.id}`)}>{cycle.reference} · {cycle.status}</button></li>)}</ul>
        <h2>Set definitions</h2><ul>{model.data.sets.map(set => <li key={set.id}>{set.code} · {set.name}: {set.items.map(item => `${item.name} × ${item.quantity}`).join(', ')}</li>)}</ul>
      </section>{model.selected ? 'tag' in model.selected ? <PackWork key={model.selected.id} record={model.selected} busy={model.busy} run={model.run} />
        : <CycleWork key={`${model.selected.id}:${model.selected.version}`} record={model.selected} busy={model.busy} run={model.run} /> : <section><p>Select a pack or cycle to read and record its processing history.</p></section>}</div></>}
    {model.data && <Pages offset={model.offset} more={model.data.has_more} busy={model.busy || model.loading} page={model.page} />}
  </Shell>
}
