"""Isolated Chromium acceptance test; synthetic pages/data, no real account.

Run: uv run --with playwright==1.58.0 python tests/browser/capacity_display.py
Install that Playwright's Chromium first, or set CAPACITY_TEST_CHROMIUM.
"""
import asyncio
import json
import os
from pathlib import Path
import statistics
import shutil
import socket
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
    forced_status = None
    requested_counts = {}
    missing_mm_restored = False
    async def batch(self, ids):
        self.calls += 1
        self.requested.update(ids)
        for i in ids:
            self.requested_counts[i] = self.requested_counts.get(i, 0) + 1
        now = int(time.time() * 1000)
        items = {}
        for i in ids:
            status = self.forced_status or {"124": "TIMEOUT", "128": "OK" if self.missing_mm_restored else "MM_MISSING", "130": "STALE"}.get(i, "OK")
            items[i] = ({"status": status, "amount": "263360.00", "price": 263.36,
                         "volume": 1000, "mm_level": 1, "currency": "SEK", "source_time": now}
                        if status == "OK" else {"status": status, "amount": None, "source_time": now - 20000 if status == "STALE" else now})
        return {"items": items, "server_time": now}


ORDER = """<!doctype html><meta charset=utf-8>
<aza-order-market-info-summary><div class=last-price>263,36 SEK</div></aza-order-market-info-summary>
<input id=amount value=""><input id=quantity value=""><button id=review>Granska köp</button>
<aza-order-depth><div role=table aria-label=Orderdjup>
<div class=level role=row><div class="price sell">1,00</div><div class="volume sell">9 000 000</div></div>
<div class=level role=row><div class="price sell">263,36 *</div><div class="volume sell">1 000</div></div>
</div></aza-order-depth>
<script>window.orderClicks=0;document.getElementById('review').onclick=()=>window.orderClicks++;</script>"""
LIST = """<!doctype html><meta charset=utf-8><aza-certificate-list>
<table><thead><tr><th>Namn</th><th>Spread</th><th>Sälj</th></tr></thead><tbody>
<tr><td><a href=/handla/order.html/kop/123>Test A</a></td><td>1,1%</td><td>263,36</td></tr>
<tr><td><a href=/borshandlade-produkter/certifikat-torg/om-certifikatet.html/456/test-b>Test B</a><a href=/aktier/om-aktien.html/789/underlying>Underliggande</a></td><td>1,2%</td><td>263,36</td></tr>
<tr><td><a href=/handla/order.html/kop/124>Timeout</a></td><td>1,3%</td><td>263,36</td></tr>
<tr><td><a href=/handla/order.html/kop/125>Test D</a></td><td>1,4%</td><td>263,36</td></tr>
<tr><td><a href=/handla/order.html/kop/126>Test E</a></td><td>1,5%</td><td>263,36</td></tr>
<tr><td><a href=/handla/order.html/kop/127>Test F</a></td><td>1,6%</td><td>263,36</td></tr>
<tr><td><a href=/handla/order.html/kop/128>MM missing</a></td><td>—</td><td>—</td></tr>
<tr><td><a href=/handla/order.html/kop/129>Live depth overrides missing list ask</a></td><td>—</td><td>—</td></tr>
<tr><td><a href=/handla/order.html/kop/130>No list ask and stale depth</a></td><td>—</td><td>—</td></tr>
<tr style="transform:translateY(2000px)"><td><a href=/handla/order.html/kop/999>Utanför vyn</a></td><td>2%</td><td>263,36</td></tr>
</tbody></table></aza-certificate-list>"""


