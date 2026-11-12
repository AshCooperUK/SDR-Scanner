import { PcmBuffer } from "./pcm-buffer.js";

const radio = document.querySelector(".radio");
const lcd = document.querySelector("#lcd");
const signalMeter = document.querySelector("#signal-meter");
const signalBars = signalMeter.querySelectorAll("[data-signal-bar]");
const display = document.querySelector("#display");
const channel = document.querySelector("#channel");
const legends = document.querySelectorAll("[data-lcd]");
const rdsToggle = document.querySelector("#rds-toggle");
const watchToggle = document.querySelector("#watch-toggle");
const recordToggle = document.querySelector("#record-toggle");
const playLast = document.querySelector("#play-last");
const lastRecording = document.querySelector("#last-recording");
const recordFolderDialog = document.querySelector("#record-folder-dialog");
const recordFolderPath = document.querySelector("#record-folder-path");
const recordFolderList = document.querySelector("#record-folder-list");
const recordFolderStatus = document.querySelector("#record-folder-status");
const recordFolderUp = document.querySelector("#record-folder-up");
const recordFolderOpen = document.querySelector("#record-folder-open");
const recordFolderSelect = document.querySelector("#record-folder-select");
const settings = document.querySelectorAll("[data-setting]");
const rdsReadout = document.querySelector("#rds-readout");
const favoriteNameEditor = document.querySelector("#favorite-name");
const muteKey = document.querySelector('[data-key="mute"]');
const tooltip = document.querySelector("#panel-tooltip");
const favorites = Array.from({ length: 24 }, (_, index) => {
  const button = document.createElement("button");
  const label = document.createElement("span");
  const cap = document.createElement("span");
  button.type = "button";
  button.className = "extension-button";
  button.dataset.favorite = String(index + 1);
  label.className = "button-label";
  label.textContent = `F${String(index + 1).padStart(2, "0")}`;
  cap.className = "button-cap";
  cap.setAttribute("aria-hidden", "true");
  button.setAttribute("aria-description", "Press and hold to save the tuned frequency. Open the context menu to name this favourite.");
  button.append(label, cap);
  return button;
});
document.querySelector("#favorite-bank").append(...favorites);
const keys = [...document.querySelectorAll("[data-key]"), ...favorites];
const dials = new Map();
const presses = new Map();
const audioRate = 24000;
const favoriteHoldDelay = 850;
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

const controlHelp = {
  hold: "Pause or resume scanning on the current frequency.",
  up: "Hold the next saved favourite in FAV mode. Elsewhere, step or search up.",
  down: "Hold the previous saved favourite in FAV mode. Elsewhere, step or search down.",
  mute: "Toggle audio and panel LEDs. The LCD stays on.",
  fav: "Toggle scanning of the FAV memories.",
  pmr: "Toggle scanning of the PMR446 preset channels.",
  close: "Cycle nearby-signal modes. Hold for Close Call Only.",
  air: "Cycle Air and Marine, Air only, Marine only, and off.",
  cb: "Cycle the CB AM and FM scan banks.",
  scan: "Start or resume scanning the selected presets or saved favourites.",
  search: "Sweep the selected range or start a full-range search.",
  lock: "Tap to skip. Double-tap to lock out permanently. Hold to clear temporary skips and unlock the current channel.",
  band: "Choose a search band. Hold to change the frequency step.",
  prog: "Program a memory. Hold to toggle Program Lock.",
  squelch: "Set how strong a signal must be to open audio. Scroll for 0.5 dB steps.",
  volume: "Click to switch between radio and clock. Drag or scroll to adjust volume.",
  rds: "Toggle station name and text from broadcast FM stations that send RDS.",
  watch: "Watch F01–F24 for signals above squelch. A flashing amber LED marks activity.",
  record: "Tap to arm or stop recording. Right-click to choose a folder. Hold to delete recordings for the tuned frequency.",
  "play-last": "Loop the latest saved recording. Tap again to return to live audio.",
};

