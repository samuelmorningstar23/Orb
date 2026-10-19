/* global AudioWorkletProcessor, registerProcessor, sampleRate */
// Runs on the audio rendering thread. No patient data or network access here.
class OrbPcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const config = options.processorOptions || {};
    const bounded = (value, fallback, low, high) => Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
    this.threshold = bounded(config.vadThreshold, 0.015, 0.001, 1);
    this.silenceSamples = bounded(config.silenceMs, 500, 100, 2000) * 16;
    this.minSpeechSamples = bounded(config.minSpeechMs, 200, 0, 2000) * 16;
    this.frame = new Float32Array(4096);
    this.used = 0;
    this.candidate = [];
    this.speechSamples = 0;
    this.silentSamples = 0;
    this.utteranceSamples = 0;
    this.speaking = false;
    this.pending = [];
    this.credit = true;
    this.failed = sampleRate !== 16000;
    this.finishing = false;
    this.port.onmessage = ({ data }) => {
      if (data === 'ack') { this.credit = true; this.flush(); }
      if (data === 'stop') this.failed = true;
      if (data === 'finish' && !this.finishing && !this.failed) {
        if (this.used) {
          this.frame.fill(0, this.used);
          this.used = 0;
          this.batch();
        }
        this.finishing = true;
        this.pending.push({ finished: true, end: this.speaking, speaking: false, level: 0 });
        this.flush();
      }
    };
    if (this.failed) this.port.postMessage({ error: '16 kHz audio capture is unavailable.' });
  }
  flush() {
    if (this.credit && this.pending.length) {
      const message = this.pending.shift();
      this.credit = false;
      this.port.postMessage(message, message.audio ? [message.audio] : []);
    }
  }
  batch() {
    const pcm = new Int16Array(this.frame.length);
    let squares = 0;
    for (let i = 0; i < this.frame.length; i++) {
      const sample = Math.max(-1, Math.min(1, this.frame[i]));
      squares += sample * sample;
      pcm[i] = sample < 0 ? sample * 32768 : sample * 32767;
    }
    const rms = Math.sqrt(squares / pcm.length);
    let audio;
    let end = false;
    if (!this.speaking) {
      if (rms > this.threshold) {
        this.candidate.push(pcm);
        this.speechSamples += pcm.length;
        if (this.speechSamples >= this.minSpeechSamples) {
          this.speaking = true;
          const joined = new Int16Array(this.speechSamples);
          let offset = 0;
          for (const frame of this.candidate) { joined.set(frame, offset); offset += frame.length; }
          audio = joined.buffer; // Preserve the start of speech, including VAD confirmation.
          this.utteranceSamples = joined.length;
          this.candidate = [];
        }
      } else { this.candidate = []; this.speechSamples = 0; }
    } else {
      audio = pcm.buffer;
      this.utteranceSamples += pcm.length;
      this.silentSamples = rms > this.threshold ? 0 : this.silentSamples + pcm.length;
      // Split continuous dictation before the server's 60-second safety bound.
      if (this.silentSamples >= this.silenceSamples || this.utteranceSamples >= 16000 * 45) {
        end = true;
        this.speaking = false;
        this.speechSamples = 0;
        this.silentSamples = 0;
        this.utteranceSamples = 0;
      }
    }
    if (this.pending.length >= 8) {
      this.failed = true;
      this.pending = [];
      this.port.postMessage({ error: 'Audio capture cannot keep up. Stop and retry this utterance.' });
      return;
    }
    this.pending.push({ audio, end, speaking: this.speaking, level: Math.min(1, rms / 0.15) });
    this.flush();
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.failed || this.finishing) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      this.frame[this.used++] = sample;
      if (this.used === this.frame.length) {
        this.used = 0;
        this.batch();
        if (this.failed) return false;
      }
    }
    return true;
  }
}
registerProcessor('orb-pcm-capture', OrbPcmCapture);
