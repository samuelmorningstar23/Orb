import { useEffect, useRef, useState } from 'react'
import { approvePayoutStatement, attributeFinanceCharge, cancelPayoutStatement, createJournalExport, createPayoutRule, createPayoutStatement, downloadJournal, downloadPayoutStatement, getFinanceCharges, getFinanceMeta, getJournalExports, getPayoutStatement, getPayoutStatements, markPayoutPaid, previewJournal, previewPayout, retirePayoutRule, saveAccountMapping, type FinanceCharge, type FinanceMeta, type FinancePeriod, type JournalPreview, type JournalExportSummary, type PayoutPreview, type PayoutRuleInput, type PayoutStatement } from '../../api/financeTools'
import { newId } from '../../lib/id'
import { formatInr, toPaise } from '../../lib/money'
import './ReportingFinance.css'

type View = 'journal' | 'accounts' | 'attribution' | 'rules' | 'payouts'
const words = (value: string) => value.replaceAll('_', ' ')
const message = (value: unknown) => value instanceof Error ? value.message : 'The finance operation could not be completed. Please retry.'

export default function FinanceToolsPanel() {
  const [meta, setMeta] = useState<FinanceMeta | null>(null)
  const [view, setView] = useState<View>('journal')
  const [period, setPeriod] = useState<FinancePeriod>({ facility_id: 1, from_day: '', to_day: '' })
  const [journal, setJournal] = useState<JournalPreview | null>(null)
  const [exports, setExports] = useState<JournalExportSummary[]>([])
  const [payout, setPayout] = useState<PayoutPreview | null>(null)
  const [charges, setCharges] = useState<FinanceCharge[]>([])
  const [selectedCharge, setSelectedCharge] = useState<FinanceCharge | null>(null)
  const [statements, setStatements] = useState<PayoutStatement[]>([])
  const [statement, setStatement] = useState<PayoutStatement | null>(null)
  const [doctor, setDoctor] = useState('')
  const [attributionDoctor, setAttributionDoctor] = useState('')
  const [reason, setReason] = useState('')
  const [accountRole, setAccountRole] = useState('')
  const [accountCode, setAccountCode] = useState('')
  const [accountName, setAccountName] = useState('')
  const [ruleDoctor, setRuleDoctor] = useState('')
  const [selector, setSelector] = useState('')
  const [selectorValue, setSelectorValue] = useState('')
  const [ruleMode, setRuleMode] = useState('')
  const [basis, setBasis] = useState('')
  const [recognition, setRecognition] = useState('')
  const [rate, setRate] = useState('')
  const [effectiveFrom, setEffectiveFrom] = useState('')
  const [effectiveTo, setEffectiveTo] = useState('')
  const [deduction, setDeduction] = useState('0.00')
  const [deductionReason, setDeductionReason] = useState('')
  const [reference, setReference] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const serial = useRef(0)
  const mounted = useRef(true)
  const exportKey = useRef(newId())
  const payoutKey = useRef(newId())
  useEffect(() => {
    mounted.current = true
    let active = true
    Promise.all([getFinanceMeta(), getPayoutStatements()]).then(([m, list]) => { if (active) { setMeta(m); setStatements(list) } }).catch(e => { if (active) setError(message(e)) })
    return () => { active = false; mounted.current = false }
  }, [])
  function clearRecords() { setJournal(null); setPayout(null); setCharges([]); setSelectedCharge(null); setStatement(null) }
  function changePeriod(next: FinancePeriod) {
    serial.current++; setPeriod(next); clearRecords(); setError(''); setNotice(''); setBusy(false)
    exportKey.current = newId(); payoutKey.current = newId()
  }
  function changePayout() { serial.current++; setPayout(null); setBusy(false); payoutKey.current = newId() }
  async function perform<T>(task: () => Promise<T>, apply: (result: T) => void) {
    const ticket = ++serial.current
    setBusy(true); setError(''); setNotice('')
    try { const result = await task(); if (mounted.current && ticket === serial.current) apply(result) }
    catch (e) { if (mounted.current && ticket === serial.current) { clearRecords(); setStatements([]); setExports([]); setError(message(e)) } }
    finally { if (mounted.current && ticket === serial.current) setBusy(false) }
  }
  async function newRule() {
    if (!ruleDoctor || !selector || !ruleMode || !basis || !recognition || !effectiveFrom) throw new Error('Choose every rule setting explicitly.')
    const amount = toPaise(rate)
    const body: PayoutRuleInput = {
      facility_id: period.facility_id, doctor_id: ruleDoctor,
      selector_type: selector as PayoutRuleInput['selector_type'], selector_value: selector === 'all' ? '' : selectorValue,
      mode: ruleMode as PayoutRuleInput['mode'], basis: basis as PayoutRuleInput['basis'], recognition: recognition as PayoutRuleInput['recognition'],
      effective_from: effectiveFrom, ...(effectiveTo ? { effective_to: effectiveTo } : {}),
      ...(ruleMode === 'percentage' ? { percent_bp: amount } : { fixed_paise: amount }),
    }
    await createPayoutRule(body)
    return getFinanceMeta(period.facility_id)
  }
  const refreshStatement = (next: PayoutStatement) => { setStatement(next); setStatements(list => [next, ...list.filter(s => s.id !== next.id)]); setPayout(null); payoutKey.current = newId() }
  const validPeriod = Boolean(period.from_day && period.to_day)
  return <section className="rf-workbench" aria-labelledby="finance-tools-title">
    <header><div className="rf-eyebrow">Local administration</div><h1 id="finance-tools-title">Accounting & doctor payouts</h1><p>Prepare balanced source journals and explicitly attributed payout statements. Downloads stay local; recording a payment here attests to payment made elsewhere.</p></header>
    {error && <p className="rf-error" role="alert">{error}</p>}{notice && <p className="rf-notice" role="status">{notice}</p>}
    {!meta ? <p role="status">{error ? 'Finance tools are unavailable.' : 'Loading finance configuration…'}</p> : <>
      <nav className="rf-tabs" aria-label="Finance views">{([['journal', 'Accounting journal'], ['accounts', 'Account mappings'], ['attribution', 'Doctor attribution'], ['rules', 'Payout rules'], ['payouts', 'Payout statements']] as const).map(([id, label]) => <button key={id} aria-pressed={view === id} onClick={() => setView(id)}>{label}</button>)}</nav>
      {(['journal', 'attribution', 'payouts'] as View[]).includes(view) && <div className="rf-card rf-grid"><label>From date<input type="date" value={period.from_day} onChange={e => changePeriod({ ...period, from_day: e.target.value })} /></label><label>Through date<input type="date" value={period.to_day} onChange={e => changePeriod({ ...period, to_day: e.target.value })} /></label><p className="rf-help">Hospital calendar · up to 366 days. Source limits require a shorter period when exceeded.</p></div>}
      {view === 'journal' && <>
        <div className="rf-actions"><button className="rf-primary" disabled={busy || !validPeriod} onClick={() => void perform(() => previewJournal(period), setJournal)}>Preview journal</button><button disabled={busy || !journal?.can_export} onClick={() => void perform(async () => { const created = await createJournalExport(period, exportKey.current); await downloadJournal(created.id); return created }, created => setNotice(`Journal CSV saved. Snapshot ${created.id}; SHA-256 ${created.content_sha256}`))}>Create snapshot & export CSV</button></div>
        <section className="rf-card"><h2>Recorded export snapshots</h2><button disabled={busy} onClick={() => void perform(() => getJournalExports(period.facility_id), setExports)}>Refresh export history</button>{exports.length > 0 && <div className="rf-scroll"><table><thead><tr><th>Period</th><th>Debit / credit</th><th>Snapshot</th><th>Download</th></tr></thead><tbody>{exports.map(item => <tr key={item.id}><td>{item.from_day} – {item.to_day}</td><td>{formatInr(item.debit_paise)} / {formatInr(item.credit_paise)}</td><td>{item.id.slice(0, 8)}<p className="rf-help">SHA-256 {item.content_sha256}</p></td><td><button disabled={busy} onClick={() => void perform(() => downloadJournal(item.id), () => setNotice('Recorded journal snapshot saved through your browser.'))}>Download {item.id.slice(0, 8)}</button></td></tr>)}</tbody></table></div>}<p className="rf-help">Recorded snapshots retain the exact source rows and account mapping from creation.</p></section>
        {journal && <section className="rf-card" aria-label="Journal preview"><h2>Balanced source journal</h2><p className="rf-amount">Debit {formatInr(journal.debit_paise)} · Credit {formatInr(journal.credit_paise)}</p><p className="rf-help">Invoices, cancellations, original receipts and reversals remain separate events. A deposit application moves an advance to receivables without adding cash.</p>{journal.issues.length > 0 && <p className="rf-error">Map these accounts before export: {[...new Set(journal.issues.map(i => i.account_role))].join(', ')}.</p>}{journal.rows.length ? <div className="rf-scroll"><table><thead><tr><th>Document</th><th>Event</th><th>Account</th><th>Debit</th><th>Credit</th><th>Supplier</th></tr></thead><tbody>{journal.rows.map(row => <tr key={`${row.entry_key}:${row.line_no}`}><td>{row.source_number}</td><td>{words(row.source_type)}</td><td>{row.account_code ?? 'Not mapped'} · {row.account_name ?? words(row.account_role)}</td><td>{formatInr(row.debit_paise)}</td><td>{formatInr(row.credit_paise)}</td><td>{row.supplier_legal_name ?? 'Not snapshotted'}{row.source_notice && <p className="rf-help">{row.source_notice}</p>}</td></tr>)}</tbody></table></div> : <p>No journal sources in this period.</p>}</section>}
      </>}
      {view === 'accounts' && <>
        <form className="rf-card" onSubmit={e => { e.preventDefault(); void perform(async () => { await saveAccountMapping({ facility_id: period.facility_id, account_role: accountRole, account_code: accountCode, account_name: accountName }); return getFinanceMeta(period.facility_id) }, next => { setMeta(next); setJournal(null); exportKey.current = newId(); setNotice('Account mapping saved. Existing export snapshots retain their recorded mapping.') }) }}><h2>Map a journal role to your ledger</h2><div className="rf-grid"><label>Journal role<select required value={accountRole} onChange={e => { const selected = meta.mappings.find(m => m.account_role === e.target.value); setAccountRole(e.target.value); setAccountCode(selected?.account_code ?? ''); setAccountName(selected?.account_name ?? '') }}><option value="">Choose an account role</option>{meta.account_roles.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}</select></label><label>Your account code<input required value={accountCode} maxLength={60} onChange={e => setAccountCode(e.target.value)} /></label><label>Your account name<input required value={accountName} maxLength={100} onChange={e => setAccountName(e.target.value)} /></label></div><div className="rf-actions"><button disabled={busy} className="rf-primary">Save mapping</button></div></form>
        <div className="rf-card rf-scroll"><table><thead><tr><th>Role</th><th>Account code</th><th>Account name</th></tr></thead><tbody>{meta.account_roles.map(role => { const mapping = meta.mappings.find(m => m.account_role === role.id); return <tr key={role.id}><td>{role.label}</td><td>{mapping?.account_code ?? 'Not configured'}</td><td>{mapping?.account_name ?? 'Not configured'}</td></tr> })}</tbody></table></div>
      </>}
      {view === 'attribution' && <>
        <p>Choose the earning doctor for each charge from an explicit record. The person who entered the charge is never assumed to be the earning doctor. Attribution used by an approved payout is locked.</p>
        <button className="rf-primary" disabled={busy || !validPeriod} onClick={() => void perform(() => getFinanceCharges(period), result => { setCharges(result.charges); setSelectedCharge(null) })}>Load charge worklist</button>
        {selectedCharge && <form className="rf-card" onSubmit={e => { e.preventDefault(); void perform(async () => { await attributeFinanceCharge(selectedCharge.id, { facility_id: period.facility_id, doctor_id: attributionDoctor, reason, ...(selectedCharge.attribution ? { version: selectedCharge.attribution.version } : {}) }); return getFinanceCharges(period) }, result => { setCharges(result.charges); setSelectedCharge(null); setPayout(null); payoutKey.current = newId(); setNotice('Explicit attribution recorded.') }) }}><h2>Attribute {selectedCharge.description}</h2><p>{selectedCharge.patient_name} · Charge {selectedCharge.id}</p><div className="rf-grid"><label>Earning doctor<select value={attributionDoctor} required onChange={e => setAttributionDoctor(e.target.value)}><option value="">Choose a doctor</option>{meta.doctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label><label>Attribution evidence or reason<textarea required maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label></div><div className="rf-actions"><button className="rf-primary" disabled={busy}>Save attribution</button><button type="button" disabled={busy} onClick={() => setSelectedCharge(null)}>Close</button></div></form>}
        <div className="rf-card rf-scroll"><table><thead><tr><th>Patient</th><th>Charge</th><th>Gross</th><th>Earning doctor</th><th>Action</th></tr></thead><tbody>{charges.map(charge => <tr key={charge.id}><td>{charge.patient_name}</td><td>{charge.description} · {words(charge.status)}</td><td>{formatInr(charge.gross_paise)}</td><td>{charge.attribution?.doctor_name ?? charge.attribution?.doctor_id ?? 'Not attributed'}</td><td><button disabled={busy} onClick={() => { setSelectedCharge(charge); setAttributionDoctor(charge.attribution?.doctor_id ?? ''); setReason(charge.attribution?.reason ?? '') }}>Attribute charge {charge.id}</button></td></tr>)}</tbody></table>{!charges.length && <p>Load a period to review its charge attributions.</p>}</div>
      </>}
      {view === 'rules' && <>
        <form className="rf-card" onSubmit={e => { e.preventDefault(); void perform(newRule, next => { setMeta(next); setPayout(null); payoutKey.current = newId(); setRate(''); setNotice('Payout rule recorded. Existing approved statements retain their snapshots.') }) }}><h2>Configure an explicit payout rule</h2><div className="rf-grid">
          <label>Doctor<select required value={ruleDoctor} onChange={e => setRuleDoctor(e.target.value)}><option value="">Choose a doctor</option>{meta.doctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
          <label>Applies to<select required value={selector} onChange={e => setSelector(e.target.value)}><option value="">Choose coverage</option><option value="service">A service code</option><option value="category">A service category</option><option value="all">All services</option></select></label>
          {selector && selector !== 'all' && <label>Existing {selector === 'service' ? 'service code' : 'category'}<input required value={selectorValue} maxLength={100} onChange={e => setSelectorValue(e.target.value)} /></label>}
          <label>Amount method<select required value={ruleMode} onChange={e => { setRuleMode(e.target.value); setRate('') }}><option value="">Choose a method</option><option value="percentage">Percentage of selected basis</option><option value="fixed">Fixed amount per invoice line</option></select></label>
          <label>Basis<select required value={basis} onChange={e => setBasis(e.target.value)}><option value="">Choose a basis</option><option value="gross_ex_tax">Gross before discount, excluding GST</option><option value="net_ex_tax">After discount, excluding GST</option></select></label>
          <label>Recognition<select required value={recognition} onChange={e => setRecognition(e.target.value)}><option value="">Choose recognition</option><option value="billed">When billed</option><option value="fully_collected">When invoice is fully collected</option></select></label>
          <label>{ruleMode === 'percentage' ? 'Share percentage (0–100)' : 'Fixed amount in rupees'}<input required inputMode="decimal" value={rate} onChange={e => setRate(e.target.value)} placeholder="Enter explicitly" /></label>
          <label>Effective from<input type="date" required value={effectiveFrom} onChange={e => setEffectiveFrom(e.target.value)} /></label><label>Effective through (optional)<input type="date" value={effectiveTo} onChange={e => setEffectiveTo(e.target.value)} /></label>
        </div><p className="rf-help">A service rule takes precedence over a category rule, then an all-services rule. Overlap at the same specificity is refused. Retiring a rule affects future drafts and approvals; recorded snapshots remain unchanged.</p><div className="rf-actions"><button className="rf-primary" disabled={busy}>Create rule</button></div></form>
        <div className="rf-card rf-scroll"><table><thead><tr><th>Doctor</th><th>Coverage</th><th>Amount</th><th>Basis & recognition</th><th>Effective</th><th>Status</th></tr></thead><tbody>{meta.rules.map(rule => <tr key={rule.id}><td>{meta.doctors.find(d => d.id === rule.doctor_id)?.name ?? rule.doctor_id}</td><td>{rule.selector_type === 'all' ? 'All services' : `${rule.selector_type}: ${rule.selector_value}`}</td><td>{rule.mode === 'percentage' ? `${((rule.percent_bp ?? 0) / 100).toFixed(2)}%` : formatInr(rule.fixed_paise ?? 0)}</td><td>{words(rule.basis)} · {words(rule.recognition)}</td><td>{rule.effective_from} – {rule.effective_to ?? 'Open ended'}</td><td>{rule.is_active ? <button disabled={busy} onClick={() => void perform(async () => { await retirePayoutRule(rule.id); return getFinanceMeta(period.facility_id) }, next => { setMeta(next); setPayout(null); setNotice('Rule retired.') })}>Retire rule {rule.id}</button> : 'Retired'}</td></tr>)}</tbody></table>{!meta.rules.length && <p>No payout rules are configured.</p>}</div>
      </>}
      {view === 'payouts' && <>
        <div className="rf-card"><label>Doctor filter<select value={doctor} onChange={e => { setDoctor(e.target.value); changePayout() }}><option value="">All explicitly attributed doctors</option>{meta.doctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label><div className="rf-actions"><button className="rf-primary" disabled={busy || !validPeriod} onClick={() => void perform(() => previewPayout(period, doctor || undefined), setPayout)}>Preview earnings</button><button disabled={busy} onClick={() => void perform(() => getPayoutStatements(period.facility_id), setStatements)}>Refresh statements</button></div></div>
        {payout && <section className="rf-card" aria-label="Payout preview"><h2>Unclaimed source earnings</h2><p className="rf-amount">Total {formatInr(payout.total_paise)}</p>{payout.issues.length > 0 && <ul className="rf-error">{payout.issues.map((issue, i) => <li key={i}>{words(issue.code)}{issue.source_line_id ? ` · source line ${issue.source_line_id}` : ''}</li>)}</ul>}{payout.excluded.length > 0 && <details><summary>{payout.excluded.length} excluded source lines</summary><ul>{payout.excluded.map((issue, i) => <li key={i}>{words(issue.code)} · line {issue.source_line_id}</li>)}</ul></details>}<div className="rf-scroll"><table><thead><tr><th>Invoice</th><th>Doctor</th><th>Kind</th><th>Basis</th><th>Earning</th></tr></thead><tbody>{payout.lines.map(line => <tr key={line.source_line_id}><td>{line.snapshot.invoice_line.invoice_number}</td><td>{meta.doctors.find(d => d.id === line.doctor_id)?.name ?? line.doctor_id}</td><td>{line.source_kind}</td><td>{formatInr(line.basis_paise)}</td><td>{formatInr(line.amount_paise)}</td></tr>)}</tbody></table></div><form onSubmit={e => { e.preventDefault(); void perform(async () => createPayoutStatement({ ...period, ...(doctor ? { doctor_id: doctor } : {}), idempotency_key: payoutKey.current, deduction_paise: toPaise(deduction), ...(toPaise(deduction) ? { deduction_reason: deductionReason } : {}) }), next => { refreshStatement(next); setNotice('Draft statement created. A different administrator must approve it.') }) }}><div className="rf-grid"><label>Explicit deduction in rupees<input inputMode="decimal" required value={deduction} onChange={e => { setDeduction(e.target.value); payoutKey.current = newId() }} /></label><label>Deduction reason (required for a deduction)<input maxLength={500} value={deductionReason} onChange={e => { setDeductionReason(e.target.value); payoutKey.current = newId() }} /></label></div><p className="rf-help">Orb does not calculate TDS. Every deduction is entered and explained by the hospital.</p><button disabled={busy || !payout.can_create} className="rf-primary">Create draft statement</button></form></section>}
        <section className="rf-card rf-scroll"><h2>Recorded statements</h2><table><thead><tr><th>Period</th><th>Status</th><th>Gross</th><th>Deduction</th><th>Net</th><th>Open</th></tr></thead><tbody>{statements.map(s => <tr key={s.id}><td>{s.from_day} – {s.to_day}</td><td>{s.status}</td><td>{formatInr(s.total_paise)}</td><td>{formatInr(s.deduction_paise)}</td><td>{formatInr(s.net_paise)}</td><td><button disabled={busy} onClick={() => void perform(() => getPayoutStatement(s.id), next => { setStatement(next); setReference('') })}>Open {s.id.slice(0, 8)}</button></td></tr>)}</tbody></table>{!statements.length && <p>No statements recorded.</p>}</section>
        {statement && <section className="rf-card" aria-label="Statement detail"><h2>Statement {statement.id.slice(0, 8)}</h2><p>{statement.from_day} – {statement.to_day} · {statement.status}</p><p className="rf-amount">Gross {formatInr(statement.total_paise)} · Deduction {formatInr(statement.deduction_paise)} · Net {formatInr(statement.net_paise)}</p>{statement.deduction_reason && <p>Deduction: {statement.deduction_reason}</p>}<p className="rf-help">Created by {statement.created_by}{statement.approved_by && ` · approved by ${statement.approved_by}`}{statement.payment_reference && ` · payment reference ${statement.payment_reference}`}</p><div className="rf-actions">{statement.status === 'draft' && <><button disabled={busy} className="rf-primary" onClick={() => void perform(() => approvePayoutStatement(statement.id), next => { refreshStatement(next); setNotice('Statement approved; source earnings claimed once.') })}>Approve as second person</button><button disabled={busy} onClick={() => void perform(() => cancelPayoutStatement(statement.id), refreshStatement)}>Cancel draft</button></>}<button disabled={busy} onClick={() => void perform(() => downloadPayoutStatement(statement.id), () => setNotice('Statement CSV saved through your browser.'))}>Export statement CSV</button></div>{statement.status === 'approved' && <form onSubmit={e => { e.preventDefault(); void perform(() => markPayoutPaid(statement.id, reference), next => { refreshStatement(next); setNotice('Payment attestation recorded. No funds were transmitted.') }) }}><label>Reference for payment already made<input required maxLength={120} value={reference} onChange={e => setReference(e.target.value)} /></label><div className="rf-actions"><button disabled={busy} className="rf-primary">Record payment already made</button></div></form>}<div className="rf-scroll"><table><thead><tr><th>Invoice</th><th>Doctor</th><th>Rule</th><th>Basis</th><th>Earning</th></tr></thead><tbody>{statement.lines?.map(line => <tr key={line.source_line_id}><td>{line.snapshot.invoice_line.invoice_number}</td><td>{line.doctor_id}</td><td>{line.rule_id}</td><td>{formatInr(line.basis_paise)}</td><td>{formatInr(line.amount_paise)}</td></tr>)}</tbody></table></div></section>}
      </>}
      <p role="status">{busy ? 'Working…' : ''}</p><ul className="rf-help">{meta.notes.map(note => <li key={note}>{note}</li>)}</ul>
    </>}
  </section>
}
