class ScannerPcm extends AudioWorkletProcessor {
  constructor({ processorOptions }) {
    super();
    this.rate = processorOptions.inputRate;
    this.ratio = this.rate / sampleRate;
    this.limit = Math.ceil(this.rate * 0.2);
    this.minimum = Math.ceil(this.rate * 0.08);
    this.buffer = new Float32Array(this.limit);
    this.port.onmessage = ({ data }) => {
      if (data === "clear") this.reset();
      else this.enqueue(data);
    };
    this.reset();
  }

  reset() {
    this.cursor = 0;
    this.available = 0;
    this.fraction = 0;
    this.buffering = true;
  }

  enqueue(samples) {
    const first = Math.max(0, samples.length - this.limit);
    for (let index = first; index < samples.length; index += 1) {
      const writeAt = (this.cursor + this.available) % this.limit;
      this.buffer[writeAt] = samples[index];
      if (this.available === this.limit) {
        this.cursor = (this.cursor + 1) % this.limit;
        this.fraction = 0;
      } else {
        this.available += 1;
      }
    }
  }

  process(_inputs, [channels]) {
    const [output] = channels;
    for (let index = 0; index < output.length; index += 1) {
      if (this.available < 2) this.buffering = true;
      if (this.buffering) {
        if (this.available < this.minimum) break;
        this.buffering = false;
      }
      const current = this.buffer[this.cursor];
      const next = this.buffer[(this.cursor + 1) % this.limit];
      output[index] = current + (next - current) * this.fraction;
      const advanced = this.fraction + this.ratio;
      const consumed = Math.min(this.available, Math.floor(advanced));
      this.cursor = (this.cursor + consumed) % this.limit;
      this.available -= consumed;
      this.fraction = advanced - Math.floor(advanced);
    }
    return true;
  }
}

registerProcessor("scanner-pcm", ScannerPcm);
