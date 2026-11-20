// Keep the shared setup separate from receiver actions and browser identities.
const copy = value => structuredClone(value);
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const stable = value => JSON.stringify(value, (_, item) => object(item)
  ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const same = (left, right) => stable(left) === stable(right);

function changesBetween(before, after) {
  const changes = {};
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (!Object.hasOwn(after, key)) changes[key] = null;
    else if (!Object.hasOwn(before, key) || !same(before[key], after[key])) changes[key] = copy(after[key]);
  }
  return changes;
}

function mergeChanges(settings, changes) {
  const result = copy(settings);
  for (const [key, value] of Object.entries(changes)) {
    if (value === null) delete result[key];
    else result[key] = copy(value);
  }
  return result;
}

function checkedProfile(value) {
  if (!object(value) || !Number.isSafeInteger(value.revision) || value.revision < 0
      || value.settings !== null && !object(value.settings)) {
    throw new Error("The saved radio setup could not be read.");
  }
  return copy(value);
}

/**
 * Synchronize a validated, flat settings snapshot through /api/profile.
 * request must throw {status: 409, data: {revision, settings}} on a conflict.
 * applySettings must be synchronous and must not execute radio actions.
 */
export function createSettingsSync({
  request, getSettings, applySettings, onStatus = () => {},
  expandChanges = changes => changes, debounceMs = 500, pollMs = 2000,
}) {
  let enabled = false;
  let generation = 0;
  let revision = 0;
  let remote = null;
  let seen = {};
  let pending = {};
  let publishLocal = false;
  let applying = false;
  let debounceTimer;
  let pollTimer;
  let job;

  const active = token => enabled && token === generation;
  const status = (token, value, error) => { if (active(token)) onStatus(value, error); };

  function settings() {
    const value = getSettings();
    if (!object(value)) throw new Error("The current radio setup could not be read.");
    return copy(value);
  }

  function capture() {
    const current = settings();
    if (seen !== null) Object.assign(pending, changesBetween(seen, current));
    seen = current;
    pending = copy(expandChanges(copy(pending), copy(current)));
    if (!object(pending)) throw new Error("The changed radio setup could not be read.");
    return current;
  }

  function accept(profile, token) {
    if (!active(token)) return;
    const current = capture();
    const value = checkedProfile(profile);
    revision = value.revision;
    remote = value.settings;
    if (publishLocal) {
      pending = copy(expandChanges(changesBetween(remote ?? {}, current), copy(current)));
      publishLocal = false;
    } else if (remote === null) {
      pending = copy(expandChanges(changesBetween({}, current), copy(current)));
    }
    const merged = mergeChanges(remote ?? {}, pending);
    if (!same(current, merged)) {
      applying = true;
      try { applySettings(copy(merged)); } finally { applying = false; }
    }
    // Remember the actual normalized local values so applying a remote profile
    // cannot be mistaken for a new edit and echoed back to the other browsers.
    seen = settings();
  }

  async function exchange(token) {
    if (!active(token)) return;
    if (remote === null) status(token, "loading");
    else if (Object.keys(pending).length) status(token, "saving");
    const profile = await request("/api/profile", {headers: {"X-SDR-Profile": "1"}});
    if (!active(token)) return;
    accept(profile, token);
    for (let attempt = 0; attempt < 4 && active(token); attempt++) {
      capture();
      if (!Object.keys(pending).length) {
        status(token, "saved");
        return;
      }
      const sent = copy(pending);
      status(token, "saving");
      let saved;
      try {
        saved = await request("/api/profile", {method: "POST", body: {revision, changes: sent}});
      } catch (error) {
        if (!active(token)) return;
        if (error.status !== 409 || !error.data) throw error;
        accept(error.data, token);
        continue;
      }
      if (!active(token)) return;
      // Capture edits made while the write was in flight before acknowledging
      // the sent values. A later edit to the same field must remain pending.
      capture();
      for (const [key, value] of Object.entries(sent)) {
        if (Object.hasOwn(pending, key) && same(pending[key], value)) delete pending[key];
      }
      accept(saved, token);
    }
    if (active(token)) status(token, Object.keys(pending).length ? "saving" : "saved");
  }

  function run() {
    if (!enabled) return Promise.resolve();
    const token = generation;
    if (job?.token === token) {
      job.again = true;
      return job.promise;
    }
    const next = {token, again: false, promise: null};
    job = next;
    next.promise = (async () => {
      do {
        next.again = false;
        await exchange(token);
      } while (active(token) && next.again);
    })().catch(error => {
      status(token, "offline", error);
    }).finally(() => {
      if (job === next) job = undefined;
    });
    return next.promise;
  }

  function schedulePoll(token) {
    if (!active(token)) return;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => {
      pollTimer = undefined;
      void run().finally(() => schedulePoll(token));
    }, pollMs);
  }

  function stop() {
    enabled = false;
    generation++;
    clearTimeout(debounceTimer);
    clearTimeout(pollTimer);
    debounceTimer = pollTimer = undefined;
    onStatus("off");
  }

  function start({publishLocal: useLocal = false} = {}) {
    if (enabled) return run();
    enabled = true;
    generation++;
    revision = 0;
    remote = null;
    pending = {};
    publishLocal = useLocal;
    try {
      seen = settings();
    } catch (error) {
      seen = null;
      status(generation, "offline", error);
      schedulePoll(generation);
      return Promise.resolve();
    }
    const token = generation;
    schedulePoll(token);
    return run();
  }

  function changed() {
    if (!enabled || applying) return;
    try {
      capture();
    } catch (error) {
      status(generation, "offline", error);
      return;
    }
    status(generation, "saving");
    clearTimeout(debounceTimer);
    const token = generation;
    debounceTimer = setTimeout(() => {
      debounceTimer = undefined;
      if (active(token)) void run();
    }, debounceMs);
  }

  function flush() {
    clearTimeout(debounceTimer);
    debounceTimer = undefined;
    return run();
  }

  return {start, stop, changed, poll: run, flush, get enabled() { return enabled; }};
}
