/**
 * Orb — Critical Alerts Service
 *
 * Makes clinically-critical realtime events perceptible when the clinician is on
 * another tab or another window. When a critical event arrives and the Orb tab is
 * hidden / unfocused, we (a) raise a browser Notification (if permitted), (b) play
 * a short synthesized chime (WebAudio, mutable), and (c) flash document.title until
 * the tab regains focus.
 *
 * Deliberately a plain module (no React) so it can be initialized once from the app
 * shell. `initCriticalAlerts()` wires up the socket listeners and returns a teardown
 * function that removes every listener, timer, and audio resource it created.
 *
 * PHI note: notifications only surface the same identifiers the in-app alert already
 * shows (patient name / MRN / reason) — no additional PHI is introduced here.
 */

import { socket, type Message } from '../api/client';

/* -------------------------------------------------------------------------- */
/*  Sound mute flag (persisted)                                               */
/* -------------------------------------------------------------------------- */

const SOUND_KEY = 'orb-alert-sound';

/** True when the chime is muted. Persisted across sessions in localStorage. */
export function isAlertSoundMuted(): boolean {
  try {
    return localStorage.getItem(SOUND_KEY) === 'muted';
  } catch {
    return false;
  }
}

/** Set the persisted mute flag. Returns the new muted state. */
export function setAlertSoundMuted(muted: boolean): boolean {
  try {
    localStorage.setItem(SOUND_KEY, muted ? 'muted' : 'on');
  } catch {
    /* localStorage may be unavailable (private mode); mute is best-effort */
  }
  return muted;
}

/** Toggle the persisted mute flag. Returns the new muted state. */
export function toggleAlertSoundMuted(): boolean {
  return setAlertSoundMuted(!isAlertSoundMuted());
}

/* -------------------------------------------------------------------------- */
/*  WebAudio chime                                                            */
/* -------------------------------------------------------------------------- */

let audioCtx: AudioContext | null = null;

/** Safari before 14.1 exposes the constructor only under its prefixed name. */
type WindowWithWebkitAudio = Window & typeof globalThis & {
  webkitAudioContext?: typeof AudioContext;
};

function getAudioContext(): AudioContext | null {
  const w = window as WindowWithWebkitAudio;
  const Ctor = w.AudioContext || w.webkitAudioContext;
  if (!Ctor) return null;
  if (!audioCtx) {
    try {
      audioCtx = new Ctor();
    } catch {
      return null;
    }
  }
  return audioCtx;
}

/**
 * Play a short two-tone chime via an oscillator envelope. No audio asset needed.
 * Respects the persisted mute flag and fails silently on unsupported browsers.
 */
function playChime(): void {
  if (isAlertSoundMuted()) return;
  const ctx = getAudioContext();
  if (!ctx) return;

  // Autoplay policies can leave the context suspended until a user gesture.
  if (ctx.state === 'suspended') {
    ctx.resume().catch(() => {});
  }

  const now = ctx.currentTime;
  // Two rising notes for an "attention" feel.
  const tones = [
    { freq: 880, start: 0, dur: 0.16 },
    { freq: 1180, start: 0.14, dur: 0.22 },
  ];

  for (const t of tones) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(t.freq, now + t.start);

    const peak = 0.14; // keep it gentle
    gain.gain.setValueAtTime(0.0001, now + t.start);
    gain.gain.exponentialRampToValueAtTime(peak, now + t.start + 0.02);
    gain.gain.exponentialRampToValueAtTime(
      0.0001,
      now + t.start + t.dur,
    );

    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now + t.start);
    osc.stop(now + t.start + t.dur + 0.02);
  }
}

/* -------------------------------------------------------------------------- */
/*  Title flashing                                                            */
/* -------------------------------------------------------------------------- */

let originalTitle: string | null = null;
let flashTimer: ReturnType<typeof setInterval> | null = null;
let flashOn = false;
let pendingCount = 0;
let focusListenerAttached = false;

function alertTitle(): string {
  return `⚠ ${pendingCount} critical alert${pendingCount === 1 ? '' : 's'} (Orb)`;
}

function stopTitleFlash(): void {
  if (flashTimer) {
    clearInterval(flashTimer);
    flashTimer = null;
  }
  if (originalTitle !== null) {
    document.title = originalTitle;
    originalTitle = null;
  }
  flashOn = false;
  pendingCount = 0;
}

function handleRegainFocus(): void {
  stopTitleFlash();
  window.removeEventListener('focus', handleRegainFocus);
  focusListenerAttached = false;
}

/** Start (or continue) flashing the tab title until the window regains focus. */
function flashTitle(): void {
  pendingCount += 1;

  if (originalTitle === null) {
    originalTitle = document.title;
  }

  if (!flashTimer) {
    flashOn = false;
    flashTimer = setInterval(() => {
      flashOn = !flashOn;
      document.title = flashOn ? alertTitle() : originalTitle ?? 'Orb';
    }, 1000);
  }

  if (!focusListenerAttached) {
    window.addEventListener('focus', handleRegainFocus);
    focusListenerAttached = true;
  }
}

