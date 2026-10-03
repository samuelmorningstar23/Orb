/**
 * The floating Sage orb: what it shows, and where it gets out of the way.
 *
 * jsdom has no layout, so the overlap itself (the orb over Relay's Send button
 * and the round's "Seen, next bed" corner) is asserted as the class that lifts
 * the orb on those tabs; the lift's geometry is in SageFloatingAssistant.css.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const runtime = vi.hoisted(() => ({ cloudSpeechAllowed: false }))

vi.mock('../../contexts/SageRuntimeContext', () => ({
  useSageRuntime: () => ({
    cloudSpeechAllowed: runtime.cloudSpeechAllowed,
    micOwner: null,
    claimMic: vi.fn(() => true),
    releaseMic: vi.fn(),
  }),
}))
vi.mock('../../contexts/ActivePatientContext', () => ({
  useActivePatient: () => ({ activePatientId: null, activePatientName: null }),
}))
vi.mock('../../contexts/ActiveViewContext', () => ({
  useActiveView: () => ({ activeTab: '', selection: null }),
  buildWireViewContext: () => ({}),
}))
vi.mock('../../hooks/useSageHotword', () => ({
  useSageHotword: () => ({ isEnabled: false, toggleEnabled: vi.fn() }),
}))
vi.mock('../../api/client', () => ({ querySageVoice: vi.fn() }))

import SageFloatingAssistant from './SageFloatingAssistant'
import { INVESTIGATIONAL_LEAD } from '../InvestigationalBanner'

beforeEach(() => {
  // jsdom has no speech synthesis; the orb cancels speech on unmount.
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), resume: vi.fn(), speak: vi.fn() })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  runtime.cloudSpeechAllowed = false
})

describe('the hotword toggle', () => {
  it('is not rendered when this hospital has not allowed voice', () => {
    runtime.cloudSpeechAllowed = false
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    expect(container.querySelector('.sage-fab__toggle')).toBeNull()
  })

  it('is rendered, labelled by its state, when voice is allowed', () => {
    runtime.cloudSpeechAllowed = true
    render(<SageFloatingAssistant activeTab="vigil" />)
    expect(screen.getByRole('button', { name: 'Enable "Sage" hotword' })).toBeTruthy()
  })
})

describe('pressing the orb with voice off', () => {
  it('says so in plain words, under the investigational label', async () => {
    const { container } = render(<SageFloatingAssistant activeTab="vigil" />)
    await act(async () => { fireEvent.click(screen.getByTitle('Ask Sage')) })
    const card = container.querySelector('.sage-fab__card')!
    expect(card.textContent).toContain('Voice input is off at this hospital')
    expect(card.textContent).not.toMatch(/ORB_|posture|egress/)
    expect(card.querySelector('[role="note"]')?.textContent).toContain(INVESTIGATIONAL_LEAD)
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

  it.each(['relay', 'round'])('stays on %s, where it is the only voice entry', tab => {
    const { container } = render(<SageFloatingAssistant activeTab={tab} />)
    expect(container.querySelector('.sage-fab')!.classList.contains('sage-fab--own-mic')).toBe(false)
  })
})
