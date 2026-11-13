import { PcmBuffer } from "./pcm-buffer.js";
import { Waterfall } from "./waterfall.js";

const radio = document.querySelector(".radio");
const mobileAdvancedToggle = document.querySelector("#mobile-advanced-toggle");
const mobilePanel = matchMedia("(max-width: 960px) and (pointer: coarse)");
const favoriteBank = document.querySelector("#favorite-bank");
const mobileKeypad = document.querySelector("#mobile-keypad");
const settingsBank = radio.querySelector(".settings-bank");
const lcd = document.querySelector("#lcd");
const waterfall = new Waterfall(document.querySelector(".waterfall"));
const waterfallPlot = waterfall.canvas;
const signalMeter = document.querySelector("#signal-meter");
const signalBars = signalMeter.querySelectorAll("[data-signal-bar]");
const display = document.querySelector("#display");
const pinStatus = document.querySelector("#pin-status");
const progKey = document.querySelector('[data-key="prog"]');
const toneReadout = document.querySelector("#tone-readout");
const toneKey = document.querySelector('[data-key="tone"]');
const toneFilterDialog = document.querySelector("#tone-filter-dialog");
const toneFilterForm = document.querySelector("#tone-filter-form");
const toneFilterChannel = document.querySelector("#tone-filter-channel");
const toneFilterKind = document.querySelector("#tone-filter-kind");
const toneFilterHz = document.querySelector("#tone-filter-hz");
const toneFilterCode = document.querySelector("#tone-filter-code");
const toneFilterPolarity = document.querySelector("#tone-filter-polarity");
const toneFilterDetected = document.querySelector("#tone-filter-detected");
const toneFilterStatus = document.querySelector("#tone-filter-status");
const toneFilterSave = document.querySelector("#tone-filter-save");
const channel = document.querySelector("#channel");
const legends = document.querySelectorAll("[data-lcd]");
const rdsToggle = document.querySelector("#rds-toggle");
const scanToggle = document.querySelector("#scan-toggle");
const dabToggle = document.querySelector("#dab-toggle");
const watchToggle = document.querySelector("#watch-toggle");
const recordToggle = document.querySelector("#record-toggle");
const discoveryToggle = document.querySelector("#discovery-toggle");
const instantReplay = document.querySelector("#instant-replay");
const discoveryPad = document.querySelector("#discovery-pad");
const discoveryClose = document.querySelector("#discovery-close");
const discoveryLogToggle = document.querySelector("#discovery-log-toggle");
const discoveryCurrent = document.querySelector("#discovery-current");
const discoveryCurrentHz = document.querySelector("#discovery-current-hz");
const discoveryCurrentMode = document.querySelector("#discovery-current-mode");
const discoveryCurrentTone = document.querySelector("#discovery-current-tone");
const discoveryStatus = document.querySelector("#discovery-status");
const discoveryHits = document.querySelector("#discovery-hits");
const discoveryPageUp = document.querySelector("#discovery-page-up");
const discoveryPageDown = document.querySelector("#discovery-page-down");
const discoveryPageNumber = document.querySelector("#discovery-page-number");
const discoveryMobileReplay = document.querySelector("#discovery-mobile-replay");
const playLast = document.querySelector("#play-last");
const lastRecording = document.querySelector("#last-recording");
const recordFolderDialog = document.querySelector("#record-folder-dialog");
const recordFolderPath = document.querySelector("#record-folder-path");
const recordFolderList = document.querySelector("#record-folder-list");
const recordFolderStatus = document.querySelector("#record-folder-status");
const recordFolderUp = document.querySelector("#record-folder-up");
const recordFolderOpen = document.querySelector("#record-folder-open");
const recordFolderSelect = document.querySelector("#record-folder-select");
const settings = document.querySelectorAll('[data-setting]:not([data-setting="dab"])');
const rdsReadout = document.querySelector("#rds-readout");
const favoriteNameEditor = document.querySelector("#favorite-name");
const muteKey = document.querySelector('[data-key="mute"]');
const saveVolumeKey = document.querySelector('[data-key="save-vol"]');
const volumeOffsetDialog = document.querySelector("#volume-offset-dialog");
const volumeOffsetValue = document.querySelector("#volume-offset-value");
const tooltip = document.querySelector("#panel-tooltip");
const favoriteKeyDescription = "Press and hold to save the tuned frequency. Open the context menu to name this favourite.";
function panelButton(text) {
  const button = document.createElement("button");
  const label = document.createElement("span");
  const cap = document.createElement("span");
  button.type = "button";
  button.className = "extension-button";
  label.className = "button-label";
  label.textContent = text;
  cap.className = "button-cap";
  cap.setAttribute("aria-hidden", "true");
  button.append(label, cap);
  return button;
}

const favorites = Array.from({ length: 24 }, (_, index) => {
  const label = `F${String(index + 1).padStart(2, "0")}`;
  const button = panelButton(label);
  button.dataset.favorite = String(index + 1);
  button.setAttribute("aria-description", favoriteKeyDescription);
  return button;
});
favoriteBank.append(...favorites);
const digitKeys = mobileKeypad ? Array.from("1234567890", digit => {
  const button = panelButton(digit === "0" ? "0 - ." : digit);
  button.dataset.digit = digit;
  if (digit === "0") {
    button.setAttribute("aria-label", "0");
    button.setAttribute("aria-description", "Hold for a decimal point.");
  } else {
    button.setAttribute("aria-description", `Hold to recall F0${digit}.`);
  }
  return button;
}) : [];
if (mobileKeypad) {
  mobileKeypad.prepend(...digitKeys.slice(0, 9));
  mobileKeypad.append(digitKeys[9]);
}
const keys = [...document.querySelectorAll("[data-key]"), ...favorites, ...digitKeys, dabToggle];
const presetBanks = {
  business: "BUS", amateur: "HAM", fmradio: "BCAST", rail: "RAIL", events: "EVT", utilities: "UTIL",
};
const presetKeys = document.querySelectorAll(".preset-key");
const dials = new Map();
const presses = new Map();
const audioRate = 24000;
const maxBoostDb = 12;
const maxBoostGain = 10 ** (maxBoostDb / 20);
const maxVolumeOffsetDb = 12;
const maxLiveGain = 10 ** ((maxBoostDb + maxVolumeOffsetDb) / 20);
const defaultVolume = 0.32;
const savedListenerVolumeKey = "sdr-scanner:audio:volume";
const scannerSessionKey = "sdr-scanner:session";
const favoriteHoldDelay = 850;
const keypadTuneDelay = 1400;
const lockInterval = 280;
const pinLength = 6;
const progDoubleTapInterval = 500;
const wheelQuietPeriod = 110;
const wheelPixelThreshold = 48;
const wheelDetentPixels = 80;
const wheelDetentInterval = 32;
const frequencyReadout = /^[0-9]{1,4}\.[0-9]{1,6}$/;
const previousScannerSession = loadScannerSession();
let scannerSession = previousScannerSession?.token ?? newScannerSession();

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

const legendNames = {
  PVT: "Private",
  FAV: "Favourites",
  PMR: "PMR 446",
  AIR: "Airband",
  MRN: "Marine",
  CB: "CB Radio",
  BUS: "Business Radio",
  HAM: "Amateur Radio",
  BCAST: "FM Radio",
  RAIL: "Railway Radio",
  EVT: "Event Radio",
  UTIL: "Utility Radio",
  AM: "AM",
  NFM: "Narrow FM",
  WFM: "Wide FM",
  USB: "Upper Sideband",
  LSB: "Lower Sideband",
  DAB: "DAB",
  CH: "Channel",
  CLOSE: "Close Call",
  HOLD: "Hold",
  SCAN: "Scan",
  SEARCH: "Search",
  "▲": "Search Up",
  "▼": "Search Down",
  T: "Temporary Lockout",
  "L/O": "Lockout",
};

const controlHelp = {
  hold: "Tap to pause or resume scanning. Hold to browse saved favourites with UP and DOWN.",
  up: "Scan or search forwards. On HOLD, step to the next channel or frequency.",
  down: "Scan or search backwards. On HOLD, step to the previous channel or frequency.",
  mute: "Toggle audio and panel LEDs. The LCD stays on.",
  fav: "Toggle scanning of the FAV memories. Hold to delete favourites.",
  pmr: "Toggle the PMR446 zone with all 16 analogue channels.",
  close: "Cycle nearby-signal modes. Hold for Close Call Only.",
  air: "Cycle the Air and Marine zones, Air only, Marine only, and off.",
  cb: "Cycle the CB AM and FM zones.",
  business: "Tap to scan the UK Business zone. Hold to search the full business radio ranges.",
  amateur: "Tap to scan the UK Amateur zone. Hold to search the full amateur allocations.",
  fmradio: "Toggle the UK FM broadcast zone.",
  rail: "Toggle the UK railway and tram voice zone.",
  events: "Toggle the UK venue and event voice zone.",
  utilities: "Toggle the UK water and power utility voice zone.",
  scan: "Start or resume scanning. Hold to select saved analogue favourites to scan.",
  search: "Sweep the selected range or start a full-range search.",
  sideband: "Cycle USB, LSB and Auto for manual tuning and Search.",
  tone: "Tap to detect CTCSS or DCS. Hold, then tap a saved narrow-FM favourite to set its squelch tone.",
  lock: "Tap to skip. Double-tap to lock out permanently. Hold to clear temporary skips and unlock the current channel.",
  band: "Choose a search band. While searching Amateur or Business, cycle through its ranges. Hold to change the frequency step.",
  prog: "Program a memory. Hold to toggle Program Lock.",
  "save-vol": "Save this channel’s listening settings in this browser. Hold or right-click to set a volume offset.",
  squelch: "Set how strong a signal must be to open audio. Scroll for 0.5 dB steps.",
  boost: "Drag or scroll to raise live audio in this browser.",
  volume: "Click to switch between radio and clock. Drag or scroll to adjust volume.",
  rds: "Toggle station names and text from FM RDS and DAB.",
  dab: "Tap to switch between DAB and analogue. Hold to rescan for local DAB stations.",
  watch: "Tap to watch F01–F24. Hold to enter or leave dedicated monitoring. A flashing amber LED marks activity.",
  record: "Tap to arm or stop recording. Right-click to choose a folder. Hold to delete recordings for the tuned station or frequency.",
  discovery: "Open or close the Discovery Log of received signals.",
  replay: "Play the last 30 seconds of received audio. Tap again to return to live audio.",
  "play-last": "Loop the latest saved recording. Tap again to return to live audio.",
};

