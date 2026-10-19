import { Fragment, useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from 'react'
import DOMPurify from 'dompurify'
import { getChannels, getMessages, sendMessage, socket, getUser, resolveAlert, surgicalTeamResponse, markChannelRead, getRoomFitFlags, type Channel, type Message, type RoomFitFlag } from '../../api/client'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import { formatClinicalTime, formatDayDivider, localDateKey, parseUTC } from '../../lib/datetime'
import { mergeById, claimOptimistic } from '../../lib/messageMerge'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import '../Relay.css'
import AlertsSection from '../../components/relay/AlertsSection'
import RelaySearch from '../../components/relay/RelaySearch'
import CaseList from '../../components/relay/CaseList'
import SageAmbientSidebar from '../../components/relay/SageAmbientSidebar'
import RoomFitWarning from '../../components/relay/RoomFitWarning'
import { formatMessage } from '../../lib/relayFormat'
import { RELAY_PAGE_SIZE } from '../../lib/relayPaging'
import Icon from '../../components/common/Icon'

// The clinical "06:38 PM", not the browser locale's. The day is carried by
// the dividers in the thread, so a time alone never stands for an old message.
function formatTime(ts: string): string {
  return formatClinicalTime(ts);
}

// Activity order for the room lists. parseUTC, not new Date: the column holds
// bare UTC strings and tz-aware ones side by side.
function activityMs(ts: string | null | undefined): number {
  if (!ts) return 0
  const t = parseUTC(ts).getTime()
  return isNaN(t) ? 0 : t
}

// Who sent it, in words. The column holds role keys ('intelligence' and
// 'system' for Orb's own posts), which were printed as they are.
const SENDER_ROLE_LABEL: Record<string, string> = {
  doctor: 'Doctor',
  surgeon: 'Surgeon',
  anesthetist: 'Anaesthetist',
  nurse: 'Nurse',
  pharmacist: 'Pharmacist',
  admin: 'Administrator',
  cashier: 'Cashier',
  frontdesk: 'Front desk',
  intelligence: 'Orb',
  system: 'Orb',
}

function senderRoleLabel(role: string | null | undefined): string {
  if (!role) return ''
  const known = SENDER_ROLE_LABEL[role.toLowerCase()]
  if (known) return known
  const words = role.replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

// The JSON body of an `alert` or `surgical_alert` message, as the thread
// renders it. Both producers name the surgery: surgery_service's structured
// prep alert (is_structured plus the case fields; relay_routes stamps
// `status` and `resolved_by` on it when it is resolved) and the surgery
// routes' per-member surgical_alert (`text`, `role`, `target_user_id`).
interface AlertMessagePayload {
  surgery_id: string
  is_structured?: boolean
  title?: string
  patient_name?: string
  mrn?: string
  procedure_name?: string
  operating_room?: string
  scheduled_time?: string
  action_required?: string
  status?: string
  resolved_by?: string
  text?: string
  role?: string
  target_user_id?: string
}



/* ═══════════════════════════════════════════════════
   RELAY PANEL — messaging (contained scrollable window)
   ═══════════════════════════════════════════════════ */

export default function RelayPanel() {
  const [channels, setChannels] = useState<Channel[]>([])
  const [selectedChannel, setSelectedChannel] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [newMessage, setNewMessage] = useState('')
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [msgError, setMsgError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  // A send refused because the device is offline. Held separately from the
  // thread's own error line: this is about the message the user just tried to
  // send, and it must sit beside the box her text is still in.
  const [sendError, setSendError] = useState<string | null>(null)
  // The draft, send status and refusal belong to one patient's room. Carrying
  // them into another room risks sending the old patient's text under a new
  // name or leaving the new composer disabled by an abandoned send.
  // Cleared DURING the render that changes rooms
  // (React's pattern for resetting state when a value changes), not in an
  // effect a render later (react-hooks/set-state-in-effect).
  const [composerRoom, setComposerRoom] = useState(selectedChannel)
  if (composerRoom !== selectedChannel) {
    setComposerRoom(selectedChannel)
    setNewMessage('')
    setSending(false)
    setSendError(null)
  }
  const [sortBy, setSortBy] = useState<'recency' | 'urgency'>('recency')
  const [searchQuery, setSearchQuery] = useState('')
  // Names of other members currently typing in the open channel (self excluded).
  const [typingUsers, setTypingUsers] = useState<string[]>([])
  // Per-alert resolution state: the alert id currently being resolved (buttons
  // disabled) and any inline error keyed by alert id.
  const [resolvingAlertId, setResolvingAlertId] = useState<number | null>(null)
  const [alertErrors, setAlertErrors] = useState<Record<number, string>>({})
  // Same per-action state for the targeted surgical alert, keyed by surgery id
  // (both buttons act on the surgery, not on the message).
  const [respondingSurgeryId, setRespondingSurgeryId] = useState<string | null>(null)
  const [surgicalErrors, setSurgicalErrors] = useState<Record<string, string>>({})
  // Wrong-room warnings, keyed by message id. Loaded with the thread (they are
  // durable and room-visible, not a transient toast for the sender), then kept
  // live by the send response and the relay:room_fit socket events.
  const [roomFit, setRoomFit] = useState<Record<number, RoomFitFlag>>({})
  /* Older messages. A thread loads its newest page; a full page means there
     may be more before it, and "Show earlier messages" fetches the page before
     the oldest one shown. A case room used to show its newest 50 and nothing
     before them, so earlier clinical discussion was unreachable. */
  const [hasEarlier, setHasEarlier] = useState(false)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [earlierError, setEarlierError] = useState<string | null>(null)
  // scrollHeight before older messages are prepended, so the reader stays on
  // the message they were reading instead of being moved by the insert.
  const prependAnchorRef = useRef<number | null>(null)
  /* Thread-load ownership. `msgReqSeqRef` is bumped by every loadMessages call;
     `selectedChannelRef` mirrors the open channel (state is a render behind,
     and a response can resolve before that render lands). A response is only
     allowed to touch state if both still agree it is wanted. The mirror is
     written in a layout effect, which runs as the render commits and before
     any passive effect or promise callback, rather than during the render
     itself (react-hooks/refs). */
  const msgReqSeqRef = useRef(0)
  const sendReqSeqRef = useRef(0)
  const selectedChannelRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    selectedChannelRef.current = selectedChannel
    // Returning to the same room is a new visit, too. An old send may finish
    // after a new send has started there and must not settle its composer.
    sendReqSeqRef.current += 1
  }, [selectedChannel])
  /* Read receipts for messages that arrive in the open room. The room used to
     be marked read only when it opened, so every message the reader watched
     arrive stayed unread and came back as a badge on the list. `shown` is the
     channel whose thread actually loaded (a room still loading has shown
     nothing); `pending` is a room with live arrivals not yet marked read. */
  const shownChannelRef = useRef<string | null>(null)
  const readPendingRef = useRef<string | null>(null)
  const readTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const messagesContainerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const typingTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const typingEmitRef = useRef(0)

  // Whether the reader is parked at the live end of the thread. Relay used to
  // scroll to the bottom UNCONDITIONALLY on every socket push, which yanks a
  // nurse off whatever she was reading the instant anyone else types — worst on
  // a busy ward round, which is exactly when threads move fastest and when
  // scrolling back to check a dose matters most.
  const stickToBottomRef = useRef(true)

  const isNearBottom = useCallback(() => {
    const el = messagesContainerRef.current
    if (!el) return true
    return el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }, [])

  const handleMessagesScroll = useCallback(() => {
    stickToBottomRef.current = isNearBottom()
  }, [isNearBottom])

  // Scroll to bottom of messages container only (not the page).
  // `force` is for the cases where the reader's own action means bottom: opening
  // a channel, and sending a message. Incoming pushes must NOT force.
  const scrollToBottom = useCallback((force = false) => {
    if (!force && !stickToBottomRef.current) return
    const container = messagesContainerRef.current
    if (container) {
      requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight
        stickToBottomRef.current = true
      })
    }
  }, [])

  // Both loaders are declared above the effects that call them on mount and
  // on every room change.
  const loadChannels = async () => {
    try { setChannels(await getChannels()) } catch (err) { console.error('Channels error:', err) }
  }

  const loadMessages = async (channelId: string) => {
    /* Clearing first was only half the fix. It stops a FAILED fetch from leaving
       the previous thread on screen, but it does nothing about a LATE one: open
       bed 3, get bored on a slow ward link, back out, open bed 7 — bed 7 paints,
       then bed 3's response finally arrives and overwrites it. The header still
       says bed 7. The clinician is reading another patient's traffic and has no
       way to tell.

       So every response is now stamped with the request that asked for it. A
       response is applied only if it is still the newest request AND its channel
       is still the open one; anything else is dropped on the floor. This covers
       the read receipt too — a late load used to mark a channel read that the
       clinician had already navigated away from. */
    const seq = ++msgReqSeqRef.current
    const owns = () => seq === msgReqSeqRef.current && selectedChannelRef.current === channelId

    setLoadingMsgs(true)
    setMessages([])
    setMsgError(null)
    setHasEarlier(false)
    setEarlierError(null)
    try {
      const msgs = await getMessages(channelId)
      if (!owns()) return
      // Always sort chronologically so messages display in natural order
      msgs.sort((a: Message, b: Message) => parseUTC(a.timestamp).getTime() - parseUTC(b.timestamp).getTime())
      setMessages(msgs)
      setHasEarlier(msgs.length >= RELAY_PAGE_SIZE)
      shownChannelRef.current = channelId
      // Opening a channel marks its messages read → clear the unread badge.
      // Optimistically zero the local count, then persist server-side. This
      // covers anything that arrived while the thread was loading.
      if (readPendingRef.current === channelId) readPendingRef.current = null
      setChannels(prev => prev.map(c => c.id === channelId ? { ...c, unread_count: 0 } : c))
      markChannelRead(channelId).catch(err => console.error('Mark-read error:', err))
      // Wrong-room flags travel with the thread, so they load with it. A failure
      // here must not blank the messages — a thread without its flags is worse
      // than a thread, but a blank thread is worse than both.
      getRoomFitFlags(channelId)
        .then(flags => {
          if (!owns()) return
          setRoomFit(Object.fromEntries((flags || []).map(f => [f.message_id, f])))
        })
        .catch(err => console.error('Room-fit flags error:', err))
    } catch (err) {
      console.error('Messages error:', err)
      if (!owns()) return
      setMsgError((err as Error)?.message || 'Could not load this thread.')
    }
    finally { if (owns()) setLoadingMsgs(false) }
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: loadChannels sets state only after its fetch resolves.
  useEffect(() => { loadChannels() }, [])

  /* Whether the reader can see the open room. The shell keeps this panel
     mounted behind other tabs with an inline display:none, so an open room is
     not necessarily a room on screen, and a message that lands there unseen
     must stay unread. */
  const roomOnScreen = useCallback(() => {
    if (document.visibilityState === 'hidden' || !roomRef.current) return false
    for (let el: HTMLElement | null = roomRef.current; el; el = el.parentElement) {
      if (el.hidden || el.style.display === 'none') return false
    }
    return true
  }, [])

  const flushRead = useCallback(() => {
    if (readTimerRef.current) { clearTimeout(readTimerRef.current); readTimerRef.current = null }
    const ch = readPendingRef.current
    if (!ch || ch !== selectedChannelRef.current || ch !== shownChannelRef.current) return
    // Not on screen: stays pending until the tab comes back, the next arrival
    // finds it on screen, or the reader backs out of the room.
    if (!roomOnScreen()) return
    readPendingRef.current = null
    setChannels(prev => prev.map(c => c.id === ch ? { ...c, unread_count: 0 } : c))
    markChannelRead(ch).catch(err => console.error('Mark-read error:', err))
  }, [roomOnScreen])

  // Debounced: a burst of arrivals is one receipt, not one POST each.
  const scheduleRead = useCallback((channelId: string) => {
    readPendingRef.current = channelId
    if (readTimerRef.current) clearTimeout(readTimerRef.current)
    readTimerRef.current = setTimeout(flushRead, 1500)
  }, [flushRead])

  useEffect(() => {
    const onVisibility = () => { if (document.visibilityState === 'visible') flushRead() }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [flushRead])

  const backToList = async () => {
    const ch = selectedChannelRef.current
    const shown = !!ch && shownChannelRef.current === ch
    if (readTimerRef.current) { clearTimeout(readTimerRef.current); readTimerRef.current = null }
    readPendingRef.current = null
    setSelectedChannel(null)
    // What arrived while the thread was on screen has been seen, so persist
    // that BEFORE the list re-reads the counts, or the badge comes straight
    // back. Never for a room still loading: it has shown nothing.
    if (shown && ch) {
      setChannels(prev => prev.map(c => c.id === ch ? { ...c, unread_count: 0 } : c))
      try { await markChannelRead(ch) } catch (err) { console.error('Mark-read error:', err) }
    }
    loadChannels()
  }

  /* At 768px and below the shell stops bounding its main column (.shell__main
     height: auto), so the room grew to the full length of its thread: the
     messages never scrolled inside it, the room opened at the oldest message
     and the composer sat thousands of pixels down the page. Bound the room to
     the viewport below whatever sits above it; the thread then scrolls inside
     and scrollToBottom lands on the newest message with the composer on screen. */
  const roomRef = useRef<HTMLDivElement>(null)
  const [roomHeight, setRoomHeight] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!selectedChannel || typeof window.matchMedia !== 'function') return
    const narrow = window.matchMedia('(max-width: 768px)')
    const fit = () => {
      const el = roomRef.current
      if (!el || !narrow.matches) { setRoomHeight(null); return }
      const top = el.getBoundingClientRect().top + window.scrollY
      setRoomHeight(Math.max(320, Math.floor(window.innerHeight - top)))
    }
    // The list may have been scrolled; the room starts from the top of the page.
    if (narrow.matches) window.scrollTo(0, 0)
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [selectedChannel])

  // Sage's panel is a slide-over drawer below 900px with no way to open it
  // until this toggle existed. Keyed to the room, so it never follows the
  // reader into the next patient's room already open.
  const [sageOpenFor, setSageOpenFor] = useState<string | null>(null)
  const sageOpen = !!selectedChannel && sageOpenFor === selectedChannel
  useEffect(() => {
    if (!sageOpen) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSageOpenFor(null) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [sageOpen])

  useEffect(() => {
    if (selectedChannel) {
      // Opening a channel always lands at the live end, regardless of where the
      // reader was parked in the channel they just left.
      stickToBottomRef.current = true
      // (A send error from the previous room is cleared during the render that
      // changed rooms: see composerRoom.)
      shownChannelRef.current = null
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      loadMessages(selectedChannel)

      const handleNewMsg = (msg: Message) => {
        // Personal socket rooms can deliver traffic for other case rooms.
        // Attribution follows the payload and the currently committed room,
        // rather than whichever subscription happened to receive it.
        if (msg.channel_id !== selectedChannel || selectedChannelRef.current !== selectedChannel) return
        setMessages(prev => {
          if (prev.find(m => m.id === msg.id)) return prev;
          // The broadcast of MY OWN send usually beats the POST response back.
          // Appending it here and letting the response then rewrite the
          // optimistic entry to the same real id is what printed every sent
          // message twice (duplicate React key). Claim the optimistic bubble
          // instead — see lib/messageMerge.ts.
          const claimed = claimOptimistic(prev, msg)
          if (claimed) return claimed
          const updated = [...prev, msg]
          // Re-sort to maintain chronological order
          updated.sort((a, b) => parseUTC(a.timestamp).getTime() - parseUTC(b.timestamp).getTime())
          return mergeById(updated)
        })
        // Someone else's message landing in the open room is read by the
        // reader watching it (your own is never unread).
        const me = getUser()
        if (msg.channel_id === selectedChannel && (!me || msg.sender_id !== me.id)) {
          scheduleRead(selectedChannel)
        }
      }
      const handleUpdateMsg = (msg: Message) => {
        if (msg.channel_id !== selectedChannel || selectedChannelRef.current !== selectedChannel) return
        setMessages(prev => prev.map(m => m.id === msg.id ? msg : m))
      }
      // Wrong-room detection. A `flag` can arrive later than the message it is
      // about: the deterministic band lands with the send response, but the
      // ambient model can promote a silent `review` a second or two afterwards,
      // and a warning that only ever reached the sender's own browser would be
      // useless to the doctor reading the thread. Both events are room-scoped.
      const handleRoomFit = (flag: RoomFitFlag) => {
        if (!flag?.message_id) return
        setRoomFit(prev => ({ ...prev, [flag.message_id]: flag }))
      }
      const handleRoomFitResolved = (flag: RoomFitFlag) => {
        if (!flag?.message_id) return
        setRoomFit(prev => (prev[flag.message_id] ? { ...prev, [flag.message_id]: flag } : prev))
      }

      socket.emit('join_channel', { channel_id: selectedChannel })
      socket.on('message:new', handleNewMsg)
      socket.on('message:update', handleUpdateMsg)
      socket.on('relay:room_fit', handleRoomFit)
      socket.on('relay:room_fit_resolved', handleRoomFitResolved)

      // Typing indicators: a peer's 'typing' event shows their name for ~4s,
      // refreshed on each keystroke event (the timer is reset per user).
      const handleTyping = (data: { channel_id: string; sender_id: string; sender_name: string }) => {
        if (data.channel_id !== selectedChannel) return
        const me = getUser()
        if (me && data.sender_id === me.id) return
        setTypingUsers(prev => prev.includes(data.sender_name) ? prev : [...prev, data.sender_name])
        clearTimeout(typingTimersRef.current[data.sender_name])
        typingTimersRef.current[data.sender_name] = setTimeout(() => {
          setTypingUsers(prev => prev.filter(n => n !== data.sender_name))
          delete typingTimersRef.current[data.sender_name]
        }, 4000)
      }

      // Reconnect resilience: the server assigns a fresh sid on every reconnect
      // and only re-enters the personal user room — channel rooms are NOT
      // restored, so live messages silently stopped after any network blip.
      // Re-emit join on (re)connect so the open channel keeps receiving events.
      const handleConnect = () => {
        socket.emit('join_channel', { channel_id: selectedChannel })
      }
      const handleDisconnect = () => setTypingUsers([])

      socket.on('typing', handleTyping)
      socket.on('connect', handleConnect)
      socket.on('disconnect', handleDisconnect)

      return () => {
        socket.off('message:new', handleNewMsg)
        socket.off('message:update', handleUpdateMsg)
        socket.off('relay:room_fit', handleRoomFit)
        socket.off('relay:room_fit_resolved', handleRoomFitResolved)
        socket.off('typing', handleTyping)
        socket.off('connect', handleConnect)
        socket.off('disconnect', handleDisconnect)
        const timers = typingTimersRef.current
        Object.values(timers).forEach(clearTimeout)
        typingTimersRef.current = {}
        setTypingUsers([])
        if (readTimerRef.current) { clearTimeout(readTimerRef.current); readTimerRef.current = null }
        readPendingRef.current = null
        shownChannelRef.current = null
        socket.emit('leave_channel', { channel_id: selectedChannel })
      }
    }
    // scheduleRead is a stable callback (its chain ends in deps []), so naming
    // it never re-runs this join/leave effect.
  }, [selectedChannel, scheduleRead])

  // Keep the reader in place across a prepend: the content above grew by
  // exactly the difference in scrollHeight. Layout effect, so it lands before
  // paint and before the scroll-to-bottom effect below.
  useLayoutEffect(() => {
    const before = prependAnchorRef.current
    if (before == null) return
    prependAnchorRef.current = null
    const el = messagesContainerRef.current
    if (el) el.scrollTop += el.scrollHeight - before
  }, [messages])

  // Scroll to bottom when messages change, but only within the container
  useEffect(() => { scrollToBottom() }, [messages, scrollToBottom])

  // The shell keeps this panel mounted for the whole session (display:none), so
  // the mount fetch above would otherwise be the ONLY channel load — unread
  // badges, last_message and urgency ordering would show the login-time
  // snapshot all shift. The server emits message:new only into channel rooms
  // (which a client joins only while that channel is open), so there is no push
  // signal for unopened rooms: poll instead, paused while the tab is hidden.
  useVisiblePolling(loadChannels, 30000)

  const loadEarlier = async () => {
    const channelId = selectedChannel
    // The oldest message the server knows (an optimistic send has a negative id).
    const oldest = messages.find(m => m.id > 0)
    if (!channelId || !oldest || loadingEarlier) return
    const seq = msgReqSeqRef.current
    const owns = () => seq === msgReqSeqRef.current && selectedChannelRef.current === channelId
    setLoadingEarlier(true)
    setEarlierError(null)
    try {
      const page = await getMessages(channelId, oldest.id)
      if (!owns()) return
      page.sort((a: Message, b: Message) => parseUTC(a.timestamp).getTime() - parseUTC(b.timestamp).getTime())
      const el = messagesContainerRef.current
      if (el) {
        prependAnchorRef.current = el.scrollHeight
        stickToBottomRef.current = false
      }
      setMessages(prev => {
        const have = new Set(prev.map(m => m.id))
        return [...page.filter(m => !have.has(m.id)), ...prev]
      })
      setHasEarlier(page.length >= RELAY_PAGE_SIZE)
    } catch (err) {
      if (!owns()) return
      setEarlierError((err as Error)?.message || 'Earlier messages did not load.')
    } finally {
      if (owns()) setLoadingEarlier(false)
    }
  }

  // Emit a 'typing' ping, throttled to at most once every 1.5s so a burst of
  // keystrokes doesn't flood the socket (server re-broadcasts to the channel).
  const emitTyping = useCallback(() => {
    if (!selectedChannel) return
    const now = Date.now()
    if (now - typingEmitRef.current < 1500) return
    typingEmitRef.current = now
    socket.emit('typing', { channel_id: selectedChannel })
  }, [selectedChannel])

  const handleSend = async () => {
    const content = newMessage.trim()
    if (!content || !selectedChannel || sending) return

    // REFUSE BEFORE THE OPTIMISTIC COPY EXISTS.
    //
    // api/client.sendMessage routes through offlineMutate: with no connection it
    // queues the request and resolves with a pending copy, which the block below
    // renders into the thread. On a ward messaging surface that is the most
    // convincing lie Orb can tell — "bed 4 deteriorating, please review" appears
    // sent, and the sender walks away believing a doctor has been called.
    //
    // The queue does not make that safe. It is deleted wholesale at sign-out
    // (App.logout → clearOfflineQueue), and a PIN session on a shared ward
    // tablet signs itself out after two minutes idle. A message queued in a
    // corridor blackspot is very likely destroyed before the wi-fi returns, with
    // nobody told. So the send is refused while she is still holding the tablet
    // and can act on it — walk to the station, or use the phone.
    try {
      assertOnlineForWrite('send this message')
    } catch (err) {
      setSendError(err instanceof Error ? err.message : 'Could not send this message.')
      return
    }
    setSendError(null)

    const channelId = selectedChannel
    const threadSeq = msgReqSeqRef.current
    const sendSeq = ++sendReqSeqRef.current
    const ownsComposer = () => sendSeq === sendReqSeqRef.current && selectedChannelRef.current === channelId
    const ownsThread = () => ownsComposer() && threadSeq === msgReqSeqRef.current

    const user = getUser()

    // Optimistic update: add the message to the list immediately.
    // `client_key` marks it as not-yet-server-authored so the socket echo of
    // this same send can claim it instead of appending a second bubble.
    const optimisticMsg: Message = {
      id: Date.now(), // temp id
      client_key: `opt-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      channel_id: channelId,
      sender_id: user?.id || 'unknown',
      sender_name: user?.name || 'You',
      sender_role: user?.role || 'doctor',
      content,
      message_type: 'text',
      timestamp: new Date().toISOString(),
    }

    setMessages(prev => [...prev, optimisticMsg])
    setNewMessage('')
    setSending(true)

    // Keep focus on input
    inputRef.current?.focus()

    try {
      const sent = await sendMessage(channelId, content)
      if (!ownsThread()) return
      // Swap the temp entry for what the send actually produced. mergeById is
      // load-bearing: if the socket echo already claimed the bubble (or landed
      // as its own entry), the map below would otherwise leave two entries
      // sharing the real id and the message would render twice.
      setMessages(prev =>
        mergeById(prev.map(m => (m.id === optimisticMsg.id ? { ...sent, client_key: undefined } : m))),
      )
      // Wrong-room warning, if the detector fired. It rides back on the send
      // response so it appears at the same instant as the sent message — as
      // immediate as a pre-send dialog would have been, with none of that
      // design's ability to swallow the message.
      if (sent?.room_fit) {
        const fit = sent.room_fit
        setRoomFit(prev => ({ ...prev, [fit.message_id ?? sent.id]: fit }))
      }
      // Offline, sendMessage does NOT reject: it durably queues the message and
      // resolves with a pending copy (negative id). Refreshing from the server
      // then — or treating the refresh failure as a send failure — would drop a
      // message that IS going to be delivered, and each re-send mints a new
      // idempotency key, so the room receives duplicates on reconnect.
      const queuedOffline = typeof sent?.id === 'number' && sent.id < 0
      if (!queuedOffline) {
        try {
          const freshMsgs = await getMessages(channelId)
          if (!ownsThread()) return
          freshMsgs.sort((a: Message, b: Message) => parseUTC(a.timestamp).getTime() - parseUTC(b.timestamp).getTime())
          setMessages(mergeById(freshMsgs))
        } catch (refreshErr) {
          // The message is already on the server — a failed refresh is cosmetic.
          console.error('Message refresh error:', refreshErr)
        }
      }
    } catch (err) {
      console.error('Send error:', err)
      if (!ownsComposer()) return
      // Genuine send failure — remove the optimistic message
      if (ownsThread()) setMessages(prev => prev.filter(m => m.id !== optimisticMsg.id))
      setNewMessage(draft => draft || content) // Restore without replacing a newer draft.
      setSendError(err instanceof Error ? err.message : 'Could not send this message.')
    } finally {
      if (ownsComposer()) setSending(false)
    }
  }
  // Resolve a structured alert (Confirm/Escalate). Awaits the request so we can
  // surface failures — 'Alert already processed', 403, network — inline near the
  // alert and re-enable the buttons instead of failing silently.
  const handleResolveAlert = async (alertId: number, action: 'confirm' | 'escalate') => {
    if (resolvingAlertId) return
    setResolvingAlertId(alertId)
    setAlertErrors(prev => {
      if (!(alertId in prev)) return prev
      const { [alertId]: _removed, ...rest } = prev
      return rest
    })
    try {
      await resolveAlert(alertId, action)
      // Refresh from server so the alert reflects its resolved status.
      if (selectedChannel) await loadMessages(selectedChannel)
    } catch (err) {
      console.error('Resolve alert error:', err)
      const message = (err as { message?: string } | null)?.message
      setAlertErrors(prev => ({ ...prev, [alertId]: message || 'Could not resolve alert. Please try again.' }))
    } finally {
      setResolvingAlertId(null)
    }
  }

  // Targeted surgical alert (Confirm/Decline). Same contract as
  // handleResolveAlert: busy state while in flight so an impatient double-tap
  // can't post the response twice (the endpoint has no already-responded
  // guard), and the failure surfaces inline instead of only in the console.
  const handleSurgicalResponse = async (surgeryId: string, action: 'confirm' | 'decline') => {
    if (!surgeryId || respondingSurgeryId) return
    setRespondingSurgeryId(surgeryId)
    setSurgicalErrors(prev => {
      if (!(surgeryId in prev)) return prev
      const { [surgeryId]: _removed, ...rest } = prev
      return rest
    })
    try {
      await surgicalTeamResponse(surgeryId, action)
      // Refresh so the thread shows the recorded response.
      if (selectedChannel) await loadMessages(selectedChannel)
    } catch (err) {
      console.error('Surgical response error:', err)
      const message = (err as { message?: string } | null)?.message
      setSurgicalErrors(prev => ({
        ...prev,
        [surgeryId]: message || 'Could not send your response. Please try again.',
      }))
    } finally {
      setRespondingSurgeryId(null)
    }
  }

  const caseRooms = useMemo(() => {
    let list = channels.filter(c => c.type === 'case_room')
    
    // Apply search filter
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(c => 
        c.name.toLowerCase().includes(q) || 
        (c.patient_id && c.patient_id.toLowerCase().includes(q)) ||
        (c.mrn && c.mrn.toLowerCase().includes(q)) ||
        (c.diagnosis && c.diagnosis.toLowerCase().includes(q))
      );
    }
    
    if (sortBy === 'recency') {
      return [...list].sort((a, b) => 
        activityMs(b.last_activity) - activityMs(a.last_activity)
      )
    } else {
      return [...list].sort((a, b) => {
        // Unread/alerts first, then recency
        const aPriority = (a.alert_count || 0) + (a.unread_count || 0);
        const bPriority = (b.alert_count || 0) + (b.unread_count || 0);
        if (bPriority !== aPriority) return bPriority - aPriority;
        return activityMs(b.last_activity) - activityMs(a.last_activity)
      })
    }
  }, [channels, sortBy, searchQuery])

  // AlertsSection fetches its feed once per mount. The polled channel list is
  // the only signal this panel gets about new alerts (the server pushes
  // message:new into channel rooms the client has not joined), so key the
  // section on the per-channel alert counts: it re-mounts — and refetches —
  // exactly when that changes, instead of flashing on every poll tick.
  const alertsKey = useMemo(
    () => channels.map(c => `${c.id}:${c.alert_count || 0}`).join('|'),
    [channels]
  )

  const deptChannels = useMemo(() => {
    const list = channels.filter(c => c.type === 'department')
    if (sortBy === 'recency') {
      return [...list].sort((a, b) => 
        activityMs(b.last_activity) - activityMs(a.last_activity)
      )
    }
    return list
  }, [channels, sortBy])

  // A divider above the first message of each calendar day ("Today",
  // "Yesterday", "30 Sep"). The thread printed bare times, so a message from
  // last week read as one from this morning.
  const dayDividers = useMemo(() => {
    const out = new Map<number, string>()
    let prevKey = ''
    for (const m of messages) {
      if (m.message_type === 'ambient_action' || !m.timestamp) continue
      const at = parseUTC(m.timestamp)
      if (isNaN(at.getTime())) continue
      const key = localDateKey(at)
      if (key !== prevKey) out.set(m.id, formatDayDivider(at))
      prevKey = key
    }
    return out
  }, [messages])

  return (
    <div className="relay-panel animate-fade-in">
      {!selectedChannel ? (
        <div className="relay-panel__channels-view">
          <div className="relay-panel__channel-scroll">
            {/* Order of the page: what needs action, then the search, then the
                rooms it filters. The Case Rooms heading and its sort used to sit
                above Action Required, labelling the wrong section. */}
            <AlertsSection key={alertsKey} onSelectChannel={setSelectedChannel} channels={channels} />
            <RelaySearch onSearch={setSearchQuery} />

            <div className="orb-section">
              <div className="relay-chat__sort-header">
                <h2 className="orb-section-title">Case Rooms</h2>
                <div className="relay-chat__sort-controls">
                  <button 
                    className={`sort-btn ${sortBy === 'recency' ? 'sort-btn--active' : ''}`}
                    onClick={() => setSortBy('recency')}
                    title="Sort by Recency"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                    Recency
                  </button>
                  <button 
                    className={`sort-btn ${sortBy === 'urgency' ? 'sort-btn--active' : ''}`}
                    onClick={() => setSortBy('urgency')}
                    title="Sort by Urgency"
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                    Urgency
                  </button>
                </div>
              </div>
              {/* title="": the sort header above is this list's heading. */}
              <CaseList
                title=""
                channels={caseRooms}
                onSelectChannel={setSelectedChannel}
              />
            </div>

            <CaseList 
              title="Department"
              channels={deptChannels} 
              onSelectChannel={setSelectedChannel} 
              emptyMessage="No department channels found."
            />
          </div>
        </div>
      ) : (
        <div
          ref={roomRef}
          className="relay-room-container"
          style={roomHeight != null ? { height: roomHeight } : undefined}
        >
          {/* min-width 0, not the old 400px: on a phone a 400px floor pushed
              the composer and the send button off the right edge. */}
          <div className="relay-chat">
            <div className="relay-chat__header">
            {/* Returning to the list refreshes it: unread counts and last_message
                for every OTHER room went stale while this channel was open. */}
            <button className="relay-chat__back" onClick={backToList} aria-label="Back to rooms">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
            </button>
            <span className="relay-chat__title">{channels.find(c => c.id === selectedChannel)?.name}</span>
            {/* Shown only below 900px (Relay.css), where Sage is a drawer. */}
            {channels.find(c => c.id === selectedChannel)?.patient_id && (
              <button
                type="button"
                className="relay-chat__sage-toggle"
                aria-expanded={sageOpen}
                onClick={() => setSageOpenFor(sageOpen ? null : selectedChannel)}
              >
                Sage
              </button>
            )}
          </div>
          <div className="relay-chat__messages" ref={messagesContainerRef} onScroll={handleMessagesScroll}>
            {loadingMsgs ? <div className="orb-panel-loading">Loading messages…</div> : msgError ? (
              <div className="relay-thread-error" role="alert">
                <strong>This thread did not load.</strong>
                <span>Nothing is shown rather than the previous thread's messages. {msgError}</span>
                <button type="button" className="relay-thread-error__retry" onClick={() => selectedChannel && loadMessages(selectedChannel)}>Retry</button>
              </div>
            ) : (<>
              {hasEarlier && (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, padding: '8px 0' }}>
                  <button
                    type="button"
                    onClick={loadEarlier}
                    disabled={loadingEarlier}
                    style={loadingEarlier
                      ? { padding: '5px 14px', borderRadius: 999, fontSize: '0.8rem', background: 'var(--btn-disabled-bg)', color: 'var(--btn-disabled-text)', border: '1px solid var(--btn-disabled-border)', cursor: 'not-allowed' }
                      : { padding: '5px 14px', borderRadius: 999, fontSize: '0.8rem', background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-glass)', cursor: 'pointer' }}
                  >
                    {loadingEarlier ? 'Loading earlier messages…' : 'Show earlier messages'}
                  </button>
                  {earlierError && (
                    <span role="alert" style={{ fontSize: '0.78rem', color: 'var(--status-danger-text)' }}>{earlierError}</span>
                  )}
                </div>
              )}
              {/* eslint-disable-next-line react-hooks/refs -- false positive, verified 2026-10-04: the only ref reads reachable from here are in loadMessages, called by handleResolveAlert and handleSurgicalResponse, which this map places in onClick props and never calls during render. The compiler cannot see that through the per-message IIFE below. */}
              {messages.map(m => {
                const dayLabel = dayDividers.get(m.id);
                const body = (() => {
                // Skip legacy ambient_action messages (deprecated Stage 2 artifacts)
                if (m.message_type === 'ambient_action') return null;
                const content = m.content;
                let isStructuredAlert = false;
                let isSurgicalAlert = false;
                let alertParseFailed = false;
                let payload: AlertMessagePayload | null = null;
                const currentUser = getUser();

                // Alert content comes in two dialects: structured JSON (vigil
                // cards with Confirm/Escalate) and plain prose (the Vigil/Lab/
                // Lens bots). Prose is a complete alert, not a parse failure —
                // only content that LOOKS like JSON but won't parse is broken,
                // so only that case earns the "could not be fully displayed"
                // caveat (previously every prose bot alert carried it).
                const looksLikeJson = typeof m.content === 'string' && m.content.trimStart().startsWith('{');
                try {
                  if (m.message_type === 'alert' && looksLikeJson) {
                    payload = JSON.parse(m.content);
                    if (payload && payload.is_structured) {
                      isStructuredAlert = true;
                    }
                  }
                  if (m.message_type === 'surgical_alert' && looksLikeJson) {
                    payload = JSON.parse(m.content);
                    isSurgicalAlert = true;
                  }
                } catch (e) {
                  console.error('[Relay] Failed to parse alert message content:', e);
                  isStructuredAlert = false;
                  isSurgicalAlert = false;
                  payload = null;
                  alertParseFailed = m.message_type === 'alert' || m.message_type === 'surgical_alert';
                }

                // Surgical Alert Card — targeted to specific user
                if (isSurgicalAlert && payload) {
                  const isTargetUser = currentUser?.id === payload.target_user_id;
                  return (
                    <div key={m.id} className="relay-msg relay-msg--surgical-alert" style={{
                      borderLeft: '3px solid var(--status-danger)',
                      background: 'linear-gradient(135deg, color-mix(in srgb, var(--status-danger) 14%, transparent), color-mix(in srgb, var(--status-danger) 4%, transparent))',
                      borderRadius: 8,
                      padding: '12px 14px',
                      marginBottom: 8,
                    }}>
                      <div className="relay-msg__header">
                        <span className="relay-msg__sender" style={{ color: 'var(--status-danger-text)' }}><Icon name="alert" size={12} /> Surgical Alert</span>
                        <span className="relay-msg__role">{m.sender_name}</span>
                        <span className="relay-msg__time">{formatTime(m.timestamp)}</span>
                      </div>
                      <div style={{ marginTop: 6, fontSize: '0.88rem', lineHeight: 1.5 }}>
                        <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{payload.text}</div>
                        {payload.role && (
                          <div style={{ marginTop: 4, fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                            Role: <strong style={{ color: 'var(--text-secondary)' }}>{payload.role}</strong>
                          </div>
                        )}
                      </div>
                      {isTargetUser && (() => {
                        const busy = respondingSurgeryId === payload.surgery_id;
                        return (
                          <>
                            <div style={{ marginTop: 10, display: 'flex', gap: '8px' }}>
                              <button
                                style={{ padding: '6px 16px', background: busy ? 'var(--btn-disabled-bg)' : 'var(--btn-gradient)', border: 'none', color: busy ? 'var(--btn-disabled-text)' : 'var(--btn-primary-text)', borderRadius: 6, fontWeight: 700, fontSize: '0.8rem', cursor: busy ? 'wait' : 'pointer', transition: 'all 0.2s' }}
                                onClick={() => handleSurgicalResponse(payload.surgery_id, 'confirm')}
                                disabled={busy}
                              >
                                ✓ Confirm
                              </button>
                              <button
                                style={{ padding: '6px 16px', background: 'transparent', border: '1px solid var(--status-danger)', color: busy ? 'var(--btn-disabled-text)' : 'var(--status-danger-text)', borderRadius: 6, fontWeight: 700, fontSize: '0.8rem', cursor: busy ? 'wait' : 'pointer', transition: 'all 0.2s' }}
                                onClick={() => handleSurgicalResponse(payload.surgery_id, 'decline')}
                                disabled={busy}
                              >
                                ✕ Decline
                              </button>
                            </div>
                            {surgicalErrors[payload.surgery_id] && (
                              <div role="alert" style={{ marginTop: 8, padding: '8px', fontSize: '0.8rem', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger-text)', borderLeft: '2px solid var(--status-danger)', borderRadius: 4 }}>
                                {surgicalErrors[payload.surgery_id]}
                              </div>
                            )}
                          </>
                        );
                      })()}
                    </div>
                  );
                }

                return (
                  <div key={m.id} className={`relay-msg ${m.message_type === 'alert' ? 'relay-msg--alert' : ''} ${m.message_type === 'system' ? 'relay-msg--system' : ''}`}>
                    <div className="relay-msg__header">
                      <span className="relay-msg__sender">{m.sender_name}</span>
                      {m.is_bot && (
                        <span
                          className="relay-msg__bot-badge"
                          title="Automated / AI-generated message, not a clinician"
                          aria-label="Automated bot message"
                        >AUTO</span>
                      )}
                      {/* A simulator's lab or imaging post (source 'simulated'):
                          its values are not this patient's. */}
                      {m.source === 'simulated' && (
                        <span
                          role="note"
                          title="Generated by the simulator. These values are not from this patient."
                          style={{ fontSize: '0.68rem', fontWeight: 600, padding: '1px 6px', borderRadius: 999, color: 'var(--status-warn-text)', border: '1px solid var(--status-warn)', background: 'var(--bg-glass)' }}
                        >Simulated</span>
                      )}
                      <span className="relay-msg__role">{senderRoleLabel(m.sender_role)}</span>
                      <span className="relay-msg__time">{formatTime(m.timestamp)}</span>
                      {/* Offline sends resolve with a negative id: the message is
                          durably queued and replays on reconnect, so say so
                          rather than letting it look like an ordinary send. */}
                      {m.id < 0 && (
                        <span
                          style={{ fontSize: '0.68rem', color: 'var(--text-muted)', fontStyle: 'italic' }}
                          title="Queued offline: will send when the connection returns"
                        >queued</span>
                      )}
                    </div>
                    {isStructuredAlert && payload ? (
                      <div className="relay-structured-alert">
                        <div style={{ fontWeight: 600, color: 'var(--accent-text)', marginBottom: 8 }}>{payload.title}</div>
                        <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                          <strong>Patient:</strong> {payload.patient_name} (MRN: {payload.mrn})<br/>
                          {payload.procedure_name && <><strong>Procedure:</strong> {payload.procedure_name}<br/></>}
                          {payload.operating_room && <><strong>OR:</strong> {payload.operating_room}{payload.scheduled_time ? ` at ${payload.scheduled_time}` : ''}<br/></>}
                          <div style={{ marginTop: 8, padding: '8px', background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)', borderLeft: '2px solid var(--accent-gold)', borderRadius: 4 }}>
                            {payload.action_required}
                          </div>
                        </div>
                        {payload.status ? (
                          <div style={{ marginTop: 12, padding: '8px', fontSize: '0.85rem', background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', color: 'var(--status-ok-text)', borderRadius: 4, display: 'flex', alignItems: 'center', gap: '6px' }}>
                            ✓ {payload.status === 'resolved' ? 'Confirmed' : 'Escalated'} by {payload.resolved_by || 'User'}
                          </div>
                        ) : (
                          <>
                            <div style={{ marginTop: 12, display: 'flex', gap: '8px' }}>
                              <button
                                style={{ padding: '6px 12px', background: resolvingAlertId === m.id ? 'var(--btn-disabled-bg)' : 'var(--btn-gradient)', border: 'none', color: resolvingAlertId === m.id ? 'var(--btn-disabled-text)' : 'var(--btn-primary-text)', borderRadius: 4, fontWeight: 600, fontSize: '0.8rem', cursor: resolvingAlertId === m.id ? 'wait' : 'pointer' }}
                                onClick={() => handleResolveAlert(m.id, 'confirm')}
                                disabled={resolvingAlertId === m.id}
                              >
                                Confirm
                              </button>
                              <button
                                style={{ padding: '6px 12px', background: 'transparent', border: '1px solid var(--text-muted)', color: resolvingAlertId === m.id ? 'var(--btn-disabled-text)' : 'var(--text-secondary)', borderRadius: 4, fontWeight: 600, fontSize: '0.8rem', cursor: resolvingAlertId === m.id ? 'wait' : 'pointer' }}
                                onClick={() => handleResolveAlert(m.id, 'escalate')}
                                disabled={resolvingAlertId === m.id}
                              >
                                Escalate
                              </button>
                            </div>
                            {alertErrors[m.id] && (
                              <div role="alert" style={{ marginTop: 8, padding: '8px', fontSize: '0.8rem', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger-text)', borderLeft: '2px solid var(--status-danger)', borderRadius: 4 }}>
                                {alertErrors[m.id]}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    ) : (
                      <>
                        <p className="relay-msg__content" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(formatMessage(content), { ALLOWED_TAGS: ['strong', 'em', 'br', 'code', 'span'], ALLOWED_ATTR: ['class'] }) }}></p>
                        {alertParseFailed && (
                          <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
                            (alert could not be fully displayed)
                          </span>
                        )}
                      </>
                    )}
                    {/* Wrong-room warning, attached to the message rather than
                        floated over the room. Shown to EVERYONE in the thread,
                        not just the sender: the reader who inherits this record
                        is the one who most needs to know a foreign clinical fact
                        may have landed in it. `auto_cleared` renders nothing —
                        a warning the system itself withdrew should not keep
                        occupying the thread. */}
                    {roomFit[m.id] && roomFit[m.id].resolution !== 'auto_cleared' && (
                      <RoomFitWarning
                        flag={roomFit[m.id]}
                        onResolved={updated => setRoomFit(prev => ({ ...prev, [m.id]: updated }))}
                      />
                    )}
                  </div>
                );
                })();
                return (
                  <Fragment key={m.id}>
                    {dayLabel && (
                      <div
                        className="relay-day-divider"
                        role="separator"
                        aria-label={dayLabel}
                        style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '10px 0 6px', fontSize: '0.72rem', fontWeight: 600, color: 'var(--text-muted)' }}
                      >
                        <span aria-hidden="true" style={{ flex: 1, height: 1, background: 'var(--border-subtle)' }} />
                        {dayLabel}
                        <span aria-hidden="true" style={{ flex: 1, height: 1, background: 'var(--border-subtle)' }} />
                      </div>
                    )}
                    {body}
                  </Fragment>
                );
              })}
            </>)}
          </div>
          {typingUsers.length > 0 && (
            <div className="relay-chat__typing" role="status" aria-live="polite" style={{ padding: '2px 16px 4px', fontSize: '0.72rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
              {typingUsers.length === 1 ? `${typingUsers[0]} is typing…` : `${typingUsers.slice(0, 2).join(', ')}${typingUsers.length > 2 ? ' and others' : ''} are typing…`}
            </div>
          )}
          {sendError && (
            <div className="relay-chat__send-error" role="alert">
              {sendError}
            </div>
          )}
          <div className="relay-chat__input glass">
            <input
              ref={inputRef}
              type="text"
              aria-label="Message"
              placeholder="Type a message…"
              value={newMessage}
              onChange={e => { setNewMessage(e.target.value); setSendError(null); emitTyping() }}
              onKeyDown={e => e.key === 'Enter' && !e.shiftKey && handleSend()}
              className="relay-chat__text-input"
              disabled={sending}
            />
            <button className="relay-chat__send" onClick={handleSend} disabled={sending} aria-label="Send message">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
              </svg>
            </button>
            </div>
          </div>
          {channels.find(c => c.id === selectedChannel)?.patient_id && (
            <SageAmbientSidebar
              channelId={selectedChannel}
              open={sageOpen}
              onClose={() => setSageOpenFor(null)}
            />
          )}
        </div>
      )}
    </div>
  )
}

