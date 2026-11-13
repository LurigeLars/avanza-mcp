const status = document.getElementById("status");
chrome.runtime.sendMessage({type: "STATUS"}).then(r => { status.textContent = r.status; });
document.getElementById("pair").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("code");
  const code = input.value.trim().toUpperCase(); input.value = "";
  try { status.textContent = (await chrome.runtime.sendMessage({type: "PAIR", code})).status; }
  catch { status.textContent = "OFFLINE"; }
});
