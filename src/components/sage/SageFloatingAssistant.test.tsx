/**
 * The floating Sage orb: what it shows, and where it gets out of the way.
 *
 * The orb is TEXT-ONLY (Orb is strictly offline): it never opens the
 * microphone and never speaks. Pressing it says voice is not available and
 * offers the Sage panel, to type.
 *
 * jsdom has no layout, so the overlap itself (the orb over Relay's Send button
 * and the round's "Seen, next bed" corner) is asserted as the class that lifts
 * the orb on those tabs; the lift's geometry is in SageFloatingAssistant.css.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const bus = vi.hoisted(() => ({ gotoTab: vi.fn() }))

vi.mock('../../contexts/ActiveViewContext', () => ({
  useActiveView: () => ({ activeTab: '', selection: null }),
}))
vi.mock('../../lib/orbBus', () => ({ gotoTab: bus.gotoTab }))

import SageFloatingAssistant from './SageFloatingAssistant'

const getUserMedia = vi.fn()
const speak = vi.fn()

beforeEach(() => {
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), resume: vi.fn(), speak })
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('pressing the orb', () => {
  it('says voice is not available, in plain words', async () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    await act(async () => { fireEvent.click(screen.getByTitle('Ask Sage')) })
    const card = container.querySelector('.sage-fab__card')!
    expect(card.textContent).toContain('Voice is not available here. Type your question in Sage instead.')
    expect(card.textContent).not.toMatch(/ORB_|posture|egress|cloud/)
  })

  it('never opens the microphone or speaks', async () => {
    render(<SageFloatingAssistant activeTab="vigil" />)
    await act(async () => { fireEvent.click(screen.getByTitle('Ask Sage')) })
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(speak).not.toHaveBeenCalled()
    expect(document.querySelector('.sage-fab--listening')).toBeNull()
  })

  it('offers the Sage panel to type, and closes as it goes there', async () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    await act(async () => { fireEvent.click(screen.getByTitle('Ask Sage')) })
    const open = screen.getByRole('button', { name: 'Open Sage to type' })
    expect(document.activeElement).toBe(open)
    await act(async () => { fireEvent.click(open) })
    expect(bus.gotoTab).toHaveBeenCalledWith('sage')
    expect(container.querySelector('.sage-fab__card')).toBeNull()
  })

  it('closes on a second press, and on Escape back to the orb', async () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    const orb = screen.getByTitle('Ask Sage')
    await act(async () => { fireEvent.click(orb) })
    expect(orb.getAttribute('aria-expanded')).toBe('true')
    await act(async () => { fireEvent.click(orb) })
    expect(container.querySelector('.sage-fab__card')).toBeNull()

    await act(async () => { fireEvent.click(orb) })
    await act(async () => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(container.querySelector('.sage-fab__card')).toBeNull()
    expect(document.activeElement).toBe(orb)
    expect(bus.gotoTab).not.toHaveBeenCalled()
  })

  it('has no hotword toggle', () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    expect(container.querySelector('.sage-fab__toggle')).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)
  })
})

describe('the orb keeps clear of a panel\'s own bottom controls', () => {
  it.each(['relay', 'round', 'sage', 'scribe'])('is lifted on %s', tab => {
    const { container } = render(<SageFloatingAssistant activeTab={tab} />)
    expect(container.querySelector('.sage-fab')!.classList.contains('sage-fab--raised')).toBe(true)
  })

  it('is not lifted on a scrolling panel, which reserves the corner instead', () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    expect(container.querySelector('.sage-fab')!.classList.contains('sage-fab--raised')).toBe(false)
  })

  it.each(['sage', 'scribe'])('steps aside on a phone on %s, which has its own microphone', tab => {
    const { container } = render(<SageFloatingAssistant activeTab={tab} />)
    expect(container.querySelector('.sage-fab')!.classList.contains('sage-fab--own-mic')).toBe(true)
  })

  it.each(['relay', 'round'])('stays on %s on a phone', tab => {
    const { container } = render(<SageFloatingAssistant activeTab={tab} />)
    expect(container.querySelector('.sage-fab')!.classList.contains('sage-fab--own-mic')).toBe(false)
  })
})
