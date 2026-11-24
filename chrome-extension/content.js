(() => {
  const C = globalThis.AvzCapacity;
  let enabled = false, pollInterval = 60000, generation = 0;
  const format = new Intl.NumberFormat("sv-SE", {maximumFractionDigits: 2});
  let snapshot = null, orderRoot = null, orderObserver = null, orderPanel = null;
  let listBusy = false, nextPoll = 0, lastRoute = location.href;
  let orderBusy = false, orderReadComplete = false, nextOrderPoll = 0, orderFailure = "DEPTH_UNVERIFIED";
  let listTable = null, listObserver = null, listDirty = true, visibleRows = [];
  let listRetryAt = 0, listFailures = 0, orderRetries = 0;
  const attempts = new Map();
  const values = new Map();
  const paused = new Map();
  const seen = new WeakMap();
  let shown = new WeakMap();
  function nextRead(retryAfter = 0) {
    return pollInterval === 0 ? Infinity : performance.now() + Math.max(pollInterval, retryAfter * 1000);
  }
  const transient = status => ["TIMEOUT", "BUSY", "OFFLINE", "RATE_LIMITED", "BACKOFF", "DEPTH_UNAVAILABLE"].includes(status);
  const retryDelay = (status, retryAfter) => Math.max(status === "RATE_LIMITED" ? 60000 : 30000,
    Number.isFinite(retryAfter) ? retryAfter * 1000 : 0);
  function nextOrderRead(status, retryAfter) {
    return pollInterval === 0 && transient(status) && orderRetries++ === 0
      ? performance.now() + retryDelay(status, retryAfter) : nextRead(retryAfter);
  }
  function resetListPass() {
    attempts.clear(); nextPoll = 0; listRetryAt = 0; listFailures = 0; listDirty = true;
  }
  function resetListView() {
    resetListPass(); listObserver?.disconnect(); listObserver = null; listTable = null; visibleRows = [];
  }
  function applyState(state) {
    if (typeof state?.enabled !== "boolean" || ![0, 30000, 60000, 120000, 300000].includes(state.pollInterval)) return;
    const wasEnabled = enabled, changedInterval = pollInterval !== state.pollInterval;
    enabled = state.enabled; pollInterval = state.pollInterval;
    if (!enabled) {
      generation++; snapshot = null; values.clear(); paused.clear(); shown = new WeakMap(); resetListView(); orderRetries = 0;
      orderFailure = "DEPTH_UNVERIFIED"; orderReadComplete = false;
      orderObserver?.disconnect(); orderObserver = null;
      orderPanel?.remove(); orderPanel = null; orderRoot = null;
      document.querySelectorAll(".avz-capacity-cell").forEach(node => node.remove());
    } else if (!wasEnabled) {
      resetListPass(); nextOrderPoll = 0; orderRetries = 0; tick();
    } else if (changedInterval) {
      nextPoll = nextRead(); nextOrderPoll = nextRead();
      if (listRetryAt === Infinity) listRetryAt = nextPoll;
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
    const current = C.validQuote(q);
    if (current) shown.set(node, {id, quote: q});
    else if (!waiting) shown.set(node, {id, failure: status});
    const entry = shown.get(node), quote = entry?.quote;
    text(node, quote ? `${format.format(Number(quote.amount))} kr` : entry?.failure ? "— FAILED" : "—");
    node.classList.toggle("avz-capacity-stale", !current);
    if (quote) {
      const title = `Last verified: ${quote.volume} units × ${format.format(quote.price)} SEK · book time ${new Date(quote.source_time).toLocaleTimeString("sv-SE")} · MM level ${quote.mm_level + 1}. Visible depth, no guaranteed execution.`
        + (current ? "" : " Not live; retained value.");
      if (node.title !== title) node.title = title;
    }
    else if (entry?.failure) { const title = `Refresh failed: ${entry.failure}.`; if (node.title !== title) node.title = title; }
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
    const failure = noAsk ? "" : !snapshot && !orderReadComplete ? "LOADING"
      : !dom ? "MM_DEPTH_UNREADABLE" : !sek ? "CURRENCY_UNVERIFIED"
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
      const result = await chrome.runtime.sendMessage({type: "CAPACITY", pathname: location.pathname, ids: [id]});
      if (!enabled || currentGeneration !== generation || document.hidden || C.bookId(location.href) !== id) return;
      orderReadComplete = true;
      const q = result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"};
      if (!(snapshot?.via === "stream" && C.fresh(snapshot.source_time))) {
        if (C.validQuote(q)) snapshot = {...q, id, via: "bridge"};
        else { snapshot = null; orderFailure = q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"; }
      }
      nextOrderPoll = nextOrderRead(q?.status, result?.retry_after);
    } catch {
      if (!enabled || currentGeneration !== generation || document.hidden || C.bookId(location.href) !== id) return;
      orderReadComplete = true;
      if (!(snapshot?.via === "stream" && C.fresh(snapshot.source_time))) {
        snapshot = null; orderFailure = "OFFLINE";
      }
      nextOrderPoll = nextOrderRead("OFFLINE");
    } finally { orderBusy = false; updateOrder(); }
  }
  function listRows() {
    if (!enabled || document.hidden || location.pathname !== "/borshandlade-produkter/certifikat-torg/lista.html") return [];
    const table = document.querySelector("aza-certificate-list table");
    if (table !== listTable) {
      listObserver?.disconnect(); listTable = table; listDirty = true; visibleRows = [];
      if (table) {
        const own = node => (node.nodeType === 1 ? node : node.parentElement)?.closest(".avz-capacity-cell");
        listObserver = new MutationObserver(records => {
          // Rendering our own cells must not invalidate the geometry cache.
          if (records.some(r => !own(r.target) && (r.type !== "childList"
              || [...r.addedNodes, ...r.removedNodes].some(n => !own(n))))) listDirty = true;
        });
        listObserver.observe(table, {subtree: true, childList: true, characterData: true,
          attributes: true, attributeFilter: ["href", "class", "style", "hidden", "aria-hidden"]});
      }
    }
    if (!table) return [];
    if (!listDirty) return visibleRows;
    listDirty = false;
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
      let cell = row.querySelector(".avz-capacity-cell");
      if (!cell) { cell = document.createElement("td"); cell.className = "avz-capacity-cell"; row.append(cell); }
      if (seen.get(row) !== id) { seen.set(row, id); display(cell, null, "LOADING", id); }
      if (visible(row)) rows.push({id, row, cell, ask});
    }
    return visibleRows = rows;
  }
  function renderList(rows) {
    for (const {id, cell, ask} of rows) {
      const pause = paused.get(id);
      if (pause && (ask !== pause.ask || performance.now() >= pause.until)) paused.delete(id);
      display(cell, paused.has(id) ? null : values.get(id), paused.has(id) ? "" : "LOADING", id);
    }
  }
  async function pollList() {
    if (!enabled || document.hidden || location.pathname !== "/borshandlade-produkter/certifikat-torg/lista.html") return;
    const rows = listRows();
    renderList(rows);
    if (!rows.length || listBusy || performance.now() < listRetryAt) return;
    const now = performance.now();
    if (now >= nextPoll) { attempts.clear(); nextPoll = Infinity; listFailures = 0; }
    const group = [...new Set(rows.map(r => r.id))].filter(id => !paused.has(id) && (attempts.get(id)?.due ?? 0) <= now)
      .sort((a, b) => (attempts.get(a)?.count ?? 0) - (attempts.get(b)?.count ?? 0)).slice(0, 5);
    if (!group.length) { if (nextPoll === Infinity) nextPoll = nextRead(); return; }
    const route = location.href, currentGeneration = generation;
    listBusy = true;
    try {
      let result;
      try { result = await chrome.runtime.sendMessage({type: "CAPACITY", pathname: location.pathname, ids: group}); }
      catch { result = {status: "OFFLINE"}; }
      if (!enabled || currentGeneration !== generation || document.hidden || location.href !== route) return;
      const statuses = [];
      for (const id of group) {
        const q = result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"};
        const status = C.validQuote(q) ? "OK" : q?.status === "OK" ? "STALE" : q?.status ?? "OFFLINE";
        statuses.push(status);
        values.set(id, status === "OK" ? q : {status});
        const count = (attempts.get(id)?.count ?? 0) + 1;
        attempts.set(id, {count, due: transient(status) && count < 2
          ? performance.now() + retryDelay(status, result?.retry_after) : Infinity});
        const ask = rows.find(r => r.id === id)?.ask;
        if (status === "MM_MISSING" || (ask === null && ["STALE", "DEPTH_UNKNOWN", "DEPTH_UNAVAILABLE"].includes(status))) {
          paused.set(id, {ask, until: performance.now() + 30000});
        }
      }
      const terminal = statuses.find(s => ["AUTH_REQUIRED", "PAIR_REQUIRED", "INVALID", "INVALID_SIZE", "DENIED"].includes(s));
      const globalPause = statuses.includes("RATE_LIMITED") || result?.retry_after > 0
        || statuses.every(s => ["TIMEOUT", "BUSY", "OFFLINE", "BACKOFF"].includes(s));
      if (terminal && statuses.every(s => s === terminal)) {
        for (const {id} of rows) if (!attempts.has(id)) values.set(id, {status: terminal});
        listRetryAt = nextPoll = nextRead();
      } else if (globalPause) {
        const delay = retryDelay(statuses.includes("RATE_LIMITED") ? "RATE_LIMITED" : "TIMEOUT", result?.retry_after);
        // On-visible mode gets one recovery opportunity, not an endless retry loop.
        if (++listFailures >= 2) listRetryAt = nextPoll = nextRead(delay / 1000);
        else { listRetryAt = performance.now() + delay; nextPoll = Infinity; }
      } else listFailures = 0;
      // Keep the cache bounded without evicting currently visible rows.
      const visibleIds = new Set(rows.map(r => r.id));
      for (const cache of [values, paused, attempts]) {
        for (const id of cache.keys()) {
          if (cache.size <= 200) break;
          if (!visibleIds.has(id)) cache.delete(id);
        }
      }
      renderList(listRows());
    } finally {
      // Serial batches, with a one-second gap even if the bridge responds instantly.
      setTimeout(() => { listBusy = false; }, 1000);
    }
  }
  function tick() {
    if (!enabled) return;
    if (location.href !== lastRoute) {
      generation++; shown = new WeakMap(); snapshot = null; values.clear(); paused.clear(); resetListView(); nextOrderPoll = 0; orderRetries = 0; orderFailure = "DEPTH_UNVERIFIED"; orderReadComplete = false; lastRoute = location.href;
    }
    if (document.hidden) {
      shown = new WeakMap(); snapshot = null; values.clear(); paused.clear(); resetListView(); orderRetries = 0; orderFailure = "DEPTH_UNVERIFIED"; orderReadComplete = false;
      if (orderPanel) display(orderPanel.querySelector("strong"), null, "PAUSED");
      return;
    }
    discoverOrder(); pollOrder(); pollList();
  }
  document.addEventListener("visibilitychange", () => {
    generation++;
    if (!document.hidden) { resetListPass(); nextOrderPoll = 0; orderRetries = 0; }
    tick();
  });
  window.addEventListener("pageshow", () => { generation++; shown = new WeakMap(); snapshot = null; values.clear(); paused.clear(); resetListView(); orderFailure = "DEPTH_UNVERIFIED"; orderReadComplete = false; nextOrderPoll = 0; orderRetries = 0; tick(); });
  window.addEventListener("scroll", () => { listDirty = true; }, {capture: true, passive: true});
  window.addEventListener("resize", () => { listDirty = true; }, {passive: true});
  setInterval(tick, 250);
})();
