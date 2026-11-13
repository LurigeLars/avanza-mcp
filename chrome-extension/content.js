(() => {
  const C = globalThis.AvzCapacity;
  const format = new Intl.NumberFormat("sv-SE", {maximumFractionDigits: 2});
  let snapshot = null, orderRoot = null, orderObserver = null, orderPanel = null;
  let listBusy = false, nextPoll = 0, lastRoute = location.href;
  const values = new Map();
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
    display(orderPanel.querySelector("strong"), q, snapshot && !C.fresh(snapshot.source_time) ? "STALE" : "DJUP EJ VERIFIERAT");
  }
  window.addEventListener("message", event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.type !== "AVZ_CAPACITY_DEPTH") return;
    const d = event.data;
    if (d.id !== C.bookId(location.href) || !C.fresh(d.source_time) || !Number.isInteger(d.mm_level)
        || d.mm_level < 0 || d.mm_level > 49 || !Number.isFinite(d.price) || !Number.isSafeInteger(d.volume)) return;
    if (snapshot && snapshot.id === d.id && snapshot.source_time > d.source_time) return;
    snapshot = {id: d.id, source_time: d.source_time, mm_level: d.mm_level, price: d.price, volume: d.volume};
    updateOrder();
  });
  function discoverOrder() {
    const root = C.bookId(location.href) ? [...document.querySelectorAll("aza-order-depth")].find(visible) : null;
    if (root === orderRoot) { updateOrder(); return; }
    orderObserver?.disconnect(); orderPanel?.remove(); orderRoot = root; orderPanel = null;
    if (!root) return;
    orderPanel = document.createElement("aside"); orderPanel.className = "avz-capacity";
    const label = document.createElement("span"); label.textContent = "Max köpbelopp vid MM:s säljkurs (synligt djup)";
    const amount = document.createElement("strong"); amount.setAttribute("role", "status");
    orderPanel.append(label, amount); root.after(orderPanel);
    orderObserver = new MutationObserver(updateOrder);
    orderObserver.observe(root, {subtree: true, childList: true, characterData: true});
    updateOrder();
  }
  function listRows() {
    const table = document.querySelector("aza-certificate-list table");
    if (!table) return [];
    const header = table.querySelector("thead tr");
    if (header && !header.querySelector(".avz-capacity-cell")) {
      const th = document.createElement("th"); th.className = "avz-capacity-cell";
      th.scope = "col"; th.textContent = "MM köpbar SEK";
      th.title = "Synlig volym på MM:s säljkurs. Befintlig spreadsortering behålls.";
      header.append(th);
    }
    const rows = [];
    for (const row of table.querySelectorAll("tbody tr")) {
      const ids = new Set([...row.querySelectorAll("a[href]")].map(a => C.certificateId(a.href)).filter(Boolean));
      if (ids.size !== 1) continue;
      const id = [...ids][0];
      let cell = row.querySelector(".avz-capacity-cell");
      if (!cell) { cell = document.createElement("td"); cell.className = "avz-capacity-cell"; row.append(cell); }
      if (seen.get(row) !== id) { seen.set(row, id); display(cell, null, "HÄMTAR"); }
      const value = values.get(id);
      display(cell, value, "HÄMTAR");
      if (visible(row)) {
        if (rows.length < 20) rows.push({id, row, cell});
        else display(cell, null, "GRÄNS: 20 SYNLIGA RADER");
      }
    }
    return rows.slice(0, 20);
  }
  async function pollList() {
    if (location.pathname !== "/borshandlade-produkter/certifikat-torg/lista.html") return;
    const rows = listRows();
    if (!rows.length || listBusy || performance.now() < nextPoll) return;
    const route = location.href;
    const ids = [...new Set(rows.map(r => r.id))];
    listBusy = true;
    try {
      for (let offset = 0; offset < ids.length; offset += 5) {
        if (document.hidden || location.href !== route) return;
        const visibleIds = new Set(listRows().map(r => r.id));
        const group = ids.slice(offset, offset + 5).filter(id => visibleIds.has(id));
        if (!group.length) continue;
        const result = await chrome.runtime.sendMessage({type: "CAPACITY", ids: group});
        if (location.href !== route || document.hidden) return;
        for (const id of group) {
          const q = result?.items?.[id];
          values.set(id, C.validQuote(q) ? q : {status: q?.status === "OK" ? "STALE" : q?.status ?? result?.status ?? "OFFLINE"});
        }
        listRows();
        if (!result?.items) {
          for (const id of ids) values.set(id, {status: result?.status ?? "OFFLINE"});
          nextPoll = performance.now() + 10000;
          return;
        }
        if (group.some(id => ["RATE_LIMITED", "OFFLINE", "TIMEOUT"].includes(values.get(id)?.status))) {
          nextPoll = performance.now() + 10000;
          return;
        }
      }
      nextPoll = performance.now() + 1000;
    } catch {
      for (const id of ids) values.set(id, {status: "OFFLINE"});
      nextPoll = performance.now() + 10000;
    } finally { listBusy = false; listRows(); }
    while (values.size > 200) values.delete(values.keys().next().value);
  }
  function tick() {
    if (location.href !== lastRoute) {
      snapshot = null; values.clear(); nextPoll = 0; lastRoute = location.href;
    }
    if (document.hidden) {
      snapshot = null; values.clear();
      if (orderPanel) display(orderPanel.querySelector("strong"), null, "PAUSAD");
      return;
    }
    discoverOrder(); pollList();
  }
  document.addEventListener("visibilitychange", tick);
  window.addEventListener("pageshow", () => { snapshot = null; values.clear(); nextPoll = 0; tick(); });
  setInterval(tick, 250);
})();
