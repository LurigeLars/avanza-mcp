/* Shared pure validation; page and bridge data are always untrusted. */
(() => {
  const MAX_AGE = 5000;
  function number(text) {
    const value = String(text).trim().replace(/[\u00a0\u202f]/g, " ");
    if (!/^(?:\d+|\d{1,3}(?: \d{3})+)(?:,\d+)?$/.test(value)) return null;
    const n = Number(value.replace(/ /g, "").replace(",", "."));
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  function bookId(href) {
    try {
      const url = new URL(href, "https://www.avanza.se");
      if (!["www.avanza.se", "avanza.se"].includes(url.hostname) || url.protocol !== "https:") return null;
      return url.pathname.match(/^\/handla\/order\.html\/(?:kop|salj)\/([0-9]{1,12})\/?$/)?.[1] ?? null;
    } catch { return null; }
  }
  function certificateId(href) {
    try {
      const url = new URL(href, "https://www.avanza.se");
      if (!["www.avanza.se", "avanza.se"].includes(url.hostname) || url.protocol !== "https:") return null;
      return bookId(href) ?? url.pathname.match(/^\/borshandlade-produkter\/certifikat-torg\/om-certifikatet\.html\/([0-9]{1,12})\/[^/]+\/?$/)?.[1] ?? null;
    } catch { return null; }
  }
  function fresh(time, now = Date.now()) {
    return Number.isSafeInteger(time) && now >= time && now - time <= MAX_AGE;
  }
  function validQuote(q, now = Date.now()) {
    return q?.status === "OK" && q.currency === "SEK" && fresh(q.source_time, now)
      && Number.isFinite(q.price) && q.price > 0 && Number.isSafeInteger(q.volume) && q.volume > 0
      && Number.isInteger(q.mm_level) && q.mm_level >= 0
      && Number.isFinite(Number(q.amount)) && Number(q.amount) > 0
      && Number(q.amount) <= 90071992547409
      && Math.abs(Number(q.amount) - q.price * q.volume) <= 0.001;
  }
  function readDom(root) {
    const rows = [...root.querySelectorAll('.level[role="row"]')];
    const matches = rows.map((row, i) => ({row, i, price: row.querySelector(".price.sell")?.textContent ?? ""}))
      .filter(r => /\*\s*$/.test(r.price.trim()));
    if (matches.length !== 1) return null;
    const {row, i, price: text} = matches[0];
    const price = number(text.replace(/\*\s*$/, ""));
    const volume = number(row.querySelector(".volume.sell")?.textContent ?? "");
    if (!price || !Number.isSafeInteger(volume)) return null;
    return {price, volume, mm_level: i, amount: String(price * volume), currency: "SEK"};
  }
  globalThis.AvzCapacity = {MAX_AGE, number, bookId, certificateId, fresh, validQuote, readDom};
  if (typeof module !== "undefined") module.exports = globalThis.AvzCapacity;
})();
