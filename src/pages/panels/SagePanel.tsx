import { Fragment, useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react'
import { querySage, querySageVoice, getSageHistory, transcribeAudio, getPatients, type SageHistoryMessage } from '../../api/client'
import { getThinkingMode } from '../../api/thinkingMode'
import { canSeeWardList } from '../../lib/roles'
import { newId } from '../../lib/id'
import { useActivePatient } from '../../contexts/ActivePatientContext'
import { useActiveView, buildWireViewContext } from '../../contexts/ActiveViewContext'
import { useSageRuntime } from '../../contexts/SageRuntimeContext'
import { useDataChanged } from '../../hooks/useDataChanged'
import { getSpeechRecognitionCtor, type SpeechRecognition } from '../../lib/speechRecognition'
import { onSageAsk, takePendingSageQuestion } from '../../lib/orbBus'
import OrbLogo from '../../components/OrbLogo'
import { SageAnswerMeta } from '../../components/SageAnswerMeta'
import InvestigationalBanner from '../../components/InvestigationalBanner'

import { SageHistorySidebar } from '../../components/sage/SageHistorySidebar'
import { SageEmptyState } from '../../components/sage/SageEmptyState'
import { SageComposer } from '../../components/sage/SageComposer'
import { SageMessage, SageStageStatus, type SageChatMessage } from '../../components/sage/SageMessage'
import '../../components/sage/Sage.css'


/* ═══════════════════════════════════════════════════
   SAGE PANEL — AI Chat with Conversational Voice Mode
   ═══════════════════════════════════════════════════ */

/** A fresh Sage thread id. Opaque to the server, which only ever uses it to
 *  scope the conversation history it prepends to a prompt. */
function newConversationId(): string {
  // newId works over plain http on a LAN too (src/lib/id.ts).
  const rand = newId().replace(/-/g, '')
  return `c-${Date.now().toString(36)}-${rand.slice(0, 12)}`
}

// ─── TTS Utilities ───

/** Strip markdown formatting for clean TTS output */
function stripMarkdown(text: string): string {
  return text
    .replace(/#{1,6}\s+/g, '')           // headers
    .replace(/\*\*([^*]+)\*\*/g, '$1')   // bold
    .replace(/\*([^*]+)\*/g, '$1')       // italic
    .replace(/__([^_]+)__/g, '$1')       // bold alt
    .replace(/_([^_]+)_/g, '$1')         // italic alt
    .replace(/`([^`]+)`/g, '$1')         // inline code
    .replace(/```[\s\S]*?```/g, '')      // code blocks
    .replace(/^\s*[-*+]\s+/gm, '')       // bullet points
    .replace(/^\s*\d+\.\s+/gm, '')       // numbered lists
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1') // links
    .replace(/\|/g, ', ')               // table pipes
    .replace(/---+/g, '')               // horizontal rules
    .replace(/\n\s*\n/g, '\n')          // collapse duplicate newlines
    .trim()
}

/** Clinical spell-correction map for common Whisper ASR misspellings.
 *  Keys are lowercase phonetic misspellings, values are the correct INN/clinical term. */
const CLINICAL_CORRECTIONS: Record<string, string> = {
  // Antibiotics
  'phosphomycin': 'Fosfomycin', 'phosmophycin': 'Fosfomycin', 'fosfhomycin': 'Fosfomycin',
  'phospho mycin': 'Fosfomycin', 'fosfomicin': 'Fosfomycin',
  'meropenim': 'Meropenem', 'meropinem': 'Meropenem', 'meropenm': 'Meropenem',
  'vancomicin': 'Vancomycin', 'vancomysin': 'Vancomycin',
  'sefepime': 'Cefepime', 'cefipime': 'Cefepime',
  'piperacilin': 'Piperacillin', 'tazobactum': 'Tazobactam',
  'amoxicilin': 'Amoxicillin', 'amoxycillin': 'Amoxicillin',
  'azithromicin': 'Azithromycin', 'azithromysin': 'Azithromycin',
  'metronidazol': 'Metronidazole', 'flagyl': 'Metronidazole (Flagyl)',
  'ciprofloxasin': 'Ciprofloxacin', 'levofloxasin': 'Levofloxacin',
  'clindamicin': 'Clindamycin', 'clindamysin': 'Clindamycin',
  'linazolid': 'Linezolid', 'linezolide': 'Linezolid',
  'colistin': 'Colistin', 'polymixin': 'Polymyxin',
  // Cardiovascular
  'lisinipril': 'Lisinopril', 'amlodapine': 'Amlodipine', 'atorvastatin': 'Atorvastatin',
  'clopidogrel': 'Clopidogrel', 'warferin': 'Warfarin',
  'enoxaperin': 'Enoxaparin', 'heperin': 'Heparin',
  // Analgesics / Common
  'paracetamol': 'Paracetamol', 'acetominophen': 'Acetaminophen',
  'ibuprofen': 'Ibuprofen', 'tramadal': 'Tramadol',
  'morphin': 'Morphine', 'fentanil': 'Fentanyl',
  // Diabetes
  'metformine': 'Metformin', 'glimipiride': 'Glimepiride',
  'insuline': 'Insulin',
  // Critical Care
  'noradrenaline': 'Noradrenaline', 'norepinephrin': 'Norepinephrine',
  'dobutamin': 'Dobutamine', 'dopamin': 'Dopamine',
  'midazolam': 'Midazolam', 'propafol': 'Propofol',
  'dexmedetomadine': 'Dexmedetomidine',
  // GI
  'pantoprazol': 'Pantoprazole', 'omeprazol': 'Omeprazole',
  'ondansetran': 'Ondansetron',
  // Steroids
  'dexamethasone': 'Dexamethasone', 'prednisolon': 'Prednisolone',
  'hydrocortison': 'Hydrocortisone',
  'methylprednisolon': 'Methylprednisolone',
}

/** Apply clinical spelling corrections to a Whisper transcript */
function correctClinicalSpelling(text: string): string {
  let result = text
  for (const [wrong, right] of Object.entries(CLINICAL_CORRECTIONS)) {
    // Case-insensitive word boundary replacement
    const regex = new RegExp(`\\b${wrong}\\b`, 'gi')
    result = result.replace(regex, right)
  }
  return result
}

function extractSentences(text: string): [string[], string] {
  // Match sentences ending with . ? ! : or \n followed by space or end
  // Colons and newlines are natural structural pauses in markdown
  const sentenceRegex = /[^.!?:\n]*[.!?:\n]+(?:\s|$)/g
  const sentences: string[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = sentenceRegex.exec(text)) !== null) {
    const sentence = match[0].trim()
    if (sentence.length > 2) sentences.push(sentence) // skip tiny fragments
    lastIndex = sentenceRegex.lastIndex
  }

  const remainder = text.slice(lastIndex)
  return [sentences, remainder]
}

/** Pick the best available voice — prefer macOS neural/enhanced voices */
function getBestVoice(): SpeechSynthesisVoice | null {
  const voices = speechSynthesis.getVoices()
  if (voices.length === 0) return null

  // Prefer high-quality English voices (macOS enhanced/premium)
  const preferred = [
    'Samantha', 'Karen', 'Daniel', 'Zoe', 'Alex',
    'Moira', 'Tessa', 'Rishi', 'Fiona', 'Victoria'
  ]

  for (const name of preferred) {
    const v = voices.find(v => v.name.includes(name) && v.lang.startsWith('en'))
    if (v) return v
  }

  // Fallback: any English voice
  return voices.find(v => v.lang.startsWith('en')) || voices[0]
}


interface SagePanelProps {
  userName?: string
}

export default function SagePanel({ userName }: SagePanelProps) {
  // (The greeting lives in SageEmptyState; the name it uses in sage/greetingName.ts.)
  const [messages, setMessages] = useState<SageChatMessage[]>([])
  const [query, setQuery] = useState('')
  const [streaming, setStreaming] = useState(false)
  // Which long phase the current query is in: 'retrieving' (guideline search)
  // or 'generating' (local model). Drives the status line under the dots.
  const [streamStage, setStreamStage] = useState<'retrieving' | 'generating' | null>(null)
  // When the current turn was sent, for the elapsed seconds on the status line.
  const [turnStartedAt, setTurnStartedAt] = useState(0)

  // Shared Sage runtime: active patient + on-screen view (so panel chat is as
  // context-aware as the floating orb) and mic ownership (so panel voice mode
  // doesn't fight the orb or Scribe for the device).
  const { activePatientId, activePatientName } = useActivePatient()
  const { activeTab: viewTab, selection } = useActiveView()
  const { claimMic, releaseMic, cloudSpeechAllowed } = useSageRuntime()
  const viewRef = useRef<{ pid: string | null; pname: string | null; tab: string; sel: typeof selection }>({ pid: null, pname: null, tab: '', sel: null })
  // Synced after each render rather than written during it (react-hooks/refs).
  useEffect(() => {
    viewRef.current = { pid: activePatientId, pname: activePatientName, tab: viewTab, sel: selection }
  }, [activePatientId, activePatientName, viewTab, selection])
  
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const messagesBoxRef = useRef<HTMLDivElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  /** The latest startVoiceRecording, for speakNext's auto-listen (set below). */
  const startVoiceRecordingRef = useRef<() => void>(() => {})

  // FOLLOW-THE-STREAM. Before this, Sage scrolled to the bottom exactly twice —
  // once when a response began and once when history loaded — and never again.
  // The answer then streamed in BELOW the fold: at ~16 tok/s a full answer takes
  // 30-50 s, so the clinician watched a static screen while the text wrote itself
  // off-viewport. Relay has the opposite bug (RelayPanel.tsx:64 sets scrollTop
  // unconditionally on every push, yanking you back down mid-read).
  //
  // The rule both surfaces should follow: keep the newest text visible ONLY while
  // the reader is already at the bottom. The moment they scroll up — to re-read a
  // dose, or to check a citation while the answer is still coming — following
  // stops and does not resume until they return to the bottom themselves.
  const stickToBottomRef = useRef(true)

  const isNearBottom = () => {
    const el = messagesBoxRef.current
    if (!el) return true
    // 48px of slack: a reader sitting a line or two off the end still counts as
    // "at the bottom", so ordinary sub-pixel scroll drift does not detach them.
    return el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }

  const handleMessagesScroll = () => { stickToBottomRef.current = isNearBottom() }

  // Runs on every token: `messages` is replaced on each chunk, so this fires as
  // the answer grows. useLayoutEffect (not useEffect) so the scroll lands in the
  // same frame as the paint and the text does not visibly jump.
  useLayoutEffect(() => {
    if (!stickToBottomRef.current) return
    const el = messagesBoxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, streaming])
  
  // History state — the sidebar's "Recent Conversations" list is the only view
  // onto stored conversations, so it is refreshed from the server rather than
  // inferred from local message state.
  const [historyList, setHistoryList] = useState<SageHistoryMessage[]>([])

  /* THREAD identity. The server prepends the last six turns of THIS id to the
     prompt and nothing else. Before it existed the server keyed prompt history
     on the user, so clearing `messages` here changed what the clinician saw and
     nothing about what the model read: a general question typed on a fresh
     screen came back answered about the patient from a conversation the
     clinician had already dismissed. Minted once per mounted panel and re-minted
     by "New Chat", so a new thread is a new thread on both sides. */
  const conversationIdRef = useRef<string>(newConversationId())

  // ─── Voice Chat Mode ───
  const [voiceMode, setVoiceMode] = useState(false)
  const [voiceRecording, setVoiceRecording] = useState(false)
  const [voiceTranscribing, setVoiceTranscribing] = useState(false)
  const [voiceElapsed, setVoiceElapsed] = useState(0)
  const [voiceLiveText, setVoiceLiveText] = useState('')
  const [voiceStatus, setVoiceStatus] = useState('')  // step-by-step status
  const [isSpeaking, setIsSpeaking] = useState(false)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioChunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const voiceTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const recognitionRef = useRef<SpeechRecognition | null>(null)
  const liveTextRef = useRef<string>('')
  const recordingStartRef = useRef<number>(0)
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [patientNamesHint, setPatientNamesHint] = useState<string>('Orb Hospital')

  // ─── TTS State ───
  const ttsQueueRef = useRef<string[]>([])          // sentences waiting to be spoken
  const ttsSpeakingRef = useRef(false)               // is TTS currently speaking an utterance
  const ttsBufferRef = useRef('')                    // accumulated unspoken text from streaming
  const spokenSentencesRef = useRef<Set<string>>(new Set()) // track what we've already queued
  const streamingDoneRef = useRef(false)             // has streaming finished?
  const voiceModeRef = useRef(false)                 // track voiceMode without re-render dependency
  const autoListenTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Keep voiceModeRef in sync
  useEffect(() => { voiceModeRef.current = voiceMode }, [voiceMode])

  // Pre-fetch active patient names to bias Whisper's transcription engine.
  // ROLE-GATED: /api/vigil/patients requires vigil.view, which a pharmacist
  // does not hold. Calling it unconditionally threw two uncaught 403s into the
  // console every time PHA001 opened Sage, the one panel a pharmacist can
  // reach. The hint is a nicety; the absence of it costs a pharmacist nothing.
  const loadPatientNamesHint = useCallback(() => {
    if (canSeeWardList()) {
      getPatients().then(patients => {
        if (patients && patients.length > 0) {
          const names = patients.map(p => p.name).join(', ')
          setPatientNamesHint(`Orb Hospital. Current patients: ${names}`)
        }
      }).catch(err => {
        // Never surface this: a failed transcription hint must not look like a
        // broken panel. warn, not log: the console is not a place for chatter.
        console.warn('Sage: patient-name hint unavailable', err instanceof Error ? err.message : err)
      })
    }
  }, [])

  // Load voices (some browsers load them async)
  useEffect(() => {
    speechSynthesis.getVoices()
    speechSynthesis.onvoiceschanged = () => speechSynthesis.getVoices()
    loadPatientNamesHint()
  }, [loadPatientNamesHint])
  // A newly admitted patient's name is in the hint too, without a page reload.
  useDataChanged(loadPatientNamesHint)

  const refreshHistory = useCallback(async () => {
    try {
      const res = await getSageHistory()
      // Stored messages are put into `messages` VERBATIM by loadConversation,
      // and the speed footer below reads `metadata.timing` off them. That is
      // the whole of how a reloaded conversation keeps its timing, so anything
      // here that re-maps a row to {role, content} silently deletes the footer
      // for every past turn — which is how it went missing in the first place.
      if (res && Array.isArray(res.messages)) setHistoryList(res.messages)
    } catch (err) {
      // Keep the last known list rather than blanking the sidebar on a blip.
      console.error(err)
    }
  }, [])

  // Load stored conversations on mount so "Recent Conversations" reflects the
  // server, and refresh whenever a turn is saved or a new chat is started.
  useEffect(() => { void refreshHistory() }, [refreshHistory])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (voiceTimerRef.current) clearInterval(voiceTimerRef.current)
      if (autoListenTimerRef.current) clearTimeout(autoListenTimerRef.current)
      if (recognitionRef.current) try { recognitionRef.current.stop() } catch { /* already stopped */ }
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') mediaRecorderRef.current.stop()
      streamRef.current?.getTracks().forEach(t => t.stop())
      speechSynthesis.cancel()
    }
  }, [])

  // ─── TTS Engine ───
  // A named function so it can call itself: recursing through the `speakNext`
  // const reads it before its declaration (react-hooks/immutability).
  const speakNext = useCallback(function speakNextSentence() {
    if (ttsSpeakingRef.current) return // already speaking
    
    const nextSentence = ttsQueueRef.current.shift()
    if (!nextSentence) {
      // Queue empty — check if streaming is done
      if (streamingDoneRef.current) {
        // Speak any remaining buffer
        const remainder = stripMarkdown(ttsBufferRef.current).trim()
        if (remainder.length > 2) {
          ttsBufferRef.current = ''
          ttsQueueRef.current.push(remainder)
          speakNextSentence()
          return
        }
        // All done speaking — auto-listen if in voice mode
        setIsSpeaking(false)
        if (voiceModeRef.current) {
          autoListenTimerRef.current = setTimeout(() => {
            if (voiceModeRef.current) {
              startVoiceRecordingRef.current()
            }
          }, 800) // brief pause before auto-listen
        }
      }
      return
    }

    ttsSpeakingRef.current = true
    setIsSpeaking(true)

    const utterance = new SpeechSynthesisUtterance(nextSentence)
    const voice = getBestVoice()
    if (voice) utterance.voice = voice
    utterance.rate = 1.05
    utterance.pitch = 1.0

    utterance.onend = () => {
      ttsSpeakingRef.current = false
      speakNextSentence() // process next in queue
    }

    utterance.onerror = () => {
      ttsSpeakingRef.current = false
      speakNextSentence()
    }

    speechSynthesis.speak(utterance)
  }, []) // startVoiceRecording is defined below: reached through startVoiceRecordingRef

  /** Feed new streamed text into TTS pipeline */
  const feedTTS = useCallback((fullResponse: string) => {
    const cleaned = stripMarkdown(fullResponse)
    const [sentences, remainder] = extractSentences(cleaned)
    ttsBufferRef.current = remainder

    for (const s of sentences) {
      if (!spokenSentencesRef.current.has(s)) {
        spokenSentencesRef.current.add(s)
        ttsQueueRef.current.push(s)
      }
    }

    // Kick off speaking if not already
    if (!ttsSpeakingRef.current && ttsQueueRef.current.length > 0) {
      speakNext()
    }
  }, [speakNext])

  const cancelTTS = useCallback(() => {
    speechSynthesis.cancel()
    ttsQueueRef.current = []
    ttsBufferRef.current = ''
    spokenSentencesRef.current.clear()
    ttsSpeakingRef.current = false
    streamingDoneRef.current = false
    setIsSpeaking(false)
    if (autoListenTimerRef.current) {
      clearTimeout(autoListenTimerRef.current)
      autoListenTimerRef.current = null
    }
  }, [])

  // ─── Submit Query ───
  const submitQuery = useCallback(async (userQuery: string) => {
    if (!userQuery.trim()) return

    // If already streaming, abort current response (keep partial answer) and start new query
    if (streaming && abortControllerRef.current) {
      abortControllerRef.current.abort()
      cancelTTS()
      setStreaming(false)
      abortControllerRef.current = null
      // Brief delay to let abort settle
      await new Promise(r => setTimeout(r, 50))
    }

    setQuery('')
    // Reset textarea height
    const textarea = document.getElementById('sage-query-input') as HTMLTextAreaElement
    if (textarea) textarea.style.height = 'auto'
    // Clean up any orphaned empty assistant messages before adding new ones
    setMessages(prev => {
      const cleaned = prev.filter((m, i) => !(m.role === 'assistant' && !m.content.trim() && i < prev.length))
      return [...cleaned, { role: 'user', content: userQuery }]
    })
    setStreaming(true)
    setTurnStartedAt(Date.now())
    setMessages(prev => [...prev, { role: 'assistant', content: '' }])

    // Reset TTS state for new response
    cancelTTS()
    streamingDoneRef.current = false

    // Sending re-pins: the clinician just asked, so they want to watch the answer
    // even if they had scrolled up while reading the previous one.
    stickToBottomRef.current = true
    setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, 50)

    abortControllerRef.current = new AbortController()

    const { pid, pname, tab, sel } = viewRef.current
    const wireView = buildWireViewContext(tab, pid, pname, sel)

    try {
      if (voiceModeRef.current) {
        let accumulatedText = ''
        const { timing } = await querySageVoice(userQuery, pid || undefined, (token) => {
          accumulatedText += token
          setMessages(prev => {
            const updated = [...prev]
            updated[updated.length - 1] = { role: 'assistant', content: accumulatedText }
            return updated
          })
          feedTTS(accumulatedText)
        }, abortControllerRef.current.signal, undefined, wireView)
        // The voice lane reports timing as the stream's return value, not as a
        // metadata event — attach it to the finished answer so the speed footer
        // renders (and persists) for voice turns exactly like chat turns.
        if (timing) {
          setMessages(prev => {
            const updated = [...prev]
            const last = updated[updated.length - 1]
            if (last?.role === 'assistant') {
              updated[updated.length - 1] = { ...last, metadata: { ...(last.metadata || {}), timing } }
            }
            return updated
          })
        }
      } else {
        await querySage(userQuery, pid || undefined, (partial) => {
          setMessages(prev => {
            const updated = [...prev]
            updated[updated.length - 1] = { role: 'assistant', content: partial }
            return updated
          })
          // Feed streamed text to TTS if in voice mode
          if (voiceModeRef.current) {
            feedTTS(partial)
          }
        }, abortControllerRef.current.signal, (incomingMeta) => {
          // Merge metadata + structured_response onto the last assistant message
          setMessages(prev => {
            const updated = [...prev]
            const last = updated[updated.length - 1]
            if (last?.role === 'assistant') {
              const existing = last.metadata || {}
              if (incomingMeta._structured_response) {
                // This is the structured_response event — merge it in
                updated[updated.length - 1] = { ...last, metadata: { ...existing, structured: incomingMeta._structured_response } }
              } else {
                // This is the primary metadata event
                updated[updated.length - 1] = { ...last, metadata: { ...existing, ...incomingMeta } }
              }
            }
            return updated
          })
        }, wireView, (stage) => {
          // Honest progress during the long local-model path.
          setStreamStage(stage)
          // THINKING MODE — read at SEND time, so the question uses the toggle
          // as it stands when Enter is pressed, not as it was when this panel
          // last rendered. This is the only caller of querySage that passes it.
        }, getThinkingMode(), conversationIdRef.current)
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setMessages(prev => {
          const updated = [...prev]
          const last = updated[updated.length - 1]
          if (last?.role === 'assistant') {
            if (!last.content.trim()) {
              return updated.slice(0, -2)
            }
            updated[updated.length - 1] = { ...last, metadata: { ...(last.metadata || {}), status: 'stopped' } }
          }
          return updated
        })
        return
      }
      setMessages(prev => {
        const updated = [...prev]
        updated[updated.length - 1] = { role: 'assistant', content: 'Connection failed. Ensure local models are running and responsive.' }
        return updated
      })
    } finally {
      setStreaming(false)
      setStreamStage(null)
      streamingDoneRef.current = true
      abortControllerRef.current = null
      // If voice mode and nothing queued, trigger final flush
      if (voiceModeRef.current && !ttsSpeakingRef.current) {
        speakNext()
      }
      // The server persists the turn as the stream closes; pick it up so the
      // sidebar isn't showing a stale list.
      void refreshHistory()
    }
  }, [cancelTTS, feedTTS, speakNext, refreshHistory])

  const handleQuery = async () => {
    await submitQuery(query.trim())
  }

  /* ── The doctor home's inline composer lands HERE ──────────────────────────
   *
   * DoctorHomePanel's "Ask Sage about this ward…" box calls askSage(), which
   * latches the question and switches to this tab (src/lib/orbBus.ts). Nothing
   * drained the latch, so the question was silently discarded: the doctor typed,
   * pressed send, and arrived at an empty greeting screen with no answer and no
   * error. The sender had a test; the receiver did not exist.
   *
   * BOTH paths are wired because either can be the live one. On the first ask of
   * a session this panel is a lazy chunk that has not mounted yet, so the event
   * fires into the void and the mount below drains the latch; once mounted, the
   * panel is kept alive by the shell (display:none, not unmounted), so a second
   * ask arrives as the event. orbBus clears the latch in whichever runs first,
   * so the question is asked exactly once.
   *
   * submitQuery is read through a ref so this effect can stay mount-only: as a
   * dependency it would re-subscribe on every render of a streaming answer.
   */
  const submitQueryRef = useRef(submitQuery)
  // Synced after each render rather than written during it (react-hooks/refs);
  // declared before the mount effect below so it runs first.
  useEffect(() => { submitQueryRef.current = submitQuery })
  useEffect(() => {
    const pending = takePendingSageQuestion()
    if (pending) void submitQueryRef.current(pending)
    return onSageAsk(q => { void submitQueryRef.current(q) })
  }, [])

  const handleStop = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      setStreaming(false)
    }
    cancelTTS()
  }

  // ─── Voice Recording Logic ───
  const startVoiceRecording = async () => {
    // Claim the shared mic so the floating orb's continuous recognition pauses
    // and Scribe dictation can't grab the device mid-recording.
    if (!claimMic('sage-panel')) {
      setVoiceStatus('Microphone is in use by another Sage surface.')
      setTimeout(() => setVoiceStatus(''), 2500)
      return
    }
    // Cancel any ongoing TTS before recording
    cancelTTS()
    setVoiceStatus('Requesting microphone…')

    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
    } catch (err) {
      // First attempt failed — retry once after 500ms (mic might be releasing from another app)
      try {
        await new Promise(r => setTimeout(r, 500))
        setVoiceStatus('Retrying mic access…')
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        streamRef.current = stream
      } catch (err2) {
        const errName = (err2 as Error)?.name || (err as Error)?.name || 'Unknown'
        const errMsg = (err2 as Error)?.message || (err as Error)?.message || ''
        const msg = errName === 'NotAllowedError' ? 'Mic blocked: allow in browser settings.'
          : errName === 'NotReadableError' ? 'Mic busy: close other apps using mic and retry.'
          : errName === 'NotFoundError' ? 'No mic detected by browser. Check System Preferences > Sound.'
          : `Mic error [${errName}]: ${errMsg}`
        setVoiceStatus(msg)
        setTimeout(() => setVoiceStatus(''), 5000)
        releaseMic('sage-panel')
        return
      }
    }

    setVoiceStatus('Listening…')
    audioChunksRef.current = []
    liveTextRef.current = ''
    setVoiceLiveText('')
    setVoiceElapsed(0)

    // MediaRecorder for Whisper transcription
    const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })
    recorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunksRef.current.push(e.data) }
    recorder.start()
    mediaRecorderRef.current = recorder

    // Web Speech API for live preview text + 2s silence auto-stop. This streams
    // audio to a cloud ASR, so it is PHI egress — only enable it when the
    // deployment posture permits cloud speech. The MediaRecorder→local-Whisper
    // path above is unaffected and remains the source of truth for the query.
    // When cloud speech is off we fall back to a fixed max-duration auto-stop.
    const SR = cloudSpeechAllowed ? getSpeechRecognitionCtor() : null
    const resetSilenceTimer = () => {
      if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current)
      silenceTimerRef.current = setTimeout(() => {
        // 2s of silence → auto-stop recording
        if (mediaRecorderRef.current?.state === 'recording' && liveTextRef.current.trim()) {
          stopVoiceRecording()
        }
      }, 2000)
    }
    if (!cloudSpeechAllowed) {
      setVoiceStatus('Listening… (local transcription; tap to stop)')
    }
    if (SR) {
      const recog = new SR()
      recog.continuous = true; recog.interimResults = true; recog.lang = 'en-US'
      recog.onresult = (event) => {
        let allFinal = '', currentInterim = ''
        for (let i = 0; i < event.results.length; i++) {
          const r = event.results[i]
          if (r.isFinal) allFinal += r[0].transcript
          else currentInterim += r[0].transcript
        }
        if (allFinal) liveTextRef.current = allFinal
        setVoiceLiveText(liveTextRef.current + (currentInterim ? ' ' + currentInterim : ''))
        // Reset silence timer on any speech
        resetSilenceTimer()
      }
      recog.onerror = () => {}
      recog.onend = () => { if (recognitionRef.current) try { recog.start() } catch { /* already started */ } }
      try { recog.start(); recognitionRef.current = recog } catch { /* live captions are optional: the recording carries on without them */ }
    }

    setVoiceRecording(true)
    const start = Date.now()
    recordingStartRef.current = start
    voiceTimerRef.current = setInterval(() => setVoiceElapsed(Math.floor((Date.now() - start) / 1000)), 1000)
    // Start initial silence timer
    resetSilenceTimer()
  }

  // Kept current after every render. speakNext is memoised once, and it used to
  // call the startVoiceRecording of the FIRST render, whose cloudSpeechAllowed
  // was always false because the AI posture had not loaded yet.
  useEffect(() => { startVoiceRecordingRef.current = () => { void startVoiceRecording() } })

  const stopVoiceRecording = () => {
    if (silenceTimerRef.current) { clearTimeout(silenceTimerRef.current); silenceTimerRef.current = null }
    if (voiceTimerRef.current) { clearInterval(voiceTimerRef.current); voiceTimerRef.current = null }
    if (recognitionRef.current) { const r = recognitionRef.current; recognitionRef.current = null; try { r.stop() } catch { /* already stopped */ } }
    // Release mic ownership so other Sage surfaces resume (the device tracks are
    // stopped below, after the recorder finalizes).
    releaseMic('sage-panel')
    setVoiceRecording(false)
    setVoiceStatus('Processing…')

    if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
      mediaRecorderRef.current.onstop = async () => {
        const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
        const liveText = liveTextRef.current.trim()
        const elapsed = (Date.now() - recordingStartRef.current) / 1000

        // Quick tap (< 1.5s) with no speech detected → skip Whisper
        if (!liveText && elapsed < 1.5) {
          setVoiceLiveText('')
          setVoiceStatus('')
          return
        }
        
        setVoiceStatus('Transcribing with Whisper AI…')
        setVoiceTranscribing(true)
        try {
          const result = await transcribeAudio(blob, `Specific names and context: ${patientNamesHint}`, true)
          const rawText = result.transcript?.trim()
          const finalText = rawText ? correctClinicalSpelling(rawText) : ''
          if (finalText) {
            setVoiceStatus('Sending to Sage…')
            setVoiceLiveText('')
            setVoiceTranscribing(false)
            await submitQuery(finalText)
          } else if (liveText) {
            setVoiceStatus('Using live transcript…')
            setVoiceLiveText('')
            setVoiceTranscribing(false)
            await submitQuery(liveText)
          } else {
            setVoiceLiveText('')
            setVoiceTranscribing(false)
            setVoiceStatus('No speech detected.')
            setTimeout(() => setVoiceStatus(''), 2000)
          }
        } catch {
          setVoiceLiveText('')
          setVoiceTranscribing(false)
          if (liveText) {
            setVoiceStatus('Whisper failed, using live transcript…')
            await submitQuery(liveText)
          } else {
            setVoiceStatus('Transcription failed.')
            setTimeout(() => setVoiceStatus(''), 2000)
          }
        }
      }
      mediaRecorderRef.current.stop()
    } else {
      setVoiceStatus('')
    }
    streamRef.current?.getTracks().forEach(t => t.stop())
  }

  const toggleVoiceMode = () => {
    cancelTTS()
    const enteringVoice = !voiceMode
    
    if (enteringVoice) {
      setVoiceMode(true)
      setVoiceLiveText('')
      setVoiceElapsed(0)
      setVoiceStatus('Starting voice mode…')
      // Auto-start recording after render settles
      setTimeout(() => startVoiceRecording(), 300)
    } else {
      // Leaving voice mode → force-kill ALL mic resources
      if (voiceTimerRef.current) { clearInterval(voiceTimerRef.current); voiceTimerRef.current = null }
      if (recognitionRef.current) { const r = recognitionRef.current; recognitionRef.current = null; try { r.stop() } catch { /* already stopped */ } }
      if (mediaRecorderRef.current && mediaRecorderRef.current.state !== 'inactive') {
        mediaRecorderRef.current.onstop = null
        try { mediaRecorderRef.current.stop() } catch { /* already inactive */ }
      }
      mediaRecorderRef.current = null
      streamRef.current?.getTracks().forEach(t => t.stop())
      streamRef.current = null
      setVoiceRecording(false)
      setVoiceMode(false)
      setVoiceLiveText('')
      setVoiceElapsed(0)
      setVoiceStatus('')
    }
  }

  const handleQuickAction = (actionQuery: string) => {
    submitQuery(actionQuery)
  }

  const formatTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

  // Extract the last 5 user messages. A question row carries no metadata of
  // its own: the patient it was about is on the answer that follows it.
  const recentUserChats = historyList
    .map((msg, index) => {
      const next = historyList[index + 1]
      const patientName = next?.role === 'assistant' ? next.metadata?.patient_name : undefined
      return { ...msg, index, patientName: typeof patientName === 'string' ? patientName : undefined }
    })
    .filter(m => m.role === 'user')
    .slice(-5)
    .reverse()

  const loadConversation = (startIndex: number) => {
    // The history is a flat message list; a "conversation" is the selected user
    // message plus the assistant reply(ies) that follow it, up to the next user
    // message. Slicing on the clicked index makes each recent-chat item distinct
    // instead of always dumping the entire history.
    if (typeof startIndex !== 'number' || startIndex < 0) {
      setMessages([...historyList])
    } else {
      let end = historyList.length
      for (let i = startIndex + 1; i < historyList.length; i++) {
        if (historyList[i].role === 'user') { end = i; break }
      }
      setMessages(historyList.slice(startIndex, end))
    }
    setTimeout(() => {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, 50)
  }

  const isLanding = messages.length === 0

  
  // ─── Render ───
  return (
    <div className="sage-workspace animate-fade-in">
      <SageHistorySidebar
        recentChats={recentUserChats}
        onSelectChat={loadConversation}
        onNewChat={() => {
          // "New Chat" opens a fresh thread — it must never delete stored
          // conversations. Prior threads stay in Recent Conversations, where
          // they are the clinician's only record of earlier reasoning.
          if (abortControllerRef.current) { abortControllerRef.current.abort(); abortControllerRef.current = null; setStreaming(false); }
          // Re-mint the thread id BEFORE clearing the panel. Clearing `messages`
          // alone used to be the whole of "New Chat", and the server carried on
          // prepending the previous conversation to every prompt.
          conversationIdRef.current = newConversationId()
          cancelTTS(); setMessages([]); setQuery(''); setStreamStage(null);
          void refreshHistory()
        }}
      />

      <div className="sage-main">
        {/* Standing label, on the landing screen and in every conversation. */}
        <InvestigationalBanner
          variant="sage"
          detail="Sage answers with a local AI model that has not been clinically validated. Check every answer against the chart before acting on it."
        />
        {isLanding && !voiceMode ? (
          <SageEmptyState userName={userName} onQuickAction={handleQuickAction} />
        ) : isLanding && voiceMode ? (
          <div className="sage-empty">
            <div className="sage-empty__logo"><OrbLogo size={48} /></div>
            <h1 className="sage-empty__title">Voice Mode</h1>
            <p className="sage-empty__subtitle">Sage is ready to listen.</p>
            {voiceStatus && <p style={{ color: 'var(--text-muted)' }}>{voiceStatus}</p>}
          </div>
        ) : (
          <div className="sage-messages" ref={messagesBoxRef} onScroll={handleMessagesScroll}>
            {messages.map((m, i) => {
              const isActiveStreaming = m.role === 'assistant' && !m.content && streaming && i === messages.length - 1;
              const isEmpty = m.role === 'assistant' && !m.content.trim();
              if (isEmpty && !isActiveStreaming) return null;

              return (
                <Fragment key={i}>
                  <SageMessage
                    message={m}
                    isStreaming={isActiveStreaming}
                    isSpeaking={m.role === 'assistant' && i === messages.length - 1 && isSpeaking}
                  />
                  {m.role === 'assistant' && !(streaming && i === messages.length - 1) && (
                    <SageAnswerMeta
                      className="sage-answer-meta--panel"
                      timing={m.metadata?.timing}
                      tier={m.metadata?.provider?.thinking_tier}
                    />
                  )}
                </Fragment>
              );
            })}
            {streaming && streamStage && (
              <SageStageStatus stage={streamStage} startedAt={turnStartedAt} />
            )}
            <div ref={messagesEndRef} />
          </div>
        )}

        <SageComposer
          query={query}
          setQuery={setQuery}
          onSend={handleQuery}
          onStop={handleStop}
          isStreaming={streaming}
          voiceMode={voiceMode}
          toggleVoiceMode={toggleVoiceMode}
          voiceRecording={voiceRecording}
          startVoiceRecording={startVoiceRecording}
          stopVoiceRecording={stopVoiceRecording}
          isSpeaking={isSpeaking}
        />

        {voiceMode && (voiceRecording || voiceTranscribing) && !isLanding && (
          <div style={{ position: 'absolute', bottom: '100px', right: '40px', background: 'var(--bg-elevated)', border: '1px solid var(--border-glass)', padding: '10px 14px', borderRadius: '10px', boxShadow: 'var(--shadow-lg)', zIndex: 10 }}>
             <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-primary)' }}>
               {voiceRecording ? `Listening... ${formatTime(voiceElapsed)}` : voiceTranscribing ? 'Transcribing...' : ''}
             </p>
             {voiceLiveText && <p style={{ margin: '4px 0 0', fontSize: '0.75rem', color: 'var(--text-muted)' }}>{voiceLiveText}</p>}
          </div>
        )}
      </div>
    </div>
  )
}
