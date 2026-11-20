(() => {
  const C = globalThis.AvzCapacity;
  let enabled = false, pollInterval = 60000, generation = 0;
  const format = new Intl.NumberFormat("sv-SE", {maximumFractionDigits: 2});
  let snapshot = null, orderRoot = null, orderObserver = null, orderPanel = null;
  let listBusy = false, nextPoll = 0, lastRoute = location.href;
  let orderBusy = false, nextOrderPoll = 0, orderFailure = "DEPTH_UNVERIFIED";
  const values = new Map();
  const paused = new Map();
  const seen = new WeakMap();
  let shown = new WeakMap();
  function nextRead(retryAfter = 0) {
    return pollInterval === 0 ? Infinity : performance.now() + Math.max(pollInterval, retryAfter * 1000);
  }
  function applyState(state) {
    if (typeof state?.enabled !== "boolean" || ![0, 30000, 60000, 120000, 300000].includes(state.pollInterval)) return;
    const wasEnabled = enabled, changedInterval = pollInterval !== state.pollInterval;
    enabled = state.enabled; pollInterval = state.pollInterval;
    if (!enabled) {
      generation++; snapshot = null; values.clear(); paused.clear(); shown = new WeakMap();
      orderFailure = "DEPTH_UNVERIFIED";
      orderObserver?.disconnect(); orderObserver = null;
      orderPanel?.remove(); orderPanel = null; orderRoot = null;
      document.querySelectorAll(".avz-capacity-cell").forEach(node => node.remove());
    } else if (!wasEnabled) {
      nextPoll = 0; nextOrderPoll = 0; tick();
    } else if (changedInterval) {
      nextPoll = nextRead(); nextOrderPoll = nextRead();
    }
  }
  chrome.runtime.onMessage.addListener((message, sender) => {
    if (sender.id === chrome.runtime.id && message?.type === "STATE_CHANGED") applyState(message);
  });
  chrome.runtime.sendMessage({type: "STATE"}).then(applyState).catch(() => {});
  const visible = element => {
    const r = element.getBoundingClientRect();
    return r.height > 0 && r.width > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth
      && !element.closest('[aria-hidden="true"], [hidden]') && getComputedStyle(element).visibility !== "hidden";
  };
  function text(node, value) { if (node.textContent !== value) node.textContent = value; }
  function display(node, q, failure = "DEPTH_UNVERIFIED", id = null) {
    if (shown.get(node)?.id !== id) shown.delete(node);
    if (failure === "" || q?.status === "MM_MISSING" || failure === "MM_MISSING") {
      shown.delete(node);
      text(node, ""); node.classList.remove("avz-capacity-stale"); node.removeAttribute("title");
      return;
    }
    const status = q?.status === "OK" ? "STALE" : q?.status ?? failure;
    const waiting = ["STALE", "LOADING", "BACKOFF", "DEPTH_UNVERIFIED", "PAUSED"].includes(status);
    if (C.validQuote(q)) shown.set(node, {id, quote: q});
    else if (!waiting) shown.set(node, {id, failure: status});
    const entry = shown.get(node), quote = entry?.quote;
    text(node, quote ? `${format.format(Number(quote.amount))} kr` : entry?.failure ? "— FAILED" : "—");
    node.classList.toggle("avz-capacity-stale", !quote);
    if (quote) node.title = `Last verified: ${quote.volume} units × ${format.format(quote.price)} SEK · book time ${new Date(quote.source_time).toLocaleTimeString("sv-SE")} · MM level ${quote.mm_level + 1}. Visible depth, no guaranteed execution.`;
    else if (entry?.failure) node.title = `Refresh failed: ${entry.failure}.`;
    else node.removeAttribute("title");
  }
  function updateOrder() {
    if (!enabled || !orderRoot?.isConnected || !orderPanel?.isConnected) return;
    const id = C.bookId(location.href);
    const dom = C.readDom(orderRoot);
    const asks = [...orderRoot.querySelectorAll(".price.sell")];
    const noAsk = asks.length > 0 && asks.every(e => /^[—–-]$/.test(e.textContent.trim()));
    const sek = [...document.querySelectorAll("aza-order-market-info-summary .last-price")]
      .some(e => visible(e) && /\bSEK\b/.test(e.textContent));
    let q = null;
    if (id && dom && sek && snapshot?.id === id && C.fresh(snapshot.source_time)
        && snapshot.price === dom.price && snapshot.volume === dom.volume && snapshot.mm_level === dom.mm_level) {
      q = {...dom, source_time: snapshot.source_time, status: "OK"};
    }
    const failure = noAsk ? "" : !dom ? "MM_DEPTH_UNREADABLE" : !sek ? "CURRENCY_UNVERIFIED"
      : snapshot && !C.fresh(snapshot.source_time) ? "STALE"
      : snapshot && dom ? "DEPTH_MISMATCH" : orderFailure;
    display(orderPanel.querySelector("strong"), q, failure, id);
  }
  window.addEventListener("message", event => {
    if (!enabled || event.source !== window || event.origin !== location.origin || event.data?.type !== "AVZ_CAPACITY_DEPTH") return;
    const d = event.data;
    if (d.id !== C.bookId(location.href) || !C.fresh(d.source_time) || !Number.isInteger(d.mm_level)
        || d.mm_level < 0 || d.mm_level > 49 || !Number.isFinite(d.price) || !Number.isSafeInteger(d.volume)) return;
    if (snapshot && snapshot.id === d.id && snapshot.source_time > d.source_time) return;
    snapshot = {id: d.id, source_time: d.source_time, mm_level: d.mm_level, price: d.price, volume: d.volume, via: "stream"};
    updateOrder();
  });
  function discoverOrder() {
    const root = C.bookId(location.href) ? [...document.querySelectorAll("aza-order-depth")].find(visible) : null;
    if (root === orderRoot) { updateOrder(); return; }
    orderObserver?.disconnect(); orderPanel?.remove(); orderRoot = root; orderPanel = null;
    if (!root) return;
    orderPanel = document.createElement("aside"); orderPanel.className = "avz-capacity";
    const label = document.createElement("span"); label.textContent = "Max buy capacity in visible order depth";
    label.title = "Market maker ask price × visible volume at that level. Order depth, not account buying power or guaranteed execution.";
    const amount = document.createElement("strong"); amount.setAttribute("role", "status");
    orderPanel.append(label, amount); root.after(orderPanel);
    orderObserver = new MutationObserver(updateOrder);
    orderObserver.observe(root, {subtree: true, childList: true, characterData: true});
    updateOrder();
  }
  async function pollOrder() {
    const id = C.bookId(location.href);
    if (!enabled || !id || !orderRoot || !C.readDom(orderRoot) || orderBusy || performance.now() < nextOrderPoll
        || (snapshot?.via === "stream" && C.fresh(snapshot.source_time))) return;
    orderBusy = true;
    const currentGeneration = generation;
    try {
      const result = await chrome.runtime.sendMessage({type: "CAPACITY", ids: [id]});
      if (!enabled || currentGeneration !== generation || document.hidden || C.bookId(location.href) !== id) return;
      const q = result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"};
      if (!(snapshot?.via === "stream" && C.fresh(snapshot.source_time))) {
        if (C.validQuote(q)) snapshot = {...q, id, via: "bridge"};
        else { snapshot = null; orderFailure = q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"; }
      }
      nextOrderPoll = nextRead(result?.retry_after);
    } catch {
      if (!enabled || currentGeneration !== generation || document.hidden || C.bookId(location.href) !== id) return;
      if (!(snapshot?.via === "stream" && C.fresh(snapshot.source_time))) {
        snapshot = null; orderFailure = "OFFLINE";
      }
      nextOrderPoll = nextRead();
    } finally { orderBusy = false; updateOrder(); }
  }
  function listRows() {
    if (!enabled) return [];
    const table = document.querySelector("aza-certificate-list table");
    if (!table) return [];
    const header = table.querySelector("thead tr");
    const askColumn = [...(header?.cells ?? [])].findIndex(cell => cell.textContent.trim() === "Sälj");
    if (header && !header.querySelector(".avz-capacity-cell")) {
      const th = document.createElement("th"); th.className = "avz-capacity-cell";
      th.scope = "col"; th.textContent = "Max buy capacity";
      th.title = "Market maker ask price × visible volume at that level, in SEK. Order depth, not account buying power or guaranteed execution.";
      header.append(th);
    }
    const rows = [];
    for (const row of table.querySelectorAll("tbody tr")) {
      const ids = new Set([...row.querySelectorAll("a[href]")].map(a => C.certificateId(a.href)).filter(Boolean));
      if (ids.size !== 1) {
        const oldCell = row.querySelector(".avz-capacity-cell");
        if (oldCell) display(oldCell, null, "IDENTITY_UNKNOWN");
        seen.delete(row);
        continue;
      }
      const id = [...ids][0];
      const ask = askColumn >= 0 && row.cells[askColumn] ? C.number(row.cells[askColumn].textContent) : undefined;
      const pause = paused.get(id);
      if (pause && (ask !== pause.ask || performance.now() >= pause.until)) paused.delete(id);
      let cell = row.querySelector(".avz-capacity-cell");
      if (!cell) { cell = document.createElement("td"); cell.className = "avz-capacity-cell"; row.append(cell); }
      if (seen.get(row) !== id) { seen.set(row, id); display(cell, null, "LOADING"); }
      const value = values.get(id);
      display(cell, value, "LOADING", id);
      if (paused.has(id)) {
        display(cell, null, "");
      }
      if (visible(row)) {
        if (rows.length < 20) rows.push({id, row, cell, ask});
        else display(cell, null, "LIMIT: 20 VISIBLE ROWS");
      }
    }
    return rows.slice(0, 20);
  }
  async function pollList() {
    if (!enabled || location.pathname !== "/borshandlade-produkter/certifikat-torg/lista.html") return;
    const rows = listRows().filter(r => !paused.has(r.id));
    if (!rows.length || listBusy || performance.now() < nextPoll) return;
    const route = location.href, currentGeneration = generation;
    const ids = [...new Set(rows.map(r => r.id))];
    listBusy = true;
    try {
      for (let offset = 0; offset < ids.length; offset += 5) {
        if (!enabled || currentGeneration !== generation || document.hidden || location.href !== route) return;
        const visibleIds = new Set(listRows().filter(r => !paused.has(r.id)).map(r => r.id));
        const group = ids.slice(offset, offset + 5).filter(id => visibleIds.has(id));
        if (!group.length) continue;
        const result = await chrome.runtime.sendMessage({type: "CAPACITY", ids: group});
        if (!enabled || currentGeneration !== generation || location.href !== route || document.hidden) return;
        for (const id of group) {
          const q = result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"};
          values.set(id, C.validQuote(q) ? q : {status: q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"});
          const ask = rows.find(r => r.id === id)?.ask;
          if (q?.status === "MM_MISSING" || (ask === null && ["STALE", "DEPTH_UNKNOWN", "DEPTH_UNAVAILABLE"].includes(q?.status))) {
            paused.set(id, {ask, until: performance.now() + 30000});
          }
        }
        listRows();
        if (!result?.items) {
          for (const id of ids) values.set(id, {status: result?.status ?? "OFFLINE"});
          nextPoll = nextRead(result?.retry_after);
          return;
        }
        if (group.some(id => ["RATE_LIMITED", "AUTH_REQUIRED", "TIMEOUT", "BUSY", "OFFLINE"].includes(values.get(id)?.status))) {
          nextPoll = nextRead(result?.retry_after);
          return;
        }
      }
      nextPoll = nextRead();
    } catch {
      if (!enabled || currentGeneration !== generation || document.hidden || location.href !== route) return;
      for (const id of ids) values.set(id, {status: "OFFLINE"});
      nextPoll = nextRead();
    } finally {
      listBusy = false;
      while (values.size > 200) values.delete(values.keys().next().value);
      while (paused.size > 200) paused.delete(paused.keys().next().value);
      listRows();
    }
  }
  function tick() {
    if (!enabled) return;
    if (location.href !== lastRoute) {
      shown = new WeakMap(); snapshot = null; values.clear(); paused.clear(); nextPoll = 0; nextOrderPoll = 0; orderFailure = "DEPTH_UNVERIFIED"; lastRoute = location.href;
    }
    if (document.hidden) {
      shown = new WeakMap(); snapshot = null; values.clear(); paused.clear();
      if (orderPanel) display(orderPanel.querySelector("strong"), null, "PAUSED");
      return;
    }
    discoverOrder(); pollOrder(); pollList();
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { nextPoll = 0; nextOrderPoll = 0; }
    tick();
  });
  window.addEventListener("pageshow", () => { shown = new WeakMap(); snapshot = null; values.clear(); paused.clear(); nextPoll = 0; nextOrderPoll = 0; tick(); });
  setInterval(tick, 250);
})();
