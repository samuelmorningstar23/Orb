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

const runtime = vi.hoisted(() => ({ localAsrAvailable: false as boolean | null, claimMic: vi.fn(() => true), releaseMic: vi.fn() }))

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
    claimMic: runtime.claimMic,
    releaseMic: runtime.releaseMic,
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
import { getSageHistory, querySage, querySageVoice, transcribeAudio } from '../../api/client'

const getUserMedia = vi.fn()

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  getUserMedia.mockReset()
  vi.mocked(querySage).mockReset()
  vi.mocked(querySageVoice).mockReset()
  vi.mocked(transcribeAudio).mockReset()
  vi.mocked(getSageHistory).mockResolvedValue({ messages: [] })
  vi.stubGlobal('speechSynthesis', { cancel: vi.fn(), resume: vi.fn(), speak: vi.fn(), getVoices: () => [] })
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  vi.restoreAllMocks()
  vi.mocked(getSageHistory).mockResolvedValue({ messages: [] })
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

describe('reopening stored Sage threads', () => {
  it('reopens all loaded turns and continues using the original thread id', async () => {
    vi.mocked(getSageHistory).mockResolvedValueOnce({ messages: [
      { role: 'user', content: 'Demo first question', conversation_id: 'thread-A', timestamp: '2026-10-11 10:00:00' },
      { role: 'assistant', content: 'Demo first answer', conversation_id: 'thread-A', timestamp: '2026-10-11 10:00:01' },
      { role: 'user', content: 'Demo follow-up', conversation_id: 'thread-A', timestamp: '2026-10-11 10:01:00' },
      { role: 'assistant', content: 'Demo second answer', conversation_id: 'thread-A', timestamp: '2026-10-11 10:01:01' },
      { role: 'user', content: 'Other thread', conversation_id: 'thread-B', timestamp: '2026-10-11 10:02:00' },
    ] })
    render(<SagePanel userName="Dr Test" />)
    await act(async () => {})
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Demo first question' })) })
    expect(screen.getByText('Demo second answer')).toBeVisible()
    expect(screen.getAllByText('Demo follow-up')).toHaveLength(1)
    await act(async () => {
      fireEvent.change(screen.getByPlaceholderText('Ask Sage a clinical question...'), { target: { value: 'Continue this thread' } })
    })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Send message' })) })
    expect(vi.mocked(querySage).mock.calls.at(-1)?.at(-1)).toBe('thread-A')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

class DemoRecorder {
  state = 'inactive'
  onstop: (() => void) | null = null
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['synthetic local audio']) })
    void this.onstop?.()
  }
}

describe('Sage asynchronous consumer ownership', () => {
  it.each(['new chat', 'unmount'])('stops late microphone permission after %s', async action => {
    runtime.localAsrAvailable = true
    const mic = deferred<MediaStream>()
    const stopTrack = vi.fn()
    const recorder = vi.fn(function () { return new DemoRecorder() })
    vi.stubGlobal('MediaRecorder', recorder)
    getUserMedia.mockReturnValueOnce(mic.promise)
    const view = render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    await act(async () => { vi.advanceTimersByTime(350) })
    expect(getUserMedia).toHaveBeenCalledOnce()
    await act(async () => {
      if (action === 'unmount') view.unmount()
      else fireEvent.click(screen.getByRole('button', { name: 'New Chat' }))
    })
    await act(async () => { mic.resolve({ getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream) })
    expect(stopTrack).toHaveBeenCalledOnce()
    expect(recorder).not.toHaveBeenCalled()
    expect(querySage).not.toHaveBeenCalled()
    expect(querySageVoice).not.toHaveBeenCalled()
  })

  it.each(['new chat', 'stored thread'])('discards transcription from before %s', async action => {
    runtime.localAsrAvailable = true
    const asr = deferred<{ transcript: string; audio_path: string }>()
    vi.mocked(transcribeAudio).mockReturnValueOnce(asr.promise)
    getUserMedia.mockResolvedValueOnce({ getTracks: () => [{ stop: vi.fn() }] })
    vi.stubGlobal('MediaRecorder', DemoRecorder)
    vi.mocked(getSageHistory).mockResolvedValue({ messages: [
      { role: 'user', content: 'Synthetic other thread', conversation_id: 'other', timestamp: '2026-10-11 10:00:00' },
      { role: 'assistant', content: 'Saved answer', conversation_id: 'other', timestamp: '2026-10-11 10:00:01' },
    ] })
    render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    await act(async () => { vi.advanceTimersByTime(350) })
    await act(async () => { vi.advanceTimersByTime(2000) })
    await act(async () => { fireEvent.click(screen.getByTitle('Stop recording')) })
    expect(transcribeAudio).toHaveBeenCalledOnce()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: action === 'new chat' ? 'New Chat' : 'Synthetic other thread' }))
    })
    await act(async () => { asr.resolve({ transcript: 'Old synthetic dictation', audio_path: 'demo.webm' }) })
    expect(querySage).not.toHaveBeenCalled()
    expect(querySageVoice).not.toHaveBeenCalled()
    expect(screen.queryByText('Old synthetic dictation')).toBeNull()
    if (action === 'stored thread') expect(screen.getByText('Saved answer')).toBeVisible()
  })

  it('revokes the active stream on an authoritative history refusal', async () => {
    const history = deferred<Awaited<ReturnType<typeof getSageHistory>>>()
    const stream = deferred<string>()
    vi.mocked(getSageHistory).mockReturnValueOnce(history.promise)
    vi.mocked(querySage).mockReturnValueOnce(stream.promise)
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<SagePanel userName="Dr Test" />)
    await act(async () => {
      fireEvent.change(screen.getByPlaceholderText('Ask Sage a clinical question...'), { target: { value: 'Synthetic question' } })
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
    })
    const call = vi.mocked(querySage).mock.calls.at(-1)!
    const signal = call[3]!
    await act(async () => { history.reject(Object.assign(new Error('Forbidden'), { status: 403 })) })
    expect(signal.aborted).toBe(true)
    expect(speechSynthesis.cancel).toHaveBeenCalled()
    await act(async () => {
      call[2]!('Late revoked answer')
      stream.resolve('Late revoked answer')
    })
    expect(screen.queryByText('Late revoked answer')).toBeNull()
    expect(screen.queryByText('Synthetic question')).toBeNull()
  })
})

