const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const sender = {tab: {id: 1}, frameId: 0, url: "https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html"};
const ok = body => ({ok: true, json: async () => body});
function worker(store, clock, fetchResponse) {
  let handler, requests = 0;
  const chrome = {
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
    requests: () => requests
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
