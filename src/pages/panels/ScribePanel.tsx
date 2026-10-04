import { useState, useEffect, useRef, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { transcribeAudio, structureNote, querySage, getScribeNoteVerification, retryScribeNoteVerification, editScribeNoteDraft, signScribeNote, rejectScribeNote, reviseScribeNote, getScribeAsrStatus, getUser, getPatients } from '../../api/client'
import type { Patient, ScribeAsrStatus, ScribePipelineMeta, ScribeVerification } from '../../api/client'
import { useStreamingTranscription } from '../../hooks/useStreamingTranscription'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { useSageRuntime } from '../../contexts/SageRuntimeContext'
import { announceDataChanged } from '../../contexts/PanelActiveContext'
import { useDataChanged } from '../../hooks/useDataChanged'
import OrbLogo from '../../components/OrbLogo'
import { humaniseSignGateReason } from '../../lib/humanise'
import { getQuickAdmitOptions, quickAdmit } from '../../api/scribeAdmission'
import type { QuickAdmitOptions } from '../../api/scribeAdmission'
import { hasPermission, PATIENT_WRITE } from '../../lib/roles'
import { getSpeechRecognitionCtor, type SpeechRecognition, type SpeechRecognitionEvent } from '../../lib/speechRecognition'
import './ScribePanel.css'

/* ═══════════════════════════════════════════════════
   SCRIBE PANEL — Hybrid: Web Speech (live) + Whisper (accuracy)
   ═══════════════════════════════════════════════════ */

export default function ScribePanel() {
  const [recording, setRecording] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [interimText, setInterimText] = useState('')
  const [structuredNote, setStructuredNote] = useState('')
  const [noteFormat, setNoteFormat] = useState<'soap' | 'discharge' | 'handoff'>('soap')
  const [language, setLanguage] = useState<string>('en')  // Whisper transcription language
  const [processing, setProcessing] = useState(false)
  const [status, setStatus] = useState('')
  const [isAdmitting, setIsAdmitting] = useState(false)
  const [structuredNoteWindowOpen, setStructuredNoteWindowOpen] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [refining, setRefining] = useState(false)
  const [patients, setPatients] = useState<Patient[]>([])
  const [selectedPatientId, setSelectedPatientId] = useState('')

  const { claimMic, releaseMic, cloudSpeechAllowed } = useSageRuntime()

  // Honest ASR state: null = probe unresolved (assume nothing, show nothing);
  // {available:false} = the appliance cannot transcribe and the UI must say so
  // plainly instead of failing generically after the clinician has dictated.
  const [asrStatus, setAsrStatus] = useState<ScribeAsrStatus | null>(null)
  const asrUnavailable = asrStatus !== null && !asrStatus.available
  // Admission writes the patient record, which only patient.write holds. A
  // nurse was offered "Admit to Vigil" and got a 403 after filling the form.
  const canAdmit = hasPermission(PATIENT_WRITE)

  // Load admitted patients so a dictated note can be linked to one (Vigil list).
  useEffect(() => {
    getPatients().then(setPatients).catch(() => {})
    getScribeAsrStatus().then(setAsrStatus).catch(() => {})
  }, [])
  // A patient admitted since (here or from the shell) can be linked too.
  useDataChanged(() => { getPatients().then(setPatients).catch(() => {}) })

  const recognitionRef = useRef<SpeechRecognition | null>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const transcriptEndRef = useRef<HTMLDivElement | null>(null)
  const finalTranscriptRef = useRef<string>('')  // accumulates Web Speech final results

  // Live streaming transcription hook
  const streaming = useStreamingTranscription({
    language,
    onFinalTranscript: (text) => {
      setTranscript(prev => prev + (prev ? ' ' : '') + text)
    },
    onError: (err) => setStatus(`⚠️ Streaming error: ${err}`),
    onStatusChange: (s) => {
      if (s === 'processing') setStatus('🧠 Processing utterance…')
      else if (s === 'ready') setStatus('🟢 Listening…')
    },
  })

  useEffect(() => {
    if (transcriptEndRef.current) {
      transcriptEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [transcript, interimText])

  const startRecording = async () => {
    // Claim the shared mic so the floating Sage orb pauses its listening.
    if (!claimMic('scribe')) {
      setStatus('Microphone is in use by Sage. Stop it before dictating.')
      return
    }
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
    } catch {
      setStatus('Microphone access denied.')
      releaseMic('scribe')
      return
    }

    setTranscript(''); setInterimText(''); setStructuredNote(''); setElapsed(0)
    audioChunksRef.current = []
    finalTranscriptRef.current = ''

    // 1) MediaRecorder captures audio for Whisper accuracy pass
    const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
    recorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunksRef.current.push(e.data) }
    recorder.start()
    mediaRecorderRef.current = recorder

    // 2) Web Speech API for instant real-time text. Cloud ASR = PHI egress, so
    // it only runs when the deployment posture permits cloud speech; the
    // local-Whisper accuracy pass (step 1) is always the source of truth.
    const SR = cloudSpeechAllowed ? getSpeechRecognitionCtor() : null
    if (SR) {
      const recog = new SR()
      recog.continuous = true; recog.interimResults = true; recog.lang = 'en-US'
      recog.onresult = (event: SpeechRecognitionEvent) => {
        // Properly accumulate ALL final results across all events
        let allFinal = '', currentInterim = ''
        for (let i = 0; i < event.results.length; i++) {
          const r = event.results[i]
          if (r.isFinal) {
            allFinal += r[0].transcript
          } else {
            currentInterim += r[0].transcript
          }
        }
        // Set the accumulated final text (Web Speech keeps all results in event.results)
        if (allFinal) {
          finalTranscriptRef.current = allFinal
          setTranscript(allFinal)
        }
        setInterimText(currentInterim)
      }
      recog.onerror = () => {}
      recog.onend = () => { if (recognitionRef.current) try { recog.start() } catch { /* already started */ } }
      try { recog.start(); recognitionRef.current = recog } catch { /* live captions are optional: the recording carries on without them */ }
    }

    setRecording(true); setStatus('🔴 Recording, speak now…')
    const start = Date.now()
    timerRef.current = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000)
  }

  const stopRecording = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
    if (recognitionRef.current) { const r = recognitionRef.current; recognitionRef.current = null; try { r.stop() } catch { /* already stopped */ } }
    releaseMic('scribe')
    setInterimText(''); setRecording(false)

    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.onstop = async () => {
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
        if (blob.size < 1000) { setStatus('Recording too short'); return }
        setRefining(true); setStatus('🧠 Refining with medical AI…')
        try {
          const result = await transcribeAudio(blob, undefined, false, language)
          if (result.transcript?.trim()) { setTranscript(result.transcript.trim()); setStatus('✅ Medical transcription complete') }
          else { setStatus('⚠️ Whisper empty, using browser transcript') }
        } catch {
          setStatus(asrUnavailable
            ? '⚠️ Transcription engine unavailable on this appliance, using browser transcript'
            : '⚠️ Refinement unavailable, using browser transcript')
        }
        setRefining(false)
      }
      mediaRecorderRef.current.stop()
    } else { setStatus('Transcription complete') }
    streamRef.current?.getTracks().forEach(t => t.stop())
  }

  useEffect(() => {
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
      if (recognitionRef.current) try { recognitionRef.current.stop() } catch { /* already stopped */ }
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') mediaRecorderRef.current.stop()
      streamRef.current?.getTracks().forEach(t => t.stop())
    }
  }, [])

  const [verificationData, setVerificationData] = useState<ScribeVerification | null>(null)
  const [pipelineData, setPipelineData] = useState<ScribePipelineMeta | null>(null)
  const [showWarnings, setShowWarnings] = useState(false)
  const [currentNoteId, setCurrentNoteId] = useState<number | null>(null)
  const [noteStatus, setNoteStatus] = useState<string>('draft')
  const verifyPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const pollAttemptsRef = useRef(0)

  // Max poll attempts before auto-stopping (~60 seconds at 3s interval)
  const MAX_POLL_ATTEMPTS = 20

  // Stop verification polling
  const stopVerificationPolling = useCallback(() => {
    if (verifyPollRef.current) {
      clearInterval(verifyPollRef.current)
      verifyPollRef.current = null
    }
    pollAttemptsRef.current = 0
  }, [])

  // Start verification polling for a given note
  const startVerificationPolling = useCallback((noteId: number) => {
    stopVerificationPolling()
    pollAttemptsRef.current = 0
    verifyPollRef.current = setInterval(async () => {
      pollAttemptsRef.current += 1

      // Safety ceiling: stop polling after MAX_POLL_ATTEMPTS
      if (pollAttemptsRef.current >= MAX_POLL_ATTEMPTS) {
        setVerificationData({ status: 'failed', error: 'Verification timed out: try again later' })
        stopVerificationPolling()
        return
      }

      try {
        const result = await getScribeNoteVerification(noteId)
        const v = result.verification
        if (v && v.status && v.status !== 'pending') {
          // Verification finished (complete or failed)
          setVerificationData(v)
          if (result.pipeline) setPipelineData(result.pipeline)
          stopVerificationPolling()
        }
      } catch {
        // Network error — keep polling, don't crash
      }
    }, 3000)
  }, [stopVerificationPolling])

  // Handle manual retry of failed verification
  const handleRetryVerification = useCallback(async (noteId: number) => {
    try {
      const result = await retryScribeNoteVerification(noteId)
      if (result.verification) {
        setVerificationData(result.verification)
        if (result.verification.status === 'pending') {
          startVerificationPolling(noteId)
        }
      }
    } catch {
      // Retry request itself failed
    }
  }, [startVerificationPolling])

  // Cleanup polling on unmount
  useEffect(() => {
    return () => stopVerificationPolling()
  }, [stopVerificationPolling])

  const handleStructure = async () => {
    if (!transcript.trim()) return
    setProcessing(true); setStructuredNote(''); setStatus(`Structuring as ${noteFormat.toUpperCase()}…`)
    setVerificationData(null); setPipelineData(null); setShowWarnings(false)
    setCurrentNoteId(null); stopVerificationPolling(); setNoteStatus('draft')
    setStructuredNoteWindowOpen(true);
    try {
      const result = await structureNote(transcript, noteFormat, selectedPatientId || undefined, undefined, true, (partial) => {
        setStructuredNote(partial)
      })
      if (result.id) setCurrentNoteId(result.id)
      if (result.status) setNoteStatus(result.status)
      if (result.verification) {
        setVerificationData(result.verification)
        // If verification is pending, start polling
        if (result.verification.status === 'pending' && result.id) {
          startVerificationPolling(result.id)
        }
      }
      if (result.pipeline) setPipelineData(result.pipeline)
      setStatus('Note structured')
    }
    catch (err) { setStatus(`Structuring failed: ${(err as Error).message || 'Is Ollama running?'}`) }
    finally { setProcessing(false) }
  }

  const handleAdmitClick = () => { setIsAdmitting(true) }
  const extractDiagnosis = (note: string) => { const m = note.match(/ASSESSMENT:?\s*([^]*?)(?:\n\n|\n[A-Z]+:|$)/i); return m ? m[1].trim() : "" }
  const formatTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  return (
    <div className="orb-panel animate-fade-in">
      <div className="scribe-container">
        {!transcript && !structuredNote && !recording && !refining ? (
          <div className="scribe-empty">
            <OrbLogo size={56} />
            <h2 className="scribe-empty__title">Orb Scribe</h2>
            {/* Follows the ASR probe: the real-time promise is only made on an
                appliance that can transcribe. */}
            <p className="scribe-empty__hint" data-testid="scribe-landing-hint">
              {asrStatus?.available
                ? 'Dictate or type clinical notes. Scribe transcribes words in real-time and refines with medical AI for accuracy. Auto-structures into SOAP notes, discharge summaries, and I-PASS handoff documents.'
                : asrUnavailable
                  ? 'Type clinical notes and Scribe structures them into SOAP notes, discharge summaries, and I-PASS handoff documents. Voice dictation is not available on this system.'
                  : 'Dictate or type clinical notes. Scribe structures them into SOAP notes, discharge summaries, and I-PASS handoff documents.'}
            </p>
          </div>
        ) : (
          <div className="scribe-output">
            <div className="scribe-section">
              <h3 className="scribe-section__title">
                Transcript
                {recording && <span style={{ marginLeft: 12, fontSize: '0.8em', color: 'var(--status-danger)', fontWeight: 400 }}>🔴 {formatTime(elapsed)}</span>}
                {refining && <span style={{ marginLeft: 12, fontSize: '0.8em', color: 'var(--accent-primary)', fontWeight: 400 }}>🧠 Refining…</span>}
                {streaming.isStreaming && (
                  <span className="scribe-live-indicator">
                    <span className={`scribe-live-dot ${streaming.isSpeaking ? 'scribe-live-dot--speaking' : streaming.isProcessing ? 'scribe-live-dot--processing' : ''}`} />
                    <span style={{ color: streaming.isSpeaking ? 'var(--status-danger)' : streaming.isProcessing ? 'var(--accent-gold)' : 'var(--status-ok)' }}>
                      {streaming.isSpeaking ? 'Speaking' : streaming.isProcessing ? 'Processing' : 'Listening'}
                    </span>
                    <span className="scribe-audio-level">
                      <span className={`scribe-audio-level__bar ${streaming.isSpeaking ? 'scribe-audio-level__bar--speaking' : ''}`} style={{ width: `${Math.round(streaming.audioLevel * 100)}%` }} />
                    </span>
                    <span style={{ color: 'var(--text-tertiary)', fontSize: '0.85em' }}>{streaming.utteranceCount} utterance{streaming.utteranceCount !== 1 ? 's' : ''}</span>
                  </span>
                )}
              </h3>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <label htmlFor="scribe-language" style={{ fontSize: '0.85em', color: 'var(--text-tertiary)' }}>Language</label>
                <select
                  id="scribe-language"
                  className="scribe-format-select"
                  value={language}
                  disabled={recording || refining}
                  onChange={e => setLanguage(e.target.value)}
                  title="Transcription language (Whisper). Choose Hindi or auto-detect for code-switching."
                >
                  <option value="en">English</option>
                  <option value="hi">Hindi (हिन्दी)</option>
                  <option value="auto">Auto-detect / code-switching</option>
                </select>
              </div>
              <div className="scribe-section__content glass-card" style={{ minHeight: 120, maxHeight: 300, overflowY: 'auto' }}>
                <div className="scribe-section__text" style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>
                  {transcript || (recording ? <span style={{ opacity: 0.4 }}>Listening…</span> : '')}
                  {interimText && <span style={{ opacity: 0.5 }}>{transcript ? ' ' : ''}{interimText}</span>}
                  {(recording || refining) && <span className="scribe-cursor">▍</span>}
                  <div ref={transcriptEndRef} />
                </div>
              </div>
              {transcript && !recording && !refining && (
                <div className="scribe-actions">
                  <select className="scribe-format-select" data-testid="scribe-patient-select" value={selectedPatientId} onChange={e => setSelectedPatientId(e.target.value)} title="Link this note to an admitted patient">
                    <option value="">No patient linked</option>
                    {patients.map(p => (
                      <option key={p.id} value={p.id}>{p.name}{p.bed ? ` · ${p.bed}` : ''}</option>
                    ))}
                  </select>
                  <select className="scribe-format-select" data-testid="scribe-format-select" value={noteFormat} onChange={e => setNoteFormat(e.target.value as 'soap' | 'discharge' | 'handoff')}>
                    <option value="soap">SOAP Note</option>
                    <option value="discharge">Discharge Summary</option>
                    <option value="handoff">I-PASS Handoff</option>
                  </select>
                  <button className="scribe-btn scribe-btn--structure" onClick={handleStructure} disabled={processing}>
                    {processing ? 'Processing...' : '✨ Structure Note'}
                  </button>
                  {canAdmit && structuredNote && !structuredNoteWindowOpen && (
                    <button className="scribe-btn scribe-btn--admit" onClick={handleAdmitClick} style={{ marginLeft: '12px', padding: '8px 20px', background: 'var(--accent-deep)', border: '1px solid var(--accent-gold)', borderRadius: 'var(--radius-md)', color: 'var(--btn-primary-text)', fontWeight: 500 }}>✨ Admit to Vigil</button>
                  )}
                  {structuredNote && !structuredNoteWindowOpen && (
                    <button className="scribe-btn" onClick={() => setStructuredNoteWindowOpen(true)} style={{ marginLeft: '12px', padding: '8px 20px', background: 'transparent', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)', color: 'var(--text-secondary)' }}>View Note</button>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {status && <div className="scribe-status">{status}</div>}

        {asrUnavailable && (
          /* Explicit degraded state, not a generic error: the appliance cannot
             transcribe, typed notes and structuring still work. The server's
             reason and the runbook are for whoever fixes the appliance, so they
             sit behind "Details for IT" instead of in the clinician's sentence.
             role="status" so the state is announced. */
          <div
            role="status"
            data-testid="scribe-asr-unavailable"
            style={{ margin: '0 0 12px', padding: '10px 14px', background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)', borderLeft: '3px solid var(--status-warn)', borderRadius: '4px', fontSize: '0.85rem', color: 'var(--text-secondary)' }}
          >
            {cloudSpeechAllowed ? (
              <>
                <strong style={{ color: 'var(--status-warn-text)' }}>On-device voice transcription is not available on this system.</strong>{' '}
                Dictation uses the browser's speech recognition only, so check the transcript before you structure it. You can still type a note and structure it.
              </>
            ) : (
              <>
                <strong style={{ color: 'var(--status-warn-text)' }}>Voice dictation is not available on this system.</strong>{' '}
                You can still type a note and structure it.
              </>
            )}
            {(asrStatus?.reason || asrStatus?.runbook) && (
              <details data-testid="scribe-asr-it-details" style={{ marginTop: 6 }}>
                <summary style={{ cursor: 'pointer', minHeight: 24, color: 'var(--text-secondary)' }}>Details for IT</summary>
                {asrStatus?.reason && <p style={{ margin: '4px 0 0' }}>{asrStatus.reason}.</p>}
                {asrStatus?.runbook && <p style={{ margin: '4px 0 0' }}>Runbook: {asrStatus.runbook}</p>}
              </details>
            )}
          </div>
        )}

        <div className="scribe-input-area glass">
          <input className="scribe-input" type="text" placeholder="Type clinical notes or use the microphone…"
            value={transcript} onChange={e => { setTranscript(e.target.value); setStructuredNote('') }} id="scribe-input"
            disabled={recording || refining} />
          {/* aria-label, not title alone: these are icon-only buttons with no text
              node, so without it a screen reader announces an unnamed button —
              and when they are disabled the title never surfaces at all (a
              disabled control is out of the tab order, and `title` needs a mouse
              hover that a ward tablet cannot produce). The visible
              role="status" banner above carries the same explanation. */}
          <button className={`scribe-record ${recording ? 'scribe-record--active' : ''}`} id="scribe-record-btn"
            title={asrUnavailable && !cloudSpeechAllowed ? 'Voice dictation is not available on this system' : recording ? 'Stop recording' : 'Start voice dictation'}
            aria-label={asrUnavailable && !cloudSpeechAllowed ? 'Voice dictation is not available on this system' : recording ? 'Stop recording' : 'Start voice dictation'}
            onClick={recording ? stopRecording : startRecording}
            disabled={processing || refining || streaming.isStreaming || (asrUnavailable && !cloudSpeechAllowed)}>

            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {recording ? (
                <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
              ) : (
                <><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></>
              )}
            </svg>
          </button>
          <button
            className={`scribe-stream-btn ${streaming.isStreaming ? 'scribe-stream-btn--active' : ''}`}
            title={asrUnavailable ? 'Voice dictation is not available on this system' : streaming.isStreaming ? 'Stop live streaming' : 'Start live streaming (VAD)'}
            aria-label={asrUnavailable ? 'Live dictation is not available on this system' : streaming.isStreaming ? 'Stop live streaming' : 'Start live streaming (VAD)'}
            onClick={streaming.isStreaming ? streaming.stopStreaming : streaming.startStreaming}
            disabled={processing || refining || recording || asrUnavailable}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {streaming.isStreaming ? (
                <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
              ) : (
                <><path d="M2 10v3a7 7 0 0 0 14 0v-3" /><path d="M9 1v0a3 3 0 0 1 3 3v8a3 3 0 0 1-6 0V4a3 3 0 0 1 3-3z" /><line x1="9" y1="22" x2="9" y2="19" /><path d="M5 22h8" /><circle cx="19" cy="5" r="3" fill="var(--status-ok)" stroke="none" /></>
              )}
            </svg>
          </button>
        </div>
      </div>

      {isAdmitting && (
        <QuickAdmitModal 
          onClose={() => setIsAdmitting(false)} 
          initialDiagnosis={extractDiagnosis(structuredNote)} 
          transcript={transcript}
          structuredNote={structuredNote}
        />
      )}

      {structuredNoteWindowOpen && (
        /* The sign/reject/revise/save handlers below reject rather than swallow:
           a failed clinical action is indistinguishable from a successful one
           unless the modal can surface it, so the modal owns the busy state and
           the inline error banner. */
        <StructuredNoteModal
          onClose={() => {
            if (!processing) {
              setStructuredNoteWindowOpen(false);
              stopVerificationPolling();
            }
          }} 
          structuredNote={structuredNote} 
          processing={processing}
          verificationData={verificationData}
          pipelineData={pipelineData}
          showWarnings={showWarnings}
          setShowWarnings={setShowWarnings}
          noteId={currentNoteId}
          noteStatus={noteStatus}
          onRetryVerification={handleRetryVerification}
          onSign={async (id: number, acknowledgeUnverified?: boolean) => {
            const r = await signScribeNote(id, acknowledgeUnverified)
            if (!r.ok) throw new Error('The server did not confirm the signature. The note is still a draft.')
            setNoteStatus('signed')
          }}
          onReject={async (id: number, reason: string) => {
            const r = await rejectScribeNote(id, reason)
            if (!r.ok) throw new Error('The server did not confirm the rejection. The note is still a draft.')
            setNoteStatus('rejected')
          }}
          onRevise={async (id: number) => {
            const r = await reviseScribeNote(id)
            if (!r.ok) throw new Error('The server did not create a revision.')
            setCurrentNoteId(r.new_note_id)
            setNoteStatus('draft')
          }}
          onSaveEdit={async (id: number, content: string) => {
            await editScribeNoteDraft(id, content)
            setStructuredNote(content)
          }}
          onAdmit={canAdmit ? () => {
            setStructuredNoteWindowOpen(false);
            stopVerificationPolling();
            setIsAdmitting(true);
          } : undefined}
        />
      )}
    </div>
  )
}

const ADMISSION_SOURCES: { value: string; label: string }[] = [
  { value: 'er', label: 'Emergency Room' },
  { value: 'clinic', label: 'Outpatient Clinic' },
  { value: 'transfer', label: 'Hospital Transfer' },
  { value: 'direct', label: 'Direct Admission' },
]

/** Quick admission from a structured note.
 *
 *  Goes through the server's admit_patient transaction (/api/scribe/quick-admit),
 *  the same one as the full admission form, so the patient gets an admission and
 *  is on the department's ward board. It used to post a bare patient row with no
 *  admission (on no board, invisible to every department scope, while this
 *  modal said "admitted successfully") and offered three hard-coded seed doctor
 *  ids that fail at any real site. Doctors now come from the staff table. */
function QuickAdmitModal({ onClose, initialDiagnosis, transcript, structuredNote }: { onClose: () => void, initialDiagnosis: string, transcript: string, structuredNote: string }) {
  const [formData, setFormData] = useState({
    name: '', age: '', sex: '', department: '', ward: '', bed: '', diagnosis: initialDiagnosis,
    attending_doctor_id: '', admission_source: 'er', allergies: '',
  })
  const [options, setOptions] = useState<QuickAdmitOptions | null>(null)
  const [optionsError, setOptionsError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [extracting, setExtracting] = useState(true)
  const [error, setError] = useState('')
  const [credential, setCredential] = useState<{ login: string; password: string } | null>(null)
  const [copied, setCopied] = useState(false)

  // The attending list, the department list and the default department come
  // from the server. The admitting doctor is preselected when they are on the
  // list, and a default department only when it is on the department list.
  useEffect(() => {
    let live = true
    const me = getUser()
    getQuickAdmitOptions()
      .then(opts => {
        if (!live) return
        setOptions(opts)
        const self = opts.doctors.find(d => d.id === me?.id)
        const listed = (d?: string | null) => (d && opts.departments.includes(d) ? d : '')
        setFormData(prev => ({
          ...prev,
          attending_doctor_id: prev.attending_doctor_id || self?.id || '',
          department: prev.department || listed(opts.default_department) || listed(self?.department),
        }))
      })
      .catch(() => { if (live) setOptionsError('Could not load the list of doctors. Close this and try again.') })
    return () => { live = false }
  }, [])

  useEffect(() => {
    const extractDetails = async () => {
      try {
        const prompt = `Extract patient demographics and details from the following clinical documentation into a pure JSON object.
Use the following exact keys for the JSON output:
- "name": string (Patient's full name. If not present, use empty string)
- "age": number (Patient's numerical age. If not present, use 0)
- "sex": string (Exactly "M", "F", or "O". If not present, use empty string)
- "bed": string (Bed number, e.g., "ICU-4". If not present, use empty string)
- "ward": string (Ward name, e.g., "General", "ICU". If not present, use empty string)
- "chief_complaint": string (Short 1-2 sentence description of symptoms or presentation)

If any data point is missing from the text, use an empty string or 0. Output ONLY the JSON block. Do not add markdown or explanations.

Documentation:
${structuredNote || transcript}`;
        
        const responseText = await querySage(prompt);
        let parseText = responseText.replace(/```json/gi, '').replace(/```/g, '').trim();
        // Sometimes it outputs text before the json
        const firstBrace = parseText.indexOf('{');
        const lastBrace = parseText.lastIndexOf('}');
        if (firstBrace >= 0 && lastBrace >= 0) {
          parseText = parseText.substring(firstBrace, lastBrace + 1);
        }
        
        const parsed = JSON.parse(parseText);
        setFormData(prev => ({
          ...prev,
          name: parsed.name || prev.name,
          age: parsed.age ? String(parsed.age) : prev.age,
          // Only a sex the note states. A default here would be a guess
          // written onto the patient record.
          sex: parsed.sex === 'M' || parsed.sex === 'F' || parsed.sex === 'O' ? parsed.sex : prev.sex,
          bed: parsed.bed || prev.bed,
          ward: parsed.ward || prev.ward,
          diagnosis: parsed.chief_complaint || prev.diagnosis
        }));
      } catch (err) {
        console.error('Extraction failed', err);
      } finally {
        setExtracting(false);
      }
    };
    
    extractDetails();
  }, [structuredNote, transcript]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      const res = await quickAdmit({
        name: formData.name,
        age: formData.age === '' ? null : parseInt(formData.age, 10),
        sex: formData.sex,
        department: formData.department,
        ward: formData.ward,
        bed: formData.bed,
        attending_doctor_id: formData.attending_doctor_id,
        admission_source: formData.admission_source,
        chief_complaint: formData.diagnosis,
        allergies: formData.allergies,
      })
      // Surface the patient's one-time Bridge login so it can be handed over.
      // It is never retrievable again, so the hand-off screen stays up until the
      // clinician explicitly dismisses it: no timer, no backdrop dismissal.
      setCredential({ login: res.patient_login_id, password: res.temporary_password })
      // Other open panels (the Orders picker, the round) list the new patient
      // without a page reload, as after an admission from the shell.
      announceDataChanged()
    } catch (err) {
      const message = err instanceof Error ? err.message : ''
      setError(message ? `Not admitted: ${message}` : 'Not admitted. Nothing was saved. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  // One-time credential hand-off screen (shown after a successful admission).
  // Mirrors AdmitPatientModal: copyable fields plus an explicit Done, because a
  // dismissed screen means the password is gone for good.
  if (credential) {
    return (
      <div className="admission-modal-backdrop">
        <div className="admission-modal animate-scale-up" role="dialog" aria-modal="true" aria-labelledby="quick-admit-credential-title" style={{ maxWidth: 460 }}>
          <div className="admission-modal__header">
            <h2 id="quick-admit-credential-title">Patient admitted ✓</h2>
          </div>
          <div style={{ padding: 24 }}>
            <p style={{ margin: '0 0 16px', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              Give the patient their one-time Bridge portal login now. It cannot be shown again.
            </p>
            <div className="glass-card" style={{ padding: 16, display: 'grid', gap: 12 }}>
              <div>
                <div style={{ fontSize: '0.8em', color: 'var(--text-tertiary)' }}>Login ID</div>
                <div style={{ fontFamily: 'monospace', fontSize: '1.1em', userSelect: 'all' }}>{credential.login}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.8em', color: 'var(--text-tertiary)' }}>One-time password</div>
                <div style={{ fontFamily: 'monospace', fontSize: '1.1em', userSelect: 'all' }}>{credential.password}</div>
              </div>
              <button
                type="button"
                className="scribe-btn"
                style={{ padding: '8px 16px', background: 'transparent', border: '1px solid var(--border-subtle)', borderRadius: 'var(--radius-md)', color: 'var(--text-secondary)', cursor: 'pointer' }}
                onClick={() => {
                  navigator.clipboard?.writeText(`Login: ${credential.login}\nPassword: ${credential.password}`)
                  setCopied(true)
                }}
              >
                {copied ? 'Copied ✓' : 'Copy credentials'}
              </button>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
              <button type="button" className="admit-submit-btn" style={{ width: 'auto', padding: '8px 24px' }} onClick={onClose}>Done</button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="admission-modal-backdrop">
      <div className="admission-modal animate-scale-up" role="dialog" aria-modal="true" aria-labelledby="quick-admit-title">
        <div className="admission-modal__header">
          <h2 id="quick-admit-title">✨ Quick Admission</h2>
          {extracting && <span style={{ marginLeft: '12px', fontSize: '0.8rem', color: 'var(--accent-gold)', display: 'flex', alignItems: 'center', gap: 6 }}><div className="sage-live-dot" style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--accent-gold)', animation: 'pulse-live 1s infinite' }} /> Auto-filling from context...</span>}
          <button className="admission-modal__close" onClick={onClose} aria-label="Close">&times;</button>
        </div>
        <form className="admission-modal__form" onSubmit={handleSubmit}>
          <div className="form-group">
            <label htmlFor="qa-name">Patient Name</label>
            <input id="qa-name" required type="text" value={formData.name} onChange={e => setFormData({...formData, name: e.target.value})} placeholder="e.g. John Doe" />
          </div>
          <div className="form-row">
            <div className="form-group">
              <label htmlFor="qa-age">Age</label>
              <input id="qa-age" required type="number" min={0} value={formData.age} onChange={e => setFormData({...formData, age: e.target.value})} placeholder="0" />
            </div>
            <div className="form-group">
              <label htmlFor="qa-sex">Sex</label>
              <select id="qa-sex" required value={formData.sex} onChange={e => setFormData({...formData, sex: e.target.value})}>
                <option value="">Select</option>
                <option value="M">Male</option>
                <option value="F">Female</option>
                <option value="O">Other</option>
              </select>
            </div>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label htmlFor="qa-attending">Attending Doctor</label>
              <select id="qa-attending" required value={formData.attending_doctor_id} onChange={e => setFormData({...formData, attending_doctor_id: e.target.value})} disabled={!options}>
                <option value="">{options ? 'Choose the attending doctor' : 'Loading doctors…'}</option>
                {(options?.doctors ?? []).map(d => (
                  <option key={d.id} value={d.id}>{d.name}{d.department ? ` · ${d.department}` : ''}</option>
                ))}
              </select>
            </div>
            <div className="form-group">
              <label htmlFor="qa-source">Admitted From</label>
              <select id="qa-source" required value={formData.admission_source} onChange={e => setFormData({...formData, admission_source: e.target.value})}>
                {ADMISSION_SOURCES.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </div>
          </div>
          <div className="form-group">
            <label htmlFor="qa-department">Department</label>
            {/* The department decides whose ward board the patient is on, so it is
                chosen from the departments staff cover, never typed: a typed
                "cardiology" put the patient on nobody's board. */}
            <select id="qa-department" required value={formData.department} onChange={e => setFormData({...formData, department: e.target.value})} disabled={!options}>
              <option value="">{!options ? 'Loading departments…' : options.departments.length ? 'Choose the department' : 'No department has active staff yet'}</option>
              {(options?.departments ?? []).map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>
          <div className="form-row">
            <div className="form-group">
              <label htmlFor="qa-ward">Ward</label>
              <input id="qa-ward" required type="text" value={formData.ward} onChange={e => setFormData({...formData, ward: e.target.value})} placeholder="e.g. ICU, W3" />
            </div>
            <div className="form-group">
              <label htmlFor="qa-bed">Bed Number</label>
              <input id="qa-bed" required type="text" value={formData.bed} onChange={e => setFormData({...formData, bed: e.target.value})} placeholder="e.g. ICU-4" />
            </div>
          </div>
          <div className="form-group">
            <label htmlFor="qa-diagnosis">Presenting Complaint or Working Diagnosis</label>
            <textarea id="qa-diagnosis" required rows={3} value={formData.diagnosis} onChange={e => setFormData({...formData, diagnosis: e.target.value})} />
          </div>
          <div className="form-group">
            <label htmlFor="qa-allergies">Known Allergies</label>
            <input id="qa-allergies" type="text" value={formData.allergies} onChange={e => setFormData({...formData, allergies: e.target.value})} placeholder="e.g. penicillin, sulfa" aria-describedby="qa-allergies-hint" />
            <small id="qa-allergies-hint" style={{ color: 'var(--text-secondary)', fontSize: '0.78rem' }}>Separate several with commas. Each is charted as an allergy that the prescribing check screens against.</small>
          </div>
          {optionsError && <div className="form-msg form-msg--error" role="alert">{optionsError}</div>}
          {error && <div className="form-msg form-msg--error" role="alert">{error}</div>}
          <button className="admit-submit-btn" type="submit" disabled={submitting || !options}>
            {submitting ? 'Admitting...' : '✨ Confirm Admission'}
          </button>
        </form>
      </div>
    </div>
  )
}

/** What apiFetch throws for the sign gate's 428 (scribe_routes sign route):
 *  the status, with the structured detail kept whole. */
interface SignGateError {
  status?: number
  detail?: { requires?: string; reasons?: string[]; verification_status?: string }
}

function StructuredNoteModal({ onClose, structuredNote, processing, verificationData, pipelineData, showWarnings, setShowWarnings, noteId, noteStatus, onRetryVerification, onSign, onReject, onRevise, onSaveEdit, onAdmit }: { onClose: () => void, structuredNote: string, processing: boolean, verificationData?: ScribeVerification | null, pipelineData?: ScribePipelineMeta | null, showWarnings: boolean, setShowWarnings: (v: boolean) => void, noteId?: number | null, noteStatus?: string, onRetryVerification?: (noteId: number) => void, onSign?: (noteId: number, acknowledgeUnverified?: boolean) => Promise<void>, onReject?: (noteId: number, reason: string) => Promise<void>, onRevise?: (noteId: number) => Promise<void>, onSaveEdit?: (noteId: number, content: string) => Promise<void>, onAdmit?: () => void }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState(structuredNote);
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  // Sign / reject / revise / save are clinical actions: they hold the modal
  // busy so a second click cannot fire a duplicate request, they report failure
  // inline, and the UI state they own (edit mode, reject input) is only torn
  // down once the request actually resolves.
  const [busyAction, setBusyAction] = useState<'sign' | 'reject' | 'revise' | 'save' | null>(null);
  const [actionError, setActionError] = useState('');
  const busy = busyAction !== null;
  // Non-null while the server's 428 sign gate is demanding an explicit
  // acknowledgment. Holds the gate's reasons so the dialog can show WHY the
  // note is unverified before the clinician decides.
  const [unverifiedAck, setUnverifiedAck] = useState<{ reasons: string[]; status: string } | null>(null);

  const runAction = async (kind: 'sign' | 'reject' | 'revise' | 'save', action: () => Promise<void>) => {
    if (busyAction) return;
    setBusyAction(kind);
    setActionError('');
    try {
      await action();
    } catch (err) {
      setActionError((err as Error)?.message || 'Request failed: nothing was saved. Please try again.');
    } finally {
      setBusyAction(null);
    }
  };

  /* SIGN IS TWO REQUESTS BY DESIGN. The first attempt never carries
     acknowledge_unverified; when the server's fail-closed gate answers 428,
     that is a decision point, not an error — open the acknowledgment dialog
     and only resend WITH the flag after the clinician's typed confirmation.
     Any other failure propagates to runAction's inline error banner. */
  const trySign = async (acknowledged: boolean) => {
    if (!noteId || !onSign) return;
    try {
      await onSign(noteId, acknowledged || undefined);
      setUnverifiedAck(null);
    } catch (err) {
      const gate = err as SignGateError | null | undefined;
      if (gate?.status === 428 && gate?.detail?.requires === 'acknowledge_unverified') {
        setUnverifiedAck({
          reasons: Array.isArray(gate.detail.reasons) ? gate.detail.reasons : [],
          status: String(gate.detail.verification_status ?? ''),
        });
        return;
      }
      throw err;
    }
  };

  /* Re-seed the edit buffer whenever the note behind it changes. Adjusted
     during render rather than in an effect so the textarea never commits a
     frame still holding the previous note's text; same semantics, one fewer
     render. */
  const [syncedNote, setSyncedNote] = useState(structuredNote);
  if (structuredNote !== syncedNote) {
    setSyncedNote(structuredNote);
    setEditContent(structuredNote);
  }

  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [structuredNote]);

  const status = noteStatus || 'draft';
  const isDraft = status === 'draft';
  const isSigned = status === 'signed';
  const isRejected = status === 'rejected';

  // Only doctors/clinicians may sign or reject notes (mirrors the backend
  // scribe.sign / scribe.reject permission gate). Nurses can create/edit drafts
  // but the backend 403s their sign/reject — so hide those buttons rather than
  // surfacing a permission error after the click.
  const currentRole = getUser()?.role;
  const canSignNote = currentRole === 'doctor' || currentRole === 'clinician';

  const verifyStatus = verificationData?.status;
  const isVerifyPending = verifyStatus === 'pending';
  const isVerifyFailed = verifyStatus === 'failed';
  const isVerifyComplete = verifyStatus === 'complete' || (verificationData && !verifyStatus && verificationData.verified !== undefined);
  const hasWarnings = isVerifyComplete && ((verificationData?.warnings?.length ?? 0) > 0 || (verificationData?.omissions?.length ?? 0) > 0);

  const badgeStyle = (bg: string, clr: string, bdr: string): React.CSSProperties => ({ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 12px', borderRadius: '12px', fontSize: '0.8rem', fontWeight: 700, letterSpacing: '0.02em', background: bg, color: clr, border: `1px solid ${bdr}` });

  const renderStatusBadge = () => {
    if (processing) return null;
    if (isDraft) return <div style={badgeStyle('color-mix(in srgb, var(--status-info) 10%, transparent)', 'var(--accent-amber)', 'color-mix(in srgb, var(--status-info) 28%, transparent)')}>📝 Draft</div>;
    if (isSigned) return <div style={badgeStyle('color-mix(in srgb, var(--status-ok) 10%, transparent)', 'var(--status-ok)', 'color-mix(in srgb, var(--status-ok) 28%, transparent)')}>✅ Signed</div>;
    if (isRejected) return <div style={badgeStyle('color-mix(in srgb, var(--status-danger) 10%, transparent)', 'var(--status-danger)', 'color-mix(in srgb, var(--status-danger) 28%, transparent)')}>❌ Rejected</div>;
    if (status === 'superseded') return <div style={badgeStyle('color-mix(in srgb, var(--text-muted) 10%, transparent)', 'var(--text-muted)', 'color-mix(in srgb, var(--text-muted) 28%, transparent)')}>📁 Superseded</div>;
    return null;
  };

  const renderVerificationBadge = () => {
    if (processing) return null;
    if (isVerifyPending) return <div style={badgeStyle('color-mix(in srgb, var(--status-info) 10%, transparent)', 'var(--accent-amber)', 'color-mix(in srgb, var(--status-info) 28%, transparent)')}><span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', border: '2px solid var(--accent-amber)', borderTopColor: 'transparent', animation: 'spin 0.8s linear infinite' }} />Verifying…</div>;
    if (isVerifyFailed) return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <div style={badgeStyle('color-mix(in srgb, var(--status-danger) 10%, transparent)', 'var(--status-danger)', 'color-mix(in srgb, var(--status-danger) 28%, transparent)')} title={verificationData?.error}>⚠ Verification unavailable</div>
        {noteId && onRetryVerification && <button onClick={() => onRetryVerification(noteId)} style={{ padding: '4px 10px', borderRadius: '12px', fontSize: '0.8rem', fontWeight: 600, background: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--status-info-text)', border: '1px solid color-mix(in srgb, var(--status-info) 28%, transparent)', cursor: 'pointer' }}>↻ Retry</button>}
      </div>
    );
    /* This badge is the ONLY control that expands the verification findings —
       which sections the model may have fabricated. As a <div onClick> it was
       mouse-only, so a keyboard user had to sign the note blind or reject a
       good one. A real <button>, disabled when there is nothing to show, both
       reaches the keyboard and states its own state. */
    if (isVerifyComplete) return (
      <button
        type="button"
        disabled={!hasWarnings}
        aria-expanded={hasWarnings ? showWarnings : undefined}
        onClick={() => hasWarnings && setShowWarnings(!showWarnings)}
        title={verificationData?.confidence ? `Confidence: ${(verificationData.confidence * 100).toFixed(0)}%` : undefined}
        style={{
          ...badgeStyle(
            hasWarnings ? 'color-mix(in srgb, var(--status-warn) 10%, transparent)' : 'color-mix(in srgb, var(--status-ok) 10%, transparent)',
            hasWarnings ? 'var(--status-warn-text)' : 'var(--status-ok-text)',
            hasWarnings ? 'color-mix(in srgb, var(--status-warn) 28%, transparent)' : 'color-mix(in srgb, var(--status-ok) 28%, transparent)',
          ),
          cursor: hasWarnings ? 'pointer' : 'default',
          fontFamily: 'inherit',
        }}
      >
        {hasWarnings ? `⚠️ Review suggested${showWarnings ? ' ▴' : ' ▾'}` : '✅ Verified'}
      </button>
    );
    return null;
  };

  const btnBase: React.CSSProperties = { borderRadius: 'var(--radius-md)', cursor: 'pointer', fontWeight: 600, fontSize: '0.85rem' };
  /* Busy swaps to the neutral disabled pair instead of fading to opacity 0.55.
     Opacity dims label and fill together, so Sign & Finalize / Save / Reject /
     Create Revision all rendered around 2:1 while the request was in flight —
     exactly the window in which a clinician needs to read which action is
     pending. This must be a function, not a spreadable object: every call site
     restates its own background/color AFTER the spread, so a busy variant of
     `actionBtn` would be overwritten. Here the neutral pair is applied last. */
  const actionBtn = (extra: React.CSSProperties): React.CSSProperties =>
    busy
      ? {
          ...btnBase,
          ...extra,
          background: 'var(--btn-disabled-bg)',
          color: 'var(--btn-disabled-text)',
          border: '1px solid var(--btn-disabled-border)',
          boxShadow: 'none',
          cursor: 'not-allowed',
        }
      : { ...btnBase, ...extra };

  // Geometry only below. The overlay, surface, border and shadow now come from
  // .admission-modal-backdrop / .admission-modal in ScribePanel.css. The inline
  // set removed here was retired-glass debt: a blur(16px) backdrop, a z-index of
  // 100 that sat under every other overlay in the app, an --accent-glow-strong
  // border and a 50px accent glow shadow — the exact four things DESIGN.md §7
  // lists as design debt rather than style options.
  return (
    <div className="admission-modal-backdrop">
      <div className="admission-modal animate-scale-up" style={{ width: '800px', maxWidth: '95vw', height: '80vh', maxHeight: '900px', display: 'flex', flexDirection: 'column' }}>
        <div className="admission-modal__header" style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <h2 style={{ color: 'var(--accent-gold)', fontSize: '1.4rem', margin: 0 }}>✨ Structured AI Note{processing && <span className="scribe-cursor" style={{ fontSize: '0.8em', color: 'var(--accent-gold)' }}>▍</span>}</h2>
            {renderStatusBadge()}
            {renderVerificationBadge()}
          </div>
          <button className="admission-modal__close" onClick={onClose} disabled={processing || busy} style={{ fontSize: '1.5rem' }}>&times;</button>
        </div>
        {isSigned && !processing && <div style={{ marginBottom: 12, padding: '8px 14px', background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', borderLeft: '3px solid var(--status-ok)', borderRadius: '4px', fontSize: '0.82rem', color: 'var(--status-ok-text)' }}>Signed and finalized: this note is read-only. To make changes, create a revision.</div>}
        {showWarnings && hasWarnings && isVerifyComplete && (
          <div style={{ marginBottom: 16, padding: '12px 16px', background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)', borderLeft: '3px solid var(--status-warn)', borderRadius: '4px' }}>
            <h4 style={{ margin: '0 0 8px 0', color: 'var(--status-warn)', fontSize: '0.9rem' }}>Verification Findings</h4>
            <ul style={{ margin: 0, paddingLeft: 20, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
              {verificationData?.warnings?.map((w, i) => <li key={`w-${i}`} style={{ marginBottom: 4 }}><strong style={{ color: 'var(--text-primary)' }}>[{w.section || 'General'}]</strong> {w.text} <span style={{ opacity: 0.5, fontSize: '0.8em', marginLeft: 6 }}>({w.type})</span></li>)}
              {verificationData?.omissions?.map((o, i) => <li key={`o-${i}`} style={{ marginBottom: 4 }}><strong style={{ color: 'var(--text-primary)' }}>[Omission]</strong> {o.text}</li>)}
            </ul>
          </div>
        )}
        <div className="scribe-section__content glass-card" style={{ flex: 1, overflowY: 'auto', border: '1px solid var(--border-subtle)', background: 'var(--bg-glass)', borderRadius: 'var(--radius-lg)', padding: '24px' }} ref={scrollRef}>
          {editing && isDraft ? (
            <textarea value={editContent} onChange={e => setEditContent(e.target.value)} style={{ width: '100%', height: '100%', background: 'transparent', color: 'var(--text-primary)', border: 'none', outline: 'none', resize: 'none', fontSize: '0.95rem', lineHeight: 1.8, fontFamily: 'inherit' }} />
          ) : (
            <pre className="scribe-section__text" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text-primary)', fontSize: '0.95rem', lineHeight: 1.8 }}>
              {structuredNote}{processing && <span className="scribe-cursor" style={{ opacity: 0.6, marginLeft: 6 }}>▍</span>}{!processing && !structuredNote && <span style={{ opacity: 0.5 }}>Waiting for content...</span>}
            </pre>
          )}
        </div>
        {showRejectInput && isDraft && canSignNote && (
          <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
            <input value={rejectReason} onChange={e => setRejectReason(e.target.value)} disabled={busy} placeholder="Reason for rejection…" style={{ flex: 1, padding: '8px 12px', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--status-danger) 28%, transparent)', background: 'var(--bg-glass)', color: 'var(--text-primary)', fontSize: '0.85rem' }} />
            <button onClick={() => { if (noteId && rejectReason.trim() && onReject) runAction('reject', async () => { await onReject(noteId, rejectReason.trim()); setShowRejectInput(false) }) }} disabled={!rejectReason.trim() || busy} style={actionBtn({ padding: '8px 16px', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger)', border: '1px solid color-mix(in srgb, var(--status-danger) 28%, transparent)' })}>{busyAction === 'reject' ? 'Rejecting…' : 'Confirm'}</button>
            <button onClick={() => setShowRejectInput(false)} disabled={busy} style={actionBtn({ padding: '8px 12px', background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-glass)' })}>Cancel</button>
          </div>
        )}
        {actionError && (
          <div role="alert" style={{ marginTop: 12, padding: '8px 14px', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', borderLeft: '3px solid var(--status-danger)', borderRadius: '4px', fontSize: '0.82rem', color: 'var(--status-danger-text)' }}>
            {actionError}
          </div>
        )}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: showRejectInput || actionError ? 12 : 24 }}>
          <div style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)', opacity: 0.7 }}>
            {/* Only print a stage that actually ran. `ASR {x || '-'}s` rendered
                the literal string "ASR -s" whenever transcription had not run
                (a typed note, or an appliance with no local Whisper) — a broken
                number shown to a clinician, which reads as a broken product
                rather than a stage that did not apply. */}
            {pipelineData && (() => {
              const t = pipelineData.timings || {}
              const parts = [
                t.asr ? `ASR ${t.asr}s` : null,
                t.correction ? `Correct ${t.correction}s` : null,
                t.draft ? `Draft ${t.draft}s` : null,
                t.verify ? `Verify ${t.verify}s` : null,
              ].filter(Boolean)
              return parts.length ? <span>Pipeline: {parts.join(' · ')}</span> : null
            })()}
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {!processing && isDraft && noteId && !editing && <button onClick={() => setEditing(true)} disabled={busy} style={actionBtn({ padding: '8px 16px', background: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--accent-amber)', border: '1px solid color-mix(in srgb, var(--status-info) 28%, transparent)' })}>✏️ Edit</button>}
            {!processing && isDraft && editing && noteId && onSaveEdit && <button onClick={() => runAction('save', async () => { await onSaveEdit(noteId, editContent); setEditing(false) })} disabled={busy} style={actionBtn({ padding: '8px 16px', background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--status-ok)', border: '1px solid color-mix(in srgb, var(--status-ok) 28%, transparent)' })}>{busyAction === 'save' ? 'Saving…' : '💾 Save'}</button>}
            {!processing && isDraft && editing && <button onClick={() => { setEditContent(structuredNote); setEditing(false) }} disabled={busy} style={actionBtn({ padding: '8px 16px', background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-glass)' })}>Cancel</button>}
            {!processing && isDraft && noteId && onSign && !editing && canSignNote && <button onClick={() => runAction('sign', () => trySign(false))} disabled={busy} style={actionBtn({ padding: '8px 20px', background: 'linear-gradient(135deg, color-mix(in srgb, var(--status-ok) 24%, transparent), color-mix(in srgb, var(--status-ok) 14%, transparent))', color: 'var(--status-ok)', border: '1px solid color-mix(in srgb, var(--status-ok) 28%, transparent)', fontWeight: 700, boxShadow: '0 2px 12px color-mix(in srgb, var(--status-ok) 15%, transparent)' })}>{busyAction === 'sign' ? 'Signing…' : '✍ Sign & Finalize'}</button>}
            {!processing && isDraft && noteId && onReject && !editing && !showRejectInput && canSignNote && <button onClick={() => setShowRejectInput(true)} disabled={busy} style={actionBtn({ padding: '8px 16px', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger)', border: '1px solid color-mix(in srgb, var(--status-danger) 28%, transparent)' })}>✕ Reject</button>}
            {!processing && isSigned && noteId && onRevise && <button onClick={() => runAction('revise', () => onRevise(noteId))} disabled={busy} style={actionBtn({ padding: '8px 20px', background: 'color-mix(in srgb, var(--status-info) 10%, transparent)', color: 'var(--accent-amber)', border: '1px solid color-mix(in srgb, var(--status-info) 28%, transparent)' })}>{busyAction === 'revise' ? 'Creating…' : '📋 Create Revision'}</button>}
            <button onClick={onClose} disabled={processing || busy} style={actionBtn({ padding: '8px 20px', background: 'transparent', border: '1px solid var(--border-glass)', color: 'var(--text-secondary)' })}>Close</button>
            {!processing && isDraft && structuredNote && onAdmit && <button onClick={onAdmit} disabled={busy} style={actionBtn({ padding: '8px 24px', background: 'var(--accent-deep)', border: '1px solid var(--accent-gold)', color: 'var(--btn-primary-text)', fontWeight: 600, boxShadow: '0 4px 20px color-mix(in srgb, var(--accent-gold) 20%, transparent)' })}>✨ Admit to Vigil</button>}
          </div>
        </div>
      </div>
      {unverifiedAck && (
        <UnverifiedSignDialog
          reasons={unverifiedAck.reasons}
          verificationStatus={unverifiedAck.status}
          busy={busy}
          signing={busyAction === 'sign'}
          error={actionError}
          onCancel={() => { if (!busy) { setUnverifiedAck(null); setActionError('') } }}
          onConfirm={() => runAction('sign', () => trySign(true))}
        />
      )}
    </div>
  )
}

/**
 * Explicit acknowledgment for signing over a failed / absent hallucination
 * verification — the client half of the server's fail-closed 428 sign gate.
 *
 * Mirrors the eMAR AdministerModal's type-to-confirm: "Sign anyway" stays
 * disabled until the clinician types SIGN, so acknowledging an unverified AI
 * note is a deliberate act, never a reflex click. There is no pre-checked box
 * and no auto-acknowledge path anywhere — cancelling sends nothing and the
 * note stays a draft.
 */
function UnverifiedSignDialog({ reasons, verificationStatus, busy, signing, error, onCancel, onConfirm }: { reasons: string[], verificationStatus: string, busy: boolean, signing: boolean, error: string, onCancel: () => void, onConfirm: () => void }) {
  const [confirmText, setConfirmText] = useState('')
  const close = useCallback(() => { if (!busy) onCancel() }, [busy, onCancel])
  const dialogRef = useFocusTrap<HTMLDivElement>(close)
  const confirmed = confirmText.trim().toLowerCase() === 'sign'

  return createPortal(
    <div className="admission-modal-backdrop" onClick={close}>
      <div
        className="admission-modal animate-scale-up"
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="scribe-unverified-sign-title"
        style={{ maxWidth: 480 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="admission-modal__header">
          <h2 id="scribe-unverified-sign-title" style={{ color: 'var(--status-warn-text)' }}>⚠ Sign without verification?</h2>
          <button className="admission-modal__close" onClick={close} aria-label="Close" disabled={busy}>&times;</button>
        </div>
        <div style={{ padding: '0 4px' }}>
          {/* Says why the gate fired. It used to say "not verified against
              the audio" for every note, including a typed one with no audio,
              on an appliance where checking is simply switched off. The check
              compares the note with the dictated or typed source text. */}
          <p data-testid="scribe-sign-gate-lead" style={{ margin: '0 0 12px', fontSize: '0.9rem', color: 'var(--text-primary)' }}>
            {verificationStatus === 'not_requested'
              ? 'Automated checking is switched off on this appliance. Read the note against what you dictated or typed before signing.'
              : verificationStatus === 'pending'
                ? 'The automated check has not finished for this note. Read the note against what you dictated or typed before signing.'
                : 'This note did not pass the automated check. Read it against what you dictated or typed before signing.'}
          </p>
          {reasons.length > 0 && (
            /* The gate's reasons are server diagnostics ("verified=None"). This
               is a consent dialog: what the clinician is accepting has to be
               readable, so each reason is translated on the way to the screen
               (lib/humanise). The wire values are untouched — the hash-chained
               audit entry for the override still records them verbatim. */
            <ul style={{ margin: '0 0 12px', paddingLeft: 20, fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              {reasons.map((r, i) => (
                <li key={i} style={{ marginBottom: 2 }}>{humaniseSignGateReason(r)}</li>
              ))}
            </ul>
          )}
          <p style={{ margin: '0 0 12px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
            Signing records your name and the time as having accepted the note without automated verification.
          </p>
          <label htmlFor="scribe-unverified-confirm" style={{ display: 'block', marginBottom: 6, fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
            Type <strong style={{ color: 'var(--text-primary)' }}>SIGN</strong> to confirm
          </label>
          <input
            id="scribe-unverified-confirm"
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder='Type "SIGN" to confirm'
            value={confirmText}
            onChange={e => setConfirmText(e.target.value)}
            disabled={busy}
            autoFocus
            style={{ width: '100%', padding: '8px 12px', borderRadius: 'var(--radius-md)', border: '1px solid color-mix(in srgb, var(--status-warn) 40%, transparent)', background: 'var(--bg-glass)', color: 'var(--text-primary)', fontSize: '0.9rem', marginBottom: 12 }}
          />
          {error && (
            <div role="alert" style={{ marginBottom: 12, padding: '8px 14px', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', borderLeft: '3px solid var(--status-danger)', borderRadius: '4px', fontSize: '0.82rem', color: 'var(--status-danger-text)' }}>
              {error}
            </div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, paddingBottom: 8 }}>
            <button
              type="button"
              onClick={close}
              disabled={busy}
              style={{ padding: '8px 16px', borderRadius: 'var(--radius-md)', cursor: busy ? 'not-allowed' : 'pointer', fontWeight: 600, fontSize: '0.85rem', background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-glass)' }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy || !confirmed}
              title={confirmed ? undefined : 'Type SIGN to enable'}
              style={busy || !confirmed
                ? { padding: '8px 20px', borderRadius: 'var(--radius-md)', cursor: 'not-allowed', fontWeight: 700, fontSize: '0.85rem', background: 'var(--btn-disabled-bg)', color: 'var(--btn-disabled-text)', border: '1px solid var(--btn-disabled-border)' }
                : { padding: '8px 20px', borderRadius: 'var(--radius-md)', cursor: 'pointer', fontWeight: 700, fontSize: '0.85rem', background: 'color-mix(in srgb, var(--status-warn) 14%, transparent)', color: 'var(--status-warn-text)', border: '1px solid color-mix(in srgb, var(--status-warn) 40%, transparent)' }}
            >
              {signing ? 'Signing…' : 'Sign anyway'}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}
