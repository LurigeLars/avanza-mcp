/* Bridge credentials never enter a content script or page. */
const ready = chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"})
  .then(async () => badge(publicState(await chrome.storage.local.get(["enabled"])).enabled));
const BASE = "http://127.0.0.1:18762";
const INTERVALS = [0, 30000, 60000, 120000, 300000];
let inFlight = 0;
let retryUntil = 0;
function publicState(values) {
  return {status: values.token ? "PAIRED" : "PAIR_REQUIRED", enabled: values.enabled !== false,
    pollInterval: INTERVALS.includes(values.pollInterval) ? values.pollInterval : 60000};
}
async function badge(enabled) {
  await Promise.all([
    chrome.action.setBadgeText({text: enabled ? "ON" : "OFF"}),
    chrome.action.setBadgeBackgroundColor({color: enabled ? "#23854a" : "#606975"}),
    chrome.action.setTitle({title: `Avanza Order Extension: ${enabled ? "ON" : "OFF"}`})
  ]);
}
ready.catch(() => {});
async function request(path, data, token) {
  const response = await fetch(BASE + path, {
    method: "POST", credentials: "omit", cache: "no-store", redirect: "error",
    headers: {"Content-Type": "application/json", "X-Avanza-Capacity": "1", ...(token ? {"X-Avanza-Token": token} : {})},
    body: JSON.stringify(data), signal: AbortSignal.timeout(3500)
  });
  if (!response.ok) return {status: response.status === 401 ? "PAIR_REQUIRED" : response.status === 429 ? "RATE_LIMITED" : "OFFLINE"};
  return response.json();
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const popup = sender.url === chrome.runtime.getURL("popup.html");
  let avanza = false, page = false, orderId = null;
  try {
    const url = new URL(sender.url);
    avanza = !!sender.tab && sender.frameId === 0 && url.protocol === "https:"
      && ["www.avanza.se", "avanza.se"].includes(url.hostname);
    // Chrome may retain the initial sender.url across history.pushState().
    // Authenticate the frame origin, but validate the current route from our isolated content script.
    const pathname = message?.pathname === undefined ? url.pathname : message.pathname;
    if (avanza && typeof pathname === "string") {
      orderId = pathname.match(/^\/handla\/order\.html\/(?:kop|salj)\/([0-9]{1,12})\/?$/)?.[1] ?? null;
      page = pathname === "/borshandlade-produkter/certifikat-torg/lista.html" || orderId;
    }
  } catch { /* Unknown sender is denied. */ }
  if (!(popup || avanza)) { reply({status: "DENIED"}); return false; }
  if (message?.type === "CAPACITY" && inFlight >= 4) { reply({status: "BACKOFF", retry_after: 5}); return false; }
  (async () => {
    await ready;
    if (popup && message?.type === "PAIR" && /^[A-F0-9]{20}$/.test(message.code)) {
      const result = await request("/pair", {code: message.code});
      if (result.status === "PAIRED" && /^[a-f0-9]{64}$/.test(result.token)) {
        await chrome.storage.local.set({token: result.token});
        return {status: "PAIRED"};
      }
      return {status: result.status ?? "PAIR_REQUIRED"};
    }
    const values = await chrome.storage.local.get(["token", "capacityRetryUntil", "enabled", "pollInterval"]);
    const state = publicState(values);
    if ((popup && message?.type === "STATUS") || (avanza && message?.type === "STATE")) return state;
    if (popup && message?.type === "SET_SETTINGS") {
      const change = {};
      if (typeof message.enabled === "boolean") change.enabled = message.enabled;
      else if (message.enabled !== undefined) return {status: "INVALID"};
      if (INTERVALS.includes(message.pollInterval)) change.pollInterval = message.pollInterval;
      else if (message.pollInterval !== undefined) return {status: "INVALID"};
      if (!Object.keys(change).length) return {status: "INVALID"};
      await chrome.storage.local.set(change);
      const updated = publicState(await chrome.storage.local.get(["token", "enabled", "pollInterval"]));
      await badge(updated.enabled);
      // Tab IDs and messaging need no URL/title access or additional permissions.
      const tabs = await chrome.tabs.query({});
      await Promise.all(tabs.map(tab => chrome.tabs.sendMessage(tab.id, {type: "STATE_CHANGED", ...updated}).catch(() => {})));
      return updated;
    }
    const ids = message?.ids;
    if (!page || message?.type !== "CAPACITY" || !Array.isArray(ids) || ids.length < 1 || ids.length > 5
        || ids.some(i => typeof i !== "string" || !/^[0-9]{1,12}$/.test(i)) || new Set(ids).size !== ids.length) return {status: "INVALID"};
    if (orderId && (ids.length !== 1 || ids[0] !== orderId)) return {status: "INVALID"};
    if (!state.enabled) return {status: "OFF"};
    const {token, capacityRetryUntil} = values;
    if (!token) return {status: "PAIR_REQUIRED"};
    if (Number.isSafeInteger(capacityRetryUntil)) retryUntil = Math.max(retryUntil, capacityRetryUntil);
    if (Date.now() < retryUntil) return {status: "BACKOFF", retry_after: Math.ceil((retryUntil - Date.now()) / 1000)};
    const result = await request("/capacity", {ids}, token).catch(() => ({status: "OFFLINE"}));
    const statuses = [result.status, ...Object.values(result.items ?? {}).map(q => q?.status)];
    const delay = statuses.includes("RATE_LIMITED") ? 60000
      : statuses.some(s => ["TIMEOUT", "BUSY", "OFFLINE"].includes(s)) ? 30000 : 0;
    if (delay) {
      retryUntil = Math.max(retryUntil, Date.now() + delay);
      await chrome.storage.local.set({capacityRetryUntil: retryUntil});
      result.retry_after = Math.ceil((retryUntil - Date.now()) / 1000);
    }
    return result;
  })().then(reply).catch(() => reply({status: "OFFLINE"})).finally(() => { inFlight--; });
  inFlight++;
  return true;
});
