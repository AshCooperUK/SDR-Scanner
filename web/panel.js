const keys = document.querySelectorAll("[data-key]");
const lcd = document.querySelector("#lcd");
const display = document.querySelector("#display");
const channel = document.querySelector("#channel");
const legends = document.querySelectorAll("[data-lcd]");
const dials = new Map();
const presses = new Map();
const audioRate = 24000;
const lockInterval = 280;
const wheelQuietPeriod = 110;
const wheelPixelThreshold = 48;
const wheelDetentPixels = 80;
const wheelDetentInterval = 32;

const segmentMap = {
  "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
  "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
  A: "abcefg", B: "cdefg", C: "adef", D: "bcdeg", E: "adefg",
  F: "aefg", G: "acdef", H: "bcefg", I: "bc", J: "bcde",
  L: "def", N: "ceg", O: "abcdef", P: "abefg", Q: "abcfg",
  R: "eg", S: "acdfg", T: "defg", U: "bcdef", X: "bcefg", Y: "bcdfg",
  "-": "g", "_": "d", " ": "",
};

const legendAliases = {
  PRIVATE: "PVT",
  MARINE: "MRN",
  FM: "NFM",
  CC: "CLOSE",
  "CLOSE CALL": "CLOSE",
  "UP": "▲",
  "DOWN": "▼",
};

let state = { display: "", power: false, volume: 0, squelch: 0, backlight: false };
let actionTail = Promise.resolve();
let pendingActions = 0;
let actionVersion = 0;
let frequencyEntry = "";
let wheelGesture;
let lockTimer;
let audioContext;
let audioReady;
let pcmNode;
let volumeNode;
let audioStream;
let audioRetry;
let lastToneId;
let previousVolume = 0.32;

function drawDigits(container, value) {
  const text = String(value ?? "").toUpperCase();
  if (container.dataset.text === text) return;
  container.dataset.text = text;
  const characters = [...text].map((character) => {
    const cell = document.createElement("span");
    if (character === "." || character === ":") {
      cell.className = character === "." ? "digit-dot" : "digit-colon";
    } else if (character in segmentMap) {
      cell.className = "digit";
      const lit = segmentMap[character];
      for (const side of "abcdefg") {
        const segment = document.createElement("span");
        segment.className = "segment segment-" + side + (lit.includes(side) ? " is-on" : "");
        cell.append(segment);
      }
    } else {
      cell.className = "digit-text";
      cell.textContent = character;
    }
    return cell;
  });
  container.replaceChildren(...characters);
}

function canTune() {
  return state.can_tune ?? state.power;
}

function render(next) {
  if (typeof next.display !== "string") return;
  const previous = state;
  state = next;
  if (!canTune()) frequencyEntry = "";
  const entering = frequencyEntry !== "";
  const active = new Set();
  for (const indicator of [...(next.upper ?? []), ...(next.lower ?? [])]) {
    const value = indicator.toUpperCase();
    if (value === "T L/O") {
      active.add("T");
      active.add("L/O");
    } else {
      active.add(legendAliases[value] ?? value);
    }
  }
  const visibleChannel = !entering && next.channel !== null && next.channel !== undefined;
  if (visibleChannel) active.add("CH");
  for (const element of legends) {
    const name = element.dataset.lcd;
    element.classList.toggle("is-visible", active.has(name));
    element.classList.toggle("is-blinking",
      active.has(name) && (next.blink_bank === name || (name === "CLOSE" && next.close_blink)));
  }

  lcd.classList.toggle("is-unlit", !(next.backlight ?? next.power));
  const readout = entering ? frequencyEntry : next.display;
  lcd.setAttribute("aria-label", entering ? `${readout} MHz`
    : [readout.trim(), ...active].filter(Boolean).join(" ") || "Scanner display");
  display.classList.toggle("is-blinking", !entering && Boolean(next.blink));
  display.classList.toggle("is-scrolling", !entering && Boolean(next.scan_scroll ?? next.display === "SCAN"));
  display.classList.toggle("is-clock", !next.power && next.display.includes(":"));
  drawDigits(display, readout);
  drawDigits(channel, visibleChannel ? String(next.channel).padStart(2, "0") : "");

  for (const dial of dials.values()) {
    if (!dial.drag && dial.pending === null && !dial.busy) {
      paintDial(dial, state[dial.name]);
    }
  }

  updateAudio(previous);
  if (next.tone && next.tone.id !== lastToneId) {
    const initialized = lastToneId !== undefined;
    lastToneId = next.tone.id;
    if (initialized || pendingActions > 0) playTone(next.tone);
  }
}

