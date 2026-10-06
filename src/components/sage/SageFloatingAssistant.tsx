import { useState, useEffect, useRef, useCallback } from 'react'
import { useActiveView } from '../../contexts/ActiveViewContext'
import { gotoTab } from '../../lib/orbBus'
import './SageFloatingAssistant.css'

interface SageFloatingAssistantProps {
  activeTab?: string
}

/* The floating Sage orb is TEXT-ONLY (owner ruling, 4 October 2026: Orb is
   strictly offline). It used to listen through the browser's Web Speech API,
   which sends the audio to an outside recognition service, and read answers
   aloud. Pressing it now says voice is not available here and offers the way
   in that does work: the Sage panel, to type. */

export default function SageFloatingAssistant({ activeTab }: SageFloatingAssistantProps) {
  const [cardOpen, setCardOpen] = useState(false)
  const { activeTab: viewTab } = useActiveView()
  const orbRef = useRef<HTMLButtonElement | null>(null)
  const openSageRef = useRef<HTMLButtonElement | null>(null)

  const closeCard = useCallback((returnFocus: boolean) => {
    setCardOpen(false)
    if (returnFocus) orbRef.current?.focus()
  }, [])

  const openSage = useCallback(() => {
    setCardOpen(false)
    gotoTab('sage')
  }, [])

  // Focus the card's one action as it opens, so a keyboard user can go
  // straight on to Sage; Escape closes it and hands focus back to the orb.
  useEffect(() => {
    if (!cardOpen) return
    openSageRef.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeCard(true) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cardOpen, closeCard])

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
      className={`sage-fab${onComposerTab ? ' sage-fab--raised' : ''}${onOwnMicTab ? ' sage-fab--own-mic' : ''}`}
    >
      {cardOpen && (
        <div className="sage-fab__card" id="sage-fab-card">
          <div className="sage-fab__card-header">
            <div className="sage-fab__card-dot" />
            <span className="sage-fab__card-title">Sage</span>
          </div>
          <div className="sage-fab__card-body">
            <p className="sage-fab__message">Voice is not available here. Type your question in Sage instead.</p>
            <button
              ref={openSageRef}
              type="button"
              className="btn-primary sage-fab__open"
              onClick={openSage}
            >
              Open Sage to type
            </button>
          </div>
        </div>
      )}

      {/* Main orb button */}
      <button
        ref={orbRef}
        type="button"
        className="sage-fab__orb"
        onClick={() => (cardOpen ? closeCard(false) : setCardOpen(true))}
        title="Ask Sage"
        aria-expanded={cardOpen}
        aria-controls={cardOpen ? 'sage-fab-card' : undefined}
      >
        <div className="sage-fab__orb-visual" />
        {/* The Sage nav icon, not a microphone: the orb never listens. */}
        <svg className="sage-fab__icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <path d="M12 16v-4M12 8h.01" />
        </svg>
      </button>
    </div>
  )
}