/* -------------------------------------------------------------------------- */
/*  Notifications                                                             */
/* -------------------------------------------------------------------------- */

function notificationsSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

function showNotification(title: string, body: string, tag: string): void {
  if (!notificationsSupported()) return;
  if (Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title, {
      body,
      tag, // dedupe: a repeat with the same tag replaces the prior toast
      // `renotify` re-alerts on a same-tag replacement; not in lib.dom's
      // NotificationOptions type, so widen the options object to pass it through.
      ...({ renotify: true } as Record<string, unknown>),
    } as NotificationOptions);
    n.onclick = () => {
      try {
        window.focus();
      } catch {
        /* no-op */
      }
      n.close();
    };
  } catch {
    /* Notification constructor can throw on some platforms; ignore */
  }
}

/* -------------------------------------------------------------------------- */
/*  Re-alert cooldown                                                         */
/* -------------------------------------------------------------------------- */

/**
 * The vitals source emits `vitals:update` for EVERY admitted patient every 5
 * seconds, and a critical patient stays critical between ticks — that is the
 * intended clinical behaviour, not a fault. What was faulty is that this module
 * treated each tick as a NEW alert: one ICU patient in steady-state critical
 * re-fired the chime, re-alerted the OS notification (same `tag` + `renotify`)
 * and bumped the title counter twelve times a minute, indefinitely, for as long
 * as the tab was unfocused. With several critical patients it is heard as a
 * continuous pulse.
 *
 * The server already refuses to page a steady-state critical patient — it pages
 * on escalation or a rising NEWS2, behind a per-patient cooldown
 * (`bot_service.ALERT_COOLDOWN_SECONDS`). But that gate sits on the Relay alert
 * bridge; the raw vitals stream this module listens to carries no gate at all,
 * so the equivalent gate has to live here.
 */
const TAG_COOLDOWN_MS = 120_000; // mirrors server ALERT_COOLDOWN_SECONDS (120s)

/**
 * Separate, much shorter floor on the AUDIO specifically. Distinct patients are
 * distinct alerts and each must be surfaced, but N patients crossing into
 * critical on the same tick would otherwise start N overlapping oscillators —
 * which is heard as one loud blare, not as N alerts.
 */
const MIN_CHIME_GAP_MS = 2_000;

/** Sweep the cooldown map only once it is big enough to be worth sweeping. */
const TAG_PRUNE_THRESHOLD = 256;

const lastFiredByTag = new Map<string, number>();
let lastChimeAt = 0;

function pruneTagCooldowns(now: number): void {
  if (lastFiredByTag.size < TAG_PRUNE_THRESHOLD) return;
  for (const [tag, ts] of lastFiredByTag) {
    if (now - ts >= TAG_COOLDOWN_MS) lastFiredByTag.delete(tag);
  }
}

/* -------------------------------------------------------------------------- */
/*  Critical event handling                                                   */
/* -------------------------------------------------------------------------- */

interface CriticalAlert {
  title: string;
  body: string;
  tag: string;
}

/** True when the operator is not actively looking at this tab. */
function isTabInattentive(): boolean {
  if (document.hidden) return true;
  if (typeof document.hasFocus === 'function' && !document.hasFocus()) {
    return true;
  }
  return false;
}

function fireAlert(alert: CriticalAlert): void {
  if (!isTabInattentive()) return; // in-app UI already surfaces it

  const now = Date.now();

  // Suppress a repeat of the SAME alert inside the cooldown window entirely —
  // not just the chime. A suppressed repeat must not re-alert the OS
  // notification or inflate the title counter either, or the tab still reads
  // "⚠ 412 critical alerts" for what is one patient.
  const lastForTag = lastFiredByTag.get(alert.tag);
  if (lastForTag !== undefined && now - lastForTag < TAG_COOLDOWN_MS) return;
  lastFiredByTag.set(alert.tag, now);
  pruneTagCooldowns(now);

  showNotification(alert.title, alert.body, alert.tag);
  if (now - lastChimeAt >= MIN_CHIME_GAP_MS) {
    lastChimeAt = now;
    playChime();
  }
  flashTitle();
}

/* ---- per-event adapters (map raw payloads → terse clinical text) ---------- */

/* The raw socket payloads, as far as these adapters read them. They arrive
   unchecked from the socket, so every field is optional and every read above
   and below is guarded. */

/** `surgery:emergency_intake` (sage_handoff_service). */
interface EmergencyIntakePayload {
  surgery_id?: string;
}

/** The JSON a Relay alert message carries in `content`. */
interface RelayAlertContent {
  patient_name?: string;
  mrn?: string;
  action_required?: string;
  title?: string;
  text?: string;
}

/** `vitals:update` (ws/realtime.vitals_update_payload and its twins).
 *  `patient_name` is added by ws/realtime.emit_patient_vitals for every lane
 *  (since 4 October 2026; before that the alert read the raw id). It is absent
 *  only when the server cannot find the patient, and the id stands in then. */
