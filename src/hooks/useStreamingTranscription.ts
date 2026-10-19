/** Private local dictation: bounded PCM/VAD capture on the audio thread. */
import { useState, useRef, useCallback, useEffect } from 'react';
import { socket } from '../api/client';

interface StreamingTranscriptionOptions {
  patientId?: string;
  hints?: string;
  language?: string;
  onInterimTranscript?: (text: string, utteranceIndex: number) => void;
  onFinalTranscript?: (text: string, utteranceIndex: number) => void;
  onError?: (error: string) => void;
  onStatusChange?: (status: string) => void;
  vadThreshold?: number;
  silenceMs?: number;
  minSpeechMs?: number;
}
interface StreamingTranscriptionState {
  isStreaming: boolean;
  isProcessing: boolean;
  isSpeaking: boolean;
  currentTranscript: string;
  fullTranscript: string;
  utteranceCount: number;
  audioLevel: number;
}
interface CaptureMessage {
  audio?: ArrayBuffer;
  end?: boolean;
  speaking?: boolean;
  level?: number;
  error?: string;
  finished?: boolean;
}
const initialState: StreamingTranscriptionState = {
  isStreaming: false, isProcessing: false, isSpeaking: false,
  currentTranscript: '', fullTranscript: '', utteranceCount: 0, audioLevel: 0,
};

