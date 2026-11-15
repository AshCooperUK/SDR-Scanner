const NUMBER_FORMAT = new Intl.NumberFormat();
const TIME_FORMAT = new Intl.DateTimeFormat(undefined, {timeStyle: "medium"});
const NO_LCD_NAVIGATION = new Set(["dial-a", "dial-b"]);
const LCD_RETURN_ONLY = new Set(["dial-c", "filter", "scan-start", "tune-step", "vfo-memory", "choose-value"]);
const CUSTOM_PRESET_NAMES = Object.fromEntries(Array.from({length: 12}, (_, index) => {
  const slot = index + 1;
  return ["CUSTOM" + slot, "Custom " + slot];
}));
export const isCustomPreset = zone => Object.hasOwn(CUSTOM_PRESET_NAMES, zone);
export const ZONE_NAMES = {FAV: "Favourite", CB: "CB", PMR: "PMR446", BUS: "Business", HAM: "Amateur",
  BCAST: "FM Radio", RAIL: "Rail", EVT: "Events", UTIL: "Utilities", AIR: "Air", MRN: "Marine",
  NEARBY: "Nearby Signals", ...CUSTOM_PRESET_NAMES};
const ZONE_PAGE_SIZE = 50;
const FULL_RANGE_SETTINGS = {HAM: "ham_full_range", BUS: "bus_full_range"};
const PROJECT_LINKS = [
  {kind: "link", label: "GitHub", href: "https://github.com/AshCooperUK", title: "Open the maintainer’s GitHub profile."},
  {kind: "link", label: "User Manual", href: "https://github.com/AshCooperUK/SDR-Scanner/blob/main/docs/V10.md",
    title: "Open the current SDr Scanner manual on GitHub."},
];
const CUSTOM_STEPS = [[0, "Auto"], [100, "100 Hz"], [500, "500 Hz"], [1000, "1 kHz"],
  [2500, "2.5 kHz"], [3125, "3.125 kHz"], [5000, "5 kHz"], [6250, "6.25 kHz"],
  [8333, "8.33 kHz"], [10000, "10 kHz"], [12500, "12.5 kHz"],
  [25000, "25 kHz"], [50000, "50 kHz"], [100000, "100 kHz"]];

function page(id, label, items) {
  return {kind: "page", id, label, items};
}

function readout(label, value) {
  return {kind: "readout", label, value};
}

function selectable(item) {
  return item && !item.disabled && ["page", "setting", "action", "link"].includes(item.kind);
}

function choice(key, label, initial, options, target = "server") {
  return {kind: "setting", key, label, initial, options, target};
}

function number(key, label, initial, min, max, step, unit = "", target = "server") {
  return {kind: "setting", key, label, initial, min, max, step, unit, target};
}

function flag(key, label, initial = false, target) {
  return choice(key, label, initial, [[false, "OFF"], [true, "ON"]], target);
}

function run(label, action, value = "") {
  return {kind: "action", label, action, value};
}

function field(label, name, options = {}) {
  return {label, name, ...options};
}

const timeChoices = (first, last, hold) => [
  ...Array.from({length: last - first + 1}, (_, index) => [index + first, (index + first) + " s"]),
  [hold, "HOLD"],
];

export const TUNING_STEPS = [100, 1000, 2500, 3125, 5000, 6250, 8333, 9000, 10000, 12500, 20000, 25000, 100000, 1000000];

const SCAN_SPEED = choice("scan_speed", "Scan Speed", 8,
  Array.from({length: 30}, (_, index) => [index + 1, String(index + 1)]));

const SCAN_SETTING = [
  flag("scan_resume", "Scan Resume", true),
  choice("pause_time", "Pause Timer", 10, timeChoices(1, 30, 0)),
  choice("resume_time", "Resume Timer", 2, timeChoices(0, 5, -1)),
  SCAN_SPEED,
  number("priority_interval", "Priority Interval", 5, 1, 15, 1, "s"),
  choice("skip_timer", "Temporary Skip Timer", 5, [[5, "5 min"], [10, "10 min"], [15, "15 min"]]),
  flag("skip_function", "SKIP Function", true),
  choice("auto_memory_clear", "Auto MW Scan Memory Clear", "ask", [["never", "OFF"], ["ask", "Display Dialog"], ["always", "ON"]]),
  flag("scan_screen", "Display SCAN Screen at Start", true),
  choice("scan_dial", "Main Dial Operation during Scan", "direction", [["direction", "Up/Down"], ["stop", "Stop"]]),
];

const displaySettings = ctx => [
  number("lcd_backlight", "LCD Backlight", 100, 0, 100, 1, "%", "client"),
  flag("mute_button_dim", "Mute Button Dim", true, "client"),
  number("led_bright", "LED Bright", 100, 0, 100, 1, "%", "client"),
  choice("lcd_type", "Display Type", "A", [["A", "A · Black"], ["B", "B · Blue"]], "client"),
  flag("display_memory", "Memory Name", true, "client"),
  flag("display_rds", "RDS", true, "client"),
  ...(modeAvailable(ctx, "P25") ? [
    choice("p25_rx_id_format", "P25 RX ID Display", "decimal",
      [["decimal", "Decimal"], ["hex", "Hexadecimal"]], "client"),
  ] : []),
];

const SPEECH = page("speech-settings", "SPEECH", [
  choice("speech_language", "Speech Language", "en-GB", [["en-GB", "English"], ["ja-JP", "Japanese"]], "client"),
  choice("speech_speed", "Speech Speed", "fast", [["slow", "Slow"], ["fast", "Fast"]], "client"),
  flag("speech_signal", "S-Level Speech", true, "client"),
  number("speech_level", "Speech Level", 50, 0, 100, 1, "%", "client"),
]);

const FUNCTION_SETTINGS = [
  number("beep_level", "Beep Level", 50, 0, 100, 1, "%", "client"),
  flag("beep_confirmation", "Beep · Confirmation", true, "client"),
  SPEECH,
  choice("speech_lock_switch", "SPEECH/LOCK Switch", "speech", [["speech", "Tap Speech"], ["lock", "Tap Lock"]], "client"),
  choice("dial_auto_ts", "Main Dial Auto TS", "high", [["off", "OFF"], ["low", "Low"], ["high", "High"]], "client"),
  flag("screen_capture", "Screen Capture · POWER Switch", false, "client"),
  readout("Screen Capture File Type", "PNG"),
];

function modeToneItems(ctx, mode) {
  const forMode = item => item && {...item, mode};
  const items = [
    forMode(rfNumber(ctx, "tone_bass", "Bass", 0, -15, 15, 1, "dB")),
    forMode(rfNumber(ctx, "tone_treble", "Treble", 0, -15, 15, 1, "dB")),
  ];
  const cutoff = (key, label, min, max) => forMode(rfChoice(ctx, key, label, 0, [
    [0, "OFF"], ...range((max - min) / 100 + 1, min / 100).map(n => [n * 100, n * 100 + " Hz"]),
  ]));
  if (mode !== "WFM") items.unshift(
    cutoff("tone_hpf", "HPF", 100, 2000),
    cutoff("tone_lpf", "LPF", 500, 2400),
  );
  if (mode === "FM") items.push(forMode(rfChoice(ctx, "fm_deemphasis", "De-Emphasis", true, [[false, "OFF"], [true, "ON"]])));
  return items;
}

const toneBranch = mode => page("tone-" + mode.toLowerCase(), mode, ctx => modeToneItems(ctx, mode));
const toneControl = ctx => [
  ...["FM", "WFM", "AM"].filter(mode => modeAvailable(ctx, mode)).map(toneBranch),
  ...(modeAvailable(ctx, "USB") || modeAvailable(ctx, "LSB") ? [
    page("tone-ssb", "SSB", ["USB", "LSB"].filter(mode => modeAvailable(ctx, mode)).map(toneBranch)),
  ] : []),
  ...["CW", "FSK", "D-STAR", "P25", "dPMR", "NXDN-VN", "DMR"]
    .filter(mode => modeAvailable(ctx, mode)).map(toneBranch),
];

const DIGITAL_SETTINGS = [
  run("History Logging", ctx => ctx.change({discovery: "toggle"}), ctx => ctx.radio.discovery_enabled ? "ON" : "OFF"),
];

const OTHER_SETTINGS = [
  run("Timer", next => next.push(timerPage(next))),
  page("information", "Information", () => [readout("Version", document.title)]),
  page("reset", "Reset", [
    run("Partial Reset", next => next.push(resetConfirm("partial"))),
    run("All Reset", next => next.push(resetConfirm("all"))),
  ]),
];

const SCOPE_SETTINGS = [
  choice("scope_max_hold", "Max Hold", "10s", [["off", "OFF"], ["10s", "10 s Hold"], ["on", "ON"]], "client"),
  choice("scope_averaging", "Averaging", 0, [[0, "OFF"], [2, "2 frames"], [3, "3 frames"], [4, "4 frames"]], "client"),
  flag("scope_waterfall", "Waterfall Display", true, "client"),
  choice("scope_waterfall_speed", "Waterfall Speed", "mid", [["slow", "Slow"], ["mid", "Mid"], ["fast", "Fast"]], "client"),
];