async function poll() {
  try {
    if (!pendingActions) {
      const startedAt = actionVersion;
      const response = await fetch("/api/state", { cache: "no-store" });
      if (response.ok) {
        const next = await response.json();
        if (startedAt === actionVersion && !pendingActions) render(next);
      }
    }
  } catch {
    // The next poll retries the local service.
  } finally {
    setTimeout(poll, 150);
  }
}

function postAction(action) {
  pendingActions += 1;
  actionVersion += 1;
  actionTail = actionTail.catch(() => {}).then(async () => {
    const response = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action),
    });
    if (response.ok) render(await response.json());
  }).finally(() => {
    pendingActions -= 1;
  });
  return actionTail.catch(() => {});
}

function flushLockTap() {
  if (lockTimer === undefined) return;
  clearTimeout(lockTimer);
  lockTimer = undefined;
  void postAction({ key: "lock", gesture: "tap" });
}

function editFrequency(value) {
  if (value === frequencyEntry) return;
  frequencyEntry = value;
  render(state);
}

function focusLCD() {
  lcd.focus({ preventScroll: true });
  if (state.power) void enableAudio();
}

lcd.addEventListener("click", focusLCD);
lcd.addEventListener("wheel", (event) => {
  // Read the units before the delta because some browsers normalize on access.
  const mode = event.deltaMode;
  const delta = event.deltaY;
  if (!delta || event.ctrlKey || event.metaKey) return;
  event.preventDefault();
  if (!canTune()) {
    wheelGesture = undefined;
    return;
  }
  const direction = delta < 0 ? 1 : -1;
  const elapsed = wheelGesture ? event.timeStamp - wheelGesture.lastAt : Infinity;
  if (!wheelGesture || wheelGesture.direction !== direction || wheelGesture.mode !== mode
      || elapsed >= wheelQuietPeriod) {
    wheelGesture = { direction, mode, distance: 0, stepped: false };
  }
  wheelGesture.lastAt = event.timeStamp;
  const distance = Math.abs(delta);
  const detent = mode !== 0 || distance >= wheelDetentPixels;
  wheelGesture.distance += distance;
  // Precision wheels can deliver several events for the same detent.
  if (wheelGesture.stepped && (!detent || elapsed < wheelDetentInterval)) return;
  if (!detent && wheelGesture.distance < wheelPixelThreshold) return;
  wheelGesture.stepped = true;
  editFrequency("");
  flushLockTap();
  void postAction({ tune: "step", direction });
}, { passive: false });

lcd.addEventListener("pointerleave", () => {
  wheelGesture = undefined;
  if (document.activeElement !== lcd) editFrequency("");
});

lcd.addEventListener("blur", () => editFrequency(""));

window.addEventListener("pointerdown", (event) => {
  if (!lcd.contains(event.target)) editFrequency("");
});

