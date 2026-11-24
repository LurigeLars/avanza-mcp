"""Isolated Chromium acceptance test; synthetic pages/data, no real account.

Run: uv run --with playwright==1.58.0 python tests/browser/capacity_display.py
Install that Playwright's Chromium first, or set CAPACITY_TEST_CHROMIUM.
"""
import asyncio
from datetime import datetime, timezone
import json
import re
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
            status = self.forced_status or {"128": "OK" if self.missing_mm_restored else "MM_MISSING", "130": "STALE"}.get(i, "OK")
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
        def setup(path, body, mode='ok'):
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("https://www.avanza.se/**", lambda route: route.fulfill(content_type="text/html", body=body))
            page.goto("https://www.avanza.se" + path)
            page.evaluate("""mode => {
                window.testCalls=0; window.testIds=[]; window.testHidden=false; window.testOffset=0; window.testMode=mode;
                window.testAge=0; window.testVolume=1000;
                const originalDateNow=Date.now.bind(Date);
                Date.now=()=>originalDateNow()+testAge;
                performance.now=()=>testOffset;
                Object.defineProperty(document, 'hidden', {get:()=>testHidden});
                window.chrome={runtime:{id:"test-extension", onMessage:{addListener:fn=>window.testSettingsListener=fn}, sendMessage:message=>{
                    if(message.type==='STATE')return Promise.resolve({status:'PAIRED',enabled:true,pollInterval:60000});
                    testCalls++; testIds.push([...message.ids]);
                    if(testMode==='reject') return Promise.reject(new Error('Synthetic message failure'));
                    if(testMode==='pending') return new Promise((resolve,reject)=>{window.testReject=reject;window.testResolve=resolve;});
                    if(testMode==='backoff')return {status:'BACKOFF',retry_after:30};
                    if(testMode==='stale')return {items:Object.fromEntries(message.ids.map(id=>[id,{status:'STALE'}]))};
                    if(testMode==='partial')return {items:Object.fromEntries(message.ids.map(id=>[id,id==='1000'?{status:'TIMEOUT'}:{status:'OK',price:263.36,volume:1000,amount:'263360',mm_level:1,currency:'SEK',source_time:Date.now()}]))};
                    if(['TIMEOUT','BUSY','RATE_LIMITED'].includes(testMode))return {
                        items:Object.fromEntries(message.ids.map(id=>[id,{status:testMode}])),
                        retry_after:testMode==='RATE_LIMITED'?60:30};
                    return {items:Object.fromEntries(message.ids.map(id=>[id,{status:'OK',price:263.36,
                        volume:testVolume,amount:String(263.36*testVolume),mm_level:1,currency:'SEK',source_time:Date.now()}]))};
                }}};
            }""", mode)
            for name in ["core.js", "content.js"]:
                page.add_script_tag(content=(ROOT / "chrome-extension" / name).read_text(encoding="utf-8"))
            return page

        # A new view must wait for its own request before declaring failure.
        for restore in ["visibility", "pageshow"]:
            for path, body, selector in [
                ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
                ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
            ]:
                page = setup(path, body, mode="reject")
                amount = page.locator(selector).first
                expect(amount).to_have_text("— FAILED")
                page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:0},{id:'test-extension'});testMode='pending'")
                if restore == "visibility":
                    page.evaluate("testHidden=true;document.dispatchEvent(new Event('visibilitychange'));testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
                else:
                    page.evaluate("window.dispatchEvent(new Event('pageshow'))")
                page.wait_for_function("typeof testReject==='function'")
                expect(amount).to_have_text("—")
                assert amount.get_attribute("title") is None
                page.wait_for_timeout(1500)
                expect(amount).to_have_text("—")
                page.evaluate("testReject(new Error('Synthetic current-view failure'))")
                expect(amount).to_have_text("— FAILED")
                page.close()

        # An incomplete DOM is not a failed read while the first book is loading.
        page = setup("/handla/order.html/kop/123", ORDER.replace("263,36 *", "Loading"), mode="pending")
        amount = page.locator(".avz-capacity strong")
        expect(amount).to_have_text("—")
        page.locator(".price.sell").nth(1).evaluate("el=>el.textContent='263,36 *'")
        page.wait_for_function("typeof testResolve==='function'")
        expect(amount).to_have_text("—")
        page.evaluate("testResolve({items:{123:{status:'OK',price:263.36,volume:1000,amount:'263360',mm_level:1,currency:'SEK',source_time:Date.now()}}})")
        expect(amount).to_contain_text("kr")
        page.close()

        # Replies issued before the tab was hidden cannot fail its new view.
        for path, body, selector in [
            ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
            ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
        ]:
            page = setup(path, body, mode="pending")
            page.wait_for_function("typeof testReject==='function'")
            page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:0},{id:'test-extension'});window.oldReject=testReject;testReject=undefined;testHidden=true;document.dispatchEvent(new Event('visibilitychange'));testHidden=false;document.dispatchEvent(new Event('visibilitychange'));oldReject(new Error('Synthetic previous-view failure'))")
            page.wait_for_function("typeof testReject==='function'")
            expect(page.locator(selector).first).to_have_text("—")
            page.evaluate("testReject(new Error('Synthetic new-view failure'))")
            expect(page.locator(selector).first).to_have_text("— FAILED")
            page.close()
        print(json.dumps({"new_view_waits_for_result": "PASS", "initial_dom_loading": "PASS", "previous_view_reply_ignored": "PASS"}))

        for path, body, selector in [
            ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
            ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
        ]:
            page = setup(path, body)
            amount = page.locator(selector).first
            expect(amount).to_contain_text("kr")
            original = amount.inner_text()
            source_title = amount.get_attribute("title")
            page.evaluate("testMode='stale';testAge=30000;testOffset=11000;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("testCalls>=2")
            expect(amount).to_have_text(original)
            expect(amount).to_have_class(re.compile(r"avz-capacity-stale"))
            assert amount.get_attribute("title") == source_title + " Not live; retained value.", "Refresh must not renew the source timestamp"
            page.evaluate("testHidden=true;document.dispatchEvent(new Event('visibilitychange'));testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
            expect(amount).to_have_text("—")
            page.evaluate("testMode='ok';testVolume=1200;testOffset=22000;document.querySelectorAll('.volume.sell')[1]?.replaceChildren('1 200');document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("testCalls>=3")
            expect(amount).not_to_have_text(original)
            expect(amount).to_contain_text("kr")
            page.evaluate("testMode='reject';testOffset=33000;document.dispatchEvent(new Event('visibilitychange'))")
            expect(amount).to_have_text("— FAILED")
            assert "OFFLINE" in amount.get_attribute("title")
            page.evaluate("testMode='backoff';testOffset=44000;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("testCalls>=5")
            expect(amount).to_have_text("— FAILED")
            page.evaluate("testMode='stale';history.replaceState({},'',location.pathname+'?revisit=1')")
            expect(amount).to_have_text("—")
            page.evaluate("history.replaceState({},'',location.pathname)")
            expect(amount).to_have_text("—")
            page.close()
        print(json.dumps({"quiet_refresh": "PASS", "no_source_retiming": "PASS", "failed_latches_until_recovery": "PASS", "no_previous_visit_amount": "PASS"}))

        for status, seconds in [('TIMEOUT', 30), ('BUSY', 30), ('RATE_LIMITED', 60)]:
            page = setup('/borshandlade-produkter/certifikat-torg/lista.html', LIST, mode=status)
            page.wait_for_function('testCalls===1')
            expect(page.locator('tbody .avz-capacity-cell').first).to_have_text('— FAILED')
            page.evaluate('testOffset=11000')
            page.wait_for_timeout(1500)
            assert page.evaluate('testCalls') == 1, 'Stop the pass and wait instead of requesting the remaining rows'
            page.evaluate('offset=>testOffset=offset', max(60, seconds) * 1000 + 1000)
            page.wait_for_function('testCalls===2')
            page.close()
        print(json.dumps({"stop_overloaded_list_pass": "PASS", "retry_pause": "PASS"}))

        for path, body, selector in [
            ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
            ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
        ]:
            page = setup(path, body)
            page.wait_for_function("s=>document.querySelector(s)?.textContent.includes('kr')", arg=selector)
            page.wait_for_timeout(1500)
            calls_per_pass = page.evaluate("testCalls")
            page.evaluate("testOffset=59000")
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == calls_per_pass, "No normal refresh before one minute"
            page.evaluate("testOffset=61000")
            page.wait_for_function("testCalls>" + str(calls_per_pass), timeout=2000)
            page.wait_for_timeout(1500)
            before = page.evaluate("testCalls")
            page.evaluate("testHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == before, "Hidden tabs must not fetch"
            page.evaluate("testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("testCalls>" + str(before), timeout=2000)
            expect(page.locator(selector).first).to_contain_text("kr")
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == before + calls_per_pass, "Resume must fetch once, then keep the normal interval"
            resumed_calls = page.evaluate("testCalls")
            page.evaluate("window.dispatchEvent(new Event('pageshow'))")
            page.wait_for_function("testCalls>" + str(resumed_calls), timeout=2000)
            expect(page.locator(selector).first).to_contain_text("kr")
            page.close()

        # All visible rows are eligible, new scroll rows are read once, and an isolated timeout does not starve later rows.
        many = '<aza-certificate-list><table><thead><tr><th>Namn</th><th>Sälj</th></tr></thead><tbody>' + ''.join(
            f'<tr><td><a href="/handla/order.html/kop/{i}">Test</a></td><td>263,36</td></tr>' for i in range(1000, 1060)) + '</tbody></table></aza-certificate-list>'
        page = setup('/borshandlade-produkter/certifikat-torg/lista.html', many, mode='partial')
        page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:0},{id:'test-extension'})")
        page.wait_for_function("testIds.flat().includes('1020')", timeout=20000)
        assert page.evaluate("testIds.every(ids=>ids.length<=5)")
        expect(page.locator('tbody .avz-capacity-cell').nth(20)).to_contain_text("kr")
        expect(page.locator('tbody .avz-capacity-cell').first).to_have_text("— FAILED")
        page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
        page.wait_for_function("testIds.flat().includes('1059')", timeout=25000)
        expect(page.locator('tbody .avz-capacity-cell').last).to_contain_text("kr")
        assert page.evaluate("testIds.flat().filter(id=>id==='1020').length") == 1
        page.close()

        # Settings must apply to an existing view, including replies issued before OFF.
        for path, body, selector in [
            ("/borshandlade-produkter/certifikat-torg/lista.html", LIST, "tbody .avz-capacity-cell"),
            ("/handla/order.html/kop/123", ORDER, ".avz-capacity strong"),
        ]:
            page = setup(path, body)
            expect(page.locator(selector).first).to_contain_text("kr")
            page.wait_for_timeout(1500)
            page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:false,pollInterval:0},{id:'other-extension'})")
            expect(page.locator(selector).first).to_contain_text("kr")
            page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:false,pollInterval:0},{id:'test-extension'})")
            expect(page.locator(selector)).to_have_count(0)
            before = page.evaluate("testCalls")
            page.evaluate("testOffset=600000")
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == before, "OFF must stop new requests"
            page.evaluate("testMode='stale';testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:0},{id:'test-extension'})")
            expect(page.locator(selector).first).to_have_text("—")
            page.wait_for_timeout(1500)
            before = page.evaluate("testCalls")
            page.evaluate("testOffset=1200000")
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == before, "On-visible mode has no timer requests"
            page.evaluate("testMode='ok';testHidden=true;document.dispatchEvent(new Event('visibilitychange'));testHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
            expect(page.locator(selector).first).to_contain_text("kr")
            page.wait_for_timeout(1500)
            # Late responses from before OFF must not become a new-session value.
            page.evaluate("testMode='pending';document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_function("typeof testResolve==='function'")
            page.evaluate("testSettingsListener({type:'STATE_CHANGED',enabled:false,pollInterval:0},{id:'test-extension'});testMode='stale';testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:0},{id:'test-extension'});testResolve({items:{123:{status:'OK',price:263.36,volume:1000,amount:'263360',mm_level:1,currency:'SEK',source_time:Date.now()}}})")
            expect(page.locator(selector).first).to_have_text("—")
            page.close()
        for seconds in [30, 120, 300]:
            page = setup("/handla/order.html/kop/123", ORDER)
            expect(page.locator(".avz-capacity strong")).to_contain_text("kr")
            page.evaluate("interval=>testSettingsListener({type:'STATE_CHANGED',enabled:true,pollInterval:interval},{id:'test-extension'})", seconds * 1000)
            before = page.evaluate("testCalls")
            page.evaluate("offset=>testOffset=offset", seconds * 1000 - 1)
            page.wait_for_timeout(1500)
            assert page.evaluate("testCalls") == before
            page.evaluate("offset=>testOffset=offset", seconds * 1000 + 1)
            page.wait_for_function("testCalls>" + str(before))
            page.close()
        print(json.dumps({"live_on_off": "PASS", "on_visible_mode": "PASS", "interval_settings": "PASS", "late_reply_after_off": "PASS"}))

        page = setup("/handla/order.html/kop/123", ORDER)
        amount = page.locator(".avz-capacity strong")
        expect(amount).to_contain_text("kr")
        page.evaluate("testMode='reject';testOffset=11000;document.dispatchEvent(new Event('visibilitychange'))")
        expect(amount).to_contain_text("FAILED")
        page.wait_for_timeout(1500)
        expect(amount).to_contain_text("FAILED")
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
        page.wait_for_timeout(1500)
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
                popup.locator("#pair-button").click()
                expect(popup.locator("#status")).to_have_text("Paired", timeout=10000)
                assert store.load()["origin"] == f"chrome-extension://{extension_id}"
                popup.close()

                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                context.route("https://www.avanza.se/**", lambda route: route.fulfill(
                    content_type="text/html", body=LIST if "certifikat-torg/" in route.request.url else ORDER))
                # Exercise real MV3 messaging after SPA navigation, without reinjecting the content script.
                page.goto("https://www.avanza.se/borshandlade-produkter/certifikat-torg/certifikat-torg.html")
                page.evaluate("history.pushState({}, '', '/borshandlade-produkter/certifikat-torg/lista.html?s=spread.asc')")
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell')?.textContent.includes('263')")
                assert page.locator("tbody .avz-capacity-cell").count() == 10
                expect(page.locator("thead .avz-capacity-cell")).to_have_text("Max buy capacity")
                assert "360" in page.locator("tbody .avz-capacity-cell").first.inner_text()
                assert reader.calls >= 1
                page.wait_for_function("document.querySelectorAll('tbody .avz-capacity-cell')[5].textContent.includes('263')")
                expect(page.locator("tbody .avz-capacity-cell").nth(6)).to_have_text("")
                expect(page.locator("tbody .avz-capacity-cell").nth(8)).to_have_text("")
                expect(page.locator("tbody .avz-capacity-cell").nth(2)).to_contain_text("kr")
                assert reader.requested == {"123", "456", "124", "125", "126", "127", "128", "129", "130"}
                page.wait_for_timeout(9000)
                assert reader.requested_counts["128"] == reader.requested_counts["130"] == 1
                assert reader.requested_counts["129"] == 1, "No early normal refresh"
                reader.missing_mm_restored = True
                page.locator("tbody tr").nth(6).locator("td").nth(2).evaluate("cell => cell.textContent='263,36'")
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_function("document.querySelectorAll('tbody .avz-capacity-cell')[6].textContent.includes('263')", timeout=5000)
                assert reader.requested_counts["129"] >= 2, "Fresh depth overrides missing list price on the next pass"
                assert reader.requested_counts["128"] >= 2, "A changed list ask must resume at the next scheduled pass"
                page.locator("tbody tr").first.locator("a").evaluate("a => a.href='/handla/order.html/kop/invalid'")
                expect(page.locator('tbody .avz-capacity-cell').first).to_have_text('— FAILED')
                assert 'IDENTITY_UNKNOWN' in page.locator('tbody .avz-capacity-cell').first.get_attribute('title')
                # Use the real popup and service worker to change existing tabs.
                popup = context.new_page()
                popup.goto(f"chrome-extension://{extension_id}/popup.html")
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "true")
                assert not popup.locator("#pairing").evaluate("el=>el.open")
                popup.locator("#interval").select_option("0")
                expect(popup.locator("#interval")).to_be_enabled()
                screenshot_dir = os.environ.get("CAPACITY_POPUP_SCREENSHOT_DIR")
                if screenshot_dir:
                    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                    popup.locator("body").screenshot(path=str(Path(screenshot_dir) / "avanza-popup-on.png"))
                popup.locator("#power").click()
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "false")
                expect(page.locator(".avz-capacity-cell")).to_have_count(0)
                assert worker.evaluate("chrome.action.getBadgeText({})") == "OFF"
                calls = reader.calls
                page.wait_for_timeout(500)
                assert reader.calls == calls
                if screenshot_dir:
                    popup.locator("body").screenshot(path=str(Path(screenshot_dir) / "avanza-popup-off.png"))
                popup.close()
                popup = context.new_page()
                popup.goto(f"chrome-extension://{extension_id}/popup.html")
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "false")
                expect(popup.locator("#interval")).to_have_value("0")
                expect(popup.locator("#status")).to_have_text("Paired")
                popup.locator("#power").click()
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "true")
                assert worker.evaluate("chrome.action.getBadgeText({})") == "ON"
                expect(page.locator("thead .avz-capacity-cell")).to_have_text("Max buy capacity")
                popup.locator("#interval").select_option("60000")
                expect(popup.locator("#interval")).to_be_enabled()
                # Keyboard control uses the same switch and retains a visible focus indicator.
                popup.locator("#power").focus()
                popup.keyboard.press("Space")
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "false")
                popup.keyboard.press("Space")
                expect(popup.locator("#power")).to_have_attribute("aria-checked", "true")
                popup.close()
                context.unroute("https://www.avanza.se/**")

                source = {"price": 263.36, "volume": 1000}
                def website(route):
                    if "/_push/" in route.request.url:
                        body = {"orderbookId": "123", "updated": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                                "marketMakerLevelInAsk": 1,
                                "levels": [{"sellPrice": 1, "sellVolume": 9000000},
                                           {"sellPrice": source["price"], "sellVolume": source["volume"]}]}
                        route.fulfill(content_type="text/event-stream", body="event: ORDER_DEPTH\ndata: " + json.dumps(body) + "\n\n")
                    else:
                        route.fulfill(content_type="text/html", body=ORDER)
                context.route("https://www.avanza.se/**", website)
                page.goto("https://www.avanza.se/handla/order.html/kop/123")
                page.wait_for_selector(".avz-capacity strong")
                expect(page.locator(".avz-capacity span")).to_have_text("Max buy capacity in visible order depth")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('263')")
                page.locator(".price.sell").nth(1).evaluate("e => e.textContent = '200,00 *'")
                page.wait_for_function("document.querySelector('.avz-capacity strong').textContent.includes('FAILED')")
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
                expect(page.locator(".avz-capacity strong")).to_contain_text("FAILED")
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
                assert "kr" in page.locator(".avz-capacity strong").inner_text()
                assert page.locator("#quantity").input_value() == ""
                assert not errors, errors

                server.should_exit = True; thread.join(timeout=5)
                page.goto("https://www.avanza.se/borshandlade-produkter/certifikat-torg/lista.html")
                # Swap to a list fixture after order tests, then verify actual network refusal.
                page.set_content(LIST)
                page.wait_for_function("document.querySelector('tbody .avz-capacity-cell')?.textContent.includes('FAILED')", timeout=15000)
                print(json.dumps({"browser": context.browser.version, "pairing": "PASS",
                                  "list_bridge": "PASS", "unquoted_pause_and_resume": "PASS", "one_minute_polling": "PASS", "dom_mm_level_1": "PASS", "order_bridge_fallback": "PASS",
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
