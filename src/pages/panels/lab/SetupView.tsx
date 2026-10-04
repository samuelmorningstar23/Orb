/**
 * Laboratory Setup (lis-4): the catalogue a pathologist owns.
 *
 * Tests and panels (done in the laboratory or at the bedside), reference
 * ranges and critical limits with their approver, approval note and the
 * clinicians a limit was agreed with, specimen types, sections and rejection
 * reasons.
 *
 * WHO CAN DO WHAT. Every laboratory role (and an administrator) holding
 * lab.read can read this screen; only a role holding lab.master (the
 * pathologist) sees the controls that change it. The server refuses a write
 * from anyone else whatever this screen shows. While the module drains
 * (switched off with open work) the catalogue is read only for everyone.
 *
 * NOT CONFIGURED IS LOUD (rulings LIS-R1 and LIS-R2: no range and no limit
 * ships). The top of the screen says how many numeric laboratory tests have no
 * approved reference range and no approved critical limit, and what that
 * means for a result; each test's row says "Not configured" rather than
 * leaving a blank.
 *
 * Discovered by ../LabPanel.tsx through its `./lab/*View.tsx` glob; the
 * exports below are the view contract in ./viewRegistry.ts.
 */
import { useCallback, useEffect, useId, useMemo, useState, type FormEvent } from 'react'

import { toLabError, type LabApiError } from '../../../api/lab'
import {
  createLabTest,
  getLabTest,
  listLabCriticalLimits,
  listLabReferenceRanges,
  listLabRejectionReasons,
  listLabSections,
  listLabSpecimenTypes,
  listLabTests,
  saveLabRejectionReason,
  saveLabSection,
  saveLabSpecimenType,
  setLabPanelComponents,
  updateLabTest,
  type LabCodeSystem,
  type LabConfigurationGaps,
  type LabCriticalLimit,
  type LabPerformedAt,
  type LabReferenceRange,
  type LabRejectionReason,
  type LabResultType,
  type LabSection,
  type LabSpecimenType,
  type LabTest,
  type LabTestKind,
} from '../../../api/lab/master'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { LAB_MASTER, hasPermission } from '../../../lib/roles'
import CriticalLimitEditor from './CriticalLimitEditor'
import RangeEditor from './RangeEditor'
import {
  KIND_LABEL,
  PERFORMED_AT_LABEL,
  RESULT_TYPE_LABEL,
  codedValuesFromText,
  codedValuesToText,
  gapLines,
  takesRanges,
} from './labSetupModel'
import type { LabViewProps } from './viewRegistry'
import './SetupView.css'

export const viewId = 'setup'
export const viewLabel = 'Setup'
export const viewOrder = 90
export const viewPermission = 'lab.read'

interface Catalogue {
  tests: LabTest[]
  gaps: LabConfigurationGaps
  ranges: LabReferenceRange[]
  limits: LabCriticalLimit[]
  specimenTypes: LabSpecimenType[]
  sections: LabSection[]
  reasons: LabRejectionReason[]
}

type Load =
  | { state: 'loading' }
  | { state: 'ready'; data: Catalogue }
  | { state: 'error'; message: string }

type Area = 'tests' | 'specimen-types' | 'sections' | 'rejection-reasons'

const AREAS: { id: Area; label: string }[] = [
  { id: 'tests', label: 'Tests and panels' },
  { id: 'specimen-types', label: 'Specimen types' },
  { id: 'sections', label: 'Sections' },
  { id: 'rejection-reasons', label: 'Rejection reasons' },
]

const MODULE_OFF_WRITE = 'The laboratory module is switched off on this site, so the catalogue cannot be changed.'

/** A write's refusal in words. A draining module refuses new configuration
 *  with module_disabled; that is said here, never turned into the whole-panel
 *  switched-off notice, because reads still work. */
function writeError(err: unknown): string {
  const failure: LabApiError = toLabError(err)
  if (failure.isModuleDisabled) return MODULE_OFF_WRITE
  if (failure.isForbidden) return 'Only a pathologist can change the laboratory catalogue.'
  return failure.message
}

function yesNo(value: unknown): string {
  return value ? 'Yes' : 'No'
}

function wholeNumber(raw: string, label: string): number | null | string {
  const text = raw.trim()
  if (!text) return null
  if (!/^\d+$/.test(text) || Number(text) <= 0) return `${label} is a whole number above zero.`
  return Number(text)
}

// ════════════════════════════════════════════════════════════════════════════
//  The view
// ════════════════════════════════════════════════════════════════════════════

