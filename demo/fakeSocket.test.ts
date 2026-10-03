/**
 * The website demo replays one recording of the socket, on a loop, for as long
 * as the page is open. Its vitals ticks carry the timestamps they were recorded
 * with, and the patient chart ignores a tick strictly older than the
 * observation already on screen (the command bar shows ONE observation). Replayed
 * unchanged, every tick of the second pass is older than the last tick of the
 * first, so from the second pass on the demo chart froze on one reading.
 *
 * Two clocks are in play. The replay runs on the decoded timeline (frame.t,
 * every quiet stretch capped at 2.5 s by decodeFrames in ./shim.ts); the payload
 * timestamps still carry the recording's full gaps. The demo clock (pinClock in
 * ./shim.ts) ticks at real speed, so a replayed tick keeps up with it only when
 * its timestamp moves with the replay.
 *
 * Expected timestamps are literals worked out by hand from the recording's own
 * format (microseconds, +00:00), never computed with the code under test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { replayTimelineShifts, shiftVitalsTimestamp, type RecordedFrame } from './fakeSocket'

// schedule(): one pass is max(last frame t, 5000) + 4000 ms. Every recording
// below but one ends by t = 5000, so a pass is 9 s; the one that runs longer
// works out its own.
const PASS_MS = 9000

const T0 = '2026-09-07T12:55:43.611262+00:00'

/** A vitals:update payload in the shape the recorder captured. */
function vitals(timestamp: unknown, patient_id = 'PAT-B3AC4BB3') {
  return {
    patient_id,
    timestamp,
    heart_rate: 146.7, systolic_bp: 108.3, diastolic_bp: 67.1, spo2: 90.0,
    temperature: 37.0, respiratory_rate: 35.5, consciousness: 'alert', on_oxygen: false,
    risk: {
      score: 0.5, level: 'critical', raw_news2: 10, scoring_model: 'NEWS2', spo2_scale: 1,
      breakdown: [{ parameter: 'Heart rate', value: 146.7, points: 3, note: 'Severe tachycardia (HR 147)' }],
    },
  }
}

const frame = (t: number, event: string, ...args: unknown[]): RecordedFrame => ({ t, event, args })

interface Tick { at: number; event: string; payload: Record<string, unknown> }

// io() hands out one module-level FakeSocket. A fresh module per test keeps one
// test's connection, timers, handlers and replay progress out of the next.
async function openReplay(frames: RecordedFrame[], events: string[]) {
  window.__orbDemoFrames = frames
  vi.resetModules()
  const { io } = await import('./fakeSocket')
  vi.useFakeTimers()
  const socket = io()
  const ticks: Tick[] = []
  for (const event of events) {
    socket.on(event, (payload: unknown) => ticks.push({ at: Date.now(), event, payload: payload as Tick['payload'] }))
  }
  return { socket, ticks }
}

async function connectReplay(frames: RecordedFrame[], events: string[], runMs: number) {
  const { socket, ticks } = await openReplay(frames, events)
  socket.connect()
  vi.advanceTimersByTime(runMs)
  socket.disconnect()
  return ticks
}

const stamp = (t: Tick) => Date.parse(t.payload.timestamp as string)

afterEach(() => {
  vi.useRealTimers()
  delete window.__orbDemoFrames
})

