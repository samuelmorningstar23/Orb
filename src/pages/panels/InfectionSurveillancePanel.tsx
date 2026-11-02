import { useEffect, useState } from 'react'
import { getUser } from '../../api/client'
import { actOnInfectionWindow, classifyInfectionCase, createInfectionRecord, endInfectionIsolation, getInfectionIsolation,
  getInfectionMetrics, getInfectionWindow, loadInfectionSurveillance, voidInfectionRecord,
  type InfectionCase, type InfectionDefinition, type InfectionIsolation, type InfectionMetrics, type InfectionWindowDetail } from '../../api/infectionSurveillance'
import OfflinePatientPicker from '../../components/OfflinePatientPicker'
import { useOperations, useRequestKey } from '../../hooks/useLocalOperations'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission } from '../../lib/roles'
import { Field, History, Pages, Select, Shell } from './localOperationsUi'

type Runner = (operation: () => Promise<unknown>) => Promise<boolean>
const can = (permission: 'read' | 'write' | 'manage') => hasPermission(`infection_surveillance.${permission}`, getUser()?.role)
const NOTICE = 'Manual local surveillance only. Case definitions, population, counting policy and results require hospital evidence. Orb does not infer infection, change care orders, recommend antibiotics or send external reports.'
type Selection = ({kind: 'window'} & InfectionWindowDetail) | ({kind: 'isolation'} & InfectionIsolation)
async function getRecord(key: string): Promise<Selection> {
  const [kind, id, page] = key.split(':')
  return kind === 'window' ? {...await getInfectionWindow(id, Number(page || 0)), kind: 'window'} : {...await getInfectionIsolation(id), kind: 'isolation'}
}
const policies = [{value: 'all_isolates', label: 'All explicitly recorded isolates'}, {value: 'first_per_patient', label: 'First isolate per patient, organism and interpretation cohort'}]

function DefinitionForm({busy, run}: {busy: boolean; run: Runner}) {
  const [fields, setFields] = useState<Record<string, string>>({}), request = useRequestKey()
  const field = (key: string, label: string, maximum = 4000, area = false) => <Field label={label} value={fields[key] || ''} onChange={value => setFields(previous => ({...previous, [key]: value}))} maxLength={maximum} area={area} />
  return <details><summary>Define a surveillance policy</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    if (await run(() => createInfectionRecord('definition', {code: fields.code, name: fields.name, case_definition: fields.definition,
      counting_policy: fields.counting, denominator_unit: fields.unit as 'patient_days' | 'device_days', device_label: fields.device || null, evidence_reference: fields.reference}, request.key.current))) { setFields({}); request.renew() }
  }}><fieldset disabled={busy}>{field('code', 'Definition code', 64)}{field('name', 'Definition name', 200)}
    {field('definition', 'Hospital-authored case definition', 10000, true)}{field('counting', 'Episode / numerator counting policy', 4000, true)}
    <Select label="Defined denominator unit" value={fields.unit || ''} onChange={value => setFields(previous => ({...previous, unit: value}))} options={[{value: 'patient_days', label: 'Explicitly observed patient-days'}, {value: 'device_days', label: 'Explicitly observed device-days'}]} />
    {fields.unit === 'device_days' && field('device', 'Device type defined by this policy', 200)}{field('reference', 'Approved hospital definition reference', 500)}
    <p>Definitions are immutable. A changed definition or counting rule requires a new code and observation window.</p><button type="submit">Save surveillance definition</button></fieldset></form></details>
}