interface VitalsAlertPayload {
  patient_id?: string;
  patient_name?: string;
  risk?: { level?: string; raw_news2?: unknown } | null;
  /** false on a set the ward clerk copied from a paper sheet more than the
   *  live alert limit after it was taken (nurse_shift_service
   *  transcribed_record_only, 2 hours by default). It is charted and shown,
   *  but it raises no alert anywhere, so it raises none here either. Absent on
   *  every other push. */
  live_hooks?: boolean;
}

function onEmergencyIntake(data: EmergencyIntakePayload | null | undefined): void {
  const surgeryId = data?.surgery_id ?? '';
  fireAlert({
    title: '⚠ Emergency surgery intake',
    body: 'A new emergency case is waiting in the Surgical Suite.',
    tag: surgeryId ? `orb-surgery-${surgeryId}` : 'orb-surgery-emergency',
  });
}

function onRelayMessage(msg: Partial<Message> | null | undefined): void {
  const type = msg?.message_type;
  if (type !== 'alert' && type !== 'surgical_alert') return; // only critical subset

  let payload: RelayAlertContent | null = null;
  try {
    payload = msg?.content ? (JSON.parse(msg.content) as RelayAlertContent | null) : null;
  } catch {
    payload = null;
  }

  const patient =
    payload?.patient_name ||
    (payload?.mrn ? `MRN ${payload.mrn}` : '') ||
    '';
  const reason =
    payload?.action_required ||
    payload?.title ||
    payload?.text ||
    'Critical clinical alert';

  const who = patient ? `${patient}: ` : '';
  fireAlert({
    title: '⚠ Critical clinical alert',
    body: `${who}${reason}`,
    // Dedupe per message so repeats of the same alert collapse.
    tag: msg?.id ? `orb-relay-${msg.id}` : 'orb-relay-alert',
  });
}

/**
 * Last risk band seen per patient, so a CONTINUING critical can be told apart
 * from an escalation INTO critical. Only the escalation is an alert.
 */
const lastLevelByPatient = new Map<string, string>();

function onVitalsUpdate(vital: VitalsAlertPayload | null | undefined): void {
  // A reading copied hours after it was taken: no chime, no notification, and
  // the band it carries is not remembered as this patient's live state.
  if (vital?.live_hooks === false) return;

  const level: string | undefined = vital?.risk?.level;
  const pid: string | undefined = vital?.patient_id;

  const prevLevel = pid ? lastLevelByPatient.get(pid) : undefined;
  if (pid) lastLevelByPatient.set(pid, level ?? 'stable');

  if (level !== 'critical') return; // only the top tier deteriorations

  // A patient who was critical on the previous tick and is still critical now
  // is not a new event — the ward has already been told, the in-app UI is still
  // showing it, and the server has already paged Relay. Paging again every 5s
  // is alert fatigue, not safety. `prevLevel === undefined` (first reading since
  // this tab connected) still fires: the operator has no prior context, so
  // "this patient is critical right now" is news to them.
  if (prevLevel === 'critical') return;

  const patient = vital?.patient_name || vital?.patient_id || 'A patient';
  // The NEWS2 aggregate, never `risk.score` as a percentage. `score` is NEWS2 / 20,
  // a display scale and not a probability, so NEWS2 13 used to be announced as
  // "65% risk". The server dropped that figure from Relay for the same reason.
  const raw = vital?.risk?.raw_news2;
  const aggregate = typeof raw === 'number' ? ` (NEWS2 ${raw})` : '';

  fireAlert({
    title: '⚠ Patient deterioration',
    body: `${patient} has entered CRITICAL status${aggregate}.`,
    // Dedupe per patient so a stream of critical readings is not a toast storm.
    tag: vital?.patient_id
      ? `orb-vitals-${vital.patient_id}`
      : 'orb-vitals-critical',
  });
}

/* -------------------------------------------------------------------------- */
/*  Public entry point                                                        */
/* -------------------------------------------------------------------------- */

/**
 * Wire up critical-alert listeners on the shared socket.
 * @returns teardown function removing all listeners, timers, and title state.
 */
export function initCriticalAlerts(): () => void {
  socket.on('surgery:emergency_intake', onEmergencyIntake);
  socket.on('message:new', onRelayMessage);
  socket.on('vitals:update', onVitalsUpdate);

  return () => {
    socket.off('surgery:emergency_intake', onEmergencyIntake);
    socket.off('message:new', onRelayMessage);
    socket.off('vitals:update', onVitalsUpdate);

    if (focusListenerAttached) {
      window.removeEventListener('focus', handleRegainFocus);
      focusListenerAttached = false;
    }
    stopTitleFlash();

    // Dedupe state is scoped to a wired session. It is deliberately NOT cleared
    // on window focus: an alt-tab away and back would otherwise re-arm a full
    // burst of chimes for every still-critical patient on the next tick.
    lastFiredByTag.clear();
    lastLevelByPatient.clear();
    lastChimeAt = 0;
  };
}
