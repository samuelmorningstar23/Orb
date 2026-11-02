import { useOperations, useRequestKey } from '../../hooks/useLocalOperations'
import { useState, type FormEvent } from 'react'
import { getUser } from '../../api/client'
import { actOnMortuaryCase, addMortuarySlot, getMortuaryCase, loadMortuary, recordMortuaryIntake,
  type MortuaryCase, type MortuarySlot } from '../../api/mortuary'
import OfflinePatientPicker from '../../components/OfflinePatientPicker'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission } from '../../lib/roles'
import { Field, History, Pages, Select, Shell } from './localOperationsUi'

const NOTICE = 'Custody register only. Orb does not pronounce death, certify its cause, clear legal restrictions or notify an authority.'
type Runner = (op: () => Promise<unknown>) => Promise<boolean>

function Setup({ busy, run }: { busy: boolean; run: Runner }) {
  const [code, setCode] = useState(''), [name, setName] = useState('')
  return <details><summary>Add storage position</summary><form onSubmit={async event => {
    event.preventDefault(); if (await run(() => addMortuarySlot({ code, name }))) { setCode(''); setName('') }
  }}><fieldset disabled={busy}><Field label="Position code" value={code} onChange={setCode} maxLength={64} />
    <Field label="Position name" value={name} onChange={setName} maxLength={200} /><button type="submit">Add position</button></fieldset></form></details>
}

function Intake({ slots, busy, run }: { slots: MortuarySlot[]; busy: boolean; run: Runner }) {
  const [tag, setTag] = useState(''), [identity, setIdentity] = useState(''), [authority, setAuthority] = useState('')
  const [patient, setPatient] = useState(''), [slot, setSlot] = useState('')
  const request = useRequestKey()
  return <details><summary>Record receipt into custody</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    if (await run(() => recordMortuaryIntake({ body_tag: tag, identity_label: identity, authority_reference: authority,
      patient_id: patient || null, slot_id: slot }, request.key.current))) {
      setTag(''); setIdentity(''); setAuthority(''); setPatient(''); setSlot(''); request.renew()
    }
  }}><fieldset disabled={busy}><Field label="Physical body tag" value={tag} onChange={setTag} maxLength={100} />
    <Field label="Recorded identity or unidentified-body label" value={identity} onChange={setIdentity} maxLength={300} />
    <OfflinePatientPicker value={patient} onChange={id => { setPatient(id); request.renew() }} label="Existing patient link (optional)" />
    <Field label="Existing death / custody authority reference" value={authority} onChange={setAuthority} maxLength={500} />
    <Select label="Free storage position" value={slot} onChange={setSlot} options={slots.filter(x => x.active && !x.occupied).map(x => ({ value: x.id, label: `${x.code} · ${x.name}` }))} />
    <button type="submit">Record receipt</button></fieldset></form></details>
}

