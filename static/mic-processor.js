/**
 * Mic capture worklet.
 *
 * Takes the browser's float32 mic blocks, resamples to 16 kHz if the audio
 * context isn't already running at that rate, converts to PCM16, and posts
 * frames of FRAME_SAMPLES back to the main thread as transferable buffers.
 */

const TARGET_RATE = 16000;
const FRAME_SAMPLES = 1024;

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.muted = false;
    this.buffer = new Float32Array(FRAME_SAMPLES);
    this.filled = 0;
    this.ratio = sampleRate / TARGET_RATE;
    this.phase = 0;
    this.tail = 0;

    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'mute') {
        this.muted = !!event.data.value;
      }
    };
  }

  push(sample) {
    this.buffer[this.filled++] = sample;
    if (this.filled === FRAME_SAMPLES) {
      const pcm = new Int16Array(FRAME_SAMPLES);
      for (let i = 0; i < FRAME_SAMPLES; i++) {
        const s = Math.max(-1, Math.min(1, this.buffer[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
      this.filled = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (!channel || channel.length === 0) return true;
    if (this.muted) return true;

    if (Math.abs(this.ratio - 1) < 1e-6) {
      for (let i = 0; i < channel.length; i++) this.push(channel[i]);
      this.tail = channel[channel.length - 1];
      return true;
    }

    let pos = this.phase;
    while (pos < channel.length) {
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const a = idx === 0 ? this.tail : channel[idx - 1];
      const b = channel[idx];
      this.push(a + (b - a) * frac);
      pos += this.ratio;
    }
    this.phase = pos - channel.length;
    this.tail = channel[channel.length - 1];
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
