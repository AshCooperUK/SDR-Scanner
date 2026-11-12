export class PcmBuffer {
  constructor(inputRate, outputRate) {
    this.ratio = inputRate / outputRate;
    this.limit = Math.ceil(inputRate * 0.2);
    this.minimum = Math.ceil(inputRate * 0.08);
    this.buffer = new Float32Array(this.limit);
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

  readInto(output) {
    for (let index = 0; index < output.length; index += 1) {
      if (this.available < 2) this.buffering = true;
      if (this.buffering) {
        if (this.available < this.minimum) {
          output.fill(0, index);
          return;
        }
        this.buffering = false;
      }
      const current = this.buffer[this.cursor];
      const next = this.buffer[(this.cursor + 1) % this.limit];
      output[index] = current + (next - current) * this.fraction;
      const advanced = this.fraction + this.ratio;
      const whole = Math.floor(advanced);
      const consumed = Math.min(this.available, whole);
      this.cursor = (this.cursor + consumed) % this.limit;
      this.available -= consumed;
      this.fraction = advanced - whole;
    }
  }
}
