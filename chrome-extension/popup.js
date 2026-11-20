const status = document.getElementById("status");
const power = document.getElementById("power");
const interval = document.getElementById("interval");
const pairing = document.getElementById("pairing");
const feedback = document.getElementById("feedback");
const pairButton = document.getElementById("pair-button");
let enabled, savedState, initialized = false;
function busy(value) { power.disabled = interval.disabled = pairButton.disabled = value; }
function render(state) {
  if (typeof state?.enabled !== "boolean" || ![0, 30000, 60000, 120000, 300000].includes(state.pollInterval)
      || !["PAIRED", "PAIR_REQUIRED"].includes(state.status)) throw Error("Invalid state");
  enabled = state.enabled;
  savedState = state;
  power.setAttribute("aria-checked", String(enabled));
  document.getElementById("power-label").textContent = enabled ? "ON" : "OFF";
  interval.value = String(state.pollInterval);
  const paired = state.status === "PAIRED";
  status.textContent = paired ? "Paired" : "Not paired";
  status.dataset.paired = String(paired);
  pairing.hidden = false;
  if (!initialized) pairing.open = !paired;
  document.getElementById("pairing-summary").textContent = paired ? "Pair bridge again" : "Pair local bridge";
  initialized = true;
}
async function change(settings) {
  const focused = document.activeElement;
  busy(true); feedback.textContent = "";
  try { render(await chrome.runtime.sendMessage({type: "SET_SETTINGS", ...settings})); }
  catch { render(savedState); feedback.textContent = "Could not save settings. Reopen the popup to check the current state."; }
  finally {
    busy(false);
    if (focused === power && document.activeElement === document.body) power.focus();
  }
}
power.addEventListener("click", () => change({enabled: !enabled}));
interval.addEventListener("change", () => change({pollInterval: Number(interval.value)}));
chrome.runtime.sendMessage({type: "STATUS"}).then(state => { render(state); busy(false); })
  .catch(() => { status.textContent = "Unavailable"; feedback.textContent = "Could not load settings. Reopen the popup to retry."; });
document.getElementById("pair").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("code");
  const code = input.value.trim().toUpperCase(); input.value = "";
  busy(true); feedback.textContent = "";
  try {
    const result = await chrome.runtime.sendMessage({type: "PAIR", code});
    if (result?.status !== "PAIRED") throw Error("Pairing failed");
    render(await chrome.runtime.sendMessage({type: "STATUS"}));
    pairing.open = false;
  } catch { feedback.textContent = "Pairing failed. Check the bridge and use a new one-time code."; }
  finally { busy(false); }
});
