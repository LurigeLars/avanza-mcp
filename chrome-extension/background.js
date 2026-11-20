/* Bridge credentials never enter a content script or page. */
const ready = chrome.storage.local.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
const BASE = "http://127.0.0.1:18762";
let inFlight = 0;
let retryUntil = 0;
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
  let page = false, orderId = null;
  try {
    const url = new URL(sender.url);
    orderId = url.pathname.match(/^\/handla\/order\.html\/(?:kop|salj)\/([0-9]{1,12})\/?$/)?.[1] ?? null;
    page = sender.tab && sender.frameId === 0 && url.protocol === "https:"
      && ["www.avanza.se", "avanza.se"].includes(url.hostname)
      && (url.pathname === "/borshandlade-produkter/certifikat-torg/lista.html" || orderId);
  } catch { /* Unknown sender is denied. */ }
  if (!(popup || page)) { reply({status: "DENIED"}); return false; }
  if (inFlight >= 4) { reply({status: "BACKOFF", retry_after: 5}); return false; }
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
    const {token, capacityRetryUntil} = await chrome.storage.local.get(["token", "capacityRetryUntil"]);
    if (popup && message?.type === "STATUS") return {status: token ? "PAIRED" : "PAIR_REQUIRED"};
    const ids = message?.ids;
    if (!page || message?.type !== "CAPACITY" || !Array.isArray(ids) || ids.length < 1 || ids.length > 5
        || ids.some(i => typeof i !== "string" || !/^[0-9]{1,12}$/.test(i)) || new Set(ids).size !== ids.length) return {status: "INVALID"};
    if (orderId && (ids.length !== 1 || ids[0] !== orderId)) return {status: "INVALID"};
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
