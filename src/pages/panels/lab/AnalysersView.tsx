/**
 * Laboratory Analysers (lis-23): the analyser registry, each analyser's key
 * and which Orb test each of its codes is.
 *
 * WHO CAN DO WHAT. Every laboratory role (and the administrator) holding
 * lab.read can read this screen. Registering an analyser, changing its
 * details, switching it on or off and issuing its key need lab.interface
 * (the administrator). Which test a code is needs lab.master (the
 * pathologist), because it decides which result a number becomes. The server
 * refuses a write from anyone else whatever this screen shows.
 *
 * THE KEY IS SHOWN ONCE. After a key is issued it appears in one panel with a
 * copy button, and is gone when the person says they have copied it or leaves
 * the screen. It is held only in this component's memory: never in storage,
 * a URL or a log. Orb keeps only a scrambled form of it.
 *
 * Setting up an analyser is new work, so the view is left out while the
 * laboratory module drains (ruling LIS-R21); the server refuses its writes
 * then anyway.
 *
 * Discovered by ../LabPanel.tsx through its `./lab/*View.tsx` glob; the
 * exports below are the view contract in ./viewRegistry.ts.
 */
import { useCallback, useEffect, useId, useMemo, useState, type FormEvent } from 'react'

import { toLabError } from '../../../api/lab'
import {
  ANALYSER_ID_PATTERN,
  calibrationState,
  changeAnalyser,
  factorProblem,
  getAnalyser,
  issueAnalyserKey,
  listAnalysers,
  registerAnalyser,
  retireAnalyserMapping,
  saveAnalyserMapping,
  setAnalyserSwitchedOn,
  type Analyser,
  type AnalyserChanges,
  type AnalyserChoice,
  type AnalyserMapping,
  type AnalyserOptions,
  type AnalyserProtocol,
  type AnalyserTransport,
} from '../../../api/lab/analysers'
import { listLabSections, listLabTests, type LabSection, type LabTest } from '../../../api/lab/master'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { LAB_INTERFACE, LAB_MASTER, hasPermission } from '../../../lib/roles'
import type { LabViewProps } from './viewRegistry'
import './SetupView.css'

export const viewId = 'analysers'
export const viewLabel = 'Analysers'
export const viewOrder = 95
export const viewPermission = 'lab.read'
export const viewWhenDraining = 'hide'

interface Registry {
  analysers: Analyser[]
  options: AnalyserOptions
  sections: LabSection[]
  tests: LabTest[]
}

type Load =
  | { state: 'loading' }
  | { state: 'ready'; data: Registry }
  | { state: 'error'; message: string }

interface ShownKey {
  analyserId: string
  analyserName: string
  key: string
  replaced: boolean
}

const MODULE_OFF_WRITE =
  'The laboratory module is switched off on this site, so analysers cannot be changed.'

function writeError(err: unknown, forbidden: string): string {
  const failure = toLabError(err)
  if (failure.isModuleDisabled) return MODULE_OFF_WRITE
  if (failure.isForbidden) return forbidden
  return failure.message
}

const INTERFACE_ONLY = 'Only an administrator can register analysers and issue their keys.'
const MASTER_ONLY = 'Only a pathologist can change which test an analyser code is.'

function whenText(at: string | null | undefined, never = 'Never'): string {
  return at ? formatClinicalDateTime(at) : never
}

function addressText(a: Analyser): string {
  if (!a.host) return 'Not recorded'
  return a.port ? `${a.host}, port ${a.port}` : a.host
}

