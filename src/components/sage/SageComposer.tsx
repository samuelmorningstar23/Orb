import React, { useRef, useEffect, useSyncExternalStore } from 'react'
import {
  getThinkingMode,
  subscribeThinkingMode,
  toggleThinkingMode,
} from '../../api/thinkingMode'

interface Props {
  query: string
  setQuery: (q: string) => void
  onSend: () => void
  onStop: () => void
  isStreaming: boolean
  voiceMode: boolean
  toggleVoiceMode: () => void
  voiceRecording: boolean
  startVoiceRecording: () => void
  stopVoiceRecording: () => void
  isSpeaking: boolean
  /** The appliance cannot transcribe (no local speech recognition). The voice
   *  button stays pressable and says so; SagePanel refuses on press. */
  voiceUnavailable?: boolean
}

export function SageComposer({
  query,
  setQuery,
  onSend,
  onStop,
  isStreaming,
  voiceMode,
  toggleVoiceMode,
  voiceRecording,
  startVoiceRecording,
  stopVoiceRecording,
  voiceUnavailable = false,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Thinking mode lives in a module store rather than in SagePanel state: the
  // value is needed by `querySage` at send time, which is outside this tree, and
  // it has to survive a reload. See src/api/thinkingMode.ts.
  const thinking = useSyncExternalStore(
    subscribeThinkingMode,
    getThinkingMode,
    () => true, // SSR/first paint: assume the safe default
  )

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setQuery(e.target.value)
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 200) + 'px'
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (query.trim() && !isStreaming) {
        onSend()
      }
    }
  }

  useEffect(() => {
    if (query === '' && textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [query])

  return (
    <div className="sage-composer-wrapper">
      <div className="sage-composer">
        <textarea
          ref={textareaRef}
          className="sage-composer__input"
          placeholder="Ask Sage a clinical question..."
          value={query}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          disabled={voiceMode && voiceRecording}
          rows={1}
        />
        
        <div className="sage-composer__actions">
          {/* ── Thinking mode ──
              A LABELLED pill, not an icon. An icon-only control would leave the
              clinician guessing which brain is about to answer, which is the one
              thing this feature exists to prevent. The OFF state is deliberately
              the loud one: "Fast" is warning-toned and carries the model name,
              because a fast answer is the state that needs explaining, not the
              careful one. */}
          <button
            type="button"
            role="switch"
            aria-checked={thinking}
            aria-label="Thinking mode"
            className={`sage-think-toggle ${thinking ? 'sage-think-toggle--on' : 'sage-think-toggle--off'}`}
            onClick={() => toggleThinkingMode()}
            title={
              thinking
                ? 'Thinking mode ON: answers come from the careful model (slower, more accurate). Click for fast answers.'
                : 'FAST MODE: general answers come from the small model (quicker, less accurate). Patient and medication questions still use the careful model. Click to think again.'
            }
          >
            {thinking ? (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 3l1.9 4.6L18.5 9.5 13.9 11.4 12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/>
                <path d="M18 15l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M13 2L4.5 13.5H11l-1 8.5 8.5-11.5H12z"/>
              </svg>
            )}
            <span className="sage-think-toggle__label">{thinking ? 'Thinking' : 'Fast'}</span>
          </button>

          {/* Voice Toggle */}
          <button
            className={`sage-btn-icon ${voiceMode ? 'sage-btn-icon--active' : ''}`}
            onClick={toggleVoiceMode}
            title={voiceUnavailable && !voiceMode ? 'Voice questions are not available on this appliance' : voiceMode ? 'Switch to text' : 'Voice mode'}
            aria-label={voiceUnavailable && !voiceMode ? 'Voice questions are not available on this appliance' : voiceMode ? 'Switch to text' : 'Voice mode'}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>
            </svg>
          </button>

          {/* Send / Stop Button */}
          {isStreaming ? (
            <button className="sage-btn-icon sage-btn-stop" onClick={onStop} title="Stop generating">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" ry="2"/>
              </svg>
            </button>
          ) : voiceMode ? (
            <button 
              className={`sage-btn-icon ${voiceRecording ? 'sage-btn-stop' : 'sage-btn-send'}`} 
              onClick={voiceRecording ? stopVoiceRecording : startVoiceRecording}
              title={voiceRecording ? "Stop recording" : "Start recording"}
            >
              {voiceRecording ? (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" ry="2"/></svg>
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                </svg>
              )}
            </button>
          ) : (
            <button className="sage-btn-icon sage-btn-send" onClick={onSend} disabled={!query.trim()} title="Send message">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Standing notice while fast mode is armed. The per-answer banner says
          what happened AFTER the fact; this says what is about to happen, so a
          clinician typing a question already knows which brain will read it.
          It also states the floor, because "fast mode is on" without "except
          for patient and drug questions" is a half-truth that would make the
          floor look like a bug the first time it fires. */}
      {!thinking && (
        <div className="sage-fast-notice" role="status" aria-live="polite">
          <span aria-hidden="true">⚡</span>
          <span>
            <strong>Fast mode.</strong> General questions are answered by the small
            model (quicker, less accurate). Patient, medication and dose questions
            still go to the careful model.
          </span>
        </div>
      )}
    </div>
  )
}