function Custody({ record, slots, busy, run }: { record: MortuaryCase; slots: MortuarySlot[]; busy: boolean; run: Runner }) {
  const [action, setAction] = useState(''), [note, setNote] = useState(''), [slot, setSlot] = useState('')
  const [authority, setAuthority] = useState(''), [recipient, setRecipient] = useState(''), [relationship, setRelationship] = useState('')
  const [check, setCheck] = useState(''), [witness, setWitness] = useState('')
  const canRelease = hasPermission('mortuary.release', getUser()?.role)
  const actions = record.status === 'released' ? [] : ['move', ...(record.status === 'stored' ? ['hold', ...(canRelease ? ['release'] : [])] : canRelease ? ['clear_hold'] : [])]
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (await run(() => actOnMortuaryCase(record.id, { action, version: record.version, note,
      ...(action === 'move' ? { slot_id: slot } : {}), ...(action === 'clear_hold' ? { authority_reference: authority } : {}),
      ...(action === 'release' ? { release_authority_reference: authority, recipient_name: recipient,
        recipient_relationship: relationship, identity_check_reference: check, witness_id: witness } : {}) }))) {
      setAction(''); setNote(''); setSlot(''); setAuthority(''); setRecipient(''); setRelationship(''); setCheck(''); setWitness('')
    }
  }
  return <section><h2>{record.body_tag} · {record.identity_label}</h2><dl>
    <dt>Status</dt><dd>{record.status}</dd><dt>Received</dt><dd>{formatClinicalDateTime(record.received_at)}</dd>
    <dt>Storage</dt><dd>{slots.find(x => x.id === record.slot_id)?.code || 'Released from storage'}</dd>
    <dt>Patient record</dt><dd>{record.patient_id || 'Not linked'}</dd><dt>Intake authority</dt><dd>{record.authority_reference}</dd></dl>
    {record.hold_reason && <p>Recorded hold: {record.hold_reason}</p>}
    {record.released_at && <p>Handover recorded {formatClinicalDateTime(record.released_at)} to {record.recipient_name} · {record.release_authority_reference}</p>}
    {hasPermission('mortuary.write', getUser()?.role) && actions.length > 0 && <form onSubmit={submit}><fieldset disabled={busy}>
      <Select label="Custody action" value={action} onChange={setAction} options={actions.map(value => ({ value, label: value === 'release' ? 'Record authorized handover' : value.replaceAll('_', ' ') }))} />
      {action === 'move' && <Select label="New free storage position" value={slot} onChange={setSlot} options={slots.filter(x => x.active && !x.occupied && x.id !== record.slot_id).map(x => ({ value: x.id, label: `${x.code} · ${x.name}` }))} />}
      {(action === 'clear_hold' || action === 'release') && <Field label="Existing clearance / release authority reference" value={authority} onChange={setAuthority} maxLength={500} />}
      {action === 'release' && <><Field label="Recipient name" value={recipient} onChange={setRecipient} maxLength={200} />
        <Field label="Recipient relationship or authority" value={relationship} onChange={setRelationship} maxLength={200} />
        <Field label="Identity check reference (do not copy a full identity document)" value={check} onChange={setCheck} maxLength={300} />
        <Field label="Different authorized witness account ID" value={witness} onChange={setWitness} maxLength={100} />
        <p>The operator records the witness reference. This does not create a separately signed witness statement.</p></>}
      <Field label="Reason / custody note" value={note} onChange={setNote} area />
      <button type="submit" disabled={!action}>Record custody action</button></fieldset></form>}
    <History events={record.events} /></section>
}

export default function MortuaryPanel() {
  const model = useOperations(loadMortuary, getMortuaryCase)
  return <Shell title="Mortuary custody" notice={NOTICE} {...model}>
    {hasPermission('mortuary.manage', getUser()?.role) && <Setup busy={model.busy} run={model.run} />}
    {model.data && <><section><h2>Storage positions</h2><ul>{model.data.slots.map(slot => <li key={slot.id}>{slot.code} · {slot.name}: {slot.occupied ? 'Occupied' : 'Free'}</li>)}</ul>
      {!model.data.slots.length && <p>Add storage positions before recording receipt.</p>}</section>
      {hasPermission('mortuary.write', getUser()?.role) && <Intake slots={model.data.slots} busy={model.busy} run={model.run} />}
      <div className="local-ops-grid"><section><h2>Custody register</h2>{!model.data.cases.length && <p>No custody records.</p>}<ul className="local-ops-list">{model.data.cases.map(record => <li key={record.id}>
        <button type="button" disabled={model.busy} aria-pressed={model.selected?.id === record.id} onClick={() => void model.select(record.id)}>{record.body_tag} · {record.identity_label} · {record.status}</button>
      </li>)}</ul></section>{model.selected ? <Custody key={model.selected.id} record={model.selected} slots={model.data.slots} busy={model.busy} run={model.run} /> : <section><p>Select a custody record.</p></section>}</div></>}
    {model.data && <Pages offset={model.offset} more={model.data.has_more} busy={model.busy || model.loading} page={model.page} />}
  </Shell>
}
