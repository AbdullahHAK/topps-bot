"""End-to-end test: loads the real extension into Playwright's Chromium and drives its dashboard
against tests/mock_shop.py (a fake Shopify store).

Usage:  .venv\\Scripts\\python -m tests.e2e_extension
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.mock_shop import STATE, VARIANT_ID, reset, serve

ROOT = Path(__file__).resolve().parent.parent
SHOP = "http://127.0.0.1:8766"
URL = f"{SHOP}/products/test-box"


def prepare_extension(dest: Path) -> Path:
    """Copy the extension and additionally allow it on the local mock shop."""
    ext = dest / "extension"
    shutil.copytree(ROOT / "extension", ext)
    manifest = json.loads((ext / "manifest.json").read_text("utf-8"))
    manifest["host_permissions"].append("http://127.0.0.1/*")
    manifest["content_scripts"][0]["matches"].append("http://127.0.0.1/*")
    (ext / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    return ext


def wait_until(predicate, what: str, timeout: float = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError(f"Timed out waiting for: {what}")


def main() -> int:
    server = serve()
    tmp = Path(tempfile.mkdtemp(prefix="toppsbot-e2e-"))
    try:
        ext = prepare_extension(tmp)
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(tmp / "profile"),
                channel="chromium",
                headless=True,
                args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
            )
            worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker")
            ext_id = worker.url.split("/")[2]
            dash = ctx.new_page()
            dash.on("dialog", lambda d: d.accept())
            dash.goto(f"chrome-extension://{ext_id}/dashboard.html")

            def storage(key):
                return dash.evaluate(f"chrome.storage.local.get('{key}').then(d => d['{key}'])")

            def status_text() -> str:
                return dash.locator("#products tbody td.status").first.inner_text()

            def has_status(text: str):
                return lambda: text.lower() in status_text().lower()

            def stopped():
                return dash.locator("#start").is_enabled() and not storage("running")

            def log_text() -> str:
                return dash.locator("#log").inner_text()

            # Fast intervals for the test (the dashboard enforces >= 5 s for real use).
            dash.evaluate("chrome.storage.local.set({settings: {intervalMin: 1, intervalMax: 2, testMode: true,"
                          " clearCart: true, sound: false, selectors: {}}})")
            dash.reload()

            print("0) Dashboard rejects non-product URLs, accepts a product URL")
            dash.fill("#url", f"{SHOP}/collections/all")
            dash.click("#add-form button[type=submit]")
            assert dash.locator("#form-error").is_visible()
            dash.fill("#url", URL)
            dash.fill("#qty", "2")
            dash.click("#add-form button[type=submit]")
            wait_until(lambda: dash.locator("#products tbody tr").count() == 1, "product row")
            assert storage("products")[0]["quantity"] == 2

            print("1) Test mode: out of stock -> restock -> checkout reached, Pay not clicked")
            reset()
            STATE["cart"] = [(1, 1)]  # stale item must be cleared
            dash.click("#start")
            wait_until(has_status("Out of stock ($49.99)"), "out of stock status")
            assert dash.locator("#state").inner_text() == "RUNNING - TEST MODE"
            STATE["stock"] = True
            wait_until(has_status("TEST OK"), "test mode reached Pay button")
            wait_until(stopped, "auto stop after all products done")
            assert STATE["orders"] == [], STATE["orders"]
            assert STATE["cart"] == [], f"cart should be emptied after a test run: {STATE['cart']}"

            print("2) Max price below product price -> does not buy")
            dash.locator("#products tbody input").nth(1).fill("10")
            dash.locator("#products tbody input").nth(1).dispatch_event("change")
            dash.click("#start")
            wait_until(has_status("over max $10.00"), "over max price status")
            dash.click("#stop")
            wait_until(stopped, "stop")
            assert STATE["orders"] == [] and STATE["cart"] == []
            dash.locator("#products tbody input").nth(1).fill("")
            dash.locator("#products tbody input").nth(1).dispatch_event("change")

            print("3) Live mode, qty 2 -> order placed with confirmation number")
            dash.uncheck("#testMode")
            dash.click("#start")
            wait_until(has_status("ORDER PLACED #T0PPS1"), "order placed")
            assert STATE["orders"] == [[(VARIANT_ID, 2)]], STATE["orders"]
            wait_until(stopped, "auto stop")
            assert dash.locator("#state").inner_text() == "STOPPED"

            print("4) Cloudflare challenge -> alert, waits, resumes after it is solved")
            STATE.update(stock=False, challenge=True)
            dash.click("#start")
            wait_until(has_status("Cloudflare check"), "challenge status")
            assert "Cloudflare check" in log_text()
            STATE["challenge"] = False  # the user solved it
            wait_until(has_status("Out of stock"), "monitoring resumed")
            assert "Cloudflare check passed" in log_text()
            dash.click("#stop")
            wait_until(stopped, "stop")

            print("5) Payment not filled in -> hands checkout over to the user")
            STATE.update(stock=True, payment_ready=False)
            dash.click("#start")
            wait_until(has_status("Waiting for you to finish checkout"), "hand-over status", timeout=60)
            assert STATE["orders"] == [[(VARIANT_ID, 2)]], "no new order expected"
            print("   user completes payment by hand...")
            STATE["payment_ready"] = True
            checkout = next(p for p in ctx.pages if "/checkouts/" in p.url)
            checkout.click("#checkout-pay-button")
            wait_until(has_status("ORDER PLACED #T0PPS2"), "order placed after manual finish")
            wait_until(stopped, "auto stop")

            print("6) Closing the pinned Topps tab -> bot reopens it")
            dash.check("#testMode")
            STATE.update(stock=False)
            dash.click("#start")
            wait_until(has_status("Out of stock"), "monitoring")
            for page in ctx.pages:
                if page.url.rstrip("/") == SHOP:
                    page.close()
            time.sleep(0.5)
            dash.evaluate("chrome.storage.local.set({statuses: {}})")
            wait_until(has_status("Out of stock"), "monitoring after worker tab closed")
            dash.click("#stop")
            wait_until(stopped, "stop")

            dash.screenshot(path=str(ROOT / "tests" / "dashboard.png"), full_page=True)
            ctx.close()
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
