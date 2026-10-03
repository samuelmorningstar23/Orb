/**
 * The NEWS2 badge is the only element on either home that is allowed a solid
 * status fill, and its colour band is the clinical signal — which means the
 * band must also exist for someone who cannot see colour, and for someone
 * reading a bedside tablet where there is no such thing as hover.
 *
 * Before tonight the band's meaning lived in a `title` attribute only: invisible
 * on touch, inconsistently announced by screen readers, and completely absent
 * for the null case, where the visible "—" announces as nothing at all.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import News2Badge from './News2Badge'
import { news2Description } from '../lib/news2'
import type { News2 } from '../api/nurse'

const news2 = (over: Partial<News2> = {}): News2 => ({
  score: 0.6,
  level: 'critical',
  raw_news2: 9,
  single_param_3: false,
  drivers: [],
  breakdown: [],
  scoring_model: 'news2-rcp-2017',
  spo2_scale: 1,
  ...over,
})

describe('News2Badge announces what it shows', () => {
  it('carries the band and the score in an aria-label, not only in a title', () => {
    const { container } = render(<News2Badge news2={news2()} labelled={false} />)
    const badge = container.querySelector('.news2')!
    expect(badge.getAttribute('aria-label')).toBe('NEWS2 9 (critical)')
    // The title stays for pointer users, and says the same sentence.
    expect(badge.getAttribute('title')).toBe(badge.getAttribute('aria-label'))
    expect(badge.getAttribute('role')).toBe('img')
  })

  /* "not recorded" and "zero" are different facts, and the visible em-dash
     announces as silence — so an unlabelled badge would leave a reader to infer
     which one they were looking at. */
  it('says so when there is no score, instead of announcing nothing', () => {
    const { container } = render(<News2Badge news2={null} />)
    expect(container.querySelector('.news2')!.getAttribute('aria-label')).toBe('NEWS2 not recorded')
    expect(container.querySelector('.news2__value')!.textContent).toBe('—')
  })

  /* The case most likely to be missed by someone scanning a board for big
     numbers: a patient can sit at an aggregate of 3 and still need urgent
     review. It must be in the sentence, not only in the corner marker. */
  it('includes the single-parameter-3 rule and the SpO₂ scale in the sentence', () => {
    const { container } = render(
      <News2Badge news2={news2({ raw_news2: 3, level: 'stable', single_param_3: true, spo2_scale: 2 })} />,
    )
    expect(container.querySelector('.news2')!.getAttribute('aria-label'))
      .toBe('NEWS2 3 (stable) · a single parameter scores 3 · SpO₂ Scale 2')
  })

  /* F047: the flag was a bare red "3" in the corner, meaning only in the hover
     title, so a dense row on a tablet read "13" with a superscript 3. It must
     say what it is in visible words, in the dense rows too. The label already
     carries the fact for assistive tech, so the pill stays hidden from it. */
  it('shows a single-parameter 3 as visible "Red score" text, never a bare 3', () => {
    for (const labelled of [true, false]) {
      const { container, unmount } = render(
        <News2Badge news2={news2({ raw_news2: 13, single_param_3: true })} labelled={labelled} />,
      )
      const pill = container.querySelector('.news2__red')!
      expect(pill.textContent).toBe('Red score')
      expect(pill.getAttribute('aria-hidden')).toBe('true')
      expect(container.querySelector('.news2__flag')).toBeNull()
      // Nothing in the badge reads as a superscript: the visible text is the
      // score, then the words.
      expect(container.querySelector('.news2')!.textContent).toBe(
        labelled ? 'NEWS213Red score' : '13Red score',
      )
      unmount()
    }
  })

  it('shows no pill when no single parameter scores 3', () => {
    const { container } = render(<News2Badge news2={news2({ single_param_3: false })} />)
    expect(container.querySelector('.news2__red')).toBeNull()
    expect(container.textContent).not.toContain('Red score')
  })

  it('puts the band in a class so the colour comes from ClinicianShell.css', () => {
    for (const level of ['critical', 'elevated', 'stable'] as const) {
      const { container } = render(<News2Badge news2={news2({ level })} />)
      expect(container.querySelector(`.news2--${level}`)).toBeTruthy()
    }
    const { container } = render(<News2Badge news2={null} size="lg" />)
    expect(container.querySelector('.news2--none.news2--lg')).toBeTruthy()
  })

  /* An unmeasured vital scores 0, so a NEWS2 from an incomplete set is a
     minimum. Printed as a plain "5" it reads as the whole score; the nurse
     board, the doctor home and the ward board all render this badge. */
  it('marks a score from an incomplete set as a minimum, on the number and in the label', () => {
    const { container } = render(
      <News2Badge news2={news2({ raw_news2: 5, level: 'elevated', complete: false })} labelled={false} />,
    )
    expect(container.querySelector('.news2__value')!.textContent).toBe('≥5')
    expect(container.querySelector('.news2')!.getAttribute('aria-label'))
      .toBe('NEWS2 5 (elevated) · incomplete set, minimum')
    // `complete` absent (older payloads) or true reads as the full score.
    for (const complete of [undefined, true]) {
      const { container: c, unmount } = render(<News2Badge news2={news2({ raw_news2: 5, complete })} />)
      expect(c.querySelector('.news2__value')!.textContent).toBe('5')
      unmount()
    }
  })

  it('exposes the sentence for callers that need it in their own label', () => {
    expect(news2Description(null)).toBe('NEWS2 not recorded')
    expect(news2Description(news2({ raw_news2: 5, level: 'elevated' }))).toBe('NEWS2 5 (elevated)')
  })
})
