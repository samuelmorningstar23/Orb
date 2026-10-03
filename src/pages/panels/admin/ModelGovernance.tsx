import { useEffect, useState } from 'react'
import { getModelRegistry } from '../../../api/client'

interface ModelRow {
  model_key: string
  display_name?: string
  version: string
  task: string
  training_data: string
  validated: number
  is_active: number
  checkpoint_present: number
  role?: string | null
  impacts?: string[]
  notes: string
}

/* Status chips on the shared admin chip classes: fill from --status-x, small
   text from --status-x-text. */
function Chip({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return (
    <span className={`admin-stat-chip admin-stat-chip--${ok ? 'ok' : 'warn'}`}>{ok ? yes : no}</span>
  )
}

/**
 * Model Governance — the honesty layer as a product surface. Shows, for every
 * AI/ML model Orb ships, its provenance, whether a trained checkpoint is actually
 * present, and whether it has been externally validated. The rows come from the
 * running configuration, so the fast model, the guideline search and the speech
 * model appear when they are in use.
 */
export default function ModelGovernance() {
  const [models, setModels] = useState<ModelRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    getModelRegistry()
      .then(res => { if (alive) setModels(res.models || []) })
      .catch(e => { if (alive) setError(e?.message || 'Could not load the model register.') })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  if (loading) return <div className="admin-empty">Loading the model register…</div>
  if (error) return <div className="admin-empty" role="alert">{error}</div>
  if (!models.length) return <div className="admin-empty">No models registered.</div>

  return (
    <section className="admin-section model-governance" aria-labelledby="mg-title">
      <h2 id="mg-title" className="admin-section__title">Model governance</h2>
      <p className="admin-td--small" style={{ color: 'var(--text-secondary)', margin: 0 }}>
        Every model Orb runs on this appliance, what it was trained on, whether its
        trained weights are present, and whether it has been validated. Orb never
        presents an untrained or unvalidated model as validated.
      </p>
      <div className="admin-table-wrap">
        <table className="admin-table" aria-label="Models">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Used for</th>
              <th scope="col">Training data</th>
              <th scope="col">Weights</th>
              <th scope="col">Validated</th>
              <th scope="col">In use</th>
            </tr>
          </thead>
          <tbody>
            {models.map(m => (
              <tr key={`${m.model_key}-${m.version}`}>
                <td>
                  <div style={{ fontWeight: 600 }}>{m.display_name || m.task}</div>
                  <div className="admin-td--small" style={{ color: 'var(--text-secondary)' }}>
                    {m.task} · version {m.version}
                  </div>
                </td>
                <td className="admin-td--small">
                  {m.role && <div style={{ fontWeight: 500 }}>{m.role}</div>}
                  {m.impacts && m.impacts.length > 0 && (
                    <ul style={{ margin: 0, paddingLeft: 16 }}>
                      {m.impacts.map(line => <li key={line}>{line}</li>)}
                    </ul>
                  )}
                  {m.notes && <div style={{ color: 'var(--text-secondary)', marginTop: 4 }}>{m.notes}</div>}
                </td>
                <td className="admin-td--small" style={{ maxWidth: 260 }}>{m.training_data}</td>
                <td><Chip ok={!!m.checkpoint_present} yes="Present" no="Not present" /></td>
                <td><Chip ok={!!m.validated} yes="Validated" no="Not validated" /></td>
                <td><Chip ok={!!m.is_active} yes="In use" no="Off" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
