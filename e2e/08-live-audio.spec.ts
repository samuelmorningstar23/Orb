import { readdirSync } from 'node:fs';
import { expect, test } from '@playwright/test';

// Real browser audio thread and the built asset; no microphone, model or PHI.
for (const mode of ['silence', 'stop'] as const) test(`built AudioWorklet captures PCM/VAD without speaker echo (${mode})`, async ({ page }) => {
  const asset = readdirSync('dist/assets').find(name => /^pcmCapture\.worklet-.*\.js$/.test(name));
  expect(asset).toBeTruthy();
  await page.goto('/');
  await page.mouse.click(2, 2); // Browser user activation for Web Audio.
  const result = await page.evaluate(async ({ url, mode }) => {
    const context = new AudioContext({ sampleRate: 16000 });
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await context.audioWorklet.addModule(url);
      const node = new AudioWorkletNode(context, 'orb-pcm-capture');
      const analyser = context.createAnalyser();
      node.connect(analyser);
      analyser.connect(context.destination);
      const buffer = context.createBuffer(1, 24000, 16000);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < 9600; i++) samples[i] = 0.2 * Math.sin(i * 2 * Math.PI * 440 / 16000);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(node);
      const result = new Promise<{ bytes: number; end: boolean; speaking: boolean; outputPeak: number; finished: boolean }>((resolve, reject) => {
        let bytes = 0;
        let speaking = false;
        node.port.onmessage = ({ data }) => {
          if (data.error) { reject(new Error(data.error)); return; }
          if (data.audio) bytes += data.audio.byteLength;
          speaking ||= data.speaking === true;
          node.port.postMessage('ack');
          if (mode === 'stop' && data.audio) node.port.postMessage('finish');
          if ((mode === 'silence' && data.end) || (mode === 'stop' && data.finished)) {
            const output = new Float32Array(analyser.fftSize);
            analyser.getFloatTimeDomainData(output);
            resolve({ bytes, end: data.end === true, speaking, finished: data.finished === true, outputPeak: Math.max(...output.map(Math.abs)) });
          }
        };
        timeout = setTimeout(() => reject(new Error('AudioWorklet did not finish the synthetic utterance')), 8000);
      });
      await context.resume();
      source.start();
      return await result;
    } finally {
      clearTimeout(timeout);
      await context.close();
    }
  }, { url: `/assets/${asset}`, mode });
  expect(result.bytes).toBeGreaterThan(mode === 'silence' ? 16000 : 0);
  expect(result.finished).toBe(mode === 'stop');
  expect(result.bytes).toBeLessThan(16000 * 2 * 2);
  expect(result.end && result.speaking).toBe(true);
  expect(result.outputPeak).toBe(0);
});
