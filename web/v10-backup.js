// Browser preferences are deliberately separate from receiver programming and dial backups.
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const number = (value, low, high) => typeof value === "number" && Number.isFinite(value) && value >= low && value <= high;
const integer = (value, low, high) => Number.isSafeInteger(value) && number(value, low, high);
const boolean = value => typeof value === "boolean";
const oneOf = values => value => values.includes(value);
const text = (value, limit) => typeof value === "string" && value.length <= limit && !value.includes("\0");
const hz = value => integer(value, 100000, 1750000000);
const modes = ["AUTO", "FM", "NFM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB",
  "CW", "CW-R", "FSK", "FSK-R", "DMR", "D-STAR", "P25", "dPMR", "DPMR", "NXDN-VN"];
const scanKinds = ["PROG", "DELTA", "AUTO", "MEMO", "SEL", "MODE", "ZONE", "SIMPLE"];
const scanProfiles = scanKinds.slice(0, 6).map(kind => "scan_" + kind);
const zones = ["FAV", "CB", "PMR", "BUS", "HAM", "BCAST", "RAIL", "EVT", "UTIL", "AIR", "MRN", "NEARBY",
  ...Array.from({length: 12}, (_, index) => "CUSTOM" + (index + 1))];
const quickItems = ["group", "meter", "record", "mirror", "tier-iii", "snap-carrier", "record-search", "rf-replay",
  "bookmarks", "mode", "tuning-step", "filter", "scan-start", "scan-settings", "scan-speed", "scan-pause", "priority", "skip", "memory", "scope",
  "volume", "boost", "squelch", "rf-gain", "history", "replay"];
const newQuickItems = ["snap-carrier", "record-search", "rf-replay", "bookmarks"];
const scanQuickItems = ["scan-speed", "scan-pause"];
const quickDefaults = ["group", "meter", "record", "mirror", "tier-iii", ...newQuickItems, ...scanQuickItems];
const mobileKeyActions = ["none", "quick", "monitor", "record", "mode", "frequency", "volume", "speech", "lockout",
  "simple-scan", "bookmarks", "scan-speed", "memory", "group", "tuning-lock", "scope", "recenter-scope"];
const panelDefaults = {dialLock: false, channelDial: false, page: 1, expanded: false, visible: true,
  freeze: false, fixed: false, marker: false, span: 1000000, edges: null, scopePan: 0};

const defaults = {
  lcd_backlight: 100, mute_button_dim: true, led_bright: 100, lcd_type: "A", waterfall_colours: "spectrum",
  display_memory: true, display_rds: true, p25_rx_id_format: "decimal",
  speech_language: "en-GB", speech_speed: "fast", speech_signal: true, speech_level: 50,
  beep_level: 50, beep_confirmation: true, speech_lock_switch: "speech", screen_capture: false,
  scope_max_hold: "off", scope_averaging: 0, scope_waterfall: true, scope_waterfall_speed: "mid",
  scope_reference: 0, scope_sweep_speed: "mid", meter_type: "dbfs", snap_to_carrier: true,
  volume: 32, volume_muted: false, audio_boost: 0, powerVolume: 0.32,
  panel_display: panelDefaults, favorite_bank_a: 0, favorite_bank_b: 1,
  quick_menu_v3: quickDefaults, radio_bookmarks: [], repeater_locator: "",
  mobile_p1_action: "quick", mobile_p2_action: "monitor",
};

function validPanel(value) {
  if (!object(value) || Object.keys(value).length !== Object.keys(panelDefaults).length
      || !Object.keys(panelDefaults).every(key => Object.hasOwn(value, key))) return false;
  return ["dialLock", "channelDial", "expanded", "visible", "freeze", "fixed", "marker"].every(key => boolean(value[key]))
    && [1, 2].includes(value.page) && (value.span === "full" || number(value.span, 1, 1800000))
    && number(value.scopePan, -1750000000, 1750000000)
    && (value.edges === null || Array.isArray(value.edges) && value.edges.length === 2
      && value.edges.every(hz) && value.edges[0] < value.edges[1])
    && (!value.fixed || value.edges !== null);
}

function validBookmarks(value) {
  return Array.isArray(value) && value.length <= 200 && value.every(entry => object(entry)
    && Object.keys(entry).every(key => ["name", "hz", "mode", "callsign"].includes(key))
    && text(entry.name, 64) && entry.name.trim().length > 0 && hz(entry.hz) && modes.includes(entry.mode)
    && (!Object.hasOwn(entry, "callsign") || text(entry.callsign, 64)));
}

