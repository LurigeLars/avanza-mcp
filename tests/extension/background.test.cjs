const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const sender = {tab: {id: 1}, frameId: 0, url: "https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html"};
const ok = body => ({ok: true, json: async () => body});
function worker(store, clock, fetchResponse) {
  let handler, requests = 0;
  const notifications = [], badges = [];
  const chrome = {
    action: {setBadgeText: async x => {badges.push(x.text);}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {}},
    tabs: {query: async () => [{id: 1}, {id: 2}], sendMessage: async (id, message) => {notifications.push({id, message});}},
    storage: {local: {
      setAccessLevel: async () => {}, get: async () => ({...store}),
      set: async value => {Object.assign(store, value);}
    }},
    runtime: {getURL: p => `chrome-extension://${"a".repeat(32)}/${p}`, onMessage: {addListener: fn => {handler = fn;}}}
  };
  vm.runInNewContext(fs.readFileSync("chrome-extension/background.js", "utf8"), {
    chrome, URL, AbortSignal, Date: {now: () => clock.now},
    fetch: (...args) => {requests++; return fetchResponse(...args);}
  });
  return {
    send: (s = sender) => new Promise(resolve => handler({type: "CAPACITY", ids: ["123"]}, s, resolve)),
    message: (message, s = sender) => new Promise(resolve => handler(message, s, resolve)),
    notifications, badges, requests: () => requests
  };
}

test("a provider rate limit pauses every tab and survives a worker restart", async () => {
  const clock = {now: 100000}, store = {token: "a".repeat(64)};
  const first = worker(store, clock, async () => ok({items: {123: {status: "RATE_LIMITED"}}}));
  assert.equal((await first.send()).retry_after, 60);
  assert.equal((await first.send({...sender, tab: {id: 2}})).status, "BACKOFF");
  assert.equal(first.requests(), 1);
  assert.deepEqual(Object.keys(store).sort(), ["capacityRetryUntil", "token"]);
  const restarted = worker(store, clock, async () => ok({items: {123: {status: "OK"}}}));
  assert.equal((await restarted.send()).status, "BACKOFF");
  assert.equal(restarted.requests(), 0);
  clock.now += 60001;
  assert.equal((await restarted.send()).items[123].status, "OK");
  assert.equal(restarted.requests(), 1);
});

test("TIMEOUT and BUSY cause a shared thirty-second pause", async () => {
  for (const status of ["TIMEOUT", "BUSY"]) {
    const clock = {now: 100000}, store = {token: "a".repeat(64)};
    const w = worker(store, clock, async () => ok({items: {123: {status}}}));
    const result = await w.send();
    assert.equal(result.items[123].status, status);
    assert.equal(result.retry_after, 30);
    assert.equal((await w.send({...sender, tab: {id: 2}})).status, "BACKOFF");
    assert.equal(w.requests(), 1);
    clock.now += 30001;
    await w.send();
    assert.equal(w.requests(), 2);
  }
});

test("HTTP 429 and network failure also trigger a pause", async () => {
  for (const [fetchResponse, status, seconds] of [
    [async () => ({ok: false, status: 429}), "RATE_LIMITED", 60],
    [async () => {throw Error("synthetic disconnect");}, "OFFLINE", 30]
  ]) {
    const w = worker({token: "a".repeat(64)}, {now: 100000}, fetchResponse);
    const result = await w.send();
    assert.equal(result.status, status);
    assert.equal(result.retry_after, seconds);
    assert.equal((await w.send()).status, "BACKOFF");
    assert.equal(w.requests(), 1);
  }
});

test("the fifth concurrent request waits without issuing another fetch", async () => {
  const waiting = [];
  const w = worker({token: "a".repeat(64)}, {now: 100000}, () => new Promise(resolve => waiting.push(resolve)));
  const pending = Array.from({length: 4}, () => w.send());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(waiting.length, 4);
  assert.equal((await w.send()).status, "BACKOFF");
  assert.equal(w.requests(), 4);
  waiting.forEach(resolve => resolve(ok({items: {123: {status: "OK"}}})));
  await Promise.all(pending);
});

