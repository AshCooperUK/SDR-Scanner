import { PcmBuffer } from "./pcm-buffer.js";

class ScannerPcm extends AudioWorkletProcessor {
  constructor({ processorOptions }) {
    super();
    this.pcm = new PcmBuffer(processorOptions.inputRate, sampleRate);
    this.port.onmessage = ({ data }) => {
      if (data === "clear") this.pcm.reset();
      else this.pcm.enqueue(data);
    };
  }

  process(_inputs, [channels]) {
    this.pcm.readInto(channels[0]);
    return true;
  }
}

registerProcessor("scanner-pcm", ScannerPcm);
