import type { SageHistoryMessage } from '../api/client'
import { localDateKey, parseUTC } from './datetime'

export interface SageThread {
  index: number
  role: string
  content: string
  conversationId: string | null
  timestamp: string
  patientName?: string
  bucket: 'Today' | 'Yesterday' | 'Earlier'
  messages: SageHistoryMessage[]
}

/** Group the server's bounded history window without mixing thread identity. */
export function sageThreads(messages: SageHistoryMessage[], now = new Date()): SageThread[] {
  const groups = new Map<string | null, SageThread>()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  messages.forEach((message, index) => {
    const id = message.conversation_id || null
    let thread = groups.get(id)
    if (!thread) {
      thread = { index, role: 'user', content: '', conversationId: id, timestamp: '', bucket: 'Earlier', messages: [] }
      groups.set(id, thread)
    }
    thread.messages.push(message)
    if (!thread.content && message.role === 'user') thread.content = message.content
    if (message.timestamp && Number.isFinite(parseUTC(message.timestamp).getTime()) &&
        (!thread.timestamp || parseUTC(message.timestamp).getTime() >= parseUTC(thread.timestamp).getTime())) {
      thread.timestamp = message.timestamp
    }
  })
  return [...groups.values()].map(thread => {
    const names = new Set(thread.messages.map(message => message.metadata?.patient_name).filter((name): name is string => !!name))
    thread.patientName = names.size === 1 ? [...names][0] : names.size > 1 ? 'Multiple patients' : undefined
    thread.content ||= thread.conversationId ? 'Earlier conversation' : 'Earlier unthreaded messages'
    const date = thread.timestamp ? parseUTC(thread.timestamp) : null
    if (thread.conversationId && date && Number.isFinite(date.getTime())) {
      const key = localDateKey(date)
      thread.bucket = key === localDateKey(now) ? 'Today' : key === localDateKey(yesterday) ? 'Yesterday' : 'Earlier'
    }
    return thread
  }).sort((a, b) => {
    const time = (value: string) => value && Number.isFinite(parseUTC(value).getTime()) ? parseUTC(value).getTime() : 0
    return time(b.timestamp) - time(a.timestamp) || b.index - a.index
  })
}