def verify_content_lifecycle(p):
    """Exercise the real content scripts with controlled message failures/time."""
    browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CAPACITY_TEST_CHROMIUM"))
    try:
        def setup(path, body):
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("https://www.avanza.se/**", lambda route: route.fulfill(content_type="text/html", body=body))
            page.goto("https://www.avanza.se" + path)
            page.evaluate("""() => {
                window.testCalls=0; window.testHidden=false; window.testOffset=0; window.testMode='ok';
                const originalNow=performance.now.bind(performance);
                performance.now=()=>originalNow()+testOffset;
                Object.defineProperty(document, 'hidden', {get:()=>testHidden});
                window.chrome={runtime:{sendMessage:message=>{
                    testCalls++;
                    if(testMode==='reject') return Promise.reject(new Error('Synthetic message failure'));
                    if(testMode==='pending') return new Promise((resolve,reject)=>{window.testReject=reject;});
                    return {items:Object.fromEntries(message.ids.map(id=>[id,{status:'OK',price:263.36,
                        volume:1000,amount:'263360.00',mm_level:1,currency:'SEK',source_time:Date.now()}]))};
                }}};
            }""")
            for name in ["core.js", "content.js"]:
                page.add_script_tag(content=(ROOT / "chrome-extension" / name).read_text(encoding="utf-8"))
            return page

        for path, body, selector in [
            ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
            ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
        ]:
            page = setup(path, body)
            page.wait_for_function("s=>document.querySelector(s)?.textContent.includes('kr')", arg=selector)
            before = page.evaluate("testCalls")
            page.evaluate("testHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(300)
            assert page.evaluate("testCalls") == before, "Hidden tabs must not fetch"
            page.evaluate("testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("testCalls>" + str(before), timeout=2000)
            expect(page.locator(selector).first).to_contain_text("kr")
            page.wait_for_timeout(300)
            assert page.evaluate("testCalls") == before * 2, "Resume must fetch once, then keep the normal interval"
            resumed_calls = page.evaluate("testCalls")
            page.evaluate("window.dispatchEvent(new Event('pageshow'))")
            page.wait_for_function("testCalls>" + str(resumed_calls), timeout=2000)
            expect(page.locator(selector).first).to_contain_text("kr")
            page.close()

        page = setup("/handla/order.html/kop/123", ORDER)
        amount = page.locator(".avz-capacity strong")
        expect(amount).to_contain_text("kr")
        page.evaluate("testMode='reject';testOffset=11000;document.dispatchEvent(new Event('visibilitychange'))")
        expect(amount).to_contain_text("OFFLINE")
        page.wait_for_timeout(300)
        expect(amount).to_contain_text("OFFLINE")
        page.evaluate("testMode='ok';testOffset=22000;document.dispatchEvent(new Event('visibilitychange'))")
        expect(amount).to_contain_text("kr")

        page.evaluate("testMode='pending';testOffset=33000;document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("typeof testReject==='function'")
        page.evaluate("""() => new Promise(resolve => {
            window.addEventListener('message', function received(e) {
                if(e.data?.type==='AVZ_CAPACITY_DEPTH') {window.removeEventListener('message',received);resolve();}
            });
            window.postMessage({type:'AVZ_CAPACITY_DEPTH',id:'123',source_time:Date.now(),
                mm_level:1,price:263.36,volume:1000},location.origin);
        })""")
        page.evaluate("testReject(new Error('Synthetic old request failure'))")
        page.wait_for_timeout(300)
        expect(amount).to_contain_text("kr")

        # A failed request for the old route must not postpone reads for the new book.
        page.evaluate("testMode='pending';testOffset=50000;history.replaceState({},'', '/handla/order.html/kop/456');document.dispatchEvent(new Event('visibilitychange'))")
        pending_calls = page.evaluate("testCalls")
        page.evaluate("history.replaceState({},'', '/handla/order.html/kop/789');document.dispatchEvent(new Event('visibilitychange'))")
        page.evaluate("testMode='ok';testReject(new Error('Synthetic previous-route failure'))")
        page.wait_for_function("testCalls>" + str(pending_calls), timeout=2000)
        expect(amount).to_contain_text("kr")
        assert page.locator("#amount").input_value() == ""
        assert page.locator("#quantity").input_value() == ""
        assert page.evaluate("window.orderClicks") == 0
        print(json.dumps({"content_visibility_resume": "PASS", "hard_message_failure": "PASS",
                          "fresh_stream_survives_failure": "PASS", "previous_route_failure": "PASS"}))
    finally:
        browser.close()


def main():
    store, reader = Pairing(MemoryStore()), SyntheticReader()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(reader, store, port=port), host="127.0.0.1",
                                           port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "Test bridge failed to bind"
        time.sleep(0.01)
    try:
        with sync_playwright() as p, tempfile.TemporaryDirectory() as profile:
            extension = Path(profile) / "extension"
            shutil.copytree(ROOT / "chrome-extension", extension)
            background = extension / "background.js"
            code = background.read_text(encoding="utf-8")
            assert code.count("http://127.0.0.1:18762") == 1
            background.write_text(code.replace("http://127.0.0.1:18762", f"http://127.0.0.1:{port}"), encoding="utf-8")
            context = p.chromium.launch_persistent_context(
                str(Path(profile) / "profile"), headless=True, channel="chromium",
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
                assert page.locator("tbody .avz-capacity-cell").count() == 10
                expect(page.locator("thead .avz-capacity-cell")).to_have_text("Max köpbelopp")
                assert "360" in page.locator("tbody .avz-capacity-cell").first.inner_text()
                assert reader.calls >= 1
                page.wait_for_function("document.querySelectorAll('tbody .avz-capacity-cell')[5].textContent.includes('263')")
                expect(page.locator("tbody .avz-capacity-cell").nth(6)).to_have_text("")
                expect(page.locator("tbody .avz-capacity-cell").nth(8)).to_have_text("")
                expect(page.locator("tbody .avz-capacity-cell").nth(2)).to_contain_text("TIMEOUT")
                assert reader.requested == {"123", "456", "124", "125", "126", "127", "128", "129", "130"}
                page.wait_for_timeout(9000)
                assert reader.requested_counts["128"] == reader.requested_counts["130"] == 1
                assert reader.requested_counts["129"] == 1, "No normal refresh before ten seconds"
                reader.missing_mm_restored = True
                page.locator("tbody tr").nth(6).locator("td").nth(2).evaluate("cell => cell.textContent='263,36'")
                page.wait_for_function("document.querySelectorAll('tbody .avz-capacity-cell')[6].textContent.includes('263')", timeout=5000)
                assert reader.requested_counts["129"] >= 2, "Fresh depth overrides missing list price on the next pass"
                assert reader.requested_counts["128"] >= 2, "A changed list ask must resume at the next scheduled pass"
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
                expect(page.locator(".avz-capacity span")).to_have_text("Max köpbelopp i synligt orderdjup")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('263')")
                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '200,00 *'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('SIDANS DJUP AVVIKER')")
                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '263,36 *'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('263')")
                fallback_calls = reader.calls
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

                assert reader.calls == fallback_calls, "A verified page stream must take priority over bridge reads"

                saved_asks = page.locator(".price.sell").all_text_contents()
                for placeholder in ["—", "–", "-"]:
                    page.locator(".price.sell").evaluate_all("(cells, value) => cells.forEach(e => e.textContent=value)", placeholder)
                    expect(page.locator(".avz-capacity strong")).to_have_text("")
                    assert page.locator(".avz-capacity strong").get_attribute("title") is None
                page.locator(".price.sell").evaluate_all("(cells, values) => cells.forEach((e,i) => e.textContent=values[i])", saved_asks)
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('kr')")
                page.locator(".volume.sell").nth(1).evaluate("e => e.textContent = 'unreadable'")
                expect(page.locator(".avz-capacity strong")).to_contain_text("MM-DJUP EJ LÄSBART")
                page.locator(".volume.sell").nth(1).evaluate("e => e.textContent = '1 020'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('kr')")

                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '263,36'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.startsWith('—')")
                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '263,36 *'")
                page.wait_for_function("!document.querySelector('.avz-capacity strong').textContent.startsWith('—')")
                reader.forced_status = "STALE"
                page.wait_for_timeout(11500)
                assert not page.locator(".avz-capacity strong").inner_text().startswith("—")
                page.wait_for_timeout(4500)
                assert "STALE" in page.locator(".avz-capacity strong").inner_text()
                assert page.locator("#quantity").input_value() == ""
                assert not errors, errors

                server.should_exit = True; thread.join(timeout=5)
                page.goto("https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html")
                # Swap to a list fixture after order tests, then verify actual network refusal.
                page.set_content(LIST)
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell')?.textContent.includes('OFFLINE')", timeout=15000)
                print(json.dumps({"browser": context.browser.version, "pairing": "PASS",
                                  "list_bridge": "PASS", "unquoted_pause_and_resume": "PASS", "ten_second_polling": "PASS", "dom_mm_level_1": "PASS", "order_bridge_fallback": "PASS",
                                  "missing_mm_stale_offline": "PASS", "blank_absent_ask_and_missing_mm": "PASS", "order_fields_unchanged": "PASS",
                                  "dom_update_ms": {"n": len(timings), "p50": statistics.median(timings),
                                                    "p95": sorted(timings)[18], "max": max(timings)}}))
            finally:
                context.close()
            verify_content_lifecycle(p)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


if __name__ == "__main__":
    main()
