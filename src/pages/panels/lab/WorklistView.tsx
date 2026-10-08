/**
 * Worklists: each laboratory section's bench (lis-10).
 *
 * Pick a section (biochemistry, haematology...) and the tests on its received
 * samples come up in the order the server decides: STAT first, then the
 * oldest receipt. Results are typed into the entry grid (./ResultEntryGrid),
 * which checks each value at the test's reporting precision before it is
 * sent; the server checks again and decides the flag, which comes only from
 * an approved reference range (rulings LIS-R2 and LIS-R7). A test cancelled
 * after its sample was received leaves the list, and the screen says who
 * cancelled it and when, so the bench stops.
 *
 * Each worklist read is audited, so the list loads when a section opens,
 * after each saved result and when asked; there is no polling. Entering a
 * result finishes open work, so this view stays while the module drains.
 */
import { useCallback, useEffect, useState } from 'react'

import { toLabError } from '../../../api/lab'
import {
  getWorklist,
  listWorklists,
  type Worklist,
  type WorklistSection,
} from '../../../api/lab/results'
import { formatAccession } from '../../../api/lab/specimens'
import { formatClinicalTime } from '../../../lib/datetime'
import ResultEntryGrid from './ResultEntryGrid'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'

export const viewId = 'worklists'
export const viewLabel = 'Worklists'
export const viewOrder = 30
export const viewPermission = 'lab.result'
export const viewWhenDraining = 'show'

function sectionCounts(section: WorklistSection): string {
  const parts = [`${section.waiting} waiting`]
  if (section.stat_waiting) parts.push(`${section.stat_waiting} STAT`)
  if (section.entered) parts.push(`${section.entered} entered`)
  return parts.join(', ')
}

export default function WorklistView({ onModuleOff }: LabViewProps) {
  const [sections, setSections] = useState<WorklistSection[] | null>(null)
  const [sectionId, setSectionId] = useState<string | null>(null)
  const [bench, setBench] = useState<Worklist | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [warning, setWarning] = useState<string | null>(null)
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)

  const fail = useCallback((err: unknown, fallback: string) => {
    const failure = toLabError(err)
    if (failure.isModuleDisabled) onModuleOff()
    else setError(failure.message || fallback)
  }, [onModuleOff])

  const loadSections = useCallback(async () => {
    try {
      const out = await listWorklists()
      setSections(out.sections)
      setSectionId(current => current
        ?? out.sections.find(s => s.waiting > 0)?.id
        ?? out.sections[0]?.id
        ?? null)
      if (out.sections.length === 0) setLoading(false)
    } catch (err) {
      setLoading(false)
      fail(err, 'The laboratory sections could not be loaded.')
    }
  }, [fail])

  const loadBench = useCallback(async (id: string) => {
    setLoading(true)
    try {
      setBench(await getWorklist(id))
      setError(null)
      setCheckedAt(new Date())
    } catch (err) {
      fail(err, 'The worklist could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [fail])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void loadSections() }, [loadSections])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger, as above.
    if (sectionId) void loadBench(sectionId)
  }, [sectionId, loadBench])

  const refresh = () => {
    void loadSections()
    if (sectionId) void loadBench(sectionId)
  }

  const onSaved = (message: string) => {
    setWarning(null)
    setNotice(message)
    void loadSections()
    if (sectionId) void loadBench(sectionId)
  }

  // The server refused a save because the row changed under the bench:
  // reload, so the screen shows what is now true.
  const onStale = (message: string) => {
    setNotice(null)
    setWarning(`${message} The worklist has been reloaded.`)
    void loadSections()
    if (sectionId) void loadBench(sectionId)
  }

  const current = bench && bench.section.id === sectionId ? bench : null

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Worklists</h2>
          <p className="lab-bench-sub">
            STAT first, then the oldest receipt. Each value is checked at the test&apos;s reporting
            precision; a flag comes only from an approved reference range.
            {checkedAt ? ` Checked at ${formatClinicalTime(checkedAt)}.` : ''}
          </p>
        </div>
        <button type="button" className="lab-bench-btn" onClick={refresh} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {sections && sections.length > 0 && (
        <div className="lab-bench-sections" role="group" aria-label="Laboratory sections">
          {sections.map(section => (
            <button
              key={section.id}
              type="button"
              className={`lab-bench-section-btn${section.id === sectionId ? ' lab-bench-section-btn--on' : ''}`}
              aria-pressed={section.id === sectionId}
              onClick={() => { setNotice(null); setWarning(null); setSectionId(section.id) }}
            >
              {section.name} <span className="lab-bench-quiet">· {sectionCounts(section)}</span>
            </button>
          ))}
        </div>
      )}

      {sections && sections.length === 0 && (
        <p className="lab-bench-empty">
          No laboratory section is set up yet. A pathologist adds sections under Setup.
        </p>
      )}

      {notice && <p className="lab-bench-note lab-bench-note--ok" role="status">{notice}</p>}

      {warning && <p className="lab-bench-note lab-bench-note--warn" role="status">{warning}</p>}

      {error && (
        <div className="lab-bench-note lab-bench-note--error" role="alert">
          <p>Couldn&apos;t load the worklist. {error}</p>
          <p>This is a display failure, not an empty bench.</p>
        </div>
      )}

      {current && current.cancelled.length > 0 && (
        <div className="lab-bench-note lab-bench-note--warn" role="status">
          <p className="lab-bench-note-title">Cancelled after the sample was received</p>
          {current.cancelled.map(c => (
            <p key={c.item_id}>
              {c.test_name} for {c.patient.name} ({formatAccession(c.specimen_id)}): cancelled
              {c.cancelled_by_name ? ` by ${c.cancelled_by_name}` : ''}
              {c.cancelled_at ? ` at ${formatClinicalTime(c.cancelled_at)}` : ''}
              {c.reason ? `. Reason: ${c.reason}` : ''}. Do not run it.
            </p>
          ))}
        </div>
      )}

      {current && !error && current.items.length === 0 && (
        <p className="lab-bench-empty">Nothing is waiting on the {current.section.name.toLowerCase()} bench.</p>
      )}

      {current && !error && current.items.length > 0 && (
        <ResultEntryGrid key={current.section.id} items={current.items} onSaved={onSaved}
          onStale={onStale} />
      )}
    </div>
  )
}