describe('demo socket replay: every pass is newer than the last', () => {
  it('stamps the second pass later than the first, in step with the demo clock', async () => {
    const ticks = await connectReplay([
      frame(0, 'vitals:update', vitals(T0)),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
    ], ['vitals:update'], 2 * PASS_MS - 1)

    // Control: both passes ran, and the first is the recording as it was.
    expect(ticks).toHaveLength(4)
    expect(ticks[0].payload.timestamp).toBe(T0)
    expect(ticks[1].payload.timestamp).toBe('2026-09-07T12:55:44.611262+00:00')

    // The defect: the second pass replayed the first pass's timestamps, so its
    // first tick was older than the reading on screen and the chart froze.
    expect(stamp(ticks[2])).toBeGreaterThan(stamp(ticks[1]))
    expect(ticks[2].payload.timestamp).toBe('2026-09-07T12:55:52.611Z')
    expect(ticks[3].payload.timestamp).toBe('2026-09-07T12:55:53.611Z')
    // A tick is as old, by the demo clock, on every pass. The fake clock fires a
    // 0 ms timer set while it is ticking 1 ms late (browsers clamp it too).
    const ageDrift = (ticks[2].at - stamp(ticks[2])) - (ticks[0].at - stamp(ticks[0]))
    expect(Math.abs(ageDrift)).toBeLessThanOrEqual(1)
  })

  it('never goes backwards when the decoder squeezed a quiet stretch', async () => {
    // The real recordings: three minutes of payload time between two ticks the
    // replay plays 2.5 s apart. Shifting each pass by the pass length alone
    // still leaves the next pass three minutes behind.
    const ticks = await connectReplay([
      frame(0, 'vitals:update', vitals(T0)),
      frame(2500, 'vitals:update', vitals('2026-09-07T12:58:43.611262+00:00')),
      frame(5000, 'vitals:update', vitals('2026-09-07T12:58:46.111262+00:00')),
    ], ['vitals:update'], 3 * PASS_MS - 1)

    // Control: three passes, and the last tick of the recording keeps its stamp.
    expect(ticks).toHaveLength(9)
    expect(ticks[2].payload.timestamp).toBe('2026-09-07T12:58:46.111262+00:00')

    // The first pass is the recording as it was. The chart's own rows carry these
    // times (the doctor recording serves a second chart bundle that already holds
    // most of its ticks), so a tick moved forward on the first pass would claim
    // to be newer than a reading the chart already has.
    expect(ticks.slice(0, 3).map(t => t.payload.timestamp)).toEqual([
      T0, '2026-09-07T12:58:43.611262+00:00', '2026-09-07T12:58:46.111262+00:00',
    ])

    const stamps = ticks.map(stamp)
    for (let i = 1; i < stamps.length; i++) expect(stamps[i]).toBeGreaterThan(stamps[i - 1])
    // From the second pass on, each tick sits on the replay's own timeline: its
    // stamp minus the moment the replay plays it is one constant.
    const playedAt = ticks.map((_, i) => Math.floor(i / 3) * PASS_MS + [0, 2500, 5000][i % 3])
    expect(new Set(stamps.slice(3).map((s, i) => s - playedAt[i + 3])).size).toBe(1)
    expect(ticks[3].payload.timestamp).toBe('2026-09-07T12:58:50.111Z')
    expect(ticks[6].payload.timestamp).toBe('2026-09-07T12:58:59.111Z')
  })

  it('moves each pass on by the whole pass, also when the recording ends on another event', async () => {
    // The doctor recording runs for minutes and its last frame is not a vitals
    // tick. Here the last frame is at t = 7000, so a pass is 7000 + 4000 = 11 s.
    const ticks = await connectReplay([
      frame(0, 'vitals:update', vitals(T0)),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
      frame(7000, 'sage:insight', { patient_id: 'PAT-B3AC4BB3', timestamp: T0, text: 'NEWS2 rising' }),
    ], ['vitals:update'], 2 * 11000 - 1)

    // Control: the first pass is the recording as it was.
    expect(ticks[0].payload.timestamp).toBe(T0)
    expect(ticks[1].payload.timestamp).toBe('2026-09-07T12:55:44.611262+00:00')

    // The second pass starts one whole pass after the first, and is stamped that
    // much later: not the 9 s floor, and not a span that stops at the last tick.
    expect(ticks[2].at - ticks[0].at).toBeGreaterThanOrEqual(11000)
    expect(ticks[2].at - ticks[0].at).toBeLessThanOrEqual(11001)
    expect(ticks[2].payload.timestamp).toBe('2026-09-07T12:55:54.611Z')
    expect(ticks[3].payload.timestamp).toBe('2026-09-07T12:55:55.611Z')
    expect(ticks).toHaveLength(4)
  })

  it('carries on after a disconnect and connect instead of starting over', async () => {
    // Re-auth after the idle lock calls login(), which disconnects and connects
    // the socket (authenticateSocket in src/api/client.ts) while the chart stays
    // mounted. A replay that started over at the recorded stamps would be older
    // than the tick on screen for as long as the demo had already run.
    const { socket, ticks } = await openReplay([
      frame(0, 'vitals:update', vitals(T0)),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
    ], ['vitals:update'])
    socket.connect()
    vi.advanceTimersByTime(4 * PASS_MS + 1500)

    // Control: five passes ran, the fifth 36 s on from the recording.
    expect(ticks).toHaveLength(10)
    expect(ticks[9].payload.timestamp).toBe('2026-09-07T12:56:20.611Z')

    socket.disconnect()
    socket.connect()
    vi.advanceTimersByTime(PASS_MS + 500)
    socket.disconnect()
    // Control: the restarted pass, and the first tick of the one after it.
    expect(ticks).toHaveLength(13)

    // The defect: the first tick after the reconnect went back to the recording.
    expect(stamp(ticks[10])).toBeGreaterThanOrEqual(stamp(ticks[9]))
    for (let i = 11; i < ticks.length; i++) expect(stamp(ticks[i])).toBeGreaterThan(stamp(ticks[i - 1]))
    // And it picks up in step with the demo clock: as old as the first pass's
    // ticks, not a pass ahead of it (37.5 s into the replay, so 37.5 s on).
    const age = (t: Tick) => t.at - stamp(t)
    for (const t of ticks.slice(10)) expect(Math.abs(age(t) - age(ticks[0]))).toBeLessThanOrEqual(1)
    expect(ticks[10].payload.timestamp).toBe('2026-09-07T12:56:21.111Z')
  })

  it('leaves the recorded frames untouched, since every pass replays them again', async () => {
    const frames = [
      frame(0, 'vitals:update', vitals(T0)),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
    ]
    const before = structuredClone(frames)
    const ticks = await connectReplay(frames, ['vitals:update'], 2 * PASS_MS - 1)

    expect(ticks).toHaveLength(4)
    expect(frames).toEqual(before)
    // The second pass differs from the recording in its timestamp and nothing else.
    expect({ ...ticks[2].payload, timestamp: null }).toEqual({ ...before[0].args[0] as object, timestamp: null })
  })

  it('passes every other event through exactly as recorded', async () => {
    const insight = { patient_id: 'PAT-B3AC4BB3', timestamp: T0, text: 'NEWS2 rising' }
    const message = { room_id: 'ward-3', created_at: T0, body: 'Bed 4 reviewed' }
    const frames = [
      frame(0, 'vitals:update', vitals(T0)),
      frame(500, 'sage:insight', insight),
      frame(1000, 'message:new', message, 'extra-arg'),
    ]
    const ticks = await connectReplay(frames, ['sage:insight', 'message:new'], 2 * PASS_MS - 1)

    expect(ticks.map(t => t.event)).toEqual(['sage:insight', 'message:new', 'sage:insight', 'message:new'])
    expect(ticks[2].payload).toBe(insight)
    expect(ticks[3].payload).toBe(message)
    expect(insight.timestamp).toBe(T0)
  })

  it('leaves a tick whose timestamp does not parse as it was, and still moves the rest', async () => {
    // The unreadable tick is the LAST vitals frame, so it must not become the
    // anchor the other ticks are laid out from.
    const bad = vitals('not a time', 'PAT-14A1F0D6')
    const ticks = await connectReplay([
      frame(0, 'vitals:update', vitals(T0)),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
      frame(2000, 'vitals:update', bad),
    ], ['vitals:update'], 2 * PASS_MS - 1)

    expect(ticks).toHaveLength(6)
    expect(ticks[2].payload).toBe(bad)
    expect(ticks[5].payload).toBe(bad)
    expect(bad.timestamp).toBe('not a time')
    expect(ticks[3].payload.timestamp).toBe('2026-09-07T12:55:52.611Z')
  })
})

