/**
 * The header chip says Sage's state in words for every role. It used to show
 * "27B · 4B" to everyone with the state only as a dot colour; the model tags
 * now live in the administrator's popover.
 */
import { describe, expect, it } from 'vitest';

import { sageStatusText } from './sageStatus';

const model = (warm: boolean) => ({ model: warm ? 'medgemma:27b' : 'medgemma:4b', role: 'primary' as const, warm });

describe('sageStatusText', () => {
  it('reads ready when every tier is loaded', () => {
    expect(sageStatusText({ ollama_reachable: true, models: [model(true), model(true)] })).toBe('Sage ready');
  });

  it('reads warming up when a tier still has to load', () => {
    expect(sageStatusText({ ollama_reachable: true, models: [model(true), model(false)] })).toBe('Sage warming up');
  });

  it('reads limited, not warming up, when the only cold tier was unloaded by an administrator', () => {
    // Nothing reloads a held tier in the background, so "warming up" would never come true.
    const held = { ...model(false), held: true };
    expect(sageStatusText({ ollama_reachable: true, models: [model(true), held] })).toBe('Sage limited');
  });

  it('still reads warming up while any cold tier is not held', () => {
    const held = { ...model(false), held: true };
    expect(sageStatusText({ ollama_reachable: true, models: [held, model(false)] })).toBe('Sage warming up');
  });

  it('reads offline when the engine cannot be reached', () => {
    expect(sageStatusText({ ollama_reachable: false, models: [] })).toBe('Sage offline');
  });

  it('never names a model tag', () => {
    const text = sageStatusText({ ollama_reachable: true, models: [model(true)] });
    expect(text).not.toMatch(/27B|4B|medgemma/i);
  });
});
