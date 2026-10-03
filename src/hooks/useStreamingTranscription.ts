/**
 * useStreamingTranscription — Real-time voice transcription via Socket.IO
 *
 * Handles: mic access, PCM16 audio capture, energy-based VAD,
 * Socket.IO streaming to server, and transcript accumulation.
 */
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

export function useStreamingTranscription(options: StreamingTranscriptionOptions = {}) {
  const {
    patientId,
    hints,
    language,
    onFinalTranscript,
    onError,
    onStatusChange,
    vadThreshold = 0.015,
    silenceMs = 500,
    minSpeechMs = 200,
  } = options;

  const [state, setState] = useState<StreamingTranscriptionState>({
    isStreaming: false,
    isProcessing: false,
    isSpeaking: false,
    currentTranscript: '',
    fullTranscript: '',
    utteranceCount: 0,
    audioLevel: 0,
  });

  // Refs for mutable state that shouldn't trigger re-renders
  const audioContextRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const isSpeakingRef = useRef(false);
  const speechStartRef = useRef(0);
  const silenceStartRef = useRef(0);
  const audioBufferRef = useRef<Int16Array[]>([]);
  const isStreamingRef = useRef(false);
  const utteranceCountRef = useRef(0);
  // Throttle audioLevel state updates: onaudioprocess fires ~4x/sec and a setState
  // per frame forces a full re-render of the consuming panel that often. We only
  // push the level into React at most ~10x/sec (cheap on the 8GB Mini demo HW).
  const lastLevelPushRef = useRef(0);

  // Socket event handlers
  useEffect(() => {
    const handleTranscript = (data: { text: string; duration_ms: number; is_final: boolean; utterance_index: number }) => {
      if (!data.text) return;

      setState(prev => ({
        ...prev,
        isProcessing: false,
        currentTranscript: data.text,
        fullTranscript: prev.fullTranscript + (prev.fullTranscript ? ' ' : '') + data.text,
        utteranceCount: data.utterance_index + 1,
      }));

      onFinalTranscript?.(data.text, data.utterance_index);
    };

    const handleStatus = (data: { status: string }) => {
      setState(prev => ({
        ...prev,
        isProcessing: data.status === 'processing',
      }));
      onStatusChange?.(data.status);
    };

    const handleError = (data: { error: string }) => {
      onError?.(data.error);
      setState(prev => ({ ...prev, isProcessing: false }));
    };

    socket.on('scribe:transcript', handleTranscript);
    socket.on('scribe:status', handleStatus);
    socket.on('scribe:error', handleError);

    return () => {
      socket.off('scribe:transcript', handleTranscript);
      socket.off('scribe:status', handleStatus);
      socket.off('scribe:error', handleError);
    };
  }, [onFinalTranscript, onError, onStatusChange]);

  const startStreaming = useCallback(async () => {
    if (isStreamingRef.current) return;

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });

      streamRef.current = stream;

      // Create audio context at 16kHz
      const audioCtx = new AudioContext({ sampleRate: 16000 });
      audioContextRef.current = audioCtx;

      const source = audioCtx.createMediaStreamSource(stream);
      sourceRef.current = source;

      // ScriptProcessor for raw PCM access (4096 samples per frame = ~256ms at 16kHz)
      // TODO (deferred — AudioWorklet migration): createScriptProcessor is
      // deprecated and runs VAD/PCM conversion on the main thread, causing jank
      // during live dictation. Move audio capture + VAD into an AudioWorkletNode
      // (off the main thread) and post only level/utterance events back. Deferred
      // because it requires a separate worklet module + build wiring and changes
      // the audio plumbing, beyond a minimal in-file fix.
      const processor = audioCtx.createScriptProcessor(4096, 1, 1);
      processorRef.current = processor;

      // Tell server to start a session
      socket.emit('scribe:start_session', { patient_id: patientId, hints, language });

      processor.onaudioprocess = (event) => {
        if (!isStreamingRef.current) return;

        const inputData = event.inputBuffer.getChannelData(0);

        // Calculate RMS energy for VAD
        let sumSquares = 0;
        for (let i = 0; i < inputData.length; i++) {
          sumSquares += inputData[i] * inputData[i];
        }
        const rms = Math.sqrt(sumSquares / inputData.length);
        const normalizedLevel = Math.min(1, rms / 0.15); // Normalize to 0-1

        const now = Date.now();

        // Throttle level updates to ~10x/sec to avoid a React render per frame.
        if (now - lastLevelPushRef.current >= 100) {
          lastLevelPushRef.current = now;
          setState(prev => ({ ...prev, audioLevel: normalizedLevel }));
        }

        const aboveThreshold = rms > vadThreshold;

        if (aboveThreshold) {
          // Record speech start only on the silence→speech transition so the
          // timestamp keeps accumulating across subsequent loud frames.
          if (!isSpeakingRef.current && speechStartRef.current === 0) {
            speechStartRef.current = now;
          }
          silenceStartRef.current = 0;

          // Mark as speaking after minSpeechMs
          if (now - speechStartRef.current > minSpeechMs) {
            if (!isSpeakingRef.current) {
              isSpeakingRef.current = true;
              setState(prev => ({ ...prev, isSpeaking: true }));
            }
          }
        } else {
          if (isSpeakingRef.current) {
            if (silenceStartRef.current === 0) {
              silenceStartRef.current = now;
            }

            // Silence exceeded threshold — end utterance
            if (now - silenceStartRef.current > silenceMs) {
              isSpeakingRef.current = false;
              speechStartRef.current = 0;
              setState(prev => ({ ...prev, isSpeaking: false, isProcessing: true }));

              // Send end_utterance signal
              socket.emit('scribe:end_utterance');
              audioBufferRef.current = [];
            }
          } else {
            // Level dropped before minSpeechMs accumulated — discard candidate start.
            speechStartRef.current = 0;
          }
        }

        // Convert Float32 to Int16 PCM
        const pcm16 = new Int16Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
          const s = Math.max(-1, Math.min(1, inputData[i]));
          pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }

        // Send audio chunk to server (only while speaking or recently speaking).
        // Reuse the single `now` captured above rather than re-reading the clock.
        if (isSpeakingRef.current || (silenceStartRef.current > 0 && now - silenceStartRef.current < silenceMs)) {
          socket.emit('scribe:audio_chunk', pcm16.buffer);
          audioBufferRef.current.push(pcm16);
        }
      };

      source.connect(processor);
      processor.connect(audioCtx.destination);

      isStreamingRef.current = true;
      utteranceCountRef.current = 0;

      setState(prev => ({
        ...prev,
        isStreaming: true,
        isProcessing: false,
        isSpeaking: false,
        currentTranscript: '',
        fullTranscript: '',
        utteranceCount: 0,
        audioLevel: 0,
      }));
    } catch (err) {
      onError?.((err as Error).message || 'Microphone access denied');
    }
  }, [patientId, hints, language, vadThreshold, silenceMs, minSpeechMs, onError]);

  const stopStreaming = useCallback(() => {
    if (!isStreamingRef.current) return;
    isStreamingRef.current = false;

    // Stop audio processing
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current = null;
    }
    if (sourceRef.current) {
      sourceRef.current.disconnect();
      sourceRef.current = null;
    }
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    // Stop mic stream
    streamRef.current?.getTracks().forEach(t => t.stop());
    streamRef.current = null;

    // Tell server to stop session
    socket.emit('scribe:stop_session');

    // Reset state
    isSpeakingRef.current = false;
    speechStartRef.current = 0;
    silenceStartRef.current = 0;
    audioBufferRef.current = [];

    setState(prev => ({
      ...prev,
      isStreaming: false,
      isProcessing: false,
      isSpeaking: false,
      audioLevel: 0,
    }));
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (isStreamingRef.current) {
        stopStreaming();
      }
    };
  }, [stopStreaming]);

  return {
    ...state,
    startStreaming,
    stopStreaming,
  };
}
