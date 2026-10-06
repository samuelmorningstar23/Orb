/**
 * WHO Surgical Safety Checklist (2009) — the interactive attestation UI over
 * the backend safety record (server/services/who_checklist.py).
 *
 * Contract, with the server as the authority:
 *   GET  /surgery/checklist/template  → {version, phases[{id,label,when,items[{id,text}]}]}
 *   GET  /surgery/{id}/checklist      → {version, state{item_id→{checked,by,at}}, progress, completed}
 *   POST /surgery/{id}/checklist      → body {item_id, checked}; the attesting
 *        actor is captured server-side from the authenticated session (never
 *        sent by the client), audited fail-closed, and the updated state is
 *        returned.
 *
 * The backend enforces NO phase ordering and NO post-completion lock — any item
 * may be checked or unchecked at any time, and "completed" simply means every
 * item is attested. This UI mirrors that contract exactly: all three phases
 * stay open, and completion is shown as a state, not enforced as a lock the
 * server would not honour. Every render after a write shows the server's
 * returned state, never an optimistic local guess.
 */
import { useCallback, useEffect, useState } from 'react'
import { getWhoChecklist, getWhoChecklistTemplate, setWhoChecklistItem, getUser } from '../../api/client'
import { formatUTCTime } from '../../lib/datetime'
import './WhoSurgicalChecklist.css'

interface WhoTemplateItem { id: string; text: string }
interface WhoTemplatePhase { id: string; label: string; when: string; items: WhoTemplateItem[] }
interface WhoTemplate { version: string; phases: WhoTemplatePhase[] }
/** `by` is the attester's user id (the record's key); `by_name` is their name,
 *  resolved by the server at read time, or null when the account is gone. */
interface WhoItemState { checked: boolean; by: string; by_name?: string | null; at: string }
interface WhoChecklistState {
  version: string
  state: Record<string, WhoItemState>
  progress: { checked: number; total: number }
  completed: boolean
}

/** The clinical "09:12 AM", the same form every other screen uses. */
function formatAttestTime(at: string): string {
  return formatUTCTime(at)
}

export default function WhoSurgicalChecklist({ surgeryId }: { surgeryId: string }) {
  const [template, setTemplate] = useState<WhoTemplate | null>(null)
  const [checklist, setChecklist] = useState<WhoChecklistState | null>(null)
  const [loadError, setLoadError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [saving, setSaving] = useState(false)

  const user = getUser()

  const load = useCallback(async () => {
    setLoadError('')
    try {
      const [tpl, state] = await Promise.all([
        getWhoChecklistTemplate(),
        getWhoChecklist(surgeryId),
      ])
      setTemplate(tpl)
      setChecklist(state)
    } catch (e) {
      setLoadError((e instanceof Error ? e.message : '') || 'Could not load the WHO checklist')
    }
  }, [surgeryId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setChecklist(null)
    load()
  }, [load])

  const toggleItem = useCallback(async (itemId: string, checked: boolean) => {
    if (saving) return
    setSaving(true)
    setSaveError('')
    try {
      setChecklist(await setWhoChecklistItem(surgeryId, itemId, checked))
    } catch (e) {
      setSaveError((e instanceof Error ? e.message : '') || 'Could not record the checklist item')
      // A failed write must leave the row showing the server's truth, not the
      // failed click — this is the OR's safety record, not a local form.
      try { setChecklist(await getWhoChecklist(surgeryId)) } catch { /* the save error above is already on screen */ }
    } finally {
      setSaving(false)
    }
  }, [surgeryId, saving])

  if (loadError) {
    return (
      <div className="glass-card who-checklist">
        <h4 className="who-checklist__title">WHO Surgical Safety Checklist</h4>
        <div className="who-checklist__error" role="alert">
          <span>{loadError}</span>
          <button type="button" className="who-checklist__retry" onClick={load}>Retry</button>
        </div>
      </div>
    )
  }

  if (!template || !checklist) {
    return (
      <div className="glass-card who-checklist">
        <h4 className="who-checklist__title">WHO Surgical Safety Checklist</h4>
        <div className="who-checklist__loading">Loading checklist…</div>
      </div>
    )
  }

  const { progress, completed, state } = checklist

  return (
    <div className="glass-card who-checklist">
      <div className="who-checklist__header">
        <h4 className="who-checklist__title">
          WHO Surgical Safety Checklist
          <span className="who-checklist__version">{template.version}</span>
        </h4>
        <span className="who-checklist__count">{progress.checked}/{progress.total}</span>
      </div>

      <div
        className="who-checklist__progress"
        role="progressbar"
        aria-label="WHO checklist progress"
        aria-valuemin={0}
        aria-valuemax={progress.total}
        aria-valuenow={progress.checked}
      >
        <div
          className="who-checklist__progress-fill"
          style={{ width: `${progress.total ? (progress.checked / progress.total) * 100 : 0}%` }}
        />
      </div>

      {user && (
        <div className="who-checklist__actor">
          Attesting as {user.name}{user.role ? ` (${user.role})` : ''}, each item is
          recorded against this account in the audit trail.
        </div>
      )}

      {completed && (
        <div className="who-checklist__complete" role="status">
          Checklist complete, all {progress.total} items attested.
        </div>
      )}

      {saveError && <div className="who-checklist__error" role="alert">{saveError}</div>}

      {template.phases.map(phase => {
        const phaseChecked = phase.items.filter(i => state[i.id]?.checked).length
        const phaseDone = phaseChecked === phase.items.length
        return (
          <section key={phase.id} className="who-checklist__phase" aria-label={phase.label}>
            <div className="who-checklist__phase-header">
              <span className="who-checklist__phase-label">{phase.label}</span>
              <span className="who-checklist__phase-when">{phase.when}</span>
              <span className={`who-checklist__phase-count ${phaseDone ? 'who-checklist__phase-count--done' : ''}`}>
                {phaseChecked}/{phase.items.length}
              </span>
            </div>
            <div className="who-checklist__items">
              {phase.items.map(item => {
                const itemState = state[item.id]
                const isChecked = !!itemState?.checked
                return (
                  <label key={item.id} className="who-checklist__item">
                    <input
                      type="checkbox"
                      checked={isChecked}
                      disabled={saving}
                      onChange={() => toggleItem(item.id, !isChecked)}
                    />
                    <span className="who-checklist__item-body">
                      <span className={`who-checklist__item-text ${isChecked ? 'who-checklist__item-text--done' : ''}`}>
                        {item.text}
                      </span>
                      {isChecked && itemState && (
                        <span className="who-checklist__attest">
                          {/* "Attested by Dr Meera Sharma, 09:12 AM". The id only
                              when the server could not name the account. */}
                          Attested by {itemState.by_name || itemState.by}
                          {user && itemState.by === user.id ? ' (you)' : ''}
                          {itemState.at && formatAttestTime(itemState.at) ? `, ${formatAttestTime(itemState.at)}` : ''}
                        </span>
                      )}
                    </span>
                  </label>
                )
              })}
            </div>
          </section>
        )
      })}
    </div>
  )
}