function WindowForm({definitions, busy, run}: {definitions: InfectionDefinition[]; busy: boolean; run: Runner}) {
  const [fields, setFields] = useState<Record<string, string>>({}), request = useRequestKey()
  const field = (key: string, label: string, type = 'text', maximum = 4000) => <Field label={label} type={type} value={fields[key] || ''} onChange={value => setFields(previous => ({...previous, [key]: value}))} maxLength={maximum} />
  return <details><summary>Open an observation window</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    if (await run(() => createInfectionRecord('window', {definition_id: fields.definition, label: fields.label, population_description: fields.population,
      start_date: fields.start, end_date: fields.end, source_reference: fields.source, rate_multiplier: Number(fields.multiplier), minimum_cell: Number(fields.minimum),
      isolate_policy: fields.policy as 'all_isolates' | 'first_per_patient'}, request.key.current))) { setFields({}); request.renew() }
  }}><fieldset disabled={busy}><Select label="Hospital definition" value={fields.definition || ''} onChange={value => setFields(previous => ({...previous, definition: value}))} options={definitions.map(row => ({value: row.id, label: `${row.code} · ${row.name}`}))} />
    {field('label', 'Observation window label', 'text', 200)}{field('population', 'Explicit surveillance population')}
    {field('start', 'Start date (hospital calendar, inclusive)', 'date')}{field('end', 'End date (hospital calendar, inclusive)', 'date')}
    {field('source', 'Population / denominator evidence reference', 'text', 500)}{field('multiplier', 'Hospital-selected rate multiplier (whole number)', 'number')}
    {field('minimum', 'Hospital-selected minimum cell (whole number)', 'number')}
    <Select label="Hospital-selected isolate counting policy" value={fields.policy || ''} onChange={value => setFields(previous => ({...previous, policy: value}))} options={policies} />
    <p>No multiplier, minimum sample or susceptibility threshold is supplied by Orb. A report suppresses an entire group if its tested count or a nonzero S/I/R outcome is below the selected minimum.</p>
    <button type="submit" disabled={!definitions.length}>Save observation window</button></fieldset></form></details>
}

function IsolationForm({busy, run}: {busy: boolean; run: Runner}) {
  const [patient, setPatient] = useState(''), [fields, setFields] = useState<Record<string, string>>({}), request = useRequestKey()
  const field = (key: string, label: string, required = true, type = 'text') => <Field label={label} type={type} required={required} value={fields[key] || ''} onChange={value => setFields(previous => ({...previous, [key]: value}))} />
  return <details><summary>Record an authored isolation start</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    if (await run(() => createInfectionRecord('isolation', {patient_id: patient, case_id: fields.case || null, precautions: fields.precautions,
      started_at: new Date(fields.start).toISOString(), start_reason: fields.reason, authority_reference: fields.reference}, request.key.current))) { setPatient(''); setFields({}); request.renew() }
  }}><fieldset disabled={busy}><OfflinePatientPicker value={patient} onChange={id => { setPatient(id); request.renew() }} />
    {field('case', 'Existing surveillance case ID (optional)', false)}{field('precautions', 'Clinician-authored precautions')}
    {field('start', 'Recorded isolation start (browser local time)', true, 'datetime-local')}{field('reason', 'Authored start reason')}{field('reference', 'Clinical decision / authority reference')}
    <p>This records an existing decision. It does not activate a bed restriction, prescription or clinical isolation order.</p><button type="submit" disabled={!patient}>Record isolation start</button></fieldset></form></details>
}

function EntryForm({window, busy, run}: {window: InfectionWindowDetail; busy: boolean; run: Runner}) {
  const [kind, setKind] = useState(''), [patient, setPatient] = useState(''), [fields, setFields] = useState<Record<string, string>>({}), request = useRequestKey()
  const field = (key: string, label: string, type = 'text', maximum = 4000, area = false) => <Field label={label} type={type} maxLength={maximum} area={area} value={fields[key] || ''} onChange={value => setFields(previous => ({...previous, [key]: value}))} />
  return <details><summary>Record source evidence in this window</summary><form onChange={request.renew} onSubmit={async event => {
    event.preventDefault()
    const common = {window_id: window.id, patient_id: patient, evidence_reference: fields.reference}
    const saved = await run(() => kind === 'case' ? createInfectionRecord('case', {...common, episode_reference: fields.episode, event_date: fields.date, statement: fields.statement}, request.key.current)
      : kind === 'exposure' ? createInfectionRecord('exposure', {...common, observed_date: fields.date, device_reference: fields.device || null, day_count: 1}, request.key.current)
        : createInfectionRecord('susceptibility', {...common, isolate_reference: fields.isolate, specimen_date: fields.date, organism_code: fields.organism,
          specimen_code: fields.specimen, antibiotic_code: fields.antibiotic, result: fields.result as 'S' | 'I' | 'R', standard_reference: fields.standard,
          method: fields.method, validator_reference: fields.validator}, request.key.current))
    if (saved) { setPatient(''); setFields({}); request.renew() }
  }}><fieldset disabled={busy}><Select label="Source record type" value={kind} onChange={value => { setKind(value); setFields({}); request.renew() }} options={[{value: 'case', label: 'Case evidence for manual review'}, {value: 'exposure', label: 'One exactly observed denominator day'}, {value: 'susceptibility', label: 'Validated manual susceptibility result'}]} />
    {kind && <><OfflinePatientPicker value={patient} onChange={id => { setPatient(id); request.renew() }} />{field('date', 'Observed / specimen date (hospital calendar)', 'date')}
      {kind === 'case' && <>{field('episode', 'Distinct episode reference under the counting policy', 'text', 200)}{field('statement', 'Clinician-authored case evidence', 'text', 10000, true)}</>}
      {kind === 'exposure' && <><p>Exactly one observed {window.definition.denominator_unit === 'device_days' ? 'device-day' : 'patient-day'} will be recorded. No census, interval or rounded duration is inferred.</p>
        {window.definition.denominator_unit === 'device_days' && field('device', 'Distinct observed device reference', 'text', 200)}</>}
      {kind === 'susceptibility' && <>{field('isolate', 'Distinct laboratory isolate reference', 'text', 200)}{field('organism', 'Hospital organism code', 'text', 100)}{field('specimen', 'Hospital specimen code', 'text', 100)}{field('antibiotic', 'Hospital antibiotic code', 'text', 100)}
        <Select label="Validated laboratory interpretation" value={fields.result || ''} onChange={value => setFields(previous => ({...previous, result: value}))} options={['S', 'I', 'R'].map(value => ({value, label: value}))} />
        {field('standard', 'Laboratory interpretation standard and edition', 'text', 300)}{field('method', 'Recorded laboratory method', 'text', 300)}{field('validator', 'Validated result / laboratory reviewer reference', 'text', 500)}
        <p>Transcribe the validated interpretation. Orb does not interpret MIC values or combine I with S.</p></>}
      {field('reference', 'Source evidence reference', 'text', 500)}</>}
    <button type="submit" disabled={!kind || !patient}>Record source evidence</button></fieldset></form></details>
}