let state = { display: "", power: false, volume: 0, squelch: 0, backlight: false };
let actionTail = Promise.resolve();
let pendingActions = 0;
let actionVersion = 0;
let frequencyEntry = "";
let editingFavorite = 0;
let wheelGesture;
let lockTimer;
let audioContext;
let audioReady;
let pcmOutput;
let volumeNode;
let audioStream;
let audioRetry;
let replayRequest;
let replayMessage = "";
let lastToneId;
let previousVolume = 0.32;
let hoveredTooltipTarget;
let focusedTooltipTarget;
let shownTooltipTarget;
let rdsMessage = "";
let recordingDirectory;
let folderRequest = 0;

function drawDigits(container, value, tunable = false) {
  const text = String(value ?? "").toUpperCase();
  const tuning = String(tunable);
  if (container.dataset.text === text && container.dataset.tunable === tuning) return;
  container.dataset.text = text;
  container.dataset.tunable = tuning;
  const decimalPoint = text.indexOf(".");
  const characters = [...text].map((character, index) => {
    const cell = document.createElement("span");
    if (character === "." || character === ":") {
      cell.className = character === "." ? "digit-dot" : "digit-colon";
    } else if (character in segmentMap) {
      cell.className = "digit";
      if (tunable && /^[0-9]$/.test(character)) {
        const place = decimalPoint - index - (index < decimalPoint ? 1 : 0);
        cell.dataset.stepHz = String(10 ** (6 + place));
      }
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

function formatMHz(hz) {
  return `${(hz / 1000000).toFixed(6).replace(/0{1,2}$/, "")} MHz`;
}

function signalDescription(next) {
  if (!Number.isFinite(next.signal_dbfs)) return "No signal measurement";
  const raw = Math.round(next.signal_dbfs);
  const usable = Math.round(next.signal_meter_dbfs ?? next.signal_dbfs);
  const offset = next.tuning_offset_hz;
  const description = [`RF power ${raw} dBFS`];
  if (usable <= -145) description.push("No usable signal detected");
  else if (raw - usable >= 2) description.push(`Usable signal ${usable} dBFS`);
  if (Number.isFinite(offset) && Math.abs(offset) >= 500) {
    const sign = offset < 0 ? "−" : "+";
    description.push(`Carrier offset ${sign}${(Math.abs(offset) / 1000).toFixed(1)} kHz`);
  }
  return description.join(" · ");
}

function tooltipMessage(target) {
  if (target === signalMeter) return signalDescription(state);
  if (target === lcd) {
    if (frequencyEntry || editingFavorite) return "";
    if (Number.isInteger(state.tune_hz) && (state.scan_active || state.search_active)) {
      return formatMHz(state.tune_hz);
    }
    return rdsMessage;
  }
  if (target.dataset.favorite !== undefined) {
    const index = Number(target.dataset.favorite) - 1;
    const favorite = state.favorites?.[index];
    const name = state.favorite_names?.[index];
    const use = favorite ? `${formatMHz(favorite.hz)} · Tap to recall or hold to replace.`
      : "Hold to save the tuned frequency.";
    const watching = state.watch_active?.includes(index + 1) ? "WATCH detected a signal." : "";
    return [name, use, "Right-click to name.", watching].filter(Boolean).join(" · ");
  }
  const control = target.dataset.setting ?? target.dataset.key ?? target.dataset.knob;
  if (control === "record") {
    return [controlHelp.record, state.record_message].filter(Boolean).join(" · ");
  }
  if (control === "play-last") {
    return [controlHelp[control], replayMessage].filter(Boolean).join(" · ");
  }
  if (control === "watch" && state.watch_receiver) {
    const receiver = state.watch_receiver === "secondary" ? "Using the second SDR." : "Using the main SDR.";
    return controlHelp.watch + " " + receiver;
  }
  return controlHelp[control] ?? "";
}

function updateTooltip() {
  const target = hoveredTooltipTarget ?? focusedTooltipTarget;
  const message = target ? tooltipMessage(target) : "";
  if (shownTooltipTarget && (shownTooltipTarget !== target || !message)) {
    shownTooltipTarget.removeAttribute("aria-describedby");
  }
  if (!message) {
    tooltip.hidden = true;
    shownTooltipTarget = undefined;
    return;
  }
  const changed = shownTooltipTarget !== target || tooltip.textContent !== message;
  shownTooltipTarget = target;
  target.setAttribute("aria-describedby", tooltip.id);
  tooltip.hidden = false;
  if (changed) {
    tooltip.textContent = message;
    positionTooltip();
  }
}

function positionTooltip() {
  if (tooltip.hidden) return;
  const anchor = shownTooltipTarget.getBoundingClientRect();
  const { width, height } = tooltip.getBoundingClientRect();
  const left = Math.max(8, Math.min(innerWidth - width - 8, anchor.left + (anchor.width - width) / 2));
  const top = anchor.top - height - 8;
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(8, Math.min(innerHeight - height - 8, top < 8 ? anchor.bottom + 8 : top))}px`;
}

for (const target of [lcd, signalMeter, ...keys, ...document.querySelectorAll("[data-knob]"), ...settings]) {
  target.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "touch") return;
    hoveredTooltipTarget = target;
    updateTooltip();
  });
  target.addEventListener("pointerleave", (event) => {
    if (hoveredTooltipTarget === target) {
      hoveredTooltipTarget = target === signalMeter && lcd.contains(event.relatedTarget) ? lcd : undefined;
    }
    updateTooltip();
  });
  target.addEventListener("focus", () => {
    if (target.matches(":focus-visible")) focusedTooltipTarget = target;
    updateTooltip();
  });
  target.addEventListener("blur", () => {
    if (focusedTooltipTarget === target) focusedTooltipTarget = undefined;
    updateTooltip();
  });
}

window.addEventListener("resize", positionTooltip);
window.addEventListener("scroll", positionTooltip, { passive: true });

function scrollingLine(selector) {
  const line = document.querySelector(selector);
  const copy = line.firstElementChild;
  let animation;
  let distance = 0;

  function update() {
    const overflow = line.hidden ? 0
      : Math.max(0, Math.ceil(copy.getBoundingClientRect().width - line.clientWidth));
    if (overflow === distance) return;
    distance = overflow;
    animation?.cancel();
    line.classList.toggle("is-scrolling", distance > 0);
    if (!distance) return;
    const pause = 1700;
    const duration = 2 * pause + distance * 1000 / 32;
    const end = `translateX(-${distance}px)`;
    animation = copy.animate([
      { transform: "translateX(0)" },
      { transform: "translateX(0)", offset: pause / duration },
      { transform: end, offset: 1 - pause / duration },
      { transform: end },
    ], { duration, iterations: Infinity });
  }

  new ResizeObserver(update).observe(line);
  return (text) => {
    if (copy.textContent === text) return;
    line.hidden = !text;
    copy.textContent = text;
    animation?.cancel();
    distance = 0;
    line.classList.remove("is-scrolling");
    update();
  };
}

const showStation = scrollingLine("#rds-station");
const showRadioText = scrollingLine("#rds-text");

function tunedFavoriteName(next) {
  const names = next.favorite_names;
  if (!names || !Number.isInteger(next.tune_hz)) return "";
  if (Number.isInteger(next.active_favorite)) return names[next.active_favorite - 1] ?? "";
  const match = next.favorites?.findIndex((entry, index) => entry?.hz === next.tune_hz && names[index]);
  return names[match] ?? "";
}

function renderRds(next, entering) {
  const showDetails = next.power && next.rds_enabled && !entering && !editingFavorite;
  const rds = showDetails ? next.rds : null;
  const favoriteName = showDetails ? tunedFavoriteName(next) : "";
  const pi = Number.isInteger(rds?.pi) ? rds.pi.toString(16).toUpperCase().padStart(4, "0") : rds?.pi;
  const station = favoriteName || rds?.ps?.trim() || (pi && !rds.text ? `PI ${pi}` : "");
  const text = rds?.text?.trim() ?? "";
  const nowPlaying = text && (/^now playing\b/i.test(text) ? text : `Now Playing: ${text}`);
  rdsMessage = [station, nowPlaying].filter(Boolean).join(" · ");
  rdsReadout.hidden = !rdsMessage;
  rdsToggle.setAttribute("aria-pressed", String(Boolean(next.rds_enabled)));
  showStation(station);
  showRadioText(nowPlaying);
}

function render(next) {
  if (typeof next.display !== "string") return;
  const previous = state;
  state = next;
  radio.classList.toggle("is-muted", Boolean(next.muted));
  muteKey.setAttribute("aria-pressed", String(Boolean(next.muted)));
  const measuredSignal = next.signal_meter_dbfs ?? next.signal_dbfs;
  const signal = Number.isFinite(measuredSignal) ? Math.round(measuredSignal) : null;
  const displayedSignal = signal === null ? -95 : Math.min(-20, Math.max(-95, signal));
  const litBars = Math.round((displayedSignal + 95) * signalBars.length / 75);
  signalMeter.hidden = !next.power;
  signalMeter.setAttribute("aria-valuenow", String(displayedSignal));
  signalMeter.setAttribute("aria-valuetext", signalDescription(next));
  signalBars.forEach((bar, index) => bar.classList.toggle("is-visible", index < litBars));
  watchToggle.setAttribute("aria-pressed", String(Boolean(next.watch_enabled)));
  recordToggle.setAttribute("aria-pressed", String(Boolean(next.record_enabled)));
  recordToggle.classList.toggle("is-recording", Boolean(next.record_active));
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
    const indicator = name === "NFM" && active.has("WFM") ? "WFM" : name;
    if (name === "NFM" && element.textContent !== indicator) element.textContent = indicator;
    element.classList.toggle("is-visible", active.has(indicator));
    element.classList.toggle("is-blinking",
      active.has(name) && (next.blink_bank === name || (name === "CLOSE" && next.close_blink)));
  }

  lcd.classList.toggle("is-unlit", !(next.backlight ?? next.power));
  renderRds(next, entering);
  const readout = entering ? frequencyEntry : next.display;
  lcd.setAttribute("aria-label", entering ? `${readout} MHz`
    : [readout.trim(), ...active, rdsMessage].filter(Boolean).join(" ") || "Scanner display");
  display.classList.toggle("is-blinking", !entering && Boolean(next.blink));
  display.classList.toggle("is-scrolling", !entering && Boolean(next.scan_scroll ?? next.display === "SCAN"));
  display.classList.toggle("is-clock", !next.power && next.display.includes(":"));
  const tunableDigits = !entering && canTune() && Number.isInteger(next.tune_hz)
    && /^[0-9]{1,4}\.[0-9]{1,6}$/.test(readout);
  drawDigits(display, readout, tunableDigits);
  drawDigits(channel, visibleChannel ? String(next.channel).padStart(2, "0") : "");

  for (const [index, button] of favorites.entries()) {
    const favorite = next.favorites?.[index];
    const tuned = Boolean(next.power && favorite && favorite.hz === next.tune_hz);
    button.classList.toggle("is-stored", Boolean(favorite));
    button.classList.toggle("is-tuned", tuned);
    button.classList.toggle("is-watched", !tuned && Boolean(next.watch_active?.includes(index + 1)));
  }
  updateTooltip();

  for (const dial of dials.values()) {
    if (!dial.drag && dial.pending === null && !dial.busy) {
      paintDial(dial, state[dial.name]);
    }
  }

  updateAudio(previous);
  lastRecording.volume = (state.volume || previousVolume) ** 1.45;
  lastRecording.muted = Boolean(state.muted);
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
    if (response.ok) {
      const next = await response.json();
      render(next);
      return next;
    }
  }).finally(() => {
    pendingActions -= 1;
  });
  return actionTail.catch(() => {});
}

function folderBusy(busy) {
  recordFolderDialog.setAttribute("aria-busy", String(busy));
  recordFolderUp.disabled = busy || !recordingDirectory?.parent;
  recordFolderOpen.disabled = busy;
  recordFolderSelect.disabled = busy;
  for (const button of recordFolderList.querySelectorAll("button")) button.disabled = busy;
}

async function browseRecordingFolder(path) {
  const request = ++folderRequest;
  folderBusy(true);
  recordFolderStatus.textContent = "Opening folder…";
  const next = await postAction({ record: "browse", path });
  if (!recordFolderDialog.open || request !== folderRequest) return;
  const directory = next?.record_browser;
  if (directory?.path && !directory.error) {
    recordingDirectory = directory;
    recordFolderPath.value = directory.path;
    const separator = directory.separator ?? "/";
    const prefix = directory.path.endsWith(separator) ? directory.path : directory.path + separator;
    recordFolderList.replaceChildren(...directory.folders.map((name) => {
      const row = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = name;
      button.addEventListener("click", () => void browseRecordingFolder(prefix + name));
      row.append(button);
      return row;
    }));
  }
  folderBusy(false);
  recordFolderStatus.textContent = directory?.error || (!directory ? "Could not open the server folder."
    : directory.truncated ? "More folders are available. Enter a path to open one."
    : !directory.writable ? "The scanner cannot write to this folder."
    : directory.folders.length ? "" : "No subfolders.");
}

async function selectRecordingFolder() {
  const request = ++folderRequest;
  folderBusy(true);
  recordFolderStatus.textContent = "Selecting folder…";
  const next = await postAction({ record: "select", path: recordFolderPath.value });
  if (!recordFolderDialog.open || request !== folderRequest) return;
  if (next?.record_selected) {
    recordFolderDialog.close();
    return;
  }
  folderBusy(false);
  recordFolderStatus.textContent = next?.record_browser?.error ?? "Could not select the server folder.";
}

recordFolderUp.addEventListener("click", () => {
  if (recordingDirectory?.parent) void browseRecordingFolder(recordingDirectory.parent);
});
recordFolderOpen.addEventListener("click", () => void browseRecordingFolder(recordFolderPath.value));
recordFolderSelect.addEventListener("click", () => void selectRecordingFolder());
recordFolderPath.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  if (!recordFolderOpen.disabled) void browseRecordingFolder(recordFolderPath.value);
});

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

function closeFavoriteEditor() {
  if (!editingFavorite) return;
  editingFavorite = 0;
  favoriteNameEditor.hidden = true;
  render(state);
}

favoriteNameEditor.addEventListener("blur", closeFavoriteEditor);
favoriteNameEditor.addEventListener("keydown", (event) => {
  if (event.isComposing || !["Enter", "Escape"].includes(event.key)) return;
  event.preventDefault();
  event.stopPropagation();
  const slot = editingFavorite;
  const name = favoriteNameEditor.value;
  closeFavoriteEditor();
  favorites[slot - 1]?.focus({ preventScroll: true });
  if (slot && event.key === "Enter") void postAction({ favorite: slot, name });
});

lcd.addEventListener("click", (event) => {
  if (event.target !== favoriteNameEditor) focusLCD();
});
lcd.addEventListener("wheel", (event) => {
  if (editingFavorite) return;
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
  const digit = event.target instanceof Element ? event.target.closest("[data-step-hz]") : null;
  const stepHz = digit ? Number(digit.dataset.stepHz) : undefined;
  const elapsed = wheelGesture ? event.timeStamp - wheelGesture.lastAt : Infinity;
  if (!wheelGesture || wheelGesture.direction !== direction || wheelGesture.mode !== mode
      || wheelGesture.stepHz !== stepHz || elapsed >= wheelQuietPeriod) {
    wheelGesture = { direction, mode, stepHz, distance: 0, stepped: false };
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
  void postAction({ tune: "step", direction, step_hz: stepHz });
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
  if (recordFolderDialog.open || !canTune() || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
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

function sendTap({ key, favorite }) {
  if (key === "play-last") {
    void toggleReplay();
  } else if (key === "record") {
    flushLockTap();
    void postAction({ record: "toggle" });
  } else if (favorite !== undefined) {
    flushLockTap();
    void postAction({ favorite: Number(favorite), gesture: "tap" });
  } else if (key !== "lock") {
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
  if (["lock", "close", "prog", "record"].includes(key)) return 2510;
  return null;
}

function beginPress(id, element) {
  if (presses.has(id)) return;
  const { key, favorite } = element.dataset;
  const press = { element, key, favorite, handled: false };
  const recordHz = key === "record" ? state.tune_hz : null;
  presses.set(id, press);
  element.classList.add("is-pressed");
  if (key !== "play-last") void enableAudio();
  const delay = favorite === undefined ? longPressDelay(key) : favoriteHoldDelay;
  if (delay !== null) {
    press.timer = setTimeout(() => {
      press.handled = true;
      flushLockTap();
      if (key === "record") {
        if (Number.isInteger(recordHz)) void postAction({ record: "delete", hz: recordHz });
        return;
      }
      const repeat = () => {
        if (!pendingActions) void postAction({ key, gesture: "long" });
      };
      void postAction(favorite === undefined ? { key, gesture: "long" }
        : { favorite: Number(favorite), gesture: "long" });
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
  if (!cancelled && !press.handled) sendTap(press);
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
  button.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (event.pointerType === "touch" || event.sourceCapabilities?.firesTouchEvents) return;
    if (button === recordToggle) {
      if (!recordFolderDialog.open) {
        recordingDirectory = undefined;
        recordFolderPath.value = "";
        recordFolderList.replaceChildren();
        recordFolderDialog.showModal();
        void browseRecordingFolder();
      }
      return;
    }
    if (button.dataset.favorite === undefined) return;
    editingFavorite = Number(button.dataset.favorite);
    frequencyEntry = "";
    const label = button.querySelector(".button-label").textContent;
    favoriteNameEditor.setAttribute("aria-label", `Name for ${label}`);
    favoriteNameEditor.placeholder = `${label} name`;
    favoriteNameEditor.value = state.favorite_names?.[editingFavorite - 1] ?? "";
    favoriteNameEditor.hidden = false;
    render(state);
    favoriteNameEditor.focus({ preventScroll: true });
    favoriteNameEditor.select();
  });
  button.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    if (!event.repeat) beginPress("keyboard:" + event.key, button);
  });
  button.addEventListener("click", (event) => {
    if (event.detail === 0) {
      if (button !== playLast) void enableAudio();
      sendTap(button.dataset);
    }
  });
}

for (const button of settings) {
  button.addEventListener("click", () => {
    void enableAudio();
    flushLockTap();
    void postAction({ setting: button.dataset.setting });
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
  const percent = Number((dial.value * 100).toFixed(dial.name === "squelch" ? 1 : 0));
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
    const step = dial.name === "squelch" ? 0.005 : 0.04;
    changeDial(dial, dial.value - Math.sign(event.deltaY) * step);
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

async function createPcmOutput(context) {
  if (context.audioWorklet && typeof AudioWorkletNode === "function") {
    try {
      await context.audioWorklet.addModule(new URL("pcm-worklet.js", import.meta.url));
      const node = new AudioWorkletNode(context, "scanner-pcm", {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { inputRate: audioRate },
      });
      return {
        node,
        enqueue: samples => node.port.postMessage(samples, [samples.buffer]),
        reset: () => node.port.postMessage("clear"),
      };
    } catch (error) {
      console.debug("Scanner AudioWorklet unavailable", error);
    }
  }

  // Plain HTTP on a private LAN is not a secure context, so it has no AudioWorklet.
  const pcm = new PcmBuffer(audioRate, context.sampleRate);
  const node = context.createScriptProcessor(1024, 0, 1);
  node.onaudioprocess = ({ outputBuffer }) => pcm.readInto(outputBuffer.getChannelData(0));
  return {
    node,
    enqueue: samples => pcm.enqueue(samples),
    reset: () => pcm.reset(),
  };
}

async function enableAudio() {
  try {
    audioContext ??= new AudioContext({ latencyHint: "interactive" });
    audioReady ??= createPcmOutput(audioContext).then(output => {
      volumeNode = audioContext.createGain();
      volumeNode.gain.value = 0;
      output.node.connect(volumeNode).connect(audioContext.destination);
      pcmOutput = output;
    }).catch(error => {
      audioReady = undefined;
      throw error;
    });
    await Promise.all([audioContext.resume(), audioReady]);
    updateAudio();
  } catch (error) {
    console.error("Scanner audio could not start", error);
  }
}

function stopReplay(message = "") {
  replayRequest?.abort();
  replayRequest = undefined;
  replayMessage = message;
  lastRecording.pause();
  lastRecording.removeAttribute("src");
  lastRecording.load();
  playLast.setAttribute("aria-pressed", "false");
  updateTooltip();
  if (state.power) void enableAudio();
}

async function toggleReplay() {
  if (replayRequest) {
    stopReplay();
    return;
  }
  const request = new AbortController();
  replayRequest = request;
  replayMessage = "Opening the latest recording…";
  playLast.setAttribute("aria-pressed", "true");
  updateTooltip();
  updateAudio();
  try {
    const response = await fetch("/api/recordings/last", { cache: "no-store", signal: request.signal });
    if (response.status === 404) {
      if (replayRequest === request) stopReplay("No saved recordings in the current folder.");
      return;
    }
    if (!response.ok) throw new Error("Recording lookup failed");
    const clip = await response.json();
    if (replayRequest !== request) return;
    if (typeof clip.url !== "string" || !clip.url.startsWith("/api/recordings/")) {
      throw new Error("Recording URL unavailable");
    }
    lastRecording.src = clip.url;
    await lastRecording.play();
    if (replayRequest === request) {
      replayMessage = clip.label || "Playing the latest recording.";
      updateTooltip();
    }
  } catch {
    if (replayRequest === request) stopReplay("Could not play the recording.");
  }
}

lastRecording.addEventListener("error", () => {
  if (replayRequest && lastRecording.error) stopReplay("Could not play the recording.");
});

function updateAudio(previous = state) {
  if (!pcmOutput) return;
  const live = state.power && !replayRequest;
  const gain = live && !state.muted ? state.volume ** 1.45 : 0;
  volumeNode.gain.setTargetAtTime(gain, audioContext.currentTime, 0.015);
  if (state.tune_hz !== previous.tune_hz || state.muted !== previous.muted
      || (!state.audio_open && previous.audio_open)) {
    pcmOutput.reset();
  }
  if (live && audioContext.state === "running") {
    if (!audioStream && !audioRetry) void streamAudio();
  } else {
    audioStream?.abort();
    clearTimeout(audioRetry);
    audioRetry = undefined;
    pcmOutput.reset();
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
      if (samples.length) pcmOutput.enqueue(samples);
    }
  } catch (error) {
    if (error.name !== "AbortError") console.debug(error.message);
  } finally {
    if (audioStream === controller) audioStream = undefined;
    if (state.power && !replayRequest && audioContext.state === "running") {
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
  if (!volumeNode || !state.power || state.muted || replayRequest
      || audioContext.state !== "running") return;
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
  replayRequest?.abort();
  lastRecording.pause();
  audioStream?.abort();
  clearTimeout(audioRetry);
});
void poll();
