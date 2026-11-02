import { useEffect, useState } from 'react'
import { apiFetch } from '../api/client'

interface SummarySnapshot {
  patient_id: string
  status: 'ready' | 'outdated' | 'unavailable'
  reason?: string
  generated_at?: string | null
  source_captured_at?: string | null
  refresh_after_seconds?: number
  summary: {
    overview: string
    important_findings: string[]
    outstanding_items: string[]
    data_gaps: string[]
  } | null
  source_metadata?: {
    partial_coverage?: boolean
    section_coverage?: Record<string, { matching_rows: number; included_rows: number; omitted_older_rows: number }>
  }
}

/** Server snapshots only. Never put patient summaries in browser persistence. */
export function PatientSummary({ patientId }: { patientId: string }) {
  // Keying the body prevents even one render of the previous patient's draft.
  return <PatientSummaryBody key={patientId} patientId={patientId} />
}

function PatientSummaryBody({ patientId }: { patientId: string }) {
  const [snapshot, setSnapshot] = useState<SummarySnapshot | null>(null)
  const [failed, setFailed] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [revision, setRevision] = useState(0)
  const [expiredSnapshot, setExpiredSnapshot] = useState<SummarySnapshot | null>(null)
  const expired = snapshot !== null && expiredSnapshot === snapshot

  useEffect(() => {
    const remaining = snapshot?.refresh_after_seconds
    if (typeof remaining !== 'number' || !Number.isFinite(remaining)) return
    const timer = window.setTimeout(() => setExpiredSnapshot(snapshot), Math.max(0, remaining) * 1000)
    return () => window.clearTimeout(timer)
  }, [snapshot])

  useEffect(() => {
    const controller = new AbortController()
    let live = true
    let reading = false
    const read = async () => {
      if (reading) return
      reading = true
      try {
        const answer = await apiFetch<SummarySnapshot>(`/patients/${encodeURIComponent(patientId)}/summary`, {
          signal: controller.signal,
        })
        if (live) {
          // A response for a different patient is never usable, even if a
          // backend/proxy bug returned a superficially valid summary.
          if (answer.patient_id !== patientId) throw new Error('Summary patient mismatch')
          setSnapshot(answer)
          setFailed(false)
        }
      } catch {
        if (live) {
          setSnapshot(null)
          setFailed(true)
        }
      } finally {
        reading = false
      }
    }
    void read()
    const timer = window.setInterval(() => { void read() }, 30_000)
    return () => { live = false; controller.abort(); window.clearInterval(timer) }
  }, [patientId, revision])

  if (snapshot?.reason === 'feature_disabled') return null
  const draft = !expired && snapshot && ['ready', 'outdated'].includes(snapshot.status) ? snapshot.summary : null
  const captured = snapshot?.source_captured_at || snapshot?.generated_at
  const capturedDate = captured ? new Date(captured) : null
  const asOf = capturedDate && !Number.isNaN(capturedDate.getTime()) ? capturedDate.toLocaleString() : null

  const refresh = async () => {
    setRefreshing(true)
    try {
      const answer = await apiFetch<SummarySnapshot>(`/patients/${encodeURIComponent(patientId)}/summary/refresh`, { method: 'POST' })
      if (answer.patient_id !== patientId) throw new Error('Summary patient mismatch')
      setSnapshot(answer)
      setFailed(false)
      setRevision(value => value + 1)
    } catch {
      setSnapshot(null)
      setFailed(true)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <section className="glass-card course-timeline" aria-label="Patient summary">
      <div className="course-timeline__head">
        <h3 className="detail__section-title" style={{ margin: 0 }}>Patient summary</h3>
        <button type="button" className="course-timeline__retry" disabled={refreshing} onClick={() => { void refresh() }}>
          {refreshing ? 'Requesting…' : failed ? 'Retry' : 'Refresh draft'}
        </button>
      </div>
      <p className="text-muted">AI draft · Check against the chart before use. Refreshed in the background every 30 minutes when available.</p>
      {draft ? (
        <>
          <p className="text-muted">{asOf ? `Records captured ${asOf}. ` : ''}Selected chart records; this is not the full patient history.</p>
          {snapshot?.source_metadata?.partial_coverage && <p>Some older records are outside this draft. Review the full chart for earlier findings.</p>}
          {snapshot?.status === 'outdated' && <p role="status">New observations have arrived since this draft. Check the latest vitals and chart.</p>}
          <p>{draft.overview}</p>
          {([
            ['Important findings', draft.important_findings],
            ['Outstanding items', draft.outstanding_items],
            ['Data gaps', draft.data_gaps],
          ] as const).map(([heading, items]) => items.length > 0 && (
            <div key={heading}>
              <h4>{heading}</h4>
              <ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul>
            </div>
          ))}
        </>
      ) : (
        <p role={failed ? 'alert' : 'status'}>
          {failed ? 'The summary could not be checked. Read the chart directly.'
            : snapshot?.reason === 'source_changed' ? 'The chart changed. A new draft is needed; read the chart directly.'
              : snapshot?.reason === 'generation_paused' ? 'Background summaries are paused. Read the chart directly.'
                : expired ? 'The previous draft has expired. Read the chart while it refreshes.'
                : snapshot?.reason === 'expired' ? 'The previous draft has expired. Read the chart while it refreshes.'
                  : 'No current draft is available. Read the chart while a background summary is prepared.'}
        </p>
      )}
    </section>
  )
}
