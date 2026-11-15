import { PcmBuffer } from "./pcm-buffer.js";
import { AF_VOLUME_STEP, createReceiverMenus, formatP25Talkgroup, hasAnalogFilter, isCustomPreset, supportsSsbDsp, TUNING_STEPS, ZONE_NAMES } from "./v10-menus.js";

const root = document.getElementById("sdr-v10-panel");
const lcd = root.querySelector(".lcd");
const lcdContent = lcd.querySelector(".lcd-content");
const menusRoot = document.getElementById("v10-menus");
const panels = [...lcd.querySelectorAll("[data-panel]")];
const view = Object.fromEntries([...root.querySelectorAll("[data-el]")].map(node => [node.dataset.el, node]));
const control = name => root.querySelector('[data-action="' + name + '"]');
const knob = name => root.querySelector('.knob[data-action="' + name + '"]');
const dialNames = ["dial-a", "dial-b", "dial-c", "dial-d", "dial-e", "frequency"];
const favoriteKeys = [...root.querySelectorAll('[data-action="favorite"]')];
const zoneKeys = [...root.querySelectorAll('[data-action="zone"]')];
const zoneKeyNames = Object.fromEntries(zoneKeys.map(key => [key.dataset.zone, key.textContent.trim()]));
const frequency = root.querySelector(".frequency-button");
const canvas = view.scope;
const context = canvas.getContext("2d");
const mirrorPath = "/lcd";
const isMirror = location.pathname === mirrorPath;
const displayUpdateInterval = 125;
const mirrorSnapshotMaxAge = 850;
const inputRate = 24000;
const audioFadeSeconds = 0.004;
const maxBoostDb = 24;
const maxBoostGain = 10 ** (maxBoostDb / 20);
const minimumHz = 100000;
const maximumHz = 1750000000;
const scopeSpectrumFraction = 0.54;
lcdContent.style.setProperty("--scope-spectrum-height", scopeSpectrumFraction * 100 + "%");
const frequencyDoubleClickDelay = 450;
const sessionPattern = /^[a-f0-9]{32}$/;
const preferencePrefix = "sdr-scanner:v10:";
const sessionKey = "sdr-scanner:session";
const mirrorSourceKey = preferencePrefix + "lcd-source";
const mirrorIdentityKey = preferencePrefix + "lcd-identity";
const listeningSessionKey = preferencePrefix + "listening-session";
const panelSettingsKey = "panel_display";
const defaultModes = ["FM", "WFM", "AM", "USB", "LSB", "DMR"];
const modeOrder = ["AUTO", "FM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB", "CW", "CW-R",
  "FSK", "FSK-R", "DMR", "D-STAR", "P25", "DPMR", "NXDN-VN"];
const modeHelp = {
  AUTO: "Select AM for airband and WFM for FM broadcasts. Check other strong signals for AM or narrow FM.",
  FM: "Receive narrowband FM voice, such as PMR446 and many two-way radios.",
  WFM: "Receive wideband FM broadcast stations.",
  AM: "Receive amplitude-modulated signals, including civil VHF airband.",
  "S-AM(D)": "Use synchronous AM detection with both sidebands.",
  "S-AM(U)": "Use synchronous AM detection with the upper sideband.",
  "S-AM(L)": "Use synchronous AM detection with the lower sideband.",
  USB: "Receive upper sideband voice or data.", LSB: "Receive lower sideband voice or data.",
  CW: "Listen to Morse code using the normal beat-note direction.",
  "CW-R": "Listen to Morse code using the reverse beat-note direction.",
  FSK: "Receive frequency-shift keyed RTTY with normal mark and space order.",
  "FSK-R": "Receive frequency-shift keyed RTTY with reversed mark and space order.",
  DMR: "Receive clear Digital Mobile Radio voice and supported data.",
  "D-STAR": "Receive clear D-STAR digital voice.", P25: "Receive clear P25 digital voice.",
  DPMR: "Receive clear dPMR digital voice.", "NXDN-VN": "Receive clear NXDN very-narrowband digital voice.",
};
const displayMode = name => name?.toUpperCase() === "DPMR" ? "dPMR" : name;
const spans = [12500, 25000, 50000, 75000, 100000, 150000, 200000, 250000, 300000,
  400000, 500000, 750000, 1000000];
const speeds = ["Slow", "Mid", "Fast"];
const clockFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const mirrorFields = ["mode", "top-state", "clock", "level", "site", "status", "detail-a", "detail-b",
  "rds-text", "scan-presets", "nearby-watch",
  "axis-left", "axis-center", "axis-right", "filter", "rf-options", "rec", "antenna", "flags", "memory-number",
  "memory-group", "offset", "step", "scope-hold", "scope-reference-mode", "scope-grid"];
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const finite = value => typeof value === "number" && Number.isFinite(value);
const formatFrequency = hz => finite(hz) ? (hz / 1e6).toFixed(6).replace(/(\d{3})$/, ".$1") : "—";
const formatAxis = hz => (Math.round(hz) / 1e6).toFixed(6);
const digits = value => String(value).padStart(2, "0");
const randomId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
  byte => byte.toString(16).padStart(2, "0")).join("");
const pressed = (node, enabled) => node.setAttribute("aria-pressed", String(Boolean(enabled)));

let state = {};
let access = {};
let session;
let actionChain = Promise.resolve();
let actionCount = 0;
let actionRevision = 0;
let stateRequestPending = false;
let nextStateRequestAt = -Infinity;
let lastStateAt = -Infinity;
let overlay;
let overlayCursor;
let favoriteToClear;
let frequencyEntryTimer;
let overlayField;
let pinSetup;
let menu;
const panelDefaults = {
  dialLock: false, channelDial: false, page: 1, expanded: false, visible: true,
  freeze: false, fixed: false, marker: false, span: 1000000, edges: null,
};
const ui = {...panelDefaults, dialAngles: {}, menuDialAngles: {}, frame: null, history: [], lastLineAt: 0, scopeForm: null};
let panelSettingsSaved;
let mirror;
let mirrorId;
let mirrorWindow;
let audioContext;
let audioGain;
let audioNode;
let audioBuffer;
let audioStarting;
let audioRequest;
let audioRetry;
let audioSignature;
let playback;
let audioVolume = 32;
let volumeMuted = false;
let powerPending = false;
let resumeSession;
let restoringSession = false;
let reportTimer = 0;

function getPreference(key, fallback) {
  try {
    const saved = localStorage.getItem(preferencePrefix + key);
    return saved === null ? fallback : JSON.parse(saved);
  } catch {
    return fallback;
  }
}

function setPreference(key, value) {
  if (key === "volume") audioVolume = value;
  if (key === "volume_muted") volumeMuted = value;
  try {
    localStorage.setItem(preferencePrefix + key, JSON.stringify(value));
  } catch {
    // An ephemeral browser can still use all live receiver controls.
  }
  if (key === "audio_boost" || key === "volume" || key === "volume_muted") updateAudioGain();
  render();
}

function panelSettings() {
  return Object.fromEntries(Object.keys(panelDefaults).map(key => [key, ui[key]]));
}

function restorePanelSettings() {
  const saved = getPreference(panelSettingsKey, null);
  for (const [key, fallback] of Object.entries(panelDefaults)) {
    if (typeof fallback === "boolean" && typeof saved?.[key] === "boolean") ui[key] = saved[key];
  }
  if (saved?.page === 2) ui.page = 2;
  if (finite(saved?.span) && saved.span > 0 && saved.span <= spans.at(-1)) ui.span = saved.span;
  if (Array.isArray(saved?.edges) && saved.edges.length === 2
      && saved.edges.every(finite) && saved.edges[0] < saved.edges[1]) ui.edges = saved.edges;
  if (!ui.edges) ui.fixed = false;
  panelSettingsSaved = JSON.stringify(panelSettings());
}

function rememberPanelSettings() {
  const current = JSON.stringify(panelSettings());
  if (current === panelSettingsSaved) return;
  panelSettingsSaved = current;
  try { localStorage.setItem(preferencePrefix + panelSettingsKey, current); } catch {}
}

const receiver = () => state.v10 || {};
const settings = () => receiver().settings || {};
const vfoMode = () => receiver().auto_mode ? "AUTO" : state.modulation || "FM";
const canSetPin = () => access.can_program_pin || access.can_program_pin_with_code;
const tuningStep = () => ui.stepOverride ?? settings().tune_step ?? 12500;
const setting = (key, value) => act({ v10: "setting", key, value });
const lastVolume = () => clamp(Number(getPreference("powerVolume", 0.32)) || 0.32, 0.01, 1);

function createSession() {
  const navigation = performance.getEntriesByType("navigation")[0]?.type;
  const returning = navigation === "reload" || navigation === "back_forward" || document.wasDiscarded;
  try {
    const saved = returning && sessionStorage.getItem(sessionKey);
    if (saved && sessionPattern.test(saved)) return saved;
    const id = randomId();
    sessionStorage.removeItem(listeningSessionKey);
    sessionStorage.setItem(sessionKey, id);
    return id;
  } catch {
    return randomId();
  }
}

function savedListeningSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(listeningSessionKey));
    if (saved?.session === session && Number.isSafeInteger(saved.hz)
        && saved.hz >= minimumHz && saved.hz <= maximumHz && typeof saved.mode === "string") return saved;
  } catch {
    // A tab with unavailable session storage can still use its live radio lease.
  }
  return null;
}

function rememberListeningSession() {
  if (restoringSession || !state.power || !Number.isSafeInteger(state.tune_hz)) return;
  const entry = receiver().frequency_mode === "memory" ? currentMemory() : null;
  const memory = entry?.hz === state.tune_hz && entry.mode === state.modulation
    ? {group: entry.group, slot: entry.slot} : null;
  try {
    sessionStorage.setItem(listeningSessionKey, JSON.stringify({session, hz: state.tune_hz,
      mode: vfoMode(), volume: state.volume, memory}));
  } catch {
    // A later refresh can still recover the live session if the lease exists.
  }
}

function clearListeningSession() {
  resumeSession = null;
  try { sessionStorage.removeItem(listeningSessionKey); } catch {}
}

function sessionUrl(path, includeRadio = true) {
  const url = new URL(path, location.origin);
  if (url.origin !== location.origin) throw new Error("The receiver only accepts same-site requests.");
  url.searchParams.set("session", session);
  if (includeRadio && state.radio_id !== null && state.radio_id !== undefined) {
    url.searchParams.set("radio", state.radio_id);
  }
  return url;
}

