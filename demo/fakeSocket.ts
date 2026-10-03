/**
 * Demo-mode stand-in for socket.io-client.
 *
 * The website embeds a build of this app that has no backend: every request is
 * answered from recordings of the real server (see ./shim.ts). This module is
 * aliased in place of `socket.io-client` in that build. It replays the realtime
 * frames that were recorded alongside the HTTP traffic (vitals ticks, alerts,
 * Relay messages) on the same relative timeline, looping so the ward keeps
 * moving for as long as the demo is open. Outbound emits are accepted and
 * dropped: joining a channel or sending a typing hint has no server to reach.
 */
type Handler = (...args: unknown[]) => void

export interface RecordedFrame { t: number; event: string; args: unknown[] }

declare global {
  interface Window { __orbDemoFrames?: RecordedFrame[] }
}

/** A vitals:update frame's payload timestamp in ms, or null when it has none that parses. */
function vitalsTimestampMs(event: string, args: readonly unknown[]): number | null {
  if (event !== 'vitals:update') return null
  const payload = args[0]
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const ts = (payload as { timestamp?: unknown }).timestamp
  if (typeof ts !== 'string') return null
  const ms = Date.parse(ts)
  return Number.isNaN(ms) ? null : ms
}

/**
 * The args of a recorded frame with its vitals:update timestamp moved by shiftMs.
 *
 * The patient chart ignores a vitals tick strictly older than the reading already
 * on screen, so a loop that replays the recorded timestamps as they are freezes
 * the demo chart from its second pass on. The result is NEW args: the recorded
 * frame is replayed again on every pass and must never change. The same args come
 * back when there is nothing to move: another event, a shift of 0, or a timestamp
 * that is missing or does not parse. Only ever builds a Date from a value, which
 * the demo's pinned Date (pinClock in ./shim.ts) leaves alone; a bare `new Date()`
 * or Date.now() would read the demo clock instead.
 */
export function shiftVitalsTimestamp(event: string, args: unknown[], shiftMs: number): unknown[] {
  const ms = vitalsTimestampMs(event, args)
  if (ms === null || shiftMs === 0 || !Number.isFinite(shiftMs)) return args
  const moved = new Date(ms + shiftMs)
  if (Number.isNaN(moved.getTime())) return args
  const [payload, ...rest] = args
  return [{ ...(payload as Record<string, unknown>), timestamp: moved.toISOString() }, ...rest]
}

/**
 * Per frame, how far its vitals timestamp moves, before the lap a pass adds, so
 * that the stamps run on the replay's own timeline. The first pass plays the
 * recording as it was; every pass after it is laid out by this.
 *
 * decodeFrames (./shim.ts) caps every quiet stretch of the recording at 2.5 s,
 * but the payload timestamps keep the full gaps: the doctor recording plays in
 * under 4 minutes while its ticks span 9. Moving each pass on by the pass length
 * alone would still start the next pass minutes older than the tick on screen.
 * So the last readable tick keeps its recorded stamp and every earlier one moves
 * forward by exactly what the decoder cut after it: the stamps then follow frame
 * order at the speed the visitor watches them, and one pass (always longer than
 * the replayed span) later the next pass starts after the last tick shown.
 * Frames with no readable vitals timestamp get 0.
 */
export function replayTimelineShifts(frames: readonly RecordedFrame[]): number[] {
  const stamps = frames.map(f => vitalsTimestampMs(f.event, f.args))
  let last = stamps.length - 1
  while (last >= 0 && stamps[last] === null) last--
  if (last < 0) return frames.map(() => 0)
  const origin = (stamps[last] as number) - frames[last].t
  return frames.map((f, i) => {
    const ms = stamps[i]
    return ms === null ? 0 : origin + f.t - ms
  })
}

class FakeSocket {
  connected = false
  id = 'demo-socket'
  auth: unknown = {}
  private handlers = new Map<string, Set<Handler>>()
  private timers: number[] = []
  // When the replay first started, by the demo clock. It lives on the socket and
  // not in schedule(): re-auth after the idle lock disconnects and connects the
  // socket (authenticateSocket in src/api/client.ts) while the chart stays
  // mounted, and a replay that started over at the recorded stamps would be
  // older than the tick on screen for as long as the demo had already run.
  private replayStartedAt: number | null = null

  on(event: string, handler: Handler): this {
    if (!this.handlers.has(event)) this.handlers.set(event, new Set())
    this.handlers.get(event)!.add(handler)
    return this
  }

  off(event: string, handler?: Handler): this {
    if (!handler) this.handlers.delete(event)
    else this.handlers.get(event)?.delete(handler)
    return this
  }

  once(event: string, handler: Handler): this {
    const wrapped: Handler = (...args) => { this.off(event, wrapped); handler(...args) }
    return this.on(event, wrapped)
  }

  emit(_event: string, ..._args: unknown[]): this {
    // Outbound traffic has no server in demo mode. An ack callback, if any, is
    // answered so callers awaiting one do not hang.
    const last = _args[_args.length - 1]
    if (typeof last === 'function') (last as Handler)({ ok: true, demo: true })
    return this
  }

  connect(): this {
    if (this.connected) return this
    this.connected = true
    this.dispatch('connect')
    this.schedule()
    return this
  }

  disconnect(): this {
    this.connected = false
    this.timers.forEach(t => window.clearTimeout(t))
    this.timers = []
    this.dispatch('disconnect', 'io client disconnect')
    return this
  }

  private dispatch(event: string, ...args: unknown[]) {
    this.handlers.get(event)?.forEach(h => { try { h(...args) } catch (e) { console.warn('[demo socket]', event, e) } })
  }

  /**
   * Replay the recorded frames on their timeline, then loop with a pause.
   *
   * The very first pass is the recording as it was. The chart's own rows carry
   * the recorded times (the doctor recording serves a second chart bundle that
   * already holds most of its ticks), so a tick moved forward there would claim
   * to be newer than a reading the chart already has. Every later pass lays its
   * vitals ticks on the replay's timeline and moves them on by how long the
   * replay has been running. Date.now() is the demo clock (pinClock in
   * ./shim.ts), which ticks at real speed, so by that clock a tick is as old on
   * every later pass and never older than the last one shown. Unlike a pass
   * count, the elapsed time also keeps up when a background tab's timers fire
   * late, and across a disconnect and connect.
   */
  private schedule() {
    const frames = window.__orbDemoFrames ?? []
    if (!frames.length) return
    const span = Math.max(frames[frames.length - 1].t, 5000)
    const passMs = span + 4000
    const shifts = replayTimelineShifts(frames)
    let asRecorded = this.replayStartedAt === null
    this.replayStartedAt ??= Date.now()
    const startedAt = this.replayStartedAt
    const run = () => {
      if (!this.connected) return
      const lap = Date.now() - startedAt
      frames.forEach((f, i) => {
        const args = asRecorded ? f.args : shiftVitalsTimestamp(f.event, f.args, shifts[i] + lap)
        this.timers.push(window.setTimeout(() => { if (this.connected) this.dispatch(f.event, ...args) }, f.t))
      })
      asRecorded = false
      this.timers.push(window.setTimeout(run, passMs))
    }
    run()
  }
}

const singleton = new FakeSocket()

// Stands in for socket.io-client's `io(url, opts)`. The app calls it with both
// (src/api/client.ts), and both are discarded: there is nothing to connect to,
// the frames are replayed from the recording. Declared with no parameters
// rather than named-and-ignored ones, which is what the lint rule is for.
export function io(): FakeSocket {
  return singleton
}

export type Socket = FakeSocket
export default io
