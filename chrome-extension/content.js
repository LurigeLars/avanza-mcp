(() => {
  const C = globalThis.AvzCapacity;
  const format = new Intl.NumberFormat("sv-SE", {maximumFractionDigits: 2});
  let snapshot = null, orderRoot = null, orderObserver = null, orderPanel = null;
  let listBusy = false, nextPoll = 0, lastRoute = location.href;
  let orderBusy = false, nextOrderPoll = 0, orderFailure = "DJUP EJ VERIFIERAT";
  const values = new Map();
  const paused = new Map();
  const seen = new WeakMap();
  const visible = element => {
    const r = element.getBoundingClientRect();
    return r.height > 0 && r.width > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth
      && !element.closest('[aria-hidden="true"], [hidden]') && getComputedStyle(element).visibility !== "hidden";
  };
  function text(node, value) { if (node.textContent !== value) node.textContent = value; }
  function display(node, q, failure = "SAKNAR VERIFIERAT DJUP") {
    const ok = C.validQuote(q);
    text(node, ok ? `${format.format(Number(q.amount))} kr` : `— ${q?.status === "OK" ? "STALE" : q?.status ?? failure}`);
    node.classList.toggle("avz-capacity-stale", !ok);
    node.title = ok ? `${q.volume} st × ${format.format(q.price)} SEK · boktid ${new Date(q.source_time).toLocaleTimeString("sv-SE")} · MM-nivå ${q.mm_level + 1}. Synligt djup, ingen garanterad fill.` : "Belopp dolt: saknat, gammalt eller obekräftat underlag.";
  }
  function updateOrder() {
    if (!orderRoot?.isConnected || !orderPanel?.isConnected) return;
    const id = C.bookId(location.href);
    const dom = C.readDom(orderRoot);
    const sek = [...document.querySelectorAll("aza-order-market-info-summary .last-price")]
      .some(e => visible(e) && /\bSEK\b/.test(e.textContent));
    let q = null;
    if (id && dom && sek && snapshot?.id === id && C.fresh(snapshot.source_time)
        && snapshot.price === dom.price && snapshot.volume === dom.volume && snapshot.mm_level === dom.mm_level) {
      q = {...dom, source_time: snapshot.source_time, status: "OK"};
    }
    const failure = !dom ? "MM-DJUP EJ LÄSBART" : !sek ? "VALUTA EJ VERIFIERAD"
      : snapshot && !C.fresh(snapshot.source_time) ? "STALE"
      : snapshot && dom ? "SIDANS DJUP AVVIKER" : orderFailure;
    display(orderPanel.querySelector("strong"), q, failure);
  }
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.type !== "AVZ_CAPACITY_DEPTH") return;
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
    const label = document.createElement("span"); label.textContent = "Max köpbelopp i synligt orderdjup";
    label.title = "Market makerns säljkurs × synligt antal på den nivån. Avser orderdjup, inte kontots köpkraft eller garanterad avslutsvolym.";
    const amount = document.createElement("strong"); amount.setAttribute("role", "status");
    orderPanel.append(label, amount); root.after(orderPanel);
    orderObserver = new MutationObserver(updateOrder);
    orderObserver.observe(root, {subtree: true, childList: true, characterData: true});
    updateOrder();
  }
  async function pollOrder() {
    const id = C.bookId(location.href);
    if (!id || !orderRoot || !C.readDom(orderRoot) || orderBusy || performance.now() < nextOrderPoll
        || (snapshot?.via === "stream" && C.fresh(snapshot.source_time))) return;
    orderBusy = true;
    try {
      const result = await chrome.runtime.sendMessage({type: "CAPACITY", ids: [id]});
      if (document.hidden || C.bookId(location.href) !== id) return;
      const q = C.keepFresh(snapshot, result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"});
      if (!(snapshot?.via === "stream" && C.fresh(snapshot.source_time))) {
        if (C.validQuote(q)) snapshot = {...q, id, via: "bridge"};
        else { snapshot = null; orderFailure = q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"; }
      }
      nextOrderPoll = performance.now() + 10000;
    } catch {
      orderFailure = "OFFLINE"; nextOrderPoll = performance.now() + 10000;
    } finally { orderBusy = false; updateOrder(); }
  }
  function listRows() {
    const table = document.querySelector("aza-certificate-list table");
    if (!table) return [];
    const header = table.querySelector("thead tr");
    const askColumn = [...(header?.cells ?? [])].findIndex(cell => cell.textContent.trim() === "Sälj");
    if (header && !header.querySelector(".avz-capacity-cell")) {
      const th = document.createElement("th"); th.className = "avz-capacity-cell";
      th.scope = "col"; th.textContent = "Max köpbelopp";
      th.title = "Market makerns säljkurs × synligt antal på den nivån, i SEK. Avser orderdjup, inte kontots köpkraft eller garanterad avslutsvolym.";
      header.append(th);
    }
    const rows = [];
    for (const row of table.querySelectorAll("tbody tr")) {
      const ids = new Set([...row.querySelectorAll("a[href]")].map(a => C.certificateId(a.href)).filter(Boolean));
      if (ids.size !== 1) {
        const oldCell = row.querySelector(".avz-capacity-cell");
        if (oldCell) display(oldCell, null, "IDENTITET OKÄND");
        seen.delete(row);
        continue;
      }
      const id = [...ids][0];
      const ask = askColumn >= 0 && row.cells[askColumn] ? C.number(row.cells[askColumn].textContent) : undefined;
      const pause = paused.get(id);
      if (pause && (ask !== pause.ask || performance.now() >= pause.until)) paused.delete(id);
      let cell = row.querySelector(".avz-capacity-cell");
      if (!cell) { cell = document.createElement("td"); cell.className = "avz-capacity-cell"; row.append(cell); }
      if (seen.get(row) !== id) { seen.set(row, id); display(cell, null, "HÄMTAR"); }
      const value = values.get(id);
      display(cell, value, "HÄMTAR");
      if (paused.has(id)) {
        text(cell, paused.get(id).label);
        cell.title = "Hämtningen är pausad för denna rad. Ny kontroll inom 30 sekunder, eller vid nästa hämtning om listans säljkurs ändras.";
      }
      if (visible(row)) {
        if (rows.length < 20) rows.push({id, row, cell, ask});
        else display(cell, null, "GRÄNS: 20 SYNLIGA RADER");
      }
    }
    return rows.slice(0, 20);
  }
  async function pollList() {
    if (location.pathname !== "/borshandlade-produkter/certifikat-torg/lista.html") return;
    const rows = listRows().filter(r => !paused.has(r.id));
    if (!rows.length || listBusy || performance.now() < nextPoll) return;
    const route = location.href;
    const ids = [...new Set(rows.map(r => r.id))];
    listBusy = true;
    try {
      for (let offset = 0; offset < ids.length; offset += 5) {
        if (document.hidden || location.href !== route) return;
        const visibleIds = new Set(listRows().filter(r => !paused.has(r.id)).map(r => r.id));
        const group = ids.slice(offset, offset + 5).filter(id => visibleIds.has(id));
        if (!group.length) continue;
        const result = await chrome.runtime.sendMessage({type: "CAPACITY", ids: group});
        if (location.href !== route || document.hidden) return;
        for (const id of group) {
          const q = C.keepFresh(values.get(id), result?.items?.[id] ?? {status: result?.status ?? "OFFLINE"});
          values.set(id, C.validQuote(q) ? q : {status: q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"});
          const ask = rows.find(r => r.id === id)?.ask;
          if (q?.status === "MM_MISSING" || (ask === null && ["STALE", "DEPTH_UNKNOWN", "DEPTH_UNAVAILABLE"].includes(q?.status))) {
            paused.set(id, {ask, until: performance.now() + 30000,
              label: q.status === "MM_MISSING" ? "— MM SAKNAS (PAUS)" : "— SÄLJKURS SAKNAS (PAUS)"});
          }
        }
        listRows();
        if (!result?.items) {
          for (const id of ids) values.set(id, {status: result?.status ?? "OFFLINE"});
          nextPoll = performance.now() + 10000;
          return;
        }
        if (group.some(id => ["RATE_LIMITED", "AUTH_REQUIRED"].includes(values.get(id)?.status))) {
          const status = group.map(id => values.get(id)?.status).find(s => ["RATE_LIMITED", "AUTH_REQUIRED"].includes(s));
          for (const id of ids) if (!values.has(id)) values.set(id, {status});
          nextPoll = performance.now() + 10000;
          return;
        }
      }
      nextPoll = performance.now() + 10000;
    } catch {
      for (const id of ids) values.set(id, {status: "OFFLINE"});
      nextPoll = performance.now() + 10000;
    } finally {
      listBusy = false;
      while (values.size > 200) values.delete(values.keys().next().value);
      while (paused.size > 200) paused.delete(paused.keys().next().value);
      listRows();
    }
  }
  function tick() {
    if (location.href !== lastRoute) {
      snapshot = null; values.clear(); paused.clear(); nextPoll = 0; nextOrderPoll = 0; orderFailure = "DJUP EJ VERIFIERAT"; lastRoute = location.href;
    }
    if (document.hidden) {
      snapshot = null; values.clear(); paused.clear();
      if (orderPanel) display(orderPanel.querySelector("strong"), null, "PAUSAD");
      return;
    }
    discoverOrder(); pollOrder(); pollList();
  }
  document.addEventListener("visibilitychange", tick);
  window.addEventListener("pageshow", () => { snapshot = null; values.clear(); paused.clear(); nextPoll = 0; tick(); });
  setInterval(tick, 250);
})();