class DemoUtterance {
  voice: SpeechSynthesisVoice | null = null
  rate = 1
  pitch = 1
  onend: (() => void) | null = null
  onerror: (() => void) | null = null
  text: string
  constructor(text: string) { this.text = text }
}

describe('Sage speech callback ownership', () => {
  beforeEach(() => {
    runtime.localAsrAvailable = true
    vi.stubGlobal('SpeechSynthesisUtterance', DemoUtterance)
    vi.spyOn(speechSynthesis, 'getVoices').mockReturnValue([
      { name: 'Synthetic offline voice', lang: 'en-US', localService: true, default: true, voiceURI: 'demo' },
    ])
    vi.mocked(querySageVoice).mockImplementation(() => new Promise(() => {}))
  })

  async function sendQuestion(text: string) {
    const input = screen.getByPlaceholderText('Ask Sage a clinical question...')
    await act(async () => { fireEvent.change(input, { target: { value: text } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    return vi.mocked(querySageVoice).mock.calls.at(-1)![2]!
  }

  it.each(['onend', 'onerror'] as const)('ignores stale utterance %s after another thread starts', async event => {
    render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    const firstToken = await sendQuestion('First synthetic question')
    await act(async () => { firstToken('First answer. ') })
    const oldUtterance = vi.mocked(speechSynthesis.speak).mock.calls[0][0] as unknown as DemoUtterance
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'New Chat' })) })
    const nextToken = await sendQuestion('Second synthetic question')
    await act(async () => { nextToken('Second answer. Queued sentence. ') })
    expect(speechSynthesis.speak).toHaveBeenCalledTimes(2)
    await act(async () => { oldUtterance[event]?.() })
    expect(speechSynthesis.speak).toHaveBeenCalledTimes(2)
    const current = vi.mocked(speechSynthesis.speak).mock.calls[1][0] as unknown as DemoUtterance
    await act(async () => { current.onend?.() })
    expect(speechSynthesis.speak).toHaveBeenCalledTimes(3)
  })

  it('ends voice mode and revokes speech/token consumers after history permission is refused', async () => {
    const history = deferred<Awaited<ReturnType<typeof getSageHistory>>>()
    vi.mocked(getSageHistory).mockReturnValueOnce(history.promise)
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    const token = await sendQuestion('Synthetic question')
    await act(async () => { token('Synthetic spoken answer. ') })
    expect(speechSynthesis.speak).toHaveBeenCalledOnce()
    const signal = vi.mocked(querySageVoice).mock.calls.at(-1)![3]!
    await act(async () => { history.reject(Object.assign(new Error('Forbidden'), { status: 403 })) })
    expect(signal.aborted).toBe(true)
    expect(screen.queryByRole('button', { name: 'Switch to text' })).toBeNull()
    await act(async () => { token('Late revoked answer. ') })
    expect(speechSynthesis.speak).toHaveBeenCalledOnce()
    expect(screen.queryByText(/Late revoked answer/)).toBeNull()
  })

  it('keeps later tokens visible without restarting speech after switching to text', async () => {
    render(<SagePanel userName="Dr Test" />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Voice mode' })) })
    const token = await sendQuestion('Synthetic question')
    await act(async () => { token('Spoken first. ') })
    expect(speechSynthesis.speak).toHaveBeenCalledOnce()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Switch to text' })) })
    await act(async () => { token('Visible second. ') })
    expect(speechSynthesis.speak).toHaveBeenCalledOnce()
    expect(screen.getByText('Spoken first. Visible second.')).toBeVisible()
  })
})

it('stops buffered stream callbacks immediately while retaining the partial answer', async () => {
  const stream = deferred<string>()
  vi.mocked(querySage).mockReturnValueOnce(stream.promise)
  render(<SagePanel userName="Dr Test" />)
  await act(async () => {
    fireEvent.change(screen.getByPlaceholderText('Ask Sage a clinical question...'), { target: { value: 'Synthetic stop question' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
  })
  const call = vi.mocked(querySage).mock.calls.at(-1)!
  await act(async () => { call[2]!('Visible partial answer') })
  await act(async () => { fireEvent.click(screen.getByTitle('Stop generating')) })
  expect(call[3]!.aborted).toBe(true)
  await act(async () => {
    call[2]!('Late buffered content')
    call[6]?.('generating')
    stream.resolve('Late buffered content')
  })
  expect(screen.getByText('Visible partial answer')).toBeVisible()
  expect(screen.queryByText('Late buffered content')).toBeNull()
  expect(screen.queryByTitle('Stop generating')).toBeNull()
})