function CaseReview({row, busy, run}: {row: InfectionCase; busy: boolean; run: Runner}) {
  const [status, setStatus] = useState(''), [reason, setReason] = useState(''), [reviewer, setReviewer] = useState('')
  return <details><summary>Review episode {row.episode_reference} · {row.status.replaceAll('_', ' ')}</summary>
    <p>Patient {row.patient_id} · {row.event_date} · Evidence {row.evidence_reference}</p><p>{row.statement}</p>
    {row.classification_reason && <p>Prior decision: {row.classification_reason} · {row.reviewer_reference}</p>}
    <form onSubmit={async event => { event.preventDefault(); await run(() => classifyInfectionCase(row.id, {version: row.version, status: status as InfectionCase['status'], reason, reviewer_reference: reviewer})) }}><fieldset disabled={busy}>
      <Select label="Authored case classification" value={status} onChange={setStatus} options={['under_review', 'included', 'excluded'].map(value => ({value, label: value.replaceAll('_', ' ')}))} />
      <Field label="Classification reason under this definition" value={reason} onChange={setReason} area />
      <Field label="Clinician decision / reviewer reference" value={reviewer} onChange={setReviewer} maxLength={500} /><button type="submit" disabled={!status}>Record case classification</button></fieldset></form></details>
}

function VoidSource({kind, id, version, busy, run}: {kind: 'exposure' | 'susceptibility'; id: string; version: number; busy: boolean; run: Runner}) {
  const [reason, setReason] = useState('')
  return <details><summary>Correct this source row</summary><form onSubmit={async event => {event.preventDefault(); await run(() => voidInfectionRecord(kind, id, {version, reason}))}}><fieldset disabled={busy}>
    <Field label="Source correction reason" value={reason} onChange={setReason} area /><button type="submit">Void source row with reason</button></fieldset></form></details>
}