async function request(path, { method = "GET", body } = {}) {
  const response = await fetch(sessionUrl(path, path !== "/api/access" && path !== "/api/state"), {
    method, cache: "no-store",
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await response.json() : null;
  if (response.status === 401 && path !== "/api/access") {
    access = { ...access, ...(data || {}), pin_required: true };
    openPanel("pin");
    stopLiveAudio();
    throw new Error("Enter your remote access PIN.");
  }
  if (!response.ok) {
    if (response.status === 409 && path === "/api/action" && data?.radio_id !== undefined) applyState(data);
    throw new Error(data?.error || data?.v10_error ||
      (response.status === 409 ? "The receiver changed. Try the action again." : "Receiver request failed."));
  }
  return data;
}

function act(action) {
  ++actionCount;
  ++actionRevision;
  const result = actionChain.then(async () => {
    const payload = typeof action === "function" ? action() : action;
    if (!payload) return;
    const next = await request("/api/action", { method: "POST", body: payload });
    if (payload.v10 === "setting" && payload.key === "tune_step" && !next?.v10_error) {
      ui.stepOverride = null;
      lastMainTurn = -Infinity;
    }
    applyState(next);
    if (next?.v10_error) throw new Error(next.v10_error);
    if (payload.v10 === "scan" && payload.enabled) setPreference("last_scan", payload);
    return next;
  });
  actionChain = result.catch(() => {}).finally(() => { --actionCount; });
  return result;
}

async function refreshState() {
  const now = performance.now();
  if (stateRequestPending || actionCount || access.pin_required || now < nextStateRequestAt) return;
  stateRequestPending = true;
  nextStateRequestAt = now + displayUpdateInterval;
  const revision = actionRevision;
  try {
    const next = await request("/api/state");
    if (revision === actionRevision) {
      applyState(next);
      if (state.power) resumeSession = null;
      else if (resumeSession) await powerOn(true);
    }
  } catch (error) {
    if (!access.pin_required && revision === actionRevision) {
      view.status.textContent = "RECEIVER OFFLINE";
      syncMirror();
    }
  } finally {
    stateRequestPending = false;
  }
}

async function refresh() {
  await refreshState();
  setTimeout(refresh, document.hidden ? 950 : 230);
}

function applyState(next) {
  if (!next) return;
  lastStateAt = performance.now();
  if (next.power && next.volume > 0 && state.volume !== next.volume) {
    try { localStorage.setItem(preferencePrefix + "powerVolume", JSON.stringify(next.volume)); } catch {}
  }
  const wasOn = state.power;
  state = next;
  rememberListeningSession();
  if (!state.power && wasOn) {
    if (state.radio_id != null) clearListeningSession();
    if (!pinSetup) closeOverlay();
    ui.stepOverride = null;
    stopPlayback();
    resetScope();
  }
  ingestScope(state.waterfall);
  syncAudio();
  menu?.update?.();
  render();
}

function showError(error) {
  if (access.pin_required) return;
  view.message.textContent = error.message || String(error);
  openPanel("message");
}

function report(text) {
  ++reportTimer;
  const revision = reportTimer;
  ui.notice = text;
  render();
  setTimeout(() => {
    if (reportTimer === revision) {
      ui.notice = null;
      render();
    }
  }, 1800);
}

function showModes() {
  const supported = new Map((receiver().capabilities?.modes || defaultModes).map(name => [name.toUpperCase(), name]));
  if (receiver().capabilities?.auto_mode) supported.set("AUTO", "AUTO");
  const options = [...new Set([...modeOrder.filter(name => supported.has(name)), ...supported.keys()])].map(name => {
    const key = document.createElement("button");
    key.type = "button";
    key.className = "key";
    key.dataset.mode = supported.get(name);
    key.textContent = displayMode(name);
    key.title = modeHelp[name] || "Select " + key.textContent + " reception.";
    pressed(key, vfoMode().toUpperCase() === name);
    return key;
  });
  view.modes.replaceChildren(...options);
}

const activePanel = () => panels.find(panel => panel.dataset.panel === overlay);
const panelItems = () => [...(activePanel()?.querySelectorAll("button:not(:disabled), input") ?? [])]
  .filter(node => node.getClientRects().length);

function setOverlayCursor(node, focus = false) {
  overlayCursor?.style.removeProperty("outline");
  overlayCursor?.style.removeProperty("outline-offset");
  overlayCursor = node;
  if (!node) return;
  if (node instanceof HTMLInputElement) overlayField = node;
  node.style.setProperty("outline", "1px solid #75cee9");
  node.style.setProperty("outline-offset", "-2px");
  if (focus) node.focus({preventScroll: true});
  const panel = activePanel();
  const bounds = node.getBoundingClientRect();
  const frame = panel.getBoundingClientRect();
  if (bounds.top < frame.top) panel.scrollTop -= frame.top - bounds.top;
  else if (bounds.bottom > frame.bottom) panel.scrollTop += bounds.bottom - frame.bottom;
}

function moveOverlay(delta) {
  const items = panelItems();
  if (!items.length) return;
  const position = items.indexOf(overlayCursor);
  const index = position < 0 ? (delta > 0 ? 0 : items.length - 1)
    : ((position + delta) % items.length + items.length) % items.length;
  setOverlayCursor(items[index], true);
}

function selectOverlay() {
  const node = overlayCursor ?? panelItems()[0];
  if (node instanceof HTMLInputElement) {
    moveOverlay(1);
  } else {
    node?.click();
  }
}

function configurePinInput(setupCode) {
  const field = view["pin-input"];
  field.value = "";
  field.inputMode = setupCode ? "text" : "numeric";
  field.maxLength = setupCode ? 32 : 6;
  field.pattern = setupCode ? "[0-9A-Fa-f]{32}" : "[0-9]{6}";
  for (const key of lcd.querySelectorAll("[data-pin-code-key]")) key.hidden = !setupCode;
}

function openPanel(name) {
  clearTimeout(frequencyEntryTimer);
  if (name !== "favorite-clear") favoriteToClear = null;
  if (overlay === "pin" || name === "pin") {
    pinSetup = null;
    configurePinInput(false);
    view["pin-error"].hidden = true;
    setText("pin-title", "REMOTE ACCESS");
    setText("pin-label", "Enter PIN");
  }
  if (menu?.isOpen()) menu.close();
  setOverlayCursor(null);
  overlayField = null;
  overlay = name;
  panels.forEach(panel => { panel.hidden = panel.dataset.panel !== name; });
  lcdContent.inert = Boolean(name);
  control("mode").setAttribute("aria-expanded", String(name === "mode"));
  if (name === "mode") {
    showModes();
    setOverlayCursor(view.modes.querySelector('[aria-pressed="true"]') || panelItems()[0], true);
  }
  if (name === "frequency") {
    view["frequency-error"].hidden = true;
    view["frequency-input"].value = finite(state.tune_hz) ? (state.tune_hz / 1e6).toFixed(6) : "";
    setOverlayCursor(view["frequency-input"], true);
    view["frequency-input"].select();
  }
  if (name === "pin") {
    setOverlayCursor(view["pin-input"], true);
    view["pin-input"].select();
  }
  if (name === "scope-value" || name === "message" || name === "favorite-clear") setOverlayCursor(panelItems()[0], true);
  syncMirror();
}

function closeOverlay() {
  if (menu?.isOpen()) menu.close();
  openPanel(null);
}

function openMenu(page) {
  if (overlay) openPanel(null);
  menu.open(page);
}

function receiverHz() {
  return state.tune_hz ?? receiver().vfo?.hz ?? minimumHz;
}

async function tune(hz, mode = vfoMode()) {
  const caps = receiver().capabilities || {};
  if (!Number.isSafeInteger(hz) || hz < (caps.min_hz ?? minimumHz) || hz > (caps.max_hz ?? maximumHz)) {
    throw new Error("Enter a frequency from 0.100000 to 1750.000000 MHz.");
  }
  return act({ v10: "tune", hz, mode });
}

function parseFrequency(value, current = 0) {
  const entry = value.trim();
  if (!/^(?:\d+\.?\d*|\.\d+)$/.test(entry)) return NaN;
  const mhz = entry.startsWith(".") ? Math.floor(current / 1e6) + Number(entry) : Number(entry);
  return Math.round(mhz * 1e6);
}

async function wakeReceiver() {
  const saved = resumeSession;
  resumeSession = null;
  restoringSession = Boolean(saved);
  try {
    const volume = saved && finite(saved.volume) ? clamp(saved.volume, 0.01, 1) : lastVolume();
    const active = await act({knob: "volume", value: volume, panel: "v10"});
    if (!saved || !active?.power || active.radio_shared) return;
    const memory = active.v10?.memories?.find(entry => saved.memory && entry.group === saved.memory.group
      && entry.slot === saved.memory.slot && entry.hz === saved.hz && entry.mode === saved.mode);
    if (memory) await act({v10: "memory", operation: "recall", group: memory.group, slot: memory.slot});
    else if (active.tune_hz !== saved.hz || vfoMode() !== saved.mode) await tune(saved.hz, saved.mode);
  } catch (error) {
    if (saved && !state.power) resumeSession = saved;
    throw error;
  } finally {
    restoringSession = false;
    rememberListeningSession();
  }
}

async function powerOn(resuming = false) {
  if (powerPending) return;
  powerPending = true;
  try {
    if (resuming) void prepareAudio().catch(() => {});
    else await prepareAudio();
    access = await request("/api/access");
    if (access.pin_required) {
      openPanel("pin");
      view["pin-error"].textContent = access.pin_configured ?
        "Enter the PIN to switch on the receiver." : "Program a remote PIN from the local panel first.";
      view["pin-error"].hidden = false;
      return;
    }
    closeOverlay();
    await wakeReceiver();
  } finally {
    powerPending = false;
  }
}

async function unlock(event) {
  event.preventDefault();
  const field = view["pin-input"];
  const error = view["pin-error"];
  if (pinSetup) return savePin(field);
  try {
    await prepareAudio();
    access = await request("/api/access", { method: "POST", body: { pin: field.value } });
    if (access.pin_required) throw new Error("The PIN was refused.");
    field.value = "";
    error.hidden = true;
    closeOverlay();
    await wakeReceiver();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
    field.select();
  }
}

function openPinSetup() {
  if (!canSetPin()) return;
  openPanel("pin");
  pinSetup = {firstEntry: null, saving: false, needsCode: !access.can_program_pin, setupCode: null};
  setText("pin-title", "SET PIN");
  configurePinInput(pinSetup.needsCode);
  setText("pin-label", pinSetup.needsCode ? "One-time setup code from the scanner host" : "New six-digit PIN");
  syncMirror();
}

async function savePin(field) {
  const setup = pinSetup;
  if (setup.saving || !canSetPin()) return;
  const digits = field.value;
  field.value = "";
  setOverlayCursor(field, true);
  const error = view["pin-error"];
  error.hidden = true;
  if (setup.needsCode) {
    setup.setupCode = digits.toLowerCase();
    setup.needsCode = false;
    configurePinInput(false);
    setText("pin-label", "New six-digit PIN");
    return;
  }
  if (setup.firstEntry === null) {
    setup.firstEntry = digits;
    setText("pin-label", "Confirm new PIN");
    return;
  }
  const matches = digits === setup.firstEntry;
  setup.firstEntry = null;
  setText("pin-label", "New six-digit PIN");
  if (!matches) {
    error.textContent = "The PINs differ. Enter the new PIN again.";
    error.hidden = false;
    return;
  }
  setup.saving = true;
  try {
    const body = setup.setupCode ? {new_pin: digits, setup_code: setup.setupCode} : {new_pin: digits};
    access = await request("/api/access", {method: "POST", body});
    if (pinSetup === setup) openMenu("set");
  } catch {
    if (pinSetup !== setup) return;
    if (setup.setupCode) {
      setup.setupCode = null;
      setup.needsCode = true;
      configurePinInput(true);
      setText("pin-label", "One-time setup code from the scanner host");
      error.textContent = "Could not save the PIN. Check the setup code and try again.";
    } else error.textContent = "Could not save the PIN. Try again.";
    error.hidden = false;
  } finally {
    setup.saving = false;
  }
}

async function powerOff() {
  clearListeningSession();
  closeOverlay();
  await act({ knob: "volume", value: 0 });
}

function audioLevel() {
  return state.power && !state.muted && !volumeMuted ? Math.pow(audioVolume / 100, 1.45) : 0;
}

function updateAudioGain() {
  const level = audioLevel();
  const boostDb = clamp(Number(getPreference("audio_boost", 0)) || 0, 0, maxBoostDb);
  const boost = 10 ** (boostDb / 20);
  audioGain?.gain.setTargetAtTime(playback ? 0 : level * boost / maxBoostGain, audioContext.currentTime, 0.015);
  if (playback) {
    playback.muted = level === 0;
    playback.volume = level;
  }
}

function liveAudioLimiter() {
  const limiter = audioContext.createWaveShaper();
  const curve = new Float32Array(4097);
  for (let index = 0; index < curve.length; index++) {
    const sample = (2 * index / (curve.length - 1) - 1) * maxBoostGain;
    const magnitude = Math.abs(sample);
    // Leave headroom for the separately mixed confirmation tones.
    curve[index] = magnitude <= 0.85 ? sample : Math.sign(sample) * (0.85 + 0.07 * Math.tanh((magnitude - 0.85) / 0.07));
  }
  limiter.curve = curve;
  return limiter;
}

async function prepareAudio() {
  if (isMirror) return;
  if (audioStarting) return audioStarting;
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) return;
  if (!audioContext) {
    audioContext = new Audio({ latencyHint: "interactive" });
    audioGain = audioContext.createGain();
    audioGain.gain.value = 0;
    audioGain.connect(liveAudioLimiter()).connect(audioContext.destination);
    audioStarting = (async () => {
      const resume = audioContext.resume().catch(() => {});
      try {
        await audioContext.audioWorklet.addModule("/pcm-worklet.js");
        audioNode = new AudioWorkletNode(audioContext, "scanner-pcm", {
          outputChannelCount: [1], processorOptions: { inputRate, fadeSeconds: audioFadeSeconds },
        });
      } catch {
        audioBuffer = new PcmBuffer(inputRate, audioContext.sampleRate, audioFadeSeconds);
        audioNode = audioContext.createScriptProcessor(1024, 0, 1);
        audioNode.onaudioprocess = event => audioBuffer.readInto(event.outputBuffer.getChannelData(0));
      }
      audioNode.connect(audioGain);
      // Some browsers keep resume pending until their first explicit activation.
      void resume;
      syncAudio();
    })().finally(() => { audioStarting = null; });
    return audioStarting;
  }
  if (audioContext.state === "suspended") void audioContext.resume().catch(() => {});
}

function resetAudioBuffer() {
  if (audioBuffer) audioBuffer.reset();
  else audioNode?.port?.postMessage("clear");
}

function stopLiveAudio() {
  clearTimeout(audioRetry);
  audioRetry = null;
  audioRequest?.abort();
  audioRequest = null;
  resetAudioBuffer();
}

function syncAudio() {
  if (!audioNode) return;
  const connected = state.power && state.radio_id !== null && state.radio_id !== undefined && !access.pin_required;
  const signature = connected ? [state.radio_id, state.tune_hz, state.modulation, state.muted].join(":") : null;
  if (signature !== audioSignature) {
    if (!connected || (audioSignature && signature.split(":")[0] !== audioSignature.split(":")[0])) stopLiveAudio();
    else resetAudioBuffer();
    audioSignature = signature;
  }
  updateAudioGain();
  if (connected && !audioRequest && !audioRetry) void readAudio();
}

async function readAudio() {
  const controller = new AbortController();
  audioRequest = controller;
  let leftover = null;
  try {
    const response = await fetch(sessionUrl("/audio"), { cache: "no-store", signal: controller.signal });
    if (!response.ok || !response.body) throw new Error("Audio is temporarily unavailable.");
    const reader = response.body.getReader();
    while (!controller.signal.aborted) {
      const { value: bytes, done } = await reader.read();
      if (done || controller.signal.aborted) break;
      const input = new Uint8Array(bytes.length + Number(leftover !== null));
      if (leftover !== null) input[0] = leftover;
      input.set(bytes, input.length - bytes.length);
      leftover = input.length % 2 ? input[input.length - 1] : null;
      const samples = new Float32Array(Math.floor(input.length / 2));
      const pcm = new DataView(input.buffer);
      for (let index = 0; index < samples.length; index++) samples[index] = pcm.getInt16(index * 2, true) / 32768;
      if (audioBuffer) audioBuffer.enqueue(samples);
      else audioNode.port.postMessage(samples, [samples.buffer]);
    }
  } catch {
    // A power change, reassignment, or dropped connection interrupts the stream.
  } finally {
    if (controller === audioRequest) {
      audioRequest = null;
      resetAudioBuffer();
      audioRetry = setTimeout(() => {
        audioRetry = null;
        syncAudio();
      }, 500);
    }
  }
}

function stopPlayback() {
  const current = playback;
  playback = null;
  current?.pause();
  syncAudio();
}

async function play(path) {
  stopPlayback();
  const current = new Audio(sessionUrl(path));
  playback = current;
  current.volume = audioLevel();
  current.addEventListener("ended", () => { if (playback === current) stopPlayback(); }, { once: true });
  try {
    syncAudio();
    await current.play();
  } catch (error) {
    if (playback === current) stopPlayback();
    throw error;
  }
}

function resetScope() {
  ui.frame = null;
  ui.heldCenter = null;
  ui.history = [];
  ui.average = null;
  ui.peakTrace = null;
  ui.lastLineAt = 0;
}

function spanHz() {
  return Math.min(ui.span, captureSpan());
}

function captureSpan() {
  return ui.frame?.span_hz ?? spans.at(-1);
}

function scopeSpans() {
  const limit = captureSpan();
  return [...new Set([...spans.filter(span => span <= limit), limit])];
}

function scopeSpanLabel(hz) {
  if (hz >= 1e6) return (hz / 1e6) + " MHz";
  return hz >= 1000 ? (hz / 1000) + " kHz" : hz + " Hz";
}

function ingestScope(frame) {
  if ((ui.freeze && ui.frame) || !frame?.levels?.length || !finite(frame.center_hz) || !finite(frame.span_hz)) return;
  if (ui.frame?.sequence === frame.sequence && ui.frame?.time_ms === frame.time_ms) return;
  const consecutive = ui.frame?.center_hz === frame.center_hz && ui.frame?.span_hz === frame.span_hz;
  const sweep = getPreference("scope_sweep_speed", "mid");
  const sampleInterval = { slow: 420, mid: 180, fast: 0 }[sweep] ?? 0;
  if (consecutive && frame.time_ms - ui.frame.time_ms < sampleInterval) return;
  if (ui.frame && !consecutive) {
    // Reset the running spectrum statistics, but keep waterfall rows and their frequency ranges.
    ui.average = null;
    ui.peakTrace = null;
    ui.lastLineAt = 0;
  }
  ui.frame = frame;
  if (ui.freeze) ui.heldCenter = receiverHz();
  const width = frame.levels.length;
  const average = clamp(getPreference("scope_averaging", 0), 0, 4);
  if (average && ui.average?.length === width) {
    ui.average = frame.levels.map((value, index) => (value + ui.average[index] * average) / (average + 1));
  } else ui.average = frame.levels;
  const maxHold = getPreference("scope_max_hold", "off");
  if (maxHold !== "off") {
    const age = maxHold === "10s" ? 10000 : Infinity;
    if (!ui.peakTrace || Date.now() - ui.peakStart >= age) {
      ui.peakTrace = frame.levels;
      ui.peakStart = Date.now();
    } else ui.peakTrace = ui.peakTrace.map((value, index) => Math.max(value, frame.levels[index]));
  } else ui.peakTrace = null;
  const speed = getPreference("scope_waterfall_speed", "mid");
  const delay = { slow: 690, mid: 380, fast: 0 }[speed] ?? 380;
  const now = Date.now();
  if (now - ui.lastLineAt >= delay) {
    ui.lastLineAt = now;
    ui.history.unshift(frame);
    ui.history.length = Math.min(ui.history.length, 88);
  }
}

function scopeRange() {
  const frame = ui.frame;
  if (ui.fixed && ui.edges) return ui.edges;
  const center = (ui.freeze ? ui.heldCenter : null) ?? receiverHz();
  const span = spanHz();
  const {min_hz = minimumHz, max_hz = maximumHz} = receiver().capabilities ?? {};
  let low = clamp(center - span / 2, min_hz, max_hz - span);
  if (frame) {
    const captureLow = frame.center_hz - frame.span_hz / 2;
    const captureHigh = frame.center_hz + frame.span_hz / 2;
    // The exported FFT shifts at the band limits to keep the tuned signal in view.
    if (center >= captureLow && center <= captureHigh) {
      low = clamp(low, captureLow, captureHigh - span);
    }
  }
  return [low, low + span];
}

const scopeFrequencyAt = (range, fraction) => range[0] + (range[1] - range[0]) * fraction;

function sampleAt(frame, row, hz) {
  const index = Math.floor((hz - frame.center_hz + frame.span_hz / 2) / frame.span_hz * row.length);
  return index >= 0 && index < row.length ? row[index] : null;
}

// Turbo-style waterfall: a dark low end and warm colours for strong carriers.
const scopeColorStops = [0x050a12, 0x101d46, 0x233c86, 0x266bdd, 0x1baddb, 0x26c995,
  0x87d94d, 0xdadf43, 0xffbf3e, 0xf6832e, 0xe44824, 0xc51d21];
const scopeColors = Array.from({length: 256}, (_, index) => {
  const position = index * (scopeColorStops.length - 1) / 255;
  const left = Math.floor(position);
  const right = Math.min(left + 1, scopeColorStops.length - 1);
  return [16, 8, 0].map(shift => {
    const from = (scopeColorStops[left] >> shift) & 255;
    const to = (scopeColorStops[right] >> shift) & 255;
    return Math.round(from + (to - from) * (position - left));
  });
});

const waterfallNoiseMarginDb = 5;
const waterfallContrastDb = 40;
const waterfallFloors = new WeakMap();

function waterfallFloor(row) {
  if (!waterfallFloors.has(row)) {
    const levels = row.filter(finite).sort((left, right) => left - right);
    // Use the lower part of the FFT so strong carriers do not lift the background.
    const noise = levels[Math.floor(levels.length * 0.2)];
    waterfallFloors.set(row, noise);
  }
  return waterfallFloors.get(row);
}

function spectrumY(value, height, reference) {
  const fraction = clamp((value + 95 - reference) / 65, 0, 1);
  return height - 2 - fraction * (height - 6);
}

function drawTrace(range, frame, row, divide, reference, color, fill) {
  const { width } = canvas;
  let began = false;
  let first = 0;
  let last = 0;
  let peak = null;
  context.beginPath();
  for (let x = 0; x < width; x++) {
    const column = x + 0.5;
    const value = sampleAt(frame, row, scopeFrequencyAt(range, column / width));
    if (value === null) continue;
    const y = spectrumY(value, divide, reference);
    if (began) context.lineTo(column, y);
    else {
      first = column;
      began = true;
      context.moveTo(column, y);
    }
    last = column;
    if (peak === null || value > peak.value) peak = { x: column, y, value };
  }
  if (began && fill) {
    context.lineTo(last, divide);
    context.lineTo(first, divide);
    context.closePath();
    context.fillStyle = "#163952";
    context.fill();
  } else if (began) {
    context.strokeStyle = color;
    context.stroke();
  }
  return peak;
}

let previousScopeDraw;
function drawScope(frame, history, range, options, average, maxTrace) {
  view["scope-nearby-sweep"].hidden = !options.nearbySweep;
  const listening = Boolean(options.nearbyListening);
  lcdContent.dataset.nearbyListening = String(listening);
  view["frequency-listening"].hidden = !listening;
  view["waterfall-listening"].hidden = !listening || !options.waterfall;
  const { width, height } = canvas;
  const showWaterfall = options.waterfall;
  const divide = showWaterfall ? Math.floor(height * scopeSpectrumFraction) : height;
  const reference = Number(options.reference) || 0;
  const drawKey = [frame?.sequence, frame?.time_ms, range[0], range[1], options.marker, options.hz,
    reference, options.squelchDb, options.waterfall, options.scopeHold, options.maxHold, history.length].join(":");
  if (drawKey === previousScopeDraw) return;
  previousScopeDraw = drawKey;
  context.fillStyle = "#050a12";
  context.fillRect(0, 0, width, height);
  if (frame && showWaterfall) {
    const strip = context.createImageData(width, height - divide);
    for (let y = 0; y < strip.height && y < history.length; y++) {
      const historical = history[y];
      const historicalFrame = Array.isArray(historical) ? frame : historical;
      const row = historical.levels ?? historical;
      const noise = waterfallFloor(row);
      if (!finite(noise)) continue;
      const bottom = noise + waterfallNoiseMarginDb + reference;
      for (let x = 0; x < width; x++) {
        const value = sampleAt(historicalFrame, row, scopeFrequencyAt(range, (x + 0.5) / width));
        if (!finite(value)) continue;
        const fraction = clamp((value - bottom) / waterfallContrastDb, 0, 1);
        const color = scopeColors[Math.round(fraction * (scopeColors.length - 1))];
        const pixel = 4 * (y * width + x);
        strip.data.set(color, pixel);
        strip.data[pixel + 3] = 255;
      }
    }
    const buffer = scopeBitmap();
    buffer.getContext("2d").putImageData(strip, 0, 0);
    context.drawImage(buffer, 0, 0, width, strip.height, 0, divide, width, strip.height);
  }
  context.strokeStyle = "#294149";
  context.beginPath();
  for (let column = 1; column < 10; column++) {
    const x = Math.round(width / 10 * column) + 0.5;
    context.moveTo(x, 0); context.lineTo(x, divide);
  }
  for (let row = 1; row < 6; row++) {
    const y = Math.round(divide / 6 * row) + 0.5;
    context.moveTo(0, y); context.lineTo(width, y);
  }
  context.stroke();
  if (frame) {
    const row = average?.length ? average : frame.levels;
    drawTrace(range, frame, row, divide, reference, "", true);
    if (maxTrace?.length) drawTrace(range, frame, maxTrace, divide, reference, "#6d9867", false);
    const peak = drawTrace(range, frame, row, divide, reference, "#68e4f2", false);
    if (options.marker && peak) {
      context.fillStyle = "#ffc985";
      context.beginPath();
      context.moveTo(peak.x - 5, peak.y - 9);
      context.lineTo(peak.x + 5, peak.y - 9);
      context.lineTo(peak.x, peak.y);
      context.fill();
    }
  }
  const x = (options.hz - range[0]) / (range[1] - range[0]) * width;
  if (frame && x >= 0 && x <= width) {
    context.strokeStyle = "#7ab491";
    context.setLineDash([4, 4]);
    context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke();
    context.setLineDash([]);
  }
  const squelch = view["scope-squelch"];
  squelch.hidden = !finite(options.squelchDb);
  if (!squelch.hidden) {
    const y = Math.round(spectrumY(options.squelchDb, divide, reference)) + 0.5;
    setText("scope-squelch", "SQL " + options.squelchDb.toFixed(1) + " dBFS");
    squelch.style.setProperty("--scope-squelch-y", (y / height * 100) + "%");
    context.save();
    context.strokeStyle = "#ffc985";
    context.setLineDash([6, 3]);
    context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
    context.restore();
  }
}

let bitmap;
function scopeBitmap() {
  if (!bitmap) {
    bitmap = document.createElement("canvas");
    bitmap.width = canvas.width;
    bitmap.height = canvas.height;
  }
  bitmap.getContext("2d").clearRect(0, 0, bitmap.width, bitmap.height);
  return bitmap;
}

let displayedScopeRange;
let scopePointer;
function scopeHzAt(event, bounds = canvas.getBoundingClientRect()) {
  const border = canvas.clientLeft;
  const width = bounds.width - 2 * border;
  const fraction = width > 0 ? clamp((event.clientX - bounds.left - border) / width, 0, 1) : 0;
  return Math.round(scopeFrequencyAt(displayedScopeRange ?? scopeRange(), fraction));
}

function scopeWheelStep() {
  const [low, high] = displayedScopeRange ?? scopeRange();
  return Math.max(1, Math.round((high - low) / 100));
}

function scopeSelectionAt(event) {
  const samePointer = event.pointerType !== "touch" && !event.sourceCapabilities?.firesTouchEvents && scopePointer
    && Math.abs(scopePointer.clientX - event.clientX) < 1 && Math.abs(scopePointer.clientY - event.clientY) < 1;
  return scopeHzAt(samePointer ? scopePointer : event);
}

function showScopeTip(event) {
  if (event.pointerType === "touch") return;
  scopePointer = {clientX: event.clientX, clientY: event.clientY};
  refreshScopeTip();
}

function refreshScopeTip() {
  const tip = view["scope-tip"];
  if (!scopePointer || !ui.frame || !ui.visible || !state.power || overlay || menu?.isOpen()) {
    tip.hidden = true;
    return;
  }
  const {clientX, clientY} = scopePointer;
  const plot = canvas.getBoundingClientRect();
  if (clientX < plot.left || clientX > plot.right || clientY < plot.top || clientY > plot.bottom) {
    tip.hidden = true;
    return;
  }
  const bounds = lcd.getBoundingClientRect();
  tip.textContent = formatAxis(scopeHzAt(scopePointer, plot)) + " MHz";
  tip.hidden = false;
  tip.style.left = clamp(clientX - bounds.left + 8, 4, lcd.clientWidth - tip.offsetWidth - 4) + "px";
  tip.style.top = Math.max(3, clientY - bounds.top - 25) + "px";
}

function updateScope() {
  const range = scopeRange();
  displayedScopeRange = range;
  const validFrame = Boolean(ui.frame);
  view["axis-left"].textContent = validFrame ? formatAxis(range[0]) : "—";
  view["axis-center"].textContent = validFrame ? formatAxis(scopeFrequencyAt(range, 0.5)) + " MHz" : "—";
  view["axis-right"].textContent = validFrame ? formatAxis(range[1]) : "—";
  view["scope-hold"].textContent = receiver().nearby_receiving ? (receiver().nearby_held ? "HOLD" : "")
    : zoneScanning() && receiver().scan_held ? "SCAN HOLD" : ui.freeze ? "HOLD" : "";
  view["scope-reference-mode"].textContent = ui.fixed ? "FIX" : "CENTER";
  view["scope-grid"].textContent = validFrame ? "Grid " + stepLabel((range[1] - range[0]) / 10) : "";
  const scopeOptions = { marker: ui.marker, hz: receiverHz(), reference: getPreference("scope_reference", 0),
    waterfall: getPreference("scope_waterfall", true), scopeHold: ui.freeze,
    maxHold: getPreference("scope_max_hold", "off"), nearbySweep: Boolean(state.power && receiver().nearby_sweeping),
    nearbyListening: state.power && state.receiver === "ready" && receiver().nearby_sweeping && !activeScan(),
    squelchDb: state.power && (isMirror || state.squelch > 0) ? state.squelch_db : null };
  drawScope(ui.frame, ui.history, range, scopeOptions, ui.average, ui.peakTrace);
  if (scopePointer) refreshScopeTip();
  return { frame: ui.frame, history: ui.history, range, options: scopeOptions, average: ui.average, maxTrace: ui.peakTrace };
}

function stepLabel(hz) {
  if (hz >= 1000000) return (hz / 1e6) + " MHz";
  return hz >= 1000 ? (hz / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 }) + "k" : hz + "Hz";
}

