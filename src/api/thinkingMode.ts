/**
 * THINKING MODE — the client half of Sage's effort switch.
 *
 * The toggle lives in the composer but its value is needed in `querySage`, two
 * components away and outside React's tree. Rather than thread a prop through
 * SagePanel, the flag lives here as a tiny observable store: the composer reads
 * and writes it, the API client reads it at send time, and it persists across
 * reloads so a clinician who chose fast mode does not silently get it reset.
 *
 * THE DEFAULT IS ON, and it is on in three independent places — here, in the
 * request body, and in the server's `resolve_thinking_flag`. A clinician gets
 * the fast model only by asking for it, and only for questions the server's
 * safety floor allows (services/thinking_mode.py). Nothing here is a security
 * boundary: the server never trusts this value beyond "the clinician would
 * prefer speed", and the floor is enforced there, not here.
 */

const STORAGE_KEY = 'orb_sage_thinking';

/** Which brain answered. Mirrors `route_info` from services/thinking_mode.stamp. */
export type ThinkingTier = 'careful' | 'fast';

/**
 * The `metadata.provider` object the Sage stream ships with every answer.
 * `lane`/`provider`/`model`/`downgraded`/`preferred_model` come from
 * `query_ollama`; the `thinking_*` / `safety_floor_*` fields come from
 * `thinking_mode.stamp`. All optional — an older server sends neither.
 */
export interface SageProvenance {
  lane?: string;
  provider?: string;
  /** The model that actually generated the answer. */
  model?: string;
  /** True when the residency router substituted a smaller tier. */
  downgraded?: boolean;
  preferred_model?: string;

  /** Did this turn get the careful model's effort? */
  thinking?: boolean;
  /** What the clinician's toggle asked for. */
  thinking_requested?: boolean;
  thinking_tier?: ThinkingTier;
  thinking_model?: string;
  thinking_available?: boolean;
  thinking_reason?: string;

  /** The floor overrode a fast request. */
  safety_floor?: boolean;
  safety_floor_rules?: string[];
  safety_floor_enabled?: boolean;
  /** The floor demanded the careful model and the appliance could not supply it. */
  safety_floor_unsatisfied?: boolean;
}

type Listener = (on: boolean) => void;

const listeners = new Set<Listener>();

/**
 * The persisted preference, defaulted. Exported so the defaulting rule can be
 * tested directly rather than by re-importing the module under a cache-busting
 * specifier — a trick that works at runtime and does not typecheck.
 *
 * Only the exact string 'off' turns thinking off. A missing key, a cleared
 * profile, a corrupted value, a storage exception, a different app version —
 * all resolve to ON, matching `resolve_thinking_flag` on the server.
 */
export function readStoredThinkingMode(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== 'off';
  } catch {
    return true;
  }
}

let thinkingOn = readStoredThinkingMode();

/** Is thinking mode on for the next question? Default true. */
export function getThinkingMode(): boolean {
  return thinkingOn;
}

export function setThinkingMode(on: boolean): void {
  thinkingOn = !!on;
  try {
    localStorage.setItem(STORAGE_KEY, thinkingOn ? 'on' : 'off');
  } catch {
    /* private mode / quota — the in-memory value still governs this session */
  }
  listeners.forEach((fn) => fn(thinkingOn));
}

export function toggleThinkingMode(): boolean {
  setThinkingMode(!thinkingOn);
  return thinkingOn;
}

/** Subscribe to changes. Returns an unsubscribe function. */
export function subscribeThinkingMode(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

// ── Reading the answer's provenance ─────────────────────────────────────────

/** Server rule name -> the clause a clinician reads. Mirrors `_FLOOR_RULES`. */
const FLOOR_RULE_TEXT: Record<string, string> = {
  patient_specific: 'it is about a specific patient',
  dosing_or_medication: 'it involves a medication or a dose',
  orb_computed_score: 'it is about a score Orb computes itself',
  // Rule 4. Without this line the floor fired with no reason given, so the
  // clinician could not tell why fast mode was overridden.
  work_request_imperative: 'it asks for hospital work to be arranged',
};

export interface AnswerProvenanceNotice {
  /** Drives the banner styling: 'fast' is informational, 'floor' is reassuring,
   *  'unsatisfied' is a warning the clinician must not miss. */
  kind: 'fast' | 'floor' | 'unsatisfied';
  icon: string;
  text: string;
}

/**
 * What (if anything) to tell the clinician about which model answered.
 *
 * Returns null for the ordinary case — thinking on, careful model, nothing to
 * report. Every other case produces a line, because the whole point of the
 * feature is that a clinician is never unsure which brain answered.
 *
 * The existing residency-downgrade banner in SageMessage is deliberately left
 * alone: it answers a different question ("the 27B is not loaded") and both can
 * be true at once.
 */
export function describeAnswerProvenance(
  p: SageProvenance | null | undefined,
): AnswerProvenanceNotice | null {
  if (!p) return null;

  // Worst case first: the floor said this needed the careful model, and the
  // appliance served something else anyway.
  if (p.safety_floor && p.safety_floor_unsatisfied) {
    return {
      kind: 'unsatisfied',
      icon: '⚠️',
      text:
        `This question needed the careful model, but ${p.preferred_model ?? 'it'} was not ` +
        `available, answered by ${p.model ?? 'a smaller model'}. Verify before acting.`,
    };
  }

  if (p.safety_floor) {
    const rules = (p.safety_floor_rules ?? [])
      .map((r) => FLOOR_RULE_TEXT[r])
      .filter(Boolean);
    const because =
      rules.length === 0
        ? ''
        : rules.length === 1
          ? ` because ${rules[0]}`
          : ` because ${rules.slice(0, -1).join(', ')} and ${rules[rules.length - 1]}`;
    return {
      kind: 'floor',
      icon: '🛡️',
      text: `Fast mode is on, but this one needed the careful model (${p.model ?? 'careful'})${because}.`,
    };
  }

  if (p.thinking_tier === 'fast') {
    return {
      kind: 'fast',
      icon: '⚡',
      text: `Fast answer from the small model (${p.model ?? 'fast'}): lower accuracy. Verify anything you would act on.`,
    };
  }

  return null;
}