describe('shiftVitalsTimestamp', () => {
  it('moves a vitals:update timestamp and writes it as ISO', () => {
    expect(shiftVitalsTimestamp('vitals:update', [vitals(T0)], 9000)[0])
      .toMatchObject({ timestamp: '2026-09-07T12:55:52.611Z' })
    expect(shiftVitalsTimestamp('vitals:update', [vitals(T0)], -1000)[0])
      .toMatchObject({ timestamp: '2026-09-07T12:55:42.611Z' })
  })

  it('reads the offset the timestamp was written with', () => {
    expect(shiftVitalsTimestamp('vitals:update', [vitals('2026-09-07T18:25:43.611+05:30')], 1000)[0])
      .toMatchObject({ timestamp: '2026-09-07T12:55:44.611Z' })
  })

  it('returns new args and never touches the ones it was given', () => {
    const payload = vitals(T0)
    const extra = { ack: true }
    const args = [payload, extra]
    const out = shiftVitalsTimestamp('vitals:update', args, 9000)

    expect(out).not.toBe(args)
    expect(out[0]).not.toBe(payload)
    expect(out[1]).toBe(extra)
    expect(args).toEqual([vitals(T0), { ack: true }])
    expect({ ...(out[0] as object), timestamp: null }).toEqual({ ...payload, timestamp: null })
  })

  it('returns the args as they are for a shift of 0', () => {
    const args = [vitals(T0)]
    expect(shiftVitalsTimestamp('vitals:update', args, 0)).toBe(args)
  })

  it('returns the args as they are when the shift cannot make a date, rather than throw', () => {
    // toISOString throws on a date past the +/-8.64e15 ms range; a replay tick
    // must never take the socket's timer callback down with it.
    const args = [vitals(T0)]
    expect(shiftVitalsTimestamp('vitals:update', args, 1e16)).toBe(args)
    expect(shiftVitalsTimestamp('vitals:update', args, Number.NaN)).toBe(args)
    expect(shiftVitalsTimestamp('vitals:update', args, Number.POSITIVE_INFINITY)).toBe(args)
  })

  it('touches only vitals:update', () => {
    const args = [{ patient_id: 'PAT-B3AC4BB3', timestamp: T0 }]
    expect(shiftVitalsTimestamp('sage:insight', args, 9000)).toBe(args)
    expect(shiftVitalsTimestamp('vitals:history', args, 9000)).toBe(args)
  })

  it.each([
    ['an unparseable string', [vitals('not a time')]],
    ['an empty string', [vitals('')]],
    ['a number', [vitals(1788785743611)]],
    ['no timestamp', [vitals(undefined)]],
    ['a null payload', [null]],
    ['a string payload', ['PAT-B3AC4BB3']],
    ['no args', []],
  ])('leaves %s alone', (_label, args) => {
    expect(shiftVitalsTimestamp('vitals:update', args as unknown[], 9000)).toBe(args)
  })

  it('works from the payload, not the clock, under the demo\'s pinned Date', () => {
    // pinClock in ./shim.ts swaps the global Date for a Proxy whose
    // zero-argument constructor and Date.now read the demo clock. A Date built
    // from a value is untouched, which is all the shift may rely on.
    const RealDate = Date
    const pinned = RealDate.parse('2031-01-01T00:00:00Z')
    globalThis.Date = new Proxy(RealDate, {
      construct: (target, args, newTarget) => Reflect.construct(target, args.length ? args : [pinned], newTarget),
      apply: () => new RealDate(pinned).toString(),
      get: (target, prop, receiver) => (prop === 'now' ? () => pinned : Reflect.get(target, prop, receiver)),
    }) as DateConstructor
    try {
      expect(shiftVitalsTimestamp('vitals:update', [vitals(T0)], 9000)[0])
        .toMatchObject({ timestamp: '2026-09-07T12:55:52.611Z' })
    } finally {
      globalThis.Date = RealDate
    }
  })
})