let state = { display: "", power: false, volume: 0, squelch: 0, backlight: false };
let actionTail = Promise.resolve();
let pendingActions = 0;
let actionVersion = 0;
let pageActive = true;
let awaitingState = Boolean(previousScannerSession);
let access;
let pinMode;
let pinDigits = "";
let pinConfirmation = "";
let pinMessage = "";
let pinBusy = false;
let lastProgTap = -Infinity;
let accessCheck;
let frequencyEntry = "";
let keypadEntry = false;
let keypadTuneTimer;
let editingFavorite = 0;
let wheelGesture;
let lockTimer;
let audioContext;
let audioReady;
let pcmOutput;
let volumeNode;
let liveGainNode;
let audioStream;
let audioRetry;
let replayRequest;
let replaySource;
let replayObjectUrl;
let replayMessage = "";
let discoveryMessage = "";
let discoveryRefresh;
let discoveryRequest;
let discoveryOpener;
let discoveryRows = "";
let discoveryEntries = [];
let discoveryPage = 0;
const discoveryPageSize = 10;
let discoveryPageHead;
let discoveryBusy = false;
let discoveryLoadError = false;
const discoverySaves = new Map();
let lastToneId;
let listenerVolume = previousScannerSession?.volume ?? loadListenerVolume();
let listenerBoost = 0;
let listeningProfileKey;
let listeningProfile;
let offsetDialogKey;
let choosingToneFavorite = false;
let toneChoiceMessage = "";
let toneFilterTarget;
let saveVolumeMessage = "";
let saveVolumeTimer;
let hoveredTooltipTarget;
let focusedTooltipTarget;
let shownTooltipTarget;
let waterfallPointer;
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
  return !pinMode && (state.can_tune ?? state.power);
}

function canEnterFrequency() {
  return !pinMode && (canTune() || (radio.classList.contains("is-mobile-slim") && state.power
    && state.dab_enabled && !state.dab_programming));
}

function canTuneWaterfall() {
  return Boolean(!pinMode && state.power && state.waterfall && !editingFavorite
    && (state.dab_enabled ? !state.dab_programming : canTune()));
}

function newScannerSession() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, "0")).join("");
}

function loadScannerSession() {
  if (performance.getEntriesByType("navigation")[0]?.type !== "reload") return;
  try {
    const saved = JSON.parse(sessionStorage.getItem(scannerSessionKey));
    if (typeof saved?.token !== "string" || !/^[0-9a-f]{32}$/.test(saved.token)) return;
    const volume = typeof saved.volume === "number" && saved.volume > 0 && saved.volume <= 1
      ? saved.volume : undefined;
    return { token: saved.token, volume };
  } catch {
    // Storage may be unavailable.
  }
}

function rememberScannerSession() {
  try {
    sessionStorage.setItem(scannerSessionKey, JSON.stringify({ token: scannerSession, volume: listenerVolume }));
    return true;
  } catch {
    return false;
  }
}

function sessionUrl(path, radioId) {
  const url = new URL(path, window.location.href);
  url.searchParams.set("session", scannerSession);
  if (Number.isInteger(radioId)) url.searchParams.set("radio", String(radioId));
  return url.pathname + url.search;
}

function canProgramPin() {
  return access?.can_program_pin && !state.power && !state.blink && /^\d{1,2}:\d{2}$/.test(state.display);
}

function pinKeyDigit({ digit, favorite }) {
  if (digit !== undefined) return digit;
  const slot = Number(favorite);
  if (slot >= 1 && slot <= 10) return String(slot % 10);
}

function pinInstructions() {
  if (pinMessage) return pinMessage;
  if (pinMode === "unlock" && !access?.pin_configured) return "Set the PIN on the scanner host first.";
  if (pinMode === "saved") return "Remote PIN saved.";
  const action = pinMode === "confirm" ? "Enter the new PIN again." : pinMode === "program"
    ? "Enter a new six-digit PIN." : "Enter the six-digit PIN to unlock the radio.";
  const keys = radio.classList.contains("is-mobile-slim") ? "0–9" : "F01–F09 for 1–9, F10 for 0";
  const progress = pinDigits ? ` ${pinDigits.length} of ${pinLength} digits entered.` : "";
  return `${action} ${keys}. Press PROG to confirm. L/O deletes a digit.${progress}`;
}

function renderPinStatus() {
  const status = pinMode ? pinInstructions() : "";
  const description = status || "Scroll to tune, or type a frequency in MHz and press Enter.";
  if (lcd.getAttribute("aria-description") !== description) lcd.setAttribute("aria-description", description);
  pinStatus.hidden = !pinMode;
  if (pinStatus.textContent !== status) pinStatus.textContent = status;
}

function updatePinControls() {
  const hasKeypad = pinMode && pinMode !== "saved" && (pinMode !== "unlock" || access?.pin_configured);
  for (const element of [...keys, ...settings, ...[...dials.values()].map(dial => dial.element), mobileAdvancedToggle]) {
    const { key } = element.dataset;
    const digit = pinKeyDigit(element.dataset);
    const confirmsPin = key === "prog" || element === mobileAdvancedToggle && mobilePanel.matches;
    const available = hasKeypad && (digit !== undefined || confirmsPin
      || key === "lock" || key === "hold" && pinMode !== "unlock");
    element.inert = Boolean(pinMode) && !available;
    if (element.dataset.favorite !== undefined && digit !== undefined) {
      if (pinMode) {
        element.setAttribute("aria-label", `F${element.dataset.favorite.padStart(2, "0")}, enter ${digit}`);
        element.removeAttribute("aria-description");
      } else {
        element.removeAttribute("aria-label");
        element.setAttribute("aria-description", favoriteKeyDescription);
      }
    }
  }
}

function setPinMode(mode) {
  pinMode = mode;
  radio.classList.toggle("is-pin-entry", Boolean(pinMode));
  updateMobilePanel();
}

function exitPin() {
  pinDigits = "";
  pinConfirmation = "";
  pinMessage = "";
  setPinMode(undefined);
  render(state);
}

function applyAccess(next) {
  if (typeof next?.pin_required !== "boolean") return;
  const wasConfigured = access?.pin_configured;
  access = next;
  if (next.pin_required) {
    awaitingState = true;
    if (pinMode !== "unlock") {
      actionVersion += 1;
      pinDigits = "";
      pinConfirmation = "";
      pinMessage = "";
      setPinMode("unlock");
      render({ display: "", power: false, volume: 0, squelch: 0, backlight: true, radio_id: null });
      lcd.focus({ preventScroll: true });
    } else if (wasConfigured !== next.pin_configured) {
      pinDigits = "";
      pinMessage = "";
      updatePinControls();
      render(state);
    }
  } else if (pinMode === "unlock") {
    exitPin();
  }
}

function enterPinDigit(digit) {
  if (pinBusy || pinMode === "saved" || pinMode === "unlock" && !access?.pin_configured) return;
  if (digit !== undefined && pinDigits.length === pinLength) return;
  pinDigits = digit === undefined ? pinDigits.slice(0, -1) : pinDigits + digit;
  pinMessage = "";
  render(state);
}