function calibrationText(a: Analyser): string {
  const bio = a.biomedical
  switch (calibrationState(bio)) {
    case 'not_linked': return 'Not linked to the biomedical register'
    case 'not_found': return 'The linked biomedical register entry is missing'
    case 'no_schedule': return 'No calibration schedule in the biomedical register'
    case 'overdue':
      return `Calibration overdue, due ${formatClinicalDateKey(bio?.calibration?.next_due_date)}`
    default:
      return `Calibration due ${formatClinicalDateKey(bio?.calibration?.next_due_date)}`
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  The view
// ════════════════════════════════════════════════════════════════════════════

export default function AnalysersView({ state, onModuleOff }: LabViewProps) {
  const canInterface = hasPermission(LAB_INTERFACE) && state === 'on'
  const canMap = hasPermission(LAB_MASTER) && state === 'on'
  const [attempt, setAttempt] = useState(0)
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [openId, setOpenId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [shownKey, setShownKey] = useState<ShownKey | null>(null)

  useEffect(() => {
    let live = true
    Promise.all([
      listAnalysers(),
      listLabSections(),
      listLabTests({ kind: 'analyte' }),
    ]).then(
      ([registry, sections, tests]) => {
        if (!live) return
        setLoad({
          state: 'ready',
          data: {
            analysers: registry.analysers ?? [],
            options: registry.options,
            sections: (sections.sections ?? []).filter((s) => Boolean(s.active)),
            tests: tests.tests ?? [],
          },
        })
      },
      (err: unknown) => {
        if (!live) return
        const failure = toLabError(err)
        if (failure.isModuleDisabled) {
          onModuleOff()
          return
        }
        setLoad({
          state: 'error',
          message: failure.isForbidden ? 'Your role cannot read the analyser list.' : failure.message,
        })
      },
    )
    return () => { live = false }
  }, [attempt, onModuleOff])

  const reload = useCallback(() => setAttempt((n) => n + 1), [])

  if (load.state === 'loading') {
    return <p className="lab-setup-hint" role="status">Loading the analysers</p>
  }
  if (load.state === 'error') {
    return (
      <section className="lab-setup-notice lab-setup-notice--error" role="alert">
        <p>{load.message}</p>
        <button type="button" className="lab-setup-btn" onClick={reload}>Try again</button>
      </section>
    )
  }

  const { data } = load
  return (
    <div className="lab-setup">
      {!data.options.http_intake_built && (
        <p className="lab-setup-notice">
          Orb does not receive results from analysers yet. This screen gets each analyser
          ready: register it, issue its key and map its test codes.
        </p>
      )}
      {state === 'on' && !canInterface && !canMap && (
        <p className="lab-setup-notice">
          You can read the analysers here. An administrator registers them and issues their
          keys; a pathologist decides which test each code is.
        </p>
      )}
      {shownKey && <KeyPanel shown={shownKey} onDone={() => setShownKey(null)} />}

      <section className="lab-setup-area-body" aria-label="Analysers">
        <div className="lab-setup-toolbar">
          {canInterface && !adding && (
            <button type="button" className="lab-setup-btn" onClick={() => setAdding(true)}>
              Register an analyser
            </button>
          )}
        </div>
        {adding && (
          <AnalyserForm
            mode="create"
            options={data.options}
            sections={data.sections}
            onCancel={() => setAdding(false)}
            onSaved={(made) => {
              setAdding(false)
              setOpenId(made.id)
              reload()
            }}
          />
        )}
        <AnalyserTable analysers={data.analysers} openId={openId}
          onOpen={(id) => setOpenId(id === openId ? null : id)} />
      </section>

      {openId && (
        <AnalyserDetail
          key={openId}
          analyserId={openId}
          data={data}
          canInterface={canInterface}
          canMap={canMap}
          onChanged={reload}
          onKey={setShownKey}
          onClose={() => setOpenId(null)}
        />
      )}
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  The key, once
// ════════════════════════════════════════════════════════════════════════════

function KeyPanel({ shown, onDone }: { shown: ShownKey; onDone: () => void }) {
  const [copied, setCopied] = useState<string | null>(null)
  const fieldId = useId()

  async function copy() {
    try {
      await navigator.clipboard.writeText(shown.key)
      setCopied('Copied.')
    } catch {
      setCopied('Could not copy. Select the key and copy it by hand.')
    }
  }

  return (
    <section className="lab-setup-detail" aria-labelledby={`${fieldId}-title`}>
      <h2 id={`${fieldId}-title`} className="lab-setup-detail__title">
        {shown.replaced ? 'New key' : 'Key'} for {shown.analyserName}: copy it now
      </h2>
      <p className="lab-setup-gapline lab-setup-gapline--warn">
        Orb shows this key once. It keeps only a scrambled form that cannot be turned back
        into the key, so it cannot show it again.
        {shown.replaced ? ' The previous key has stopped working.' : ''}
      </p>
      <label htmlFor={fieldId} className="lab-setup-field lab-setup-field--wide">
        <span>Key (enter it in the analyser or its middleware as X-Instrument-Key)</span>
        <input id={fieldId} className="lab-setup-input lab-setup-num" readOnly value={shown.key}
          onFocus={(e) => e.currentTarget.select()} spellCheck={false} autoComplete="off" />
      </label>
      <p className="lab-setup-hint">
        Analyser id (X-Instrument-Id): <span className="lab-setup-num">{shown.analyserId}</span>
      </p>
      <div className="lab-setup-actions">
        <button type="button" className="lab-setup-btn" onClick={copy}>Copy the key</button>
        <button type="button" className="lab-setup-btn lab-setup-btn--primary" onClick={onDone}>
          I have copied it
        </button>
      </div>
      {copied && <p className="lab-setup-hint" role="status">{copied}</p>}
    </section>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  The list
// ════════════════════════════════════════════════════════════════════════════

function AnalyserTable({ analysers, openId, onOpen }: {
  analysers: Analyser[]
  openId: string | null
  onOpen: (id: string) => void
}) {
  if (analysers.length === 0) {
    return (
      <p className="lab-setup-hint">
        No analyser is registered. Until one is, every result is entered by hand.
      </p>
    )
  }
  return (
    <div className="lab-setup-table-wrap">
      <table className="lab-setup-table">
        <thead>
          <tr>
            <th scope="col">Analyser</th>
            <th scope="col">Section</th>
            <th scope="col">Connection</th>
            <th scope="col">Key</th>
            <th scope="col">State</th>
            <th scope="col">Codes mapped</th>
            <th scope="col">Last result received</th>
            <th scope="col"><span className="lab-setup-sr">Open</span></th>
          </tr>
        </thead>
        <tbody>
          {analysers.map((a) => (
            <tr key={a.id} className={a.id === openId ? 'lab-setup-row--open' : undefined}>
              <td>
                <span className="lab-setup-strong">{a.name}</span>
                <span className="lab-setup-tag lab-setup-num">{a.id}</span>
              </td>
              <td>{a.section_name ?? 'None'}</td>
              <td>{a.protocol_label}{a.supported ? '' : ', not supported yet'}</td>
              {a.key.issued
                ? <td>Issued {whenText(a.key.issued_at)}</td>
                : <td className="lab-setup-cell--gap">Not issued</td>}
              <td>{a.enabled ? 'Switched on' : 'Switched off'}</td>
              {a.mapped_codes > 0
                ? <td className="lab-setup-num">{a.mapped_codes}</td>
                : <td className="lab-setup-cell--gap">None</td>}
              <td>{whenText(a.last_message_at)}</td>
              <td className="lab-setup-cell--actions">
                <button type="button" className="lab-setup-btn" aria-label={`Open ${a.name}`}
                  onClick={() => onOpen(a.id)}>
                  {a.id === openId ? 'Close' : 'Open'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  Register or change an analyser (lab.interface)
// ════════════════════════════════════════════════════════════════════════════

interface AnalyserFormProps {
  mode: 'create' | 'edit'
  analyser?: Analyser
  options: AnalyserOptions
  sections: LabSection[]
  onCancel: () => void
  onSaved: (analyser: Analyser) => void
}

function ChoiceOptions({ choices }: { choices: AnalyserChoice[] }) {
  return (
    <>
      {choices.map((c) => (
        <option key={c.value} value={c.value} disabled={!c.supported}>
          {c.supported ? c.label : `${c.label} (not supported yet)`}
        </option>
      ))}
    </>
  )
}

function AnalyserForm({ mode, analyser, options, sections, onCancel, onSaved }: AnalyserFormProps) {
  const [ident, setIdent] = useState('')
  const [name, setName] = useState(analyser?.name ?? '')
  const [make, setMake] = useState(analyser?.make ?? '')
  const [model, setModel] = useState(analyser?.model ?? '')
  const [serial, setSerial] = useState(analyser?.serial ?? '')
  const [sectionId, setSectionId] = useState(analyser?.section_id ?? '')
  const [protocol, setProtocol] = useState<AnalyserProtocol>(analyser?.protocol ?? 'hl7v2')
  const [transport, setTransport] = useState<AnalyserTransport>(analyser?.transport ?? 'http')
  const [host, setHost] = useState(analyser?.host ?? '')
  const [port, setPort] = useState(analyser?.port ? String(analyser.port) : '')
  const [zone, setZone] = useState(analyser?.assumed_timezone ?? '')
  const [asset, setAsset] = useState(analyser?.biomedical_asset_id ? String(analyser.biomedical_asset_id) : '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()

  function problem(): string | null {
    if (mode === 'create' && !ANALYSER_ID_PATTERN.test(ident.trim().toUpperCase())) {
      return 'The analyser id is 2 to 32 letters, digits, - or _, for example XN1000-1.'
    }
    if (!name.trim()) return 'Give the analyser a name.'
    if (port.trim() && (!/^\d+$/.test(port.trim()) || +port < 1 || +port > 65535)) {
      return 'The port is a whole number from 1 to 65535.'
    }
    if (asset.trim() && !/^\d+$/.test(asset.trim())) {
      return 'The biomedical register entry is a number.'
    }
    return null
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const wrong = problem()
    if (wrong) {
      setError(wrong)
      return
    }
    const fields = {
      name: name.trim(),
      make: make.trim() || null,
      model: model.trim() || null,
      serial: serial.trim() || null,
      section_id: sectionId || null,
      protocol,
      transport,
      host: host.trim() || null,
      port: port.trim() ? Number(port.trim()) : null,
      assumed_timezone: zone.trim() || null,
      biomedical_asset_id: asset.trim() ? Number(asset.trim()) : null,
    }
    setBusy(true)
    setError(null)
    try {
      if (mode === 'create') {
        onSaved(await registerAnalyser({ id: ident.trim().toUpperCase(), ...fields }))
      } else if (analyser) {
        const changes: AnalyserChanges = {}
        const blank = (v: unknown) => (v === null || v === undefined ? '' : String(v))
        for (const [key, value] of Object.entries(fields)) {
          if (blank((analyser as unknown as Record<string, unknown>)[key]) !== blank(value)) {
            (changes as Record<string, unknown>)[key] = value
          }
        }
        if (Object.keys(changes).length === 0) {
          setError('Nothing has changed.')
          return
        }
        onSaved(await changeAnalyser(analyser.id, { ...changes, version: analyser.version }))
      }
    } catch (err) {
      setError(writeError(err, INTERFACE_ONLY))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="lab-setup-form" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="lab-setup-form__title">
        {mode === 'create' ? 'Register an analyser' : `Change ${analyser?.name ?? 'analyser'}`}
      </h3>
      <div className="lab-setup-grid">
        {mode === 'create' && (
          <label className="lab-setup-field">
            <span>Analyser id (it sends this with every result)</span>
            <input className="lab-setup-input lab-setup-num" value={ident} maxLength={32}
              autoComplete="off" spellCheck={false}
              onChange={(e) => setIdent(e.target.value)} placeholder="XN1000-1" />
          </label>
        )}
        <label className="lab-setup-field">
          <span>Name</span>
          <input className="lab-setup-input" value={name} maxLength={200}
            onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Make</span>
          <input className="lab-setup-input" value={make} maxLength={200}
            onChange={(e) => setMake(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Model</span>
          <input className="lab-setup-input" value={model} maxLength={200}
            onChange={(e) => setModel(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Serial number</span>
          <input className="lab-setup-input" value={serial} maxLength={200}
            onChange={(e) => setSerial(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Section</span>
          <select className="lab-setup-input" value={sectionId}
            onChange={(e) => setSectionId(e.target.value)}>
            <option value="">None</option>
            {sections.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label className="lab-setup-field">
          <span>Message format</span>
          <select className="lab-setup-input" value={protocol}
            onChange={(e) => setProtocol(e.target.value as AnalyserProtocol)}>
            <ChoiceOptions choices={options.protocols} />
          </select>
        </label>
        <label className="lab-setup-field">
          <span>Connection</span>
          <select className="lab-setup-input" value={transport}
            onChange={(e) => setTransport(e.target.value as AnalyserTransport)}>
            <ChoiceOptions choices={options.transports} />
          </select>
        </label>
        <label className="lab-setup-field">
          <span>Address on the hospital network</span>
          <input className="lab-setup-input" value={host} maxLength={253} autoComplete="off"
            spellCheck={false} onChange={(e) => setHost(e.target.value)} placeholder="192.168.10.21" />
        </label>
        <label className="lab-setup-field">
          <span>Port on the analyser, if any</span>
          <input className="lab-setup-input" inputMode="numeric" value={port}
            onChange={(e) => setPort(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Its clock&apos;s time zone (blank means hospital time, {options.hospital_timezone})</span>
          <input className="lab-setup-input" value={zone} maxLength={64}
            onChange={(e) => setZone(e.target.value)} placeholder="Asia/Kolkata" />
        </label>
        <label className="lab-setup-field">
          <span>Biomedical register entry number, if any</span>
          <input className="lab-setup-input" inputMode="numeric" value={asset}
            onChange={(e) => setAsset(e.target.value)} />
        </label>
      </div>
      {transport === 'mllp_listen' && (
        <p className="lab-setup-hint">
          The analyser connects to port {options.listener.port} on the Orb computer.
        </p>
      )}
      {mode === 'create' && (
        <p className="lab-setup-hint">
          A new analyser starts switched off and with no key. Issue its key and switch it on
          once it is set up.
        </p>
      )}
      <div className="lab-setup-actions">
        <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
          {mode === 'create' ? 'Register' : 'Save'}
        </button>
        <button type="button" className="lab-setup-btn" onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  One analyser
// ════════════════════════════════════════════════════════════════════════════

type Detail = { id: string; analyser: Analyser } | { id: string; error: string }

function AnalyserDetail({ analyserId, data, canInterface, canMap, onChanged, onKey, onClose }: {
  analyserId: string
  data: Registry
  canInterface: boolean
  canMap: boolean
  onChanged: () => void
  onKey: (shown: ShownKey) => void
  onClose: () => void
}) {
  const [version, setVersion] = useState(0)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    let live = true
    getAnalyser(analyserId).then(
      (analyser) => { if (live) setDetail({ id: analyserId, analyser }) },
      (err: unknown) => { if (live) setDetail({ id: analyserId, error: toLabError(err).message }) },
    )
    return () => { live = false }
  }, [analyserId, version])

  const refresh = useCallback(() => {
    setVersion((v) => v + 1)
    onChanged()
  }, [onChanged])

  const current = detail && detail.id === analyserId ? detail : null
  if (!current) return <p className="lab-setup-hint" role="status">Loading the analyser</p>
  if ('error' in current) return <p className="lab-setup-error" role="alert">{current.error}</p>
  const { analyser } = current

  return (
    <section className="lab-setup-detail" aria-label={analyser.name}>
      <header className="lab-setup-detail__head">
        <div>
          <h2 className="lab-setup-detail__title">{analyser.name}</h2>
          <p className="lab-setup-hint">
            <span className="lab-setup-num">{analyser.id}</span>
            {analyser.enabled ? ' · Switched on' : ' · Switched off'}
          </p>
        </div>
        <div className="lab-setup-actions">
          {canInterface && !editing && (
            <button type="button" className="lab-setup-btn" onClick={() => setEditing(true)}>
              Change details
            </button>
          )}
          <button type="button" className="lab-setup-btn" onClick={onClose}>Close</button>
        </div>
      </header>

      {!analyser.supported && (
        <p className="lab-setup-gapline lab-setup-gapline--warn">
          This analyser&apos;s message format or connection is not supported in this version of
          Orb, so it cannot be switched on.
        </p>
      )}

      {editing ? (
        <AnalyserForm
          mode="edit"
          analyser={analyser}
          options={data.options}
          sections={data.sections}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            refresh()
          }}
        />
      ) : (
        <dl className="lab-setup-facts">
          <div><dt>Make and model</dt><dd>{[analyser.make, analyser.model].filter(Boolean).join(' ') || 'Not recorded'}</dd></div>
          <div><dt>Serial number</dt><dd>{analyser.serial || 'Not recorded'}</dd></div>
          <div><dt>Section</dt><dd>{analyser.section_name ?? 'None'}</dd></div>
          <div><dt>Message format</dt><dd>{analyser.protocol_label}</dd></div>
          <div><dt>Connection</dt><dd>{analyser.transport_label}</dd></div>
          <div><dt>Address</dt><dd>{addressText(analyser)}</dd></div>
          <div>
            <dt>Clock time zone</dt>
            <dd>{analyser.assumed_timezone ?? `Hospital time (${data.options.hospital_timezone})`}</dd>
          </div>
          <div><dt>Biomedical register</dt><dd>{calibrationText(analyser)}</dd></div>
          <div>
            <dt>Key</dt>
            <dd>{analyser.key.issued
              ? `Issued ${whenText(analyser.key.issued_at)}${analyser.key.issued_by_name ? ` by ${analyser.key.issued_by_name}` : ''}`
              : 'Not issued'}</dd>
          </div>
          <div>
            <dt>State</dt>
            <dd>{analyser.enabled ? 'Switched on' : 'Switched off'}{analyser.status_reason ? `: ${analyser.status_reason}` : ''}</dd>
          </div>
          <div><dt>Last result received</dt><dd>{whenText(analyser.last_message_at)}</dd></div>
          <div>
            <dt>Version</dt>
            <dd>{analyser.version}, changed {whenText(analyser.updated_at)}{analyser.updated_by_name ? ` by ${analyser.updated_by_name}` : ''}</dd>
          </div>
        </dl>
      )}

      {canInterface && (
        <div className="lab-setup-editors">
          <KeyAction analyser={analyser} onIssued={(issued) => {
            onKey({ analyserId: analyser.id, analyserName: analyser.name, key: issued.key,
              replaced: issued.replaced })
            refresh()
          }} />
          <SwitchAction analyser={analyser} onDone={refresh} />
        </div>
      )}

      <CodeMap analyser={analyser} tests={data.tests} canMap={canMap} onChanged={refresh} />
    </section>
  )
}

function KeyAction({ analyser, onIssued }: {
  analyser: Analyser
  onIssued: (issued: { key: string; replaced: boolean }) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const replacing = analyser.key.issued
  const id = useId()

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (replacing && !reason.trim()) {
      setError('Say why the key is being replaced.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const issued = await issueAnalyserKey(analyser.id, reason.trim() || undefined)
      setOpen(false)
      setReason('')
      onIssued(issued)
    } catch (err) {
      setError(writeError(err, INTERFACE_ONLY))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-setup-fieldset">
      <h3 id={`${id}-title`} className="lab-setup-form__title">Key</h3>
      {!open && (
        <button type="button" className="lab-setup-btn" onClick={() => setOpen(true)}>
          {replacing ? 'Replace the key' : 'Issue a key'}
        </button>
      )}
      {open && (
        <form className="lab-setup-inline" onSubmit={submit} aria-labelledby={`${id}-title`}>
          <p className="lab-setup-hint">
            {replacing
              ? 'The current key stops working at once. The analyser, or its middleware, needs the new key before it can send again.'
              : 'Orb shows the key once, here. Have the analyser or its middleware ready to take it.'}
          </p>
          {replacing && (
            <label className="lab-setup-field lab-setup-field--wide">
              <span>Why it is being replaced</span>
              <input className="lab-setup-input" value={reason} maxLength={500}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Service engineer visit, or the key may have been seen" />
            </label>
          )}
          <div className="lab-setup-actions">
            <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
              {replacing ? 'Replace the key' : 'Issue the key'}
            </button>
            <button type="button" className="lab-setup-btn" onClick={() => {
              setOpen(false)
              setError(null)
            }}>Cancel</button>
          </div>
          {error && <p className="lab-setup-error" role="alert">{error}</p>}
        </form>
      )}
    </div>
  )
}

function SwitchAction({ analyser, onDone }: { analyser: Analyser; onDone: () => void }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const turningOn = !analyser.enabled
  const id = useId()
  const blocked = turningOn && !analyser.key.issued
    ? 'Issue this analyser a key before switching it on.'
    : (turningOn && !analyser.supported
        ? 'This analyser cannot be switched on in this version of Orb.'
        : null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!reason.trim()) {
      setError(turningOn ? 'Say why it is being switched on.' : 'Say why it is being switched off.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await setAnalyserSwitchedOn(analyser.id, turningOn, reason.trim())
      setOpen(false)
      setReason('')
      onDone()
    } catch (err) {
      setError(writeError(err, INTERFACE_ONLY))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="lab-setup-fieldset">
      <h3 id={`${id}-title`} className="lab-setup-form__title">On or off</h3>
      {blocked && <p className="lab-setup-hint">{blocked}</p>}
      {!open && !blocked && (
        <button type="button" className="lab-setup-btn" onClick={() => setOpen(true)}>
          {turningOn ? 'Switch on' : 'Switch off'}
        </button>
      )}
      {open && (
        <form className="lab-setup-inline" onSubmit={submit} aria-labelledby={`${id}-title`}>
          <p className="lab-setup-hint">
            {turningOn
              ? 'Once switched on, results this analyser sends with its key are accepted.'
              : 'Once switched off, everything this analyser sends is refused.'}
          </p>
          <label className="lab-setup-field lab-setup-field--wide">
            <span>Reason</span>
            <input className="lab-setup-input" value={reason} maxLength={500}
              onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="lab-setup-actions">
            <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
              {turningOn ? 'Switch on' : 'Switch off'}
            </button>
            <button type="button" className="lab-setup-btn" onClick={() => {
              setOpen(false)
              setError(null)
            }}>Cancel</button>
          </div>
          {error && <p className="lab-setup-error" role="alert">{error}</p>}
        </form>
      )}
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  The code map (lab.master)
// ════════════════════════════════════════════════════════════════════════════

function CodeMap({ analyser, tests, canMap, onChanged }: {
  analyser: Analyser
  tests: LabTest[]
  canMap: boolean
  onChanged: () => void
}) {
  const [editing, setEditing] = useState<AnalyserMapping | 'new' | null>(null)
  const [retiring, setRetiring] = useState<string | null>(null)
  const [showRetired, setShowRetired] = useState(false)
  const rows = (analyser.map ?? []).filter((m) => showRetired || m.active)
  const retiredCount = (analyser.map ?? []).filter((m) => !m.active).length
  const mappable = useMemo(
    () => tests.filter((t) => t.kind === 'analyte' && Boolean(t.active) && t.performed_at === 'lab'),
    [tests])

  return (
    <section className="lab-setup-bands" aria-label="Test codes">
      <h3 className="lab-setup-bands__title">Test codes</h3>
      <p className="lab-setup-hint">
        Which Orb test each code from this analyser is. A code with no mapping, or a retired
        one, is never put on a patient&apos;s record: it waits for laboratory staff.
      </p>
      {!canMap && (
        <p className="lab-setup-hint">Only a pathologist can change which test a code is.</p>
      )}
      <div className="lab-setup-toolbar">
        {canMap && editing === null && (
          <button type="button" className="lab-setup-btn" onClick={() => setEditing('new')}>
            Map a code
          </button>
        )}
        {retiredCount > 0 && (
          <label className="lab-setup-check">
            <input type="checkbox" checked={showRetired}
              onChange={(e) => setShowRetired(e.target.checked)} />
            <span>Show retired codes ({retiredCount})</span>
          </label>
        )}
      </div>
      {editing !== null && (
        <MappingForm
          analyser={analyser}
          mapping={editing === 'new' ? undefined : editing}
          tests={mappable}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            onChanged()
          }}
        />
      )}
      {rows.length === 0 ? (
        <p className="lab-setup-cell--gap">No code is mapped yet.</p>
      ) : (
        <div className="lab-setup-table-wrap">
          <table className="lab-setup-table">
            <thead>
              <tr>
                <th scope="col">Analyser code</th>
                <th scope="col">Orb test</th>
                <th scope="col">Factor</th>
                <th scope="col">Analyser unit</th>
                <th scope="col">Approved</th>
                <th scope="col">Note</th>
                {canMap && <th scope="col"><span className="lab-setup-sr">Actions</span></th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr key={m.instrument_code}>
                  <td>
                    <span className="lab-setup-strong lab-setup-num">{m.instrument_code}</span>
                    {!m.active && <span className="lab-setup-tag">Retired</span>}
                  </td>
                  <td>
                    {m.test_name ?? m.test_id}
                    {m.test_found && !m.test_active && <span className="lab-setup-tag">Switched off</span>}
                  </td>
                  <td className="lab-setup-num">{m.factor === '1' ? 'None' : `× ${m.factor}`}</td>
                  <td>{m.instrument_unit ?? (m.test_unit ? 'Not stated' : 'None')}
                    {m.test_unit ? <span className="lab-setup-hint"> (Orb: {m.test_unit})</span> : null}</td>
                  <td>{whenText(m.active ? m.approved_at : m.retired_at)}
                    {m.active
                      ? (m.approved_by_name ? `, ${m.approved_by_name}` : '')
                      : `, retired${m.retired_by_name ? ` by ${m.retired_by_name}` : ''}: ${m.retire_reason ?? ''}`}</td>
                  <td>{m.note}</td>
                  {canMap && (
                    <td className="lab-setup-cell--actions">
                      {m.active ? (
                        <>
                          <button type="button" className="lab-setup-btn"
                            aria-label={`Change code ${m.instrument_code}`}
                            onClick={() => setEditing(m)}>Change</button>
                          <button type="button" className="lab-setup-btn"
                            aria-label={`Retire code ${m.instrument_code}`}
                            onClick={() => setRetiring(m.instrument_code)}>Retire</button>
                        </>
                      ) : (
                        <button type="button" className="lab-setup-btn"
                          aria-label={`Bring back code ${m.instrument_code}`}
                          onClick={() => setEditing(m)}>Bring back</button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {retiring && (
        <RetireForm analyser={analyser} code={retiring}
          onCancel={() => setRetiring(null)}
          onDone={() => {
            setRetiring(null)
            onChanged()
          }} />
      )}
    </section>
  )
}

function MappingForm({ analyser, mapping, tests, onCancel, onSaved }: {
  analyser: Analyser
  mapping?: AnalyserMapping
  tests: LabTest[]
  onCancel: () => void
  onSaved: () => void
}) {
  const [code, setCode] = useState(mapping?.instrument_code ?? '')
  const [testId, setTestId] = useState(mapping?.test_id ?? '')
  const [factor, setFactor] = useState(mapping?.factor ?? '1')
  const [unit, setUnit] = useState(mapping?.instrument_unit ?? '')
  const [note, setNote] = useState(mapping?.note ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const test = tests.find((t) => t.id === testId)
  const numeric = test ? test.result_type === 'numeric' : true

  function problem(): string | null {
    if (!code.trim()) return 'Type the code the analyser sends for this test.'
    if (/[|^~\\&]/.test(code)) return 'An analyser code cannot contain | ^ ~ \\ or &.'
    if (!testId) return 'Choose the Orb test.'
    if (numeric) {
      const wrong = factorProblem(factor)
      if (wrong) return wrong
    }
    if (!note.trim()) return 'Add a note, for example the manual and revision you checked the code against.'
    return null
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const wrong = problem()
    if (wrong) {
      setError(wrong)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await saveAnalyserMapping(analyser.id, {
        instrument_code: code.trim(),
        test_id: testId,
        factor: numeric ? (factor.trim() || '1') : '1',
        instrument_unit: numeric ? (unit.trim() || null) : null,
        note: note.trim(),
      })
      onSaved()
    } catch (err) {
      setError(writeError(err, MASTER_ONLY))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="lab-setup-form" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="lab-setup-form__title">
        {mapping ? `Code ${mapping.instrument_code}` : 'Map a code'}
      </h3>
      <div className="lab-setup-grid">
        <label className="lab-setup-field">
          <span>Analyser code (as the analyser sends it)</span>
          <input className="lab-setup-input lab-setup-num" value={code} maxLength={64}
            readOnly={Boolean(mapping)} autoComplete="off" spellCheck={false}
            onChange={(e) => setCode(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Orb test</span>
          <select className="lab-setup-input" value={testId} onChange={(e) => setTestId(e.target.value)}>
            <option value="">Choose a test</option>
            {tests.map((t) => (
              <option key={t.id} value={t.id}>{t.name} ({t.code_system} {t.code})</option>
            ))}
          </select>
        </label>
        {numeric && (
          <>
            <label className="lab-setup-field">
              <span>Multiply the analyser&apos;s number by</span>
              <input className="lab-setup-input lab-setup-num" inputMode="decimal" value={factor}
                onChange={(e) => setFactor(e.target.value)} />
            </label>
            <label className="lab-setup-field">
              <span>Unit the analyser sends{test?.unit ? ` (Orb reports ${test.unit})` : ''}</span>
              <input className="lab-setup-input" value={unit} maxLength={32}
                onChange={(e) => setUnit(e.target.value)} />
            </label>
          </>
        )}
        <label className="lab-setup-field lab-setup-field--wide">
          <span>Note</span>
          <textarea className="lab-setup-input lab-setup-textarea" value={note} maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Checked against the analyser's host interface manual, revision 4" />
        </label>
      </div>
      {numeric && (
        <p className="lab-setup-hint">
          Leave the factor at 1 unless the analyser reports in a different unit. A factor
          with more than three decimal places is refused, never rounded: set the analyser to
          report in the test&apos;s unit instead.
        </p>
      )}
      <div className="lab-setup-actions">
        <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
          {mapping && !mapping.active ? 'Bring back' : 'Save'}
        </button>
        <button type="button" className="lab-setup-btn" onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

function RetireForm({ analyser, code, onCancel, onDone }: {
  analyser: Analyser
  code: string
  onCancel: () => void
  onDone: () => void
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!reason.trim()) {
      setError('Say why this code is being retired.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await retireAnalyserMapping(analyser.id, code, reason.trim())
      onDone()
    } catch (err) {
      setError(writeError(err, MASTER_ONLY))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="lab-setup-inline" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="lab-setup-form__title">Retire code {code}</h3>
      <p className="lab-setup-hint">
        A result with this code will then wait for laboratory staff instead of reaching a
        patient&apos;s record. The mapping is kept, so earlier results can still be traced.
      </p>
      <label className="lab-setup-field lab-setup-field--wide">
        <span>Reason</span>
        <input className="lab-setup-input" value={reason} maxLength={500}
          onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="lab-setup-actions">
        <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
          Retire
        </button>
        <button type="button" className="lab-setup-btn" onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}