window.addEventListener("keydown", (event) => {
  if (!canTune() || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
  if (document.activeElement !== lcd && !lcd.matches(":hover")) return;
  if (event.target instanceof HTMLElement
      && (event.target.isContentEditable || event.target.matches("input, textarea, select"))) return;

  const { key } = event;
  const decimal = key === "." || (key === "," && event.code === "NumpadDecimal");
  if (/^[0-9]$/.test(key) || decimal) {
    event.preventDefault();
    const value = frequencyEntry + (decimal ? "." : key);
    const next = value.startsWith(".") ? "0" + value : value;
    if (/^[0-9]{1,4}(?:\.[0-9]{0,6})?$/.test(next)) editFrequency(next);
    return;
  }
  if (!frequencyEntry || !["Backspace", "Escape", "Enter"].includes(key)) return;

  event.preventDefault();
  event.stopPropagation();
  if (key === "Backspace") {
    editFrequency(frequencyEntry.slice(0, -1));
  } else {
    const mhz = frequencyEntry.replace(/\.$/, "");
    editFrequency("");
    if (key === "Enter") {
      void enableAudio();
      flushLockTap();
      void postAction({ tune: "frequency", mhz });
    }
  }
}, { capture: true });

function sendTap(key) {
  if (key !== "lock") {
    flushLockTap();
    void postAction({ key, gesture: "tap" });
  } else if (lockTimer !== undefined) {
    clearTimeout(lockTimer);
    lockTimer = undefined;
    void postAction({ key, gesture: "double" });
  } else {
    lockTimer = setTimeout(flushLockTap, lockInterval);
  }
}

function isArrow(key) {
  return key === "up" || key === "down";
}

function longPressDelay(key) {
  if (isArrow(key)) return state.power ? 1010 : 2010;
  if (key === "band") return 850;
  if (["lock", "close", "prog"].includes(key)) return 2510;
  return null;
}

function beginPress(id, element) {
  if (presses.has(id)) return;
  const key = element.dataset.key;
  const press = { element, key, handled: false };
  presses.set(id, press);
  element.classList.add("is-pressed");
  void enableAudio();
  const delay = longPressDelay(key);
  if (delay !== null) {
    press.timer = setTimeout(() => {
      press.handled = true;
      flushLockTap();
      const repeat = () => {
        if (!pendingActions) void postAction({ key, gesture: "long" });
      };
      void postAction({ key, gesture: "long" });
      if (isArrow(key)) press.repeat = setInterval(repeat, 110);
    }, delay);
  }
}

function clearPressTimers(press) {
  clearTimeout(press.timer);
  clearInterval(press.repeat);
}

function endPress(id, cancelled = false) {
  const press = presses.get(id);
  if (!press) return;
  presses.delete(id);
  clearPressTimers(press);
  if (![...presses.values()].some((other) => other.element === press.element)) {
    press.element.classList.remove("is-pressed");
  }
  if (!cancelled && !press.handled) sendTap(press.key);
}

for (const button of keys) {
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    button.setPointerCapture(event.pointerId);
    beginPress(event.pointerId, button);
  });
  button.addEventListener("pointerup", (event) => endPress(event.pointerId));
  button.addEventListener("pointercancel", (event) => endPress(event.pointerId, true));
  button.addEventListener("lostpointercapture", (event) => endPress(event.pointerId, true));
  button.addEventListener("contextmenu", (event) => event.preventDefault());
  button.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    if (!event.repeat) beginPress("keyboard:" + event.key, button);
  });
  button.addEventListener("click", (event) => {
    if (event.detail === 0) {
      void enableAudio();
      sendTap(button.dataset.key);
    }
  });
}

window.addEventListener("keyup", (event) => {
  const id = "keyboard:" + event.key;
  if (!presses.has(id)) return;
  event.preventDefault();
  endPress(id);
});
window.addEventListener("blur", () => {
  editFrequency("");
  for (const id of presses.keys()) endPress(id, true);
});

function limit(value) {
  return Math.min(1, Math.max(0, value));
}

function paintDial(dial, value) {
  if (typeof value !== "number") return;
  dial.value = limit(value);
  dial.element.style.setProperty("--rotation", (-90 + dial.value * 270) + "deg");
  const percent = Math.round(dial.value * 100);
  dial.element.setAttribute("aria-valuenow", String(percent));
  dial.element.setAttribute("aria-valuetext",
    dial.name === "volume" && dial.value === 0 ? "Clock standby" : percent + " percent");
  if (dial.name === "volume" && dial.value > 0) previousVolume = dial.value;
}

