const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const C = require("../../chrome-extension/core.js");

test("Swedish numeric parser refuses ambiguous/malformed/zero values", () => {
  assert.equal(C.number("263,36"), 263.36);
  assert.equal(C.number("1\u00a0000"), 1000);
  assert.equal(C.number("1\u202f000,50"), 1000.5);
  for (const text of ["", "-1", "0", "1.234", "1,2,3", "10 00", "NaN", "Infinity"]) assert.equal(C.number(text), null);
});

test("identity and source age are strict", () => {
  assert.equal(C.bookId("https://www.avanza.se/handla/order.html/kop/123?x=1"), "123");
  assert.equal(C.certificateId("/borshandlade-produkter/certifikat-torg/om-certifikatet.html/123/test"), "123");
  assert.equal(C.certificateId("/aktier/om-aktien.html/123/underlying"), null);
  for (const url of ["https://evil.example/handla/order.html/kop/123", "http://avanza.se/handla/order.html/kop/123", "/handla/order.html/kop/１２３"]) assert.equal(C.bookId(url), null);
  assert.equal(C.fresh(10000, 15000), true);
  assert.equal(C.fresh(10000, 15001), false);
  assert.equal(C.fresh(10001, 10000), false);
});

test("bridge quotes require exact units, source age and consistent amount", () => {
  const q = {status: "OK", price: 263.36, volume: 1000, amount: "263360.00", mm_level: 1, currency: "SEK", source_time: 10000};
  assert.equal(C.validQuote(q, 10001), true);
  for (const change of [{currency: "EUR"}, {source_time: 1}, {amount: "100"}, {mm_level: null}, {volume: 1.5}, {price: NaN}]) assert.equal(C.validQuote({...q, ...change}, 10001), false);
});

test("background pins requests and never gives content scripts the token", async () => {
  let handler, access, requested;
  const chrome = {
    storage: {local: {setAccessLevel: async x => {access = x;}, get: async () => ({token: "a".repeat(64)}), set: async () => {}}},
    runtime: {getURL: x => `chrome-extension://${"a".repeat(32)}/${x}`, onMessage: {addListener: fn => {handler = fn;}}}
  };
  vm.runInNewContext(fs.readFileSync("chrome-extension/background.js", "utf8"), {
    chrome, URL, AbortSignal, fetch: async (url, options) => {requested = {url, options}; return {ok: true, json: async () => ({items: {123: {status: "STALE"}}})};}
  });
  const sender = {tab: {id: 1}, frameId: 0, url: "https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html"};
  const send = (m, s = sender) => new Promise(resolve => handler(m, s, resolve));
  assert.equal((await send({type: "CAPACITY", ids: ["123"]})).items[123].status, "STALE");
  assert.equal(access.accessLevel, "TRUSTED_CONTEXTS");
  assert.equal(requested.url, "http://127.0.0.1:18762/capacity");
  assert.equal(requested.options.credentials, "omit");
  assert.equal((await send({type: "CAPACITY", ids: ["../account"]})).status, "INVALID");
  assert.equal((await send({type: "PAIR", code: "A".repeat(20)})).status, "INVALID");
  assert.equal((await send({type: "STATUS"}, {...sender, url: "https://evil.example"})).status, "DENIED");
  const orderSender = {...sender, url: "https://www.avanza.se/handla/order.html/kop/123"};
  assert.equal((await send({type: "CAPACITY", ids: ["123"]}, orderSender)).items[123].status, "STALE");
  assert.equal((await send({type: "CAPACITY", ids: ["456"]}, orderSender)).status, "INVALID");
  assert.equal((await send({type: "CAPACITY", ids: ["123", "456"]}, orderSender)).status, "INVALID");
});

test("passive SSE observer handles split CRLF without requests or credential leakage", async () => {
  const messages = [];
  const payload = {orderbookId: "123", receivedTime: Date.now(), marketMakerLevelInAsk: 1,
    levels: [{sellPrice: 1, sellVolume: 9000000}, {sellPrice: 263.36, sellVolume: 1000}], cookies: "must-not-leave"};
  const raw = `event: ORDER_DEPTH\r\ndata: ${JSON.stringify(payload)}\r\n\r\n`;
  const chunks = raw.split("");
  let calls = 0;
  const window = {fetch: async () => {
    calls++;
    return new Response(new ReadableStream({start(controller) {
      for (const part of chunks) controller.enqueue(new TextEncoder().encode(part));
      controller.close();
    }}), {headers: {"Content-Type": "text/event-stream"}});
  }, postMessage: message => messages.push(message)};
  vm.runInNewContext(fs.readFileSync("chrome-extension/stream-observer.js", "utf8"), {
    window, location: {href: "https://www.avanza.se/handla/order.html/kop/123", origin: "https://www.avanza.se"}, URL, TextDecoder
  });
  const response = await window.fetch("/_push/order-depth-web-push/123");
  assert.equal(await response.text(), raw);
  await new Promise(r => setTimeout(r, 30));
  assert.equal(calls, 1);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].mm_level, 1);
  assert.equal(messages[0].price, 263.36);
  assert.equal(JSON.stringify(messages).includes("must-not-leave"), false);
  await window.fetch("/_api/accounts");
  await new Promise(r => setTimeout(r, 10));
  assert.equal(messages.length, 1);
});
