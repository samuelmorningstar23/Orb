import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE, buildHeaders, getUser } from '../api/client'
import { sageStatusText } from './sageStatus'

interface ModelStatus {
  model: string
  role: 'primary' | 'ambient'
  warm: boolean
  /** Unloaded by an administrator: it stays out until they load it again. */
  held?: boolean
  detail?: { size_vram?: number | null } | null
  impacts?: string[]
}

interface ModelsHealth {
  ollama_reachable: boolean
  models: ModelStatus[]
}

function gb(bytes?: number | null): string {
  if (!bytes) return '—'
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`
}

/** Header AI control: shows whether each local inference tier is resident in
 *  memory, and (for admins) lets an operator load or unload a tier.
 *
 *  Warm models answer immediately; a cold one pays a multi-gigabyte load on
 *  first use. Unloading is how you reclaim ~17 GB on a memory-pressured box —
 *  so the confirmation spells out exactly which clinical features stop working
 *  until the model is loaded again. */
export default function AiStatusChip() {
  const [health, setHealth] = useState<ModelsHealth | null>(null)
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<ModelStatus | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)

  const poll = useCallback(async () => {
    try {
      const resp = await fetch(`${API_BASE}/health/models`, {
        credentials: 'include',
        headers: buildHeaders('GET'),
      })
      if (!resp.ok) return
      setHealth((await resp.json()) as ModelsHealth)
    } catch {
      setHealth(null)
    }
  }, [])

  useEffect(() => {
    // `poll` only calls setState AFTER awaiting fetch(), so this cannot cascade
    // renders — but the rule cannot see through the await and flags the call.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    poll()
    const id = setInterval(poll, 60_000)
    return () => clearInterval(id)
  }, [poll])

  // Close on outside click / Escape — a popover anchored in the header must not
  // trap the clinician.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false); setConfirming(null)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setOpen(false); setConfirming(null) }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const act = async (model: string, action: 'load' | 'unload') => {
    setBusy(model); setError(null)
    try {
      const resp = await fetch(`${API_BASE}/health/models/${action}`, {
        method: 'POST',
        credentials: 'include',
        headers: buildHeaders('POST'),
        body: JSON.stringify({ model }),
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        // `detail` is a plain string for our own HTTPExceptions but a structured
        // object for the RBAC layer's 403 — rendering it raw printed
        // "[object Object]" to the clinician.
        const d = body?.detail
        const msg =
          typeof d === 'string' ? d
          : d?.message ? d.message
          : Array.isArray(d) && d[0]?.msg ? d[0].msg
          : `Request failed (${resp.status})`
        throw new Error(msg)
      }
      await poll()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setBusy(null); setConfirming(null)
    }
  }

  if (!health) return null

  const allWarm = health.ollama_reachable && health.models.every(m => m.warm)
  const statusText = sageStatusText(health)
  // Loading/unloading is an admin action (it takes clinical features offline
  // house-wide), so everyone else gets the read-only status view rather than
  // buttons that would only ever return 403.
  const canManage = getUser()?.role === 'admin'

  return (
    <div className="ai-chip-root" ref={rootRef}>
      <button
        className={`ai-status-chip ${allWarm ? 'ai-status-chip--warm' : 'ai-status-chip--cold'}`}
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={canManage ? 'Local AI models: click to configure' : 'Sage status'}
      >
        <span className={`ai-status-chip__model ${allWarm ? 'is-warm' : 'is-cold'}`}>
          <span className="ai-status-chip__dot" aria-hidden="true" />
          {statusText}
        </span>
        <span className="ai-status-chip__caret" aria-hidden="true">▾</span>
      </button>

      {open && !canManage && (
        <div className="ai-config" role="dialog" aria-label="Sage status">
          <div className="ai-config__head">
            <strong>{statusText}</strong>
          </div>
          <div className="ai-config__meta">
            {!health.ollama_reachable
              ? 'Sage cannot answer right now. Tell your administrator.'
              : allWarm
                ? 'Sage is loaded and answers straight away.'
                : statusText === 'Sage limited'
                  ? 'An administrator has unloaded part of Sage to free memory. Some features are reduced until it is loaded again, and an answer from the smaller model says so.'
                  : 'Part of Sage is not loaded yet. The first answer can take longer while it loads.'}
          </div>
          <div className="ai-config__foot">
            Sage runs entirely on this machine. Loading and unloading requires an administrator.
          </div>
        </div>
      )}

      {open && canManage && (
        <div className="ai-config" role="dialog" aria-label="Local AI model configuration">
          <div className="ai-config__head">
            <strong>Local AI models</strong>
            <span className={`ai-config__engine ${health.ollama_reachable ? 'is-up' : 'is-down'}`}>
              {health.ollama_reachable ? 'Engine running' : 'Engine unreachable'}
            </span>
          </div>

          {error && <div className="ai-config__error">{error}</div>}

          {health.models.map(m => (
            <div key={m.model} className="ai-config__row">
              <div className="ai-config__info">
                <div className="ai-config__name">
                  <span className={`ai-status-chip__dot ${m.warm ? 'is-warm-dot' : 'is-cold-dot'}`} aria-hidden="true" />
                  {m.model}
                </div>
                <div className="ai-config__meta">
                  {m.role === 'primary' ? 'Primary: clinical reasoning & vision' : 'Ambient: background classification'}
                  {' · '}
                  {m.warm ? `loaded, ${gb(m.detail?.size_vram)}` : m.held ? 'unloaded by an administrator' : 'not loaded'}
                </div>
              </div>
              {canManage ? (
                m.warm ? (
                  <button
                    className="ai-config__btn ai-config__btn--off"
                    disabled={busy === m.model}
                    onClick={() => setConfirming(m)}
                  >
                    {busy === m.model ? 'Unloading…' : 'Unload'}
                  </button>
                ) : (
                  <button
                    className="ai-config__btn ai-config__btn--on"
                    disabled={busy === m.model || !health.ollama_reachable}
                    onClick={() => act(m.model, 'load')}
                  >
                    {busy === m.model ? 'Loading…' : 'Load'}
                  </button>
                )
              ) : (
                <span className={`ai-config__state ${m.warm ? 'is-warm' : 'is-cold'}`}>
                  {m.warm ? 'Ready' : 'Not loaded'}
                </span>
              )}
            </div>
          ))}

          {confirming && (
            <div className="ai-config__confirm">
              <div className="ai-config__confirm-title">
                Unload {confirming.model}?
              </div>
              <p className="ai-config__confirm-body">
                This frees {gb(confirming.detail?.size_vram)} of memory. While it is
                unloaded:
              </p>
              <ul className="ai-config__impacts">
                {(confirming.impacts || []).map(i => <li key={i}>{i}</li>)}
              </ul>
              <p className="ai-config__confirm-note">
                Orb will not reload it in the background. Nothing is deleted, and
                Load brings it back.
              </p>
              <div className="ai-config__confirm-actions">
                <button className="ai-config__btn" onClick={() => setConfirming(null)}>Cancel</button>
                <button
                  className="ai-config__btn ai-config__btn--danger"
                  onClick={() => act(confirming.model, 'unload')}
                  disabled={busy === confirming.model}
                >
                  {busy === confirming.model ? 'Unloading…' : 'Unload model'}
                </button>
              </div>
            </div>
          )}

          <div className="ai-config__foot">
            {canManage
              ? 'Models run entirely on this machine. Loading and unloading affects only memory use, never patient data.'
              : 'Models run entirely on this machine. Loading and unloading requires an administrator.'}
          </div>
        </div>
      )}
    </div>
  )
}
