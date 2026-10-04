import React from 'react'
import OrbLogo from '../OrbLogo'
import { SAGE_TAGLINE } from './SageEmptyState'
import type { SageHistoryMessage } from '../../api/client'

interface HistoryItem {
  role: string
  content: string
  metadata?: SageHistoryMessage['metadata']
  /** The patient the conversation was about, when the server knows it. */
  patientName?: string
}

interface Props {
  recentChats: (HistoryItem & { index: number })[]
  onSelectChat: (chatIndex: number) => void
  onNewChat: () => void
}

export function SageHistorySidebar({ recentChats, onSelectChat, onNewChat }: Props) {
  // We can group by today/yesterday if we had dates, but for now we just show Recent Conversations
  return (
    <div className="sage-sidebar">
      <div className="sage-sidebar__header">
        <div className="sage-sidebar__brand">
          <OrbLogo size={26} />
          <div className="sage-sidebar__brand-text">
            <span className="sage-sidebar__brand-name">Sage</span>
            <span className="sage-sidebar__brand-sub">{SAGE_TAGLINE}</span>
          </div>
        </div>
        <button className="sage-new-chat-btn" onClick={onNewChat} title="New Chat">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          New Chat
        </button>
      </div>

      <div className="sage-sidebar__list">
        {recentChats.length === 0 ? (
          <div className="sage-sidebar__empty">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            <span className="sage-sidebar__empty-text">No recent conversations.<br/>Start by asking Sage anything.</span>
          </div>
        ) : (
          <div className="sage-history-group">
            <div className="sage-history-group-label">Recent Conversations</div>
            {recentChats.map((chat) => (
              /* A real button: reloading a past conversation is the only way
                 back to it, and as a <div onClick> the whole list was skipped
                 by Tab — keyboard users were locked into the current chat. */
              <button
                type="button"
                key={chat.index}
                onClick={() => onSelectChat(chat.index)}
                className="sage-history-item"
              >
                <span className="sage-history-item__title">
                  {chat.content || "Empty chat"}
                </span>
                {/* Nothing rather than a guess: "General Medical" was printed
                    under every chat the server had no patient for, including
                    chats about a patient. */}
                {(chat.patientName || chat.metadata?.patient_name) && (
                  <span className="sage-history-item__preview">
                    Patient: {chat.patientName || chat.metadata?.patient_name}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
