/**
 * Ward Round — the dictate control.
 *
 * Voice is the primary input here, not a convenience: a doctor at a bed is
 * holding a tablet in one hand and cannot type a plan in the time available.
 * So the record button is the biggest target on the screen, it is press-to-
 * start / press-to-stop (not hold-to-talk, which fails with gloves and fails
 * the moment the tablet is put down), and the transcript lands in an editable
 * box because dictation is a draft, never a signature.
 *
 * PHI HANDLING. The accuracy pass is the LOCAL Whisper endpoint
 * (api/client.transcribeAudio), always. The browser's Web Speech API — which
 * streams microphone audio to a cloud recogniser — is used only for the live
 * "hearing…" line, only when the deployment posture permits cloud speech, and
 * its output is never written into the note. If the posture is unknown the
 * runtime reports not-allowed, so it stays off.
 *
 * Dictated text is held in memory only. It is deliberately never mirrored to
 * localStorage: this is free clinical text about a named patient, on a shared
 * ward tablet.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { transcribeAudio } from '../../api/client'
import { useSageRuntime } from '../../contexts/SageRuntimeContext'

/** There is deliberately no 'queued' state.
 *
 *  There used to be: offline, api/client queued the note and this said "held on
 *  this tablet and sent when it reconnects". The first half was true and the
 *  second was not — the queue is deleted wholesale at sign-out, and a ward round
 *  ends by handing the tablet on. A promise Orb cannot keep about a plan the
 *  next shift is relying on is worse than a refusal, so RoundPanel now refuses
 *  the save outright while offline and this state has nothing left to describe. */
export type SaveState = 'idle' | 'saving' | 'saved' | 'error'

interface Props {
  patientId: string
  /** Improves local-Whisper accuracy on names and conditions. Sent to the
   *  on-appliance transcription endpoint, which already holds this patient. */
  hints: string
  draft: string
  onDraftChange: (text: string) => void
  onSave: () => void
  saveState: SaveState
  saveError: string | null
}

function mmss(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60)
  const s = totalSeconds % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