const popup = {url: `chrome-extension://${"a".repeat(32)}/popup.html`};
test("public settings persist, update the badge and notify tabs without sharing credentials", async () => {
  const store = {token: "a".repeat(64)}, clock = {now: 100000};
  const w = worker(store, clock, async () => ok({}));
  const state = await w.message({type: "STATE"});
  assert.equal(state.enabled, true);
  assert.equal(state.pollInterval, 60000);
  assert.equal(state.token, undefined);
  const changed = await w.message({type: "SET_SETTINGS", enabled: false, pollInterval: 0}, popup);
  assert.equal(changed.enabled, false);
  assert.equal(changed.pollInterval, 0);
  assert.equal(w.badges.at(-1), "OFF");
  assert.equal(w.notifications.length, 2);
  assert.equal(w.notifications[0].message.type, "STATE_CHANGED");
  assert.equal(w.notifications[0].message.token, undefined);
  assert.equal((await w.send()).status, "OFF");
  assert.equal(w.requests(), 0);
  const restarted = worker(store, clock, async () => ok({}));
  assert.equal((await restarted.message({type: "STATUS"}, popup)).enabled, false);
  assert.equal((await restarted.message({type: "STATE"})).pollInterval, 0);
  assert.equal(restarted.badges.at(-1), "OFF");
  await restarted.message({type: "SET_SETTINGS", enabled: true}, popup);
  assert.equal(restarted.badges.at(-1), "ON");
  assert.equal(store.token, "a".repeat(64));
});

test("only the popup can change settings, with bounded values and strict types", async () => {
  const store = {}, w = worker(store, {now: 100000}, async () => ok({}));
  for (const change of [{enabled: false}, {pollInterval: 0}]) {
    assert.equal((await w.message({type: "SET_SETTINGS", ...change})).status, "INVALID");
  }
  for (const change of [{enabled: "false"}, {pollInterval: "60000"}, {pollInterval: -1}, {pollInterval: 1}, {}]) {
    assert.equal((await w.message({type: "SET_SETTINGS", ...change}, popup)).status, "INVALID");
  }
  assert.deepEqual(store, {});
});

test("OFF remains available while all four capacity requests are busy", async () => {
  const waiting = [], w = worker({token: "a".repeat(64)}, {now: 100000}, () => new Promise(resolve => waiting.push(resolve)));
  const pending = Array.from({length: 4}, () => w.send());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await w.message({type: "SET_SETTINGS", enabled: false}, popup)).enabled, false);
  waiting.forEach(resolve => resolve(ok({items: {}})));
  await Promise.all(pending);
});

test("current isolated content-script route survives same-document navigation", async () => {
  const w = worker({token: "a".repeat(64)}, {now: 100000}, async () => ok({items: {123: {status: "OK"}}}));
  const initial = {...sender, url: "https://www.avanza.se/hem/hem.html"};
  const list = {type: "CAPACITY", pathname: "/borshandlade-produkter/certifikat-torg/lista.html", ids: ["123"]};
  const order = {type: "CAPACITY", pathname: "/handla/order.html/kop/123", ids: ["123"]};
  assert.equal((await w.message(list, initial)).items[123].status, "OK");
  assert.equal((await w.message(order, initial)).items[123].status, "OK");
  assert.equal((await w.message(list, {...sender, url: "https://www.avanza.se/handla/order.html/kop/456"})).items[123].status, "OK");
  assert.equal((await w.message({...order, ids: ["456"]}, initial)).status, "INVALID");
  assert.equal((await w.message({...order, ids: ["123", "456"]}, initial)).status, "INVALID");
  assert.equal((await w.message({...list, pathname: "/hem/hem.html"}, sender)).status, "INVALID");
  assert.equal((await w.message({...list, pathname: "https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html"}, sender)).status, "INVALID");
  assert.equal((await w.message({...list, pathname: null}, sender)).status, "INVALID");
  assert.equal((await w.message(list, {...initial, frameId: 1})).status, "DENIED");
  assert.equal((await w.message(list, {...initial, url: "https://example.com/"})).status, "DENIED");
  assert.equal(w.requests(), 3);
});

test("bridge HTTP error status is preserved without exposing arbitrary text", async () => {
  for (const [http, body, expected, seconds] of [
    [429, {status: "BUSY"}, "BUSY", 30],
    [504, {status: "TIMEOUT"}, "TIMEOUT", 30],
    [400, {status: "INVALID"}, "INVALID", undefined],
    [403, {status: "DENIED"}, "DENIED", undefined],
    [503, {status: "secret"}, "OFFLINE", 30]
  ]) {
    const w = worker({token: "a".repeat(64)}, {now: 100000}, async () => ({ok: false, status: http, json: async () => body}));
    const result = await w.send();
    assert.equal(result.status, expected);
    assert.equal(result.retry_after, seconds);
  }
});
test("a mixed batch timeout does not stop healthy instruments, but a provider limit does", async () => {
  for (const [status, expected] of [["TIMEOUT", undefined], ["RATE_LIMITED", 60]]) {
    const w = worker({token: "a".repeat(64)}, {now: 100000}, async () =>
      ok({items: {123: {status: "OK"}, 456: {status}}}));
    const result = await w.message({type: "CAPACITY", ids: ["123", "456"]});
    assert.equal(result.items[123].status, "OK");
    assert.equal(result.retry_after, expected);
  }
});
