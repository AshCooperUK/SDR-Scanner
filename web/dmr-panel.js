const tunePattern = /^\d{1,4}(?:\.\d{0,6})?$/;
const minHz = 100_000;
const maxHz = 1_750_000_000;
const defaultVolume = .32;
const scopes = ["system", "department", "channel"];
const scopeHelp = [
  "Limit scanning to this saved system.",
  "Limit scanning to this saved department.",
  "Hold the current channel or resume scanning.",
];
const choices = {
  slot: [["", "Any"], ["1", "1"], ["2", "2"]],
  step: [["6250", "6.25"], ["12500", "12.5"], ["25000", "25"]],
};
const clock = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function frequencyHz(mhz) {
  return tunePattern.test(mhz) ? Math.round(Number(mhz) * 1_000_000) : 0;
}

function validHz(hz) {
  return hz >= minHz && hz <= maxHz;
}

function mhz(hz) {
  return Number.isInteger(hz) ? (hz / 1_000_000).toFixed(6).replace(/(\.\d{4}\d*?)0+$/, "$1") : "—";
}

function flag(element, property, value) {
  const text = String(Boolean(value));
  if (element.getAttribute(property) !== text) element.setAttribute(property, text);
}

function relativeStep(element, currentValue, change, press) {
  let drag;
  let dragged = false;
  element.addEventListener("pointerdown", event => {
    if (event.button !== 0) return;
    dragged = false;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, value: currentValue() };
    element.setPointerCapture(event.pointerId);
  });
  element.addEventListener("pointermove", event => {
    if (drag?.id !== event.pointerId) return;
    const offset = event.clientX - drag.x - (event.clientY - drag.y);
    if (!dragged && Math.abs(offset) < 5) return;
    dragged = true;
    if (drag.value === null) {
      if (Math.abs(offset) < 22) return;
      change(Math.sign(offset), true);
      drag.x = event.clientX;
      drag.y = event.clientY;
    } else {
      drag.next = Math.max(0, Math.min(1, drag.value + offset / 180));
      element.style.setProperty("--dmr-angle", `${Math.round(drag.next * 270 - 135)}deg`);
    }
  });
  function endDrag(event) {
    if (drag?.id !== event.pointerId) return;
    if (event.type === "pointerup" && drag.next !== undefined) change(drag.next, false);
    drag = undefined;
  }
  for (const action of ["pointerup", "pointercancel", "lostpointercapture"]) {
    element.addEventListener(action, endDrag);
  }
  element.addEventListener("click", event => {
    if (!dragged || event.detail === 0) press();
    dragged = false;
  });
  element.addEventListener("wheel", event => {
    event.preventDefault();
    if (event.deltaY) change(Math.sign(event.deltaY), true);
  }, { passive: false });
  element.addEventListener("keydown", event => {
    const direction = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[event.key];
    if (!direction) return;
    event.preventDefault();
    event.stopPropagation();
    change(direction, true);
  });
}