function setText(key, content) {
  if (view[key].textContent !== content) view[key].textContent = content;
}

function renderFrequency(hz) {
  const text = formatFrequency(hz);
  if (frequency.textContent === text) return;
  if (!finite(hz)) {
    frequency.textContent = text;
    return;
  }
  let place = text.replaceAll(".", "").length - 1;
  frequency.replaceChildren(...Array.from(text, character => {
    if (character === ".") return character;
    const digit = document.createElement("span");
    digit.dataset.stepHz = String(10 ** place);
    digit.textContent = character;
    place--;
    return digit;
  }));
}

function currentMemory() {
  return (receiver().memories || []).find(item => item.group === receiver().group && item.slot === receiver().slot);
}

function favoriteMemory(bank, slot) {
  return receiver().memories?.find(entry => entry.group === bank && entry.slot === slot);
}

function renderFavorites() {
  for (const key of favoriteKeys) {
    const bank = Number(key.dataset.bank);
    const slot = Number(key.dataset.slot);
    const entry = favoriteMemory(bank, slot);
    const current = Boolean(state.power && entry && state.tune_hz === entry.hz && state.modulation === entry.mode);
    const lamp = key.querySelector(".lamp");
    lamp?.classList.toggle("green", current);
    lamp?.classList.toggle("amber", Boolean(state.power && entry && !current));
    pressed(key, current);
    key.title = entry ? [entry.name, (entry.hz / 1e6).toFixed(6) + " MHz", entry.mode,
      "Click to recall. Hold to replace. Right-click twice to clear."].filter(Boolean).join(" · ")
      : "Empty favourite. Hold to save the tuned frequency.";
  }
}

