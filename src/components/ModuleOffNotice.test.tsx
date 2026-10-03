/**
 * The switched-off notice, rendered through the real i18n bundles.
 *
 * Its whole job is one honest sentence, so the sentence is what is pinned: the
 * module named, the state said plainly, who can change it, in English and in
 * Hindi, and never a word that claims more than Orb knows (no "switched off"
 * when the server could not be asked).
 */
import { render, screen, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '../i18n'
import ModuleOffNotice, { type ModuleOffSubject } from './ModuleOffNotice'
import type { ModuleOffState } from '../shellConfig'

afterEach(async () => {
  cleanup()
  await i18n.changeLanguage('en')
})

describe('ModuleOffNotice in English', () => {
  it('says Beds is switched off and who can switch it on', () => {
    render(<ModuleOffNotice subject="adt" state="off" />)
    const box = screen.getByRole('status')
    expect(box).toHaveTextContent('Beds is switched off on this site.')
    expect(box).toHaveTextContent('Ask your administrator to switch it on.')
    expect(box.dataset.subject).toBe('adt')
    expect(box.dataset.state).toBe('off')
  })

  it('names each subject plainly', () => {
    const cases: Array<[ModuleOffSubject, string]> = [
      ['registration', 'Patient registration is switched off on this site.'],
      ['adt', 'Beds is switched off on this site.'],
      ['lab', 'Laboratory is switched off on this site.'],
      ['opd', 'OPD token board is switched off on this site.'],
    ]
    for (const [subject, sentence] of cases) {
      const { unmount } = render(<ModuleOffNotice subject={subject} state="off" />)
      expect(screen.getByRole('status')).toHaveTextContent(sentence)
      unmount()
    }
  })

  it('says checking, with no hint, before the server answers', () => {
    render(<ModuleOffNotice subject="lab" state="checking" />)
    const box = screen.getByRole('status')
    expect(box).toHaveTextContent('Checking whether Laboratory is switched on')
    expect(box.querySelectorAll('p')).toHaveLength(1)
  })

  it('never claims "switched off" when the server could not be asked', () => {
    render(<ModuleOffNotice subject="adt" state="unreachable" />)
    const box = screen.getByRole('status')
    expect(box).toHaveTextContent('Orb could not reach the server to check whether Beds is switched on.')
    expect(box).toHaveTextContent('Check the connection, then reload the page.')
    expect(box.textContent).not.toContain('switched off')
  })

  it('says the screen cannot open yet when the feature is on', () => {
    render(<ModuleOffNotice subject="adt" state="unavailable" />)
    const box = screen.getByRole('status')
    expect(box).toHaveTextContent('Beds cannot be opened on this site yet.')
    expect(box.textContent).not.toContain('switched off')
  })

  it('uses plain words: no mock, demo, em dash or raw key on screen', () => {
    const states: ModuleOffState[] = ['checking', 'off', 'unavailable', 'unreachable']
    const subjects: ModuleOffSubject[] = ['registration', 'adt', 'lab', 'opd']
    for (const state of states) {
      for (const subject of subjects) {
        const { unmount } = render(<ModuleOffNotice subject={subject} state={state} />)
        const text = screen.getByRole('status').textContent ?? ''
        expect(text).not.toMatch(/mock|demo|moduleOff\.|\u2014/i)
        unmount()
      }
    }
  })
})

describe('ModuleOffNotice in Hindi', () => {
  it('renders every state from the Hindi bundle, never the English sentence', async () => {
    await i18n.changeLanguage('hi')
    const states: ModuleOffState[] = ['checking', 'off', 'unavailable', 'unreachable']
    for (const state of states) {
      const { unmount } = render(<ModuleOffNotice subject="adt" state={state} />)
      const text = screen.getByRole('status').textContent ?? ''
      expect(text).toContain('बेड')
      expect(text).not.toMatch(/switched|moduleOff\./)
      unmount()
    }
  })
})
