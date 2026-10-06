/**
 * Sage's voice mode on an appliance that cannot transcribe.
 *
 * Orb is strictly offline, so local Whisper is the only speech recognition
 * there is. When the AI posture says it is not installed, pressing the voice
 * button refuses up front, in words, and never opens the microphone. It used
 * to record anyway and end on "No speech detected".
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const runtime = vi.hoisted(() => ({ localAsrAvailable: false as boolean | null }))

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client')
  return {
    ...actual,
    querySage: vi.fn(),
    querySageVoice: vi.fn(),
    getSageHistory: vi.fn(() => Promise.resolve({ messages: [] })),
    transcribeAudio: vi.fn(),
    getPatients: vi.fn(() => Promise.resolve([])),
  }
})
vi.mock('../../contexts/SageRuntimeContext', () => ({
  useSageRuntime: () => ({
    posture: null,
    localAsrAvailable: runtime.localAsrAvailable,
    micOwner: null,
    claimMic: () => true,
    releaseMic: () => {},
  }),
}))
vi.mock('../../contexts/ActivePatientContext', () => ({
  useActivePatient: () => ({ activePatientId: null, activePatientName: null }),
}))
vi.mock('../../contexts/ActiveViewContext', () => ({
  useActiveView: () => ({ activeTab: 'sage', selection: null }),
  buildWireViewContext: () => ({}),
}))
vi.mock('../../hooks/useDataChanged', () => ({ useDataChanged: () => {} }))
vi.mock('../../lib/roles', async () => {
  const actual = await vi.importActual<typeof import('../../lib/roles')>('../../lib/roles')
  return { ...actual, canSeeWardList: () => false }
})

import SagePanel from './SagePanel'

const getUserMedia = vi.fn()

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), resume: vi.fn(), speak: vi.fn(), getVoices: () => [] })
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  runtime.localAsrAvailable = false
})

describe('voice mode without local speech recognition', () => {
  it('says so before anyone speaks, and never opens the microphone', async () => {
    render(<SagePanel userName="Dr Test" />)
    const voice = screen.getByRole('button', { name: 'Voice questions are not available on this appliance' })
    await act(async () => { fireEvent.click(voice) })
    await act(async () => { vi.advanceTimersByTime(1000) })

    expect(screen.getByRole('alert').textContent).toContain(
      'Voice questions are not available on this appliance: local speech recognition is not installed.',
    )
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(screen.queryByText('Voice Mode')).toBeNull()
  })

  it('opens voice mode as before when transcription is installed', async () => {
    runtime.localAsrAvailable = true
    getUserMedia.mockRejectedValue(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    await act(async () => { vi.advanceTimersByTime(1000) })

    expect(screen.getByText('Voice Mode')).toBeTruthy()
    expect(getUserMedia).toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
