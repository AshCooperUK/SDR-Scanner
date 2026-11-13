const historySeconds = 8;
const historyRows = 50;
const rowDuration = historySeconds * 1000 / historyRows;
const maxContinuousGap = 550;

const formatMHz = hz => String(Number((hz / 1_000_000).toFixed(4)));

export class Waterfall {
  constructor(element) {
    this.element = element;
    this.canvas = element.querySelector("canvas");
    this.context = this.canvas.getContext("2d");
    this.ticks = element.querySelectorAll(".waterfall-frequencies span");
    this.age = element.querySelector(".waterfall-time span:last-child");
    this.frame = null;
    this.history = null;
    this.tunedPosition = null;
  }

  clear() {
    if (!this.frame) return;
    this.frame = null;
    this.history = null;
    this.tunedPosition = null;
    this.element.style.removeProperty("--tuned-position");
    this.context.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.ticks.forEach(tick => { tick.textContent = "—"; });
    this.age.textContent = "";
    this.element.setAttribute("aria-label", "Radio signal history");
  }

  update(frame, tuneHz) {
    if (!this.context) return;
    if (!frame || !Number.isFinite(frame.center_hz) || !Number.isFinite(frame.span_hz)
        || frame.span_hz <= 0 || !Number.isFinite(frame.time_ms) || !Array.isArray(frame.levels)
        || !frame.levels.length) {
      this.clear();
      return;
    }
    const previous = this.frame;
    const sameAxis = previous && previous.center_hz === frame.center_hz
      && previous.span_hz === frame.span_hz && previous.levels.length === frame.levels.length;
    const repeated = sameAxis && previous.sequence === frame.sequence && previous.time_ms === frame.time_ms;
    if (!repeated && !frame.levels.every(Number.isFinite)) return;
    this.updateMarker(frame, tuneHz);
    if (repeated) return;

    if (!sameAxis || frame.time_ms < previous.time_ms) {
      this.startHistory(frame);
    } else {
      this.shiftHistory(previous.time_ms, frame.time_ms);
    }

    this.drawRow(frame.levels);
    this.context.putImageData(this.history, 0, 0);
    this.frame = frame;
  }

  updateMarker(frame, tuneHz) {
    const fraction = Number.isFinite(tuneHz) ? 0.5 + (tuneHz - frame.center_hz) / frame.span_hz : 0.5;
    const position = Math.max(0, Math.min(100, fraction * 100));
    if (position === this.tunedPosition) return;
    this.tunedPosition = position;
    this.element.style.setProperty("--tuned-position", `${position}%`);
  }

  frequencyAt(clientX) {
    if (!this.frame || !Number.isFinite(clientX)) return null;
    const { left, width } = this.canvas.getBoundingClientRect();
    if (width <= 0) return null;
    const fraction = Math.max(0, Math.min(1, (clientX - left) / width));
    return Math.round(this.frame.center_hz + (fraction - 0.5) * this.frame.span_hz);
  }

  startHistory(frame) {
    this.canvas.width = frame.levels.length;
    this.canvas.height = historyRows;
    this.history = this.context.createImageData(this.canvas.width, this.canvas.height);
    const firstHz = frame.center_hz - frame.span_hz / 2;
    this.ticks.forEach((tick, index) => {
      tick.textContent = formatMHz(firstHz + frame.span_hz * index / (this.ticks.length - 1));
    });
    this.age.textContent = `−${historySeconds} s`;
    this.element.setAttribute("aria-label",
      `Radio signal history from ${formatMHz(firstHz)} to ${formatMHz(firstHz + frame.span_hz)} MHz`);
  }

  shiftHistory(previousMs, nextMs) {
    const elapsedRows = Math.floor(nextMs / rowDuration) - Math.floor(previousMs / rowDuration);
    if (!elapsedRows) return;
    const pixels = this.history.data;
    const rowBytes = this.canvas.width * 4;
    if (elapsedRows >= historyRows) {
      pixels.fill(0);
      return;
    }
    const offset = elapsedRows * rowBytes;
    pixels.copyWithin(offset, 0, pixels.length - offset);
    if (nextMs - previousMs <= maxContinuousGap) {
      for (let row = 1; row < elapsedRows; row += 1) pixels.copyWithin(row * rowBytes, 0, rowBytes);
    } else {
      pixels.fill(0, 0, offset);
    }
  }

  drawRow(levels) {
    const ordered = levels.slice().sort((a, b) => a - b);
    const floor = ordered[Math.floor(ordered.length * 0.15)];
    const pixels = this.history.data;
    for (let bin = 0; bin < levels.length; bin += 1) {
      const index = bin * 4;
      const strength = Math.max(0, Math.min(1, (levels[bin] - floor - 2) / 36));
      pixels[index] = 47;
      pixels[index + 1] = 22;
      pixels[index + 2] = 3;
      pixels[index + 3] = Math.round(19 + 225 * strength ** 0.8);
    }
  }
}