function dmrDetails() {
  const call = state.dmr?.call || receiver().trunk?.call;
  const filters = state.dmr?.entry || settings().dmr_squelch || {};
  const color = call?.color_code ?? filters.color_code;
  const slot = call?.slot ?? filters.slot;
  const target = call?.talkgroup ?? filters.talkgroup;
  const source = call?.unit_id ?? call?.source ?? call?.source_id;
  return [
    ["CC " + (color ?? "ANY"), "TS " + (slot ?? "ANY")].join(" · "),
    [target != null ? "TG " + target : "", source != null ? "ID " + source : ""].filter(Boolean).join(" · "),
  ];
}

function digitalDetails(call, mode) {
  if (!call) return ["", ""];
  const source = call.unit_id ?? call.source;
  const talkgroup = mode === "P25" && call.talkgroup != null
    ? formatP25Talkgroup(call.talkgroup, getPreference("p25_rx_id_format", "decimal"))
    : call.talkgroup;
  return [
    [call.receive_code != null ? (call.receive_kind || "CODE") + " " + call.receive_code : "",
      source != null ? "ID " + source : ""].filter(Boolean).join(" · "),
    [talkgroup != null ? "TG " + talkgroup : "",
      call.destination != null ? "TO " + call.destination : ""].filter(Boolean).join(" · "),
  ];
}

function receiverModeLabel(mode) {
  if (!mode) return "";
  if (receiver().nearby_receiving) return "AUTO";
  return receiver().auto_mode ? "AUTO " + displayMode(mode) : displayMode(mode);
}

function nearbyWatchLabel() {
  return state.power && receiver().nearby_enabled && !receiver().nearby_receiving ? "NEARBY WATCH" : "";
}

function customPresetName(zone) {
  return receiver().custom_presets?.[zone]?.name || receiver().custom_preset_names?.[zone];
}

function receiverStatus() {
  const trunk = receiver().trunk;
  if (trunk?.enabled) return trunk.phase?.replaceAll("_", " ").toUpperCase() || "TIER III";
  if (["error", "unavailable"].includes(state.receiver)) return "SDR UNAVAILABLE";
  if (state.dmr?.error) return "DMR DECODER ERROR";
  if (state.digital?.error) return (displayMode(state.digital.mode) || "DIGITAL") + " DECODER ERROR";
  if (state.receiver === "starting") return "LOADING";
  if (receiver().nearby_receiving) return receiver().nearby_held ? "NEARBY HOLD" : "NEARBY RX";
  if (receiver().monitor || ui.monitor) return "MONI";
  if (receiver().scan_kind || state.scan_active || state.search_active) {
    const kind = receiver().scan_kind;
    const zone = receiver().scan_zone;
    const scan = kind === "SIMPLE" ? "S.SCAN" : customPresetName(zone) || ZONE_NAMES[zone] || kind || "SCAN";
    if (kind === "ZONE") {
      if (receiver().scan_held) return scan + " HOLD";
      if (receiver().scan_paused) return (customPresetName(zone) || scan.toUpperCase()) + " PAUSE";
    }
    return [scan, receiver().scan_direction === -1 ? "◀" : "▶"].join(" ");
  }
  if (state.digital?.call?.encrypted) return "ENCR";
  if (state.audio_open) return "RECEIVE";
  if (state.digital?.call?.sync) return "DIGITAL SYNC";
  return "";
}

function setLcdExpanded(expanded, resized = false) {
  if (expanded && (resized || lcd.dataset.expansion !== "true")) {
    lcd.dataset.expansion = "false";
    lcd.style.setProperty("--normal-lcd-height", lcd.getBoundingClientRect().height + "px");
  }
  lcd.dataset.expansion = String(expanded);
}

const dialRoleHelp = {
  pause_time: "Set how long scanning pauses on a signal.",
  scan_speed: "Set how fast analogue scanning checks channels.",
  resume_time: "Set how long scanning waits after the signal ends.",
  priority_interval: "Set the time between priority-channel checks.",
  volume: "Adjust listening volume.", audio_boost: "Boost live listening audio without changing AF volume.",
  squelch: "Set the signal level needed to open the audio.", rf_gain: "Adjust gain at the SDR input.",
  tone_treble: "Adjust the higher audio frequencies.", tone_bass: "Adjust the lower audio frequencies.",
  beep_level: "Adjust the loudness of confirmation beeps.",
  "memory-channel": "Step through saved channels.", "fine-tuning": "Tune in fixed 10 Hz steps.",
  "coarse-tuning": "Tune in fixed 10 kHz steps.", "tuning-step": "Change the increment used for normal tuning.",
  "scope-span": "Change the range of frequencies visible on the scope.",
  scope_reference: "Shift the scope display level and waterfall contrast.",
  "scan-direction": "Change direction during an active scan.", tuning: "Tune using the selected tuning step.",
};

const dialPressHelp = {
  "dial-a": "Press for scan settings.",
  "dial-b": "Press for audio and reception settings.",
  "dial-c": "Press for mode-specific receive settings.",
  "dial-d": "Press to choose this dial’s function.",
  "dial-e": "Press to choose this dial’s function.",
  frequency: "Press to open frequency entry.",
};
const dialDisplayNames = {rf_gain: "RF Gain", squelch: "Squelch", audio_boost: "Audio Boost",
  tone_treble: "Treble", tone_bass: "Bass", scan_speed: "Scan Speed",
  resume_time: "Resume Time", priority_interval: "Priority Interval"};

function updateDialTooltips() {
  const overlayLabels = {
    pin: "Choose a PIN keypad key", mode: "Choose a receive mode", frequency: "Choose a frequency keypad key",
    "scope-value": "Choose a scope editor key", message: "Choose an on-screen button",
    "favorite-clear": "Cancel clearing the saved favourite",
  };
  for (const name of dialNames) {
    const dial = knob(name);
    const assignment = menu?.assignment(name);
    if (assignment) {
      const id = name === "frequency" ? "MAIN" : name.at(-1).toUpperCase();
      const functionName = dialDisplayNames[assignment.key] ?? assignment.label;
      const labelNode = dial.previousElementSibling;
      const visible = `${id} - ${functionName}`;
      if (labelNode.textContent !== visible) labelNode.textContent = visible;
      const accessible = `${name === "frequency" ? "Main dial" : "Dial " + id}, ${functionName}`;
      if (dial.getAttribute("aria-label") !== accessible) dial.setAttribute("aria-label", accessible);
    }
    let label = assignment?.label || dial.getAttribute("aria-label");
    let role = assignment?.key;
    if (name === "frequency") {
      if (ui.dialLock) { label = "Tuning dial locked"; role = null; }
      else if (activeScan() && (activeScan() === "SIMPLE" || ui.channelDial || role === "tuning")) { label = "Scan Direction"; role = "scan-direction"; }
      else if (ui.channelDial) { label = "Memory Channel"; role = "memory-channel"; }
    }
    const scopeReference = overlay === "scope-value" && ui.scopeForm === "ref"
      && overlayCursor === overlayField && name !== "dial-c";
    const contextual = menu?.dialFunction(name) || (scopeReference ? "Scope reference level" : overlayLabels[overlay]);
    const current = (contextual ? contextual + ". Turn to navigate or adjust. Right-click to go back."
      : [label + ".", dialRoleHelp[role], dialPressHelp[name]].filter(Boolean).join(" "))
      + " Hold to change this dial’s assignment.";
    if (dial.title !== current) dial.title = current;
  }
}

function assignedDialAngle(name) {
  if (name === "frequency" && (ui.channelDial || activeScan() === "SIMPLE")) return undefined;
  const fraction = menu?.dialSettingFraction(name);
  return finite(fraction) ? clamp(fraction, 0, 1) * 270 - 135 : undefined;
}

