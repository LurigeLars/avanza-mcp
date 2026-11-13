"""Isolated Chromium acceptance test; synthetic pages/data, no real account.

Run: uv run --with playwright==1.58.0 python tests/browser/capacity_display.py
Install that Playwright's Chromium first, or set CAPACITY_TEST_CHROMIUM.
"""
import asyncio
import json
import os
from pathlib import Path
import statistics
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright, expect
import uvicorn

from avanza_mcp.capacity_bridge import Pairing, create_app

ROOT = Path(__file__).resolve().parents[2]


class MemoryStore:
    value = None
    def get_password(self, *_): return self.value
    def set_password(self, *args): self.value = args[-1]


class SyntheticReader:
    calls = 0
    requested = set()
    async def batch(self, ids):
        self.calls += 1
        self.requested.update(ids)
        now = int(time.time() * 1000)
        return {"items": {i: {"status": "OK", "amount": "263360.00", "price": 263.36,
                              "volume": 1000, "mm_level": 1, "currency": "SEK",
                              "source_time": now} for i in ids}, "server_time": now}


ORDER = """<!doctype html><meta charset=utf-8>
<aza-order-market-info-summary><div class=last-price>263,36 SEK</div></aza-order-market-info-summary>
<input id=amount value=""><input id=quantity value=""><button id=review>Granska köp</button>
<aza-order-depth><div role=table aria-label=Orderdjup>
<div class=level role=row><div class="price sell">1,00</div><div class="volume sell">9 000 000</div></div>
<div class=level role=row><div class="price sell">263,36 *</div><div class="volume sell">1 000</div></div>
</div></aza-order-depth>
<script>window.orderClicks=0;document.getElementById('review').onclick=()=>window.orderClicks++;</script>"""
LIST = """<!doctype html><meta charset=utf-8><aza-certificate-list>
<table><thead><tr><th>Namn</th><th>Spread</th></tr></thead><tbody>
<tr><td><a href=/handla/order.html/kop/123>Test A</a></td><td>1,1%</td></tr>
<tr><td><a href=/borshandlade-produkter/certifikat-torg/om-certifikatet.html/456/test-b>Test B</a><a href=/aktier/om-aktien.html/789/underlying>Underliggande</a></td><td>1,2%</td></tr>
<tr style="transform:translateY(2000px)"><td><a href=/handla/order.html/kop/999>Utanför vyn</a></td><td>2%</td></tr>
</tbody></table></aza-certificate-list>"""


def main():
    store, reader = Pairing(MemoryStore()), SyntheticReader()
    server = uvicorn.Server(uvicorn.Config(create_app(reader, store), host="127.0.0.1",
                                           port=18762, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "Test bridge failed to bind"
        time.sleep(0.01)
    try:
        with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
            extension = ROOT / "chrome-extension"
            context = p.chromium.launch_persistent_context(
                profile, headless=True, channel="chromium",
                executable_path=os.environ.get("CAPACITY_TEST_CHROMIUM"),
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
                viewport={"width": 1280, "height": 900})
            try:
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                extension_id = worker.url.split("/")[2]
                popup = context.new_page()
                popup.goto(f"chrome-extension://{extension_id}/popup.html")
                popup.locator("#code").fill(store.issue())
                popup.locator("button").click()
                expect(popup.locator("#status")).to_have_text("PAIRED", timeout=10000)
                assert store.load()["origin"] == f"chrome-extension://{extension_id}"
                popup.close()

                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                context.route("https://www.avanza.se/**", lambda route: route.fulfill(
                    content_type="text/html", body=LIST if "lista.html" in route.request.url else ORDER))
                page.goto("https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html?s=spread.asc")
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell')?.textContent.includes('263')")
                assert page.locator("tbody .avz-capacity-cell").count() == 3
                assert "360" in page.locator("tbody .avz-capacity-cell").first.inner_text()
                assert reader.calls >= 1
                assert reader.requested == {"123", "456"}, "Only visible certificate identities may be fetched"
                page.locator("tbody tr").first.locator("a").evaluate("a => a.href='/handla/order.html/kop/invalid'")
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell').textContent.includes('IDENTITET OKÄND')")
                context.unroute("https://www.avanza.se/**")

                source = {"price": 263.36, "volume": 1000}
                def website(route):
                    if "/_push/" in route.request.url:
                        body = {"orderbookId": "123", "receivedTime": int(time.time() * 1000),
                                "marketMakerLevelInAsk": 1,
                                "levels": [{"sellPrice": 1, "sellVolume": 9000000},
                                           {"sellPrice": source["price"], "sellVolume": source["volume"]}]}
                        route.fulfill(content_type="text/event-stream", body="event: ORDER_DEPTH\ndata: " + json.dumps(body) + "\n\n")
                    else:
                        route.fulfill(content_type="text/html", body=ORDER)
                context.route("https://www.avanza.se/**", website)
                page.goto("https://www.avanza.se/handla/order.html/kop/123")
                page.wait_for_selector(".avz-capacity strong")
                assert "263" not in page.locator(".avz-capacity strong").inner_text(), "DOM without source time must fail closed"
                page.evaluate("fetch('/_push/order-depth-web-push/123')")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('263')")
                assert reader.calls >= 1
                assert page.locator("#amount").input_value() == ""
                assert page.evaluate("window.orderClicks") == 0

                timings = []
                for i in range(20):
                    source["volume"] = 1001 + i
                    page.evaluate("fetch('/_push/order-depth-web-push/123')")
                    elapsed = page.evaluate("""volume => new Promise(resolve => {
                        const out = document.querySelector('.avz-capacity strong');
                        const start = performance.now();
                        const observer = new MutationObserver(() => {
                            if (!out.textContent.startsWith('—')) { observer.disconnect(); resolve(performance.now()-start); }
                        });
                        observer.observe(out, {childList:true,subtree:true,characterData:true});
                        document.querySelectorAll('.volume.sell')[1].textContent = new Intl.NumberFormat('sv-SE').format(volume);
                        setTimeout(()=>{observer.disconnect();resolve(9999);}, 1000);
                    })""", source["volume"])
                    assert elapsed < 1000, "DOM update did not refresh capacity"
                    timings.append(elapsed)

                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '263,36'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.startsWith('—')")
                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '263,36 *'")
                page.wait_for_function("!document.querySelector('.avz-capacity strong').textContent.startsWith('—')")
                page.wait_for_timeout(5300)
                assert "STALE" in page.locator(".avz-capacity strong").inner_text()
                assert page.locator("#quantity").input_value() == ""
                assert not errors, errors

                server.should_exit = True; thread.join(timeout=5)
                page.goto("https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html")
                # Swap to a list fixture after order tests, then verify actual network refusal.
                page.set_content(LIST)
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell')?.textContent.includes('OFFLINE')", timeout=15000)
                print(json.dumps({"browser": context.browser.version, "pairing": "PASS",
                                  "list_bridge": "PASS", "dom_mm_level_1": "PASS",
                                  "missing_mm_stale_offline": "PASS", "order_fields_unchanged": "PASS",
                                  "dom_update_ms": {"n": len(timings), "p50": statistics.median(timings),
                                                    "p95": sorted(timings)[18], "max": max(timings)}}))
            finally:
                context.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
