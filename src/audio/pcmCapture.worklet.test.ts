// @vitest-environment node
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

interface Packet { audio?: ArrayBuffer; end?: boolean; speaking?: boolean; error?: string; finished?: boolean }
interface Processor {
  port: { onmessage: (event: { data: string }) => void };
  pending: Packet[];
  candidate: Int16Array[];
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean;
}
function capture(config = {}, rate = 16000) {
  const packets: Packet[] = [];
  let Constructor: new (options: object) => Processor;
  class Base {
    port = {
      onmessage: (_event: { data: string }) => {},
      postMessage: (message: Packet) => { packets.push(structuredClone(message)); },
    };
  }
  runInNewContext(readFileSync(new URL('./pcmCapture.worklet.js', import.meta.url), 'utf8'), {
    AudioWorkletProcessor: Base, sampleRate: rate,
    registerProcessor: (_name: string, constructor: typeof Constructor) => { Constructor = constructor; },
  });
  const processor = new Constructor!({ processorOptions: config });
  function frame(level = 0.2, ack = true) {
    const output = new Float32Array(4096).fill(1);
    const alive = processor.process([[new Float32Array(4096).fill(level)]], [[output]]);
    expect(output.every(value => value === 0)).toBe(true);
    if (ack) processor.port.onmessage({ data: 'ack' });
    return alive;
  }
  return { packets, processor, frame };
}

describe('audio-thread PCM/VAD capture', () => {
  it('retains the beginning of confirmed speech and ends on sample-based silence', () => {
    const { packets, frame } = capture({ minSpeechMs: 500 });
    frame(); frame();
    expect(packets.filter(x => x.audio)).toHaveLength(1);
    expect(new Int16Array(packets[1].audio!)).toHaveLength(8192);
    expect(new Int16Array(packets[1].audio!)[0]).toBe(6553);
    frame(0); frame(0);
    expect(packets.at(-1)).toMatchObject({ end: true, speaking: false });
  });
  it('discards a short noise candidate and emits no silent audio', () => {
    const { packets, frame, processor } = capture({ minSpeechMs: 500 });
    frame(); frame(0); frame(0);
    expect(packets.some(x => x.audio)).toBe(false);
    expect(processor.candidate).toHaveLength(0);
  });
  it('bounds stalled message delivery and reports lost capture explicitly', () => {
    const { packets, frame, processor } = capture();
    for (let i = 0; i < 12; i++) frame(0.2, false);
    expect(processor.pending).toHaveLength(0);
    expect(packets.filter(x => x.audio)).toHaveLength(1);
    expect(packets.at(-1)?.error).toContain('cannot keep up');
    expect(frame()).toBe(false);
  });
  it('splits long continuous speech below the server safety cap', () => {
    const { packets, frame } = capture();
    for (let i = 0; i < 177; i++) frame();
    expect(packets.filter(x => x.end)).toHaveLength(1);
    const firstEnd = packets.findIndex(x => x.end);
    const samples = packets.slice(0, firstEnd + 1).reduce((sum, x) => sum + (x.audio?.byteLength ?? 0) / 2, 0);
    expect(samples).toBeGreaterThanOrEqual(16000 * 45);
    expect(samples).toBeLessThan(16000 * 60);
  });
  it('drains queued PCM before the final stop marker', () => {
    const { packets, frame, processor } = capture();
    frame(0.2, false); frame(0.2, false);
    processor.port.onmessage({ data: 'finish' });
    expect(packets.some(x => x.finished)).toBe(false);
    processor.port.onmessage({ data: 'ack' });
    expect(packets.at(-1)?.audio).toBeTruthy();
    processor.port.onmessage({ data: 'ack' });
    expect(packets.at(-1)).toMatchObject({ finished: true, end: true });
  });
  it('refuses the wrong sample rate and terminates on stop', () => {
    const invalid = capture({}, 48000);
    expect(invalid.packets[0].error).toContain('16 kHz');
    expect(invalid.frame()).toBe(false);
    const valid = capture();
    valid.processor.port.onmessage({ data: 'stop' });
    expect(valid.frame()).toBe(false);
  });
});