function render() {
  rememberPanelSettings();
  const status = receiverStatus();
  const memory = currentMemory();
  const tuningMode = receiver().frequency_mode || "vfo";
  const mode = state.modulation || "FM";
  const dmr = mode === "DMR";
  const trunk = dmr && receiver().trunk?.enabled ? receiver().trunk : null;
  const name = trunk?.profile?.name || (tuningMode === "memory" && getPreference("display_memory", true) ? memory?.name : "");
  const level = state.signal_meter_dbfs ?? state.signal_dbfs;
  const meter = finite(level) ? clamp((level + 92) / 66, 0, 1) : 0;
  const meterType = getPreference("meter_type", "dbfs");
  const radioSettings = settings();
  const step = tuningStep();
  const filter = radioSettings.filter ?? radioSettings.if_filter ?? "FIL1";
  const showRds = getPreference("display_rds", true);
  const rds = showRds && mode === "WFM" ? state.rds : null;
  const flags = [
    receiver().current_lockout || memory?.skip ? "L/O" : "",
    receiver().priority?.enabled ? "PRIO" : "",
    state.muted ? "MUTE" : "",
    supportsSsbDsp(mode) && radioSettings.noise_blanker === true ? "NB" : "",
    hasAnalogFilter(mode) && radioSettings.noise_reduction === true ? "NR" : "",
  ];
  const dialDetails = dmr ? dmrDetails() : state.digital ? digitalDetails(state.digital.call, mode) : [
    [supportsSsbDsp(mode) && radioSettings.agc ? "AGC-" + String(radioSettings.agc).charAt(0).toUpperCase() : "",
      state.channel_tone?.label || (state.tone_filter ? "TSQL" : "")].filter(Boolean).join(" · "),
    rds?.ps || rds?.name || "",
  ];
  renderFrequency(state.tune_hz);
  frequency.title = [finite(state.tune_hz) ? (state.tune_hz / 1e6).toFixed(6) + " MHz." : "",
    "Click to enter a frequency. Double-click to copy it. Scroll over a digit to adjust its place value."].filter(Boolean).join(" ");
  setText("mode", receiverModeLabel(mode));
  view.mode.title = modeHelp[mode.toUpperCase()] || "Current receive mode.";
  setText("top-state", tuningMode === "memory" ? "MEMO" : "VFO");
  setText("memory-number", tuningMode === "memory" && receiver().slot != null ? digits(receiver().slot) : "");
  setText("memory-group", receiver().group === 100 ? "A" : receiver().group != null ? digits(receiver().group) : "");
  setText("clock", receiver().clock?.local ?? clockFormat.format(new Date()));
  const levelText = !finite(level) ? "—" :
    meterType === "relative" ? Math.round(meter * 100) + "% REL" : level.toFixed(1) + " dBFS";
  setText("level", levelText);
  const adjustableFilter = hasAnalogFilter(mode) && receiver().capabilities?.rf?.includes("bandwidth_hz");
  setText("filter", adjustableFilter ? String(filter).toUpperCase() : "");
  view.filter.hidden = !adjustableFilter;
  view.filter.title = (radioSettings.bandwidth_hz ? stepLabel(radioSettings.bandwidth_hz) + "Hz. " : "")
    + "Choose a filter or change its receive bandwidth.";
  setText("rf-options", state.radio_shared ? "LAN" : "");
  setText("rec", state.record_active ? "●" : state.record_enabled ? "Ⅱ" : "");
  setText("antenna", radioSettings.antenna ? "ANT" + radioSettings.antenna : "");
  setText("site", name || "");
  setText("flags", flags.filter(Boolean).join(" "));
  setText("detail-a", dialDetails[0]);
  setText("detail-b", dialDetails[1]);
  setText("rds-text", rds?.text || "");
  view["rds-text"].title = rds?.text || "";
  view["rds-text"].hidden = !showRds;
  setText("status", ui.notice || status);
  view.status.title = trunk?.error || state.dmr?.error || state.digital?.error || receiver().error || "";
  setText("offset", finite(state.tuning_offset_hz) ? Math.round(state.tuning_offset_hz) + " Hz" : "");
  setText("step", "TS " + stepLabel(step));
  root.querySelector(".meter-body i").style.width = meter * 100 + "%";
  control("power").querySelector(".lamp").classList.toggle("blue", Boolean(state.power));
  pressed(control("power"), state.power);
  control("power").title = getPreference("screen_capture", false)
    ? "Press to power on or capture the display. Hold for one second to return to standby."
    : "Press to power on. Hold for one second to return to standby.";
  const record = control("record");
  const armed = Boolean(state.power && state.record_enabled);
  const recording = Boolean(armed && state.record_active);
  pressed(record, armed);
  const recordLamp = record.querySelector(".lamp");
  recordLamp.classList.toggle("amber", armed && !recording);
  recordLamp.classList.toggle("red", recording);
  record.title = recording ? "Recording. Click to stop." : armed ? "Recording armed. Click to disarm."
    : "Arm recording. Audio is saved when a signal opens squelch.";
  control("speech").title = getPreference("speech_lock_switch", "speech") === "lock"
    ? "Lock or unlock main tuning. Hold to announce the tuned frequency and mode."
    : "Announce the tuned frequency and mode. Hold to lock or unlock main tuning.";
  view.receive.classList.toggle("green", Boolean(state.audio_open || receiver().monitor || ui.monitor));
  view.lock.classList.toggle("white", ui.dialLock);
  pressed(control("lockout"), receiver().current_lockout);
  pressed(control("scan"), Boolean(activeScan() && activeScan() !== "SIMPLE"));
  pressed(control("simple-scan"), activeScan() === "SIMPLE");
  pressed(control("prio"), receiver().priority?.enabled);
  pressed(control("channel"), ui.channelDial);
  renderFavorites();
  const scanningPresets = zoneScanning();
  const selectedZones = scanZones();
  setText("scan-presets", presetSummary(selectedZones));
  setText("nearby-watch", nearbyWatchLabel());
  for (const key of zoneKeys) {
    const zone = key.dataset.zone;
    if (zone === "NEARBY") {
      pressed(key, receiver().nearby_enabled);
      key.title = receiver().nearby_enabled ? "Stop Nearby watch. Hold to view recent hits."
        : "Watch for strong nearby signals. Hold to view recent hits.";
      continue;
    }
    pressed(key, selectedZones.includes(zone));
    if (isCustomPreset(zone)) {
      const preset = receiver().custom_presets?.[zone];
      const label = preset?.name || zoneKeyNames[zone];
      if (key.textContent !== label) key.textContent = label;
      key.title = preset ? label + " · " + formatAxis(preset.start_hz) + "–" + formatAxis(preset.end_hz)
        + " MHz. Tap to add or remove this range from the preset scan. Hold to edit."
        : "Tap to set " + label + "’s name and frequency range.";
      continue;
    }
    key.title = `Tap to add or remove ${ZONE_NAMES[zone]} from the preset scan. Hold to edit the presets.`;
  }
  const nearbyReceiving = receiver().nearby_receiving;
  pressed(control("hold"), nearbyReceiving ? receiver().nearby_held : scanningPresets ? receiver().scan_held : ui.freeze);
  control("hold").title = nearbyReceiving ? "Hold or release the current Nearby signal."
    : scanningPresets ? "Pause or resume the current preset scan."
    : "Freeze or resume the spectrum and waterfall. Hold to clear the stored peak trace.";
  control("speed").title = scanningPresets ? "Set the preset scan speed."
    : "Cycle through slow, medium and fast spectrum updates.";
  pressed(control("center"), ui.fixed);
  pressed(control("peak"), ui.marker);
  pressed(control("expand"), ui.expanded);
  control("span").textContent = ui.fixed ? "EDGE" : "SPAN";
  control("span").title = ui.fixed ? "Set the lower and upper frequency edges of the fixed scope."
    : "Choose the width of the spectrum and waterfall.";
  control("scope-page").textContent = "<" + ui.page + ">";
  control("scope-page").setAttribute("aria-label", "Scope menu page " + ui.page + ". Switch to page " + (3 - ui.page));
  root.querySelectorAll("[data-page]").forEach(key => { key.hidden = Number(key.dataset.page) !== ui.page; });
  lcd.dataset.off = String(!state.power);
  lcd.dataset.scope = ui.visible ? "shown" : "hidden";
  setLcdExpanded(ui.expanded);
  lcd.dataset.displayType = String(getPreference("lcd_type", "A")).toLowerCase();
  lcdContent.style.opacity = clamp(Number(getPreference("lcd_backlight", 100)) / 100, 0, 1);
  const muted = Boolean(state.power && volumeMuted);
  if (view["local-mute"]) view["local-mute"].hidden = !muted || Boolean(state.muted);
  const volumeDial = knob("volume");
  if (volumeDial) {
    const dimOnMute = getPreference("mute_button_dim", true) === true;
    root.classList.toggle("volume-muted", Boolean(muted && dimOnMute));
    pressed(volumeDial, muted);
    volumeDial.style.setProperty("--angle", (audioVolume * 2.7 - 135) + "deg");
    const press = muted ? "unmute" : "mute";
    const display = dimOnMute ? (muted ? " and restore this LCD" : " and dim this LCD") : "";
    volumeDial.title = "Volume " + Math.round(audioVolume) + "%. Turn to adjust. Press to " + press + display + ".";
  }
  root.style.setProperty("--led-opacity", clamp(Number(getPreference("led_bright", 100)) / 100, 0, 1));
  if (!overlay && !menu?.isOpen()) ui.menuDialAngles = {};
  for (const name of dialNames) {
    const angle = ui.menuDialAngles[name] ?? assignedDialAngle(name) ?? ui.dialAngles[name] ?? 0;
    knob(name).style.setProperty("--angle", angle + "deg");
  }
  updateDialTooltips();
  const scope = updateScope();
  syncMirror(scope);
}

let mirrorHistoryKey;
function selectedMirrorSource() {
  try {
    const id = localStorage.getItem(mirrorSourceKey);
    if (id && sessionPattern.test(id)) return id;
  } catch {}
  return null;
}

function setupMirrorSource() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(mirrorIdentityKey));
    if (saved?.session === session && typeof saved.id === "string" && sessionPattern.test(saved.id)) mirrorId = saved.id;
  } catch {}
  if (mirrorId && selectedMirrorSource() === mirrorId) connectMirrorSource();
  window.addEventListener("storage", event => {
    if (event.key !== mirrorSourceKey || !mirrorId) return;
    if (selectedMirrorSource() === mirrorId) connectMirrorSource();
    else if (mirror) {
      mirror.close();
      mirror = undefined;
      mirrorHistoryKey = undefined;
    }
  });
  window.addEventListener("message", event => {
    if (mirrorWindow && event.source === mirrorWindow && event.origin === location.origin
        && event.data?.type === "sdr-lcd-ready") announceMirrorSource();
  });
}

function connectMirrorSource() {
  if (mirror) return;
  const channel = new BroadcastChannel("sdr-scanner-v10-lcd:" + mirrorId);
  mirror = channel;
  channel.addEventListener("message", ({data}) => {
    if (mirror !== channel || !["ready", "probe"].includes(data?.type)) return;
    const powered = Boolean(state.power);
    const fresh = performance.now() - lastStateAt < mirrorSnapshotMaxAge;
    if (data.type === "ready" && (fresh || !powered)) syncMirror(undefined, true);
    if (data.type === "probe") channel.postMessage({type: "alive", powered, fresh});
    // A visible mirror can request its source's state even when the source's tab timers are throttled.
    if (powered) void refreshState();
  });
}

function announceMirrorSource() {
  mirrorWindow.postMessage({type: "sdr-lcd-source", id: mirrorId}, location.origin);
}

function syncMirror(scope, force = false) {
  if (!mirror || isMirror) return;
  const active = overlay ? panels.find(panel => panel.dataset.panel === overlay) : menu?.isOpen() ? menusRoot : null;
  const menuSnapshot = active && !active.hidden && overlay !== "pin" ? {
    title: active.querySelector(".overlay-title")?.textContent || "",
    items: [...active.querySelectorAll("button:not([hidden]), .menu-status, .list p, label")].slice(0, 25)
      .map(node => node.textContent.trim()).filter(Boolean),
  } : null;
  const mirrorScope = scope || updateScope();
  const message = {
    type: "display", fields: Object.fromEntries(mirrorFields.map(key => [key, view[key].textContent])),
    frequency: frequency.textContent, off: lcd.dataset.off, expansion: lcd.dataset.expansion, scope: lcd.dataset.scope,
    brightness: lcdContent.style.opacity, typeStyle: lcd.dataset.displayType, meter: root.querySelector(".meter-body i").style.width,
    showRds: !view["rds-text"].hidden,
    menu: menuSnapshot,
  };
  const key = JSON.stringify([message, mirrorScope.frame?.sequence, mirrorScope.frame?.time_ms,
    mirrorScope.range, mirrorScope.options, mirrorScope.history.length]);
  if (force || key !== mirrorHistoryKey) {
    mirrorHistoryKey = key;
    mirror.postMessage({ ...message, display: mirrorScope });
  }
}

function openMirror() {
  if (!mirrorId) {
    mirrorId = randomId();
    try { sessionStorage.setItem(mirrorIdentityKey, JSON.stringify({session, id: mirrorId})); } catch {}
  }
  connectMirrorSource();
  try { localStorage.setItem(mirrorSourceKey, mirrorId); } catch {}
  if (!mirrorWindow || mirrorWindow.closed) mirrorWindow = window.open(mirrorPath, "sdr-scanner-v10-lcd");
  if (!mirrorWindow) throw new Error("Allow the LCD window to open from this site.");
  announceMirrorSource();
  mirrorWindow.focus();
  syncMirror(undefined, true);
}

function renderPublicDisplay(snapshot = null, offline = false) {
  if (!snapshot?.power || !state.power) resetScope();
  state = snapshot ? {...snapshot, v10: {...snapshot.v10, scan_direction: snapshot.scan_direction}} : {};
  const level = state.signal_meter_dbfs ?? state.signal_dbfs;
  const meter = state.power && finite(level) ? clamp((level + 92) / 66, 0, 1) : 0;
  const fields = {
    mode: state.power ? receiverModeLabel(state.modulation) : "",
    "top-state": state.power ? (receiver().frequency_mode === "memory" ? "MEMO" : "VFO") : "",
    clock: state.clock ?? clockFormat.format(new Date()),
    status: offline ? "RECEIVER OFFLINE" : state.power ? receiverStatus() : "STANDBY",
    level: state.power && finite(level) ? level.toFixed(1) + " dBFS" : "",
    filter: state.power ? receiver().filter ?? "" : "",
    flags: state.muted && state.power ? "MUTE" : "",
    "scan-presets": state.power ? presetSummary(scanZones()) : "",
    "nearby-watch": nearbyWatchLabel(),
    step: state.power && finite(receiver().tune_step) ? "TS " + stepLabel(receiver().tune_step) : "",
  };
  for (const key of mirrorFields) {
    if (!key.startsWith("axis-") && !key.startsWith("scope-")) setText(key, fields[key] ?? "");
  }
  frequency.textContent = state.power ? formatFrequency(state.tune_hz) : "";
  lcd.dataset.off = "false";
  lcd.dataset.scope = state.power ? "shown" : "hidden";
  setLcdExpanded(false);
  lcd.dataset.displayType = "a";
  lcdContent.style.opacity = "1";
  view.filter.hidden = !fields.filter;
  view["rds-text"].hidden = true;
  root.querySelector(".meter-body i").style.width = meter * 100 + "%";
  menusRoot.hidden = true;
  menusRoot.replaceChildren();
  if (state.power) ingestScope(state.waterfall);
  updateScope();
}

async function fetchPublicDisplay() {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch("/api/v10/display", {
      cache: "no-store", credentials: "omit", signal: controller.signal,
    });
    if (!response.ok) throw new Error("Display feed unavailable.");
    const snapshot = await response.json();
    if (typeof snapshot?.power !== "boolean") throw new Error("Invalid display feed.");
    return snapshot;
  } finally {
    clearTimeout(deadline);
  }
}