async function submitPin() {
  if (pinBusy || pinMode === "saved" || pinMode === "unlock" && !access?.pin_configured) return;
  if (pinDigits.length !== pinLength) {
    pinMessage = "Enter all six digits, then press PROG.";
    render(state);
    return;
  }
  if (pinMode === "program") {
    pinConfirmation = pinDigits;
    pinDigits = "";
    pinMessage = "";
    setPinMode("confirm");
    render(state);
    return;
  }
  const programming = pinMode === "confirm";
  if (programming && pinConfirmation !== pinDigits) {
    pinConfirmation = "";
    pinDigits = "";
    pinMessage = "The PINs differ. Enter the new PIN again.";
    setPinMode("program");
    render(state);
    return;
  }
  const session = scannerSession;
  const body = JSON.stringify(programming ? { new_pin: pinDigits } : { pin: pinDigits });
  pinConfirmation = "";
  pinDigits = "";
  pinBusy = true;
  pinMessage = programming ? "Saving PIN…" : "Checking PIN…";
  actionVersion += 1;
  render(state);
  try {
    const response = await fetch(sessionUrl("/api/access"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    const result = await response.json().catch(() => null);
    if (!pageActive || session !== scannerSession) return;
    if (response.ok && result?.pin_required === false) {
      applyAccess(result);
      if (programming) {
        pinMessage = "";
        setPinMode("saved");
        setTimeout(() => { if (pinMode === "saved") exitPin(); }, 1500);
      }
    } else {
      if (programming) setPinMode("program");
      pinMessage = response.status === 429 ? "Too many attempts. Wait and try again."
        : programming ? "Could not save the PIN. Try again." : "Incorrect PIN. Try again.";
    }
  } catch {
    if (pageActive && session === scannerSession) {
      if (programming) setPinMode("program");
      pinMessage = "Connection interrupted. Enter the PIN again.";
    }
  } finally {
    pinBusy = false;
    if (pageActive && session === scannerSession) render(state);
  }
}

function formatMHz(hz) {
  return `${(hz / 1000000).toFixed(6).replace(/0{1,2}$/, "")} MHz`;
}

function formatTone(tone) {
  if (tone?.kind === "CTCSS") return `CTCSS ${tone.hz.toFixed(1)} Hz`;
  if (tone?.kind === "DCS") return `DCS ${tone.code}${tone.polarity}`;
  return "";
}

function setToneFavoriteChoice(choosing, message = "") {
  choosingToneFavorite = choosing;
  toneChoiceMessage = message;
  renderTone();
  updateTooltip();
}

function renderTone() {
  const filter = state.tone_filter;
  let text = "";
  if (choosingToneFavorite) {
    text = toneChoiceMessage || "TAP A NFM FAVOURITE";
  } else if (filter) {
    const status = state.tone_matched === true ? "MATCH"
      : state.tone_matched === false ? (state.channel_tone ? "NO MATCH" : "NO TONE") : "WAIT";
    text = `${formatTone(filter)} · ${status}`;
  } else {
    text = formatTone(state.channel_tone) || (state.tone_searching ? "TONE SEARCH" : "");
  }
  if (toneReadout.textContent !== text) toneReadout.textContent = text;
  toneReadout.hidden = Boolean(frequencyEntry || editingFavorite || !text);
  toneKey.setAttribute("aria-pressed", String(Boolean(state.tone_enabled)));
  toneKey.classList.toggle("is-selecting", choosingToneFavorite);
}

function detectedToneForFavorite() {
  return toneFilterTarget && !state.dab_enabled && state.modulation === "FM"
    && state.tune_hz === toneFilterTarget.hz ? state.channel_tone : null;
}

function renderToneFilterFields() {
  const kind = toneFilterKind.value;
  const ctcss = kind === "CTCSS";
  const dcs = kind === "DCS";
  toneFilterHz.parentElement.hidden = toneFilterHz.disabled = !ctcss;
  toneFilterCode.parentElement.hidden = toneFilterCode.disabled = toneFilterPolarity.disabled = !dcs;
}

function fillToneFilter(tone) {
  toneFilterKind.value = tone?.kind ?? "";
  toneFilterHz.value = tone?.hz ?? "";
  toneFilterCode.value = tone?.code ?? "";
  toneFilterPolarity.value = tone?.polarity ?? "N";
  renderToneFilterFields();
}

function openToneFilter(favorite) {
  const entry = state.favorites?.[favorite - 1];
  if (entry?.mode !== "FM") {
    setToneFavoriteChoice(true, "CHOOSE A SAVED NFM SLOT");
    return;
  }
  setToneFavoriteChoice(false);
  toneFilterTarget = { favorite, hz: entry.hz };
  const label = favorites[favorite - 1].querySelector(".button-label").textContent;
  toneFilterChannel.textContent = [label, state.favorite_names?.[favorite - 1], formatMHz(entry.hz)]
    .filter(Boolean).join(" · ");
  toneFilterStatus.textContent = "";
  toneFilterSave.disabled = false;
  toneFilterDetected.hidden = !detectedToneForFavorite();
  fillToneFilter(entry.tone);
  toneFilterDialog.showModal();
}

toneFilterKind.addEventListener("change", renderToneFilterFields);
document.querySelector("#tone-filter-cancel").addEventListener("click", () => toneFilterDialog.close());
toneFilterDetected.addEventListener("click", () => {
  const tone = detectedToneForFavorite();
  if (tone) fillToneFilter(tone);
});

toneFilterForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!toneFilterTarget || toneFilterSave.disabled) return;
  const target = toneFilterTarget;
  const kind = toneFilterKind.value;
  const tone = kind === "CTCSS" ? { kind, hz: toneFilterHz.valueAsNumber }
    : kind === "DCS" ? { kind, code: toneFilterCode.value, polarity: toneFilterPolarity.value } : null;
  toneFilterStatus.textContent = "Saving…";
  toneFilterSave.disabled = true;
  const next = await postAction({ ...target, tone });
  if (!toneFilterDialog.open || target !== toneFilterTarget) return;
  toneFilterSave.disabled = false;
  if (next?.tone_filter_saved) toneFilterDialog.close();
  else toneFilterStatus.textContent = "Could not save. Check the tone or code and try again.";
});

toneFilterDialog.addEventListener("close", () => {
  toneFilterTarget = undefined;
});

function loadListenerVolume() {
  try {
    const volume = Number(localStorage.getItem(savedListenerVolumeKey));
    if (volume > 0 && volume <= 1) return volume;
  } catch {
    // Storage may be unavailable.
  }
}

function profileKey(next) {
  if (!next.power || !Number.isInteger(next.tune_hz) || next.watch_only
      || next.scan_active || next.search_active || next.dab_rescanning || next.dab_tuning
      || next.dab_programming) return;
  if (next.dab_enabled) {
    const sid = next.dab_recording_identity?.sid;
    if (sid) return `dab:${next.tune_hz}:${sid}`;
    return;
  }
  if (next.can_tune === false) return;
  return `rf:${next.tune_hz}`;
}

function loadListeningProfile(next) {
  const key = profileKey(next);
  if (key === listeningProfileKey) return;
  listeningProfileKey = key;
  listeningProfile = undefined;
  if (!key) return;
  try {
    const profile = JSON.parse(localStorage.getItem(`sdr-scanner:audio:${key}`));
    if (profile?.volume > 0 && [profile.volume, profile.squelch, profile.boost].every(value =>
      Number.isFinite(value) && value >= 0 && value <= 1)
        && Number.isFinite(profile.offset) && Math.abs(profile.offset) <= maxVolumeOffsetDb) {
      listeningProfile = profile;
    }
  } catch {
    // Storage may be unavailable or contain a damaged entry.
  }
}

function dialValue(name) {
  if (name === "volume") return state.power ? listeningProfile?.volume ?? listenerVolume ?? state.volume : 0;
  if (name === "boost") return listeningProfile?.boost ?? listenerBoost;
  return listeningProfile?.squelch ?? state.squelch;
}

function showSaveVolumeMessage(message, saved = false) {
  saveVolumeMessage = message;
  saveVolumeKey.classList.toggle("is-confirmed", saved);
  clearTimeout(saveVolumeTimer);
  saveVolumeTimer = setTimeout(() => {
    saveVolumeMessage = "";
    saveVolumeKey.classList.remove("is-confirmed");
    updateTooltip();
  }, 1800);
  updateTooltip();
}

function saveListeningProfile(offset = listeningProfile?.offset ?? 0) {
  const volume = dials.get("volume").value;
  if (!listeningProfileKey || volume <= 0) {
    showSaveVolumeMessage("Hold a frequency or tune a DAB service first.");
    return;
  }
  const profile = { volume, squelch: dials.get("squelch").value, boost: dials.get("boost").value, offset };
  try {
    localStorage.setItem(`sdr-scanner:audio:${listeningProfileKey}`, JSON.stringify(profile));
    localStorage.setItem(savedListenerVolumeKey, String(volume));
  } catch {
    showSaveVolumeMessage("This browser could not save the listening settings.");
    return;
  }
  listenerVolume = volume;
  listeningProfile = profile;
  updateAudioGain();
  showSaveVolumeMessage("Listening settings saved for this channel.", true);
}

function editVolumeOffset() {
  if (!listeningProfileKey) {
    showSaveVolumeMessage("Hold a frequency or tune a DAB service first.");
    return;
  }
  if (volumeOffsetDialog.open) return;
  offsetDialogKey = listeningProfileKey;
  volumeOffsetDialog.returnValue = "";
  volumeOffsetValue.value = listeningProfile?.offset ?? 0;
  volumeOffsetDialog.showModal();
  volumeOffsetValue.focus();
  volumeOffsetValue.select();
}

