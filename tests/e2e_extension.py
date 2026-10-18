"""End-to-end test: loads the real extension into Playwright's Chromium and drives its dashboard
against tests/mock_shop.py (a fake topps.com with Shopify-style checkout).

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

from tests.mock_shop import STATE, reset, serve

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

            def row_input(index: int):
                return dash.locator("#products tbody input").nth(index)  # 0 option, 1 qty, 2 max price

            def set_input(index: int, value: str):
                row_input(index).fill(value)
                row_input(index).dispatch_event("change")

            # Fast intervals for the test (the dashboard enforces >= 5 s for real use).
            dash.evaluate("chrome.storage.local.set({settings: {intervalMin: 1, intervalMax: 2, testMode: true,"
                          " clearCart: true, sound: false, selectors: {}}})")
            dash.reload()

            print("0) Dashboard rejects non-product URLs, accepts a product URL with option + qty")
            dash.fill("#url", f"{SHOP}/collections/all")
            dash.click("#add-form button[type=submit]")
            assert dash.locator("#form-error").is_visible()
            dash.fill("#url", URL)
            dash.fill("#option", "5 cards")
            dash.fill("#qty", "2")
            dash.click("#add-form button[type=submit]")
            wait_until(lambda: dash.locator("#products tbody tr").count() == 1, "product row")
            assert storage("products")[0]["option"] == "5 cards" and storage("products")[0]["quantity"] == 2

            print("1) Test mode: out of stock -> restock -> checkout reached, Pay not clicked, cart emptied")
            reset()
            dash.click("#start")
            wait_until(has_status("Out of stock (5 cards $34.99)"), "out of stock status")
            assert dash.locator("#state").inner_text() == "RUNNING - TEST MODE"
            STATE["stock"] = True
            wait_until(has_status("TEST OK"), "test mode reached Pay button", timeout=60)
            wait_until(stopped, "auto stop after all products done")
            assert STATE["orders"] == [], STATE["orders"]
            wait_until(lambda: STATE["cart"] == [], "cart emptied after the test run")

            print("2) Max price below product price -> does not buy")
            set_input(2, "10")
            dash.click("#start")
            wait_until(has_status("over max $10.00"), "over max price status")
            dash.click("#stop")
            wait_until(stopped, "stop")
            assert STATE["orders"] == []
            set_input(2, "")

            print("3) Purchase limit error from the site -> reported, retried")
            STATE["limit"] = 1
            dash.click("#start")
            wait_until(has_status("hit the limit"), "limit error status", timeout=60)
            dash.click("#stop")
            wait_until(stopped, "stop")
            STATE["limit"] = 0

            print("4) Live mode with leftover cart items -> cart emptied via Remove, order placed, checkboxes untouched")
            dash.uncheck("#testMode")
            STATE["cart"] = [("111", 9), ("999", 1)]  # leftovers on the account, e.g. an unavailable item
            dash.click("#start")
            wait_until(has_status("ORDER PLACED #T0PPS1"), "order placed", timeout=60)
            assert STATE["orders"] == [{"items": [("555", 2)], "marketing": False}], STATE["orders"]
            wait_until(stopped, "auto stop")
            assert dash.locator("#state").inner_text() == "STOPPED"

            print("5) Cloudflare challenge -> alert, waits, resumes after it is solved")
            STATE.update(stock=False, challenge=True)
            dash.click("#start")
            wait_until(has_status("Cloudflare check"), "challenge status")
            assert "Cloudflare check" in log_text()
            STATE["challenge"] = False  # the user solved it
            wait_until(has_status("Out of stock"), "monitoring resumed")
            assert "Cloudflare check passed" in log_text()
            dash.click("#stop")
            wait_until(stopped, "stop")

            print("6) Payment not filled in -> hands checkout over to the user")
            STATE.update(stock=True, payment_ready=False)
            dash.click("#start")
            wait_until(has_status("Waiting for you to finish checkout"), "hand-over status", timeout=90)
            assert len(STATE["orders"]) == 1, "no new order expected"
            print("   user completes payment by hand...")
            STATE["payment_ready"] = True
            checkout = next(p for p in reversed(ctx.pages) if p.url.endswith("/en-us"))
            checkout.click("#checkout-pay-button")
            wait_until(has_status("ORDER PLACED #T0PPS2"), "order placed after manual finish")
            wait_until(stopped, "auto stop")

            print("7) Closing the pinned Topps tab -> bot reopens it")
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

            print("8) Single-variant product (box, 'Default Title', no option buttons) -> test checkout reached")
            dash.locator("#products tbody button.remove").first.click()
            dash.fill("#url", f"{SHOP}/products/value-box")
            dash.fill("#qty", "1")
            dash.click("#add-form button[type=submit]")
            wait_until(lambda: dash.locator("#products tbody tr").count() == 1, "value box row")
            STATE.update(stock=True)
            dash.click("#start")
            wait_until(has_status("TEST OK"), "value box reached Pay button", timeout=60)
            wait_until(stopped, "auto stop")

            dash.screenshot(path=str(ROOT / "tests" / "dashboard.png"), full_page=True)
            ctx.close()
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