function startMirror() {
  root.classList.add("thin");
  lcdContent.inert = true;
  menusRoot.inert = true;
  renderPublicDisplay();
  let linkedAt = -Infinity;
  const linkIsCurrent = () => performance.now() - linkedAt < mirrorSnapshotMaxAge;
  function displayMessage({ data }) {
    if (data?.type === "alive") {
      if (!data.powered || data.fresh === false) linkedAt = -Infinity;
      else if (linkIsCurrent()) linkedAt = performance.now();
      return;
    }
    if (data?.type !== "display") return;
    if (data.off === "true") {
      linkedAt = -Infinity;
      return;
    }
    if (!linkIsCurrent()) {
      state = {};
      resetScope();
    }
    linkedAt = performance.now();
    for (const key of mirrorFields) if (typeof data.fields?.[key] === "string") setText(key, data.fields[key]);
    frequency.textContent = data.frequency;
    lcd.dataset.off = data.off;
    lcd.dataset.scope = data.scope;
    setLcdExpanded(data.expansion === "true");
    lcd.dataset.displayType = data.typeStyle;
    lcdContent.style.opacity = data.brightness;
    view.filter.hidden = !data.fields?.filter;
    view["rds-text"].hidden = data.showRds === false;
    root.querySelector(".meter-body i").style.width = data.meter;
    menusRoot.hidden = !data.menu;
    if (data.menu) {
      const title = document.createElement("h2");
      title.className = "overlay-title";
      title.textContent = data.menu.title;
      const list = document.createElement("div");
      list.className = "list";
      for (const item of data.menu.items) {
        const row = document.createElement("p");
        row.textContent = item;
        list.append(row);
      }
      menusRoot.replaceChildren(title, list);
    }
    const scope = data.display;
    drawScope(scope.frame, scope.history, scope.range, scope.options, scope.average, scope.maxTrace);
  }
  function followSource(id) {
    if (typeof id !== "string" || !sessionPattern.test(id) || mirrorId === id) return;
    linkedAt = -Infinity;
    mirror?.close();
    mirrorId = id;
    const channel = new BroadcastChannel("sdr-scanner-v10-lcd:" + id);
    mirror = channel;
    channel.addEventListener("message", event => {
      if (mirror === channel) displayMessage(event);
    });
    channel.postMessage({ type: "ready" });
  }
  const legacyId = location.hash.slice(1);
  if (location.hash) history.replaceState(history.state, "", mirrorPath);
  followSource(selectedMirrorSource() || (sessionPattern.test(legacyId) ? legacyId : null));
  window.addEventListener("storage", event => {
    if (event.key === mirrorSourceKey) followSource(selectedMirrorSource());
  });
  if (window.opener) {
    window.addEventListener("message", event => {
      if (event.source === window.opener && event.origin === location.origin && event.data?.type === "sdr-lcd-source") {
        followSource(selectedMirrorSource() || event.data.id);
      }
    });
    window.opener.postMessage({type: "sdr-lcd-ready"}, location.origin);
  }
  let displayTimer;
  let displayRequestPending = false;
  async function refreshDisplay() {
    if (displayRequestPending) return;
    clearTimeout(displayTimer);
    displayRequestPending = true;
    const started = performance.now();
    try {
      mirror?.postMessage({type: linkIsCurrent() ? "probe" : "ready"});
      if (!linkIsCurrent()) {
        const snapshot = await fetchPublicDisplay();
        if (!linkIsCurrent()) renderPublicDisplay(snapshot);
      }
    } catch {
      if (!linkIsCurrent()) renderPublicDisplay(null, true);
    } finally {
      displayRequestPending = false;
      const delay = document.hidden ? 1250 : Math.max(20, displayUpdateInterval - (performance.now() - started));
      displayTimer = setTimeout(refreshDisplay, delay);
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refreshDisplay();
  });
  void refreshDisplay();
}

function editNumber(field, key, allowed) {
  if (!field) return false;
  if (key === "CE") {
    field.value = "";
    return true;
  }
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? start;
  if (key === "⌫") {
    if (start === end && start > 0) field.setRangeText("", start - 1, end, "end");
    else field.setRangeText("", start, end, "end");
    return true;
  }
  const proposed = field.value.slice(0, start) + key + field.value.slice(end);
  if (allowed.test(proposed)) field.setRangeText(key, start, end, "end");
  return true;
}

function editOverlayNumber(key) {
  if (overlay === "frequency" || (overlay === "scope-value" && ui.scopeForm === "edge")) {
    return editNumber(overlayField, key, /^\d{0,4}(?:\.\d{0,6})?$/);
  }
  if (overlay === "scope-value" && ui.scopeForm === "ref") {
    return editNumber(overlayField, key, /^-?\d{0,2}(?:\.\d?)?$/);
  }
  if (overlay === "pin") return editNumber(overlayField, key, pinSetup?.needsCode ? /^[0-9a-fA-F]{0,32}$/ : /^\d{0,6}$/);
  return false;
}

async function commitFrequency(event) {
  event.preventDefault();
  const error = view["frequency-error"];
  try {
    await tune(parseFrequency(view["frequency-input"].value, receiverHz()));
    ui.stepOverride = null;
    closeOverlay();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  }
}

function scopeField(name, label, value, attributes = {}) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  wrap.textContent = label;
  const input = document.createElement("input");
  for (const [attribute, settingValue] of Object.entries(attributes)) input.setAttribute(attribute, settingValue);
  input.name = name;
  input.value = String(value);
  input.required = true;
  wrap.append(input);
  return wrap;
}

function showScopeForm(kind) {
  let fields;
  let label;
  if (kind === "span" && ui.fixed) {
    const [low, high] = scopeRange();
    label = "FIXED SCOPE EDGES";
    fields = [
      scopeField("low", "Lower edge · MHz", (low / 1e6).toFixed(6), { inputmode: "decimal" }),
      scopeField("high", "Upper edge · MHz", (high / 1e6).toFixed(6), { inputmode: "decimal" }),
    ];
    kind = "edge";
  } else if (kind === "span") {
    if (overlay) openPanel(null);
    menu.openChoices("SCOPE SPAN", scopeSpans().map(hz => [hz, scopeSpanLabel(hz)]), spanHz(), value => {
      ui.span = value;
      render();
    });
    return;
  } else if (kind === "ref") {
    label = "REFERENCE LEVEL";
    fields = [scopeField("ref", "Level · dB", getPreference("scope_reference", 0), { inputmode: "decimal" })];
  } else {
    return openMenu("tune-step");
  }
  ui.scopeForm = kind;
  setText("scope-title", label);
  view["scope-values"].replaceChildren(...fields, numericKeys("Scope keypad", kind === "ref" ? [".", "-"] : ["."]));
  view["scope-error"].hidden = true;
  openPanel("scope-value");
  overlayField?.select();
}

function commitScope(event) {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  try {
    if (ui.scopeForm === "edge") {
      const low = parseFrequency(values.get("low"));
      const high = parseFrequency(values.get("high"));
      const maxSpan = captureSpan();
      if (!(low >= minimumHz && high <= maximumHz && low < high && high - low <= maxSpan)) {
        throw new Error("Select edges within the SDR range and its " + scopeSpanLabel(maxSpan) + " capture span.");
      }
      ui.edges = [low, high];
    } else {
      const ref = Number(values.get("ref"));
      if (!Number.isFinite(ref) || ref < -20 || ref > 20 || ref * 2 % 1) {
        throw new Error("Set the level from −20 to +20 dB in 0.5 dB steps.");
      }
      setPreference("scope_reference", ref);
    }
    closeOverlay();
    render();
  } catch (problem) {
    setText("scope-error", problem.message);
    view["scope-error"].hidden = false;
  }
}

function changeScopeSpeed() {
  const old = getPreference("scope_sweep_speed", "mid");
  const next = speeds[(speeds.findIndex(speed => speed.toLowerCase() === old) + 1) % speeds.length];
  setPreference("scope_sweep_speed", next.toLowerCase());
  control("speed").title = next + " sweep. Click to change the spectrum update speed.";
  report("SPEED " + next.toUpperCase());
}

function canOperate(name) {
  if (name === "power" || name === "exit") return true;
  if (overlay === "pin" && name === "dial-c") return true;
  if (!state.power && canSetPin()) {
    if (name === "menu") return true;
    if ((menu?.isOpen() || pinSetup) && ["dial-c", "dial-d", "dial-e", "frequency"].includes(name)) return true;
  }
  return Boolean(state.power && !access.pin_required);
}

function activeScan() {
  return receiver().scan_kind || (state.scan_active || state.search_active ? "MEMO" : null);
}

const zoneScanning = () => activeScan() === "ZONE";

function scopeTuneAction(hz) {
  return zoneScanning() ? {v10: "scan_control", operation: "inspect", hz}
    : {v10: "tune", hz, mode: vfoMode()};
}

function scanZones() {
  const radio = receiver();
  return zoneScanning() ? radio.scan_zones ?? [radio.scan_zone].filter(Boolean) : [];
}

function presetSummary(zones) {
  return zones.map(zone => customPresetName(zone) || zoneKeyNames[zone]).filter(Boolean).join(" · ");
}

async function togglePresetScan(zone) {
  await act(() => {
    const selected = scanZones();
    const zones = selected.includes(zone) ? selected.filter(item => item !== zone) : [...selected, zone];
    return zones.length ? {v10: "scan", kind: "ZONE", zones, enabled: true}
      : {v10: "scan", kind: "ZONE", enabled: false};
  });
  closeOverlay();
}

async function scan(hold) {
  const active = activeScan();
  if (active) {
    await act({ v10: "scan", kind: active, enabled: false });
    closeOverlay();
    return;
  }
  if (receiver().trunk?.enabled) return act({ v10: "trunk", operation: "stop" });
  const previous = getPreference("last_scan", null);
  if (hold && previous?.enabled === true && previous.v10 === "scan") {
    await act(previous);
    if (settings().scan_screen !== false) openMenu("scan");
    return;
  }
  openMenu("scan-start");
}

async function simpleScan(direction) {
  const enabled = activeScan() === "SIMPLE";
  if (enabled && direction) return turnScanDirection(direction);
  await act({v10: "scan", kind: "SIMPLE", enabled: !enabled,
    ...(enabled ? {} : {direction: direction || 1, step: tuningStep()})});
  closeOverlay();
}

async function priority(hold) {
  const current = receiver().priority || {};
  if (hold && current.enabled) {
    await act({ v10: "priority", enabled: false });
    return act({ v10: "memory", operation: "recall", group: current.group, slot: current.slot });
  }
  if (!current.enabled && !currentMemory()) return openMenu("prio");
  return act({ v10: "priority", enabled: !current.enabled, group: current.group, slot: current.slot });
}

async function groupChange(direction) {
  const group = receiver().group || 0;
  const memories = receiver().memories || [];
  const groups = [...new Set(memories.map(entry => entry.group))].sort((left, right) => left - right);
  if (!groups.length) return openMenu("group");
  const recent = direction > 0 ? groups.find(value => value > group) : groups.findLast(value => value < group);
  const next = recent ?? (direction > 0 ? groups[0] : groups.at(-1));
  const target = memories.find(entry => entry.group === next);
  const inVfo = receiver().frequency_mode === "vfo";
  return inVfo ? openMenu("group") : act({ v10: "memory", operation: "recall", group: next, slot: target.slot });
}

async function writeMemory(hold) {
  if (!hold || !finite(state.tune_hz) || receiver().scan_paused) return openMenu("write");
  const group = receiver().group ?? 0;
  const existing = receiver().memories || [];
  const slot = Array.from({ length: 100 }, (_, index) => index)
    .find(index => !existing.some(entry => entry.group === group && entry.slot === index));
  if (slot === undefined) return openMenu("write");
  await storeTunedMemory(group, slot);
  report("MEMORY WRITTEN");
}

function storeTunedMemory(group, slot, name = "") {
  const mode = state.modulation;
  const memory = { v10: "memory", operation: "write", group, slot, hz: state.tune_hz, mode, name };
  if (mode === "DMR") {
    const saved = receiver().frequency_mode === "memory" ? currentMemory() : null;
    const filters = saved?.mode === "DMR" && saved.hz === state.tune_hz ? saved : null;
    if (filters) {
      memory.color_code = filters.color_code ?? null;
      memory.timeslot = filters.timeslot ?? null;
      memory.talkgroup = filters.talkgroup ?? null;
    }
  } else if (state.tone_filter) memory.tone = state.tone_filter;
  return act(memory);
}

async function useFavorite(hold, key) {
  const bank = Number(key.dataset.bank);
  const slot = Number(key.dataset.slot);
  const entry = favoriteMemory(bank, slot);
  if (!hold) {
    if (entry) await act({v10: "memory", operation: "recall", group: bank, slot});
    else report("CH" + slot + " EMPTY");
    return;
  }
  if (!finite(state.tune_hz)) return;
  const previous = entry?.hz === state.tune_hz && entry.mode === state.modulation ? entry : currentMemory();
  const name = previous?.hz === state.tune_hz && previous.mode === state.modulation ? previous.name : "";
  await storeTunedMemory(bank, slot, name || "");
  report("BANK " + bank + " CH" + slot + " SAVED");
}

function rightClickFavorite(key) {
  const group = Number(key.dataset.bank);
  const slot = Number(key.dataset.slot);
  const identity = "BANK " + group + " CH" + slot;
  if (overlay === "favorite-clear" && favoriteToClear?.group === group && favoriteToClear.slot === slot) {
    const expected = favoriteToClear;
    closeOverlay();
    void act({v10: "memory", operation: "delete", group, slot, expected})
      .then(() => report(identity + " CLEARED")).catch(showError);
    return true;
  }
  const entry = favoriteMemory(group, slot);
  if (!entry) return false;
  favoriteToClear = structuredClone(entry);
  setText("favorite-clear-detail", [identity, entry.name, formatAxis(entry.hz) + " MHz", entry.mode].filter(Boolean).join(" · "));
  setText("favorite-clear-instruction", "Right-click " + identity + " again to clear.");
  openPanel("favorite-clear");
  return true;
}

async function lockout(hold) {
  const scanning = Boolean(activeScan());
  await act({v10: "lockout", operation: hold ? "clear-temporary" : "toggle"});
  report(hold ? "TEMPORARY LOCKOUTS CLEARED" : scanning ? "FREQUENCY SKIPPED"
    : receiver().current_lockout ? "LOCKED OUT" : "LOCKOUT CLEARED");
}

function speech(hold) {
  const swapped = getPreference("speech_lock_switch", "speech") === "lock";
  if (hold !== swapped) {
    if (!overlay && !menu.isOpen()) ui.dialLock = !ui.dialLock;
    return;
  }
  if (volumeMuted || !window.speechSynthesis || !finite(state.tune_hz)) return;
  const fragments = [(state.tune_hz / 1e6).toFixed(6), "megahertz", state.modulation];
  if (getPreference("speech_signal", true) && finite(state.signal_dbfs)) {
    fragments.push(Math.round(state.signal_dbfs) + " D B F S");
  }
  const utterance = new SpeechSynthesisUtterance(fragments.join(" "));
  utterance.lang = getPreference("speech_language", "en-GB");
  utterance.rate = getPreference("speech_speed", "fast") === "fast" ? 1.05 : 0.8;
  utterance.volume = clamp(Number(getPreference("speech_level", 50)) / 100, 0, 1);
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
}