function heldBootChord() {
  const active = [...presses.values()];
  const held = new Set(active.map(({ key }) => key));
  const chord = [["hold", "down"], ["lock", "prog"]].find((pair) => pair.every((key) => held.has(key)));
  if (!chord) return undefined;
  for (const press of active) {
    if (chord.includes(press.key)) {
      press.handled = true;
      clearPressTimers(press);
    }
  }
  return chord;
}

function flushDial(dial) {
  if (dial.pending === null || dial.busy) return;
  const action = dial.pending;
  dial.pending = null;
  dial.busy = true;
  void postAction(action).finally(() => {
    dial.busy = false;
    if (dial.pending !== null) {
      flushDial(dial);
    } else if (!dial.drag) {
      paintDial(dial, state[dial.name]);
    }
  });
}

function changeDial(dial, value) {
  const previous = dial.value;
  const rounded = Math.round(limit(value) * 1000) / 1000;
  if (previous === rounded) return;
  paintDial(dial, rounded);
  const action = { knob: dial.name, value: rounded };
  if (dial.name === "volume" && previous === 0 && rounded > 0 && !state.power) {
    const held = heldBootChord();
    if (held) {
      action.held = held;
      if (dial.drag) dial.drag.suppress = true;
    }
  }
  dial.pending = action;
  flushDial(dial);
}

function tapDial(dial, event) {
  if (dial.name === "volume") {
    changeDial(dial, dial.value > 0 ? 0 : previousVolume);
    return;
  }
  const rect = dial.element.getBoundingClientRect();
  const x = event.clientX - rect.left - rect.width / 2;
  const y = event.clientY - rect.top - rect.height / 2;
  if (Math.hypot(x, y) < rect.width * 0.18) return;
  const angle = Math.atan2(x, -y) * 180 / Math.PI;
  changeDial(dial, (angle + 135) / 270);
}

for (const element of document.querySelectorAll("[data-knob]")) {
  const name = element.dataset.knob;
  const dial = { name, element, value: 0, pending: null, busy: false, drag: null };
  dials.set(name, dial);
  paintDial(dial, 0);
  element.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || dial.drag) return;
    void enableAudio();
    dial.drag = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      start: dial.value,
      moved: false,
    };
    element.setPointerCapture(event.pointerId);
  });
  element.addEventListener("pointermove", (event) => {
    const drag = dial.drag;
    if (drag?.id !== event.pointerId || drag.suppress) return;
    const x = event.clientX - drag.x;
    const y = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(x, y) < 4) return;
    drag.moved = true;
    element.classList.add("is-dragging");
    const distance = Math.max(140, element.getBoundingClientRect().width * 1.8);
    changeDial(dial, drag.start + (x - y) / distance);
  });
  const finishDrag = (event) => {
    if (dial.drag?.id !== event.pointerId) return;
    const { moved } = dial.drag;
    dial.drag = null;
    element.classList.remove("is-dragging");
    if (!moved && event.type === "pointerup") tapDial(dial, event);
    if (!dial.busy) paintDial(dial, state[dial.name]);
  };
  element.addEventListener("pointerup", finishDrag);
  element.addEventListener("pointercancel", finishDrag);
  element.addEventListener("lostpointercapture", finishDrag);
  element.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (!event.deltaY) return;
    void enableAudio();
    changeDial(dial, dial.value - Math.sign(event.deltaY) * 0.04);
  }, { passive: false });
  element.addEventListener("keydown", (event) => {
    let value;
    if (event.key === "ArrowUp" || event.key === "ArrowRight") value = dial.value + 0.04;
    if (event.key === "ArrowDown" || event.key === "ArrowLeft") value = dial.value - 0.04;
    if (event.key === "PageUp") value = dial.value + 0.15;
    if (event.key === "PageDown") value = dial.value - 0.15;
    if (event.key === "Home") value = 0;
    if (event.key === "End") value = 1;
    if (dial.name === "volume" && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      if (!event.repeat) {
        void enableAudio();
        tapDial(dial, event);
      }
      return;
    }
    if (value !== undefined) {
      event.preventDefault();
      void enableAudio();
      changeDial(dial, value);
    }
  });
}