export function createDmrPanel({ onMode, onCommand }) {
  const radio = document.querySelector(".radio");
  const face = document.querySelector("#dmr-face");
  const lcd = face.querySelector(".dmr-lcd");
  const displays = new Map(Array.from(face.querySelectorAll("[data-dmr-readout]"),
    element => [element.dataset.dmrReadout, element]));
  const switches = document.querySelectorAll("[data-dmr-mode]");
  const feedback = face.querySelector("#dmr-feedback");
  const meter = face.querySelector(".dmr-bars");
  const bars = [...meter.querySelectorAll("i")];
  const tuneDial = face.querySelector('[data-dmr-dial="tune"]');
  const commands = new Map(Array.from(face.querySelectorAll("[data-dmr-command]"),
    button => [button.dataset.dmrCommand, button]));
  const soft = Array.from(face.querySelectorAll(".dmr-soft-keys button"));
  const legends = Array.from(lcd.querySelectorAll(".dmr-soft-labels button"));
  const menu = lcd.querySelector(".dmr-lcd-menu");
  const screens = new Map(["menu", "favorites", "sms", "message", "entry", "search", "confirm"].map(
    name => [name, document.querySelector(`#dmr-${name}`)]));
  const list = document.querySelector("#dmr-favorite-list");
  const emptyFavorites = document.querySelector("#dmr-empty-favorites");
  const smsList = document.querySelector("#dmr-sms-list");
  const emptySms = document.querySelector("#dmr-empty-sms");
  const smsIndicator = document.querySelector("#dmr-sms-indicator");
  const messageDetail = document.querySelector(".dmr-sms-detail");
  const messageAddress = document.querySelector("#dmr-message-address");
  const messageReception = document.querySelector("#dmr-message-reception");
  const messageText = document.querySelector("#dmr-message-text");
  const entryForm = document.querySelector("#dmr-entry-form");
  const searchForm = document.querySelector("#dmr-search-form");
  const confirmTitle = document.querySelector("#dmr-confirm-title");
  const confirmMessage = document.querySelector("#dmr-confirm-message");
  const confirmFeedback = document.querySelector("#dmr-confirm-feedback");
  const dialNodes = {
    volume: face.querySelector('[data-dmr-dial="volume"]'),
    squelch: face.querySelector('[data-dmr-dial="squelch"]'),
  };
  let state = {};
  let data = null;
  let draft = "";
  let localMessage = "";
  let functionMode = false;
  let backlight = true;
  let rotation = 0;
  let sentMode;
  let savedVolume = defaultVolume;
  let editing;
  let playingReplay = false;
  let favoriteRows = "";
  let smsRows = "";
  let viewedMessage;
  const readMessages = new Set();
  let menuPath = [];
  let menuOpener = null;
  let menuFocus = null;
  let confirmation;
  let menuPending = false;

  function currentScreen() {
    return menuPath.at(-1);
  }

  function messageKey(message) {
    return [state.radio_id, message.time_ms, message.id].join(":");
  }

  function updateSmsIndicator() {
    smsIndicator.hidden = !state.power || !data?.messages?.some(message => !readMessages.has(messageKey(message)));
  }

  function paint(key, value) {
    const field = displays.get(key);
    const text = String(value ?? "—");
    if (field.textContent !== text) field.textContent = text;
  }

  function display() {
    if (!data) return;
    const power = Boolean(state.power);
    const entry = power ? data.entry : null;
    const call = power ? data.call : null;
    const held = power ? data.hold_scope : null;
    const level = Number.isFinite(data.signal_dbfs) ? data.signal_dbfs : state.signal_meter_dbfs ?? state.signal_dbfs;
    const signalCount = power && Number.isFinite(level)
      ? Math.round(Math.max(0, Math.min(1, (level + 95) / 75)) * bars.length) : 0;
    const volume = Math.round((state.volume ?? 0) * 100);
    const squelch = Math.round((state.squelch ?? 0) * 100);
    const protocol = call?.slot ? `DMR · S${call.slot}` : "DMR";
    const clearVoice = call?.voice && call.encrypted === false && call.matched !== false;
    const voiceStatus = call?.encrypted ? "PROTECTED" : clearVoice ? "VOICE" : "";
    let activity = "";
    if (power && draft) activity = "PRESS E / YES TO TUNE";
    else if (call?.encrypted) activity = "PROTECTED CALL · AUDIO MUTED";
    else if (call?.voice && call.matched === false) activity = "FILTERED DMR CALL";
    else if (call?.voice && call.encrypted !== false) activity = "WAITING FOR CALL HEADER";
    else if (clearVoice) activity = "RECEIVING DIGITAL VOICE";
    else if (call?.sync) activity = "DMR SYNC · WAITING FOR VOICE";
    else if (power && data.mode === "scan") activity = "SCANNING FAVOURITES";
    else if (power && data.mode === "search") activity = "SEARCHING FOR DMR";
    else if (power && data.hz) activity = "WAITING FOR DMR";
    else if (power) activity = "ENTER A FREQUENCY OR SELECT FAV";
    paint("protocol", protocol);
    paint("clock", clock.format(new Date()).replace(",", ""));
    paint("volume", String(volume).padStart(2, "0"));
    paint("squelch", String(squelch).padStart(2, "0"));
    paint("sync", !power ? "STANDBY" : functionMode ? "FUNC" : call?.sync ? "SYNC" : "NO SYNC");
    paint("system", entry?.system || "—");
    paint("department", entry?.department || "—");
    paint("channel", entry?.name || (power && data.hz ? "Direct tune" : "—"));
    paint("system_hold", held === "system" ? "HOLD" : "");
    paint("department_hold", held === "department" ? "HOLD" : "");
    paint("channel_hold", data.avoided && power ? (data.avoid_kind === "permanent" ? "AVOID" : "TEMP AVOID")
      : held === "channel" ? "HOLD" : "");
    paint("frequency", power ? draft || mhz(data.hz) : "—");
    paint("activity", activity);
    paint("color_code", Number.isInteger(call?.color_code) ? String(call.color_code).padStart(2, "0") : "—");
    paint("slot", call?.slot);
    paint("talkgroup", call?.talkgroup);
    paint("unit_id", call?.unit_id);
    paint("rf", power && Number.isFinite(level) ? `RF ${Math.round(level)} dBFS` : "RF —");
    paint("errors", Number.isInteger(call?.errors) ? `DECODER ERR ${call.errors}` : "DECODER ERR —");
    paint("voice", voiceStatus || (power && state.record_enabled ? "REC" : ""));
    updateSmsIndicator();
    meter.setAttribute("aria-valuenow", String(signalCount));
    bars.forEach((bar, index) => bar.classList.toggle("is-lit", index < signalCount));
    paintSoftKeys(held);
    for (const [key, value] of Object.entries({ volume, squelch })) {
      dialNodes[key].style.setProperty("--dmr-angle", `${Math.round(value * 2.7 - 135)}deg`);
    }
    lcd.classList.toggle("is-dark", !currentScreen() && (!backlight || state.muted));
    flag(commands.get("scan"), "aria-pressed", power && data.mode === "scan");
    flag(commands.get("search"), "aria-pressed", power && data.mode === "search");
    flag(commands.get("replay"), "aria-pressed", power && (state.record_enabled || playingReplay));
    flag(commands.get("mute"), "aria-pressed", state.muted);
    const message = localMessage || (power ? data.error || data.notice : "") || "";
    if (feedback.textContent !== message) feedback.textContent = message;
    feedback.title = message;
  }

  function useFunction() {
    const active = functionMode;
    functionMode = false;
    flag(tuneDial, "aria-pressed", false);
    return active;
  }

  function editDigit(digit) {
    if (!state.power) return;
    localMessage = "";
    if (digit === "." && draft.includes(".")) draft = "";
    else if (digit === "." && !draft) draft = "0.";
    else if (tunePattern.test(draft + digit)) draft += digit;
    display();
  }

  function enter() {
    const hz = frequencyHz(draft);
    if (!draft) return;
    if (!validHz(hz)) {
      localMessage = "Enter a frequency from 0.1000 to 1750.0000 MHz.";
    } else {
      onCommand("tune", hz);
      draft = localMessage = "";
    }
    display();
  }

  function paintSoftKeys(held) {
    const current = currentScreen();
    const labels = {
      menu: ["BACK", "SELECT", "NEXT"],
      favorites: ["BACK", "SELECT", "NEXT"],
      sms: ["BACK", "SELECT", "NEXT"],
      message: ["BACK", "", "DELETE"],
      entry: ["BACK", "NEXT", "SAVE"],
      search: ["BACK", "NEXT", "SEARCH"],
      confirm: ["CANCEL", "", "YES"],
    }[current] ?? ["SYSTEM", "DEPT", "CHAN"];
    flag(dialNodes.squelch, "aria-expanded", current);
    soft.forEach((key, index) => {
      const legend = legends[index];
      const label = labels[index];
      const pressed = !current && held === scopes[index];
      if (legend.textContent !== label) legend.textContent = label;
      legend.classList.toggle("is-held", pressed);
      key.disabled = legend.disabled = !label || Boolean(current && menuPending);
      for (const control of [key, legend]) {
        const hint = current ? label : scopeHelp[index];
        if (control.title !== hint) control.title = hint;
        if (current) control.removeAttribute("aria-pressed");
        else flag(control, "aria-pressed", pressed);
      }
      const name = current ? label : `${scopes[index]} hold`;
      if (key.getAttribute("aria-label") !== name) key.setAttribute("aria-label", name);
    });
  }

  function showScreen(focus = true) {
    const current = currentScreen();
    lcd.classList.toggle("is-menu-open", Boolean(current));
    menu.hidden = !current;
    for (const [name, screen] of screens) screen.hidden = name !== current;
    menuFocus?.classList.remove("is-selected");
    menuFocus = null;
    display();
    if (!focus) return;
    if (!current) {
      menuOpener?.focus({ preventScroll: true });
      menuOpener = null;
    } else {
      const screen = screens.get(current);
      const first = screen.querySelector(".dmr-menu-list button, .dmr-sms-detail") || screen.querySelector("h2");
      if (first.matches("h2")) first.tabIndex = -1;
      first.focus({ preventScroll: true });
    }
  }

  function open(name) {
    if (menuPending || currentScreen() === name) return;
    if (!currentScreen()) menuOpener = document.activeElement;
    menuPath.push(name);
    showScreen();
  }

  function closeMenu(focus = true) {
    if (!currentScreen()) return;
    menuPath = [];
    showScreen(focus);
    menuOpener = null;
  }

  function goBack(steps = 1) {
    if (menuPending) return;
    menuPath.splice(-steps);
    showScreen();
  }

  function moveSelection(direction) {
    const current = currentScreen();
    if (current === "confirm" || menuPending) return;
    if (current === "message") {
      messageDetail.scrollBy(0, direction * messageDetail.clientHeight * .8);
      return;
    }
    const screen = screens.get(current);
    const options = Array.from(screen.querySelectorAll('.dmr-menu-list button, input:not([type="hidden"]), [data-dmr-choice]'));
    if (!options.length) return;
    const active = options.indexOf(menuFocus);
    const index = active < 0 ? (direction > 0 ? 0 : options.length - 1)
      : (active + direction + options.length) % options.length;
    options[index].focus();
  }

  function selectMenuItem() {
    const current = currentScreen();
    if (menuPending) return;
    if (current === "confirm") {
      if (confirmation.action === "delete_sms" && viewedMessage.radioId !== state.radio_id) {
        renderSms();
        goBack(2);
        return;
      }
      void applyMenu(confirmation.action, confirmation.value, confirmFeedback, confirmation.success);
    } else if (current === "entry" || current === "search") {
      if (menuFocus?.matches("[data-dmr-choice]")) menuFocus.click();
      else submitScreen();
    } else {
      const screen = screens.get(current);
      const button = menuFocus?.matches(".dmr-menu-list button") && screen.contains(menuFocus)
        ? menuFocus : screen.querySelector(".dmr-menu-list button");
      button?.click();
    }
  }

  function submitScreen() {
    screens.get(currentScreen()).querySelector("form").requestSubmit();
  }

  function requirePower() {
    if (state.power) return true;
    localMessage = "Turn on VOLUME to use the DMR receiver.";
    display();
    return false;
  }

  function draftForm(form) {
    const feedback = form.querySelector(".dmr-form-feedback");
    showFeedback(feedback);
    return { fields: new FormData(form), feedback };
  }

  function setChoice(form, field, value) {
    const [key, label] = choices[field].find(([key]) => key === String(value)) ?? choices[field][0];
    form.elements.namedItem(field).value = key;
    form.querySelector(`[data-dmr-choice="${field}"]`).textContent = label;
  }

  function showEntry(entry) {
    if (!requirePower()) return;
    editing = entry ?? null;
    const fields = entryForm.elements;
    const preview = entry || {};
    for (const key of ["name", "system", "department", "color_code", "slot", "talkgroup"]) {
      fields.namedItem(key).value = preview[key] ?? "";
    }
    for (const key of ["color_code", "slot", "talkgroup"]) {
      if (!entry && data?.call?.[key] != null) fields.namedItem(key).value = data.call[key];
    }
    setChoice(entryForm, "slot", fields.slot.value);
    const hz = preview.hz ?? data?.hz;
    fields.frequency.value = Number.isInteger(hz) ? mhz(hz) : "";
    draftForm(entryForm);
    open("entry");
  }

  function showSearch() {
    if (!requirePower()) return;
    const fields = searchForm.elements;
    const range = data.search;
    fields.lower.value = range ? mhz(range.lower) : "";
    fields.upper.value = range ? mhz(range.upper) : "";
    setChoice(searchForm, "step", range?.step ?? 12_500);
    draftForm(searchForm);
    open("search");
  }

  function clearAvoids() {
    if (requirePower()) showConfirmation("Clear avoids", "Clear all temporary and permanent DMR avoids?", "clear_avoids");
  }

  function showConfirmation(title, message, action, value, success) {
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    showFeedback(confirmFeedback);
    confirmation = { action, value, success };
    open("confirm");
  }

  function showFavorites() {
    if (!requirePower()) return;
    renderFavorites();
    open("favorites");
  }

  function renderFavorites() {
    const entries = data?.entries ?? [];
    const signature = JSON.stringify(entries);
    if (signature === favoriteRows) return;
    favoriteRows = signature;
    emptyFavorites.hidden = entries.length !== 0;
    list.replaceChildren(...entries.map(entry => {
      const row = document.createElement("li");
      row.className = "dmr-favorite-row";
      const name = entry.name || `${mhz(entry.hz)} MHz`;
      for (const [action, label] of [["recall", `${name} · ${mhz(entry.hz)} MHz`],
        ["edit", "Edit"], ["delete", "Delete"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.dmrFavorite = action;
        button.dataset.id = String(entry.id);
        button.textContent = label;
        if (action !== "recall") button.setAttribute("aria-label", `${label} ${name}`);
        row.append(button);
      }
      return row;
    }));
  }

  function renderSms() {
    const messages = data?.messages ?? [];
    const signature = messages.map(messageKey).join(" ");
    if (signature === smsRows && messages.length === smsList.childElementCount) return;
    smsRows = signature;
    const selected = menuFocus?.dataset.dmrMessage;
    emptySms.hidden = Boolean(messages.length);
    const rows = messages.map(message => {
      const row = document.createElement("li");
      const button = document.createElement("button");
      const address = document.createElement("strong");
      const preview = document.createElement("span");
      button.type = "button";
      button.dataset.dmrMessage = messageKey(message);
      address.textContent = `From ${message.source_id} to ${message.target_id}`;
      preview.textContent = message.text;
      button.append(address, preview);
      row.append(button);
      return row;
    });
    smsList.replaceChildren(...rows);
    if (selected) {
      const button = rows.map(row => row.firstElementChild)
        .find(button => button.dataset.dmrMessage === selected);
      (button ?? rows[0]?.firstElementChild)?.focus({ preventScroll: true });
    }
  }

  function showMessage(key) {
    const message = data?.messages?.find(message => messageKey(message) === key);
    if (!message) return;
    viewedMessage = { radioId: state.radio_id, id: message.id, timeMs: message.time_ms };
    readMessages.add(key);
    if (readMessages.size > 96) readMessages.delete(readMessages.values().next().value);
    messageAddress.textContent = `From ${message.source_id} to ${message.target_id}`;
    const date = Number.isFinite(message.time_ms) ? clock.format(message.time_ms) : "";
    const color = Number.isInteger(message.color_code) ? `CC ${message.color_code}` : "";
    const slot = Number.isInteger(message.slot) ? `TS ${message.slot}` : "";
    messageReception.textContent = [date, `${mhz(message.hz)} MHz`, color, slot].filter(Boolean).join(" · ");
    messageText.textContent = message.text;
    messageDetail.scrollTop = 0;
    open("message");
  }

  function confirmSmsDelete() {
    const { radioId, id, timeMs } = viewedMessage;
    if (radioId !== state.radio_id) {
      renderSms();
      goBack();
      return;
    }
    showConfirmation("Delete SMS", "Delete this message?", "delete_sms", { id, time_ms: timeMs }, () => {
      renderSms();
      goBack(2);
    });
  }

  function showFeedback(feedback, message = "") {
    feedback.textContent = message;
    feedback.hidden = !message;
  }

  async function applyMenu(action, value, feedback, success = closeMenu) {
    if (menuPending) return;
    const active = currentScreen();
    menuPending = true;
    paintSoftKeys();
    let issue = "";
    try {
      const result = await onCommand(action, value);
      issue = result?.dmr?.action_error || result?.dmr?.notice || (!result && "The scanner could not apply this change.");
    } catch {
      issue = "The scanner could not apply this change.";
    } finally {
      menuPending = false;
      display();
    }
    if (currentScreen() !== active) return;
    if (issue) showFeedback(feedback, issue);
    else success();
  }

  entryForm.addEventListener("submit", event => {
    event.preventDefault();
    const { fields, feedback } = draftForm(entryForm);
    const hz = frequencyHz(fields.get("frequency"));
    if (!validHz(hz)) {
      showFeedback(feedback, "Enter a frequency from 0.1000 to 1750.0000 MHz.");
      return;
    }
    const entry = { hz };
    if (editing) entry.id = editing.id;
    for (const key of ["name", "system", "department"]) entry[key] = fields.get(key).trim();
    for (const key of ["color_code", "slot", "talkgroup"]) {
      const value = fields.get(key);
      entry[key] = value === "" ? null : Number(value);
    }
    for (const [key, maximum, message] of [
      ["color_code", 15, "Enter a colour code from 0 to 15 or leave it blank."],
      ["talkgroup", 16_777_215, "Enter a valid talkgroup number or leave it blank."],
    ]) {
      if (entry[key] !== null && (!/^\d+$/.test(fields.get(key)) || entry[key] > maximum)) {
        showFeedback(feedback, message);
        return;
      }
    }
    void applyMenu("save", entry, feedback);
  });

  searchForm.addEventListener("submit", event => {
    event.preventDefault();
    const { fields, feedback } = draftForm(searchForm);
    const lower = frequencyHz(fields.get("lower"));
    const upper = frequencyHz(fields.get("upper"));
    const step = Number(fields.get("step"));
    if (!validHz(lower) || !validHz(upper) || upper < lower) {
      showFeedback(feedback, "Choose an ascending frequency range from 0.1000 to 1750.0000 MHz.");
      return;
    }
    void applyMenu("search", { lower, upper, step }, feedback);
  });

  list.addEventListener("click", event => {
    const button = event.target.closest("[data-dmr-favorite]");
    if (!button || menuPending) return;
    const id = Number(button.dataset.id);
    const entry = data?.entries?.find(candidate => candidate.id === id);
    if (!entry) return;
    const action = button.dataset.dmrFavorite;
    if (action === "edit") showEntry(entry);
    else if (action === "recall") {
      closeMenu();
      onCommand("recall", id);
    } else showConfirmation("Delete favourite", `Delete ${entry.name || "this DMR favourite"}?`, "delete", id, goBack);
  });

  menu.addEventListener("click", event => {
    if (menuPending) return;
    const choice = event.target.closest("[data-dmr-choice]");
    if (choice) {
      const field = choice.dataset.dmrChoice;
      const values = choices[field];
      const input = choice.form.elements.namedItem(field);
      const index = values.findIndex(([key]) => key === input.value);
      setChoice(choice.form, field, values[(index + 1) % values.length][0]);
      return;
    }
    const action = event.target.closest("[data-dmr-menu]")?.dataset.dmrMenu;
    const message = event.target.closest("[data-dmr-message]")?.dataset.dmrMessage;
    if (message) showMessage(message);
    if (action === "favorites") showFavorites();
    if (action === "entry") showEntry();
    if (action === "search") showSearch();
    if (action === "sms" && requirePower()) {
      renderSms();
      open("sms");
    }
    if (action === "clear-avoids") clearAvoids();
  });
  menu.addEventListener("focusin", event => {
    menuFocus?.classList.remove("is-selected");
    menuFocus = event.target;
    if (menuFocus.matches("button, input")) menuFocus.classList.add("is-selected");
  });

  for (const button of switches) {
    button.addEventListener("click", async () => {
      if (sentMode !== undefined || radio.classList.contains("is-pin-entry")) return;
      const enabled = button.dataset.dmrMode === "on";
      if (enabled === Boolean(data)) return;
      sentMode = enabled;
      try {
        await onMode(enabled);
      } finally {
        sentMode = undefined;
      }
    });
  }

  relativeStep(tuneDial, () => null, direction => {
    if (!requirePower()) return;
    rotation += 20 * direction;
    tuneDial.style.setProperty("--dmr-angle", `${rotation % 360}deg`);
    if (currentScreen()) {
      moveSelection(direction);
      return;
    }
    useFunction();
    onCommand("step", direction);
  }, () => {
    if (currentScreen()) {
      selectMenuItem();
      return;
    }
    functionMode = !functionMode;
    flag(tuneDial, "aria-pressed", functionMode);
    display();
  });

  for (const [name, element] of Object.entries(dialNodes)) {
    relativeStep(element, () => state[name] ?? 0, (value, relative) => {
      const next = relative ? (state[name] ?? 0) + value * (name === "squelch" ? .005 : .04) : value;
      onCommand(name, Math.max(0, Math.min(1, next)));
    }, () => {
      const alternate = useFunction();
      if (name === "squelch") {
        if (currentScreen()) goBack();
        else if (requirePower()) open("menu");
      } else if (alternate) {
        backlight = !backlight;
      } else {
        onCommand("volume", state.power ? 0 : savedVolume);
      }
      display();
    });
  }

  face.addEventListener("click", event => {
    const target = event.target.closest("[data-dmr-command], [data-dmr-digit], [data-dmr-soft]");
    if (!target || !data) return;
    const { dmrCommand, dmrDigit, dmrSoft } = target.dataset;
    const current = currentScreen();
    if (current && dmrSoft !== undefined) {
      if (dmrSoft === "0") goBack();
      else if (dmrSoft === "2" && current === "message") confirmSmsDelete();
      else if (dmrSoft === "2" && ["entry", "search"].includes(current)) submitScreen();
      else if ((dmrSoft === "2" && current === "confirm")
          || (dmrSoft === "1" && ["menu", "favorites", "sms"].includes(current))) selectMenuItem();
      else moveSelection(1);
      return;
    }
    if (current && dmrCommand === "enter") {
      selectMenuItem();
      return;
    }
    if (dmrDigit !== undefined) {
      if (!current) editDigit(dmrDigit);
      else if (!menuPending && menuFocus?.matches('input[inputmode="decimal"], input[inputmode="numeric"]')) {
        const { value, selectionStart, selectionEnd, maxLength, inputMode } = menuFocus;
        const updated = value.slice(0, selectionStart) + dmrDigit + value.slice(selectionEnd);
        const valid = inputMode === "decimal" ? tunePattern.test(updated) : /^\d+$/.test(updated);
        if (valid && (maxLength === -1 || updated.length <= maxLength)) {
          menuFocus.setRangeText(dmrDigit, selectionStart, selectionEnd, "end");
        }
      } else if (dmrDigit === ".") goBack();
      return;
    }
    const alternate = useFunction();
    if (dmrCommand === "discovery") onCommand("discovery");
    else if (requirePower()) {
      if (dmrSoft !== undefined) onCommand("hold", scopes[Number(dmrSoft)]);
      else if (dmrCommand === "enter") enter();
      else if (dmrCommand === "fav") showFavorites();
      else if (dmrCommand === "search" && alternate) showSearch();
      else if (dmrCommand === "avoid") onCommand("avoid", alternate);
      else if (dmrCommand === "replay" && alternate) onCommand("record");
      else onCommand(dmrCommand);
    }
    display();
  });

  function handleKey(event) {
    if (!data || !state.power || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
        || event.target.closest?.("dialog, #discovery-pad")) return false;
    if (currentScreen() && event.key === "Escape") {
      goBack();
      event.preventDefault();
      return true;
    }
    if (event.target.closest?.("input, select, textarea, [contenteditable]")) return false;
    if (currentScreen()) {
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        moveSelection(event.key === "ArrowDown" ? 1 : -1);
        event.preventDefault();
        return true;
      }
      return false;
    }
    if (/^[0-9.]$/.test(event.key)) editDigit(event.key);
    else if (event.key === "Enter" && draft) enter();
    else if (event.key === "Escape" || event.key === "Backspace") {
      if (!draft && !localMessage) return false;
      draft = event.key === "Escape" ? "" : draft.slice(0, -1);
      localMessage = "";
      display();
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      onCommand("step", event.key === "ArrowUp" ? 1 : -1);
    } else return false;
    event.preventDefault();
    return true;
  }

  return {
    get active() { return Boolean(data); },
    handleKey,
    setPlayback({ replay, last, discovery }) {
      if (replay !== undefined) {
        playingReplay = Boolean(replay);
        flag(commands.get("replay"), "aria-pressed", state.power && (playingReplay || state.record_enabled));
      }
      if (last !== undefined) flag(commands.get("play-last"), "aria-pressed", last);
      if (discovery !== undefined) flag(commands.get("discovery"), "aria-expanded", discovery);
    },
    render(next) {
      const wasActive = Boolean(data);
      const wasPowered = Boolean(state.power);
      state = next;
      data = next.pin_mode ? null : next.dmr ?? null;
      document.body.classList.toggle("dmr-mode", Boolean(data));
      radio.classList.toggle("is-dmr", Boolean(data));
      face.hidden = !data;
      if (next.volume > 0) savedVolume = next.volume;
      if (!data || !next.power) {
        closeMenu(false);
        draft = localMessage = "";
        useFunction();
      }
      if (data && wasPowered !== Boolean(next.power)) backlight = true;
      if (data) this.setPlayback({ replay: next.replay_active, last: next.play_last_active,
        discovery: next.discovery_open });
      if (data && currentScreen() === "favorites") renderFavorites();
      if (data && currentScreen() === "sms") renderSms();
      if (wasActive !== Boolean(data) && sentMode !== undefined) {
        const replacement = data ? document.querySelector("#dmr-return")
          : [...switches].find(button => button.dataset.dmrMode === "on" && button.getClientRects().length);
        replacement?.focus({ preventScroll: true });
      }
      display();
    },
  };
}
