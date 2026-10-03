import { useEffect, useState } from 'react'
import { apiFetch } from '../api/client'
import './ProvenanceChip.css'

/**
 * Per-output provenance chip.
 *
 * Renders inline on any AI-generated output (Sage answers, Scribe drafts,
 * Forecast, Lens, the Vigil CfC overlay) to show WHICH model + version produced
 * it and whether that model is validated — Orb's "honesty layer" made visible at
 * the point of use, not buried in an admin console. The full lineage/metrics live
 * in Admin → Model Governance, which this chip's tooltip points to.
 *
 * Provenance is fetched once and cached process-wide (the registry is tiny and
 * effectively static within a session), so dropping chips on many outputs costs
 * a single request.
 */

interface ModelProvenance {
  model_key: string
  version: string
  task: string
  training_data: string
  validated: number
  checkpoint_present: number
  is_active: number
}

let _cache: Record<string, ModelProvenance> | null = null
let _inflight: Promise<Record<string, ModelProvenance>> | null = null

async function loadProvenance(): Promise<Record<string, ModelProvenance>> {
  if (_cache) return _cache
  if (!_inflight) {
    _inflight = apiFetch<{ models: ModelProvenance[] }>('/admin/models/provenance')
      .then(res => {
        _cache = Object.fromEntries(res.models.map(m => [m.model_key, m]))
        return _cache
      })
      .catch(() => {
        // Fail quiet BUT do NOT poison the cache: leaving _cache null means the
        // next chip that mounts retries the fetch, so a transient blip doesn't
        // permanently hide every provenance chip for the whole session.
        return {} as Record<string, ModelProvenance>
      })
      .finally(() => { _inflight = null })
  }
  return _inflight
}

type Status = 'validated' | 'unvalidated' | 'untrained'

function statusOf(m: ModelProvenance): Status {
  if (m.validated) return 'validated'
  if (!m.checkpoint_present) return 'untrained'
  return 'unvalidated'
}

const STATUS_LABEL: Record<Status, string> = {
  validated: 'Validated',
  unvalidated: 'Unvalidated',
  untrained: 'Untrained',
}

export default function ProvenanceChip({
  modelKey,
  compact = false,
}: {
  modelKey: string
  compact?: boolean
}) {
  const [model, setModel] = useState<ModelProvenance | null>(null)

  useEffect(() => {
    let alive = true
    loadProvenance().then(map => { if (alive) setModel(map[modelKey] ?? null) })
    return () => { alive = false }
  }, [modelKey])

  if (!model) return null

  const status = statusOf(model)
  const tip =
    `${model.task}\n` +
    `Model: ${model.model_key} · ${model.version}\n` +
    `Training data: ${model.training_data}\n` +
    `Status: ${STATUS_LABEL[status]}. See Admin → Model Governance`

  return (
    <span
      className={`prov-chip prov-chip--${status} ${compact ? 'prov-chip--compact' : ''}`}
      title={tip}
      role="note"
      aria-label={`AI provenance: ${model.model_key} version ${model.version}, ${STATUS_LABEL[status]}`}
    >
      <span className="prov-chip__dot" aria-hidden="true" />
      <span className="prov-chip__model">AI</span>
      {/* A model with no checkpoint carries the literal version string
          "untrained" (model_governance: forecast_lstm), and the status beside
          it is "Untrained" — so the chip on the Forecast header read
          "AI untrained Untrained". Drop the version when it is only repeating
          the status in lower case; the status is the load-bearing word. */}
      {!compact && model.version.toLowerCase() !== STATUS_LABEL[status].toLowerCase() && (
        <span className="prov-chip__version">{model.version}</span>
      )}
      <span className="prov-chip__status">{STATUS_LABEL[status]}</span>
    </span>
  )
}
