"""End-to-end test: runs the real engine in headless Chrome against tests/mock_shop.py.

Usage:  .venv\\Scripts\\python -m tests.e2e_mock
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / ".test-profile"
os.environ["TOPPSBOT_HEADLESS"] = "1"
os.environ["TOPPSBOT_PROFILE_DIR"] = str(PROFILE)

from tests.mock_shop import STATE, serve  # noqa: E402
from toppsbot.config import Product, Settings  # noqa: E402
from toppsbot.engine import Engine  # noqa: E402

URL = "http://127.0.0.1:8765/p/test-box.html"
events: queue.Queue = queue.Queue()
seen: list[dict] = []


def wait_for(predicate, timeout: float = 60, what: str = "") -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = events.get(timeout=0.5)
        except queue.Empty:
            continue
        seen.append(event)
        if event["type"] in ("log", "status"):
            print("   ", event.get("text") or event.get("msg"))
        if predicate(event):
            return event
    raise AssertionError(f"Timed out waiting for: {what}")


def status_has(text: str):
    return lambda e: e["type"] == "status" and text.lower() in e["text"].lower()


def stopped(e: dict) -> bool:
    return e["type"] == "running" and e["value"] is False


def settings(**kw) -> Settings:
    product = Product(url=URL, quantity=kw.pop("quantity", 1), max_price=kw.pop("max_price", 0))
    return Settings(products=[product], interval_min=1, interval_max=2, **kw)


def main() -> int:
    shutil.rmtree(PROFILE, ignore_errors=True)
    server = serve()
    engine = Engine(events.put)
    try:
        print("1) Test mode: out of stock -> restock -> stops before Place Order")
        STATE.update(stock=False, cart=[], orders=[])
        engine.start(settings(test_mode=True))
        wait_for(status_has("Out of stock"), what="out of stock status")
        STATE["stock"] = True
        wait_for(status_has("TEST OK"), what="test mode reached Place Order")
        wait_for(stopped, what="engine stopped after all products done")
        assert STATE["orders"] == [], STATE["orders"]
        assert STATE["cart"] == [], f"cart should be emptied after test run: {STATE['cart']}"

        print("2) Max price below product price -> does not buy")
        engine.start(settings(test_mode=False, max_price=10))
        wait_for(status_has("over max"), what="over max price status")
        engine.stop()
        wait_for(stopped, what="stop")
        assert STATE["orders"] == []

        print("3) Live mode, qty 2, stale item in cart -> cart cleared, order placed")
        STATE["cart"] = [5]
        engine.start(settings(test_mode=False, quantity=2))
        event = wait_for(status_has("ORDER PLACED"), what="order placed")
        assert "#000000001" in event["text"], event["text"]
        assert STATE["orders"] == [{"items": [2], "payment": "checkmo"}], STATE["orders"]
        wait_for(stopped, what="engine stopped")

        print("4) Preferred payment method selector override")
        engine.start(settings(test_mode=False, selectors={"preferred_payment_method": "braintree_cc_vault"}))
        wait_for(status_has("ORDER PLACED"), what="second order placed")
        assert STATE["orders"][-1]["payment"] == "braintree_cc_vault_1", STATE["orders"]
        wait_for(stopped, what="engine stopped")
    finally:
        engine.shutdown()
        server.shutdown()
        shutil.rmtree(PROFILE, ignore_errors=True)
    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