function Metrics({id}: {id: string}) {
  const [grouping, setGrouping] = useState('organism,antibiotic'), [report, setReport] = useState<InfectionMetrics | null>(null), [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    getInfectionMetrics(id, grouping).then(value => {if (!cancelled) setReport(value)}, reason => {if (!cancelled) setError(reason instanceof Error ? reason.message : 'Results could not be read.')})
    return () => {cancelled = true}
  }, [id, grouping])
  return <section><h3>Local denominator and antibiogram</h3><Select label="Antibiogram grouping" value={grouping} onChange={value => {setReport(null); setError(''); setGrouping(value)}} options={[{value: 'organism,antibiotic', label: 'Organism + antibiotic'}, {value: 'organism,antibiotic,specimen', label: 'Organism + antibiotic + specimen'}]} />
    {error && <p role="alert">{error}</p>}{!report && !error && <p role="status">Reading reviewed source records…</p>}
    {report && <><p>{report.notice}</p><p>Population: {report.population} · {report.start_date} through {report.end_date} (inclusive).</p>
      <p>Hospital minimum cell: {report.minimum_cell} · Isolate policy: {report.isolate_policy.replaceAll('_', ' ')}. Laboratory standards and methods are kept in separate groups.</p>
      {report.rate.suppressed ? <p>Surveillance rate suppressed by the hospital minimum-cell policy.</p> : <p>Recorded included cases: {report.rate.numerator}; observed denominator days: {report.rate.denominator}. Recorded rate: {report.rate.value ?? 'Unavailable'} per {report.rate.multiplier} {report.definition.denominator_unit.replaceAll('_', ' ')}.</p>}
      <div className="local-ops-table local-ops-antibiogram" role="region" aria-label="Susceptibility results, scroll horizontally for all columns" tabIndex={0}><table><caption>Validated manually transcribed susceptibility results</caption><thead><tr><th>Organism</th><th>Antibiotic</th><th>Specimen</th><th>Standard / method</th><th>Tested</th><th>S / I / R</th><th>S percent</th></tr></thead>
        <tbody>{report.antibiogram.map((cell, index) => <tr key={index}><td>{cell.organism}</td><td>{cell.antibiotic}</td><td>{cell.specimen || 'Combined by selected grouping'}</td><td>{cell.standard_reference} · {cell.method}</td>
          <td>{cell.suppressed ? 'Suppressed' : cell.tested}</td><td>{cell.suppressed ? 'Suppressed' : cell.counts && `${cell.counts.S} / ${cell.counts.I} / ${cell.counts.R}`}</td><td>{cell.suppressed ? 'Suppressed' : `${cell.susceptible_percent}%`}</td></tr>)}</tbody></table></div>
      {!report.antibiogram.length && <p>No validated active susceptibility records in this window.</p>}</>}
  </section>
}

function WindowWork({record, busy, run, select}: {record: InfectionWindowDetail; busy: boolean; run: Runner; select: (key: string) => Promise<void>}) {
  const [reason, setReason] = useState('')
  return <section><h2>{record.label}</h2><p>{record.status} · {record.start_date} through {record.end_date} (inclusive)</p>
    <h3>{record.definition.name}</h3><p>{record.definition.case_definition}</p><p>Counting policy: {record.definition.counting_policy}</p>
    <p>Population: {record.population_description}</p><p>Definition evidence: {record.definition.evidence_reference} · Population evidence: {record.source_reference}</p>
    {record.finalization_note && <p>Completeness attestation: {record.finalization_note}</p>}
    {record.status === 'open' && can('write') && <EntryForm window={record} busy={busy} run={run} />}
    {can('manage') && <form onSubmit={async event => { event.preventDefault(); await run(() => actOnInfectionWindow(record.id, {version: record.version, action: record.status === 'open' ? 'finalize' : 'reopen', reason})) }}><fieldset disabled={busy}>
      <Field label={record.status === 'open' ? 'Population / denominator completeness attestation' : 'Reason to reopen and correct this window'} value={reason} onChange={setReason} area />
      <button type="submit">{record.status === 'open' ? 'Finalize reviewed observation window' : 'Reopen for correction'}</button></fieldset></form>}
    {record.status === 'finalized' ? <Metrics key={`${record.id}:${record.version}`} id={record.id} /> : <p>Rates and antibiogram results remain unavailable until the ended observation window is reviewed and finalized.</p>}
    <h3>Recorded case evidence</h3>{!record.cases.length && <p>No cases on this page.</p>}{record.cases.map(row => record.status === 'open' && can('manage') ? <CaseReview key={`${row.id}:${row.version}`} row={row} busy={busy} run={run} /> : <p key={row.id}>{row.episode_reference} · Patient {row.patient_id} · {row.status} · {row.statement}</p>)}
    <h3>Exact observed denominator days</h3><ul>{record.exposures.map(row => <li key={row.id}>{row.observed_date} · Patient {row.patient_id} · {row.device_reference || 'Patient-day'} · Count {row.day_count} · {row.status} · Evidence {row.evidence_reference}
      {row.void_reason && <p>{row.void_reason}</p>}{record.status === 'open' && row.status === 'active' && can('write') && <VoidSource kind="exposure" id={row.id} version={row.version} busy={busy} run={run} />}</li>)}</ul>
    <h3>Validated manual susceptibility sources</h3><ul>{record.susceptibility.map(row => <li key={row.id}>{row.isolate_reference} · Patient {row.patient_id} · {row.specimen_date} · {row.organism_code} / {row.specimen_code} / {row.antibiotic_code} · {row.result} · {row.standard_reference} · {row.method} · {row.status}
      <p>Evidence {row.evidence_reference} · Validation {row.validator_reference}</p>{row.void_reason && <p>{row.void_reason}</p>}{record.status === 'open' && row.status === 'active' && can('write') && <VoidSource kind="susceptibility" id={row.id} version={row.version} busy={busy} run={run} />}</li>)}</ul>
    <Pages offset={record.offset} more={record.has_more} busy={busy} page={offset => void select(`window:${record.id}:${offset}`)} /><History events={record.events} />
  </section>
}