const FSK_OPTIONS = [
  choice("fsk_center", "RX Frequency", "center", [["center", "Mark / Space Centre"], ["mark", "Mark (Space)"]]),
  choice("fsk_pitch", "Tone Frequency", 1615, [1275, 1500, 1615, 2125].map(n => [n, n + " Hz"])),
  choice("fsk_shift", "Shift Width", 170, [170, 200, 425, 800, 850].map(n => [n, n + " Hz"])),
  choice("fsk_baud", "Baud Rate", 45, [[45, "45.45 baud"], [50, "50 baud"]]),
  flag("fsk_usos", "Decode USOS", true),
  choice("fsk_newline", "Decode New Line Code", "any", [["any", "CR, LF or CR+LF"], ["pair", "CR+LF only"]]),
];

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(label, action, className = "key") {
  const element = node("button", className, label);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

function navigationKey(symbol, name, action) {
  const key = button(symbol, action);
  key.setAttribute("aria-label", name);
  key.title = name === "Back" ? "Return to the previous screen." : `${name} through the menu items.`;
  return key;
}

const frequency = hz => Number.isFinite(hz) ? (hz / 1e6).toFixed(6) + " MHz" : "—";
const channel = value => String(value).padStart(2, "0");
const range = (length, offset = 0) => Array.from({length}, (_, i) => i + offset);

function frequencyValue(value, label) {
  const hz = Math.round(Number(value) * 1e6);
  if (typeof value !== "string" || !/^\d{1,4}(?:\.\d{1,6})?$/.test(value.trim())
      || !Number.isSafeInteger(hz) || hz < 100_000 || hz > 1_750_000_000) {
    throw new Error(label + " must be between 0.1 and 1750 MHz.");
  }
  return hz;
}

function requestJson(path) {
  return fetch(path, {cache: "no-store"}).then(response => {
    if (!response.ok) throw new Error(response.status === 404 ? "No completed recordings." : "Receiver data is unavailable.");
    return response.json();
  });
}

function optionLabel(spec, value) {
  return spec.options.find(([option]) => String(option) === value)?.[1] ?? value;
}

function createField(spec, value, onChoose) {
  const input = node("input");
  input.name = spec.name;
  input.value = value ?? "";
  if (spec.options) {
    input.type = "hidden";
    const key = button(spec.label, () => onChoose(spec, input), "key menu-choice-field");
    key.title = fieldHelp(spec) || `See the available choices for ${spec.label}.`;
    key.dataset.field = spec.name;
    key.append(node("span", "menu-value", optionLabel(spec, input.value)));
    const wrap = node("div", "menu-form-choice");
    wrap.append(key, input);
    return wrap;
  }
  const label = node("label", "field", spec.label);
  input.type = spec.type === "time" ? "text" : spec.type ?? "text";
  if (spec.type === "time") {
    input.dataset.format = "time";
    input.placeholder = "HH:MM";
    input.maxLength = 5;
  }
  for (const attr of ["min", "max", "step", "maxLength", "inputMode", "placeholder", "accept"]) {
    if (spec[attr] !== undefined) input[attr] = spec[attr];
  }
  input.required = spec.required ?? false;
  input.title = fieldHelp(spec) || `Enter ${spec.label}.`;
  label.append(input);
  return label;
}

function getFormValue(form, name) {
  const input = form.elements.namedItem(name);
  const value = input.value.trim();
  return input.dataset.format === "time" ? timeValue(value) : value;
}

const SET_PAGES = [
  page("tones", "Tone Control", toneControl),
  page("function-settings", "Function", FUNCTION_SETTINGS),
  page("digital-settings", "Digital Set", DIGITAL_SETTINGS),
  page("display", "Display", displaySettings),
  page("dials", "Dials", dialAssignmentItems),
  page("presets", "Presets", ctx => Object.entries(ZONE_NAMES).map(([zone, name]) =>
    run(name, async next => next.push(await getZonePage(next, zone)), ctx.v10.custom_presets?.[zone]?.name))),
  page("quick-menu-settings", "Quick Menu", quickMenuSettingItems),
  page("settings-backup", "Settings Backup", [
    run("Load Setting", next => next.push(restorePage())),
    run("Save Setting", saveBackup),
  ]),
  page("others", "Others", OTHER_SETTINGS),
];

const SET_MENU = page("set", "SET", ctx => [
  ...(ctx.radio.power ? SET_PAGES : []),
  ctx.access.can_program_pin || ctx.access.can_program_pin_with_code
    ? run("PIN", next => next.setupPin?.(), ctx.access.pin_configured ? "Configured" : "Not set") : null,
]);

const ANALOGUE_DSP_MODES = ["AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB", "CW", "CW-R", "FSK", "FSK-R"];
export const supportsSsbDsp = mode => ANALOGUE_DSP_MODES.includes(mode);
export const hasAnalogFilter = mode => supportsSsbDsp(mode) || ["FM", "NFM", "WFM"].includes(mode);
const RADIO_MODES = [
  "FM", "WFM", "AM", "S-AM(D)", "S-AM(U)", "S-AM(L)", "USB", "LSB",
  "CW", "CW-R", "FSK", "FSK-R", "D-STAR", "P25", "dPMR", "NXDN-VN", "DMR",
].map(mode => [mode, mode]);
const modeAvailable = (ctx, mode) => ctx.v10.capabilities?.modes?.includes(mode) === true;
const radioModes = ctx => RADIO_MODES.filter(([mode]) => modeAvailable(ctx, mode));

export function formatP25Talkgroup(talkgroup, format) {
  return format === "hex" && Number.isInteger(talkgroup)
    ? talkgroup.toString(16).toUpperCase().padStart(4, "0")
    : String(talkgroup);
}
const DELTA_SPANS = [5000, 10000, 20000, 50000, 100000, 500000, 1000000];
const SCANS = [
  ["PROG", "PROG", "Programmed Scan"],
  ["DELTA", "ΔF", "Delta Frequency Scan"],
  ["AUTO", "AUTO", "Auto Memory Write Scan"],
  ["MEMO", "MEMO", "Memory Scan"],
  ["SEL", "SEL", "Select Memory Scan"],
  ["MODE", "MODE", "Mode Select Scan"],
];
const asMHz = hz => Number.isFinite(hz) ? String(hz / 1e6) : "";
const fmtStep = hz => hz < 1_000 ? hz + " Hz"
  : hz >= 1_000_000 ? NUMBER_FORMAT.format(hz / 1_000_000) + " MHz" : NUMBER_FORMAT.format(hz / 1000) + " kHz";
const stepOptions = current => [...new Set([10, ...TUNING_STEPS, current])]
  .sort((first, second) => first - second)
  .map(hz => [hz, hz === 10 ? "0.010 kHz · 10 Hz fine" : fmtStep(hz)]);
const groupOptions = [["", "All Groups"], ...range(100).map(n => [n, channel(n)]), [100, "A · Auto Memory"]];
const tagOptions = [[0, "OFF"], ...range(9, 1).map(n => [n, "★" + n])];
const supports = (ctx, key) => ctx.v10.capabilities?.rf?.includes(key);
const groupLabel = group => group === 100 ? "A" : channel(group);

function formatTone(tone) {
  if (tone?.kind === "CTCSS" && Number.isFinite(tone.hz)) return tone.hz.toFixed(1);
  if (tone?.kind === "DCS") return "D" + tone.code + tone.polarity;
  return "";
}

function parseTone(value) {
  if (!value) return null;
  if (/^\d{2,3}(?:\.\d)?$/.test(value) && Number(value) >= 67 && Number(value) <= 254.1) {
    return {kind: "CTCSS", hz: Number(value)};
  }
  const code = /^D([0-7]{3})(N|I|R)$/.exec(value.toUpperCase());
  if (code) return {kind: "DCS", code: code[1], polarity: code[2] === "N" ? "N" : "I"};
  throw new Error("Enter a CTCSS tone such as 123.0 or a DCS code such as D023N.");
}

function displayValue(option, value) {
  if (option.options) return option.options.find(([item]) => item === value)?.[1] ?? String(value ?? "");
  return [value, option.unit].filter(part => part !== undefined && part !== "").join(" ");
}

function memoryList(ctx) {
  return Object.values(ctx.v10.memories ?? {}).sort((a, b) => a.group - b.group || a.slot - b.slot);
}

function memLabel(entry) {
  const id = entry.group === 100 ? "A" + String(entry.slot).padStart(3, "0") : channel(entry.group) + "-" + channel(entry.slot);
  return id + (entry.name ? "  " + entry.name : "");
}

function currentMemory(ctx) {
  return memoryList(ctx).find(entry => entry.group === ctx.v10.group && entry.slot === ctx.v10.slot);
}

function memoryAction(operation, entry, values = {}) {
  return {v10: "memory", operation, group: entry.group, slot: entry.slot, ...values};
}

function confirmPage(id, label, description, action) {
  return {...page(id, label, [run("YES", action), run("NO", ctx => ctx.back())]), description, navigation: false};
}

function formPage(id, label, specs, submit, buttonLabel = "ENT") {
  return {...page(id, label, []), specs, submit, buttonLabel};
}

function editMemory(ctx, entry, kind = "write") {
  const tuned = {hz: ctx.hz, mode: ctx.mode};
  const specs = kind === "name"
    ? [field("Name", "name", {value: entry.name, maxLength: 24})]
    : [
      field("Group", "group", {options: groupOptions.slice(1), value: entry.group, required: true}),
      field("Channel", "slot", {type: "number", min: 0, max: 199, value: entry.slot, required: true}),
      field("Frequency · MHz", "hz", {value: asMHz(entry.hz ?? tuned.hz), inputMode: "decimal", required: true}),
      field("Mode", "mode", {options: radioModes(ctx), value: entry.mode ?? tuned.mode}),
      field("Name", "name", {value: entry.name, maxLength: 24}),
      field("FM Tone · 123.0 or D023N", "tone", {value: formatTone(entry.tone)}),
      field("DMR Colour Code", "color_code", {type: "number", min: 0, max: 15, value: entry.color_code}),
      field("DMR Time Slot", "timeslot", {type: "number", min: 1, max: 2, value: entry.timeslot}),
      field("DMR Talkgroup", "talkgroup", {type: "number", min: 1, max: 16777215, value: entry.talkgroup}),
    ];
  return formPage("memory-editor", kind === "name" ? "EDIT NAME" : "CH SELECT", specs, async (current, form) => {
    const name = getFormValue(form, "name");
    if (kind === "name") return current.change(memoryAction(kind, entry, {name}));
    const group = Number(getFormValue(form, "group"));
    const slot = Number(getFormValue(form, "slot"));
    if (group < 100 && slot > 99) throw new Error("Regular groups hold channels 00 to 99.");
    const hz = frequencyValue(getFormValue(form, "hz"), "Frequency");
    const mode = getFormValue(form, "mode");
    const values = {hz, mode, name};
    if (entry.selected) values.selected = entry.selected;
    if (entry.skip) values.skip = true;
    if (mode === "FM") values.tone = parseTone(getFormValue(form, "tone"));
    if (mode === "DMR") {
      for (const key of ["color_code", "timeslot", "talkgroup"]) {
        const value = getFormValue(form, key);
        values[key] = value === "" ? null : Number(value);
      }
    }
    const write = async next => {
      await next.change(memoryAction("write", {group, slot}, values));
      next.close();
    };
    if (memoryList(current).some(memory => memory.group === group && memory.slot === slot)) {
      current.push({...confirmPage("memory-overwrite", "Overwrite this Channel", "", write),
        description: "Replace " + memLabel({group, slot}) + "?"});
    } else {
      await write(current);
    }
  }, kind === "name" ? "SET" : "MW");
}

function memoryDetails(entry) {
  return {...page("memory-detail", memLabel(entry), ctx => {
    const current = memoryList(ctx).find(memory => memory.group === entry.group && memory.slot === entry.slot);
    if (!current) return [readout("Empty Channel", "")];
    return [
      run("Recall Channel", async next => {
        await next.change(memoryAction("recall", current));
        next.close();
      }, frequency(current.hz) + " · " + displayMode(current.mode)),
      run("SELECT", next => next.push(formPage("select-tag", "SELECT MEMORY", [
        field("Select Tag", "selected", {options: tagOptions, value: Number(current.selected) || 0}),
      ], async (active, form) => {
        await active.change(memoryAction("select", current, {selected: Number(getFormValue(form, "selected"))}));
        active.back();
      }, "SET")), current.selected ? "★" + Number(current.selected) : "OFF"),
      run("SKIP", next => next.change(memoryAction("skip", current, {skip: !current.skip})), current.skip ? "SKIP" : "OFF"),
      run("Edit Name", next => next.push(editMemory(next, current, "name")), current.name),
      run("Memory Write", next => next.push(editMemory(next, {...current, hz: next.hz, mode: next.mode}))),
      run("Memory Copy", next => next.push(editMemory(next, {...current, slot: nextFree(next, current.group) ?? current.slot}))),
      run("Memory Clear", next => next.push({...confirmPage("memory-delete", "Memory Clear", "", async active => {
        await active.change(memoryAction("delete", entry));
        active.back();
        active.back();
      }), description: "Clear " + memLabel(current) + "?"})),
    ];
  }), description: frequency(entry.hz) + " · " + displayMode(entry.mode)};
}

function nextFree(ctx, group) {
  const occupied = new Set(memoryList(ctx).filter(entry => entry.group === group).map(entry => entry.slot));
  return range(group === 100 ? 200 : 100).find(slot => !occupied.has(slot));
}

function writeItems(ctx, group = ctx.v10.group ?? 0) {
  const selected = currentMemory(ctx);
  const slot = nextFree(ctx, group);
  const newChannel = slot !== undefined
    ? run("Write to a New Channel", next => next.push(editMemory(next, {group, slot})))
    : null;
  return [
    newChannel,
    selected ? run("Write to the Selected Channel", next => next.push(editMemory(next, {
      ...selected, hz: next.hz, mode: next.mode,
    }))) : null,
    run("Select the Channel and Write", next => next.push(editMemory(next, {group, slot: slot ?? 0}))),
  ];
}

function memoryGroupPage(ctx, group = ctx.v10.group ?? 0) {
  return page("memory-group", "MEMORY · GROUP " + groupLabel(group), next => [
    run("GROUP", active => active.push(makeGroupPage(active)), groupLabel(group)),
    run("CH-INP", active => active.push(channelInput(group))),
    run("MW", active => active.push(page("write", "MEMORY WRITE", next => writeItems(next, group)))),
    ...memoryList(next).filter(entry => entry.group === group).map(entry =>
      run(memLabel(entry), active => active.push(memoryDetails(entry)),
        frequency(entry.hz) + "  " + displayMode(entry.mode) + (entry.skip ? "  SKIP" : "") + (entry.selected ? "  ★" + Number(entry.selected) : ""))),
  ]);
}

function makeGroupPage(ctx) {
  const counts = new Map();
  for (const entry of memoryList(ctx)) counts.set(entry.group, (counts.get(entry.group) ?? 0) + 1);
  const rows = range(101).map(group => run(groupLabel(group), next => next.push(memoryGroupPage(next, group)),
    counts.has(group) ? String(counts.get(group)) + " ch" : ""));
  const special = [
    rows.pop(),
    run("P", next => next.push(scanForm(next, "PROG", true)), "Programmed Scan Edge Pairs"),
  ];
  return page("group", "GROUP SELECT", [...special, ...rows]);
}

function getZone(ctx, zone) {
  return ctx.request("/api/v10/zones/" + zone);
}

async function getZonePage(ctx, zone) {
  const snapshot = await getZone(ctx, zone);
  return isCustomPreset(zone) ? customPresetEditor(zone, snapshot.range)
    : zoneListPage(zone, snapshot.entries, zone !== "NEARBY");
}

function customPresetEditor(zone, preset) {
  return formPage("custom-preset-editor", ZONE_NAMES[zone] + " Preset", [
    field("Name", "name", {value: preset?.name ?? ZONE_NAMES[zone], maxLength: 24, required: true}),
    field("Start · MHz", "start_hz", {value: asMHz(preset?.start_hz), inputMode: "decimal", required: true}),
    field("End · MHz", "end_hz", {value: asMHz(preset?.end_hz), inputMode: "decimal", required: true}),
    field("Scan Step", "step_hz", {options: CUSTOM_STEPS, value: preset?.step_hz ?? 0}),
  ], async (next, form) => {
    const name = getFormValue(form, "name");
    const start_hz = frequencyValue(getFormValue(form, "start_hz"), "Start frequency");
    const end_hz = frequencyValue(getFormValue(form, "end_hz"), "End frequency");
    if (!name) throw new Error("Enter a name for this preset.");
    if (start_hz >= end_hz) throw new Error("The end frequency must be higher than the start frequency.");
    const step_hz = Number(getFormValue(form, "step_hz"));
    await next.change({v10: "zone", operation: "configure", zone, name, start_hz, end_hz,
      ...(step_hz ? {step_hz} : {})});
    next.back();
  }, "SET");
}

function zoneChannelLabel(zone, entry) {
  const label = zone === "FAV" ? `Bank ${Math.floor(entry.index / 100)} CH${entry.index % 100}` : frequency(entry.hz);
  return entry.name ? label + " · " + entry.name : label;
}

function zoneListPage(zone, entries, editing, pageNumber = 0) {
  const lastPage = Math.max(0, Math.ceil(entries.length / ZONE_PAGE_SIZE) - 1);
  const activePage = Math.min(pageNumber, lastPage);
  const offset = activePage * ZONE_PAGE_SIZE;
  const items = [];
  if (editing) {
    items.push(run("Add Preset", next => next.push(zoneEditor(next, zone, activePage))));
    if (FULL_RANGE_SETTINGS[zone]) {
      items.push(choice(FULL_RANGE_SETTINGS[zone], "Full Range Scan", false, [[false, "Off"], [true, "On"]]));
    }
  } else if (zone === "NEARBY") items.push(run("Watch Nearby Signals", async next => {
    await next.change({v10: "nearby", enabled: true});
    next.close();
  }));
  if (activePage > 0) items.push(run("Previous Page", next => next.replace(zoneListPage(zone, entries, editing, activePage - 1))));
  if (activePage < lastPage) items.push(run("Next Page", next => next.replace(zoneListPage(zone, entries, editing, activePage + 1))));
  if (!entries.length) items.push(readout(zone === "NEARBY" ? "No nearby signals received yet" : "No presets stored"));
  items.push(...entries.slice(offset, offset + ZONE_PAGE_SIZE).map(entry => run(zoneChannelLabel(zone, entry), async next => {
    if (editing) next.push(zoneEntry(zone, activePage, entry));
    else {
      await next.change({v10: "zone", zone, operation: "recall", index: entry.index});
      next.close();
    }
  }, zone === "FAV" ? frequency(entry.hz) + " · " + entry.mode : entry.mode)));
  return {...page(editing ? "edit-zone" : "zone-view", (editing ? "Edit " : "") + ZONE_NAMES[zone] + " Presets", items),
    description: lastPage ? `Page ${activePage + 1} of ${lastPage + 1} · ${entries.length} presets` : ""};
}

async function refreshZoneEditor(ctx, zone, pageNumber, editedExisting) {
  ctx.back();
  if (editedExisting) ctx.back();
  try {
    const {entries} = await getZone(ctx, zone);
    ctx.replace(zoneListPage(zone, entries, true, pageNumber));
  } catch {
    ctx.notice("The preset change was saved. Reopen the zone to refresh the list.");
  }
}

function zoneEditor(ctx, zone, pageNumber, entry) {
  return formPage("zone-editor", entry ? "Edit Preset" : "Add Preset", [
    field("Frequency · MHz", "hz", {value: asMHz(entry?.hz ?? ctx.hz), inputMode: "decimal", required: true}),
    field("Mode", "mode", {options: radioModes(ctx), value: entry?.mode ?? ctx.mode, required: true}),
    field("Name", "name", {value: entry?.name, maxLength: 24}),
  ], async (next, form) => {
    const hz = frequencyValue(getFormValue(form, "hz"), "Preset frequency");
    const mode = getFormValue(form, "mode");
    const name = getFormValue(form, "name");
    await next.change({v10: "zone", zone, operation: "write", hz, mode, name,
      ...(entry ? {index: entry.index} : {})});
    await refreshZoneEditor(next, zone, pageNumber, Boolean(entry));
  }, "SET");
}

function zoneEntry(zone, pageNumber, entry) {
  return page("zone-entry", zoneChannelLabel(zone, entry), [
    readout("Receive", frequency(entry.hz) + " · " + entry.mode),
    run("Tune", async next => {
      await next.change({v10: "zone", zone, operation: "recall", index: entry.index});
      next.close();
    }),
    run("Edit Preset", next => next.push(zoneEditor(next, zone, pageNumber, entry))),
    run("Delete Preset", next => next.push(confirmPage("zone-delete", "Delete Preset?", zoneChannelLabel(zone, entry), async active => {
      await active.change({v10: "zone", zone, operation: "delete", index: entry.index});
      await refreshZoneEditor(active, zone, pageNumber, true);
    }))),
  ]);
}

function channelInput(group) {
  return formPage("channel-input", "CH-INP", [
    field("Channel", "slot", {type: "number", min: 0, max: group === 100 ? 199 : 99, required: true}),
  ], async (ctx, form) => {
    const slot = Number(getFormValue(form, "slot"));
    if (!memoryList(ctx).some(entry => entry.group === group && entry.slot === slot)) throw new Error("This channel is empty.");
    await ctx.change(memoryAction("recall", {group, slot}));
    ctx.close();
  });
}

const displayMode = mode => mode === "NFM" ? "FM" : mode || "—";

function scanForm(ctx, kind, edgeOnly = false) {
  const defaults = ctx.v10.scan_profiles?.[kind] ?? {};
  const preset = ctx.getPreference("scan_" + kind, defaults);
  const rangeScan = kind === "PROG" || kind === "AUTO";
  const fields = [];
  let scanEdge = null;
  if (rangeScan) {
    const edges = ctx.v10.edges ?? [];
    scanEdge = edges.find(edge => edge.number === preset.edge) ?? edges[0] ?? {
      number: 0, mode: preset.mode ?? ctx.mode, step: ctx.v10.settings?.tune_step ?? 12500,
      start_hz: preset.start_hz ?? Math.min(ctx.hz || 100000, 1_749_000_000),
      end_hz: preset.end_hz ?? Math.min((ctx.hz || 100000) + 1_000_000, 1_750_000_000),
    };
    fields.push(
      field("Edge Pair", "edge", {options: range(50).map(index => [index, "P" + channel(index)]), value: scanEdge.number}),
      field("Lower Edge · MHz", "start_hz", {value: asMHz(scanEdge.start_hz), inputMode: "decimal", required: true}),
      field("Upper Edge · MHz", "end_hz", {value: asMHz(scanEdge.end_hz), inputMode: "decimal", required: true}),
    );
  }
  if (kind === "DELTA") fields.push(
    field("Centre · MHz", "center_hz", {value: asMHz(preset.center_hz ?? ctx.hz), inputMode: "decimal", required: true}),
    field("ΔF SPAN", "span_hz", {options: DELTA_SPANS.map(span => [span, "±" + fmtStep(span)]), value: preset.span_hz ?? 100000}),
  );
  if (rangeScan || kind === "DELTA" || kind === "MODE") {
    fields.push(field("Receive Mode", "mode", {options: radioModes(ctx), value: scanEdge?.mode ?? preset.mode ?? ctx.mode}));
  }
  if (["MEMO", "SEL", "MODE"].includes(kind)) {
    fields.push(
      field("Group", "group", {options: groupOptions, value: preset.group ?? ""}),
      field("Lower Channel", "start_slot", {type: "number", min: 0, max: 199, value: preset.start_slot, placeholder: "Any"}),
      field("Upper Channel", "end_slot", {type: "number", min: 0, max: 199, value: preset.end_slot, placeholder: "Any"}),
    );
  }
  if (kind === "SEL") fields.push(field("Select Tag", "select_tag", {options: [["", "All Selected"], ...tagOptions.slice(1)], value: preset.select_tag ?? ""}));
  if (kind === "AUTO" && ctx.v10.settings?.auto_memory_clear === "ask") fields.push(
    field("Clear Auto Memory", "auto_clear", {options: [["no", "Keep Existing"], ["yes", "Clear"]], value: "no"}),
  );
  if (rangeScan || kind === "DELTA") {
    const step = scanEdge?.step ?? ctx.v10.settings?.tune_step ?? 12500;
    fields.push(field("Tuning Step", "step", {options: stepOptions(step), value: step}));
  }

  async function start(next, form) {
    const payload = {v10: "scan", kind, enabled: true};
    if (rangeScan) {
      payload.edge = Number(getFormValue(form, "edge"));
      payload.start_hz = frequencyValue(getFormValue(form, "start_hz"), "Lower edge");
      payload.end_hz = frequencyValue(getFormValue(form, "end_hz"), "Upper edge");
      if (payload.start_hz >= payload.end_hz) throw new Error("The upper edge must be above the lower edge.");
    }
    if (kind === "DELTA") {
      payload.center_hz = frequencyValue(getFormValue(form, "center_hz"), "Centre");
      payload.span_hz = Number(getFormValue(form, "span_hz"));
    }
    if (form.elements.namedItem("mode")) payload.mode = getFormValue(form, "mode");
    if (form.elements.namedItem("group")) {
      const group = getFormValue(form, "group");
      if (group !== "") payload.group = Number(group);
    }
    for (const key of ["start_slot", "end_slot", "select_tag"]) {
      if (form.elements.namedItem(key) && getFormValue(form, key) !== "") payload[key] = Number(getFormValue(form, key));
    }
    if (payload.start_slot > payload.end_slot) throw new Error("The upper channel must be at or above the lower channel.");
    if (kind === "AUTO") payload.auto_clear = form.elements.namedItem("auto_clear")
      ? getFormValue(form, "auto_clear") === "yes" : next.v10.settings?.auto_memory_clear === "always";
    if (form.elements.namedItem("step")) {
      const step = Number(getFormValue(form, "step"));
      if (kind === "DELTA" && step > payload.span_hz) throw new Error("Select a tuning step within the ΔF span.");
      if (rangeScan) {
        await next.change({
          v10: "edge", number: payload.edge, start_hz: payload.start_hz, end_hz: payload.end_hz, mode: payload.mode, step,
        });
        if (edgeOnly) {
          next.back();
          return;
        }
      } else if (step !== next.v10.settings?.tune_step) {
        await next.change({v10: "setting", key: "tune_step", value: step});
      }
    }
    await next.change(payload);
    next.setPreference("scan_" + kind, payload);
    if (next.getV10().settings?.scan_screen === false) next.close();
    else next.replace(makeScanPage(next, false));
  }

  return {
    ...formPage("scan-editor", edgeOnly ? "PROGRAM EDGE" : SCANS.find(item => item[0] === kind)[2].toUpperCase(),
      fields, start, edgeOnly ? "SET" : "START"),
    prepare: rangeScan ? form => form.elements.namedItem("edge").addEventListener("change", event => {
      const edge = ctx.v10.edges?.find(item => item.number === Number(event.target.value));
      if (!edge) return;
      for (const name of ["start_hz", "end_hz"]) form.elements.namedItem(name).value = asMHz(edge[name]);
      form.elements.namedItem("mode").value = edge.mode;
      if (edge.step) form.elements.namedItem("step").value = String(edge.step);
    }) : null,
  };
}

function makeScanPage(ctx, starter) {
  if (starter && !ctx.v10.scan_kind) return {
    ...page("scan-start", "SCAN START", SCANS.map(([kind, label, detail]) =>
      run(label, active => startSavedScan(active, kind), detail))),
    columns: 3,
  };
  const quick = ctx.v10.scan_kind && ctx.v10.scan_kind !== "OFF";
  return page(starter ? "scan-start" : "scan", starter && !quick ? "SCAN START" : "SCAN", next => [
    ...(next.v10.scan_kind && next.v10.scan_kind !== "OFF" ? [
      run("STOP", async active => {
        await active.change({v10: "scan", kind: active.v10.scan_kind, enabled: false});
        active.close();
      }, next.v10.scan_kind),
      ...(next.v10.settings?.skip_function ? [run("SKIP", active => active.change({v10: "skip"}))] : []),
      ...(["PROG", "DELTA"].includes(next.v10.scan_kind) ? [
        run("FINE", active => active.change({v10: "scan_control", operation: "fine", enabled: !active.v10.scan_fine}), next.v10.scan_fine ? "ON" : "OFF"),
        run(next.v10.scan_kind === "DELTA" ? "ΔF SPAN" : "LOWER EDGE", active => active.change({v10: "scan_control", operation: "jump"})),
        run("RECALL", async active => {
          await active.change({v10: "scan_control", operation: "recall"});
          active.close();
        }),
      ] : []),
    ] : []),
    ...SCANS.map(([kind, label, detail]) => run(label, active => active.push(scanForm(active, kind)), detail)),
    run("PRIO", active => active.push(priorityPage(active)), "Priority Scan"),
    run("SET", active => active.push(page("scan-set", "SCAN SET", SCAN_SETTING))),
  ]);
}

async function startSavedScan(ctx, kind) {
  const preset = ctx.getPreference("scan_" + kind, {});
  const edge = (ctx.v10.edges ?? []).find(item => item.number === preset.edge) ?? ctx.v10.edges?.[0];
  const rangeScan = kind === "PROG" || kind === "AUTO";
  const autoClear = ctx.v10.settings?.auto_memory_clear;
  if ((rangeScan && !edge) || (kind === "AUTO" && autoClear === "ask")) return ctx.push(scanForm(ctx, kind));

  const payload = {...preset, v10: "scan", kind, enabled: true};
  if (rangeScan) {
    payload.edge = edge.number;
    delete payload.start_hz;
    delete payload.end_hz;
    if (kind === "AUTO") payload.auto_clear = autoClear === "always";
  }
  if (kind === "DELTA") {
    payload.center_hz = ctx.hz;
    payload.span_hz ??= 100000;
    payload.mode ??= ctx.mode;
  }
  if (kind === "MODE") payload.mode ??= ctx.mode;
  await ctx.change(payload);
  if (ctx.getV10().settings?.scan_screen === false) ctx.close();
  else ctx.replace(makeScanPage(ctx, false));
}

function priorityPage(ctx) {
  const options = memoryList(ctx).map(entry => [channel(entry.group) + "-" + channel(entry.slot), memLabel(entry)]);
  const interval = number("priority_interval", "PRIO INTVL", 5, 1, 15, 1, "s");
  async function stop(active) {
    await active.change({v10: "priority", enabled: false});
    active.close();
  }
  return page("prio", "PRIORITY SCAN", [
    ctx.v10.priority?.enabled ? run("STOP", stop) :
      options.length ? run("SELECT", next => next.push(formPage("prio-select", "PRIORITY CHANNEL", [
        field("Memory Channel", "memory", {options,
          value: options.find(([key]) => key === channel(ctx.v10.priority?.group) + "-" + channel(ctx.v10.priority?.slot))?.[0] ?? options[0][0]}),
      ], async (active, form) => {
        const [group, slot] = getFormValue(form, "memory").split("-").map(Number);
        await active.change({v10: "priority", enabled: true, group, slot});
        active.close();
      }, "START"))) :
      readout("No saved memories"),
    interval,
  ]);
}

function rfChoice(ctx, key, label, initial, options) {
  return supports(ctx, key) ? choice(key, label, initial, options) : null;
}

function rfNumber(ctx, key, label, initial, min, max, step, unit = "") {
  return supports(ctx, key) ? number(key, label, initial, min, max, step, unit) : null;
}

function filterPage(ctx) {
  const mode = ctx.mode;
  const configured = ctx.v10.settings?.bandwidth_hz;
  const cw = mode === "CW" || mode === "CW-R";
  const fsk = mode === "FSK" || mode === "FSK-R";
  const am = mode === "AM" || mode.startsWith("S-AM");
  if (!hasAnalogFilter(mode)) return page("filter", "FILTER", []);
  const choices = mode === "WFM" ? [200000]
    : mode === "NFM" || mode === "FM" ? [50000, 15000, 7000]
    : am ? range(50, 1).map(n => n * 200)
    : cw || fsk ? [...range(10, 1).map(n => n * 50), ...range(cw ? 31 : 22, 6).map(n => n * 100)]
    : [...range(10, 1).map(n => n * 50), ...range(31, 6).map(n => n * 100)];
  const presets = mode === "WFM" ? [200000]
    : am ? [9000, 6000, 3000]
    : mode === "NFM" || mode === "FM" ? [50000, 15000, 7000]
    : cw ? [1200, 500, 250]
    : fsk ? [2400, 500, 250]
    : choices.length === 3 ? choices
    : [3000, 2400, 1800];
  const actions = supports(ctx, "bandwidth_hz") ? presets.map((hz, i) => run("FIL" + (i + 1), next => {
    return next.change({v10: "setting", key: "bandwidth_hz", value: hz});
  }, fmtStep(hz))) : [];
  return page("filter", "FILTER · " + displayMode(mode), [
    ...actions,
    rfChoice(ctx, "bandwidth_hz", "BW", choices.includes(configured) ? configured : presets[0], choices.map(hz => [hz, fmtStep(hz)])),
    ...(["USB", "LSB", "CW", "CW-R"].includes(mode) ? [
      rfChoice(ctx, "if_shape", "Shape", "sharp", [["sharp", "SHARP"], ["soft", "SOFT"]]),
    ] : []),
    ...(supportsSsbDsp(mode) && supports(ctx, "pbt1") && supports(ctx, "pbt2") ? [
      run("PBT CLEAR", async next => {
        await next.change({v10: "setting", key: "pbt1", value: 0});
        await next.change({v10: "setting", key: "pbt2", value: 0});
      }),
    ] : []),
  ]);
}

function tonePage(ctx) {
  const tone = ctx.radio.channel_tone ?? ctx.radio.tone_filter;
  const entry = currentMemory(ctx);
  const available = entry?.mode === "FM" && ctx.v10.frequency_mode === "memory";
  const apply = async (next, configured) => {
    await next.change(memoryAction("write", entry, {...entry, tone: configured}));
    await next.change(memoryAction("recall", entry));
    next.back();
  };
  return page("tone", "TONE FREQUENCY", [
    run("T-SCAN", active => active.change({setting: "tone"}), ctx.radio.tone_enabled ? "ON" : "OFF"),
    available ? run("T-SQL / DTCS", next => next.push(formPage("tone-select", "TONE FREQUENCY", [
      field("CTCSS or DCS Code", "tone", {value: formatTone(tone), placeholder: "123.0 or D023N"}),
    ], (active, form) => apply(active, parseTone(getFormValue(form, "tone"))), "SET")), formatTone(entry.tone))
      : null,
    ...(available && tone ? [run("Use Detected Tone", next => {
      const filter = parseTone(formatTone(tone));
      return next.change(memoryAction("write", entry, {...entry, tone: filter}));
    }, formatTone(tone))] : []),
  ]);
}

function digitalSquelchPage(ctx) {
  if (ctx.mode !== "DMR") {
    const p25 = ctx.mode === "P25";
    const nxdn = ctx.mode.startsWith("NXDN");
    const key = p25 ? "p25_nac" : "nxdn_ran";
    const current = ctx.v10.settings?.[key];
    const format = value => value == null ? "" : p25 ? value.toString(16).padStart(3, "0").toUpperCase() : value;
    const label = p25 ? "NAC" : "RAN";
    return page("dmr-squelch", "D.SQL · " + ctx.mode, [
      run(label, next => next.push(formPage("digital-squelch-editor", "SET " + label, [
        field(p25 ? "NAC · Hex" : "RAN", "code", {value: format(current), inputMode: p25 ? "hex" : "numeric", maxLength: p25 ? 3 : 2, placeholder: "Any"}),
      ], async (active, form) => {
        const raw = getFormValue(form, "code");
        if (raw && !(p25 ? /^[\dA-Fa-f]{1,3}$/.test(raw) : /^\d{1,2}$/.test(raw) && Number(raw) <= 63)) {
          throw new Error(p25 ? "Enter a hexadecimal NAC from 001 to FFE." : "Enter a RAN from 0 to 63.");
        }
        if (p25 && raw && [0, 4095].includes(Number.parseInt(raw, 16))) {
          throw new Error("Enter a NAC from 001 to FFE, or clear the field for any NAC.");
        }
        await active.change({v10: "setting", key, value: raw ? Number.parseInt(raw, p25 ? 16 : 10) : null});
        active.back();
      }, "SET")), current == null ? "ANY" : String(format(current))),
    ]);
  }
  return page("dmr-squelch", "D.SQL · DMR", [
    choice("dmr_squelch", "Digital Squelch", "any", [["any", "ANY"], ["group", "GROUP"], ["individual", "INDIVIDUAL"]]),
    readout("Colour Code", String(ctx.radio.dmr?.call?.color_code ?? "—")),
    readout("Time Slot", String(ctx.radio.dmr?.call?.slot ?? "—")),
    readout("Talkgroup", String(ctx.radio.dmr?.call?.talkgroup ?? "—")),
    run("Conventional Memory", active => active.push(dmrMemoryPage(active))),
  ]);
}

function functionPage(ctx) {
  const mode = ctx.mode;
  const fm = mode === "FM";
  const fsk = mode === "FSK" || mode === "FSK-R";
  const digitalSquelch = ["DMR", "P25", "NXDN-VN"].includes(mode) && modeAvailable(ctx, mode);
  return {...page("function", "FUNCTION", [
    fm ? run("TONE", next => next.push(tonePage(next))) : null,
    fsk ? page("fsk-tone", "TONE/SHIFT", FSK_OPTIONS.slice(0, 3)) : null,
    digitalSquelch ? run("D.SQL", next => next.push(digitalSquelchPage(next))) : null,
    supportsSsbDsp(mode)
      ? rfChoice(ctx, "agc", "AGC", "mid", [["off", "OFF"], ["fast", "FAST"], ["mid", "MID"], ["slow", "SLOW"]])
      : null,
    rfNumber(ctx, "notch", "NOTCH", 0, 0, 10_000, 1, "Hz"),
    supportsSsbDsp(mode) && supports(ctx, "noise_blanker") ? page("dsp-noise-blanker", "NB", [
      flag("noise_blanker", "Noise Blanker"), number("nb_level", "LEVEL", 45, 0, 100, 1, "%"),
    ]) : null,
    hasAnalogFilter(mode) && supports(ctx, "noise_reduction") ? page("dsp-noise-reduction", "NR", [
      flag("noise_reduction", "Noise Reduction"), number("nr_level", "LEVEL", 7, 0, 15, 1),
    ]) : null,
  ].filter(Boolean)), columns: 5};
}

export const AF_VOLUME_STEP = 2.5;
const DIAL_B_VOLUME = number("volume", "AF Volume", 32, 0, 100, 0.5, "%", "client");
const SQUELCH = {...number("squelch", "SQL", 0, 0, 100, 1, "%", "knob"), knob: "squelch"};
const AUDIO_BOOST = number("audio_boost", "AUDIO BOOST", 0, 0, 24, 1, "dB", "client");
const DIAL_LABELS = {"dial-a": "Dial A", "dial-b": "Dial B", "dial-c": "Dial C", "dial-d": "Dial D", "dial-e": "Dial E",
  frequency: "Main Dial"};
const DIAL_PREFERENCES = {"dial-a": "dial_a_function", "dial-b": "dial_b_function", "dial-c": "dial_c_function",
  "dial-d": "dial_d_function", "dial-e": "dial_e_function", frequency: "main_dial_function"};
const DIAL_SENSITIVITIES = [[0.25, "Very Low"], [0.5, "Low"], [1, "Normal"], [2, "High"], [4, "Very High"]];
const dialSensitivityKey = dial => DIAL_PREFERENCES[dial].replace(/_function$/, "_sensitivity");
const DIAL_DEFAULTS = {"dial-a": "pause_time", "dial-b": "volume", "dial-c": "memory-channel",
  "dial-d": "squelch", "dial-e": "fine-tuning", frequency: "tuning"};
const SCAN_PAUSE = number("pause_time", "Scan Pause Time", 10, 0, 30, 1, "s");
const PRIORITY_INTERVAL = number("priority_interval", "PRIO INTVL", 5, 1, 15, 1, "s");
const SCAN_RESUME = choice("resume_time", "RESUME TIME", 2, timeChoices(0, 5, -1));
const dialAction = (key, label) => ({key, label});
const FINE_TUNING = dialAction("fine-tuning", "Fine Tuning · 10 Hz");
const TUNING_STEP = dialAction("tuning-step", "Tuning Step");
const SCAN_DIRECTION = dialAction("scan-direction", "Scan Direction");
const MEMORY_CHANNEL = dialAction("memory-channel", "Memory Channel");
const SCOPE_SPAN = dialAction("scope-span", "Scope Span");
const SCOPE_REFERENCE = number("scope_reference", "Scope Reference", 0, -20, 20, 0.5, "dB", "client");

function dialBOptions(ctx) {
  return [
    DIAL_B_VOLUME,
    rfNumber(ctx, "rf_gain", "RF GAIN", 100, 0, 100, 1, "%"),
    SQUELCH,
    AUDIO_BOOST,
    rfNumber(ctx, "tone_treble", "TREBLE", 0, -15, 15, 1, "dB"),
    rfNumber(ctx, "tone_bass", "BASS", 0, -15, 15, 1, "dB"),
    number("beep_level", "Beep Level", 50, 0, 100, 5, "%", "client"),
  ].filter(Boolean);
}

function dialOptions(ctx) {
  return [dialAction("tuning", "Frequency Tuning"), FINE_TUNING,
    dialAction("coarse-tuning", "Coarse Tuning · 10 kHz"), TUNING_STEP, MEMORY_CHANNEL,
    ...dialBOptions(ctx), SCOPE_SPAN, SCOPE_REFERENCE, SCAN_DIRECTION, SCAN_SPEED, SCAN_PAUSE,
    SCAN_RESUME, PRIORITY_INTERVAL];
}

function assignedDial(ctx, dial, options = dialOptions(ctx)) {
  const defaultKey = DIAL_DEFAULTS[dial];
  const key = ctx.getPreference(DIAL_PREFERENCES[dial], defaultKey);
  return options.find(option => option.key === key) ?? options.find(option => option.key === defaultKey);
}

function dialAssignmentPage(ctx, dial) {
  const options = dialOptions(ctx);
  const assignment = choicesPage("dial-assignment", DIAL_LABELS[dial], options.map(({key, label}) => [key, label]),
    assignedDial(ctx, dial, options).key, (active, key) => {
      active.setPreference(DIAL_PREFERENCES[dial], key);
      active.back();
    });
  const sensitivity = choice(dialSensitivityKey(dial), "Sensitivity", 1, DIAL_SENSITIVITIES, "client");
  return {...assignment, dial, items: [sensitivity, ...assignment.items], selected: assignment.selected + 1};
}

function dialAssignmentItems(ctx) {
  return Object.entries(DIAL_LABELS).map(([dial, label]) => run(label,
    next => next.push(dialAssignmentPage(next, dial)), assignedDial(ctx, dial).label));
}

function dialPage(ctx, id) {
  if (id === "dial-a") return page(id, "DIAL A", [SCAN_SPEED, PRIORITY_INTERVAL, SCAN_RESUME]);
  if (id === "dial-b") {
    const options = dialBOptions(ctx);
    const monitor = run("Monitor", async next => {
      await next.change({v10: "monitor", enabled: !next.getState().v10?.monitor});
      next.replace(dialPage(next, id));
    }, ctx.getV10().monitor ? "ON" : "OFF");
    const selected = options.findIndex(option => option.key === assignedDial(ctx, id).key);
    return {...page(id, "DIAL B", [...options, monitor]), selected: Math.max(0, selected)};
  }
  const items = [];
  if (["AM", "LSB", "USB", "FSK", "FSK-R", "CW", "CW-R"].includes(ctx.mode) || ctx.mode.startsWith("S-AM")) items.push(
    rfNumber(ctx, "pbt1", "PBT1", 0, -2500, 2500, 50, "Hz"),
    rfNumber(ctx, "pbt2", "PBT2", 0, -2500, 2500, 50, "Hz"),
  );
  if (ctx.mode.startsWith("CW")) items.push(
    rfNumber(ctx, "cw_pitch", "CW PITCH", 600, 300, 900, 5, "Hz"),
    rfChoice(ctx, "audio_peak", "APF", false, [[false, "OFF"], [true, "ON"]]),
  );
  if (ctx.mode.startsWith("FSK")) items.push(flag("fsk_tpf", "TPF"));
  items.push(number("lcd_backlight", "BACKLIGHT", 100, 0, 100, 1, "%", "client"));
  return page(id, "DIAL C", items);
}

function recorderPage(ctx) {
  return page("record", "RECORD", [
    run(ctx.radio.record_enabled ? "«REC Stop»" : "«REC Start»", next => next.change({record: "toggle"}),
      ctx.radio.record_active ? "Recording" : ctx.radio.record_enabled ? "Armed" : "OFF"),
    run("Play Files", async next => {
      const clip = await next.request("/api/recordings/last");
      next.push(page("record-play", "PLAY FILES", [
        run(clip.label, value => value.play(clip.url)),
        run("STOP", value => value.stopPlayback()),
      ]));
    }),
    ...(ctx.radio.replay_available ? [run("Instant Replay", next => next.replay())] : []),
    page("recorder-set", "Recorder Set", [
      readout("RX REC Condition", "Squelch Auto"),
      readout("File Split", "ON"),
      readout("File Format", "MP3"),
      readout("Minimum Duration", "2 s"),
      formPage("record-folder", "REC FOLDER", [
        field("Folder on Receiver Host", "path", {value: ctx.radio.record_folder, required: true}),
      ], async (active, form) => {
        await active.change({record: "select", path: getFormValue(form, "path")});
        const message = active.getState().record_message;
        if (message) active.notice(message);
        else active.back();
      }, "SET"),
    ]),
  ]);
}

function hitLabel(hit) {
  const time = TIME_FORMAT.format(new Date(hit.time_ms));
  return time + "  " + frequency(hit.hz) + "  " + displayMode(hit.mode);
}

function historyTone(tone) {
  if (tone?.kind === "CTCSS" && Number.isFinite(tone.hz)) return "CTCSS " + tone.hz.toFixed(1) + " Hz";
  if (tone?.kind === "DCS" && tone.code && tone.polarity) return "DCS " + formatTone(tone);
  return "";
}

function historySignal(value) {
  return Number.isFinite(value) ? value.toFixed(1) + " dBFS" : "";
}

function hitSummary(hit) {
  return [hit.sms ? "SMS" : historyTone(hit.tone), historySignal(hit.signal_dbfs)].filter(Boolean).join(" · ");
}

function hitDetails(hit) {
  const call = hit.digital ?? hit.dmr;
  const tone = historyTone(hit.tone);
  const receiveCode = hit.digital?.receive_code;
  const receiveCodeKind = hit.digital?.receive_kind;
  return page("history-hit", hitLabel(hit), ctx => {
    const metadata = [
      [hit.mode_detected ? "Detected Mode" : "Receive Mode", displayMode(hit.mode)],
      ["Detected Tone", tone || (hit.mode === "FM" ? "—" : "")],
      ["Station / Memory", hit.station_name],
      ["Audio Duration", hit.sms ? null : (hit.duration_ms / 1000).toFixed(2) + " s"],
      ["Peak Signal", historySignal(hit.signal_dbfs)],
      ["Peak SNR", Number.isFinite(hit.snr_db) ? hit.snr_db.toFixed(1) + " dB" : ""],
      ["Carrier Offset (Estimated)", Number.isFinite(hit.offset_hz)
        ? (hit.offset_hz >= 0 ? "+" : "") + Math.round(hit.offset_hz) + " Hz" : ""],
      ["Colour Code", hit.dmr?.color_code],
      ["Time Slot", hit.dmr?.slot],
      ["Talkgroup", call?.talkgroup != null && hit.mode === "P25"
        ? formatP25Talkgroup(call.talkgroup, ctx.getPreference("p25_rx_id_format", "decimal")) : call?.talkgroup],
      ["Radio ID", call?.unit_id],
      ["Source", hit.digital?.source], ["Destination", hit.digital?.destination],
      [receiveCodeKind, receiveCodeKind === "NAC" && Number.isInteger(receiveCode)
        ? "0x" + receiveCode.toString(16).toUpperCase().padStart(3, "0") : receiveCode],
      ["SMS Destination", hit.sms?.target_id],
    ];
    return [
      ...metadata.filter(([label, value]) => label && value != null && value !== "")
        .map(([label, value]) => readout(label, String(value))),
      ...(hit.playable ? [run("PLAY", next => next.play("/api/discovery/hit/" + hit.id))] : []),
      ...(hit.recordable ? [run("REC", async next => {
        const result = await next.request("/api/discovery/hit/" + hit.id + "/record", {method: "POST"});
        next.notice(result.message ?? "Clip saved.");
      })] : []),
    ];
  });
}

function historyPage(ctx) {
  return page("history", "RX HISTORY", [
    run(ctx.radio.discovery_enabled ? "Pause Log" : "Resume Log", next => next.change({discovery: "toggle"})),
    run("RX History", async next => {
      const status = await next.request("/api/discovery");
      next.push(page("history-list", "RX HISTORY LIST", status.hits?.length
        ? status.hits.map(hit => run(hitLabel(hit), active => active.push(hitDetails(hit)), hitSummary(hit)))
        : [readout("No entries", "")]));
    }),
    run("DMR SMS", next => next.push(smsPage(next)), String(ctx.radio.dmr?.messages?.length ?? 0)),
  ]);
}

function smsPage(ctx) {
  const messages = ctx.radio.dmr?.messages ?? [];
  return page("sms", "DMR SMS", messages.length ? messages.map(message =>
    run(TIME_FORMAT.format(new Date(message.time_ms)) + " · " + message.source_id + " → " + message.target_id, next => {
      next.push({...page("sms-detail", "DMR SMS", [
        readout("Frequency", frequency(message.hz)),
        readout("Message", message.text ?? message.message ?? ""),
        run("Delete", active => active.push({
          ...confirmPage("sms-delete", "Delete SMS", "", async value => {
            await value.change({dmr: "delete_sms", id: message.id, time_ms: message.time_ms});
            value.back();
            value.back();
            value.replace(smsPage(value));
          }), description: "Delete this received message?",
        })),
      ]), description: "From " + message.source_id + " to " + message.target_id});
    })) : [readout("No messages", "")]);
}

function dmrMemoryPage(ctx) {
  const entries = memoryList(ctx).filter(entry => entry.mode === "DMR");
  return page("dmr-conventional", "DMR CONVENTIONAL", [
    run("Program Channel", next => next.push(page("dmr-write", "PROGRAM DMR", writeItems))),
    ...(ctx.v10.scan_kind === "MODE" ? [
      run("STOP", async next => {
        await next.change({v10: "scan", kind: "MODE", enabled: false});
        next.close();
      }),
    ] : [run("SCAN", async next => {
      await next.change({v10: "scan", kind: "MODE", enabled: true, mode: "DMR"});
      next.close();
    })]),
    ...entries.map(entry => run(memLabel(entry), next => next.push(memoryDetails(entry)), frequency(entry.hz))),
  ]);
}

function mapField(value, label, parse) {
  const pairs = value.split(",").filter(item => item.trim()).map(item => {
    const [key, ...tail] = item.split("=");
    const value = tail.join("=").trim();
    if (!/^\d+$/.test(key.trim()) || !value) throw new Error(label + " uses ID=value pairs separated by commas.");
    return [Number(key), parse(value)];
  });
  if (new Set(pairs.map(([id]) => id)).size !== pairs.length) throw new Error(label + " contains a repeated ID.");
  return Object.fromEntries(pairs);
}

function listField(values) {
  return Object.entries(values ?? {}).map(([id, value]) => id + "=" + value).join(", ");
}

function trunkEditor(ctx) {
  const profile = ctx.v10.trunk?.profile ?? {};
  return formPage("trunk-editor", "TIER III SITE SET", [
    field("Control Channel · MHz", "control", {value: asMHz(profile.control_hz ?? ctx.hz), inputMode: "decimal", required: true}),
    field("LCN Map · MHz", "channel_map", {
      value: listField(Object.fromEntries(Object.entries(profile.channel_map ?? {}).map(([id, hz]) => [id, asMHz(hz)]))),
      placeholder: "101=460.125, 102=460.225", required: false,
    }),
    field("Talkgroups · ID=Name", "talkgroups", {value: listField(profile.talkgroups), placeholder: "101=Dispatch, 103=Ops"}),
    field("Hold Talkgroup", "hold", {type: "number", min: 1, max: 16777215, value: profile.hold}),
    field("Blocked Talkgroups · IDs", "blocked", {value: (profile.blocked ?? []).join(", ")}),
    field("Talkgroup Filter", "allow_list", {options: [["off", "Allow any"], ["on", "Only listed"]], value: profile.allow_list ? "on" : "off"}),
  ], async (active, form) => {
    const blocked = getFormValue(form, "blocked");
    if (blocked && !/^\d+(\s*,\s*\d+)*$/.test(blocked)) throw new Error("Enter blocked talkgroup IDs separated by commas.");
    const config = {
      control_hz: frequencyValue(getFormValue(form, "control"), "Control channel"),
      channel_map: mapField(getFormValue(form, "channel_map"), "LCN Map", input => frequencyValue(input, "Mapped channel")),
      talkgroups: mapField(getFormValue(form, "talkgroups"), "Talkgroups", input => input),
      hold: Number(getFormValue(form, "hold")) || null,
      blocked: blocked ? blocked.split(",").map(Number) : [],
      allow_list: getFormValue(form, "allow_list") === "on",
    };
    if (!Object.keys(config.channel_map).length || Object.keys(config.channel_map).some(id => Number(id) < 1 || Number(id) > 4094)) {
      throw new Error("Enter at least one logical channel number from 1 to 4094.");
    }
    if (!Object.values(config.channel_map).includes(config.control_hz)) {
      throw new Error("Include the control channel frequency in the LCN map.");
    }
    if ([...Object.keys(config.talkgroups).map(Number), ...config.blocked].some(id => id < 1 || id > 16777215)) {
      throw new Error("Talkgroup IDs must be from 1 to 16777215.");
    }
    await active.change({v10: "trunk", operation: "save", profile: config});
    active.back();
  }, "SET");
}

function trunkPage(ctx) {
  const trunk = ctx.v10.trunk ?? {};
  return page("tier-iii", "DMR TIER III", [
    run("Site Set", next => next.push(trunkEditor(next)), frequency(trunk.profile?.control_hz)),
    trunk.enabled
      ? run("STOP", next => next.change({v10: "trunk", operation: "stop"}), trunk.phase ?? "")
      : run("START", next => trunk.profile
        ? next.change({v10: "trunk", operation: "start"})
        : next.push(trunkEditor(next))),
    readout("Control Channel", frequency(trunk.cc_freq_hz)),
    readout("Traffic Channel", frequency(trunk.vc_freq_hz)),
    ...(trunk.error ? [readout("Decoder", trunk.error)] : []),
  ]);
}

function decodePage(ctx) {
  return ctx.mode === "DMR" ? page("decode", "DECODE · DMR", [
    run("Conventional", next => next.push(dmrMemoryPage(next))),
    run("Tier III", next => next.push(trunkPage(next)), ctx.v10.trunk?.enabled ? "ON" : ""),
    run("DMR SMS", next => next.push(smsPage(next)), String(ctx.radio.dmr?.messages?.length ?? 0)),
    run("D.SQL", next => next.push(digitalSquelchPage(next))),
  ]) : {
    ...page("fsk-decode", "FSK DECODE", next => [
      run("HOLD", active => active.freezeFsk(), next.fskHeld ? "HOLD" : "LIVE"),
      run("CLR", active => active.clearFsk()),
      run("TIME", active => active.stampFsk()),
      FSK_OPTIONS[3],
      page("decode-set", "FSK Decode Set", FSK_OPTIONS.slice(4)),
    ]),
    output: true,
  };
}

function vfoMemoryPage(ctx) {
  const noMemory = !memoryList(ctx).length;
  return page("vfo-memory", "VFO / MEMORY", [
    run("VFO", async next => {
      await next.change({v10: "frequency_mode", value: "vfo"});
      next.close();
    }),
    noMemory ? null : run("MEMORY", async next => {
        await next.change({v10: "frequency_mode", value: "memory"});
        next.close();
      }),
    run("GROUP", next => next.push(makeGroupPage(next)), groupLabel(ctx.v10.group ?? 0)),
    run("MW", next => next.push(page("write", "MEMORY WRITE", writeItems))),
    run("CH-INP", next => next.push(channelInput(next.v10.group ?? 0))),
    ...(currentMemory(ctx) ? [run("Edit Selected Channel", next => next.push(memoryDetails(currentMemory(next))))] : []),
  ]);
}

const DEFAULT_QUICK_ITEMS = ["group", "meter", "record", "mirror", "tier-iii"];

function quickMenuItems(ctx) {
  return [
    ["group", "Group Select", run("Group Select", next => next.push(makeGroupPage(next)))],
    ["meter", "Meter Type", choice("meter_type", "Meter Type", "dbfs",
      [["relative", "S · Relative"], ["dbfs", "dBFS"]], "client")],
    ["record", "Start / Stop Recording", run(ctx.radio.record_enabled ? "«REC Stop»" : "«REC Start»", async next => {
      await next.change({record: "toggle"});
      next.close();
    })],
    ["mirror", "Second Display", run("Second Display", next => next.mirror())],
    ["tier-iii", "DMR Tier III", ctx.mode === "DMR" ? run("DMR Tier III", next => next.push(trunkPage(next))) : null],
    ["mode", "Mode", run("Mode", next => next.selectMode())],
    ["tuning-step", "Tuning Step", run("Tuning Step", next => next.push(tuningStepPage(next)))],
    ["filter", "Receive Filter", hasAnalogFilter(ctx.mode) && supports(ctx, "bandwidth_hz")
      ? run("Receive Filter", next => next.push(filterPage(next))) : null],
    ["scan-start", "Scan Start", run("Scan Start", next => next.push(makeScanPage(next, true)))],
    ["scan-settings", "Scan Settings", run("Scan Settings", next => next.push(menuRoute("scan-set", next)))],
    ["priority", "Priority Scan", run("Priority Scan", next => next.push(priorityPage(next)))],
    ["skip", "Temporary Skip", ctx.v10.scan_paused && ctx.v10.settings?.skip_function
      ? run("Temporary Skip", async next => { await next.change({v10: "skip"}); next.close(); }) : null],
    ["memory", "Memory", run("Memory", next => next.push(memoryGroupPage(next)))],
    ["scope", "Scope Settings", run("Scope Settings", next => next.push(menuRoute("scope-set", next)))],
    ["volume", "AF Volume", DIAL_B_VOLUME],
    ["boost", "Audio Boost", AUDIO_BOOST],
    ["squelch", "Squelch", SQUELCH],
    ["rf-gain", "RF Gain", rfNumber(ctx, "rf_gain", "RF Gain", 100, 0, 100, 1, "%")],
    ["history", "RX History", run("RX History", next => next.push(historyPage(next)))],
    ["replay", "Instant Replay", run("Instant Replay", async next => { await next.replay(); next.close(); })],
  ];
}

function selectedQuickItems(ctx) {
  const saved = ctx.getPreference("quick_menu", DEFAULT_QUICK_ITEMS);
  return new Set(Array.isArray(saved) ? saved : DEFAULT_QUICK_ITEMS);
}

function quickMenuSettingItems(ctx) {
  const selected = selectedQuickItems(ctx);
  return [
    ...quickMenuItems(ctx).map(([key, label]) => run(label, next => {
      const items = selectedQuickItems(next);
      if (items.has(key)) items.delete(key);
      else items.add(key);
      next.setPreference("quick_menu", [...items]);
    }, selected.has(key) ? "ON" : "OFF")),
    run("Restore Default Shortcuts", next => next.setPreference("quick_menu", DEFAULT_QUICK_ITEMS)),
  ];
}

function quickPage(ctx, highlighted) {
  if (highlighted?.kind === "setting") return page("quick-setting", "QUICK MENU", [
    run("Default", async next => {
      await next.set(highlighted, highlighted.initial);
      next.back();
    }, displayValue(highlighted, highlighted.initial)),
  ]);
  const selected = selectedQuickItems(ctx);
  const items = quickMenuItems(ctx).flatMap(([key, , item]) => selected.has(key) && item ? [item] : []);
  return page("quick", "QUICK MENU", items.length ? items : [readout("Choose shortcuts in MENU › SET › Quick Menu")]);
}

function timerPage(ctx) {
  const timer = ctx.v10.timer ?? {};
  return page("timer", "TIMER", [
    ...(ctx.radio.power ? [number("sleep_minutes", "Sleep Timer", 0, 0, 120, 5, "min")] : []),
    run("TIMER1", next => next.push(timerEditor(next)), timer.enabled ? "ON" : "OFF"),
  ]);
}

function timerEditor(ctx) {
  const timer = ctx.v10.timer ?? {};
  const memories = memoryList(ctx);
  const memoryKey = entry => entry.group + "/" + entry.slot;
  const chosen = memories.find(entry => entry.group === timer.group && entry.slot === timer.slot)
    ?? currentMemory(ctx) ?? memories[0];
  const chosenMemory = chosen ? memoryKey(chosen) : "";
  const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const yesNo = [["off", "OFF"], ["on", "ON"]];
  return formPage("timer-editor", "TIMER1", [
    field("Timer Status", "enabled", {options: yesNo, value: timer.enabled ? "on" : "off"}),
    field("Reservation Timer", "on", {type: "time", value: timer.on}),
    field("Power OFF Timer", "off", {type: "time", value: timer.off}),
    field("Reservation Channel", "memory", {options: memories.length
      ? memories.map(entry => [memoryKey(entry), memLabel(entry)]) : [["", "No stored channels"]], value: chosenMemory}),
    field("Day of the Week", "weekday", {options: [["", "Every Day"], ...weekdays.map((label, n) => [n, label])], value: timer.weekday ?? ""}),
    field("Repeat Setting", "repeat", {options: yesNo, value: timer.repeat ? "on" : "off"}),
  ], (active, form) => {
    const config = {
      v10: "timer", enabled: getFormValue(form, "enabled") === "on",
      on: getFormValue(form, "on") || null, off: getFormValue(form, "off") || null,
      weekday: getFormValue(form, "weekday") === "" ? null : Number(getFormValue(form, "weekday")),
      repeat: getFormValue(form, "repeat") === "on",
      group: 0, slot: 0,
    };
    const memory = getFormValue(form, "memory");
    if (memory) [config.group, config.slot] = memory.split("/").map(Number);
    if (config.enabled && !memory) throw new Error("Write a memory channel before setting a reservation.");
    if ((config.enabled || config.off) && !config.on) throw new Error("Choose a power-on time.");
    if (config.off && config.off <= config.on) throw new Error("Choose a power-off time after the power-on time.");
    active.push(confirmPage("timer-confirm", "Set Timer?", "", async next => {
      await next.change(config);
      next.back();
      next.back();
    }));
  }, "SET");
}

async function saveBackup(ctx) {
  const data = await ctx.request("/api/v10/backup");
  const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = "sdr-scanner-settings-" + new Date().toISOString().slice(0, 10) + ".json";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function restorePage() {
  return formPage("restore-settings", "LOAD SETTING", [
    field("Settings Backup", "backup", {type: "file", accept: ".json,application/json", required: true}),
  ], async (ctx, form) => {
    const [file] = form.elements.namedItem("backup").files;
    if (!file || file.size > 5 * 1024 * 1024) throw new Error("Choose a settings backup no larger than 5 MB.");
    let backup;
    try {
      backup = JSON.parse(await file.text());
    } catch {
      throw new Error("Choose a valid SDr Scanner settings backup.");
    }
    if (!backup || Array.isArray(backup) || typeof backup !== "object") throw new Error("Choose a valid SDr Scanner settings backup.");
    ctx.push(confirmPage("restore-confirm", "Load Setting?", "Replace current receiver settings and memories?", async next => {
      await next.change({v10: "restore", backup});
      next.close();
    }));
  }, "LOAD");
}

function tuningStepPage(ctx) {
  const current = ctx.tuningStep;
  const route = choicesPage("tune-step", "TUNING STEP", stepOptions(current), current, async (next, value) => {
    await next.change({v10: "setting", key: "tune_step", value});
    next.close();
  });
  const custom = current >= 100 && current <= 999900 && current % 100 === 0 && !TUNING_STEPS.includes(current);
  route.items.push(formPage("programmable-step", "PROGRAMMABLE TS", [
    field("Step · kHz", "step", {
      type: "number", min: 0.1, max: 999.9, step: 0.1, value: custom ? current / 1000 : 50, required: true,
    }),
  ], async (next, form) => {
    await next.change({v10: "setting", key: "tune_step", value: Math.round(Number(getFormValue(form, "step")) * 1000)});
    next.close();
  }, "SET"));
  return route;
}

function menuRoute(id, ctx) {
  switch (id) {
    case "menu": {
      const showDecode = ctx.radio.power && (ctx.mode === "DMR" || ctx.mode.startsWith("FSK"));
      return {...page(id, "MENU", ctx.radio.power ? [
        run("SCOPE", next => next.scope()),
        run("SCAN", next => next.push(makeScanPage(next, false))),
        run("MEMORY", next => next.push(memoryGroupPage(next))),
        run("RECORD", next => next.push(recorderPage(next))),
        run("HISTORY", next => next.push(historyPage(next))),
        SET_MENU,
        showDecode ? run("DECODE", next => next.push(decodePage(next))) : null,
      ] : [SET_MENU]), columns: showDecode ? 4 : 3};
    }
    case "scan-start": return makeScanPage(ctx, true);
    case "scan": return makeScanPage(ctx, false);
    case "scan-set": return page(id, "SCAN SET", SCAN_SETTING);
    case "scan-speed": return settingPage(ctx, SCAN_SPEED);
    case "prio": return priorityPage(ctx);
    case "memory": return memoryGroupPage(ctx);
    case "group": return makeGroupPage(ctx);
    case "vfo-memory": return vfoMemoryPage(ctx);
    case "program":
    case "write": return ctx.mode === "DMR" && id === "program" ? dmrMemoryPage(ctx) : page(id, "MEMORY WRITE", writeItems);
    case "function": return functionPage(ctx);
    case "filter": return filterPage(ctx);
    case "dial-a":
    case "dial-b":
    case "dial-c": return dialPage(ctx, id);
    case "dial-d":
    case "dial-e": return dialAssignmentPage(ctx, id);
    case "timer": return timerPage(ctx);
    case "tune-step":
    case "ts": return tuningStepPage(ctx);
    case "quick": return quickPage(ctx);
    case "record": return recorderPage(ctx);
    case "history":
    case "discovery": return historyPage(ctx);
    case "scope-set": return page(id, "SCOPE SET", [
      ...SCOPE_SETTINGS,
      number("scope_reference", "REF", 0, -20, 20, 0.5, "dB", "client"),
    ]);
    case "set": return SET_MENU;
    case "decode": return decodePage(ctx);
    case "dmr":
    case "conventional": return dmrMemoryPage(ctx);
    case "trunk":
    case "tier-iii": return trunkPage(ctx);
    case "tone": return tonePage(ctx);
    case "sms": return smsPage(ctx);
    default: return page(id, id.toUpperCase(), []);
  }
}

function resetConfirm(scope) {
  const description = scope === "all"
    ? "Clear stored receiver memories and settings?" : "Restore receiver settings to their defaults?";
  return confirmPage("reset-confirm", "RESET", description, async active => {
    await active.change({v10: "reset", scope});
    active.close();
  });
}

function settingValue(ctx, option) {
  if (option.target === "client") return ctx.getPreference(option.key, option.initial);
  if (option.target === "knob") return Math.round((ctx.radio[option.knob] ?? 0) * 100);
  if (option.mode) return ctx.v10.settings_by_mode?.[option.mode]?.[option.key] ?? option.initial;
  return ctx.v10.settings?.[option.key] ?? option.initial;
}

const DIGITS = "1234567890";
const LETTER_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];
const SYMBOLS = ".,:;=/\\-_+!@#$%&*()[]{}'\"<>?~^|`";

function timeValue(value) {
  if (!value) return value;
  const separated = value.match(/^(\d{1,2}):(\d{2})$/);
  const compact = value.match(/^(\d{1,2})(\d{2})$/);
  const parts = separated || compact;
  if (!parts || Number(parts[1]) > 23 || Number(parts[2]) > 59) {
    throw new Error("Enter a time from 00:00 to 23:59.");
  }
  return parts[1].padStart(2, "0") + ":" + parts[2];
}

function createLcdKeyboard({label, value, inputSpec = {}, onAccept}) {
  const time = inputSpec.type === "time";
  const hexadecimal = inputSpec.inputMode === "hex";
  const numeric = time || hexadecimal || inputSpec.type === "number"
    || ["numeric", "decimal"].includes(inputSpec.inputMode);
  const decimal = !time && !hexadecimal && inputSpec.inputMode !== "numeric"
    && (inputSpec.inputMode === "decimal" || inputSpec.step === "any" || Number(inputSpec.step ?? 1) % 1 !== 0);
  const signed = !time && !hexadecimal && Number(inputSpec.min ?? (inputSpec.type === "number" ? -1 : 0)) < 0;
  const extraKeys = time ? ":" : hexadecimal ? "ABCDEF" : `${decimal ? "." : ""}${signed ? "-" : ""}`;
  let draft = Array.from(String(value ?? ""));
  let cursor = draft.length;
  let capitals = false;
  let symbols = false;

  function insert(char) {
    const characters = Array.from(char);
    if (inputSpec.maxLength != null && draft.length + characters.length > inputSpec.maxLength) return;
    draft.splice(cursor, 0, ...characters);
    cursor += characters.length;
  }

  function erase() {
    if (cursor > 0) draft.splice(--cursor, 1);
  }

  function moveCursor(delta) {
    cursor = Math.max(0, Math.min(draft.length, cursor + delta));
  }

  function clear() {
    draft = [];
    cursor = 0;
  }

  function accept(ctx) {
    const text = draft.join("");
    return onAccept(time ? timeValue(text) : text, ctx);
  }

  const character = char => run(char, () => insert(char));
  const caseKey = () => run(capitals ? "abc" : "ABC", () => { capitals = !capitals; });
  const symbolsKey = () => run(symbols ? "ABC" : "SYM", () => { symbols = !symbols; });

  return {
    kind: "page",
    id: "lcd-keyboard",
    label,
    columns: numeric ? 4 : 10,
    get entry() { return [draft.slice(0, cursor).join(""), draft.slice(cursor).join("")]; },
    items() {
      const clearKey = run(numeric ? "CE" : "CLR", clear);
      const enter = run("ENT", accept);
      if (numeric) return [
        ...[..."123"].map(character), run("⌫", erase),
        ...[..."456"].map(character), clearKey,
        ...[..."789"].map(character), enter,
        ...[...`0${extraKeys}`].map(character),
      ];
      const digits = [...DIGITS].map(character);
      const space = run("SPACE", () => insert(" "));
      const left = {...run("←", () => moveCursor(-1)), accessibleName: "Move cursor left"};
      const right = {...run("→", () => moveCursor(1)), accessibleName: "Move cursor right"};
      if (symbols) return [...digits, ...[...SYMBOLS].map(character), symbolsKey(), space, left, right, clearKey, enter];
      const [top, middle, bottom] = LETTER_ROWS.map(row => [...(capitals ? row.toUpperCase() : row)].map(character));
      return [
        ...digits, ...top, ...middle, clearKey,
        ...bottom, caseKey(), space, left, right, symbolsKey(), enter,
      ];
    },
    handleKey(key) {
      if (key === "Enter") return accept;
      if (key === "Backspace" || key === "Delete") return erase;
      if (!numeric && key === "ArrowLeft") return () => moveCursor(-1);
      if (!numeric && key === "ArrowRight") return () => moveCursor(1);
      if (typeof key !== "string" || Array.from(key).length !== 1) return false;
      const char = hexadecimal ? key.toUpperCase() : key;
      if (numeric && !`${DIGITS}${extraKeys}`.includes(char)) return false;
      return () => insert(char);
    },
  };
}

function settingPage(ctx, option) {
  const current = settingValue(ctx, option);
  if (option.options) {
    return {...choicesPage("edit-setting", option.label, option.options, current, async (next, value) => {
      await next.set(option, value);
      next.back();
    }), option};
  }
  return {...formPage("edit-setting", option.label, [
    field(option.unit ? option.label + " · " + option.unit : option.label, option.key, {
      type: "number", min: option.min, max: option.max, step: option.step, required: true, value: current,
    }),
  ], async (next, form) => {
    const value = Number(getFormValue(form, option.key));
    if (!Number.isFinite(value)) throw new Error("Choose a valid setting.");
    await next.set(option, value);
    next.back();
  }, "SET"), option};
}

function choicesPage(id, label, options, current, choose) {
  const selected = options.findIndex(([value]) => String(value) === String(current));
  return {...page(id, label, options.map(([value, name], index) =>
    run(name, next => choose(next, value), index === selected ? "✓" : ""))),
    selected: Math.max(0, selected), isChoice: true};
}

function rowTitle(row, ctx) {
  if (row.kind === "setting") return displayValue(row, settingValue(ctx, row));
  return typeof row.value === "function" ? row.value(ctx) : row.value;
}

const settingHelp = {
  agc: "Set how quickly receiver gain responds to changes in signal strength.",
  audio_boost: "Raise live listening audio on this device without changing AF volume.",
  audio_peak: "Use a narrow audio filter to make a CW tone easier to hear.",
  auto_memory_clear: "Choose whether starting Auto Memory Write Scan clears its saved hits.",
  bandwidth_hz: "Set how wide the signal filter is around the tuned frequency.",
  beep_confirmation: "Turn the confirmation sound for key presses on or off.",
  beep_level: "Adjust the level of the confirmation sound.",
  cw_pitch: "Set the audio pitch of received CW signals.",
  dial_auto_ts: "Choose how much tuning accelerates when you turn the main dial quickly.",
  display_memory: "Show the stored channel name on the operating screen.",
  display_rds: "Show the station name and RadioText on FM broadcasts in this browser.",
  dmr_squelch: "Choose which types of digital calls open the squelch.",
  fm_deemphasis: "Reduce the boosted high frequencies used in FM transmissions.",
  fsk_baud: "Set the symbol rate for decoding RTTY.",
  fsk_center: "Choose whether the displayed frequency refers to the tone centre or the mark tone.",
  fsk_newline: "Choose which decoded characters start a new line in the RTTY display.",
  fsk_pitch: "Set the mark-tone frequency for RTTY decoding.",
  fsk_shift: "Set the separation between the mark and space tones.",
  fsk_tpf: "Use a twin-peak filter to isolate both RTTY tones.",
  fsk_usos: "Return to letters after a space when decoding Baudot text.",
  if_shape: "Choose how sharply the receive filter cuts off beyond its passband.",
  lcd_backlight: "Adjust the brightness of the receiver display in this browser.",
  lcd_type: "Choose the LCD colour scheme.",
  led_bright: "Adjust the brightness of the front-panel indicator lamps.",
  meter_type: "Choose between a signal-strength scale and the dBFS reading.",
  mute_button_dim: "Dim this browser’s receiver LCD to 10% brightness while the VOLUME knob is muted.",
  nb_level: "Adjust how strongly the noise blanker suppresses brief pulses.",
  noise_blanker: "Suppress short bursts of impulse noise in the selected mode.",
  noise_reduction: "Reduce steady background noise in the selected mode.",
  notch: "Set the filter frequency used to suppress an unwanted steady tone.",
  nr_level: "Adjust how strongly the noise reduction acts.",
  p25_rx_id_format: "Show P25 receive IDs in decimal or hexadecimal.",
  pause_time: "Set how long scanning pauses on an active signal.",
  pbt1: "Shift one edge of the receive passband to reduce interference.",
  pbt2: "Shift the other edge of the receive passband.",
  priority_interval: "Set how often the receiver checks the priority channel.",
  resume_time: "Set how long the radio waits before resuming after a signal ends.",
  rf_gain: "Adjust receiver input gain before demodulation.",
  scan_dial: "Choose whether turning the main dial changes scan direction or stops scanning.",
  scan_resume: "Choose whether scanning resumes automatically after it pauses.",
  scan_screen: "Show the scan controls automatically when a scan starts.",
  scan_speed: "Set how quickly the receiver checks quiet frequencies during a scan.",
  scope_averaging: "Average successive spectrum traces for a steadier display.",
  scope_max_hold: "Set how long the scope keeps peak levels visible.",
  scope_reference: "Shift the signal level range shown by the spectrum and waterfall.",
  scope_waterfall: "Show or hide signal history beneath the spectrum trace.",
  scope_waterfall_speed: "Choose how quickly new lines move through the waterfall.",
  screen_capture: "Let a short press of POWER save an image of the LCD.",
  skip_function: "Allow temporary skipping of a channel that has paused a scan.",
  skip_timer: "Set when temporarily skipped frequencies are scanned again.",
  sleep_minutes: "Choose when the receiver switches off automatically.",
  speech_language: "Choose the language used for spoken receiver information.",
  speech_level: "Adjust the level of spoken receiver information.",
  speech_lock_switch: "Choose whether tapping SPEECH/LOCK speaks or toggles the lock.",
  speech_signal: "Include the signal reading in the spoken announcement.",
  speech_speed: "Choose the speed of spoken announcements.",
  squelch: "Set the signal level needed to open the audio gate.",
  tone_bass: "Raise or lower the bass of the received audio.",
  tone_hpf: "Set the low-frequency cutoff of the audio filter.",
  tone_lpf: "Set the high-frequency cutoff of the audio filter.",
  tone_treble: "Raise or lower the treble of the received audio.",
  tune_step: "Set how far the receiver moves with each tuning step.",
  volume: "Adjust received audio volume on this device.",
};

const fieldDescriptions = {
  allow_list: "Choose whether to hear any talkgroup or only those listed for this site.",
  auto_clear: "Choose whether to clear previous automatically written memories before starting.",
  backup: "Choose a saved receiver settings file to load.",
  blocked: "Enter talkgroup IDs to ignore, separated by commas.",
  center_hz: "Enter the centre frequency of the delta scan in MHz.",
  channel_map: "Pair each logical channel number with its frequency in MHz.",
  color_code: "Limit DMR reception to a colour code from 0 to 15.",
  control: "Enter the DMR site's control-channel frequency in MHz.",
  edge: "Choose a saved pair of scan limits.",
  end_hz: "Enter the upper frequency limit in MHz.",
  end_slot: "Enter the last memory channel to scan.",
  group: "Choose the bank of memory channels.",
  hold: "Enter a talkgroup ID to follow exclusively.",
  hz: "Enter the receive frequency in MHz.",
  memory: "Choose the saved channel this function uses.",
  mode: "Choose the modulation used to receive the signal.",
  name: "Enter a label for the stored channel.",
  off: "Choose when the receiver should power off.",
  on: "Choose when the reservation timer should switch the receiver on.",
  path: "Enter a recording folder on the computer running the receiver.",
  repeat: "Choose whether to repeat the reservation after it runs.",
  select_tag: "Choose the memory tag used for select scans.",
  selected: "Assign the memory tag used for select scans.",
  slot: "Enter the memory channel number within the selected group.",
  span_hz: "Choose the total frequency range around the scan centre.",
  start_hz: "Enter the lower frequency limit in MHz.",
  start_slot: "Enter the first memory channel to scan.",
  step: "Set the frequency increment between successive channels.",
  talkgroup: "Limit DMR reception to one talkgroup ID.",
  talkgroups: "Enter talkgroup IDs and their display names.",
  timeslot: "Limit DMR reception to slot 1 or slot 2.",
  tone: "Enter the CTCSS tone or DCS code that must match to open audio.",
  weekday: "Choose the day of the week on which the timer runs.",
};

function fieldHelp(spec) {
  return fieldDescriptions[spec.name] ?? settingHelp[spec.name];
}

const menuActionsHelp = {
  "All Reset": "Open confirmation to clear stored memories and receiver settings.",
  "CH-INP": "Enter a memory channel number to recall directly.",
  "Conventional": "Manage and scan conventional DMR channels.",
  "Conventional Memory": "Open the conventional DMR memory list.",
  "D.SQL": "Set how digital calls open the audio gate.",
  "DECODE": "Open decoder controls for the current receive mode.",
  "DMR SMS": "View received DMR text messages.",
  "DMR Tier III": "Set up or follow a DMR Tier III trunked site.",
  "Default": "Restore the selected setting to its default value.",
  "Delete": "Open confirmation to delete this received message.",
  "Edit Name": "Change the saved channel's display name.",
  "Edit Selected Channel": "Open the current channel's memory settings.",
  "FIL": "Choose a receive filter preset for the current mode.",
  "FINE": "Toggle fine tuning while scanning a frequency range.",
  "GROUP": "Choose which memory group to browse.",
  "Group Select": "Choose which memory group to browse.",
  "HISTORY": "Browse recently received signals and recorded hits.",
  "HOLD": "Pause or resume the live decoded text display.",
  "History Logging": "Toggle logging of received signals.",
  "Instant Replay": "Play the latest audio from the receiver's replay buffer.",
  "Load Setting": "Select a backup to restore receiver settings and memories.",
  "LOWER EDGE": "Return to the lower frequency limit of the current scan.",
  "MEMORY": "Browse saved channels or switch to the current memory channel.",
  "MW": "Open the memory writing controls.",
  "Memory": "Browse saved channels in the current group.",
  "Memory Clear": "Open confirmation to delete this saved channel.",
  "Memory Copy": "Open an editor to copy this channel to another memory.",
  "Memory Write": "Open an editor to write the current tuning to this memory.",
  "Mode": "Choose the receive modulation.",
  "NO": "Cancel and return without applying this operation.",
  "P": "Edit pairs of frequency limits used for programmed scans.",
  "PBT CLEAR": "Return both passband tuning offsets to zero.",
  "PIN": "Set or change the six-digit PIN for remote access.",
  "PLAY": "Play the audio captured for this history entry.",
  "PRIO": "Choose a channel to check at regular intervals while listening.",
  "Partial Reset": "Open confirmation to restore receiver settings to their defaults.",
  "Pause Log": "Pause collection of new receiver history entries.",
  "Play Files": "Find the most recent saved recording for playback.",
  "Priority Scan": "Set up regular checks of a priority memory channel.",
  "Program Channel": "Open the editor for a new or existing DMR memory channel.",
  "REC": "Save the captured history audio as a recording.",
  "RECALL": "Stop the scan and return to the tuning used before it started.",
  "RECORD": "Open recording, playback and recording-folder controls.",
  "RX History": "Open the list of recorded receive events.",
  "Recall Channel": "Tune to this saved channel and apply its receive settings.",
  "Receive Filter": "Choose the filter for the current receive mode.",
  "Restore Default Shortcuts": "Reset the list of shortcuts shown under QUICK.",
  "Resume Log": "Continue collecting new receiver history entries.",
  "SCAN": "Open scan options or start scanning the relevant memories.",
  "SCOPE": "Open the live spectrum and waterfall display.",
  "SELECT": "Choose the memory tag used for select scans.",
  "SET": "Open the settings for this function.",
  "SKIP": "Exclude the current frequency or memory from scanning.",
  "START": "Start the operation with the chosen settings.",
  "STOP": "Stop the current operation.",
  "Save Setting": "Download a backup of receiver settings and memories.",
  "Scan Settings": "Adjust scan timing, speed and temporary skip behaviour.",
  "Scan Start": "Choose a scan type and start it.",
  "Scope Settings": "Adjust the spectrum and waterfall display.",
  "Second Display": "Open a separate window that mirrors the receiver display.",
  "Select the Channel and Write": "Choose the memory group and channel to write.",
  "Site Set": "Edit the control frequency, channel map and filters for this site.",
  "T-SCAN": "Look for the CTCSS tone or DCS code in a received FM signal.",
  "T-SQL / DTCS": "Set a CTCSS tone or DCS code for receive squelch.",
  "TIME": "Add the current time to the decoded text display.",
  "TIMER1": "Set when the receiver switches on or off.",
  "TONE": "Open tone search and coded squelch controls for FM reception.",
  "Temporary Skip": "Skip the frequency that has paused the current scan until the timer expires.",
  "Tier III": "Follow the channels assigned by a DMR Tier III control site.",
  "Tuning Step": "Choose how far the frequency moves with each tuning step.",
  "Use Detected Tone": "Save the detected CTCSS tone or DCS code to this channel.",
  "VFO": "Leave memory recall and return to freely tuned operation.",
  "Write to a New Channel": "Open the editor with an empty memory channel selected.",
  "Write to the Selected Channel": "Open an editor to update the selected memory.",
  "YES": "Confirm and apply this operation.",
  "ΔF SPAN": "Restart the delta sweep from the lower edge of its range.",
  "«REC Start»": "Arm recording for audio that opens the squelch.",
  "«REC Stop»": "Disarm recording and finish any current recording.",
};

const menuPageHelp = {
  dials: "Choose the everyday function assigned to each dial.",
  display: "Adjust what appears on the receiver display and how it looks.",
  "digital-settings": "Adjust settings for digital reception and history logging.",
  "dsp-noise-blanker": "Configure impulse-noise suppression.",
  "dsp-noise-reduction": "Configure reduction of steady background noise.",
  "function-settings": "Set confirmation sounds, speech, panel lock and power-key behaviour.",
  "quick-menu-settings": "Choose which shortcuts appear under QUICK.",
  reset: "Choose which settings or memories to restore or clear.",
  set: "Open receiver settings for display, sound, dials and shortcuts.",
  "settings-backup": "Save or restore a copy of your receiver settings and memories.",
  "speech-settings": "Adjust what the receiver announces and how it sounds.",
  tones: "Adjust receive audio tone and filtering by mode.",
};

function keypadHelp(label) {
  return {
    "←": "Move the editing cursor left.",
    "→": "Move the editing cursor right.",
    "⌫": "Delete the character before the cursor.",
    CE: "Clear the current entry.",
    CLR: "Clear the current entry.",
    ENT: "Accept the entry and return to the editor.",
    SPACE: "Insert a space.",
    SYM: "Switch to symbols.",
    ABC: "Switch to letters or upper case.",
    abc: "Switch to lower case.",
  }[label] ?? `Insert ${label} at the cursor.`;
}

function menuHelp(item, route) {
  const {kind, label} = item;
  if (route.id === "lcd-keyboard") return keypadHelp(label);
  if (kind === "setting") return settingHelp[item.key] ?? `Adjust ${label}.`;
  if (kind === "page") return menuPageHelp[item.id] ?? `Open ${label}.`;
  if (route.id === "quick-menu-settings" && label !== "Restore Default Shortcuts") {
    return `Show or hide ${label} under QUICK.`;
  }
  if (route.id === "dials") return `Choose what ${label} does when menus are closed.`;
  if (route.id === "dial-assignment") return label === "Sensitivity"
    ? `Choose how much ${route.label} changes its assigned setting when turned.` : `Assign ${label} to ${route.label}.`;
  if (route.id === "tune-step" && route.isChoice) return `Use a tuning step of ${label}.`;
  if (route.isChoice) return `Choose ${label} for ${route.label}.`;
  if (route.id === "scan-start" || route.id === "scan") {
    const scan = SCANS.find(([, key]) => key === label);
    if (scan) return route.id === "scan-start" ? `Start ${scan[2]}.` : `Set up ${scan[2]}.`;
  }
  if (route.id === "fsk-decode" && label === "CLR") return "Clear all decoded text.";
  if (route.id === "group" && label !== "P") return "Browse channels saved in this memory group.";
  if (route.id === "history-list") return "View this receive event's details and available audio.";
  if (route.id === "sms") return "Read this received message.";
  if (route.id === "record-play") return label === "STOP" ? "Stop playing the recording." : "Play the latest saved recording.";
  if (menuActionsHelp[label]) return menuActionsHelp[label];
  if (["memory-group", "dmr-conventional"].includes(route.id)) return "Open this channel's memory details.";
  return `Select ${label}.`;
}

const editorSubmitHelp = {
  ENT: "Accept the entered values.",
  LOAD: "Check the selected backup before restoring it.",
  MW: "Write the configured frequency and settings to memory.",
  SET: "Apply the displayed settings.",
  START: "Start using the settings in this editor.",
};

export function createReceiverMenus({
  root, getState, act, onMirror, onScope, onMode, onClose, onOpen, onScopeSettings,
  getAccess, onPinSetup, onDialAction,
  request = requestJson, onPlay, onReplay, onStopPlayback, onRender,
  getPreference, setPreference, getTuningStep,
}) {
  if (!root) throw new Error("The receiver LCD menu mount is missing.");
  // Persist client-only changes in this page when a host panel does not supply storage.
  const local = new Map();
  const read = getPreference ?? ((key, fallback) => local.has(key) ? local.get(key) : fallback);
  const write = setPreference ?? ((key, value) => local.set(key, value));
  const stack = [];
  let selected = 0;
  let busy = false;
  let error = "";
  let rows = [];
  let rowNodes = [];
  let formNode = null;
  let formCursor = null;
  let submitKey = null;
  let keypadKey = null;
  let scrollPane = null;
  let renderedMode = null;
  let fskHeld = false;
  let fskDisplay = "";
  let fskCleared = "";
  let fskStamp = "";

  const current = () => stack.at(-1);
  const getV10 = () => getState()?.v10 ?? {};
  const isoContext = () => {
    const radio = getState() ?? {};
    const v10 = radio.v10 ?? {};
    const mode = displayMode(v10.mode ?? (radio.dmr?.enabled ? "DMR" : radio.modulation ?? v10.vfo?.mode ?? "FM"));
    return {
      radio, v10, mode, access: getAccess?.() ?? {}, setupPin: onPinSetup,
      hz: v10.hz ?? radio.tune_hz ?? (mode === "DMR" ? radio.dmr?.hz : v10.vfo?.hz),
      tuningStep: getTuningStep?.() ?? v10.settings?.tune_step ?? 12500,
      getState, getV10, change, close, push, replace, back, notice,
      getPreference: read, setPreference: write, set,
      request,
      fskHeld,
      freezeFsk: () => {
        update();
        fskHeld = !fskHeld;
      },
      clearFsk: () => {
        fskCleared = getV10().fsk_text ?? "";
        fskDisplay = fskStamp = "";
        fskHeld = false;
      },
      stampFsk: () => {
        fskStamp = [fskStamp, TIME_FORMAT.format(new Date())].filter(Boolean).join("\n");
        fskDisplay = [fskDisplay, fskStamp.split("\n").at(-1)].filter(Boolean).join("\n");
      },
      mirror: () => {
        onMirror?.();
        close();
      },
      scope: () => {
        close();
        onScope?.();
      },
      selectMode: () => {
        close();
        onMode?.();
      },
      play: path => onPlay ? onPlay(path) : Promise.reject(new Error("Audio playback is unavailable.")),
      replay: () => onReplay ? onReplay() : Promise.reject(new Error("Instant replay is unavailable.")),
      stopPlayback: () => onStopPlayback?.(),
    };
  };

  function change(action) {
    return act(action);
  }

  async function set(option, value) {
    if (option.target === "client") {
      write(option.key, value);
      if (option.key.startsWith("scope_")) onScopeSettings?.(option.key, value);
    } else if (option.target === "knob") {
      await change({knob: option.knob, value: value / 100});
    } else {
      await change({v10: "setting", key: option.key, value, ...(option.mode ? {mode: option.mode} : {})});
    }
  }

  function notice(message) {
    error = message;
    render();
  }

  function push(next) {
    if (current()) {
      current().selected = selected;
      current().scrollTop = scrollPane?.scrollTop ?? 0;
      if (current().specs && formNode) {
        current().formSelection = formControls().indexOf(formCursor);
        current().draft = Object.fromEntries(current().specs.flatMap(spec => {
          if (spec.type === "file") return [];
          const input = formNode.elements.namedItem(spec.name);
          return input ? [[spec.name, input.value]] : [];
        }));
      }
    }
    stack.push(next);
    selected = next.selected ?? 0;
    error = "";
    render();
    if (stack.length === 1) onOpen?.(next.id);
  }

  function replace(next) {
    stack.pop();
    stack.push(next);
    selected = next.selected ?? 0;
    error = "";
    render();
  }

  function back() {
    if (stack.length < 2) return close();
    stack.pop();
    selected = current().selected ?? 0;
    error = "";
    render();
    return true;
  }

  function close() {
    if (!stack.length) return false;
    stack.length = 0;
    selected = 0;
    error = "";
    render();
    onClose?.();
    return true;
  }

  function open(label) {
    const id = String(label || "menu").toLowerCase().replaceAll("_", "-");
    if (id === "scope") return isoContext().scope();
    if (id === "mode" && onMode) {
      close();
      onMode();
      return;
    }
    const ctx = isoContext();
    if (id === "quick" && current()) {
      const option = current().option ?? rows[selected];
      push(quickPage(ctx, option));
      return;
    }
    if (id === current()?.id) return close();
    const hadMenu = stack.length > 0;
    stack.length = 0;
    if (hadMenu) onClose?.();
    push(menuRoute(id, ctx));
  }

  function openDialAssignment(dial) {
    const route = current();
    if (route?.dial === dial) return;
    const assignment = dialAssignmentPage(isoContext(), dial);
    if (route?.id === "dial-assignment") replace(assignment);
    else push(assignment);
  }

  async function openZone(zone) {
    if (!Object.hasOwn(ZONE_NAMES, zone)) return;
    close();
    const loading = page("zone-view", ZONE_NAMES[zone], [readout("Loading presets…")]);
    push(loading);
    try {
      const route = await getZonePage(isoContext(), zone);
      if (current() === loading) replace(route);
    } catch (error) {
      if (current() === loading) notice(error.message);
    }
  }

  function openChoices(label, options, value, apply) {
    close();
    push(choicesPage("choose-value", label, options, value, async (next, selectedValue) => {
      await apply(selectedValue);
      next.close();
    }));
  }

  function notifyFieldChange(name) {
    formNode.elements.namedItem(name).dispatchEvent(new Event("change", {bubbles: true}));
    for (const spec of current().specs) {
      if (!spec.options) continue;
      const input = formNode.elements.namedItem(spec.name);
      const key = formNode.querySelector('[data-field="' + spec.name + '"]');
      key.querySelector(".menu-value").textContent = optionLabel(spec, input.value);
    }
  }

  function openFieldOptions(spec, input) {
    const editor = current();
    push(choicesPage("choose-field", spec.label, spec.options, input.value, (next, value) => {
      editor.draft[spec.name] = String(value);
      next.back();
      notifyFieldChange(spec.name);
      focusFormControl(formNode.querySelector('[data-field="' + spec.name + '"]'));
    }));
  }

  function formControls() {
    return [...formNode.querySelectorAll("input:not([type=hidden]), button[data-field]"), submitKey];
  }

  function focusFormControl(control, reveal = true) {
    if (!control || control === formCursor && !reveal) return;
    formCursor?.classList.remove("menu-cursor");
    formCursor = control;
    control.classList.add("menu-cursor");
    keypadKey.disabled = control.tagName !== "INPUT" || control.type === "file";
    if (reveal && scrollPane.contains(control)) revealInMenu(control);
  }

  function openEditorKeyboard() {
    const spec = current().specs.find(field => field.name === formCursor?.name);
    if (!spec || spec.options || spec.type === "file") return;
    const editor = current();
    push(createLcdKeyboard({
      label: spec.label,
      value: formCursor.value,
      inputSpec: spec,
      onAccept: (value, next) => {
        editor.draft[spec.name] = value;
        next.back();
        notifyFieldChange(spec.name);
        focusFormControl(formNode.elements.namedItem(spec.name));
      },
    }));
  }

  async function perform(action, ctx, token = current(), repaint = true) {
    if (busy) return;
    busy = true;
    root.setAttribute("aria-busy", "true");
    error = "";
    try {
      await action(ctx);
    } catch (exception) {
      error = exception instanceof Error ? exception.message : "The receiver rejected this operation.";
    } finally {
      busy = false;
      root.removeAttribute("aria-busy");
      if (current() === token) {
        if (error && formNode) showError();
        else if (repaint || error) render();
      }
    }
  }

  function activate(item) {
    if (!selectable(item) || busy) return;
    if (item.kind === "link") return rowNodes[selected].click();
    const ctx = isoContext();
    if (item.kind === "page") return push({...item});
    if (item.kind === "setting") {
      if (current().id === "dial-b") write("dial_b_function", item.key);
      return push(settingPage(ctx, item));
    }
    return perform(item.action, ctx);
  }

  function revealInMenu(element) {
    if (!element) return;
    const row = element.getBoundingClientRect();
    const pane = scrollPane.getBoundingClientRect();
    const top = pane.top + (current().id === "tune-step" ? scrollPane.firstElementChild.offsetHeight : 0);
    if (row.top < top) scrollPane.scrollTop += row.top - top;
    else if (row.bottom > pane.bottom) scrollPane.scrollTop += row.bottom - pane.bottom;
  }

  function highlight(index, reveal = false) {
    selected = index;
    for (let i = 0; i < rowNodes.length; i++) {
      const entry = rowNodes[i];
      if (entry instanceof HTMLButtonElement) entry.setAttribute("aria-pressed", String(i === index));
      else if (entry instanceof HTMLAnchorElement) entry.classList.toggle("menu-cursor", i === index);
    }
    if (reveal) revealInMenu(rowNodes[selected]);
  }

  function showError() {
    root.querySelector(".error[role=status]")?.remove();
    if (!error) return;
    const item = node("p", "error", error);
    item.setAttribute("role", "status");
    scrollPane.insertBefore(item, scrollPane.children[1] ?? null);
  }

  function update() {
    if (current()?.id === "function" && !busy && isoContext().mode !== renderedMode) render();
    if (!current()?.output) return;
    const output = root.querySelector(".fsk-output");
    if (!output) return;
    if (!fskHeld) {
      const raw = getV10().fsk_text ?? "";
      if (!raw.startsWith(fskCleared)) fskCleared = "";
      const text = raw.slice(fskCleared.length);
      fskDisplay = [fskStamp, text].filter(Boolean).join("\n");
    }
    if (output.textContent !== fskDisplay) {
      output.textContent = fskDisplay;
      output.scrollTop = output.scrollHeight;
    }
  }

  function appendProjectInfo() {
    const section = node("section", "menu-project");
    section.append(
      node("h3", "menu-project-title", document.title),
      node("p", "menu-project-description",
        "A receive-only RTL-SDR scanner for analogue and digital radio, with UK presets, a live spectrum and waterfall, and audio recording."),
    );
    const links = node("div", "menu-project-links");
    for (const item of PROJECT_LINKS) {
      const index = rows.length;
      const link = node("a", "key", item.label);
      Object.assign(link, {href: item.href, target: "_blank", rel: "noopener noreferrer", title: item.title});
      link.addEventListener("focus", () => highlight(index));
      rows.push(item);
      rowNodes.push(link);
      links.append(link);
    }
    section.append(links);
    scrollPane.append(section);
  }

  function render() {
    const route = current();
    root.hidden = !route;
    if (!route) {
      root.replaceChildren();
      rows = rowNodes = [];
      formNode = formCursor = submitKey = keypadKey = scrollPane = null;
      onRender?.();
      return;
    }
    // State updates may refresh a list while the receiver is busy. A visible editor retains its draft.
    if (formNode?.dataset.route === route.id && formNode.isConnected && route.specs) {
      showError();
      onRender?.();
      return;
    }
    const ctx = isoContext();
    renderedMode = ctx.mode;
    const scrollTop = root.dataset.route === route.id ? scrollPane?.scrollTop ?? 0 : route.scrollTop ?? 0;
    root.dataset.route = route.id;
    scrollPane = node("div", "menu-scroll");
    scrollPane.append(node("h2", "overlay-title", route.label));
    root.replaceChildren(scrollPane);
    showError();
    if (route.description) {
      const paragraph = node("p", "menu-description", route.description);
      paragraph.style.whiteSpace = "pre-line";
      scrollPane.append(paragraph);
    }
    const entry = route.entry;
    if (entry) {
      const value = node("p", "menu-entry", entry[0]);
      const caret = node("span", "menu-entry-caret");
      caret.setAttribute("aria-hidden", "true");
      value.append(caret, entry[1]);
      scrollPane.append(value);
    }
    if (route.output) {
      const output = node("pre", "fsk-output");
      output.setAttribute("aria-label", "Decoded RTTY text");
      Object.assign(output.style, {
        minHeight: "70px", maxHeight: "100px", flex: "0 0 auto", margin: "0",
        padding: "5px", border: "1px solid #497587", color: "#80ff80",
        whiteSpace: "pre-wrap", overflow: "auto",
      });
      scrollPane.append(output);
      update();
    }
    const livePages = {
      "record": recorderPage, "history": historyPage, "tier-iii": trunkPage,
      "dmr-conventional": dmrMemoryPage, "prio": priorityPage, "tone": tonePage,
      "function": functionPage, "dmr-squelch": digitalSquelchPage, "timer": timerPage,
    };
    const items = livePages[route.id]?.(ctx).items ?? route.items;
    rows = (typeof items === "function" ? items(ctx) : items).filter(Boolean);
    rowNodes = [];
    formNode = formCursor = submitKey = keypadKey = null;
    const controls = node("div", "overlay-actions");
    if (route.specs) {
      const form = node("form", "menu-form");
      form.id = root.id + "-editor";
      form.dataset.route = route.id;
      form.style.display = "grid";
      form.style.gap = "6px";
      form.append(...route.specs.map(spec => createField(spec, route.draft?.[spec.name] ?? spec.value, openFieldOptions)));
      keypadKey = button("KEYPAD", openEditorKeyboard);
      keypadKey.title = "Open the LCD keypad for the highlighted field.";
      submitKey = node("button", "key", route.buttonLabel);
      submitKey.type = "submit";
      submitKey.title = editorSubmitHelp[route.buttonLabel] ?? "Apply the current entry.";
      submitKey.setAttribute("form", form.id);
      controls.append(keypadKey, submitKey);
      form.addEventListener("submit", event => {
        event.preventDefault();
        perform(next => route.submit(next, form), isoContext(), route, false);
      });
      formNode = form;
      scrollPane.append(form);
      route.prepare?.(form);
      const fields = formControls();
      focusFormControl(fields[route.formSelection] ?? fields[0], false);
      form.addEventListener("focusin", event => {
        if (fields.includes(event.target)) focusFormControl(event.target, false);
      });
      submitKey.addEventListener("focus", () => focusFormControl(submitKey, false));
      form.addEventListener("invalid", event => {
        event.preventDefault();
        focusFormControl(event.target);
        notice(event.target.validationMessage);
      }, true);
    } else if (rows?.length) {
      const grid = node("div", route.columns ? "tile-grid" : "menu-rows");
      if (route.columns) grid.style.gridTemplateColumns = "repeat(" + route.columns + ", minmax(0, 1fr))";
      else Object.assign(grid.style, {display: "grid", gap: "3px"});
      rowNodes = rows.map((item, index) => {
        const active = selectable(item);
        const element = active ? button(item.label, () => {
          highlight(index);
          activate(item);
        }) : node("div", "key menu-status", item.label);
        if (active) element.title = menuHelp(item, route);
        if (item.accessibleName) element.setAttribute("aria-label", item.accessibleName);
        const value = rowTitle(item, ctx);
        if (!route.columns) {
          Object.assign(element.style, {
            display: "flex", justifyContent: "space-between", alignItems: "center",
            textAlign: "left", padding: "4px 6px", gap: "5px",
          });
        }
        if (value) {
          const label = node("span", "menu-value", value);
          if (route.columns) label.style.display = "block";
          element.append(label);
        } else if (item.kind === "page" && !route.columns) {
          const icon = node("span", "menu-value", "›");
          icon.setAttribute("aria-hidden", "true");
          element.append(icon);
        }
        return element;
      });
      grid.append(...rowNodes);
      scrollPane.append(grid);
      if (route.id === "menu") appendProjectInfo();
      if (!selectable(rows[selected])) selected = rows.findIndex(selectable);
      highlight(selected);
    }
    const activeScan = (route.id === "scan" || route.id === "scan-start")
      && ctx.v10.scan_kind && ctx.v10.scan_kind !== "OFF";
    if (route.navigation !== false && !NO_LCD_NAVIGATION.has(route.id) && !activeScan) {
      if (!route.columns && !route.specs && !LCD_RETURN_ONLY.has(route.id)) {
        const scroll = direction => {
          scrollPane.scrollTop += direction * scrollPane.clientHeight * 0.9;
        };
        controls.append(navigationKey("▲", "Scroll up", () => scroll(-1)),
          navigationKey("▼", "Scroll down", () => scroll(1)));
      }
      controls.append(navigationKey("↩", "Back", back));
    }
    if (controls.childElementCount) root.append(controls);
    scrollPane.scrollTop = scrollTop;
    if (!scrollTop && current().selected > 0 && !formNode) revealInMenu(rowNodes[selected]);
    onRender?.();
  }

  function handleDial(delta, dial) {
    if (!current()) return false;
    if (!delta || busy) return true;
    if (formNode) {
      const fields = formControls();
      const input = formCursor;
      if (dial === "navigation" || (dial === "dial-c" && !editingWithDial(dial))) {
        const index = (fields.indexOf(input) + Math.sign(delta) + fields.length) % fields.length;
        focusFormControl(fields[index]);
      } else if (input?.dataset.field) {
        const spec = current().specs.find(item => item.name === input.dataset.field);
        const value = formNode.elements.namedItem(spec.name);
        const currentIndex = spec.options.findIndex(([item]) => String(item) === value.value);
        const index = Math.max(0, Math.min(spec.options.length - 1, currentIndex + Math.sign(delta)));
        if (spec.options[index]) {
          value.value = String(spec.options[index][0]);
          notifyFieldChange(spec.name);
        }
      } else if (input?.type === "number") {
        if (delta < 0) input.stepDown(Math.abs(delta));
        else input.stepUp(delta);
      }
      return true;
    }
    const option = rows[selected];
    if (current().id === dial && option?.kind === "setting") {
      const ctx = isoContext();
      return perform(active => adjustSetting(active, option, delta), ctx).then(() => true);
    }
    const candidates = rows.flatMap((item, index) => selectable(item) ? [index] : []);
    if (candidates.length) {
      const position = candidates.indexOf(selected);
      const nextIndex = (position + Math.sign(delta) + candidates.length) % candidates.length;
      highlight(candidates[nextIndex], true);
      onRender?.();
    } else if (scrollPane) {
      scrollPane.scrollTop += Math.sign(delta) * scrollPane.clientHeight * .75;
    }
    return true;
  }

  async function adjustSetting(ctx, option, delta) {
    const value = settingValue(ctx, option);
    let updated;
    if (option.options) {
      const index = option.options.findIndex(([item]) => item === value);
      const next = Math.max(0, Math.min(option.options.length - 1, index + delta));
      updated = option.options[next][0];
    } else {
      const step = option.key === "volume" ? AF_VOLUME_STEP : option.step;
      const next = Number((value + delta * step).toFixed(6));
      updated = Math.max(option.min, Math.min(option.max, next));
    }
    if (updated !== value) await ctx.set(option, updated);
  }

  function adjustAssignedDial(dial, delta) {
    const ctx = isoContext();
    const option = assignedDial(ctx, dial);
    return option.kind === "setting" ? adjustSetting(ctx, option, delta) : onDialAction?.(option.key, delta, dial);
  }

  function dialSettingFraction(dial) {
    const ctx = isoContext();
    const option = assignedDial(ctx, dial);
    if (option?.kind !== "setting") return undefined;
    if (option.target === "knob") return ctx.radio[option.knob] ?? 0;
    const value = settingValue(ctx, option);
    if (!option.options) return (value - option.min) / (option.max - option.min);
    const index = option.options.findIndex(([item]) => item === value);
    return Math.max(0, index) / (option.options.length - 1);
  }

  function editingWithDial(dial) {
    return current()?.option?.kind === "setting" && stack.at(-2)?.id === dial
      && formCursor?.type === "number";
  }

  function dialFunction(dial) {
    if (!current()) return "";
    if (formNode) {
      if (dial === "dial-c" && !editingWithDial(dial)) return "Choose an editor field";
      const field = current().specs.find(spec => (formCursor?.dataset.field || formCursor?.name) === spec.name);
      if (field && (formCursor.type === "number" || field.options)) return field.label;
      return "Editor navigation";
    }
    if (current().id === dial && rows[selected]?.kind === "setting") return rows[selected].label;
    if (current().id === "lcd-keyboard") return "Choose a keypad key";
    return "Browse " + current().label;
  }

  function select() {
    if (!current()) return false;
    if (!formNode) activate(rows[selected]);
    else if (formCursor === submitKey) submit();
    else if (formCursor?.dataset.field || formCursor?.type === "file") formCursor.click();
    else openEditorKeyboard();
    return true;
  }

  function submit() {
    if (!formNode) return false;
    formNode.requestSubmit();
    return true;
  }

  function handleKey(key) {
    const route = current();
    const action = route?.handleKey?.(key);
    if (!action) return false;
    perform(action, isoContext(), route);
    return true;
  }

  return {open, openChoices, openZone, openDialAssignment, close, back, handleDial, dialFunction, handleKey, select, submit,
    assignment: dial => assignedDial(isoContext(), dial), adjustAssignedDial, dialSettingFraction,
    dialSensitivity: dial => {
      const saved = read(dialSensitivityKey(dial), 1);
      return DIAL_SENSITIVITIES.some(([factor]) => factor === saved) ? saved : 1;
    },
    isOpen: () => Boolean(current()), render, update};
}