function feedback(hold) {
  if (!audioContext || audioContext.state !== "running" || playback || !getPreference("beep_confirmation", true)) return;
  const level = clamp(Number(getPreference("beep_level", 50)) / 100, 0, 1) * audioLevel() * 0.08;
  if (!level) return;
  const gain = audioContext.createGain();
  const sound = audioContext.createOscillator();
  const now = audioContext.currentTime;
  const duration = hold ? 0.16 : 0.07;
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(level, now + 0.008);
  gain.gain.setValueAtTime(level, now + duration * 0.55);
  gain.gain.linearRampToValueAtTime(0, now + duration);
  gain.connect(audioContext.destination);
  sound.connect(gain);
  sound.frequency.value = hold ? 1040 : 830;
  sound.start(now);
  sound.stop(now + duration);
  sound.addEventListener("ended", () => gain.disconnect(), { once: true });
}

function snapshotScreen() {
  if (!getPreference("screen_capture", false)) return;
  const result = document.createElement("canvas");
  result.width = 840;
  result.height = 440;
  const capture = result.getContext("2d");
  capture.fillStyle = "#071017";
  capture.fillRect(0, 0, result.width, result.height);
  capture.fillStyle = "#eff7fb";
  capture.font = "600 25px monospace";
  capture.fillText([view.mode.textContent, view.site.textContent, view.clock.textContent].filter(Boolean).join(" · "), 25, 37);
  capture.font = "600 60px monospace";
  capture.fillText(frequency.textContent, 25, 105);
  capture.font = "18px monospace";
  capture.fillText([view["top-state"].textContent, view.status.textContent, view.level.textContent,
    view.step.textContent].join(" · "), 25, 137);
  const scopeX = 25, scopeY = 154, scopeWidth = 790, scopeHeight = 235;
  capture.drawImage(canvas, scopeX, scopeY, scopeWidth, scopeHeight);
  const squelch = view["scope-squelch"];
  if (!squelch.hidden) {
    const position = Number.parseFloat(squelch.style.getPropertyValue("--scope-squelch-y")) / 100;
    const labelY = scopeY + Math.max(2, scopeHeight * position - 20);
    capture.save();
    capture.font = "600 12px monospace";
    capture.textBaseline = "top";
    const labelWidth = capture.measureText(squelch.textContent).width;
    const labelX = scopeX + scopeWidth - labelWidth - 10;
    capture.fillStyle = "#08131c";
    capture.fillRect(labelX - 5, labelY, labelWidth + 10, 17);
    capture.fillStyle = "#ffc985";
    capture.fillText(squelch.textContent, labelX, labelY + 2);
    capture.restore();
  }
  capture.fillText([view["axis-left"], view["axis-center"], view["axis-right"]].map(node => node.textContent).join(" · "), 25, 418);
  const link = document.createElement("a");
  link.download = "SDr-Scanner-" + new Date().toISOString().replace(/[:.]/g, "-") + ".png";
  link.href = result.toDataURL("image/png");
  link.click();
}

function setMonitor(enabled) {
  return act({ v10: "monitor", enabled });
}

function moveMenuCursor(delta) {
  if (overlay) {
    moveOverlay(delta);
    return true;
  }
  return menu.handleDial(delta, "navigation");
}

function selectMenuItem() {
  if (overlay) {
    selectOverlay();
    return true;
  }
  return menu.select();
}

function openDialAssignment(name) {
  if (!state.power || access.pin_required) return;
  if (overlay) openPanel(null);
  menu.openDialAssignment(name);
}

const commands = {
  power: hold => state.power ? (hold ? powerOff() : snapshotScreen()) : powerOn(),
  volume: () => {
    const mute = !volumeMuted;
    if (mute) window.speechSynthesis?.cancel();
    setPreference("volume_muted", mute);
  },
  mode: () => overlay === "mode" ? closeOverlay() : openPanel("mode"),
  record: () => act({record: "toggle"}),
  "dial-a": hold => hold ? openDialAssignment("dial-a") : openMenu("dial-a"),
  "dial-b": hold => hold ? openDialAssignment("dial-b") : openMenu("dial-b"),
  "dial-c": hold => { if (hold) openDialAssignment("dial-c"); else if (!selectMenuItem()) openMenu("dial-c"); },
  "dial-d": hold => { if (hold) openDialAssignment("dial-d"); else if (!selectMenuItem()) openMenu("dial-d"); },
  "dial-e": hold => { if (hold) openDialAssignment("dial-e"); else if (!selectMenuItem()) openMenu("dial-e"); },
  "group-up": () => groupChange(1),
  "group-down": () => groupChange(-1),
  vm: async hold => {
    const inMemory = receiver().frequency_mode === "memory";
    if (hold && inMemory && finite(state.tune_hz)) return tune(state.tune_hz);
    return act({ v10: "frequency_mode", value: inMemory ? "vfo" : "memory" });
  },
  "vfo-memory": () => openMenu("vfo-memory"),
  memory: writeMemory,
  favorite: useFavorite,
  zone: (hold, key) => {
    const zone = key.dataset.zone;
    if (!hold && zone === "NEARBY") {
      return act(() => ({v10: "nearby", enabled: !receiver().nearby_enabled})).then(closeOverlay);
    }
    if (!hold && (!isCustomPreset(zone) || receiver().custom_presets?.[zone])) return togglePresetScan(zone);
    if (overlay) openPanel(null);
    return menu.openZone(zone);
  },
  lockout,
  channel: () => { ui.channelDial = !ui.channelDial; },
  scan,
  "simple-scan": hold => simpleScan(hold ? (activeScan() === "SIMPLE" ? -receiver().scan_direction : -1) : 0),
  prio: priority,
  menu: () => openMenu("menu"),
  function: () => openMenu("function"),
  scope: () => {
    closeOverlay();
    ui.visible = !ui.visible;
  },
  quick: () => openMenu("quick"),
  exit: () => {
    if (menu.isOpen()) {
      if (typeof menu.back === "function") menu.back();
      else menu.close();
    } else if (pinSetup) openMenu("set");
    else if (overlay) closeOverlay();
    else if (ui.expanded) ui.expanded = false;
  },
  speech,
  frequency: (hold, controlNode, event) => {
    if (controlNode.matches(".knob")) {
      if (hold) return openDialAssignment("frequency");
      if (overlay) return selectOverlay();
      if (menu.isOpen()) return menu.select();
    }
    if (ui.dialLock) return;
    if (hold && !controlNode.matches(".knob")) {
      ui.stepOverride = 1000000;
      return;
    }
    if (controlNode === frequency && event.detail && event.pointerType !== "touch") {
      clearTimeout(frequencyEntryTimer);
      if (event.detail === 1) {
        frequencyEntryTimer = setTimeout(() => {
          if (canOperate("frequency") && !ui.dialLock && !overlay && !menu.isOpen()) openPanel("frequency");
        }, frequencyDoubleClickDelay);
      }
      return;
    }
    openPanel("frequency");
  },
  filter: () => openMenu("filter"),
  clock: () => {
    const clock = receiver().clock;
    const local = clock?.local ?? clockFormat.format(new Date());
    const utc = clock?.utc ?? new Date().toISOString().slice(11, 16);
    view.message.textContent = local + " " + (clock?.zone || "LOCAL") + " · " + utc + " UTC";
    openPanel("message");
  },
  "tune-step": () => showScopeForm("step"),
  "scope-page": () => { ui.page = 3 - ui.page; },
  span: () => showScopeForm("span"),
  hold: long => {
    if (receiver().nearby_receiving || zoneScanning()) return act(() => {
      const held = receiver().nearby_receiving ? receiver().nearby_held : receiver().scan_held;
      return {v10: "scan_control", operation: held ? "resume" : "hold"};
    });
    if (long) {
      ui.peakTrace = null;
      ui.peakStart = 0;
      ui.freeze = false;
      previousScopeDraw = null;
    } else ui.freeze = !ui.freeze;
    ui.heldCenter = ui.freeze && ui.frame ? receiverHz() : null;
  },
  center: () => {
    ui.edges = !ui.fixed ? scopeRange() : null;
    ui.fixed = !ui.fixed;
  },
  ref: () => showScopeForm("ref"),
  speed: () => zoneScanning() ? openMenu("scan-speed") : changeScopeSpeed(),
  peak: () => { ui.marker = !ui.marker; },
  expand: hold => {
    if (hold) openMenu("scope-set");
    else ui.expanded = !ui.expanded;
  },
};

function invoke(node, held, event) {
  const action = node.dataset.action;
  if (!canOperate(action) || !commands[action]) return;
  if (node !== frequency || held) clearTimeout(frequencyEntryTimer);
  if (action !== "power" && action !== "volume") feedback(held);
  Promise.resolve().then(() => commands[action](held, node, event)).then(render).catch(showError);
}

const dialQueue = new Map();
const dialRemainders = new Map();

function scaleDialTurn(name, delta) {
  const factor = menu.dialSensitivity(name);
  const previous = dialRemainders.get(name);
  const scaled = delta * factor + (previous?.factor === factor ? previous.remainder : 0);
  const whole = Math.trunc(scaled);
  dialRemainders.set(name, {factor, remainder: scaled - whole});
  return whole;
}
function adjustDial(name, delta) {
  if (!canOperate(name)) return;
  if (name === "volume") {
    setPreference("volume", clamp(audioVolume + delta * AF_VOLUME_STEP, 0, 100));
    return;
  }
  const navigating = overlay || menu.isOpen();
  if (name === "frequency" && ui.dialLock && !navigating) return;
  if (navigating || assignedDialAngle(name) === undefined) {
    const angles = navigating ? ui.menuDialAngles : ui.dialAngles;
    const dial = knob(name);
    const angle = Number.parseFloat(dial.style.getPropertyValue("--angle")) || 0;
    angles[name] = (angle + 12 * delta) % 360;
    dial.style.setProperty("--angle", angles[name] + "deg");
  }
  dialQueue.set(name, (dialQueue.get(name) || 0) + delta);
  void drainDials();
}

let draining;
let lastMainTurn = -Infinity;

function stepThrough(values, current, delta) {
  const options = [...new Set([...values, current])].sort((left, right) => left - right);
  return options[clamp(options.indexOf(current) + delta, 0, options.length - 1)];
}

function adjustScopeSpan(delta) {
  const [low, high] = scopeRange();
  const width = high - low;
  const next = stepThrough(scopeSpans(), width, delta);
  if (ui.fixed) {
    const left = clamp(Math.round((low + high - next) / 2), minimumHz, maximumHz - next);
    ui.edges = [left, left + next];
  } else if (next !== width) ui.span = next;
}

function turnScanDirection(delta) {
  if (activeScan() && !receiver().trunk?.enabled) return act({v10: "step", direction: Math.sign(delta)});
}

function tuneByOffset(offset) {
  const {min_hz = minimumHz, max_hz = maximumHz} = receiver().capabilities ?? {};
  return tune(clamp(receiverHz() + offset, min_hz, max_hz));
}

async function turnMemoryChannels(delta) {
  const memories = receiver().memories || [];
  const group = receiver().group ?? 0;
  const local = memories.filter(item => item.group === group);
  const channels = local.length ? local : memories;
  if (!channels.length) return;
  const index = channels.findIndex(item => item.group === group && item.slot === receiver().slot);
  const start = index === -1 && delta < 0 ? 0 : index;
  const entry = channels[(start + delta % channels.length + channels.length) % channels.length];
  await act({v10: "memory", operation: "recall", group: entry.group, slot: entry.slot});
}

function turnMainTuning(delta) {
  if (activeScan() && !receiver().trunk?.enabled) return turnScanDirection(delta);
  const now = performance.now();
  const fast = now - lastMainTurn < 150;
  const auto = getPreference("dial_auto_ts", "high");
  const step = tuningStep();
  const accelerate = fast && auto !== "off" && ui.stepOverride == null && step !== 10;
  const multiplier = accelerate ? (auto === "high" && step <= 1000 ? 5 : 2) : 1;
  lastMainTurn = now;
  return tuneByOffset(delta * step * multiplier);
}

const dialActions = {
  tuning: turnMainTuning,
  "fine-tuning": delta => tuneByOffset(delta * 10),
  "coarse-tuning": delta => tuneByOffset(delta * 10000),
  "tuning-step": delta => {
    const step = stepThrough([10, ...TUNING_STEPS], tuningStep(), delta);
    if (step !== tuningStep()) return setting("tune_step", step);
  },
  "scan-direction": turnScanDirection,
  "memory-channel": turnMemoryChannels,
  "scope-span": adjustScopeSpan,
};

async function drainDials() {
  if (draining) return;
  draining = true;
  try {
    while (dialQueue.size) {
      const [name, accumulated] = dialQueue.entries().next().value;
      dialQueue.delete(name);
      const delta = clamp(accumulated, -20, 20);
      if (!delta || !canOperate(name)) continue;
      if (menu.isOpen() && await menu.handleDial(delta, name)) continue;
      if (overlay) {
        if (name !== "dial-c" && overlay === "scope-value" && ui.scopeForm === "ref" && overlayCursor === overlayField) {
          overlayField.value = String(clamp((Number(overlayField.value) || 0) + delta, -20, 20));
        } else {
          moveOverlay(delta);
        }
        continue;
      }
      if (name === "frequency" && (ui.channelDial || activeScan() === "SIMPLE")) {
        if (activeScan() && !receiver().trunk?.enabled) await turnScanDirection(delta);
        else {
          const turns = scaleDialTurn(name, delta);
          if (turns) await turnMemoryChannels(turns);
        }
        continue;
      }
      const turns = scaleDialTurn(name, delta);
      if (turns) await menu.adjustAssignedDial(name, turns);
    }
  } catch (error) {
    showError(error);
  } finally {
    draining = false;
    render();
  }
}

