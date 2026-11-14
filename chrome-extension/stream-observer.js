/* Observe only Avanza's existing depth fetch. Never create a request or read headers. */
(() => {
  const original = window.fetch;
  window.fetch = async function (...args) {
    const response = await Reflect.apply(original, this, args);
    try {
      const url = new URL(typeof args[0] === "string" || args[0] instanceof URL ? args[0] : args[0].url, location.href);
      if (url.origin !== location.origin || !/^\/_push\/order-depth-web-push\/[0-9]{0,12}$/.test(url.pathname)
          || !response.ok || !response.headers.get("content-type")?.startsWith("text/event-stream")) return response;
      observe(response.clone()).catch(() => {});
    } catch { /* A changed response shape must leave the original request untouched. */ }
    return response;
  };
  async function observe(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer = (buffer + decoder.decode(value, {stream: true})).replace(/\r\n/g, "\n");
        let end;
        while ((end = buffer.indexOf("\n\n")) >= 0) {
          if (end + 2 > 65536) return;
          const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
          if (!/^event:\s*ORDER_DEPTH\s*$/m.test(frame)) continue;
          try {
            const raw = JSON.parse(frame.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trimStart()).join("\n"));
            if (!/^[0-9]{1,12}$/.test(String(raw.orderbookId)) || !Array.isArray(raw.levels) || raw.levels.length > 50) continue;
            const i = raw.marketMakerLevelInAsk, side = raw.levels[i];
            // Only public depth scalars cross into the isolated content script.
            window.postMessage({type: "AVZ_CAPACITY_DEPTH", id: String(raw.orderbookId),
              source_time: raw.receivedTime, mm_level: i,
              price: side?.sellPrice, volume: side?.sellVolume}, location.origin);
          } catch { /* Invalid frames cannot refresh freshness. */ }
        }
        if (buffer.length > 65536) return;
      }
    } finally { await reader.cancel().catch(() => {}); }
  }
})();
