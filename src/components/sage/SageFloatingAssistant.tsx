import { useState, useEffect, useRef, useCallback } from 'react'
import { useActivePatient } from '../../contexts/ActivePatientContext'
import { useActiveView, buildWireViewContext } from '../../contexts/ActiveViewContext'
import { useSageRuntime } from '../../contexts/SageRuntimeContext'
import { useSageHotword } from '../../hooks/useSageHotword'
import { querySageVoice } from '../../api/client'
import { SageAnswerMeta, type SageAnswerTiming } from '../SageAnswerMeta'
import InvestigationalBanner from '../InvestigationalBanner'
import './SageFloatingAssistant.css'

type SageState = 'dormant' | 'listening' | 'processing' | 'speaking'

interface SageFloatingAssistantProps {
  activeTab?: string
}

function sanitizeTextForTTS(text: string): string {
  return text
    .replace(/\*\*?/g, '') // Remove bold/italics asterisks
    .replace(/__?/g, '') // Remove bold/italics underscores
    .replace(/[`#]/g, '') // Remove backticks and hashes
    .replace(/<[^>]*>/g, '') // Remove any XML/HTML tags
    .replace(/Thought:\s*/gi, '') // Remove model scaffolding
    .replace(/Response:\s*/gi, '')
    .replace(/(,\s*){2,}/g, '') // Remove repeating commas
    .replace(/(\.\s*){3,}/g, '') // Remove repeating dots
    .replace(/Full transcription:\s*/gi, '')
    .replace(/Transcript:\s*/gi, '')
    .replace(/Context:\s*/gi, '')
    .replace(/Analysis:\s*/gi, '')
    .replace(/Sage:\s*/gi, '') // Remove model output prefix to prevent self-barge-in
    .trim()
}

export default function SageFloatingAssistant({ activeTab }: SageFloatingAssistantProps) {
  const [state, setState] = useState<SageState>('dormant')
  const [responseText, setResponseText] = useState('')
  // Timing of the FINISHED answer on the card. Null while a turn is in flight
  // or after any reset — the footer must never describe a different answer
  // than the one on screen.
  const [answerTiming, setAnswerTiming] = useState<SageAnswerTiming | null>(null)
  const [queryText, setQueryText] = useState('')
  const [cardVisible, setCardVisible] = useState(false)
  const [cardDismissing, setCardDismissing] = useState(false)
  const { activePatientId, activePatientName } = useActivePatient()
  const { activeTab: viewTab, selection } = useActiveView()
  const { cloudSpeechAllowed, micOwner, claimMic, releaseMic } = useSageRuntime()

  // Live view context refs so the async voice handler always sends the
  // CURRENT screen state, not the one captured when recognition started.
  const viewRef = useRef<{ tab: string; sel: typeof selection }>({ tab: '', sel: null })
  viewRef.current = { tab: viewTab || activeTab || '', sel: selection }
  
  const abortRef = useRef<AbortController | null>(null)
  const dismissTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const initialSilenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const ignoreUntilRef = useRef<number>(0)
  
  // TTS State Refs
  const ttsBufferRef = useRef('')
  const activeUtterancesRef = useRef<Set<SpeechSynthesisUtterance>>(new Set())
  const isStreamDoneRef = useRef(false)
  
  const liveTextRef = useRef('')
  const submittedTextRef = useRef('')
  
  // Audio-reactive visual Refs
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const microphoneStreamRef = useRef<MediaStream | null>(null)
  const animationFrameIdRef = useRef<number | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  
  // Conversation memory — rolling window of last 10 query-response pairs
  const conversationHistoryRef = useRef<Array<{ query: string; response: string }>>([])
  const currentResponseRef = useRef('')
  const stateRef = useRef(state)
  
  const setStateAndRef = useCallback((newState: SageState) => {
    stateRef.current = newState
    setState(newState)
  }, [])

  // Sage is omnipresent: the orb stays mounted and usable on EVERY tab,
  // including sage/scribe (the old blanket self-suspend is gone). What
  // suspends it now is MIC CONTENTION, not the tab: while another surface
  // (SagePanel voice mode, Scribe dictation) owns the shared mic, this
  // assistant's recognition pauses and the orb greys out.
  const micBusyElsewhere = micOwner !== null && micOwner !== 'sage-fab'
  const isTabSuspended = micBusyElsewhere

  const stopAudioAnalysis = useCallback(() => {
    if (animationFrameIdRef.current !== null) {
      cancelAnimationFrame(animationFrameIdRef.current)
      animationFrameIdRef.current = null
    }
    if (microphoneStreamRef.current) {
      microphoneStreamRef.current.getTracks().forEach(t => t.stop())
      microphoneStreamRef.current = null
    }
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(console.error)
      audioContextRef.current = null
    }
    analyserRef.current = null
    if (containerRef.current) {
      containerRef.current.style.setProperty('--audio-level', '0')
    }
  }, [])

  const startAudioAnalysis = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      microphoneStreamRef.current = stream
      
      const audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)()
      audioContextRef.current = audioCtx
      
      const analyser = audioCtx.createAnalyser()
      analyser.fftSize = 256
      analyser.smoothingTimeConstant = 0.5
      
      const source = audioCtx.createMediaStreamSource(stream)
      source.connect(analyser)
      analyserRef.current = analyser
      
      const dataArray = new Uint8Array(analyser.frequencyBinCount)
      
      const updateFrame = () => {
        if (!analyserRef.current) return
        analyserRef.current.getByteFrequencyData(dataArray)
        let sum = 0
        for (let i = 0; i < dataArray.length; i++) {
          sum += dataArray[i]
        }
        const avg = sum / dataArray.length
        // Normalize 0-255 to 0.0-1.0 and boost slightly
        const level = Math.min(1.0, (avg / 255) * 1.8)
        
        if (containerRef.current) {
          containerRef.current.style.setProperty('--audio-level', level.toString())
        }
        animationFrameIdRef.current = requestAnimationFrame(updateFrame)
      }
      
      updateFrame()
    } catch (e) {
      console.warn('[Sage] Web Audio API mic access failed:', e)
    }
  }, [])

  // Manage audio analysis based on state
  useEffect(() => {
    if (state === 'listening') {
      startAudioAnalysis()
    } else {
      stopAudioAnalysis()
    }
    return stopAudioAnalysis
  }, [state, startAudioAnalysis, stopAudioAnalysis])

  // Reset assistant when tab changes to active sage/scribe
  useEffect(() => {
    if (isTabSuspended) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      setStateAndRef('dormant')
      setResponseText('')
      setAnswerTiming(null)
      setQueryText('')
      setCardVisible(false)
      setCardDismissing(false)
      window.speechSynthesis.cancel()
      activeUtterancesRef.current.clear()
      abortRef.current?.abort()
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
      if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current)
      if (initialSilenceTimerRef.current) clearTimeout(initialSilenceTimerRef.current)
    }
  }, [isTabSuspended])

  // ── TTS Callbacks ──
  const followUpTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const handleTTSFinished = useCallback(() => {
    
    // Enter listening state for follow-up questions (no hotword needed)
    liveTextRef.current = ''
    captureStartIndexRef.current = resultsCountRef.current // Ignore any audio buffered during TTS playback
    ignoreUntilRef.current = Date.now() + 1000 // 1-second grace period for delayed acoustic echo
    setStateAndRef('listening')
    
    // Clear any existing dismiss timer
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
    if (followUpTimerRef.current) clearTimeout(followUpTimerRef.current)
    
    // 5s follow-up window — if no speech, go dormant and dismiss card
    followUpTimerRef.current = setTimeout(() => {
      if (stateRef.current === 'listening' && !liveTextRef.current.trim()) {
        setStateAndRef('dormant')
        
        // Dismiss card after a short delay
        dismissTimerRef.current = setTimeout(() => {
          setCardDismissing(true)
          setTimeout(() => {
            setCardVisible(false)
            setCardDismissing(false)
            setResponseText('')
            setAnswerTiming(null)
            setQueryText('')
          }, 300)
        }, 2000)
      }
    }, 5000)
  }, [setStateAndRef])

  const speakSentence = useCallback((sentence: string, isLast = false) => {
    if (!window.speechSynthesis) {
      if (isLast) handleTTSFinished()
      return
    }

    const sanitized = sanitizeTextForTTS(sentence)
    if (!sanitized) {
      if (isLast) {
        isStreamDoneRef.current = true
        if (activeUtterancesRef.current.size === 0) {
          handleTTSFinished()
        }
      }
      return
    }

    const utterance = new SpeechSynthesisUtterance(sanitized)
    
    // Set voice to a natural-sounding English voice if available
    const voices = window.speechSynthesis.getVoices()
    const voice = voices.find(v => v.lang.startsWith('en') && (v.name.includes('Google') || v.name.includes('Natural'))) ||
                  voices.find(v => v.lang.startsWith('en'))
    if (voice) {
      utterance.voice = voice
    }
    
    utterance.rate = 1.08 // natural but snappy round pace

    activeUtterancesRef.current.add(utterance)
    if (isLast) {
      isStreamDoneRef.current = true
    }

    utterance.onend = () => {
      activeUtterancesRef.current.delete(utterance)
      if (isStreamDoneRef.current && activeUtterancesRef.current.size === 0) {
        handleTTSFinished()
      }
    }

    utterance.onerror = (e) => {
      console.error('[Sage] TTS utterance error:', e)
      activeUtterancesRef.current.delete(utterance)
      if (isStreamDoneRef.current && activeUtterancesRef.current.size === 0) {
        handleTTSFinished()
      }
    }

    window.speechSynthesis.resume()
    window.speechSynthesis.speak(utterance)
  }, [handleTTSFinished])

  const flushTTSBuffer = useCallback(() => {
    const remaining = ttsBufferRef.current.trim()
    if (remaining) {
      speakSentence(remaining, true)
    } else {
      isStreamDoneRef.current = true
      if (activeUtterancesRef.current.size === 0) {
        handleTTSFinished()
      }
    }
  }, [speakSentence, handleTTSFinished])

  const processTokensForTTS = useCallback((newTokens: string) => {
    ttsBufferRef.current += newTokens
    const text = ttsBufferRef.current
    
    // Speak in small clauses for real-time streaming feel.
    // Split on natural break points: sentence endings, commas, semicolons, colons, dashes
    // but only after accumulating enough text for natural speech (~30+ chars).
    const breakRegex = /([.?!;:,–—])\s+/g
    let match
    let lastIndex = 0
    const chunks: string[] = []

    while ((match = breakRegex.exec(text)) !== null) {
      const chunkEnd = match.index + match[1].length // include the punctuation
      const chunk = text.substring(lastIndex, chunkEnd).trim()
      
      if (chunk.length >= 15) {
        // Good chunk — speak it
        chunks.push(chunk)
        lastIndex = breakRegex.lastIndex
      } else if (chunk.length > 0 && text.length - lastIndex > 60) {
        // Short chunk but buffer is getting long — speak it anyway
        chunks.push(chunk)
        lastIndex = breakRegex.lastIndex
      }
      // Otherwise keep accumulating for a more natural phrase
    }

    // Also flush if buffer is getting long with no punctuation break
    if (chunks.length === 0 && text.length >= 50) {
      // Find last space to break at a word boundary
      const lastSpace = text.lastIndexOf(' ', 45)
      if (lastSpace > 15) {
        chunks.push(text.substring(0, lastSpace).trim())
        lastIndex = lastSpace + 1
      }
    }

    if (chunks.length > 0) {
      ttsBufferRef.current = text.substring(lastIndex)
      chunks.forEach(s => speakSentence(s))
    }
  }, [speakSentence])

  // ASR Refs
  const recognitionRef = useRef<any>(null)
  const resultsCountRef = useRef(0)
  const captureStartIndexRef = useRef(0)
  const submittedResultsCountRef = useRef(0)
  const isRecognitionActiveRef = useRef(false)
  const restartTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Voice query handler ──
  const handleQuery = useCallback(async (text: string) => {
    setQueryText(text)
    setResponseText('Let me check...')
    setAnswerTiming(null)
    speakSentence('Let me check.', false)
    currentResponseRef.current = ''
    setStateAndRef('processing')
    setCardVisible(true)
    setCardDismissing(false)
    if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
    if (followUpTimerRef.current) clearTimeout(followUpTimerRef.current)

    // Reset TTS State
    if (window.speechSynthesis) {
      window.speechSynthesis.cancel()
    }
    activeUtterancesRef.current.clear()
    ttsBufferRef.current = ''
    isStreamDoneRef.current = false

    // Record submission markers
    submittedTextRef.current = text
    submittedResultsCountRef.current = resultsCountRef.current

    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller

    try {
      let isFirstToken = true
      const wireView = buildWireViewContext(
        viewRef.current.tab,
        activePatientId,
        activePatientName,
        viewRef.current.sel,
      )
      const { timing } = await querySageVoice(
        text,
        activePatientId || undefined,
        (token: string) => {
          if (isFirstToken) {
            isFirstToken = false
            setResponseText(token)
          } else {
            setResponseText(prev => prev + token)
          }
          currentResponseRef.current += token
          processTokensForTTS(token)
          if (stateRef.current === 'processing') setStateAndRef('speaking')
        },
        controller.signal,
        conversationHistoryRef.current,
        // Page-awareness: tell Sage which screen the clinician is on and which
        // patient/selection is in focus, so the answer is scoped to the current
        // view (wire shape built by buildWireViewContext above).
        wireView,
      )

      if (!controller.signal.aborted) {
        if (timing) setAnswerTiming(timing)
        flushTTSBuffer()
        // Save completed exchange to conversation memory (rolling 10)
        conversationHistoryRef.current = [
          ...conversationHistoryRef.current,
          { query: text, response: currentResponseRef.current },
        ].slice(-10)
      }
    } catch (e: any) {
      if (e.name !== 'AbortError') {
        const errorText = 'Sorry, I encountered an error. Please try again.'
        setResponseText(errorText)
        setStateAndRef('speaking')
        speakSentence(errorText, true)
      }
    }
  }, [activePatientId, activePatientName, processTokensForTTS, flushTTSBuffer, speakSentence])

  // ── Hotword settings integration ──
  const { isEnabled, toggleEnabled } = useSageHotword()

  // Only rendered when voice is allowed at all: a toggle that can never turn
  // anything on is a second, broken-looking orb.
  const toggleLabel = isEnabled ? 'Disable "Sage" hotword' : 'Enable "Sage" hotword'

  const clearSilenceTimer = useCallback(() => {
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current)
      silenceTimerRef.current = null
    }
  }, [])

  const resetSilenceTimer = useCallback(() => {
    clearSilenceTimer()
    silenceTimerRef.current = setTimeout(() => {
      const textToSubmit = liveTextRef.current.trim()
      if (textToSubmit && stateRef.current === 'listening') {
        submittedTextRef.current = textToSubmit
        submittedResultsCountRef.current = resultsCountRef.current
        handleQuery(textToSubmit)
      }
    }, 1200)
  }, [clearSilenceTimer, handleQuery])

  const clearInitialSilenceTimer = useCallback(() => {
    if (initialSilenceTimerRef.current) {
      clearTimeout(initialSilenceTimerRef.current)
      initialSilenceTimerRef.current = null
    }
  }, [])

  const resetInitialSilenceTimer = useCallback(() => {
    clearInitialSilenceTimer()
    initialSilenceTimerRef.current = setTimeout(() => {
      if (stateRef.current === 'listening' && !liveTextRef.current.trim()) {
        setStateAndRef('dormant')
      }
    }, 5000)
  }, [clearInitialSilenceTimer, setStateAndRef])

  // ── Click handlers ──
  const handleOrbClick = useCallback(() => {
    if (isTabSuspended) return

    // Voice is governance-gated (cloud speech = egress of spoken PHI). Tell
    // the clinician why the mic won't open instead of failing silently.
    if (!cloudSpeechAllowed) {
      setCardVisible(true)
      setCardDismissing(false)
      setQueryText('')
      setAnswerTiming(null)
      setResponseText(
        'Voice input is off at this hospital, because browser speech recognition sends audio to an outside service. Use the Sage tab to ask by text.'
      )
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
      dismissTimerRef.current = setTimeout(() => {
        setCardDismissing(true)
        setTimeout(() => {
          setCardVisible(false)
          setCardDismissing(false)
          setResponseText('')
          setAnswerTiming(null)
        }, 300)
      }, 6000)
      return
    }

    // Autoplay unlock gesture for TTS
    if (window.speechSynthesis) {
      window.speechSynthesis.resume()
      const silentUtterance = new SpeechSynthesisUtterance('')
      window.speechSynthesis.speak(silentUtterance)
    }

    if (state === 'dormant') {
      setStateAndRef('listening')
      setQueryText('')
      liveTextRef.current = ''
      submittedTextRef.current = ''
      captureStartIndexRef.current = resultsCountRef.current
      resetInitialSilenceTimer()
    } else if (state === 'listening') {
      // Manual cue to process command
      const textToSubmit = liveTextRef.current.trim()
      if (textToSubmit) {
        clearSilenceTimer()
        submittedTextRef.current = textToSubmit
        submittedResultsCountRef.current = resultsCountRef.current
        handleQuery(textToSubmit)
      } else {
        if (window.speechSynthesis) {
          window.speechSynthesis.cancel()
        }
        activeUtterancesRef.current.clear()
        abortRef.current?.abort()
        if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
        setStateAndRef('dormant')
        setCardVisible(false)
        setCardDismissing(false)
        setResponseText('')
        setAnswerTiming(null)
        setQueryText('')
      }
    } else if (state === 'processing' || state === 'speaking') {
      if (window.speechSynthesis) {
        window.speechSynthesis.cancel()
      }
      activeUtterancesRef.current.clear()
      abortRef.current?.abort()
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
      setStateAndRef('dormant')
      setCardVisible(false)
      setCardDismissing(false)
      setResponseText('')
      setAnswerTiming(null)
      setQueryText('')
    }
  }, [state, isTabSuspended, cloudSpeechAllowed, handleQuery, clearSilenceTimer, resetInitialSilenceTimer, setStateAndRef])

  // PHI GOVERNANCE GATE: the Web Speech API streams mic audio to a cloud ASR
  // service — egress of spoken PHI. Recognition may only run when the server's
  // AI posture explicitly allows cloud speech (ORB_CLOUD_SPEECH_ALLOWED).
  // Until the posture loads, this is false — fail-closed.
  const shouldRecognize = cloudSpeechAllowed && !isTabSuspended && (isEnabled || state !== 'dormant')

  // Mic ownership: claim while actively engaged (listening/processing/
  // speaking), release when dormant, so SagePanel voice mode and Scribe
  // dictation can't fight this assistant for the input device.
  useEffect(() => {
    if (state !== 'dormant') {
      claimMic('sage-fab')
    } else {
      releaseMic('sage-fab')
    }
    return () => releaseMic('sage-fab')
  }, [state, claimMic, releaseMic])
  const micDeniedRef = useRef(false)
  const shouldRecognizeRef = useRef(shouldRecognize)
  // A denied mic must keep recognition OFF across re-renders — otherwise the effect
  // restarts it every render and floods the console with 'not-allowed' errors.
  shouldRecognizeRef.current = shouldRecognize && !micDeniedRef.current

  // ── Unified Continuous Speech Recognition Effect ──
  useEffect(() => {
    const SpeechRecognitionClass = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SpeechRecognitionClass) return

    const startRecog = () => {
      if (!shouldRecognizeRef.current) return
      if (recognitionRef.current) return

      const recog = new SpeechRecognitionClass()
      recog.continuous = true
      recog.interimResults = true
      recog.lang = 'en-US'

      // Clean session states on start
      captureStartIndexRef.current = 0
      resultsCountRef.current = 0
      submittedResultsCountRef.current = 0

      recog.onstart = () => {
        isRecognitionActiveRef.current = true
      }

      recog.onresult = (event: any) => {
        resultsCountRef.current = event.results.length
        const currentState = stateRef.current

        // ─── Case 1: Dormant State (Listening for Hotword) ───
        if (currentState === 'dormant') {
          for (let i = event.resultIndex; i < event.results.length; i++) {
            const transcript = event.results[i][0].transcript
            if (/\bsage\b/i.test(transcript)) {
              
              const matchIdx = transcript.toLowerCase().indexOf('sage')
              const afterSage = transcript.substring(matchIdx + 4).trim()
              
              captureStartIndexRef.current = i
              
              if (window.speechSynthesis) {
                window.speechSynthesis.resume()
                const silentUtterance = new SpeechSynthesisUtterance('')
                window.speechSynthesis.speak(silentUtterance)
              }

              if (afterSage.length > 2) {
                setStateAndRef('listening')
                setQueryText(afterSage)
                liveTextRef.current = afterSage
                resetSilenceTimer()
              } else {
                setStateAndRef('listening')
                setQueryText('')
                liveTextRef.current = ''
                resetInitialSilenceTimer()
              }
              break
            }
          }
        }
        // ─── Case 2: Listening State (Capturing Query) ───
        else if (currentState === 'listening') {
          clearInitialSilenceTimer()

          // Aggressive Echo Cancellation: Discard transcriptions arriving just after TTS finishes
          if (Date.now() < ignoreUntilRef.current) {
            captureStartIndexRef.current = event.results.length
            return
          }

          let allFinal = ''
          let currentInterim = ''
          const startIdx = captureStartIndexRef.current
          
          for (let i = startIdx; i < event.results.length; i++) {
            const r = event.results[i]
            let trans = r[0].transcript
            if (i === startIdx) {
              trans = trans.replace(/^\s*sage\s*/i, '')
            }
            if (r.isFinal) {
              allFinal += trans + ' '
            } else {
              currentInterim += trans
            }
          }

          const fullTranscript = (allFinal + currentInterim).trim()
          if (fullTranscript) {
            liveTextRef.current = fullTranscript
            setQueryText(fullTranscript)
            resetSilenceTimer()
          }
        }
        // ─── Case 3: Processing or Speaking State (Hotword-Only Barge-In) ───
        // Only respond to the "Sage" hotword during playback.
        // This prevents TTS echo from being treated as new input,
        // while still allowing the user to interrupt with "Sage, ..."
        else if (currentState === 'processing' || currentState === 'speaking') {
          for (let i = event.resultIndex; i < event.results.length; i++) {
            const transcript = event.results[i][0].transcript
            if (/\bsage\b/i.test(transcript)) {
              
              // Cancel current TTS and processing
              abortRef.current?.abort()
              if (window.speechSynthesis) {
                window.speechSynthesis.cancel()
              }
              activeUtterancesRef.current.clear()
              if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)

              // Save the interrupted exchange to memory before switching
              if (currentResponseRef.current.trim()) {
                conversationHistoryRef.current = [
                  ...conversationHistoryRef.current,
                  { query: submittedTextRef.current, response: currentResponseRef.current },
                ].slice(-10)
              }

              const matchIdx = transcript.toLowerCase().indexOf('sage')
              const afterSage = transcript.substring(matchIdx + 4).trim()
              
              captureStartIndexRef.current = i

              if (afterSage.length > 2) {
                setStateAndRef('listening')
                setCardVisible(true)
                setCardDismissing(false)
                setResponseText('')
                setAnswerTiming(null)
                setQueryText(afterSage)
                liveTextRef.current = afterSage
                resetSilenceTimer()
              } else {
                setStateAndRef('listening')
                setCardVisible(true)
                setCardDismissing(false)
                setResponseText('')
                setAnswerTiming(null)
                setQueryText('')
                liveTextRef.current = ''
                resetInitialSilenceTimer()
              }
              break
            }
          }
        }
      }

      recog.onerror = (e: any) => {
        // Mic permission denied/blocked: stop permanently and log once — otherwise the
        // restart loop spams the console (and burns CPU) on every render.
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
          if (!micDeniedRef.current) {
            console.warn('[Sage] Hotword disabled — microphone permission not granted.')
          }
          micDeniedRef.current = true
          shouldRecognizeRef.current = false
          return
        }
        // 'no-speech' and 'aborted' are normal lifecycle events, not real errors.
        if (e.error !== 'no-speech' && e.error !== 'aborted') {
          console.error('[Sage] Speech recognition error:', e.error)
        }
      }

      recog.onend = () => {
        isRecognitionActiveRef.current = false
        recognitionRef.current = null

        if (shouldRecognizeRef.current) {
          restartTimeoutRef.current = setTimeout(() => {
            if (shouldRecognizeRef.current) {
              startRecog()
            }
          }, 50)
        }
      }

      try {
        recog.start()
        recognitionRef.current = recog
      } catch (err) {
        console.error('[Sage] Failed to start recognition:', err)
        recognitionRef.current = null
        if (shouldRecognizeRef.current) {
          restartTimeoutRef.current = setTimeout(() => {
            if (shouldRecognizeRef.current) startRecog()
          }, 200)
        }
      }
    }

    if (shouldRecognize) {
      startRecog()
    } else {
      if (recognitionRef.current) {
        try { recognitionRef.current.abort() } catch {}
        // DO NOT null out recognitionRef.current here. Let onend handle the teardown.
      }
    }

    return () => {
      if (restartTimeoutRef.current) clearTimeout(restartTimeoutRef.current)
      if (recognitionRef.current) {
        try { recognitionRef.current.abort() } catch {}
      }
      isRecognitionActiveRef.current = false
    }
  }, [shouldRecognize, isEnabled])

  // ── Cleanup ──
  useEffect(() => {
    return () => {
      abortRef.current?.abort()
      window.speechSynthesis.cancel()
      if (dismissTimerRef.current) clearTimeout(dismissTimerRef.current)
      if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current)
      if (initialSilenceTimerRef.current) clearTimeout(initialSilenceTimerRef.current)
      if (followUpTimerRef.current) clearTimeout(followUpTimerRef.current)
    }
  }, [])

  const showCard = cardVisible

  /* Tabs whose own composer owns the bottom-right corner. The orb is fixed at
     bottom:24px right:24px, so on these it lands ON TOP of a real control:
     measured on Scribe, the orb covered the right-hand ~27px of
     `.scribe-stream-btn` and elementFromPoint returned the orb, not the button —
     the panel's second dictation control was largely unclickable. Sage was
     already listed because its send button had the same problem; Scribe has the
     same shape and was missed. Lifting the orb 96px clears both composers.

     Relay (the Send button in department and direct rooms) and the Ward Round
     (the "Seen, next bed" action corner) have the same shape: a fixed bar at
     the foot of a panel that does not scroll, so they are lifted too.

     Panels that scroll get the other half of the answer: a bottom safe area
     (--fab-clearance) on their scroller, so the last row can always be
     scrolled clear of the orb (.shell__module-scroll, the admin panel, the
     payer drawer). */
  const bottomComposerTabs = new Set(['sage', 'scribe', 'relay', 'round'])
  // Sage and Scribe carry their own microphone, so on a phone the orb hides.
  const ownMicTabs = new Set(['sage', 'scribe'])
  const currentTab = (viewTab || activeTab) ?? ''
  const onComposerTab = bottomComposerTabs.has(currentTab)
  const onOwnMicTab = ownMicTabs.has(currentTab)

  return (
    <div
      ref={containerRef}
      className={`sage-fab sage-fab--${state}${onComposerTab ? ' sage-fab--raised' : ''}${onOwnMicTab ? ' sage-fab--own-mic' : ''}`}
    >
      {/* Listening rings */}
      {state === 'listening' && (
        <div className="sage-fab__rings">
          <div className="sage-fab__ring" />
          <div className="sage-fab__ring" />
          <div className="sage-fab__ring" />
        </div>
      )}

      {/* Status label */}
      {state === 'listening' && (
        <div className="sage-fab__status-label">Listening...</div>
      )}

      {/* Card */}
      {showCard && (
        <div className={`sage-fab__card ${cardDismissing ? 'sage-fab__card--dismissing' : ''}`}>
          <div className="sage-fab__card-header">
            <div className="sage-fab__card-dot" />
            <span className="sage-fab__card-title">
              {state === 'processing' ? 'Thinking' : 'Sage'}
            </span>
            {activePatientName && (
              <span className="sage-fab__card-patient">{activePatientName}</span>
            )}
          </div>
          <InvestigationalBanner variant="compact" />
          <div className="sage-fab__card-body">
            {queryText && <div className="sage-fab__query">{queryText}</div>}
            {state === 'processing' && !responseText && (
              <div className="sage-fab__shimmer" />
            )}
            {responseText && (
              <div className="sage-fab__response">{responseText}</div>
            )}
            {responseText && (
              <SageAnswerMeta className="sage-fab__meta" timing={answerTiming} />
            )}
          </div>
        </div>
      )}

      {/* Main orb button */}
      <button
        className="sage-fab__orb"
        onClick={handleOrbClick}
        title={state === 'dormant' ? 'Ask Sage' : state === 'speaking' ? 'Stop' : 'Sage is listening'}
      >
        <div className="sage-fab__orb-visual" />
        {state === 'speaking' ? (
          <svg className="sage-fab__stop-icon" width="20" height="20" viewBox="0 0 24 24" fill="currentColor" stroke="none">
            <rect x="6" y="6" width="12" height="12" rx="2" />
          </svg>
        ) : state === 'listening' || state === 'dormant' ? (
          <svg className="sage-fab__mic-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="23" />
            <line x1="8" y1="23" x2="16" y2="23" />
          </svg>
        ) : null}
      </button>

      {/* Enable/disable toggle - Rendered after the orb for CSS sibling selector matching.
          Absent when this hospital has not allowed voice: the orb's own card
          says why when it is pressed. */}
      {cloudSpeechAllowed && (
        <button
          className={`sage-fab__toggle ${isEnabled ? 'sage-fab__toggle--active' : ''}`}
          onClick={(e) => { e.stopPropagation(); toggleEnabled() }}
          // Icon-only control: the label IS the state, so it has to be readable by
          // a screen reader, not just on hover as a tooltip.
          aria-label={toggleLabel}
          title={toggleLabel}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="23" />
          </svg>
        </button>
      )}
    </div>
  )
}