function numericKeys(label, extra = [], grid = document.createElement("div")) {
  grid.className = "tile-grid frequency-keys";
  grid.setAttribute("aria-label", label);
  for (const value of ["1", "2", "3", "⌫", "4", "5", "6", "CE", "7", "8", "9", "0", ...extra]) {
    const key = document.createElement("button");
    key.type = "button";
    key.className = "key";
    key.textContent = value;
    key.title = {"⌫": "Delete the previous character.", CE: "Clear the current entry.",
      ".": "Enter the decimal point.", "-": "Enter a minus sign."}[value] || "Enter " + value + ".";
    if (label === "PIN keypad" && /^[A-F]$/.test(value)) {
      key.dataset.pinCodeKey = "";
      key.hidden = true;
    }
    key.addEventListener("pointerdown", event => event.preventDefault());
    if (value === "⌫") key.setAttribute("aria-label", "Backspace");
    if (value === "CE") key.setAttribute("aria-label", "Clear entry");
    key.addEventListener("click", () => {
      setOverlayCursor(key);
      editOverlayNumber(value);
    });
    grid.append(key);
  }
  return grid;
}

function configureTuningWheel(element, stepAtPointer) {
  let gesture;
  element.addEventListener("pointerleave", () => { gesture = null; });
  element.addEventListener("wheel", event => {
    const step = stepAtPointer(event);
    const units = event.deltaMode;
    const delta = event.deltaY;
    if (!step || !delta || event.ctrlKey || event.metaKey) return;
    if (!canOperate("frequency") || ui.dialLock || overlay || menu.isOpen()) return;
    event.preventDefault();

    const direction = delta < 0 ? 1 : -1;
    const elapsed = gesture ? event.timeStamp - gesture.lastAt : Infinity;
    if (!gesture || gesture.step !== step || gesture.direction !== direction || gesture.units !== units || elapsed >= 110) {
      gesture = {step, direction, units, distance: 0, applied: false};
    }
    gesture.lastAt = event.timeStamp;
    gesture.distance += Math.abs(delta);
    // Smooth wheels can emit several events for one detent.
    const detent = units !== 0 || Math.abs(delta) >= 80;
    if (gesture.applied ? !detent || elapsed < 32 : !detent && gesture.distance < 48) return;
    gesture.applied = true;

    void act(() => {
      if (!canOperate("frequency") || ui.dialLock || !finite(state.tune_hz)) return;
      const {min_hz = minimumHz, max_hz = maximumHz} = receiver().capabilities ?? {};
      const hz = clamp(state.tune_hz + step * direction, min_hz, max_hz);
      if (hz !== state.tune_hz) return element === canvas ? scopeTuneAction(hz) : {v10: "tune", hz, mode: vfoMode()};
    }).catch(showError);
  }, {passive: false});
}

const longControls = new Set(["power", "dial-a", "dial-b", "dial-c", "dial-d", "dial-e", "scan", "simple-scan", "prio", "lockout", "zone",
  "speech", "vm", "memory", "favorite", "frequency", "hold", "expand"]);
const gestures = new WeakMap();

function finishGesture(node, data) {
  clearTimeout(data.timer);
  if (data.held || data.moved) data.suppressClick = true;
  data.timer = null;
}

function configureControl(node) {
  const name = node.dataset.action;
  if (!commands[name]) return;
  if (longControls.has(name)) node.addEventListener("contextmenu", event => {
    if (event.pointerType && event.pointerType !== "mouse") event.preventDefault();
  });
  const isKnob = node.classList.contains("knob");
  if (longControls.has(name) || isKnob) {
    node.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      if (audioContext && audioContext.state === "suspended") void prepareAudio();
      const data = {
        pointerId: event.pointerId, initialX: event.clientX, initialY: event.clientY,
        steps: 0, moved: false, held: false, suppressClick: false,
      };
      gestures.set(node, data);
      if (isKnob) node.setPointerCapture(event.pointerId);
      if (longControls.has(name) && canOperate(name)) {
        data.timer = setTimeout(() => {
          data.held = true;
          invoke(node, true, event);
        }, 1000);
      }
    });
    node.addEventListener("pointermove", event => {
      const data = gestures.get(node);
      if (!data || data.pointerId !== event.pointerId || data.held) return;
      const dx = event.clientX - data.initialX;
      const dy = event.clientY - data.initialY;
      if (Math.abs(dx) + Math.abs(dy) > 7) {
        clearTimeout(data.timer);
        data.moved = true;
      }
      if (!isKnob) return;
      const steps = Math.trunc((Math.abs(dx) >= Math.abs(dy) ? dx : -dy) / 9);
      if (steps !== data.steps) {
        adjustDial(name, steps - data.steps);
        data.steps = steps;
      }
    });
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
      node.addEventListener(type, event => {
        const data = gestures.get(node);
        if (data?.pointerId === event.pointerId && data.pointerId !== null) {
          data.pointerId = null;
          finishGesture(node, data);
        }
      });
    }
    node.addEventListener("pointerleave", () => {
      const data = gestures.get(node);
      if (data && !isKnob && data.pointerId !== null) {
        data.pointerId = null;
        finishGesture(node, data);
      }
    });
    node.addEventListener("keydown", event => {
      if (![" ", "Enter"].includes(event.key) || event.repeat || !longControls.has(name) || !canOperate(name)) return;
      const data = gestures.get(node) || {};
      data.held = false;
      data.suppressClick = false;
      data.timer = setTimeout(() => {
        data.held = true;
        invoke(node, true, event);
      }, 1000);
      gestures.set(node, data);
    });
    for (const kind of ["keyup", "blur"]) node.addEventListener(kind, event => {
      if (kind === "keyup" && ![" ", "Enter"].includes(event.key)) return;
      const data = gestures.get(node);
      if (data?.timer) finishGesture(node, data);
    });
  }
  node.addEventListener("click", event => {
    const data = gestures.get(node);
    if (data?.suppressClick) {
      gestures.delete(node);
      return;
    }
    if (data?.held) return;
    if (audioContext && audioContext.state === "suspended") void prepareAudio();
    invoke(node, false, event);
  });
  if (name === "simple-scan") node.addEventListener("keydown", event => {
    if (!canOperate(name) || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    void simpleScan(event.key === "ArrowUp" ? 1 : -1).catch(showError);
  });
  if (isKnob) {
    node.addEventListener("wheel", event => {
      const movement = event.deltaY || event.deltaX;
      if (!movement) return;
      event.preventDefault();
      adjustDial(name, Math.sign(movement));
    }, { passive: false });
    node.addEventListener("keydown", event => {
      const vertical = name !== "volume" && (overlay || menu.isOpen()) ? -1 : 1;
      const delta = { ArrowUp: vertical, ArrowRight: 1, ArrowDown: -vertical, ArrowLeft: -1 }[event.key];
      if (delta) {
        event.preventDefault();
        adjustDial(name, delta);
      }
    });
  }
}

function handlePanelKeys(event) {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
  const key = event.key;
  const target = event.target;
  if (key === "Enter" && target.closest?.("button.hardware, .knob")) return;
  const menuOpen = menu.isOpen();
  if (menuOpen && menu.handleKey?.(key)) {
    event.preventDefault();
    return;
  }
  if (key === "Escape") {
    event.preventDefault();
    invoke(control("exit"), false, event);
    return;
  }
  if ((key === "ArrowUp" || key === "ArrowDown") && (overlay || menuOpen)) {
    event.preventDefault();
    moveMenuCursor(key === "ArrowDown" ? 1 : -1);
    return;
  }

  const nativeField = target instanceof HTMLElement && target.matches("input, textarea, [contenteditable=true]");
  if (overlay && !nativeField && (/^[0-9.\-]$/.test(key) || key === "Backspace"
      || pinSetup?.needsCode && /^[a-f]$/i.test(key))) {
    if (editOverlayNumber(key === "Backspace" ? "⌫" : key)) {
      event.preventDefault();
      setOverlayCursor(overlayField, true);
    }
    return;
  }
  if (key !== "Enter") return;

  const active = overlay ? activePanel() : menuOpen ? menusRoot : null;
  if (active) {
    if (active.contains(target) && target.matches?.("textarea, [contenteditable=true]")) return;
    event.preventDefault();
    if (active.contains(target) && nativeField && target.form) {
      target.form.requestSubmit();
      return;
    }
    const keyControl = target.closest?.("button, a[href]");
    if (keyControl && active.contains(keyControl)) {
      keyControl.click();
      return;
    }
    if (overlay) selectOverlay();
    else if (!menu.submit?.()) menu.select();
  } else if (access.pin_required && !target.closest?.("button")) {
    event.preventDefault();
    invoke(control("power"), false, event);
  }
}

function configurePanel() {
  restorePanelSettings();
  const savedVolume = getPreference("volume", lastVolume() * 100);
  audioVolume = finite(savedVolume) ? Math.round(clamp(savedVolume, 0, 100) * 2) / 2 : 32;
  volumeMuted = getPreference("volume_muted", false) === true;
  session = createSession();
  resumeSession = savedListeningSession();
  setupMirrorSource();
  menu = createReceiverMenus({
    root: menusRoot,
    getState: () => state,
    getAccess: () => access,
    onPinSetup: openPinSetup,
    onDialAction: (action, delta, dial) => dialActions[action](delta, dial),
    act,
    request,
    getPreference: (key, fallback) => key === "volume" ? audioVolume : getPreference(key, fallback),
    setPreference,
    getTuningStep: tuningStep,
    onOpen: () => { lcdContent.inert = true; syncMirror(); },
    onRender: () => { updateDialTooltips(); syncMirror(); },
    onClose: () => { lcdContent.inert = Boolean(overlay); syncMirror(); },
    onMirror: openMirror,
    onTune: tune,
    onScope: () => { closeOverlay(); ui.visible = true; ui.expanded = true; render(); },
    onMode: () => openPanel("mode"),
    onPlay: play,
    onReplay: () => play("/api/discovery/replay"),
    onStopPlayback: stopPlayback,
  });
  numericKeys("Frequency keypad", ["."], view["frequency-keys"]);
  view["pin-error"].before(numericKeys("PIN keypad", ["A", "B", "C", "D", "E", "F"]));
  for (const panel of panels) {
    panel.style.scrollbarWidth = "none";
    panel.querySelector("[data-panel-back]")?.addEventListener("click", event => invoke(control("exit"), false, event));
    panel.addEventListener("focusin", event => {
      if (panel === activePanel() && panelItems().includes(event.target)) setOverlayCursor(event.target);
    });
  }
  for (const node of root.querySelectorAll("[data-action]")) configureControl(node);
  root.addEventListener("contextmenu", event => {
    const key = event.target.closest("button");
    if (!key || event.pointerType === "touch" || event.sourceCapabilities?.firesTouchEvents) return;
    event.preventDefault();
    if (event.button === 2 && key.dataset.action === "favorite" && canOperate("favorite") && rightClickFavorite(key)) return;
    invoke(control("exit"), false, event);
  });
  panels.find(panel => panel.dataset.panel === "frequency").addEventListener("submit", commitFrequency);
  panels.find(panel => panel.dataset.panel === "scope-value").addEventListener("submit", commitScope);
  panels.find(panel => panel.dataset.panel === "pin").addEventListener("submit", unlock);
  view.modes.addEventListener("click", event => {
    const key = event.target.closest("[data-mode]");
    if (!key || key.disabled || !canOperate("mode")) return;
    feedback(false);
    void tune(receiverHz(), key.dataset.mode).then(() => {
      closeOverlay();
      render();
    }).catch(showError);
  });
  canvas.addEventListener("pointermove", showScopeTip);
  canvas.addEventListener("pointerleave", () => {
    scopePointer = null;
    refreshScopeTip();
  });
  canvas.addEventListener("click", event => {
    if (!ui.frame || !canOperate("frequency") || ui.dialLock) return;
    const hz = scopeSelectionAt(event);
    void act(() => scopeTuneAction(hz)).catch(showError);
  });
  configureTuningWheel(frequency, event => Number(event.target.closest("[data-step-hz]")?.dataset.stepHz));
  configureTuningWheel(canvas, scopeWheelStep);
  frequency.addEventListener("dblclick", event => {
    clearTimeout(frequencyEntryTimer);
    if (!finite(state.tune_hz)) return;
    event.preventDefault();
    const text = (state.tune_hz / 1e6).toFixed(6) + " MHz";
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).then(() => report("FREQUENCY COPIED")).catch(showError);
    } else {
      const field = document.createElement("textarea");
      field.value = text;
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      const copied = document.execCommand("copy");
      field.remove();
      if (copied) report("FREQUENCY COPIED");
    }
  });
  document.addEventListener("keydown", handlePanelKeys);
  document.addEventListener("pointerdown", () => {
    if (state.power || audioContext) void prepareAudio().catch(() => {});
  });
  window.addEventListener("blur", () => { if (receiver().monitor) void setMonitor(false).catch(() => {}); });
  request("/api/access").then(result => { access = result; }).catch(() => {}).then(refresh);
  // Restored tabs can already own an active receiver. Audio becomes available on activation if autoplay is restricted.
  window.addEventListener("pageshow", () => void prepareAudio().catch(() => {}));
}

window.addEventListener("resize", () => {
  if (lcd.dataset.expansion === "true") setLcdExpanded(true, true);
  if (!isMirror && scopePointer) refreshScopeTip();
});

if (isMirror) startMirror();
else configurePanel();
