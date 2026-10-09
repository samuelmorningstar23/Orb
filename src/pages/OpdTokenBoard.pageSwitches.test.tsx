/**
 * The token board reads the switches the server wrote into the page (9 October
 * 2026): they are the server's answer already, so the board says OPD is off on
 * its first render instead of "checking" while the probe is still out. With no
 * switches on the page it still says it is checking, never "off" on a guess.
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

async function loadBoard(page: unknown) {
  vi.resetModules()
  if (page === undefined) delete window.__ORB_SWITCHES__
  else window.__ORB_SWITCHES__ = page
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
  await import('../i18n')
  return (await import('./OpdTokenBoard')).default
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  delete window.__ORB_SWITCHES__
  localStorage.clear()
})

describe('the token board before the probe answers', () => {
  it('says OPD is switched off at once when the page carried the switches', async () => {
    const Board = await loadBoard({ modules: {}, features: { opd: false }, clinical: {} })
    render(<Board />)
    expect(screen.getByRole('status')).toHaveTextContent('OPD token board is switched off on this site.')
  })

  it('still says it is checking when the page carried none', async () => {
    const Board = await loadBoard(undefined)
    render(<Board />)
    expect(screen.getByRole('status')).not.toHaveTextContent('switched off')
  })
})