volumeOffsetDialog?.addEventListener("close", () => {
  if (volumeOffsetDialog.returnValue !== "save") return;
  if (offsetDialogKey !== listeningProfileKey) {
    showSaveVolumeMessage("Tuning changed. Open SAVE VOL again for the new channel.");
    return;
  }
  const offset = volumeOffsetValue.valueAsNumber;
  if (Number.isFinite(offset) && Math.abs(offset) <= maxVolumeOffsetDb) saveListeningProfile(offset);
});

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
  if (pinMode) {
    if (target === lcd) return pinInstructions();
    if (target.inert) return "";
    const digit = pinKeyDigit(target.dataset);
    if (digit !== undefined) return `Enter ${digit}.`;
    if (target === mobileAdvancedToggle || target.dataset.key === "prog") return "Confirm PIN.";
    if (target.dataset.key === "lock") return "Delete one digit.";
    if (target.dataset.key === "hold") return "Cancel PIN setup.";
    return "";
  }
  if (target === progKey && canProgramPin()) return "Double-tap to set or change the remote PIN. Hold to set the clock.";
  if (target === mobileAdvancedToggle) {
    return radio.classList.contains("is-mobile-slim") ? "Show the full radio panel." : "Show only the essential controls.";
  }
  if (target.dataset.digit !== undefined) {
    return [`Tap to enter ${target.dataset.digit}.`, target.getAttribute("aria-description")]
      .filter(Boolean).join(" ");
  }
  if (target === waterfallPlot) {
    const hz = waterfall.frequencyAt(waterfallPointer?.x);
    return hz === null ? "" : formatMHz(hz);
  }
  if (target === toneReadout || target === toneKey) {
    const tone = state.channel_tone;
    const detected = tone?.alias ? `Detected ${formatTone(tone)} or ${tone.alias}${tone.alias_polarity}.` : "";
    if (choosingToneFavorite) return "Tap a saved narrow-FM favourite. Tap TONE again to cancel.";
    if (state.tone_filter) {
      const match = state.tone_matched === true ? "Match."
        : state.tone_matched === false ? (tone ? "Different tone detected." : "No tone detected.") : "Waiting for a match.";
      return [`Saved filter ${formatTone(state.tone_filter)}.`, match, detected,
        target === toneKey ? controlHelp.tone : ""].filter(Boolean).join(" ");
    }
    if (detected && target === toneReadout) return detected;
    if (target === toneReadout) return toneReadout.textContent;
    return [detected, controlHelp.tone, state.tone_enabled ? "Search on." : "Search off."].filter(Boolean).join(" ");
  }
  if (target === signalMeter) return signalDescription(state);
  const legend = target.dataset.lcd;
  if (legend !== undefined) {
    if (!target.classList.contains("is-visible")) return "";
    if (state.lower?.includes("SCAN") && (legend === "▲" || legend === "▼")) {
      return legend === "▲" ? "Scan Up" : "Scan Down";
    }
    return legendNames[legend === "NFM" ? target.textContent : legend] ?? "";
  }
  if (target === lcd) {
    if (frequencyEntry || editingFavorite) return "";
    const watched = state.watch_only && state.watch_channel;
    if (Number.isInteger(watched?.slot) && Number.isInteger(watched?.hz)) {
      return `F${String(watched.slot).padStart(2, "0")} · ${formatMHz(watched.hz)}`;
    }
    const frequency = Number.isInteger(state.tune_hz) ? formatMHz(state.tune_hz) : "";
    const tuning = state.dab_tuning ? "Acquiring DAB audio…" : "";
    return [frequency, state.band_search_range, rdsMessage, state.dab_error || tuning].filter(Boolean).join(" · ");
  }
  if (target.dataset.favorite !== undefined) {
    const index = Number(target.dataset.favorite) - 1;
    const dabFavorite = state.dab_favorites?.[index];
    const favorite = dabFavorite ?? state.favorites?.[index];
    const name = state.favorite_names?.[index] || dabFavorite?.name;
    const mode = dabFavorite ? ` · DAB ${dabFavorite.block}` : "";
    const filter = favorite?.tone ? `Tone filter ${formatTone(favorite.tone)}` : "";
    if (choosingToneFavorite) {
      return favorite?.mode === "FM"
        ? [name, formatMHz(favorite.hz), filter, "Tap to set tone squelch."].filter(Boolean).join(" · ")
        : "Choose a saved narrow-FM favourite.";
    }
    if (state.favorite_edit === "scan") {
      if (!state.favorites?.[index]) return "Save an analogue favourite to scan this slot.";
      const selected = state.scan_favorites?.includes(index + 1);
      return [name, formatMHz(favorite.hz), selected ? "Tap to remove from scan." : "Tap to add to scan."].filter(Boolean).join(" · ");
    }
    if (state.favorite_edit === "delete") {
      return favorite ? [name, formatMHz(favorite.hz), "Tap to delete this favourite."].filter(Boolean).join(" · ")
        : "Empty favourite slot.";
    }
    const saveHint = state.dab_enabled ? "Hold to save the selected DAB station." : "Hold to save the tuned frequency.";
    const use = favorite ? `${formatMHz(favorite.hz)}${mode} · Tap to recall or hold to replace.` : saveHint;
    const watching = state.watch_active?.includes(index + 1) ? "WATCH detected a signal." : "";
    return [name, use, filter, "Right-click to name.", watching].filter(Boolean).join(" · ");
  }
  const control = target.dataset.setting ?? target.dataset.key ?? target.dataset.knob;
  if (control === "save-vol") {
    const offset = listeningProfile?.offset;
    const profile = offset === undefined ? "" : `Channel volume offset ${offset > 0 ? "+" : ""}${offset} dB.`;
    return [saveVolumeMessage, profile, controlHelp[control]].filter(Boolean).join(" ");
  }
  if (control === "squelch" && Number.isFinite(state.squelch_db)) {
    const value = dialValue("squelch");
    if (listeningProfile) {
      if (state.dab_enabled) return "The saved squelch setting applies to analogue audio.";
      const level = value === 0 ? "Listening squelch open." : `Listening squelch ${(-98 + 100 * value).toFixed(1)} dBFS.`;
      return `${level} Applies to audio in this browser.`;
    }
    const level = value === 0 ? "Squelch open." : `Squelch threshold ${state.squelch_db.toFixed(1)} dBFS.`;
    return `${level} ${controlHelp.squelch}`;
  }
  if (control === "boost") {
    return `Audio boost ${target.getAttribute("aria-valuetext")}. ${controlHelp.boost}`;
  }
  if (control === "record") {
    return [controlHelp.record, state.record_message].filter(Boolean).join(" · ");
  }
  if (control === "play-last") {
    const holdHint = mobilePanel.matches
      ? "Hold to open the Discovery log." : "";
    return [controlHelp[control], holdHint, replayMessage].filter(Boolean).join(" · ");
  }
  if (control === "replay") {
    return [controlHelp.replay, discoveryMessage].filter(Boolean).join(" · ");
  }
  if (control === "discovery") {
    return [controlHelp.discovery, state.discovery_enabled ? "Logging is on." : "Logging is off."]
      .join(" ");
  }
  if (control === "dab") {
    const stations = state.dab_station_count ?? 0;
    const count = `${stations} DAB ${stations === 1 ? "station" : "stations"} discovered.`;
    return `${count} ${state.dab_error || controlHelp.dab}`;
  }
  if ((control === "scan" || control === "search") && state.dab_enabled) {
    return control === "scan" ? "Hold to select analogue favourites. Hold DAB to find digital stations."
      : "Hold DAB to scan for digital stations.";
  }
  if (state.dab_enabled && !state.dab_programming) {
    if (control === "fav") return "Switch between discovered DAB stations and saved favourites. Hold to delete favourites.";
    if (control === "up" || control === "down") {
      const direction = control === "up" ? "next" : "previous";
      const list = state.dab_browse_favorites ? "saved favourite" : "discovered DAB station";
      return `Tune to the ${direction} ${list}.`;
    }
  }
  if (control === "watch" && state.watch_receiver) {
    const receiver = state.watch_receiver === "secondary" ? "Using a spare SDR." : "Using this panel’s SDR.";
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
  if (changed) tooltip.textContent = message;
  if (changed || target === waterfallPlot) positionTooltip();
}

