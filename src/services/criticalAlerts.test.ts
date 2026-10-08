/**
 * Regression cover for the critical-alert storm.
 *
 * The vitals source emits `vitals:update` for every admitted patient every 5
 * seconds, and an ICU patient stays in the `critical` band between ticks. This
 * module used to treat every one of those ticks as a fresh alert, so a single
 * steady-state critical patient re-fired the chime and re-alerted the OS
 * notification twelve times a minute for as long as the tab was unfocused.
 *
 * The tests below are written against the EVENT STREAM the simulator actually
 * produces (repeated identical critical readings), not against `fireAlert` — a
 * fixture that only ever emits one tick per patient cannot express this defect.
 */
import { describe, it, expect, beforeEach, afterEach, vi, type MockInstance } from 'vitest';

vi.mock('../api/client', () => {
  const handlers: Record<string, Array<(payload: unknown) => void>> = {};
  return {
    socket: {
      on(event: string, fn: (payload: unknown) => void) {
        (handlers[event] ||= []).push(fn);
      },
      off(event: string, fn: (payload: unknown) => void) {
        handlers[event] = (handlers[event] || []).filter((h) => h !== fn);
      },
      __handlers: handlers,
    },
  };
});

/* ---- WebAudio + Notification doubles ------------------------------------- */

let oscillatorCount = 0;
let notifications: Array<{ title: string; tag?: string; body?: string }> = [];

/** jsdom's window, widened for the constructors this module feature-detects. */
const globals = () => window as unknown as Record<string, unknown>;

function installAudioDouble(): void {
  class FakeParam {
    setValueAtTime = vi.fn();
    exponentialRampToValueAtTime = vi.fn();
  }
  class FakeOscillator {
    type = 'sine';
    frequency = new FakeParam();
    connect = vi.fn();
    start = vi.fn();
    stop = vi.fn();
  }
  class FakeGain {
    gain = new FakeParam();
    connect = vi.fn();
  }
  class FakeAudioContext {
    state = 'running';
    currentTime = 0;
    destination = {};
    resume = vi.fn().mockResolvedValue(undefined);
    createOscillator() {
      oscillatorCount += 1;
      return new FakeOscillator();
    }
    createGain() {
      return new FakeGain();
    }
  }
  globals().AudioContext = FakeAudioContext;
}

function installNotificationDouble(): void {
  class FakeNotification {
    static permission = 'granted';
    onclick: (() => void) | null = null;
    close = vi.fn();
    constructor(title: string, opts?: { tag?: string; body?: string }) {
      notifications.push({ title, tag: opts?.tag, body: opts?.body });
    }
  }
  globals().Notification = FakeNotification;
}

/** One chime is a two-tone envelope, i.e. two oscillators. */
const chimes = () => oscillatorCount / 2;

/* ---- harness -------------------------------------------------------------- */

type MockedClient = {
  socket: { __handlers: Record<string, Array<(payload: unknown) => void>> };
};

let teardown: (() => void) | null = null;
let emit: (event: string, payload: unknown) => void;
let hasFocusSpy: MockInstance<() => boolean>;

async function wire(): Promise<void> {
  vi.resetModules();
  const client = (await import('../api/client')) as unknown as MockedClient;
  const { initCriticalAlerts } = await import('./criticalAlerts');
  teardown = initCriticalAlerts();
  emit = (event, payload) => {
    for (const h of client.socket.__handlers[event] || []) h(payload);
  };
}

/** A `vitals:update` payload as the simulator emits it. */
const vitals = (patientId: string, level: string) => ({
  patient_id: patientId,
  patient_name: `Patient ${patientId}`,
  risk: { level, score: 0.91 },
});

beforeEach(async () => {
  vi.useFakeTimers();
  oscillatorCount = 0;
  notifications = [];
  localStorage.clear();
  installAudioDouble();
  installNotificationDouble();
  // The operator is working in another window — the only state in which this
  // module makes any sound at all.
  hasFocusSpy = vi.spyOn(document, 'hasFocus').mockReturnValue(false);
  await wire();
});

