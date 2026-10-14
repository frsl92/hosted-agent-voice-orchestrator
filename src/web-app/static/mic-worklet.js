// Microphone capture: downsample to 24 kHz mono PCM16 and post ~20 ms frames to the main thread.
// A software gain stage lifts quiet microphones (Bluetooth headsets often deliver speech at ~-35 dBFS, which
// the server VAD ignores). It only adapts while speech is present, drops instantly to avoid clipping, and
// rises slowly, so room noise is not pumped up. Disable with processorOptions.autoGain = false.
const TARGET_RMS = 0.1, MAX_GAIN = 25, SPEECH_FLOOR = 0.003, PEAK_CEIL = 0.9;

class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.autoGain = o.autoGain !== false;
    this.gain = this.autoGain ? 4 : 1;
    this.env = 0;
    this.ratio = sampleRate / 24000;
    this.pos = 0;
    this.out = [];
    this.peak = 0;
    this.sumSq = 0;
    this.n = 0;
  }
  adaptGain(ch) {
    let sq = 0, pk = 0;
    for (let i = 0; i < ch.length; i++) {
      const a = ch[i] < 0 ? -ch[i] : ch[i];
      sq += a * a;
      if (a > pk) pk = a;
    }
    const p = sq / ch.length;
    this.env += (p - this.env) * (p > this.env ? 0.3 : 0.005);
    const rms = Math.sqrt(this.env);
    let want = this.gain;
    if (rms > SPEECH_FLOOR) want = Math.min(MAX_GAIN, TARGET_RMS / rms);
    if (pk * want > PEAK_CEIL) want = Math.min(want, PEAK_CEIL / pk);
    this.gain = want < this.gain ? want : this.gain + (want - this.gain) * 0.01;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    if (this.autoGain) this.adaptGain(ch);
    const g = this.gain;
    for (let i = 0; i < ch.length; i++) {
      const v = ch[i];
      const a = v < 0 ? -v : v;
      if (a > this.peak) this.peak = a;
      this.sumSq += v * v;
      this.n++;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        this.out.push(Math.max(-1, Math.min(1, v * g)));
      }
    }
    if (this.n >= sampleRate) {
      this.port.postMessage({ peak: this.peak, rms: Math.sqrt(this.sumSq / this.n), gain: this.gain });
      this.peak = this.sumSq = this.n = 0;
    }
    while (this.out.length >= 480) {
      const frame = this.out.splice(0, 480);
      const pcm = new Int16Array(480);
      for (let i = 0; i < 480; i++) pcm[i] = frame[i] < 0 ? frame[i] * 0x8000 : frame[i] * 0x7fff;
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("mic-processor", MicProcessor);