async function enableAudio() {
  try {
    if (!audioContext) {
      audioContext = new AudioContext({ latencyHint: "interactive" });
      audioReady = audioContext.audioWorklet.addModule(new URL("pcm-worklet.js", import.meta.url)).then(() => {
        pcmNode = new AudioWorkletNode(audioContext, "scanner-pcm", {
          numberOfInputs: 0,
          numberOfOutputs: 1,
          outputChannelCount: [1],
          processorOptions: { inputRate: audioRate },
        });
        volumeNode = audioContext.createGain();
        pcmNode.connect(volumeNode).connect(audioContext.destination);
      });
    }
    await Promise.all([audioContext.resume(), audioReady]);
    updateAudio();
  } catch (error) {
    console.error("Scanner audio could not start", error);
  }
}

function updateAudio(previous = state) {
  if (!pcmNode) return;
  volumeNode.gain.setTargetAtTime(state.power ? state.volume ** 1.45 : 0, audioContext.currentTime, 0.015);
  if (state.tune_hz !== previous.tune_hz || (!state.audio_open && previous.audio_open)) {
    pcmNode.port.postMessage("clear");
  }
  if (state.power && audioContext.state === "running") {
    if (!audioStream && !audioRetry) void streamAudio();
  } else {
    audioStream?.abort();
    clearTimeout(audioRetry);
    audioRetry = undefined;
    pcmNode.port.postMessage("clear");
  }
}

async function streamAudio() {
  const controller = new AbortController();
  audioStream = controller;
  let trailing = -1;
  try {
    const response = await fetch("/audio", { cache: "no-store", signal: controller.signal });
    if (!response.ok || !response.body) throw new Error("Audio stream unavailable");
    const reader = response.body.getReader();
    for (;;) {
      const { value: bytes, done } = await reader.read();
      if (done || controller.signal.aborted) break;
      const samples = new Float32Array((bytes.length + (trailing >= 0 ? 1 : 0)) >> 1);
      let offset = 0;
      let output = 0;
      if (trailing >= 0 && bytes.length > 0) {
        samples[output++] = signedSample(trailing, bytes[offset++]);
        trailing = -1;
      }
      while (offset + 1 < bytes.length) {
        samples[output++] = signedSample(bytes[offset], bytes[offset + 1]);
        offset += 2;
      }
      if (offset < bytes.length) trailing = bytes[offset];
      if (samples.length) pcmNode.port.postMessage(samples, [samples.buffer]);
    }
  } catch (error) {
    if (error.name !== "AbortError") console.debug(error.message);
  } finally {
    if (audioStream === controller) audioStream = undefined;
    if (state.power && audioContext.state === "running") {
      audioRetry = setTimeout(() => {
        audioRetry = undefined;
        updateAudio();
      }, controller.signal.aborted ? 0 : 1000);
    }
  }
}

function signedSample(low, high) {
  const value = low | (high << 8);
  return ((value ^ 0x8000) - 0x8000) / 0x8000;
}

function playTone({ count, kind }) {
  if (!volumeNode || !state.power || audioContext.state !== "running") return;
  const frequency = kind === "error" ? 460 : kind === "close" ? 1100 : 780;
  for (let index = 0; index < Math.min(count, 2); index += 1) {
    const start = audioContext.currentTime + index * 0.1;
    const oscillator = audioContext.createOscillator();
    const envelope = audioContext.createGain();
    oscillator.frequency.value = frequency;
    envelope.gain.setValueAtTime(0.001, start);
    envelope.gain.exponentialRampToValueAtTime(0.19, start + 0.008);
    envelope.gain.exponentialRampToValueAtTime(0.001, start + 0.065);
    oscillator.connect(envelope).connect(volumeNode);
    oscillator.start(start);
    oscillator.stop(start + 0.07);
  }
}

window.addEventListener("pagehide", () => {
  audioStream?.abort();
  clearTimeout(audioRetry);
});
void poll();
