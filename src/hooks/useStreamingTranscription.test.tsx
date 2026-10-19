import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useStreamingTranscription } from './useStreamingTranscription';

const mocks = vi.hoisted(() => ({
  handlers: new Map<string, (data: never) => void>(),
  emit: vi.fn(),
}));
vi.mock('../api/client', () => ({ socket: {
  connected: true,
  on: vi.fn((name, fn) => mocks.handlers.set(name, fn)),
  off: vi.fn((name) => mocks.handlers.delete(name)),
  emit: mocks.emit,
  timeout: () => ({ emit: mocks.emit }),
} }));
let trackStop: ReturnType<typeof vi.fn>;
let permission: ReturnType<typeof vi.fn>;
let contexts: MockContext[];
let processors: MockProcessor[];
class MockContext {
  state = 'suspended'; sampleRate = 16000; destination = {};
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
  source = { connect: vi.fn(), disconnect: vi.fn() };
  constructor() { contexts.push(this); }
  createMediaStreamSource() { return this.source; }
  suspend = vi.fn().mockResolvedValue(undefined);
  resume = vi.fn().mockResolvedValue(undefined);
  close = vi.fn().mockImplementation(async () => { this.state = 'closed'; });
}
class MockProcessor {
  port = { onmessage: null as null | ((event: MessageEvent) => void), postMessage: vi.fn(), close: vi.fn() };
  connect = vi.fn(); disconnect = vi.fn();
  constructor() { processors.push(this); }
}
function event(name: string, data: object) { act(() => { mocks.handlers.get(name)?.(data as never); }); }
function id() { return mocks.emit.mock.calls.find(x => x[0] === 'scribe:start_session')?.[1].stream_id as string; }
async function begin(result: { current: ReturnType<typeof useStreamingTranscription> }) {
  await act(async () => { await result.current.startStreaming(); });
  event('scribe:status', { status: 'ready', stream_id: id() });
}

beforeEach(() => {
  vi.useFakeTimers();
  mocks.emit.mockReset(); mocks.handlers.clear(); contexts = []; processors = [];
  trackStop = vi.fn();
  permission = vi.fn().mockResolvedValue({ getTracks: () => [{ stop: trackStop }] });
  vi.stubGlobal('AudioContext', MockContext);
  vi.stubGlobal('AudioWorkletNode', MockProcessor);
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: permission } });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('live dictation lifecycle', () => {
  it('waits for server readiness and ignores another stream’s transcript', async () => {
    const final = vi.fn();
    const { result } = renderHook(() => useStreamingTranscription({ onFinalTranscript: final }));
    await act(async () => { await result.current.startStreaming(); });
    expect(result.current.isStreaming).toBe(false);
    expect(contexts[0].resume).not.toHaveBeenCalled();
    event('scribe:status', { status: 'ready', stream_id: id() });
    expect(result.current.isStreaming).toBe(true);
    event('scribe:transcript', { stream_id: 'retired', text: 'private', utterance_index: 0 });
    expect(final).not.toHaveBeenCalled();
    event('scribe:transcript', { stream_id: id(), text: 'synthetic', utterance_index: 0 });
    expect(result.current.fullTranscript).toBe('synthetic');
  });
  it('releases permission granted after unmount without opening audio or a server session', async () => {
    let grant!: (stream: object) => void;
    permission.mockReturnValue(new Promise(resolve => { grant = resolve; }));
    const { result, unmount } = renderHook(() => useStreamingTranscription());
    let pending!: Promise<void>;
    act(() => { pending = result.current.startStreaming(); });
    unmount();
    await act(async () => { grant({ getTracks: () => [{ stop: trackStop }] }); await pending; });
    expect(trackStop).toHaveBeenCalledTimes(1);
    expect(contexts).toHaveLength(0);
    expect(mocks.emit).not.toHaveBeenCalled();
  });
  it('cancels pending capture when the patient changes', async () => {
    let grant!: (stream: object) => void;
    permission.mockReturnValue(new Promise(resolve => { grant = resolve; }));
    const { result, rerender } = renderHook(({ patientId }) => useStreamingTranscription({ patientId }), { initialProps: { patientId: 'A' } });
    let pending!: Promise<void>;
    act(() => { pending = result.current.startStreaming(); });
    rerender({ patientId: 'B' });
    await act(async () => { grant({ getTracks: () => [{ stop: trackStop }] }); await pending; });
    expect(trackStop).toHaveBeenCalled();
    expect(mocks.emit).not.toHaveBeenCalled();
  });
  it('waits for audio receipt before credit and the utterance boundary', async () => {
    const { result } = renderHook(() => useStreamingTranscription());
    await begin(result);
    const port = processors[0].port;
    act(() => { port.onmessage?.({ data: { audio: new ArrayBuffer(16), end: true } } as MessageEvent); });
    expect(port.postMessage).not.toHaveBeenCalled();
    const call = mocks.emit.mock.calls.find(x => x[0] === 'scribe:audio_chunk')!;
    act(() => { call[2](null); });
    expect(mocks.emit).toHaveBeenCalledWith('scribe:end_utterance');
    expect(port.postMessage).toHaveBeenCalledWith('ack');
  });
  it('keeps final flushed text after stop and then permits another session', async () => {
    const { result } = renderHook(() => useStreamingTranscription());
    await begin(result);
    const firstId = id();
    act(() => result.current.stopStreaming());
    expect(trackStop).toHaveBeenCalled();
    expect(contexts[0].close).not.toHaveBeenCalled();
    act(() => { processors[0].port.onmessage?.({ data: { finished: true, end: true } } as MessageEvent); });
    expect(contexts[0].close).toHaveBeenCalled();
    event('scribe:transcript', { stream_id: firstId, text: 'final words', utterance_index: 0 });
    expect(result.current.fullTranscript).toBe('final words');
    event('scribe:status', { stream_id: firstId, status: 'stopped' });
    await act(async () => { await result.current.startStreaming(); });
    expect(contexts).toHaveLength(2);
  });
  it('stops the microphone on timeout, server rejection, and disconnection', async () => {
    const error = vi.fn();
    const { result } = renderHook(() => useStreamingTranscription({ onError: error }));
    await act(async () => { await result.current.startStreaming(); });
    act(() => vi.advanceTimersByTime(10000));
    expect(trackStop).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledWith(expect.stringContaining('did not become ready'));
    await begin(result);
    event('disconnect', {});
    expect(trackStop).toHaveBeenCalledTimes(2);
    expect(result.current.isStreaming).toBe(false);
  });
});
