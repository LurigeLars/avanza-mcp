const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function initialStatus(response, error) {
  const status = {textContent: "Kontrollerar…"};
  let pending;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../../chrome-extension/popup.js"), "utf8"), {
    document: {getElementById(id) {return id === "status" ? status : {addEventListener() {}};}},
    chrome: {runtime: {sendMessage() {return {then(show) {
      pending = (error ? Promise.reject(error) : Promise.resolve(response)).then(show);
      return pending;
    }};}}}
  });
  await pending.catch(() => {});
  return status.textContent;
}

test("popup shows the initial pairing status", async () => {
  assert.equal(await initialStatus({status: "PAIRED"}), "PAIRED");
});

test("popup shows OFFLINE when the initial worker request fails or returns no response", async () => {
  assert.equal(await initialStatus(undefined, Error("worker unavailable")), "OFFLINE");
  assert.equal(await initialStatus(undefined), "OFFLINE");
});