export default function RoundDictation({
  patientId, hints, draft, onDraftChange, onSave, saveState, saveError,
}: Props) {
  const { claimMic, releaseMic, cloudSpeechAllowed, localAsrAvailable } = useSageRuntime()

  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [micError, setMicError] = useState<string | null>(null)
  const [heard, setHeard] = useState('')

  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const recognitionRef = useRef<{ stop: () => void } | null>(null)
  /** Kept so a failed transcription can be retried without asking the doctor to
   *  say it all again — the single most infuriating way to lose a ward round. */
  const [lastBlob, setLastBlob] = useState<Blob | null>(null)
  // The latest draft, for appending a transcript that lands after an await.
  // Updated after each render rather than during it (react-hooks/refs).
  const draftRef = useRef(draft)
  useEffect(() => { draftRef.current = draft }, [draft])

  const teardownCapture = useCallback(() => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
    if (recognitionRef.current) {
      const r = recognitionRef.current
      recognitionRef.current = null
      try { r.stop() } catch { /* already stopped */ }
    }
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    releaseMic('round')
  }, [releaseMic])

  const appendToDraft = useCallback((text: string) => {
    const clean = text.trim()
    if (!clean) return
    const existing = draftRef.current.trim()
    onDraftChange(existing ? `${existing}\n${clean}` : clean)
  }, [onDraftChange])

  const runTranscription = useCallback(async (blob: Blob) => {
    setTranscribing(true)
    setMicError(null)
    try {
      const result = await transcribeAudio(blob, hints, false)
      if (result.transcript?.trim()) {
        appendToDraft(result.transcript)
      } else {
        setMicError('Nothing was transcribed from that recording. Try again, closer to the microphone.')
      }
    } catch (e) {
      setMicError(
        `Transcription failed: ${(e as Error).message}. The recording is still here. Retry, or type the plan.`,
      )
    } finally {
      setTranscribing(false)
    }
  }, [hints, appendToDraft])

  const stopRecording = useCallback(() => {
    const recorder = recorderRef.current
    setRecording(false)
    setHeard('')
    if (recorder && recorder.state !== 'inactive') {
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
        chunksRef.current = []
        if (blob.size < 800) {
          setMicError('That recording was too short to transcribe.')
        } else {
          setLastBlob(blob)
          void runTranscription(blob)
        }
      }
      try { recorder.stop() } catch { /* already stopped */ }
    }
    recorderRef.current = null
    teardownCapture()
  }, [runTranscription, teardownCapture])

  /* EVERY exit from this function must leave a message on screen.
     Before this, three separate paths could end a click with nothing at all:
     `navigator.mediaDevices` absent (no such object to call), the SECOND
     `new MediaRecorder(stream)` throwing (it was outside any try), and
     `recorder.start()` throwing. All three rejected an async callback nobody
     awaited, so "Dictate the plan" produced no UI change, no error and no
     network request — a control that looks broken because it says nothing is
     worse than one that admits what it cannot do. */
  const startRecording = useCallback(async () => {
    setMicError(null)

    if (localAsrAvailable === false) {
      setMicError(
        'Dictation is unavailable on this appliance: local speech recognition is not installed, '
        + 'so nothing spoken could be transcribed here. Type the plan below: it saves to the chart '
        + 'exactly the same way.',
      )
      return
    }

    if (!claimMic('round')) {
      setMicError('The microphone is in use by Sage. Stop it before dictating.')
      return
    }

    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      setMicError(
        'This browser will not give Orb a microphone (it needs a secure context, https or localhost). '
        + 'Type the plan below instead.',
      )
      releaseMic('round')
      return
    }

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      setMicError('Microphone access was denied. Type the plan instead, or allow the microphone and retry.')
      releaseMic('round')
      return
    }
    streamRef.current = stream
    chunksRef.current = []

    let recorder: MediaRecorder
    try {
      try {
        recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
      } catch {
        // Safari and some tablet browsers reject the explicit mimeType.
        recorder = new MediaRecorder(stream)
      }
      recorder.ondataavailable = e => { if (e.data.size > 0) chunksRef.current.push(e.data) }
      recorder.start()
    } catch (e) {
      setMicError(
        `This browser could not start an audio recorder (${(e as Error).message}). `
        + 'Type the plan below instead.',
      )
      stream.getTracks().forEach(t => t.stop())
      streamRef.current = null
      releaseMic('round')
      return
    }
    recorderRef.current = recorder

    // Live "hearing…" line only. Cloud ASR is PHI egress, so it runs only when
    // the server's posture allows it, and its text never enters the note.
    const SR = cloudSpeechAllowed
      ? ((window as unknown as Record<string, unknown>).SpeechRecognition
        ?? (window as unknown as Record<string, unknown>).webkitSpeechRecognition)
      : null
    if (typeof SR === 'function') {
      try {
        const recog = new (SR as new () => any)()
        recog.continuous = true
        recog.interimResults = true
        recog.onresult = (event: any) => {
          let interim = ''
          for (let i = event.resultIndex; i < event.results.length; i++) {
            interim += event.results[i][0].transcript
          }
          setHeard(interim)
        }
        recog.onerror = () => {}
        recog.start()
        recognitionRef.current = recog
      } catch { /* live preview is optional */ }
    }

    setRecording(true)
    setElapsed(0)
    const started = Date.now()
    timerRef.current = setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    )
  }, [claimMic, releaseMic, cloudSpeechAllowed, localAsrAvailable])

  // Moving to the next bed must never leave the microphone open on the previous
  // patient. Unmount and patient change both tear the capture down.
  useEffect(() => {
    return () => {
      const recorder = recorderRef.current
      if (recorder && recorder.state !== 'inactive') {
        recorder.onstop = null
        try { recorder.stop() } catch { /* already stopped */ }
      }
      recorderRef.current = null
      if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
      if (recognitionRef.current) {
        const r = recognitionRef.current
        recognitionRef.current = null
        try { r.stop() } catch { /* already stopped */ }
      }
      streamRef.current?.getTracks().forEach(t => t.stop())
      streamRef.current = null
      releaseMic('round')
      setRecording(false)
      setHeard('')
    }
  }, [patientId, releaseMic])

  const busy = transcribing || saveState === 'saving'

  return (
    <section className="round-dictate" aria-label="Dictate the plan">
      <div className="round-dictate__controls">
        <button
          type="button"
          className={`round-mic${recording ? ' round-mic--live' : ''}`}
          onClick={recording ? stopRecording : startRecording}
          disabled={transcribing}
          aria-pressed={recording}
        >
          <span className="round-mic__icon" aria-hidden="true">
            {recording ? (
              <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            ) : (
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <rect x="9" y="2" width="6" height="12" rx="3" />
                <path d="M5 11a7 7 0 0 0 14 0" />
                <path d="M12 18v4" />
              </svg>
            )}
          </span>
          <span className="round-mic__label">
            {recording ? `Stop · ${mmss(elapsed)}` : transcribing ? 'Transcribing…' : 'Dictate the plan'}
          </span>
        </button>

        <div className="round-dictate__status" aria-live="polite">
          {recording && (
            <span className="round-dictate__rec">
              <span className="round-dictate__rec-dot" aria-hidden="true" />
              Recording
            </span>
          )}
          {transcribing && <span className="round-dictate__note">Transcribing on this appliance…</span>}
          {!recording && !transcribing && draft.trim() === '' && (
            <span className="round-dictate__note">
              {localAsrAvailable === false
                // Say it BEFORE the doctor speaks. The typed path is fully
                // functional and is the one sentence that matters here.
                ? 'Local speech recognition is not installed on this appliance. Type the plan below.'
                : 'Press to dictate, or type below.'}
            </span>
          )}
        </div>
      </div>

      {recording && heard && (
        <p className="round-dictate__heard">
          <span className="round-dictate__heard-label">Hearing</span> {heard}
        </p>
      )}

      {micError && (
        <div className="round-dictate__error" role="alert">
          <span>{micError}</span>
          {lastBlob && !transcribing && (
            <button
              type="button"
              className="round-btn round-btn--quiet round-btn--xs"
              onClick={() => { void runTranscription(lastBlob) }}
            >
              Retry transcription
            </button>
          )}
        </div>
      )}

      <label className="round-dictate__field">
        <span className="round-dictate__field-label">Plan for today</span>
        <textarea
          className="round-dictate__text"
          value={draft}
          onChange={e => onDraftChange(e.target.value)}
          placeholder="Dictated text appears here. Edit it before saving: it is a draft, not a signature."
          rows={3}
          spellCheck
        />
      </label>

      <div className="round-dictate__save">
        <button
          type="button"
          className="round-btn round-btn--primary"
          onClick={onSave}
          disabled={busy || draft.trim() === ''}
        >
          {saveState === 'saving' ? 'Saving…' : 'Save plan to chart'}
        </button>
        {saveState === 'saved' && (
          <span className="round-dictate__saved" role="status">Saved to the chart</span>
        )}
        {saveState === 'error' && (
          <span className="round-dictate__error-inline" role="alert">
            {saveError ?? 'Save failed.'} The text is still here. Try again.
          </span>
        )}
      </div>
    </section>
  )
}