afterEach(() => {
  teardown?.();
  teardown = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('criticalAlerts — steady-state critical patients', () => {
  it('alerts once when a patient enters critical, not once per 5s tick', () => {
    // One minute of simulator output for one ICU patient who is critical
    // throughout: 12 ticks, all identical.
    for (let i = 0; i < 12; i++) {
      emit('vitals:update', vitals('P001', 'critical'));
      vi.advanceTimersByTime(5_000);
    }

    expect(chimes()).toBe(1);
    expect(notifications).toHaveLength(1);
  });

  it('stays silent well past the cooldown while the band never changes', () => {
    // The cooldown alone is not the whole fix: at 120s a steady-state critical
    // would still chime every two minutes, forever. Continuing-critical must
    // not re-alert at all.
    for (let i = 0; i < 120; i++) {
      emit('vitals:update', vitals('P001', 'critical'));
      vi.advanceTimersByTime(5_000); // 10 minutes of ward time
    }

    expect(chimes()).toBe(1);
    expect(notifications).toHaveLength(1);
  });

  it('alerts again when a patient recovers and then re-deteriorates', () => {
    emit('vitals:update', vitals('P001', 'critical'));
    vi.advanceTimersByTime(5_000);
    emit('vitals:update', vitals('P001', 'elevated'));
    vi.advanceTimersByTime(5_000);

    // Same patient, genuinely new deterioration — must not be swallowed.
    vi.advanceTimersByTime(TAG_COOLDOWN_MS);
    emit('vitals:update', vitals('P001', 'critical'));

    expect(notifications).toHaveLength(2);
  });

  it('alerts on an escalation from elevated into critical', () => {
    emit('vitals:update', vitals('P002', 'elevated'));
    vi.advanceTimersByTime(5_000);
    emit('vitals:update', vitals('P002', 'critical'));

    expect(chimes()).toBe(1);
    expect(notifications).toHaveLength(1);
  });

  it('ignores stable and elevated readings entirely', () => {
    emit('vitals:update', vitals('P003', 'stable'));
    emit('vitals:update', vitals('P004', 'elevated'));

    expect(chimes()).toBe(0);
    expect(notifications).toHaveLength(0);
  });
});

describe('criticalAlerts — multiple patients', () => {
  it('notifies per patient but collapses a same-tick burst into one chime', () => {
    // Six ICU patients cross into critical on the same simulator tick. Six
    // overlapping oscillator pairs is heard as one blare, not as six alerts.
    for (const pid of ['P1', 'P2', 'P3', 'P4', 'P5', 'P6']) {
      emit('vitals:update', vitals(pid, 'critical'));
    }

    expect(notifications).toHaveLength(6);
    expect(chimes()).toBe(1);
  });

  it('chimes again for a new patient once the audio floor has passed', () => {
    emit('vitals:update', vitals('P1', 'critical'));
    vi.advanceTimersByTime(5_000);
    emit('vitals:update', vitals('P2', 'critical'));

    expect(chimes()).toBe(2);
    expect(notifications).toHaveLength(2);
  });
});

describe('criticalAlerts — gating', () => {
  it('makes no sound at all while the operator is looking at the tab', async () => {
    teardown?.();
    hasFocusSpy.mockReturnValue(true);
    await wire();

    emit('vitals:update', vitals('P001', 'critical'));

    expect(chimes()).toBe(0);
    expect(notifications).toHaveLength(0);
  });

  it('honours the persisted mute flag for audio but still notifies', async () => {
    teardown?.();
    localStorage.setItem('orb-alert-sound', 'muted');
    await wire();

    emit('vitals:update', vitals('P001', 'critical'));

    expect(chimes()).toBe(0);
    expect(notifications).toHaveLength(1);
  });

  it('deduplicates a repeated Relay alert with the same message id', () => {
    const msg = {
      id: 'm-42',
      message_type: 'alert',
      content: JSON.stringify({ patient_name: 'A. Rao', action_required: 'Review ABG' }),
    };
    emit('message:new', msg);
    vi.advanceTimersByTime(5_000);
    emit('message:new', msg); // socket replay / reconnect redelivery

    expect(notifications).toHaveLength(1);
  });
});

/** Mirrors the module constant; kept local so the test states its own expectation. */
const TAG_COOLDOWN_MS = 120_000;

/**
 * What the deterioration notification SAYS. It used to append
 * `(${Math.round(risk.score * 100)}% risk)`, and `risk.score` is NEWS2 / 20, a
 * display scale, not a probability: a NEWS2 13 patient was announced as "65%
 * risk". The server stopped printing that figure in Relay for the same reason
 * (bot_service: "NEWS2 is an ordinal escalation trigger, NOT a calibrated
 * probability"). The notification names the aggregate instead.
 */
describe('criticalAlerts: the deterioration notification text', () => {
  it('names the NEWS2 aggregate and prints no percentage', () => {
    emit('vitals:update', {
      patient_id: 'P009', patient_name: 'Patient P009',
      risk: { level: 'critical', score: 0.65, raw_news2: 13 },
    });
    expect(notifications).toHaveLength(1);
    expect(notifications[0].body).toContain('NEWS2 13');
    expect(notifications[0].body).not.toMatch(/%/);
  });

  it('prints no number at all when the payload carries no aggregate', () => {
    emit('vitals:update', {
      patient_id: 'P010', patient_name: 'Patient P010',
      risk: { level: 'critical', score: 0.65 },
    });
    expect(notifications).toHaveLength(1);
    expect(notifications[0].body).toBe('Patient P010 has entered CRITICAL status.');
  });
});

/**
 * A set the ward clerk copied from the paper sheet more than 2 hours after it
 * was taken raises no alert (nurse_shift_service.transcribed_record_only). The
 * server still pushes it to an open chart when it is the newest set, marked
 * `live_hooks: false`, and this module must not chime for it either.
 */
describe('criticalAlerts: a reading copied hours after it was taken', () => {
  it('neither chimes nor notifies for a critical set marked live_hooks false', () => {
    emit('vitals:update', { ...vitals('P011', 'critical'), live_hooks: false });
    expect(chimes()).toBe(0);
    expect(notifications).toHaveLength(0);
  });

  it('does not count it as the patient being critical already', () => {
    emit('vitals:update', { ...vitals('P012', 'critical'), live_hooks: false });
    // A live critical set after it is still news, and is announced.
    emit('vitals:update', vitals('P012', 'critical'));
    expect(notifications).toHaveLength(1);
  });
});