function validScan(value) {
  if (!object(value) || value.v10 !== "scan" || value.enabled !== true || !scanKinds.includes(value.kind)) return false;
  const fields = {
    v10: item => item === "scan", kind: item => scanKinds.includes(item), enabled: item => item === true,
    start_hz: hz, end_hz: hz, center_hz: hz, span_hz: item => integer(item, 1, 1750000000),
    mode: item => modes.includes(item) && item !== "AUTO", group: item => integer(item, 0, 100),
    edge: item => integer(item, 0, 49), select_tag: item => integer(item, 1, 9),
    start_slot: item => integer(item, 0, 199), end_slot: item => integer(item, 0, 199), auto_clear: boolean,
    direction: oneOf([-1, 1]), step: item => integer(item, 1, 1000000),
    scope_span_hz: item => integer(item, 1, 1800000), zone: item => zones.includes(item),
    zones: items => Array.isArray(items) && items.length > 0 && items.length <= zones.length
      && new Set(items).size === items.length && items.every(item => zones.includes(item)),
  };
  if (!Object.entries(value).every(([key, item]) => Object.hasOwn(fields, key) && fields[key](item))) return false;
  if (value.start_hz !== undefined && value.end_hz !== undefined && value.start_hz >= value.end_hz) return false;
  if (value.start_slot !== undefined && value.end_slot !== undefined && value.start_slot > value.end_slot) return false;
  if (value.kind === "ZONE") return Boolean(value.zone || value.zones);
  return true;
}

function validQuickItems(value) {
  return Array.isArray(value) && value.length <= quickItems.length
    && new Set(value).size === value.length && value.every(item => quickItems.includes(item));
}

const validators = {
  lcd_backlight: value => number(value, 0, 100), mute_button_dim: boolean, led_bright: value => number(value, 0, 100),
  lcd_type: oneOf(["A", "B", "C", "D", "E"]), waterfall_colours: oneOf(["spectrum", "blue", "amber", "greyscale"]),
  display_memory: boolean, display_rds: boolean, p25_rx_id_format: oneOf(["decimal", "hex"]),
  speech_language: oneOf(["en-GB", "ja-JP"]), speech_speed: oneOf(["slow", "fast"]), speech_signal: boolean,
  speech_level: value => number(value, 0, 100), beep_level: value => number(value, 0, 100), beep_confirmation: boolean,
  speech_lock_switch: oneOf(["speech", "lock"]), screen_capture: boolean,
  scope_max_hold: oneOf(["off", "10s", "on"]), scope_averaging: oneOf([0, 2, 3, 4]), scope_waterfall: boolean,
  scope_waterfall_speed: oneOf(["slow", "mid", "fast"]), scope_reference: value => number(value, -20, 20),
  scope_sweep_speed: oneOf(["slow", "mid", "fast"]), meter_type: oneOf(["relative", "dbfs"]), snap_to_carrier: boolean,
  volume: value => number(value, 0, 100), volume_muted: boolean, audio_boost: value => number(value, 0, 24),
  powerVolume: value => number(value, 0.01, 1), panel_display: validPanel,
  favorite_bank_a: value => integer(value, 0, 99), favorite_bank_b: value => integer(value, 0, 99),
  quick_menu_v3: validQuickItems,
  mobile_p1_action: oneOf(mobileKeyActions), mobile_p2_action: oneOf(mobileKeyActions),
  radio_bookmarks: validBookmarks,
  repeater_locator: value => typeof value === "string" && (value === "" || /^[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?$/i.test(value)),
  ...Object.fromEntries(scanProfiles.map(key => [key, value => validScan(value) && value.kind === key.slice(5)])),
  last_scan: validScan,
};

// Remove the older key when applying a canonical backup so it cannot compete with v3.
export const CLIENT_SETTING_KEYS = Object.freeze([...Object.keys(validators), "quick_menu_v2"]);

export function validateClientSettings(value) {
  if (object(value)) {
    // Backups and shared profiles from before programmable mobile keys remain valid.
    value = {mobile_p1_action: defaults.mobile_p1_action, mobile_p2_action: defaults.mobile_p2_action, ...value};
  }
  if (object(value) && Object.hasOwn(value, "quick_menu_v2") && !Object.hasOwn(value, "quick_menu_v3")
      && validQuickItems(value.quick_menu_v2)) {
    value = {...value, quick_menu_v3: [...new Set([...value.quick_menu_v2, ...scanQuickItems])]};
    delete value.quick_menu_v2;
  }
  if (!object(value) || !Object.keys(defaults).every(key => Object.hasOwn(value, key))
      || !Object.entries(value).every(([key, item]) => Object.hasOwn(validators, key) && validators[key](item))) {
    throw new Error("This backup contains unsupported browser settings.");
  }
  return structuredClone(value);
}

export function snapshotClientSettings(read, panel) {
  const result = {};
  for (const [key, fallback] of Object.entries(defaults)) {
    const saved = key === "panel_display" ? panel : read(key, fallback);
    result[key] = structuredClone(validators[key](saved) ? saved : fallback);
  }
  // Mirror the menu's migrations while exporting only the current selection key.
  if (!Array.isArray(read("quick_menu_v3", null))) {
    const previous = read("quick_menu_v2", null);
    const legacy = read("quick_menu", null);
    const migrated = Array.isArray(previous) ? [...new Set([...previous, ...scanQuickItems])]
      : Array.isArray(legacy) ? [...new Set([...legacy, ...newQuickItems, ...scanQuickItems])] : quickDefaults;
    if (validQuickItems(migrated)) result.quick_menu_v3 = migrated;
  }
  if (read("volume", null) === null) result.volume = result.powerVolume * 100;
  for (const key of [...scanProfiles, "last_scan"]) {
    const saved = read(key, null);
    if (saved !== null && validators[key](saved)) result[key] = structuredClone(saved);
  }
  return validateClientSettings(result);
}
