import './SageAnswerMeta.css'

/** Wire shape shared by both Sage lanes: the chat stream's `metadata.timing`
 *  ({ retrieval_ms, ttft_ms|null, first_content_ms|null, total_ms }) and the
 *  voice stream's done-event timing ({ ttft_ms, total_ms, tokens }). Only the
 *  two fields this footer renders are declared. */
export interface SageAnswerTiming {
  ttft_ms?: number | null
  total_ms?: number | null
}

interface Props {
  timing?: SageAnswerTiming | null
  /** Served model tier when the payload carries one (`provider.thinking_tier`
   *  from the thinking-mode work). Absent = say nothing. */
  tier?: string | null
  className?: string
}

/** A measured duration is a positive finite number. The voice lane's refusal
 *  path ships literal zeros ({ttft_ms: 0, total_ms: 0}) for a turn the model
 *  never ran — zeros and nulls both mean "not measured", and an unmeasured
 *  answer must render nothing, never "0.0s". */
function measured(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0
}

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)}s`

/** Per-answer speed footer: "first words 1.2s · total 8.4s · careful". */
export function SageAnswerMeta({ timing, tier, className }: Props) {
  const rawTtft = timing?.ttft_ms
  const rawTotal = timing?.total_ms
  const ttft = measured(rawTtft) ? rawTtft : null
  const total = measured(rawTotal) ? rawTotal : null
  if (ttft === null && total === null) return null

  return (
    <div className={`sage-answer-meta${className ? ` ${className}` : ''}`}>
      {ttft !== null && (
        <span>
          first words <span className="sage-answer-meta__num">{seconds(ttft)}</span>
        </span>
      )}
      {ttft !== null && total !== null && <span aria-hidden="true">·</span>}
      {total !== null && (
        <span>
          total <span className="sage-answer-meta__num">{seconds(total)}</span>
        </span>
      )}
      {tier && (
        <>
          <span aria-hidden="true">·</span>
          <span>{tier}</span>
        </>
      )}
    </div>
  )
}
