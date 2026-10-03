/**
 * The per-answer speed footer.
 *
 * The invariant under test is HONESTY: the footer states only durations that
 * were actually measured. No timing → no footer. A half-measured turn shows
 * the half that exists.
 *
 * Both spellings of "not measured" have to render as nothing. The server now
 * sends nulls (routes/sage_routes.py::_answer_timing), but the zero case is
 * kept and still load-bearing: the voice refusal path shipped literal zeros
 * for a turn the model never ran, and turns persisted under that behaviour are
 * still on disk and still reachable by reloading a conversation.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SageAnswerMeta } from './SageAnswerMeta'

describe('SageAnswerMeta', () => {
  it('renders nothing when there is no timing at all', () => {
    const { container } = render(<SageAnswerMeta />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when timing is null or empty', () => {
    const { container: a } = render(<SageAnswerMeta timing={null} />)
    expect(a).toBeEmptyDOMElement()
    const { container: b } = render(<SageAnswerMeta timing={{}} />)
    expect(b).toBeEmptyDOMElement()
    const { container: c } = render(<SageAnswerMeta timing={{ ttft_ms: null, total_ms: null }} />)
    expect(c).toBeEmptyDOMElement()
  })

  it('treats the voice refusal path\'s literal zeros as unmeasured — never "0.0s"', () => {
    const { container } = render(<SageAnswerMeta timing={{ ttft_ms: 0, total_ms: 0 }} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for the refusal payload the server now sends', () => {
    // Verbatim shape of _answer_timing() with nothing measured — what /voice
    // emits on a refusal, and what a stored untimed turn projects to. Declared
    // as a value, not inline, so the extra `tokens` the wire carries is passed
    // exactly as the server sends it rather than trimmed to fit the interface.
    const refusalPayload = { ttft_ms: null, total_ms: null, tokens: null }
    const { container } = render(<SageAnswerMeta timing={refusalPayload} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when timing is absent even if a tier is present', () => {
    const { container } = render(<SageAnswerMeta tier="careful" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows both durations to one decimal place when both are measured', () => {
    const { container } = render(<SageAnswerMeta timing={{ ttft_ms: 1234, total_ms: 8449 }} />)
    expect(container.textContent).toContain('first words 1.2s')
    expect(container.textContent).toContain('total 8.4s')
    expect(container.textContent).not.toContain('0.0s')
  })

  it('shows only the total when first-token time was not measured (chat lane sends null)', () => {
    const { container } = render(<SageAnswerMeta timing={{ ttft_ms: null, total_ms: 61500 }} />)
    expect(container.textContent).toContain('total 61.5s')
    expect(container.textContent).not.toContain('first words')
  })

  it('shows only first words when the total was not measured', () => {
    const { container } = render(<SageAnswerMeta timing={{ ttft_ms: 900 }} />)
    expect(container.textContent).toContain('first words 0.9s')
    expect(container.textContent).not.toContain('total')
  })

  it('appends the served tier only when the payload carries one', () => {
    const withTier = render(
      <SageAnswerMeta timing={{ ttft_ms: 1200, total_ms: 8400 }} tier="fast" />,
    )
    expect(withTier.container.textContent).toContain('fast')

    const withoutTier = render(<SageAnswerMeta timing={{ ttft_ms: 1200, total_ms: 8400 }} />)
    expect(withoutTier.container.textContent).toBe('first words 1.2s·total 8.4s')
  })

  it('sets the numbers in the data font (mono), not the prose font', () => {
    render(<SageAnswerMeta timing={{ ttft_ms: 1200, total_ms: 8400 }} />)
    expect(screen.getByText('1.2s').className).toBe('sage-answer-meta__num')
    expect(screen.getByText('8.4s').className).toBe('sage-answer-meta__num')
  })
})