export function useStreamingTranscription(options: StreamingTranscriptionOptions = {}) {
  const [state, setState] = useState(initialState);
  const optionsRef = useRef(options);
  useEffect(() => { optionsRef.current = options; }, [options]);
  const generationRef = useRef(0);
  const activeIdRef = useRef('');
  const startingRef = useRef(false);
  const mountedRef = useRef(true);
  const contextRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const processorRef = useRef<AudioWorkletNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readyRef = useRef(false);
  const finishingRef = useRef(false);

  const releaseMicrophone = useCallback(() => {
    const processor = processorRef.current;
    if (processor) {
      processor.port.onmessage = null;
      processor.onprocessorerror = null;
      processor.port.postMessage('stop');
      processor.port.close();
      processor.disconnect();
      processorRef.current = null;
    }
    sourceRef.current?.disconnect();
    sourceRef.current = null;
    streamRef.current?.getTracks().forEach(track => track.stop());
    streamRef.current = null;
    const context = contextRef.current;
    contextRef.current = null;
    if (context && context.state !== 'closed') void context.close().catch(() => {});
  }, []);

  const abortStreaming = useCallback(() => {
    generationRef.current += 1;
    startingRef.current = false;
    readyRef.current = false;
    finishingRef.current = false;
    releaseMicrophone();
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    if (activeIdRef.current) socket.emit('scribe:stop_session');
    activeIdRef.current = '';
    if (mountedRef.current) setState(prev => ({ ...prev, isStreaming: false, isProcessing: false, isSpeaking: false, audioLevel: 0 }));
  }, [releaseMicrophone]);

  const fail = useCallback((message: string) => {
    abortStreaming();
    if (mountedRef.current) optionsRef.current.onError?.(message);
  }, [abortStreaming]);

  const stopStreaming = useCallback(() => {
    generationRef.current += 1; // Cancel any pending permission/module request.
    if (finishingRef.current) return;
    const processor = processorRef.current;
    if (!processor || !readyRef.current) { abortStreaming(); return; }
    finishingRef.current = true;
    // Stop the microphone now, then drain already captured PCM before closing
    // the audio thread or asking the server to flush the final utterance.
    sourceRef.current?.disconnect();
    sourceRef.current = null;
    streamRef.current?.getTracks().forEach(track => track.stop());
    streamRef.current = null;
    processor.port.postMessage('finish');
    timerRef.current = setTimeout(() => fail('Audio delivery did not finish. Please retry the last utterance.'), 10000);
    setState(prev => ({ ...prev, isStreaming: false, isSpeaking: false, audioLevel: 0 }));
  }, [abortStreaming, fail]);

  useEffect(() => {
    mountedRef.current = true;
    const matches = (data: { stream_id?: string }) => Boolean(activeIdRef.current && data.stream_id === activeIdRef.current);
    const transcript = (data: { stream_id?: string; text: string; utterance_index: number }) => {
      if (!matches(data) || !data.text) return;
      setState(prev => ({ ...prev, isProcessing: false, currentTranscript: data.text,
        fullTranscript: prev.fullTranscript + (prev.fullTranscript ? ' ' : '') + data.text,
        utteranceCount: data.utterance_index + 1 }));
      optionsRef.current.onFinalTranscript?.(data.text, data.utterance_index);
    };
    const status = (data: { stream_id?: string; status: string }) => {
      if (!matches(data)) return;
      if (data.status === 'ready' && startingRef.current) {
        readyRef.current = true;
        startingRef.current = false;
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = null;
        setState(prev => ({ ...prev, isStreaming: true }));
        void contextRef.current?.resume().catch(() => fail('Audio capture could not start. Please retry.'));
      }
      if (data.status === 'stopped') activeIdRef.current = '';
      setState(prev => ({ ...prev, isProcessing: data.status === 'processing' }));
      optionsRef.current.onStatusChange?.(data.status);
    };
    const error = (data: { stream_id?: string; error: string; retryable?: boolean }) => {
      // Legacy/global errors may arrive before a server session is established.
      if (!matches(data) && !(startingRef.current && !data.stream_id)) return;
      if (!data.retryable) fail(data.error);
      else {
        setState(prev => ({ ...prev, isProcessing: false }));
        optionsRef.current.onError?.(data.error);
      }
    };
    const disconnected = () => { if (activeIdRef.current || startingRef.current) fail('Dictation disconnected. Please reconnect and retry.'); };
    socket.on('scribe:transcript', transcript);
    socket.on('scribe:status', status);
    socket.on('scribe:error', error);
    socket.on('disconnect', disconnected);
    return () => {
      mountedRef.current = false;
      abortStreaming();
      socket.off('scribe:transcript', transcript);
      socket.off('scribe:status', status);
      socket.off('scribe:error', error);
      socket.off('disconnect', disconnected);
    };
  }, [fail, abortStreaming]);

  // A chart/language change cannot route pending speech into the new context.
  useEffect(() => {
    return () => {
      abortStreaming();
    };
  }, [options.patientId, options.language, abortStreaming]);

  const startStreaming = useCallback(async () => {
    if (startingRef.current || contextRef.current) return;
    if (!socket.connected) { optionsRef.current.onError?.('Dictation is disconnected. Please reconnect first.'); return; }
    if (activeIdRef.current) {
      optionsRef.current.onError?.('The last utterance is still finishing. Please wait.');
      return;
    }
    const generation = ++generationRef.current;
    startingRef.current = true;
    const cancelled = () => !mountedRef.current || generation !== generationRef.current;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: {
        sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true,
      } });
      if (cancelled()) { stream.getTracks().forEach(track => track.stop()); return; }
      streamRef.current = stream;
      const context = new AudioContext({ sampleRate: 16000 });
      contextRef.current = context;
      // Suspend until the server authorizes this stream; capture cannot outrun it.
      await context.suspend();
      if (cancelled()) return;
      if (!context.audioWorklet || context.sampleRate !== 16000) throw new Error('16 kHz AudioWorklet capture is unavailable in this browser.');
      await context.audioWorklet.addModule(new URL('../audio/pcmCapture.worklet.js?no-inline', import.meta.url).href);
      if (cancelled()) return;
      const { vadThreshold = 0.015, silenceMs = 500, minSpeechMs = 200, patientId, hints, language } = optionsRef.current;
      const processor = new AudioWorkletNode(context, 'orb-pcm-capture', {
        numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        processorOptions: { vadThreshold, silenceMs, minSpeechMs },
      });
      processorRef.current = processor;
      processor.onprocessorerror = () => fail('Audio capture failed. Please retry the last utterance.');
      const source = context.createMediaStreamSource(stream);
      sourceRef.current = source;
      processor.port.onmessage = ({ data }: MessageEvent<CaptureMessage>) => {
        const current = () => mountedRef.current && processorRef.current === processor && Boolean(activeIdRef.current);
        if (!current() || !readyRef.current) return;
        if (data.error) { fail(data.error); return; }
        setState(prev => ({ ...prev, audioLevel: data.level ?? 0, isSpeaking: Boolean(data.speaking),
          isProcessing: data.end ? true : prev.isProcessing }));
        const acknowledge = () => {
          if (!current()) return;
          if (data.end) socket.emit('scribe:end_utterance');
          if (data.finished) {
            readyRef.current = false;
            finishingRef.current = false;
            if (timerRef.current) clearTimeout(timerRef.current);
            timerRef.current = null;
            releaseMicrophone();
            socket.emit('scribe:stop_session');
          } else processor.port.postMessage('ack');
        };
        if (data.audio) {
          // Wait for receipt before granting another worklet message credit.
          // Socket.IO's offline send buffer must never collect patient audio.
          if (!socket.connected) { fail('Dictation disconnected. Please retry.'); return; }
          socket.timeout(5000).emit('scribe:audio_chunk', data.audio, (error: Error | null) => {
            if (!current()) return;
            if (error) fail('Audio delivery timed out. Please retry this utterance.');
            else acknowledge();
          });
        } else acknowledge();
      };
      source.connect(processor);
      processor.connect(context.destination); // Worklet writes silence to output.
      activeIdRef.current = crypto.randomUUID();
      setState(initialState);
      timerRef.current = setTimeout(() => fail('Dictation did not become ready. Please retry.'), 10000);
      socket.emit('scribe:start_session', { patient_id: patientId, hints, language, stream_id: activeIdRef.current });
    } catch (error) {
      if (!cancelled()) fail(error instanceof Error ? error.message : 'Microphone access denied');
    }
  }, [fail, releaseMicrophone]);

  return { ...state, startStreaming, stopStreaming };
}