export default function SetupView({ state, onModuleOff }: LabViewProps) {
  const canEdit = hasPermission(LAB_MASTER) && state === 'on'
  const [attempt, setAttempt] = useState(0)
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [area, setArea] = useState<Area>('tests')

  useEffect(() => {
    let live = true
    Promise.all([
      listLabTests({ include_inactive: true }),
      listLabReferenceRanges(),
      listLabCriticalLimits(),
      listLabSpecimenTypes({ include_inactive: true }),
      listLabSections({ include_inactive: true }),
      listLabRejectionReasons({ include_inactive: true }),
    ]).then(
      ([tests, ranges, limits, types, sections, reasons]) => {
        if (!live) return
        setLoad({
          state: 'ready',
          data: {
            tests: tests.tests ?? [],
            gaps: tests.gaps,
            ranges: ranges.ranges ?? [],
            limits: limits.critical_limits ?? [],
            specimenTypes: types.specimen_types ?? [],
            sections: sections.sections ?? [],
            reasons: reasons.rejection_reasons ?? [],
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
          message: failure.isForbidden
            ? 'Your role cannot read the laboratory catalogue.'
            : failure.message,
        })
      },
    )
    return () => { live = false }
  }, [attempt, onModuleOff])

  const reload = useCallback(() => setAttempt((n) => n + 1), [])

  if (load.state === 'loading') {
    return <p className="lab-setup-hint" role="status">Loading the laboratory catalogue</p>
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
      <GapSummary gaps={data.gaps} />
      {state === 'draining' && (
        <p className="lab-setup-notice">
          The laboratory module is switched off on this site, so the catalogue is read only.
        </p>
      )}
      {state === 'on' && !canEdit && (
        <p className="lab-setup-notice">
          You can read the catalogue here. Only a pathologist can change it.
        </p>
      )}
      <nav className="lab-setup-areas" aria-label="Setup sections">
        {AREAS.map((a) => (
          <button
            key={a.id}
            type="button"
            className={`lab-setup-area${a.id === area ? ' lab-setup-area--on' : ''}`}
            aria-current={a.id === area ? 'page' : undefined}
            onClick={() => setArea(a.id)}
          >
            {a.label}
          </button>
        ))}
      </nav>
      {area === 'tests' && <TestsArea data={data} canEdit={canEdit} onChanged={reload} />}
      {area === 'specimen-types' && (
        <SpecimenTypesArea rows={data.specimenTypes} canEdit={canEdit} onChanged={reload} />
      )}
      {area === 'sections' && <SectionsArea rows={data.sections} canEdit={canEdit} onChanged={reload} />}
      {area === 'rejection-reasons' && (
        <RejectionReasonsArea rows={data.reasons} canEdit={canEdit} onChanged={reload} />
      )}
    </div>
  )
}

function GapSummary({ gaps }: { gaps: LabConfigurationGaps }) {
  return (
    <section className="lab-setup-gaps" aria-label="Not configured">
      {gapLines(gaps).map((line) => (
        <p key={line.key}
          className={`lab-setup-gapline${line.warn ? ' lab-setup-gapline--warn' : ' lab-setup-gapline--ok'}`}>
          {line.sentence}
        </p>
      ))}
    </section>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  Tests and panels
// ════════════════════════════════════════════════════════════════════════════

function countByTest(rows: { test_id: string }[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const row of rows) counts.set(row.test_id, (counts.get(row.test_id) ?? 0) + 1)
  return counts
}

function ConfigCell({ test, count }: { test: LabTest; count: number }) {
  if (!takesRanges(test)) return <td className="lab-setup-cell--quiet">Not used</td>
  if (!count) return <td className="lab-setup-cell--gap">Not configured</td>
  return <td>{count === 1 ? 'Approved' : `${count} approved`}</td>
}

function TestsArea({ data, canEdit, onChanged }: {
  data: Catalogue
  canEdit: boolean
  onChanged: () => void
}) {
  const [query, setQuery] = useState('')
  const [showOff, setShowOff] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const findId = useId()
  const ranges = useMemo(() => countByTest(data.ranges), [data.ranges])
  const limits = useMemo(() => countByTest(data.limits), [data.limits])
  const specimenNames = useMemo(
    () => new Map(data.specimenTypes.map((s) => [s.code, s.display])), [data.specimenTypes])
  const term = query.trim().toLowerCase()
  const shown = data.tests.filter((t) => (showOff || t.active)
    && (!term || t.name.toLowerCase().includes(term) || t.short_name.toLowerCase().includes(term)
      || t.code.toLowerCase().includes(term)))

  return (
    <section className="lab-setup-area-body" aria-label="Tests and panels">
      <div className="lab-setup-toolbar">
        <label htmlFor={findId} className="lab-setup-field">
          <span>Find a test</span>
          <input id={findId} className="lab-setup-input" value={query}
            onChange={(e) => setQuery(e.target.value)} placeholder="Name or code" />
        </label>
        <label className="lab-setup-check">
          <input type="checkbox" checked={showOff} onChange={(e) => setShowOff(e.target.checked)} />
          <span>Show switched-off tests</span>
        </label>
        {canEdit && !adding && (
          <button type="button" className="lab-setup-btn" onClick={() => setAdding(true)}>
            Add a test
          </button>
        )}
      </div>
      {adding && (
        <TestForm
          mode="create"
          sections={data.sections}
          specimenTypes={data.specimenTypes}
          onCancel={() => setAdding(false)}
          onSaved={(test) => {
            setAdding(false)
            setOpenId(test.id)
            onChanged()
          }}
        />
      )}
      <div className="lab-setup-table-wrap">
        <table className="lab-setup-table">
          <thead>
            <tr>
              <th scope="col">Test</th>
              <th scope="col">Code</th>
              <th scope="col">Type</th>
              <th scope="col">Done in</th>
              <th scope="col">Specimen</th>
              <th scope="col">Reference range</th>
              <th scope="col">Critical limit</th>
              <th scope="col"><span className="lab-setup-sr">Open</span></th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={t.id} className={t.id === openId ? 'lab-setup-row--open' : undefined}>
                <td>
                  <span className="lab-setup-strong">{t.name}</span>
                  {!t.active && <span className="lab-setup-tag">Switched off</span>}
                </td>
                <td className="lab-setup-num">{t.code_system} {t.code}</td>
                <td>{KIND_LABEL[t.kind] ?? t.kind}</td>
                <td>{PERFORMED_AT_LABEL[t.performed_at] ?? t.performed_at}</td>
                <td>{t.specimen_type_code
                  ? (specimenNames.get(t.specimen_type_code) ?? 'Not in the specimen list')
                  : 'None'}</td>
                <ConfigCell test={t} count={ranges.get(t.id) ?? 0} />
                <ConfigCell test={t} count={limits.get(t.id) ?? 0} />
                <td className="lab-setup-cell--actions">
                  <button type="button" className="lab-setup-btn"
                    aria-label={`Open ${t.name}`}
                    onClick={() => setOpenId(t.id === openId ? null : t.id)}>
                    {t.id === openId ? 'Close' : 'Open'}
                  </button>
                </td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr><td colSpan={8} className="lab-setup-cell--quiet">No test matches.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {openId && (
        <TestDetail
          key={openId}
          testId={openId}
          data={data}
          canEdit={canEdit}
          onChanged={onChanged}
          onClose={() => setOpenId(null)}
        />
      )}
    </section>
  )
}

type Detail = { id: string; test: LabTest } | { id: string; error: string }

function TestDetail({ testId, data, canEdit, onChanged, onClose }: {
  testId: string
  data: Catalogue
  canEdit: boolean
  onChanged: () => void
  onClose: () => void
}) {
  const [version, setVersion] = useState(0)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    let live = true
    getLabTest(testId).then(
      (test) => { if (live) setDetail({ id: testId, test }) },
      (err: unknown) => { if (live) setDetail({ id: testId, error: toLabError(err).message }) },
    )
    return () => { live = false }
  }, [testId, version])

  const refresh = useCallback(() => {
    setVersion((v) => v + 1)
    onChanged()
  }, [onChanged])

  const current = detail && detail.id === testId ? detail : null
  if (!current) return <p className="lab-setup-hint" role="status">Loading the test</p>
  if ('error' in current) {
    return <p className="lab-setup-error" role="alert">{current.error}</p>
  }
  const { test } = current
  const sectionName = data.sections.find((s) => s.id === test.section_id)?.name
  const specimen = data.specimenTypes.find((s) => s.code === test.specimen_type_code)
  const analytes = data.tests.filter((t) => t.kind === 'analyte' && t.active)

  return (
    <section className="lab-setup-detail" aria-label={test.name}>
      <header className="lab-setup-detail__head">
        <div>
          <h2 className="lab-setup-detail__title">{test.name}</h2>
          <p className="lab-setup-hint">{test.code_system} {test.code} · {test.short_name}</p>
        </div>
        <div className="lab-setup-actions">
          {canEdit && !editing && (
            <button type="button" className="lab-setup-btn" onClick={() => setEditing(true)}>
              Change test
            </button>
          )}
          <button type="button" className="lab-setup-btn" onClick={onClose}>Close</button>
        </div>
      </header>

      {editing ? (
        <TestForm
          mode="edit"
          test={test}
          sections={data.sections}
          specimenTypes={data.specimenTypes}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            refresh()
          }}
        />
      ) : (
        <dl className="lab-setup-facts">
          <div><dt>Type</dt><dd>{KIND_LABEL[test.kind] ?? test.kind}</dd></div>
          <div><dt>Done in</dt><dd>{PERFORMED_AT_LABEL[test.performed_at] ?? test.performed_at}</dd></div>
          <div><dt>Section</dt><dd>{sectionName ?? 'None'}</dd></div>
          <div>
            <dt>Specimen</dt>
            <dd>{specimen ? `${specimen.display}${specimen.container ? `, ${specimen.container}` : ''}` : 'None'}</dd>
          </div>
          {test.kind === 'analyte' && (
            <div><dt>Result</dt><dd>{RESULT_TYPE_LABEL[test.result_type] ?? test.result_type}</dd></div>
          )}
          {takesRanges(test) && (
            <>
              <div><dt>Unit</dt><dd>{test.unit || 'None'}</dd></div>
              <div><dt>Decimal places</dt><dd>{test.decimal_places}</dd></div>
            </>
          )}
          <div><dt>Orderable on its own</dt><dd>{yesNo(test.orderable)}</dd></div>
          <div><dt>In use</dt><dd>{test.active ? 'Yes' : 'No, switched off'}</dd></div>
          <div>
            <dt>Version</dt>
            <dd>{test.version}{test.updated_at ? `, changed ${formatClinicalDateTime(test.updated_at)}` : ''}{test.updated_by ? ` by ${test.updated_by}` : ''}</dd>
          </div>
        </dl>
      )}

      {test.performed_at === 'ward_poct' && (
        <p className="lab-setup-hint">
          Done at the bedside: orders for this test stay on the ward and never reach the
          laboratory&apos;s work lists. Changing it applies to orders placed afterwards.
        </p>
      )}

      {test.kind === 'panel' && (
        <ComponentsEditor panel={test} analytes={analytes} canEdit={canEdit} onSaved={refresh} />
      )}

      {takesRanges(test) ? (
        <div className="lab-setup-editors">
          <RangeEditor test={test} ranges={test.reference_ranges ?? []} canEdit={canEdit}
            onChanged={refresh} />
          <CriticalLimitEditor test={test} limits={test.critical_limits ?? []} canEdit={canEdit}
            onChanged={refresh} />
        </div>
      ) : test.kind === 'analyte' && (
        <p className="lab-setup-hint">
          Reference ranges and critical limits apply only to tests with a number result.
        </p>
      )}
    </section>
  )
}