function IsolationWork({record, busy, run}: {record: InfectionIsolation; busy: boolean; run: Runner}) {
  const [end, setEnd] = useState(''), [reason, setReason] = useState(''), [reference, setReference] = useState('')
  return <section><h2>Authored isolation record</h2><p>Patient {record.patient_id} · {record.status}</p><p>Recorded start: {formatClinicalDateTime(record.started_at)}</p><p>{record.precautions}</p>
    <p>Start reason: {record.start_reason} · Authority: {record.authority_reference}</p>{record.case_id && <p>Surveillance case: {record.case_id}</p>}
    {record.ended_at && <p>Recorded end: {formatClinicalDateTime(record.ended_at)} · {record.end_reason} · {record.end_authority_reference}</p>}
    {record.status === 'active' && can('write') && <form onSubmit={async event => {event.preventDefault(); await run(() => endInfectionIsolation(record.id, {version: record.version, ended_at: new Date(end).toISOString(), reason, authority_reference: reference}))}}><fieldset disabled={busy}>
      <Field label="Recorded isolation end (browser local time)" value={end} onChange={setEnd} type="datetime-local" /><Field label="Authored end reason" value={reason} onChange={setReason} area />
      <Field label="Clinical end decision / authority reference" value={reference} onChange={setReference} maxLength={500} /><button type="submit">Record isolation end</button></fieldset></form>}
    <History events={record.events} /></section>
}

export default function InfectionSurveillancePanel() {
  const model = useOperations(loadInfectionSurveillance, getRecord)
  return <Shell title="Infection surveillance & isolation records" notice={NOTICE} {...model}>
    {can('manage') && <DefinitionForm busy={model.busy} run={model.run} />}{can('write') && <IsolationForm busy={model.busy} run={model.run} />}
    {model.data && <>{can('manage') && <WindowForm definitions={model.data.definitions} busy={model.busy} run={model.run} />}
      <div className="local-ops-grid"><section><h2>Observation windows</h2>{!model.data.windows.length && <p>No observation windows. Define a hospital policy and population before recording surveillance evidence.</p>}
        <ul className="local-ops-list">{model.data.windows.map(row => <li key={row.id}><button type="button" disabled={model.busy} onClick={() => void model.select(`window:${row.id}:0`)}>{row.label} · {row.status}</button></li>)}</ul>
        <h2>Authored isolation register</h2>{!model.data.isolations.length && <p>No authored isolation records.</p>}<ul className="local-ops-list">{model.data.isolations.map(row => <li key={row.id}><button type="button" disabled={model.busy} onClick={() => void model.select(`isolation:${row.id}`)}>Patient {row.patient_id} · {row.status} · {formatClinicalDateTime(row.started_at)}</button></li>)}</ul>
        <h2>Hospital definitions</h2><ul>{model.data.definitions.map(row => <li key={row.id}>{row.code} · {row.name} · {row.denominator_unit.replaceAll('_', ' ')}</li>)}</ul></section>
        {model.selected ? model.selected.kind === 'window' ? <WindowWork key={`${model.selected.id}:${model.selected.version}`} record={model.selected} busy={model.busy} run={model.run} select={model.select} />
          : <IsolationWork key={`${model.selected.id}:${model.selected.version}`} record={model.selected} busy={model.busy} run={model.run} /> : <section><p>Select a window or authored isolation record.</p></section>}
      </div><Pages offset={model.offset} more={model.data.has_more} busy={model.busy || model.loading} page={model.page} /></>}
  </Shell>
}