function positionTooltip() {
  if (tooltip.hidden) return;
  const anchor = shownTooltipTarget.getBoundingClientRect();
  const { width, height } = tooltip.getBoundingClientRect();
  const pointer = shownTooltipTarget === waterfallPlot ? waterfallPointer : null;
  const left = Math.max(8, Math.min(innerWidth - width - 8,
    pointer ? pointer.x + 12 : anchor.left + (anchor.width - width) / 2));
  const top = (pointer?.y ?? anchor.top) - height - 8;
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(8, Math.min(innerHeight - height - 8, top < 8 ? (pointer?.y ?? anchor.bottom) + 8 : top))}px`;
}

waterfallPlot.addEventListener("pointermove", (event) => {
  if (event.pointerType === "touch") return;
  waterfallPointer = { x: event.clientX, y: event.clientY };
  hoveredTooltipTarget = waterfallPlot;
  updateTooltip();
});
waterfallPlot.addEventListener("pointerleave", (event) => {
  if (hoveredTooltipTarget !== waterfallPlot) return;
  waterfallPointer = undefined;
  hoveredTooltipTarget = lcd.contains(event.relatedTarget) ? lcd : undefined;
  updateTooltip();
});

const tooltipTargets = [lcd, signalMeter, toneReadout, ...legends, ...keys, ...document.querySelectorAll("[data-knob]"), ...settings];
if (mobileAdvancedToggle) tooltipTargets.push(mobileAdvancedToggle);
for (const target of tooltipTargets) {
  target.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "touch") return;
    hoveredTooltipTarget = target;
    updateTooltip();
  });
  target.addEventListener("pointerleave", (event) => {
    if (hoveredTooltipTarget === target) {
      const staysInLcd = target !== lcd && lcd.contains(target) && lcd.contains(event.relatedTarget);
      hoveredTooltipTarget = staysInLcd ? lcd : undefined;
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

let mobileAdvanced = false;

function updateMobilePanel() {
  if (!mobileAdvancedToggle) return;
  const slim = mobilePanel.matches && (!mobileAdvanced || Boolean(pinMode));
  radio.classList.toggle("is-mobile-slim", slim);
  const bank = slim ? mobileKeypad ?? favoriteBank : settingsBank;
  if (!slim) {
    if (keypadEntry) editFrequency("");
    for (const [id, press] of presses) {
      if (press.digit !== undefined) endPress(id, true);
    }
  }
  if (mobileAdvancedToggle.parentElement !== bank) {
    const focused = document.activeElement === mobileAdvancedToggle;
    bank.append(mobileAdvancedToggle);
    if (focused && mobilePanel.matches) mobileAdvancedToggle.focus({ preventScroll: true });
  }
  if (slim && pinMode) {
    mobileAdvancedToggle.setAttribute("aria-label", "Confirm PIN");
    mobileAdvancedToggle.removeAttribute("aria-pressed");
  } else {
    mobileAdvancedToggle.removeAttribute("aria-label");
    mobileAdvancedToggle.setAttribute("aria-pressed", String(mobileAdvanced));
  }
  updatePinControls();
  if (pinMode) renderPinStatus();
  updateTooltip();
  positionTooltip();
}

mobileAdvancedToggle?.addEventListener("click", () => {
  if (pinMode) {
    void submitPin();
    return;
  }
  mobileAdvanced = !mobileAdvanced;
  updateMobilePanel();
});
mobilePanel.addEventListener("change", updateMobilePanel);
updateMobilePanel();

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
  if (next.dab_enabled) return "";
  const match = next.favorites?.findIndex((entry, index) => entry?.hz === next.tune_hz && names[index]);
  return names[match] ?? "";
}

function renderRds(next, entering) {
  const showDetails = next.power && next.rds_enabled && !next.dab_tuning && !entering && !editingFavorite;
  const rds = showDetails ? next.rds : null;
  const favoriteName = showDetails ? tunedFavoriteName(next) : "";
  const pi = Number.isInteger(rds?.pi) ? rds.pi.toString(16).toUpperCase().padStart(4, "0") : rds?.pi;
  const station = favoriteName || rds?.ps?.trim() || (pi && !rds.text ? `PI ${pi}` : "");
  const text = rds?.text?.trim() ?? "";
  const radioText = text && (next.dab_enabled || /^now playing\b/i.test(text) ? text : `Now Playing: ${text}`);
  rdsMessage = [station, radioText].filter(Boolean).join(" · ");
  rdsReadout.hidden = !rdsMessage;
  rdsToggle.setAttribute("aria-pressed", String(Boolean(next.rds_enabled)));
  showStation(station);
  showRadioText(radioText);
}

function resetRadioInteraction() {
  disconnectAudio();
  waterfall.update(null);
  if (replayRequest) stopReplay();
  closeDiscovery(false);
  discoverySaves.clear();
  replayMessage = "";
  discoveryMessage = "";
  lastToneId = undefined;
  cancelKeypadTune();
  frequencyEntry = "";
  editingFavorite = 0;
  favoriteNameEditor.hidden = true;
  choosingToneFavorite = false;
  toneChoiceMessage = "";
  if (toneFilterDialog.open) toneFilterDialog.close();
  if (recordFolderDialog.open) recordFolderDialog.close();
  if (volumeOffsetDialog?.open) volumeOffsetDialog.close();
  recordingDirectory = undefined;
  folderRequest += 1;
  wheelGesture = undefined;
  clearTimeout(lockTimer);
  lockTimer = undefined;
  for (const id of presses.keys()) endPress(id, true);
  for (const dial of dials.values()) {
    dial.drag = null;
    dial.pending = null;
    dial.element.classList.remove("is-dragging");
  }
}

function render(next) {
  if (typeof next.display !== "string") return;
  const previous = state;
  state = next;
  const parked = next.radio_id === null;
  if (previous.radio_id !== undefined && next.radio_id !== previous.radio_id) {
    resetRadioInteraction();
  } else if (next.record_folder !== previous.record_folder) {
    discoverySaves.clear();
    updateDiscoveryHitControls();
  }
  if (!next.power) {
    choosingToneFavorite = false;
    toneChoiceMessage = "";
    if (toneFilterDialog.open) toneFilterDialog.close();
  }
  if (next.power && listenerVolume === undefined) listenerVolume = next.volume;
  loadListeningProfile(next);
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
  scanToggle?.classList.toggle("is-selecting", next.favorite_edit === "scan");
  scanToggle?.classList.toggle("is-scanning", Boolean(next.power && !next.dab_enabled
    && next.lower?.includes("SCAN") && !next.lower.includes("HOLD")));
  recordToggle.setAttribute("aria-pressed", String(Boolean(next.record_enabled)));
  updateDiscoveryControls();
  dabToggle.setAttribute("aria-pressed", String(Boolean(next.dab_enabled)));
  recordToggle.classList.toggle("is-recording", Boolean(next.record_active));
  if (!canEnterFrequency()) {
    cancelKeypadTune();
    frequencyEntry = "";
  }
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
  for (const button of presetKeys) {
    button.setAttribute("aria-pressed", String(active.has(presetBanks[button.dataset.key])));
  }
  const modeLegend = ["DAB", "WFM", "USB", "LSB"].find(mode => active.has(mode)) ?? "NFM";
  for (const element of legends) {
    const name = element.dataset.lcd;
    const indicator = name === "NFM" ? modeLegend : name;
    if (name === "NFM" && element.textContent !== indicator) element.textContent = indicator;
    element.classList.toggle("is-visible", active.has(indicator));
    element.classList.toggle("is-blinking",
      active.has(name) && (next.blink_bank === name || (name === "CLOSE" && next.close_blink)));
  }
  renderTone();
  if (toneFilterDialog.open) toneFilterDetected.hidden = !detectedToneForFavorite();

  lcd.classList.toggle("is-unlit", !pinMode && !(next.backlight ?? next.power));
  waterfall.update(next.waterfall ?? null, next.tune_hz);
  waterfall.element.classList.toggle("has-frame", Boolean(waterfall.frame));
  waterfall.element.classList.toggle("can-tune", canTuneWaterfall());
  renderRds(next, entering);
  const readout = pinMode ? pinDigits ? "•".repeat(pinDigits.length) : "PIN" : entering ? frequencyEntry : next.display;
  lcd.setAttribute("aria-label", pinMode ? "Scanner PIN" : entering ? `${readout} MHz`
    : [readout.trim(), ...active, toneReadout.textContent, rdsMessage].filter(Boolean).join(" ") || "Scanner display");
  renderPinStatus();
  display.classList.toggle("is-blinking", !pinMode && !entering && Boolean(next.blink));
  display.classList.toggle("is-scrolling", !pinMode && !entering && Boolean(next.scan_scroll ?? next.display === "SCAN"));
  display.classList.toggle("is-clock", !pinMode && !next.power && next.display.includes(":"));
  const tunableDigits = !entering && canTune() && Number.isInteger(next.tune_hz)
    && frequencyReadout.test(readout);
  drawDigits(display, readout, tunableDigits);
  drawDigits(channel, visibleChannel ? String(next.channel).padStart(2, "0") : "");

  const dabActiveFavorites = next.dab_active_favorites ?? [next.active_favorite];
  for (const [index, button] of favorites.entries()) {
    const favorite = next.favorites?.[index];
    const dabFavorite = next.dab_favorites?.[index];
    const matching = next.dab_enabled ? dabFavorite && dabActiveFavorites.includes(index + 1)
      : favorite && favorite.hz === next.tune_hz;
    const tuned = Boolean(next.power && matching);
    const selected = Boolean(!parked && next.scan_favorites?.includes(index + 1));
    button.classList.toggle("is-stored", !parked && Boolean(favorite || dabFavorite));
    button.classList.toggle("is-tuned", tuned);
    button.classList.toggle("is-scan-selected", selected);
    button.classList.toggle("is-scan-running", selected && Boolean(next.scan_favorites_running));
    button.classList.toggle("is-watched", !parked && !tuned && !selected && Boolean(next.watch_active?.includes(index + 1)));
    if (index < 9 && digitKeys[index]) {
      digitKeys[index].classList.toggle("is-stored", !parked && Boolean(favorite || dabFavorite));
      digitKeys[index].classList.toggle("is-tuned", tuned);
    }
    if (next.favorite_edit === "scan") button.setAttribute("aria-pressed", String(selected));
    else button.removeAttribute("aria-pressed");
  }
  updateTooltip();

  for (const dial of dials.values()) {
    if (!dial.drag && dial.pending === null && !dial.busy) {
      paintDial(dial, dialValue(dial.name));
    }
  }

  updateAudio(previous);
  if (previousScannerSession && previous.radio_id === undefined && next.power) void enableAudio();
  lastRecording.volume = (state.power ? dialValue("volume") : listenerVolume ?? defaultVolume) ** 1.45;
  lastRecording.muted = Boolean(state.muted);
  if (next.tone && next.tone.id !== lastToneId) {
    const initialized = lastToneId !== undefined;
    lastToneId = next.tone.id;
    if (initialized || pendingActions > 0) playTone(next.tone);
  }
}

async function refreshAccess() {
  const session = scannerSession;
  const version = actionVersion;
  const response = await fetch(sessionUrl("/api/access"), { cache: "no-store" });
  if (!response.ok) return;
  const next = await response.json();
  if (pageActive && session === scannerSession && version === actionVersion) applyAccess(next);
}

async function poll() {
  let delay = 150;
  try {
    if (pageActive && !pendingActions && !pinBusy) {
      if (pinMode === "unlock") {
        delay = 1000;
        await refreshAccess();
        return;
      }
      const startedAt = actionVersion;
      if (!access && !accessCheck) {
        accessCheck = refreshAccess().catch(() => {}).finally(() => {
          setTimeout(() => { accessCheck = undefined; }, 1000);
        });
      }
      const response = await fetch(sessionUrl("/api/state"), { cache: "no-store" });
      if (!response.ok && response.status !== 401) return;
      const next = await response.json();
      if (!pageActive || startedAt !== actionVersion || pendingActions) return;
      if (response.status === 401) {
        applyAccess(next);
      } else if (!access?.pin_required && typeof next.display === "string") {
        awaitingState = false;
        render(next);
      }
    }
  } catch {
    // The next poll retries the local service.
  } finally {
    setTimeout(poll, delay);
  }
}

function postAction(action) {
  const session = scannerSession;
  const radioId = state.radio_id;
  pendingActions += 1;
  actionVersion += 1;
  actionTail = actionTail.catch(() => {}).then(async () => {
    if (!pageActive || awaitingState || pinMode || session !== scannerSession
        || (radioId !== undefined && state.radio_id !== radioId)) return;
    const response = await fetch(sessionUrl("/api/action", radioId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action),
    });
    if (pageActive && session === scannerSession && (response.ok || [401, 409].includes(response.status))) {
      const next = await response.json();
      if (!pageActive || session !== scannerSession) return;
      if (response.status === 401) applyAccess(next);
      else if (!access?.pin_required) render(next);
      if (response.ok) return next;
    }
  }).finally(() => {
    pendingActions -= 1;
  });
  return actionTail.catch(() => {});
}

const discoveryTime = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

function updateDiscoveryControls() {
  const logging = Boolean(state.discovery_enabled);
  discoveryLogToggle.setAttribute("aria-pressed", String(Boolean(state.discovery_started && !logging)));
  discoveryLogToggle.disabled = !state.power || discoveryBusy;
  const status = discoveryMessage || (!state.power ? "Power on to log reception."
    : discoveryLoadError ? "Could not load the log. Retrying…"
      : logging ? "Logging received audio." : "Logging is paused.");
  if (discoveryStatus.textContent !== status) discoveryStatus.textContent = status;
  const current = state.discovery_current;
  discoveryCurrent.hidden = !current;
  const frequency = current ? formatMHz(current.hz).replace(/ MHz$/, "") : "";
  const mode = current?.mode === "FM" ? "NFM" : current?.mode ?? "";
  const tone = formatTone(current?.tone).replace(/^CTCSS /, "");
  drawDigits(discoveryCurrentHz, frequency);
  drawDigits(discoveryCurrentMode, mode);
  drawDigits(discoveryCurrentTone, tone);
  if (current) {
    const label = [`${frequency} MHz`, mode, tone].filter(Boolean).join(", ");
    if (discoveryCurrent.getAttribute("aria-label") !== label) discoveryCurrent.setAttribute("aria-label", label);
  }
}

function updateReplayControls() {
  playLast.setAttribute("aria-pressed", String(replaySource === "saved"));
  instantReplay.setAttribute("aria-pressed", String(replaySource === "instant"));
  discoveryMobileReplay.setAttribute("aria-pressed", String(replaySource === "instant"));
  updateDiscoveryHitControls();
}

function updateDiscoveryHitControls() {
  for (const button of discoveryHits.querySelectorAll("[data-discovery-hit]")) {
    const playing = Number(button.dataset.discoveryHit) === replaySource;
    button.setAttribute("aria-pressed", String(playing));
    button.disabled = !playing && button.dataset.playable !== "true";
    button.title = playing ? "Stop playback" : button.disabled ? "Audio is no longer in the replay buffer" : "Play this hit";
  }
  for (const button of discoveryHits.querySelectorAll("[data-discovery-record]")) {
    const save = discoverySaves.get(Number(button.dataset.discoveryRecord));
    button.dataset.saved = String(Boolean(save?.filename));
    button.setAttribute("aria-busy", String(Boolean(save?.pending)));
    button.disabled = Boolean(save?.pending) || button.dataset.recordable !== "true";
    if (save?.filename) button.title = `Saved as ${save.filename}`;
    else if (save?.pending) button.title = "Saving this hit";
    else if (button.disabled) button.title = button.dataset.playable === "true"
      ? "Needs at least two seconds of buffered audio" : "Audio is no longer in the replay buffer";
    else button.title = "Save this hit to the recording folder";
  }
}

function renderDiscoveryHits(hits) {
  discoveryEntries = hits.map(hit => {
    const time = Number.isFinite(hit.time_ms) ? discoveryTime.format(hit.time_ms) : "—";
    const frequency = formatMHz(hit.hz).replace(/ MHz$/, "");
    const mode = hit.mode === "FM" ? "NFM" : hit.mode;
    const tone = formatTone(hit.tone).replace(/^CTCSS /, "");
    return { id: hit.id, time, frequency, mode, tone, playable: hit.playable, recordable: hit.recordable };
  });
  const retained = new Set(discoveryEntries.map(entry => entry.id));
  for (const id of discoverySaves.keys()) {
    if (!retained.has(id)) discoverySaves.delete(id);
  }
  renderDiscoveryPage();
}

function renderDiscoveryPage() {
  let entries = discoveryEntries;
  if (discoveryPageHead !== undefined) {
    // Keep older pages stable as newly received hits are added at the top.
    const first = entries.findIndex(entry => entry.id <= discoveryPageHead);
    if (first !== -1 && entries.length - first > discoveryPageSize) {
      entries = entries.slice(first);
    } else {
      discoveryPage = 0;
      discoveryPageHead = undefined;
    }
  }
  const pages = Math.max(1, Math.ceil(entries.length / discoveryPageSize));
  discoveryPage = Math.min(discoveryPage, pages - 1);
  discoveryPageUp.disabled = discoveryPage === 0;
  discoveryPageDown.disabled = discoveryPage === pages - 1;
  const label = `PAGE ${discoveryPage + 1} / ${pages}`;
  if (discoveryPageNumber.textContent !== label) discoveryPageNumber.textContent = label;

  const start = discoveryPage * discoveryPageSize;
  const rows = entries.slice(start, start + discoveryPageSize);
  const signature = JSON.stringify(rows);
  if (signature === discoveryRows) return;
  discoveryRows = signature;
  const { discoveryHit: focusedPlay, discoveryRecord: focusedRecord } = document.activeElement?.dataset ?? {};
  discoveryHits.replaceChildren(...Array.from({ length: discoveryPageSize }, (_, index) => {
    const entry = rows[index];
    const row = document.createElement("tr");
    if (!entry) {
      row.className = "is-empty";
      row.setAttribute("aria-hidden", "true");
    }
    for (const text of entry ? [entry.time, entry.frequency, entry.mode, entry.tone || "—"] : ["", "", "", ""]) {
      row.insertCell().textContent = text;
    }
    for (const [key, action] of [["discoveryHit", "Play"], ["discoveryRecord", "Save"]]) {
      const button = document.createElement("button");
      const cap = document.createElement("span");
      button.type = "button";
      if (entry) {
        button.dataset[key] = String(entry.id);
        button.dataset.playable = String(entry.playable);
        if (key === "discoveryRecord") button.dataset.recordable = String(entry.recordable);
        button.setAttribute("aria-label", `${action} hit at ${entry.time} on ${entry.frequency} MHz`);
      } else {
        button.disabled = true;
      }
      cap.className = "discovery-hit-cap";
      cap.setAttribute("aria-hidden", "true");
      button.append(cap);
      row.insertCell().append(button);
    }
    return row;
  }));
  updateReplayControls();
  if (focusedPlay !== undefined) discoveryHits.querySelector(`[data-discovery-hit="${CSS.escape(focusedPlay)}"]`)?.focus();
  if (focusedRecord !== undefined) discoveryHits.querySelector(`[data-discovery-record="${CSS.escape(focusedRecord)}"]`)?.focus();
}

async function saveDiscoveryHit(id) {
  if (!Number.isSafeInteger(id) || discoverySaves.get(id)?.pending) return;
  const save = { pending: true };
  discoverySaves.set(id, save);
  discoveryMessage = "Saving hit…";
  updateDiscoveryHitControls();
  updateDiscoveryControls();
  try {
    const response = await fetch(sessionUrl(`/api/discovery/hit/${id}/record`, state.radio_id),
      { method: "POST" });
    const result = await response.json().catch(() => null);
    if (discoverySaves.get(id) !== save) return;
    if (response.ok && typeof result?.filename === "string") {
      save.pending = false;
      save.filename = result.filename;
      discoveryMessage = `Saved ${result.filename}.`;
    } else {
      discoverySaves.delete(id);
      discoveryMessage = result?.error || "Could not save this hit.";
    }
  } catch {
    if (discoverySaves.get(id) !== save) return;
    discoverySaves.delete(id);
    discoveryMessage = "Could not save this hit.";
  }
  updateDiscoveryHitControls();
  updateDiscoveryControls();
}

async function refreshDiscovery() {
  clearTimeout(discoveryRefresh);
  if (discoveryPad.hidden || !pageActive || !state.power || discoveryRequest) return;
  const request = new AbortController();
  discoveryRequest = request;
  try {
    const response = await fetch(sessionUrl("/api/discovery", state.radio_id),
      { cache: "no-store", signal: request.signal });
    if (!response.ok) throw new Error("Discovery lookup failed");
    const snapshot = await response.json();
    if (discoveryRequest === request) {
      discoveryLoadError = false;
      renderDiscoveryHits(snapshot.hits);
    }
  } catch {
    if (discoveryRequest === request) discoveryLoadError = true;
  } finally {
    if (discoveryRequest === request) {
      discoveryRequest = undefined;
      updateDiscoveryControls();
      discoveryRefresh = setTimeout(refreshDiscovery, 1000);
    }
  }
}

function closeDiscovery(restoreFocus = true) {
  if (discoveryPad.hidden) return;
  clearTimeout(discoveryRefresh);
  discoveryRequest?.abort();
  discoveryRequest = undefined;
  discoveryPad.hidden = true;
  document.body.classList.remove("discovery-open");
  discoveryToggle.setAttribute("aria-expanded", "false");
  playLast.removeAttribute("aria-expanded");
  if (restoreFocus) discoveryOpener?.focus();
  discoveryOpener = undefined;
}

async function toggleDiscovery(opener) {
  if (!discoveryPad.hidden) {
    closeDiscovery();
    return;
  }
  discoveryOpener = opener;
  if (!replayRequest) discoveryMessage = "";
  discoveryLoadError = false;
  discoveryRows = "";
  discoveryPage = 0;
  discoveryPageHead = undefined;
  discoveryPad.hidden = false;
  document.body.classList.add("discovery-open");
  discoveryEntries = [];
  renderDiscoveryPage();
  discoveryToggle.setAttribute("aria-expanded", "true");
  if (opener === playLast) playLast.setAttribute("aria-expanded", "true");
  discoveryClose.focus();
  if (state.power && !discoveryBusy) {
    discoveryBusy = true;
    updateDiscoveryControls();
    await postAction({ discovery: "open" });
    discoveryBusy = false;
  }
  updateDiscoveryControls();
  void refreshDiscovery();
}

discoveryClose.addEventListener("click", () => closeDiscovery());
discoveryMobileReplay.addEventListener("click", () => void toggleInstantReplay());
discoveryPageUp.addEventListener("click", () => {
  discoveryPage -= 1;
  if (!discoveryPage) discoveryPageHead = undefined;
  renderDiscoveryPage();
});
discoveryPageDown.addEventListener("click", () => {
  if (!discoveryPage) discoveryPageHead = discoveryEntries[0]?.id;
  discoveryPage += 1;
  renderDiscoveryPage();
});
discoveryLogToggle.addEventListener("click", async () => {
  discoveryBusy = true;
  discoveryMessage = "";
  updateDiscoveryControls();
  const next = await postAction({ discovery: "toggle" });
  discoveryBusy = false;
  if (!next && !discoveryPad.hidden) discoveryMessage = "Could not change logging.";
  updateDiscoveryControls();
  void refreshDiscovery();
});
discoveryHits.addEventListener("click", (event) => {
  const button = event.target.closest("[data-discovery-hit]");
  if (button) void playReplay(Number(button.dataset.discoveryHit));
  const record = event.target.closest("[data-discovery-record]");
  if (record) void saveDiscoveryHit(Number(record.dataset.discoveryRecord));
});

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

function pauseKeypadTune() {
  clearTimeout(keypadTuneTimer);
  keypadTuneTimer = undefined;
}

function cancelKeypadTune() {
  pauseKeypadTune();
  keypadEntry = false;
}

function queueKeypadTune() {
  pauseKeypadTune();
  if (!keypadEntry || frequencyEntry.endsWith(".")
      || [...presses.values()].some(press => press.digit !== undefined)) return;
  keypadTuneTimer = setTimeout(() => {
    if (pageActive && canEnterFrequency() && frequencyEntry) tuneFromLCD(frequencyEntry);
    else editFrequency("");
  }, keypadTuneDelay);
}

function editFrequency(value) {
  if (!value) cancelKeypadTune();
  if (value === frequencyEntry) return;
  frequencyEntry = value;
  render(state);
}

function appendFrequency(character, timed = false) {
  const value = frequencyEntry + character;
  const next = value.startsWith(".") ? "0" + value : value;
  if (!/^[0-9]{1,4}(?:\.[0-9]{0,6})?$/.test(next)) return;
  if (timed) keypadEntry = true;
  editFrequency(next);
  if (keypadEntry) queueKeypadTune();
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
  if (event.target === waterfallPlot && event.button === 0 && canTuneWaterfall()) {
    const hz = waterfall.frequencyAt(event.clientX);
    if (hz !== null) tuneFromLCD((hz / 1_000_000).toFixed(6));
  }
  if (event.target !== favoriteNameEditor) focusLCD();
});

function tuneFromLCD(mhz) {
  editFrequency("");
  void enableAudio();
  flushLockTap();
  void postAction({ tune: "frequency", mhz });
}

async function copyFrequency(hz) {
  const frequency = `${hz / 1_000_000} MHz`;
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(frequency);
      return;
    } catch {
      // The browser can deny Clipboard API access even on a secure origin.
    }
  }

  const previousFocus = document.activeElement;
  const field = document.createElement("textarea");
  field.value = frequency;
  field.readOnly = true;
  field.tabIndex = -1;
  field.style.cssText = "position: fixed; top: 0; left: 0; opacity: 0; pointer-events: none;";
  document.body.append(field);
  try {
    field.focus({ preventScroll: true });
    field.select();
    document.execCommand("copy");
  } catch (error) {
    console.debug("Could not copy the tuned frequency", error);
  } finally {
    field.remove();
    previousFocus?.focus({ preventScroll: true });
  }
}

display.addEventListener("dblclick", (event) => {
  if (event.button !== 0 || editingFavorite || frequencyEntry || !Number.isInteger(state.tune_hz)) return;
  const dabFrequency = state.dab_enabled && state.power && !state.scan_active && !state.dab_rescanning
    && !state.lower?.includes("CH") && frequencyReadout.test(state.display);
  if (display.dataset.tunable !== "true" && !dabFrequency) return;
  event.preventDefault();
  void copyFrequency(state.tune_hz);
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
  if (!keypadEntry && document.activeElement !== lcd) editFrequency("");
});

lcd.addEventListener("blur", () => {
  if (!keypadEntry) editFrequency("");
});

window.addEventListener("pointerdown", (event) => {
  if (!lcd.contains(event.target) && !event.target.closest?.("[data-digit]")) editFrequency("");
});

window.addEventListener("keydown", (event) => {
  if (pinMode) {
    const { key } = event;
    const modified = event.isComposing || event.altKey || event.ctrlKey || event.metaKey;
    if (key === "Enter" || key === "Return" || event.code === "NumpadEnter") {
      event.preventDefault();
      event.stopPropagation();
      if (!modified && !event.shiftKey && !event.repeat) {
        lcd.focus({ preventScroll: true });
        void submitPin();
      }
      return;
    }
    if (modified || event.repeat) return;
    if (/^[0-9]$/.test(key)) enterPinDigit(key);
    else if (key === "Backspace") enterPinDigit();
    else if (key === "Escape" && pinMode !== "unlock" && !pinBusy) exitPin();
    else return;
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  if (event.key === "Escape" && choosingToneFavorite) {
    setToneFavoriteChoice(false);
    return;
  }
  if (event.key === "Escape" && !discoveryPad.hidden && !frequencyEntry && !editingFavorite
      && !recordFolderDialog.open && !volumeOffsetDialog?.open && !toneFilterDialog.open) {
    event.preventDefault();
    closeDiscovery();
    return;
  }
  const slim = radio.classList.contains("is-mobile-slim");
  if (recordFolderDialog.open || volumeOffsetDialog?.open || toneFilterDialog.open || !discoveryPad.hidden || !canEnterFrequency()
      || event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
  if (!slim && document.activeElement !== lcd && !lcd.matches(":hover")) return;
  if (event.target instanceof HTMLElement
      && (event.target.isContentEditable || event.target.matches("input, textarea, select"))) return;

  const { key } = event;
  const decimal = key === "." || (key === "," && event.code === "NumpadDecimal");
  if (/^[0-9]$/.test(key) || decimal) {
    event.preventDefault();
    appendFrequency(decimal ? "." : key, slim);
    return;
  }
  if (!frequencyEntry || !["Backspace", "Escape", "Enter"].includes(key)) return;

  event.preventDefault();
  event.stopPropagation();
  if (key === "Backspace") {
    editFrequency(frequencyEntry.slice(0, -1));
    if (keypadEntry) queueKeypadTune();
  } else {
    const mhz = frequencyEntry.replace(/\.$/, "");
    if (key === "Enter") tuneFromLCD(mhz);
    else editFrequency("");
  }
}, { capture: true });

function sendTap({ key, favorite, setting, digit, element, favoriteEdit = state.favorite_edit }) {
  if (pinMode) {
    if (key === "prog") void submitPin();
    else if (key === "lock") enterPinDigit();
    else if (key === "hold" && pinMode !== "unlock" && !pinBusy) exitPin();
    else {
      const value = pinKeyDigit({ favorite, digit });
      if (value !== undefined) enterPinDigit(value);
    }
    return;
  }
  if (key === "prog" && canProgramPin()) {
    const now = performance.now();
    if (now - lastProgTap <= progDoubleTapInterval) {
      lastProgTap = -Infinity;
      resetRadioInteraction();
      setPinMode("program");
      render(state);
      lcd.focus({ preventScroll: true });
      return;
    }
    lastProgTap = now;
  } else {
    lastProgTap = -Infinity;
  }
  if (digit !== undefined) {
    if (radio.classList.contains("is-mobile-slim") && canEnterFrequency()) appendFrequency(digit, true);
    return;
  }
  if (choosingToneFavorite) {
    if (favorite !== undefined) {
      openToneFilter(Number(favorite));
      return;
    }
    setToneFavoriteChoice(false);
    if (key === "tone") return;
  }
  if (favoriteEdit === "tone") return;
  if (setting === "dab") {
    flushLockTap();
    void postAction({ setting });
  } else if (key === "play-last") {
    void toggleReplay();
  } else if (key === "discovery") {
    void toggleDiscovery(element ?? discoveryToggle);
  } else if (key === "replay") {
    void toggleInstantReplay();
  } else if (key === "save-vol") {
    saveListeningProfile();
  } else if (key === "record") {
    flushLockTap();
    void postAction({ record: "toggle" });
  } else if (key === "tone") {
    flushLockTap();
    void postAction({ setting: "tone" });
  } else if (favorite !== undefined) {
    flushLockTap();
    const gesture = favoriteEdit === "scan" ? "select" : favoriteEdit === "delete" ? "delete" : "tap";
    void postAction({ favorite: Number(favorite), gesture });
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
  if (key === "play-last" && mobilePanel.matches) return 850;
  if (["hold", "band", "watch", "scan", "fav", "save-vol", "tone", "amateur", "business"].includes(key)) return 850;
  if (["lock", "close", "prog", "record"].includes(key)) return 2510;
  return null;
}

function beginPress(id, element) {
  if (presses.has(id) || element.inert) return;
  const { key, favorite, setting, digit } = element.dataset;
  if (digit !== undefined) pauseKeypadTune();
  if (choosingToneFavorite && favorite === undefined && key !== "tone") setToneFavoriteChoice(false);
  const favoriteEdit = choosingToneFavorite ? "tone" : state.favorite_edit ?? null;
  const press = { element, key, favorite, setting, digit, favoriteEdit, handled: false };
  const recording = key === "record" && Number.isInteger(state.tune_hz)
    && (!state.dab_enabled || state.dab_recording_identity)
    ? { record: "delete", hz: state.tune_hz, ...(state.dab_enabled ? { dab: state.dab_recording_identity } : {}) }
    : null;
  presses.set(id, press);
  element.classList.add("is-pressed");
  if (pinMode) return;
  if (!["play-last", "replay", "discovery"].includes(key)) void enableAudio();
  const delay = digit !== undefined ? favoriteHoldDelay
    : setting === "dab" ? 850 : favorite === undefined ? longPressDelay(key) : favoriteEdit ? null : favoriteHoldDelay;
  if (delay !== null) {
    press.timer = setTimeout(() => {
      press.handled = true;
      if (digit !== undefined) {
        if (!radio.classList.contains("is-mobile-slim")) return;
        if (digit === "0") {
          if (canEnterFrequency()) appendFrequency(".", true);
        } else {
          editFrequency("");
          if (state.power) {
            flushLockTap();
            void postAction({ favorite: Number(digit), gesture: "tap" });
          }
        }
        return;
      }
      if (key === "play-last") {
        void toggleDiscovery(element);
        return;
      }
      if (key === "save-vol") {
        editVolumeOffset();
        return;
      }
      if (key === "tone") {
        if (state.power) setToneFavoriteChoice(!choosingToneFavorite);
        return;
      }
      flushLockTap();
      if (setting === "dab") {
        void postAction({ setting, gesture: "long" });
        return;
      }
      if (key === "record") {
        if (recording) void postAction(recording);
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
  if (press.digit !== undefined && keypadEntry) queueKeypadTune();
}

for (const button of keys) {
  const startsLiveAudio = ![playLast, instantReplay, discoveryToggle].includes(button);
  button.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    button.setPointerCapture(event.pointerId);
    beginPress(event.pointerId, button);
  });
  button.addEventListener("pointerup", (event) => {
    if (!pinMode && event.pointerType === "touch" && startsLiveAudio && presses.has(event.pointerId)) void enableAudio();
    endPress(event.pointerId);
  });
  button.addEventListener("pointercancel", (event) => endPress(event.pointerId, true));
  button.addEventListener("lostpointercapture", (event) => endPress(event.pointerId, true));
  if (button.dataset.favorite !== undefined) {
    button.addEventListener("mousedown", (event) => {
      if (event.button !== 1) return;
      event.preventDefault();
      flushLockTap();
      void postAction({ favorite: Number(button.dataset.favorite), gesture: "lock" });
    });
    button.addEventListener("auxclick", (event) => {
      if (event.button === 1) event.preventDefault();
    });
  }
  button.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (pinMode) return;
    if (event.pointerType === "touch" || event.sourceCapabilities?.firesTouchEvents) return;
    if (button === saveVolumeKey) {
      editVolumeOffset();
      return;
    }
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
    if (event.detail === 0 && !button.inert) {
      if (!pinMode && startsLiveAudio) void enableAudio();
      sendTap({ ...button.dataset, element: button });
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
  dial.element.style.setProperty("--rotation", (-135 + dial.value * 270) + "deg");
  if (dial.name === "boost") {
    const db = Number((dial.value * maxBoostDb).toFixed(1));
    dial.element.setAttribute("aria-valuenow", String(db));
    dial.element.setAttribute("aria-valuetext", db ? `+${db} dB` : "0 dB");
    return;
  }
  const percent = Number((dial.value * 100).toFixed(dial.name === "squelch" ? 1 : 0));
  dial.element.setAttribute("aria-valuenow", String(percent));
  dial.element.setAttribute("aria-valuetext",
    dial.name === "volume" && dial.value === 0 ? "Clock standby" : percent + " percent");
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
      paintDial(dial, dialValue(dial.name));
    }
  });
}

function changeDial(dial, value) {
  if (pinMode) return;
  const previous = dial.value;
  const rounded = Math.round(limit(value) * 1000) / 1000;
  if (previous === rounded) return;
  paintDial(dial, rounded);
  const { name } = dial;
  if (name === "boost" || (name === "squelch" && listeningProfile)) {
    if (listeningProfile) listeningProfile[name] = rounded;
    else listenerBoost = rounded;
    updateAudioGain();
    updateTooltip();
    return;
  }
  if (name === "volume" && rounded > 0) {
    if (listeningProfile) listeningProfile.volume = rounded;
    else listenerVolume = rounded;
    if (previous > 0) {
      lastRecording.volume = rounded ** 1.45;
      updateAudioGain();
      return;
    }
  }
  const action = { knob: name, value: rounded };
  if (name === "volume" && previous === 0 && rounded > 0 && !state.power) {
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
    changeDial(dial, dial.value > 0 ? 0 : listenerVolume ?? defaultVolume);
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
    if (event.type === "pointerup") {
      if (event.pointerType === "touch") void enableAudio();
      if (!moved) tapDial(dial, event);
    }
    if (!dial.busy) paintDial(dial, dialValue(dial.name));
  };
  element.addEventListener("pointerup", finishDrag);
  element.addEventListener("pointercancel", finishDrag);
  element.addEventListener("lostpointercapture", finishDrag);
  element.addEventListener("wheel", (event) => {
    event.preventDefault();
    if (!event.deltaY) return;
    void enableAudio();
    const step = dial.name === "squelch" ? 0.005 : 0.04;
    changeDial(dial, dial.value + Math.sign(event.deltaY) * step);
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
      volumeNode.connect(audioContext.destination);
      liveGainNode = audioContext.createGain();
      liveGainNode.gain.value = 0;
      const limiter = audioContext.createWaveShaper();
      // The shaper input spans the full boost range. Leave headroom at the
      // loudest peaks with a smooth transition to full scale.
      const curve = new Float32Array(4097);
      for (let index = 0; index < curve.length; index += 1) {
        const sample = (2 * index / (curve.length - 1) - 1) * maxLiveGain;
        const magnitude = Math.abs(sample);
        curve[index] = magnitude <= .9 ? sample : Math.sign(sample) * (.9 + .1 * Math.tanh((magnitude - .9) / .1));
      }
      limiter.curve = curve;
      output.node.connect(liveGainNode).connect(limiter).connect(audioContext.destination);
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
  const source = replaySource;
  replayRequest?.abort();
  replayRequest = undefined;
  replaySource = undefined;
  if (source === "saved") replayMessage = message;
  else discoveryMessage = message;
  lastRecording.pause();
  lastRecording.removeAttribute("src");
  lastRecording.load();
  if (replayObjectUrl) URL.revokeObjectURL(replayObjectUrl);
  replayObjectUrl = undefined;
  updateReplayControls();
  updateDiscoveryControls();
  updateTooltip();
  if (pageActive && state.power) void enableAudio();
}

function toggleReplay() {
  return playReplay("saved");
}

function toggleInstantReplay() {
  return playReplay("instant");
}

async function playReplay(source) {
  if (awaitingState) return;
  if (replayRequest) {
    const sameSource = source === replaySource;
    stopReplay();
    if (sameSource) return;
  }
  const request = new AbortController();
  const radioId = state.radio_id;
  const saved = source === "saved";
  const path = saved ? "/api/recordings/last"
    : source === "instant" ? "/api/discovery/replay" : `/api/discovery/hit/${source}`;
  replayRequest = request;
  replaySource = source;
  lastRecording.loop = saved;
  if (saved) replayMessage = "Opening the latest recording…";
  else discoveryMessage = "Opening replay…";
  updateReplayControls();
  updateDiscoveryControls();
  updateTooltip();
  updateAudio();
  try {
    const response = await fetch(sessionUrl(path, radioId),
      { cache: "no-store", signal: request.signal });
    if (replayRequest !== request) return;
    if (response.status === 404) {
      stopReplay(saved ? "No saved recordings in the current folder."
        : source === "instant" ? "No recent received audio to replay."
          : "This hit has rolled out of the replay buffer.");
      return;
    }
    if (!response.ok) throw new Error("Replay lookup failed");
    let label;
    if (saved) {
      const clip = await response.json();
      if (replayRequest !== request) return;
      if (typeof clip.url !== "string" || !clip.url.startsWith("/api/recordings/")) {
        throw new Error("Recording URL unavailable");
      }
      lastRecording.src = sessionUrl(clip.url, radioId);
      label = clip.label || "Playing the latest recording.";
    } else {
      const audio = await response.blob();
      if (replayRequest !== request) return;
      replayObjectUrl = URL.createObjectURL(audio);
      lastRecording.src = replayObjectUrl;
      label = source === "instant" ? "Playing recent received audio." : "Playing the logged hit.";
    }
    await lastRecording.play();
    if (replayRequest === request) {
      if (saved) replayMessage = label;
      else discoveryMessage = label;
      updateDiscoveryControls();
      updateTooltip();
    }
  } catch {
    if (replayRequest === request) stopReplay(saved ? "Could not play the recording." : "Could not play replay audio.");
  }
}

lastRecording.addEventListener("error", () => {
  if (replayRequest && lastRecording.error) {
    stopReplay(replaySource === "saved" ? "Could not play the recording." : "Could not play replay audio.");
  }
});
lastRecording.addEventListener("ended", () => {
  if (replayRequest && replaySource !== "saved" && lastRecording.ended) stopReplay();
});

function disconnectAudio() {
  audioStream?.abort();
  audioStream = undefined;
  clearTimeout(audioRetry);
  audioRetry = undefined;
  pcmOutput?.reset();
}

function liveAudioEnabled() {
  return pageActive && !awaitingState && !pinMode && state.power && !state.watch_only && !replayRequest;
}

function updateAudioGain(live = liveAudioEnabled()) {
  if (!pcmOutput) return;
  const gain = live && !state.muted ? dialValue("volume") ** 1.45 : 0;
  const boostGain = maxBoostGain ** dialValue("boost");
  const offsetGain = 10 ** ((listeningProfile?.offset ?? 0) / 20);
  const level = state.modulation === "WFM" ? state.signal_meter_dbfs ?? state.signal_dbfs : state.signal_dbfs;
  const threshold = listeningProfile?.squelch;
  const listeningOpen = state.dab_enabled || !threshold || (Number.isFinite(level) && level >= -98 + 100 * threshold);
  const now = audioContext.currentTime;
  volumeNode.gain.setTargetAtTime(gain, now, 0.015);
  liveGainNode.gain.setTargetAtTime(listeningOpen ? gain * boostGain * offsetGain / maxLiveGain : 0, now, 0.015);
}

function updateAudio(previous = state) {
  if (!pcmOutput) return;
  const live = liveAudioEnabled();
  updateAudioGain(live);
  if (state.tune_hz !== previous.tune_hz || state.modulation !== previous.modulation || state.muted !== previous.muted
      || (!state.audio_open && previous.audio_open)) {
    pcmOutput.reset();
  }
  if (live && audioContext.state === "running") {
    if (!audioStream && !audioRetry) void streamAudio();
  } else {
    disconnectAudio();
  }
}

async function streamAudio() {
  const controller = new AbortController();
  audioStream = controller;
  let trailing = -1;
  try {
    const response = await fetch(sessionUrl("/audio"), { cache: "no-store", signal: controller.signal });
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
    if (audioStream === controller) {
      audioStream = undefined;
      if (pageActive && state.power && !state.watch_only && !replayRequest && audioContext.state === "running") {
        audioRetry = setTimeout(() => {
          audioRetry = undefined;
          updateAudio();
        }, 1000);
      }
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

window.addEventListener("pagehide", (event) => {
  pageActive = false;
  pinDigits = "";
  pinConfirmation = "";
  editFrequency("");
  closeDiscovery(false);
  if (replayRequest) stopReplay();
  disconnectAudio();
  // Reload and close share this event. The lease expires if the tab never reconnects.
  if (event.persisted || !rememberScannerSession()) {
    void fetch(sessionUrl("/api/session/leave"), { method: "POST", keepalive: true }).catch(() => {});
  }
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    scannerSession = newScannerSession();
    rememberScannerSession();
    actionVersion += 1;
    awaitingState = true;
    access = undefined;
    exitPin();
  }
  pageActive = true;
});
rememberScannerSession();
void poll();