function ComponentsEditor({ panel, analytes, canEdit, onSaved }: {
  panel: LabTest
  analytes: LabTest[]
  canEdit: boolean
  onSaved: () => void
}) {
  const currentIds = useMemo(() => (panel.components ?? []).map((c) => c.test_id), [panel.components])
  const [editing, setEditing] = useState(false)
  const [chosen, setChosen] = useState<string[]>(currentIds)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const ordered = useMemo(() => {
    const byId = new Map(analytes.map((a) => [a.id, a]))
    const first = currentIds.map((id) => byId.get(id)).filter((a): a is LabTest => Boolean(a))
    const rest = analytes.filter((a) => !currentIds.includes(a.id))
    return [...first, ...rest]
  }, [analytes, currentIds])

  function toggle(id: string, on: boolean) {
    setChosen((prev) => (on ? [...prev.filter((x) => x !== id), id] : prev.filter((x) => x !== id)))
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (chosen.length === 0) {
      setError('A panel needs at least one test.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await setLabPanelComponents(panel.id, chosen)
      setEditing(false)
      onSaved()
    } catch (err) {
      setError(writeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="lab-setup-bands" aria-label="Tests in this panel">
      <h3 className="lab-setup-bands__title">Tests in this panel</h3>
      {(panel.components ?? []).length === 0 ? (
        <p className="lab-setup-gap">This panel has no tests yet, so an order for it cannot reach the laboratory.</p>
      ) : (
        <ol className="lab-setup-components">
          {(panel.components ?? []).map((c) => (
            <li key={c.test_id}>{c.name ?? c.test_id}</li>
          ))}
        </ol>
      )}
      {canEdit && !editing && (
        <button type="button" className="lab-setup-btn" onClick={() => {
          setChosen(currentIds)
          setEditing(true)
        }}>
          Change tests in this panel
        </button>
      )}
      {editing && (
        <form className="lab-setup-form" onSubmit={save}>
          <fieldset className="lab-setup-fieldset">
            <legend>Tests in this panel, in the order they are reported</legend>
            {ordered.map((a) => (
              <label key={a.id} className="lab-setup-check">
                <input type="checkbox" checked={chosen.includes(a.id)}
                  onChange={(e) => toggle(a.id, e.target.checked)} />
                <span>{a.name}</span>
              </label>
            ))}
          </fieldset>
          <div className="lab-setup-actions">
            <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
              {busy ? 'Saving' : 'Save tests'}
            </button>
            <button type="button" className="lab-setup-btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
          {error && <p className="lab-setup-error" role="alert">{error}</p>}
        </form>
      )}
    </section>
  )
}

interface TestFormProps {
  mode: 'create' | 'edit'
  test?: LabTest
  sections: LabSection[]
  specimenTypes: LabSpecimenType[]
  onCancel: () => void
  onSaved: (test: LabTest) => void
}

function TestForm({ mode, test, sections, specimenTypes, onCancel, onSaved }: TestFormProps) {
  const [codeSystem, setCodeSystem] = useState<LabCodeSystem>('LOCAL')
  const [code, setCode] = useState('')
  const [kind, setKind] = useState<LabTestKind>(test?.kind ?? 'analyte')
  const [name, setName] = useState(test?.name ?? '')
  const [shortName, setShortName] = useState(test?.short_name ?? '')
  const [performedAt, setPerformedAt] = useState<LabPerformedAt>(test?.performed_at ?? 'lab')
  const [sectionId, setSectionId] = useState(test?.section_id ?? '')
  const [specimenCode, setSpecimenCode] = useState(test?.specimen_type_code ?? '')
  const [resultType, setResultType] = useState<LabResultType>(test?.result_type ?? 'numeric')
  const [unit, setUnit] = useState(test?.unit ?? '')
  const [decimalPlaces, setDecimalPlaces] = useState(String(test?.decimal_places ?? 0))
  const [codedText, setCodedText] = useState(codedValuesToText(test?.coded_values_json))
  const [orderable, setOrderable] = useState(test ? Boolean(test.orderable) : true)
  const [active, setActive] = useState(test ? Boolean(test.active) : true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const isPanel = kind === 'panel'

  function problem(): string | null {
    if (mode === 'create' && !code.trim()) return 'Give the test its code.'
    if (!name.trim()) return 'Give the test a name.'
    if (!shortName.trim()) return 'Give the test a short name for labels.'
    if (shortName.trim().length > 16) return 'The short name prints on a label: 16 characters at most.'
    if (performedAt === 'lab' && !sectionId) return 'A laboratory test needs a section.'
    if (!specimenCode) return 'Choose the specimen type.'
    if (!isPanel && resultType === 'coded' && codedValuesFromText(codedText).length === 0) {
      return 'List the choices for this test, separated by commas.'
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
    const fields: Partial<LabTest> = {
      name: name.trim(),
      short_name: shortName.trim(),
      performed_at: performedAt,
      section_id: sectionId || null,
      specimen_type_code: specimenCode,
      orderable,
    }
    if (!isPanel) {
      fields.result_type = resultType
      fields.unit = unit.trim() || null
      fields.decimal_places = Number(decimalPlaces)
      fields.coded_values_json = resultType === 'coded' ? codedValuesFromText(codedText) : null
    }
    setBusy(true)
    setError(null)
    try {
      if (mode === 'create') {
        const created = await createLabTest({
          ...fields,
          code_system: codeSystem,
          code: code.trim(),
          kind,
          name: name.trim(),
          short_name: shortName.trim(),
          specimen_type_code: specimenCode,
        })
        onSaved(created)
      } else if (test) {
        const changes: Partial<LabTest> = {}
        // Blank and missing are the same value (a unit of '' and of null both
        // mean "no unit"), so an untouched field is never sent as a change.
        const blankAsEmpty = (v: unknown) => (v === null || v === undefined ? '' : v)
        for (const [key, value] of Object.entries({ ...fields, active })) {
          const before = test[key]
          const same = key === 'coded_values_json'
            ? codedValuesToText(before) === codedValuesToText(value)
            : (typeof value === 'boolean'
              ? Boolean(before) === value
              : blankAsEmpty(before) === blankAsEmpty(value))
          if (!same) (changes as Record<string, unknown>)[key] = value
        }
        if (Object.keys(changes).length === 0) {
          setError('Nothing has changed.')
          return
        }
        onSaved(await updateLabTest(test.id, changes))
      }
    } catch (err) {
      setError(writeError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="lab-setup-form" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="lab-setup-form__title">
        {mode === 'create' ? 'Add a test' : `Change ${test?.name ?? 'test'}`}
      </h3>
      <div className="lab-setup-grid">
        {mode === 'create' && (
          <>
            <label className="lab-setup-field">
              <span>Code system</span>
              <select className="lab-setup-input" value={codeSystem}
                onChange={(e) => setCodeSystem(e.target.value as LabCodeSystem)}>
                <option value="LOINC">LOINC</option>
                <option value="LOCAL">Local code</option>
              </select>
            </label>
            <label className="lab-setup-field">
              <span>Code</span>
              <input className="lab-setup-input" value={code} maxLength={64}
                onChange={(e) => setCode(e.target.value)} />
            </label>
            <label className="lab-setup-field">
              <span>Type</span>
              <select className="lab-setup-input" value={kind}
                onChange={(e) => setKind(e.target.value as LabTestKind)}>
                <option value="analyte">Test</option>
                <option value="panel">Panel of tests</option>
              </select>
            </label>
          </>
        )}
        <label className="lab-setup-field">
          <span>Name</span>
          <input className="lab-setup-input" value={name} maxLength={200}
            onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Short name (on labels, 16 characters)</span>
          <input className="lab-setup-input" value={shortName} maxLength={16}
            onChange={(e) => setShortName(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Done in</span>
          <select className="lab-setup-input" value={performedAt}
            onChange={(e) => setPerformedAt(e.target.value as LabPerformedAt)}>
            <option value="lab">Laboratory</option>
            <option value="ward_poct">At the bedside (stays on the ward)</option>
          </select>
        </label>
        <label className="lab-setup-field">
          <span>Section</span>
          <select className="lab-setup-input" value={sectionId}
            onChange={(e) => setSectionId(e.target.value)}>
            <option value="">None</option>
            {sections.filter((s) => s.active || s.id === sectionId).map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <label className="lab-setup-field">
          <span>Specimen type</span>
          <select className="lab-setup-input" value={specimenCode}
            onChange={(e) => setSpecimenCode(e.target.value)}>
            <option value="">Choose</option>
            {specimenTypes.filter((s) => s.active || s.code === specimenCode).map((s) => (
              <option key={s.code} value={s.code}>{s.display}</option>
            ))}
          </select>
        </label>
        {!isPanel && (
          <>
            <label className="lab-setup-field">
              <span>Result</span>
              <select className="lab-setup-input" value={resultType}
                onChange={(e) => setResultType(e.target.value as LabResultType)}>
                <option value="numeric">Number</option>
                <option value="text">Text</option>
                <option value="coded">Choice from a list</option>
              </select>
            </label>
            <label className="lab-setup-field">
              <span>Unit</span>
              <input className="lab-setup-input" value={unit} maxLength={40}
                onChange={(e) => setUnit(e.target.value)} />
            </label>
            <label className="lab-setup-field">
              <span>Decimal places reported</span>
              <select className="lab-setup-input" value={decimalPlaces}
                onChange={(e) => setDecimalPlaces(e.target.value)}>
                {[0, 1, 2, 3].map((n) => <option key={n} value={String(n)}>{n}</option>)}
              </select>
            </label>
          </>
        )}
      </div>
      {!isPanel && resultType === 'coded' && (
        <label className="lab-setup-field lab-setup-field--wide">
          <span>Choices, separated by commas</span>
          <input className="lab-setup-input" value={codedText}
            onChange={(e) => setCodedText(e.target.value)} />
        </label>
      )}
      <label className="lab-setup-check">
        <input type="checkbox" checked={orderable} onChange={(e) => setOrderable(e.target.checked)} />
        <span>Can be ordered on its own</span>
      </label>
      {mode === 'edit' && (
        <label className="lab-setup-check">
          <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
          <span>In use (untick to switch the test off)</span>
        </label>
      )}
      {mode === 'edit' && (
        <p className="lab-setup-hint">
          A change of unit or decimal places is refused while the test has a current range or
          limit: retire those first.
        </p>
      )}
      <div className="lab-setup-actions">
        <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
          {busy ? 'Saving' : mode === 'create' ? 'Add test' : 'Save changes'}
        </button>
        <button type="button" className="lab-setup-btn" onClick={onCancel}>Cancel</button>
      </div>
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

// ════════════════════════════════════════════════════════════════════════════
//  Specimen types, sections and rejection reasons
// ════════════════════════════════════════════════════════════════════════════

function useSave<T>(save: () => Promise<T>, onDone: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = useCallback(async (problem: string | null) => {
    if (problem) {
      setError(problem)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await save()
      onDone()
    } catch (err) {
      setError(writeError(err))
    } finally {
      setBusy(false)
    }
  }, [save, onDone])
  return { busy, error, run }
}

const CODE_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/

function FormButtons({ busy, label, onCancel }: { busy: boolean; label: string; onCancel: () => void }) {
  return (
    <div className="lab-setup-actions">
      <button type="submit" className="lab-setup-btn lab-setup-btn--primary" disabled={busy}>
        {busy ? 'Saving' : label}
      </button>
      <button type="button" className="lab-setup-btn" onClick={onCancel}>Cancel</button>
    </div>
  )
}

function StatusCell({ active }: { active: boolean | number }) {
  return active ? <td>In use</td> : <td className="lab-setup-cell--quiet">Switched off</td>
}

function SpecimenTypeForm({ row, onCancel, onSaved }: {
  row: LabSpecimenType | null
  onCancel: () => void
  onSaved: () => void
}) {
  const [code, setCode] = useState(row?.code ?? '')
  const [display, setDisplay] = useState(row?.display ?? '')
  const [container, setContainer] = useState(row?.container ?? '')
  const [colour, setColour] = useState(row?.container_colour_name ?? '')
  const [volume, setVolume] = useState(row?.min_volume_ul ? String(row.min_volume_ul) : '')
  const [stability, setStability] = useState(row?.stability_minutes ? String(row.stability_minutes) : '')
  const [note, setNote] = useState(row?.handling_note ?? '')
  const [active, setActive] = useState(row ? Boolean(row.active) : true)
  const volumeValue = wholeNumber(volume, 'Minimum volume')
  const stabilityValue = wholeNumber(stability, 'Stability')
  const save = useCallback(() => saveLabSpecimenType(code.trim(), {
    display: display.trim(),
    container: container.trim() || null,
    container_colour_name: colour.trim() || null,
    min_volume_ul: typeof volumeValue === 'number' ? volumeValue : null,
    stability_minutes: typeof stabilityValue === 'number' ? stabilityValue : null,
    handling_note: note.trim() || null,
    active,
  }), [code, display, container, colour, volumeValue, stabilityValue, note, active])
  const { busy, error, run } = useSave(save, onSaved)

  function problem(): string | null {
    if (!row && !CODE_PATTERN.test(code.trim())) {
      return 'The code is lower-case letters, numbers, hyphens or underscores.'
    }
    if (!display.trim()) return 'Give the specimen type a name.'
    if (typeof volumeValue === 'string') return volumeValue
    if (typeof stabilityValue === 'string') return stabilityValue
    return null
  }

  return (
    <form className="lab-setup-form" onSubmit={(e) => { e.preventDefault(); void run(problem()) }}>
      <h3 className="lab-setup-form__title">{row ? `Change ${row.display}` : 'Add a specimen type'}</h3>
      <div className="lab-setup-grid">
        {!row && (
          <label className="lab-setup-field">
            <span>Code (used inside Orb)</span>
            <input className="lab-setup-input" value={code} maxLength={64}
              onChange={(e) => setCode(e.target.value)} />
          </label>
        )}
        <label className="lab-setup-field">
          <span>Name</span>
          <input className="lab-setup-input" value={display} onChange={(e) => setDisplay(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Container</span>
          <input className="lab-setup-input" value={container} onChange={(e) => setContainer(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Container colour</span>
          <input className="lab-setup-input" value={colour} onChange={(e) => setColour(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Minimum volume (microlitres)</span>
          <input className="lab-setup-input" inputMode="numeric" value={volume}
            onChange={(e) => setVolume(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Stable for (minutes)</span>
          <input className="lab-setup-input" inputMode="numeric" value={stability}
            onChange={(e) => setStability(e.target.value)} />
        </label>
      </div>
      <label className="lab-setup-field lab-setup-field--wide">
        <span>Handling note</span>
        <input className="lab-setup-input" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
      </label>
      <label className="lab-setup-check">
        <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
        <span>In use</span>
      </label>
      <FormButtons busy={busy} label={row ? 'Save changes' : 'Add specimen type'} onCancel={onCancel} />
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

function SpecimenTypesArea({ rows, canEdit, onChanged }: {
  rows: LabSpecimenType[]
  canEdit: boolean
  onChanged: () => void
}) {
  const [editing, setEditing] = useState<LabSpecimenType | 'new' | null>(null)
  const done = useCallback(() => {
    setEditing(null)
    onChanged()
  }, [onChanged])
  return (
    <section className="lab-setup-area-body" aria-label="Specimen types">
      {canEdit && editing === null && (
        <div className="lab-setup-toolbar">
          <button type="button" className="lab-setup-btn" onClick={() => setEditing('new')}>
            Add a specimen type
          </button>
        </div>
      )}
      {editing !== null && (
        <SpecimenTypeForm key={editing === 'new' ? 'new' : editing.code}
          row={editing === 'new' ? null : editing} onCancel={() => setEditing(null)} onSaved={done} />
      )}
      <div className="lab-setup-table-wrap">
        <table className="lab-setup-table">
          <thead>
            <tr>
              <th scope="col">Specimen type</th>
              <th scope="col">Container</th>
              <th scope="col">Colour</th>
              <th scope="col">Minimum volume</th>
              <th scope="col">Stable for</th>
              <th scope="col">Handling</th>
              <th scope="col">Status</th>
              {canEdit && <th scope="col"><span className="lab-setup-sr">Change</span></th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code}>
                <td className="lab-setup-strong">{r.display}</td>
                <td>{r.container || 'Not recorded'}</td>
                <td>{r.container_colour_name || 'Not recorded'}</td>
                <td className="lab-setup-num">{r.min_volume_ul ? `${r.min_volume_ul} µL` : 'Not recorded'}</td>
                <td className="lab-setup-num">{r.stability_minutes ? `${r.stability_minutes} min` : 'Not recorded'}</td>
                <td>{r.handling_note || 'None'}</td>
                <StatusCell active={r.active} />
                {canEdit && (
                  <td className="lab-setup-cell--actions">
                    <button type="button" className="lab-setup-btn" aria-label={`Change ${r.display}`}
                      onClick={() => setEditing(r)}>Change</button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function SectionForm({ row, onCancel, onSaved }: {
  row: LabSection | null
  onCancel: () => void
  onSaved: () => void
}) {
  const [code, setCode] = useState(row?.id ?? '')
  const [name, setName] = useState(row?.name ?? '')
  const [order, setOrder] = useState(String(row?.sort_order ?? 0))
  const [active, setActive] = useState(row ? Boolean(row.active) : true)
  const save = useCallback(() => saveLabSection(code.trim(), {
    name: name.trim(), sort_order: Number(order), active,
  }), [code, name, order, active])
  const { busy, error, run } = useSave(save, onSaved)

  function problem(): string | null {
    if (!row && !CODE_PATTERN.test(code.trim())) {
      return 'The code is lower-case letters, numbers, hyphens or underscores.'
    }
    if (!name.trim()) return 'Give the section a name.'
    if (!/^\d+$/.test(order.trim())) return 'The order is a whole number.'
    return null
  }

  return (
    <form className="lab-setup-form" onSubmit={(e) => { e.preventDefault(); void run(problem()) }}>
      <h3 className="lab-setup-form__title">{row ? `Change ${row.name}` : 'Add a section'}</h3>
      <div className="lab-setup-grid">
        {!row && (
          <label className="lab-setup-field">
            <span>Code (used inside Orb)</span>
            <input className="lab-setup-input" value={code} maxLength={64}
              onChange={(e) => setCode(e.target.value)} />
          </label>
        )}
        <label className="lab-setup-field">
          <span>Name</span>
          <input className="lab-setup-input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="lab-setup-field">
          <span>Order in lists</span>
          <input className="lab-setup-input" inputMode="numeric" value={order}
            onChange={(e) => setOrder(e.target.value)} />
        </label>
      </div>
      <label className="lab-setup-check">
        <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
        <span>In use</span>
      </label>
      <FormButtons busy={busy} label={row ? 'Save changes' : 'Add section'} onCancel={onCancel} />
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

function SectionsArea({ rows, canEdit, onChanged }: {
  rows: LabSection[]
  canEdit: boolean
  onChanged: () => void
}) {
  const [editing, setEditing] = useState<LabSection | 'new' | null>(null)
  const done = useCallback(() => {
    setEditing(null)
    onChanged()
  }, [onChanged])
  return (
    <section className="lab-setup-area-body" aria-label="Sections">
      {canEdit && editing === null && (
        <div className="lab-setup-toolbar">
          <button type="button" className="lab-setup-btn" onClick={() => setEditing('new')}>
            Add a section
          </button>
        </div>
      )}
      {editing !== null && (
        <SectionForm key={editing === 'new' ? 'new' : editing.id}
          row={editing === 'new' ? null : editing} onCancel={() => setEditing(null)} onSaved={done} />
      )}
      <div className="lab-setup-table-wrap">
        <table className="lab-setup-table">
          <thead>
            <tr>
              <th scope="col">Section</th>
              <th scope="col">Order</th>
              <th scope="col">Status</th>
              {canEdit && <th scope="col"><span className="lab-setup-sr">Change</span></th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="lab-setup-strong">{r.name}</td>
                <td className="lab-setup-num">{r.sort_order}</td>
                <StatusCell active={r.active} />
                {canEdit && (
                  <td className="lab-setup-cell--actions">
                    <button type="button" className="lab-setup-btn" aria-label={`Change ${r.name}`}
                      onClick={() => setEditing(r)}>Change</button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function RejectionReasonForm({ row, onCancel, onSaved }: {
  row: LabRejectionReason | null
  onCancel: () => void
  onSaved: () => void
}) {
  const [code, setCode] = useState(row?.code ?? '')
  const [display, setDisplay] = useState(row?.display ?? '')
  const [incident, setIncident] = useState(row ? Boolean(row.incident_prompt) : false)
  const [active, setActive] = useState(row ? Boolean(row.active) : true)
  const save = useCallback(() => saveLabRejectionReason(code.trim(), {
    display: display.trim(), incident_prompt: incident, active,
  }), [code, display, incident, active])
  const { busy, error, run } = useSave(save, onSaved)

  function problem(): string | null {
    if (!row && !CODE_PATTERN.test(code.trim())) {
      return 'The code is lower-case letters, numbers, hyphens or underscores.'
    }
    if (!display.trim()) return 'Give the reason a name.'
    return null
  }

  return (
    <form className="lab-setup-form" onSubmit={(e) => { e.preventDefault(); void run(problem()) }}>
      <h3 className="lab-setup-form__title">{row ? `Change ${row.display}` : 'Add a rejection reason'}</h3>
      <div className="lab-setup-grid">
        {!row && (
          <label className="lab-setup-field">
            <span>Code (used inside Orb)</span>
            <input className="lab-setup-input" value={code} maxLength={64}
              onChange={(e) => setCode(e.target.value)} />
          </label>
        )}
        <label className="lab-setup-field">
          <span>Name</span>
          <input className="lab-setup-input" value={display} onChange={(e) => setDisplay(e.target.value)} />
        </label>
      </div>
      <label className="lab-setup-check">
        <input type="checkbox" checked={incident} onChange={(e) => setIncident(e.target.checked)} />
        <span>Prompt for an incident report when a specimen is rejected for this reason</span>
      </label>
      <label className="lab-setup-check">
        <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
        <span>In use</span>
      </label>
      <FormButtons busy={busy} label={row ? 'Save changes' : 'Add reason'} onCancel={onCancel} />
      {error && <p className="lab-setup-error" role="alert">{error}</p>}
    </form>
  )
}

function RejectionReasonsArea({ rows, canEdit, onChanged }: {
  rows: LabRejectionReason[]
  canEdit: boolean
  onChanged: () => void
}) {
  const [editing, setEditing] = useState<LabRejectionReason | 'new' | null>(null)
  const done = useCallback(() => {
    setEditing(null)
    onChanged()
  }, [onChanged])
  return (
    <section className="lab-setup-area-body" aria-label="Rejection reasons">
      {canEdit && editing === null && (
        <div className="lab-setup-toolbar">
          <button type="button" className="lab-setup-btn" onClick={() => setEditing('new')}>
            Add a rejection reason
          </button>
        </div>
      )}
      {editing !== null && (
        <RejectionReasonForm key={editing === 'new' ? 'new' : editing.code}
          row={editing === 'new' ? null : editing} onCancel={() => setEditing(null)} onSaved={done} />
      )}
      <div className="lab-setup-table-wrap">
        <table className="lab-setup-table">
          <thead>
            <tr>
              <th scope="col">Reason</th>
              <th scope="col">Incident report prompt</th>
              <th scope="col">Status</th>
              {canEdit && <th scope="col"><span className="lab-setup-sr">Change</span></th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.code}>
                <td className="lab-setup-strong">{r.display}</td>
                <td>{yesNo(r.incident_prompt)}</td>
                <StatusCell active={r.active} />
                {canEdit && (
                  <td className="lab-setup-cell--actions">
                    <button type="button" className="lab-setup-btn" aria-label={`Change ${r.display}`}
                      onClick={() => setEditing(r)}>Change</button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