describe('replayTimelineShifts', () => {
  it('is all zeros when the stamps already move with the replay', () => {
    expect(replayTimelineShifts([
      frame(0, 'vitals:update', vitals(T0)),
      frame(400, 'sage:insight', { timestamp: T0 }),
      frame(1000, 'vitals:update', vitals('2026-09-07T12:55:44.611262+00:00')),
    ])).toEqual([0, 0, 0])
  })

  it('pulls the ticks before a squeezed gap forward by what the decoder cut', () => {
    // 180 s of payload time played in 2.5 s: the first tick moves 177.5 s on.
    expect(replayTimelineShifts([
      frame(0, 'vitals:update', vitals(T0)),
      frame(1200, 'sage:insight', { timestamp: T0 }),
      frame(2500, 'vitals:update', vitals('2026-09-07T12:58:43.611262+00:00')),
      frame(3000, 'vitals:update', vitals('not a time')),
    ])).toEqual([177500, 0, 0, 0])
  })

  it('is all zeros with no readable vitals timestamp to anchor on', () => {
    expect(replayTimelineShifts([])).toEqual([])
    expect(replayTimelineShifts([
      frame(0, 'vitals:update', vitals('not a time')),
      frame(500, 'message:new', { body: 'hi' }),
    ])).toEqual([0, 0])
  })
})
