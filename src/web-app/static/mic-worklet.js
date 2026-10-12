// Microphone capture: downsample to 24 kHz mono PCM16 and post ~20 ms frames to the main thread.
class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 24000;
    this.pos = 0;
    this.out = [];
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        this.out.push(Math.max(-1, Math.min(1, ch[i])));
      }
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
