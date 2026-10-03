/**
 * Thinking mode — client store and answer labelling.
 *
 * The server owns the safety floor; these tests own the two things the client
 * can get wrong on its own: defaulting the toggle to ON, and describing an
 * answer's provenance in words a clinician can act on.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import {
  describeAnswerProvenance,
  getThinkingMode,
  readStoredThinkingMode,
  setThinkingMode,
  subscribeThinkingMode,
  toggleThinkingMode,
  type SageProvenance,
} from './thinkingMode';

beforeEach(() => {
  localStorage.clear();
  setThinkingMode(true);
});

describe('the toggle defaults to ON', () => {
  it('is on with nothing stored', () => {
    localStorage.clear();
    expect(readStoredThinkingMode()).toBe(true);
  });

  it.each(['perhaps', '', 'OFF', 'false', '0', 'null'])(
    'is on when the stored value is %o — only the exact string "off" counts',
    (stored) => {
      localStorage.setItem('orb_sage_thinking', stored);
      expect(readStoredThinkingMode()).toBe(true);
    },
  );

  it('honours an explicit stored off', () => {
    localStorage.setItem('orb_sage_thinking', 'off');
    expect(readStoredThinkingMode()).toBe(false);
  });

  it('is on when storage itself throws (private mode)', () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = () => {
      throw new Error('denied');
    };
    try {
      expect(readStoredThinkingMode()).toBe(true);
    } finally {
      Storage.prototype.getItem = original;
    }
  });
});

describe('the store', () => {
  it('toggles, persists and notifies', () => {
    const seen: boolean[] = [];
    const unsubscribe = subscribeThinkingMode((on) => seen.push(on));

    expect(toggleThinkingMode()).toBe(false);
    expect(getThinkingMode()).toBe(false);
    expect(localStorage.getItem('orb_sage_thinking')).toBe('off');

    expect(toggleThinkingMode()).toBe(true);
    expect(localStorage.getItem('orb_sage_thinking')).toBe('on');

    unsubscribe();
    setThinkingMode(false);
    expect(seen).toEqual([false, true]);
  });
});

describe('describeAnswerProvenance', () => {
  it('says nothing for an ordinary careful answer', () => {
    const p: SageProvenance = { thinking: true, thinking_tier: 'careful', safety_floor: false };
    expect(describeAnswerProvenance(p)).toBeNull();
  });

  it('says nothing when the server sent no provenance at all (older server)', () => {
    expect(describeAnswerProvenance(undefined)).toBeNull();
    expect(describeAnswerProvenance({})).toBeNull();
  });

  it('names the fast model on a fast answer', () => {
    const notice = describeAnswerProvenance({
      thinking: false, thinking_tier: 'fast', model: 'medgemma:4b', safety_floor: false,
    });
    expect(notice?.kind).toBe('fast');
    expect(notice?.text).toContain('medgemma:4b');
    expect(notice?.text.toLowerCase()).toContain('verify');
  });

  it('explains a floor override in the clinician\'s terms, not the rule name', () => {
    const notice = describeAnswerProvenance({
      thinking: true, thinking_requested: false, thinking_tier: 'careful',
      model: 'medgemma:27b',
      safety_floor: true, safety_floor_rules: ['dosing_or_medication'],
    });
    expect(notice?.kind).toBe('floor');
    expect(notice?.text).toContain('careful model');
    expect(notice?.text).toContain('medication or a dose');
    expect(notice?.text).not.toContain('dosing_or_medication');
  });

  it('joins multiple floor reasons readably', () => {
    const notice = describeAnswerProvenance({
      safety_floor: true,
      safety_floor_rules: ['patient_specific', 'dosing_or_medication'],
      model: 'medgemma:27b',
    });
    expect(notice?.text).toContain('about a specific patient and it involves a medication');
  });

  it('escalates to a warning when the floor could NOT be honoured', () => {
    const notice = describeAnswerProvenance({
      safety_floor: true, safety_floor_rules: ['patient_specific'],
      safety_floor_unsatisfied: true,
      model: 'medgemma:4b', preferred_model: 'medgemma:27b',
    });
    expect(notice?.kind).toBe('unsatisfied');
    expect(notice?.text).toContain('medgemma:27b');
    expect(notice?.text).toContain('medgemma:4b');
    expect(notice?.text).toContain('Verify before acting');
  });

  it('gives the work-request reason (floor rule 4) in words', () => {
    const notice = describeAnswerProvenance({
      safety_floor: true, safety_floor_rules: ['work_request_imperative'], model: 'medgemma:27b',
    });
    expect(notice?.text).toContain('because it asks for hospital work to be arranged');
    expect(notice?.text).not.toContain('work_request_imperative');
  });

  it('an unknown rule name degrades to a bare sentence rather than leaking it', () => {
    const notice = describeAnswerProvenance({
      safety_floor: true, safety_floor_rules: ['some_future_rule'], model: 'medgemma:27b',
    });
    expect(notice?.kind).toBe('floor');
    expect(notice?.text).not.toContain('some_future_rule');
  });
});
