const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise(resolve => setImmediate(resolve));
async function popup(state = {status: "PAIRED", enabled: true, pollInterval: 60000}, failType) {
  const nodes = Object.fromEntries(["status", "power", "power-label", "interval", "pairing", "pairing-summary", "feedback", "pair-button", "pair", "code"].map(id => [id, {
    textContent: "", value: "", disabled: true, hidden: true, open: false, dataset: {}, attrs: {}, listeners: {},
    setAttribute(k,v) {this.attrs[k]=v;}, addEventListener(k,v) {this.listeners[k]=v;}
  }]));
  const messages = [];
  vm.runInNewContext(fs.readFileSync("chrome-extension/popup.js", "utf8"), {
    document: {getElementById: id => nodes[id]},
    chrome: {runtime: {sendMessage: async message => {
      messages.push(message);
      if (message.type === failType) throw Error("Synthetic failure");
      if (!state) throw Error("Unavailable");
      if (message.type === "SET_SETTINGS") Object.assign(state, Object.fromEntries(Object.entries(message).filter(([k]) => k !== "type")));
      if (message.type === "PAIR") {state.status = "PAIRED"; return {status: "PAIRED"};}
      return {...state};
    }}}
  });
  await flush();
  return {nodes, messages};
}

test("paired popup shows accessible ON and keeps pairing collapsed", async () => {
  const {nodes} = await popup();
  assert.equal(nodes.status.textContent, "Paired");
  assert.equal(nodes.power.attrs["aria-checked"], "true");
  assert.equal(nodes["power-label"].textContent, "ON");
  assert.equal(nodes.power.disabled, false);
  assert.equal(nodes.pairing.hidden, false);
  assert.equal(nodes.pairing.open, false);
});

test("OFF and on-visible interval are saved through the worker", async () => {
  const {nodes, messages} = await popup();
  await nodes.power.listeners.click();
  assert.equal(nodes.power.attrs["aria-checked"], "false");
  assert.equal(nodes["power-label"].textContent, "OFF");
  nodes.interval.value = "0";
  await nodes.interval.listeners.change();
  assert.equal(messages.at(-1).pollInterval, 0);
  assert.equal(nodes.interval.value, "0");
});

test("pairing is expanded only when needed and clears the one-time code", async () => {
  const {nodes, messages} = await popup({status: "PAIR_REQUIRED", enabled: true, pollInterval: 0});
  assert.equal(nodes.pairing.open, true);
  nodes.code.value = "a".repeat(20);
  await nodes.pair.listeners.submit({preventDefault() {}});
  assert.equal(nodes.code.value, "");
  assert.equal(messages.find(m => m.type === "PAIR").code, "A".repeat(20));
  assert.equal(nodes.status.textContent, "Paired");
  assert.equal(nodes.pairing.open, false);
});

test("missing or invalid worker state never enables controls or claims ON", async () => {
  for (const state of [undefined, {status: "OFFLINE"}]) {
    const {nodes} = await popup(state === undefined ? null : state);
    assert.equal(nodes.status.textContent, "Unavailable");
    assert.equal(nodes.power.disabled, true);
    assert.notEqual(nodes["power-label"].textContent, "ON");
  }
});

test("failed settings restore the last confirmed selection and show an English error", async () => {
  const {nodes} = await popup({status: "PAIRED", enabled: true, pollInterval: 60000}, "SET_SETTINGS");
  nodes.interval.value = "0";
  await nodes.interval.listeners.change();
  assert.equal(nodes.interval.value, "60000");
  assert.match(nodes.feedback.textContent, /Could not save settings/);
  assert.equal(nodes.power.disabled, false);
});

test("pairing failure clears the code and leaves setup available", async () => {
  const {nodes} = await popup({status: "PAIR_REQUIRED", enabled: true, pollInterval: 60000}, "PAIR");
  nodes.code.value = "a".repeat(20);
  await nodes.pair.listeners.submit({preventDefault() {}});
  assert.equal(nodes.code.value, "");
  assert.equal(nodes.pairing.open, true);
  assert.match(nodes.feedback.textContent, /Pairing failed/);
});
