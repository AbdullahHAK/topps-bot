"""Bot engine: owns the Chrome window and runs one monitor task per product.

All Playwright work happens on a private asyncio loop in a background thread.
The GUI calls the public methods (thread-safe) and receives events through `emit`.
Events are dicts: {"type": "log"|"status"|"running"|"alert", ...}.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import DEFAULT_ORIGIN, PROFILE_DIR, Product, Settings
from .store import CheckoutError, ToppsStore, origin_of

log = logging.getLogger("toppsbot")

MAX_FAILED_ATTEMPTS = 5


def short_name(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.removesuffix(".html").replace("-", " ")[:60] or url


class Engine:
    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="bot-engine", daemon=True).start()
        self._pw = None
        self._ctx: BrowserContext | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._checkout_lock = asyncio.Lock()
        self._settings = Settings()
        self._store: ToppsStore | None = None
        self.running = False

    # ---------- public API (called from the GUI thread) ----------

    def open_login(self, origin: str = DEFAULT_ORIGIN) -> None:
        self._submit(self._open_login(origin))

    def start(self, settings: Settings) -> None:
        self._submit(self._start(settings))

    def stop(self) -> None:
        self._submit(self._stop("Stopped"))

    def shutdown(self) -> None:
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result(timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    # ---------- events ----------

    def _submit(self, coro) -> None:
        async def guarded():
            try:
                await coro
            except Exception as e:
                log.exception("Engine error")
                self._log(f"Error: {e}", "error")

        asyncio.run_coroutine_threadsafe(guarded(), self._loop)

    def _log(self, msg: str, level: str = "info") -> None:
        getattr(log, level)(msg)
        self._emit({"type": "log", "level": level, "msg": msg})

    def _status(self, product: Product, text: str) -> None:
        self._emit({"type": "status", "id": product.id, "text": text})

    def _alert(self, title: str, msg: str) -> None:
        self._log(f"{title}: {msg}", "warning")
        self._emit({"type": "alert", "title": title, "msg": msg})

    def _set_running(self, value: bool) -> None:
        self.running = value
        self._emit({"type": "running", "value": value})

    async def _ask_human(self, page: Page, message: str) -> None:
        self._alert("Action needed", message)
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass

    # ---------- browser ----------

    async def _ensure_browser(self) -> BrowserContext:
        if self._ctx:
            return self._ctx
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if not self._pw:
            self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel="chrome",
                headless=os.environ.get("TOPPSBOT_HEADLESS") == "1",
                no_viewport=True,
            )
        except PlaywrightError as e:
            raise RuntimeError(
                "Could not start Google Chrome. Make sure Chrome is installed and close any "
                "Chrome window the bot opened earlier."
            ) from e
        self._ctx.on("close", lambda _: self._on_browser_closed())
        self._log("Chrome started")
        return self._ctx

    def _on_browser_closed(self) -> None:
        self._ctx = None
        if self.running:
            self._log("Chrome window was closed - stopping", "warning")
            asyncio.ensure_future(self._stop("Stopped (Chrome closed)"))

    async def _main_page(self) -> Page:
        ctx = await self._ensure_browser()
        return ctx.pages[0] if ctx.pages else await ctx.new_page()

    # ---------- commands ----------

    async def _open_login(self, origin: str) -> None:
        page = await self._main_page()
        await page.bring_to_front()
        await page.goto(f"{origin}/customer/account/login/", wait_until="domcontentloaded", timeout=60000)
        self._log("Log in to your Topps account in the Chrome window. The login is remembered for next time.")

    async def _start(self, settings: Settings) -> None:
        if self.running:
            return
        products = list(settings.products)
        if not products:
            self._log("Add at least one product URL first", "warning")
            return
        self._settings = settings
        self._set_running(True)
        try:
            self._store = ToppsStore(settings.selectors, self._ask_human, self._log)
            page = await self._main_page()
            origin = origin_of(products[0].url)
            self._log("Checking that you are logged in...")
            if not await self._store.is_logged_in(page, origin):
                self._alert("Not logged in", "Click 'Open Chrome / Log in', sign in to Topps, then press Start again.")
                self._set_running(False)
                return
            if settings.clear_cart:
                await self._store.clear_cart(page, origin)
        except Exception:
            self._set_running(False)
            raise
        mode = "TEST MODE (will not place orders)" if settings.test_mode else "LIVE (will place orders)"
        self._log(f"Started monitoring {len(products)} product(s) - {mode}")
        for product in products:
            self._status(product, "Starting...")
            self._tasks[product.id] = asyncio.create_task(self._monitor(product))

    async def _stop(self, reason: str) -> None:
        if not self.running:
            return
        self._set_running(False)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._log(reason)

    async def _shutdown(self) -> None:
        await self._stop("Stopped")
        if self._ctx:
            try:
                await self._ctx.close()
            except PlaywrightError:
                pass
        if self._pw:
            await self._pw.stop()

    # ---------- per-product work ----------

    async def _monitor(self, product: Product) -> None:
        name = short_name(product.url)
        failures = 0
        page = None
        try:
            page = await self._ctx.new_page()
            while True:
                self._status(product, "Checking...")
                try:
                    info = await self._store.check_product(page, product.url)
                    price = f" (${info.price:.2f})" if info.price is not None else ""
                    if not info.in_stock:
                        self._status(product, info.reason + price)
                    elif product.max_price and info.price and info.price > product.max_price:
                        self._status(product, f"In stock but{price} is over max ${product.max_price:.2f}")
                    else:
                        self._log(f"{name}: IN STOCK{price}")
                        if await self._buy(page, product):
                            return
                        failures += 1
                        if failures >= MAX_FAILED_ATTEMPTS:
                            self._status(product, f"Gave up after {failures} failed attempts")
                            self._alert("Gave up", f"{name}: {failures} checkout attempts failed - see log")
                            return
                except CheckoutError as e:
                    self._status(product, f"Error: {e}")
                    self._log(f"{name}: {e}", "warning")
                except PlaywrightError as e:
                    if not self._ctx:
                        return
                    first_line = str(e).strip().splitlines()[0][:150]
                    self._status(product, f"Error: {first_line}")
                    self._log(f"{name}: {first_line}", "warning")
                delay = random.uniform(self._settings.interval_min, max(self._settings.interval_min, self._settings.interval_max))
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._status(product, "Stopped")
            raise
        finally:
            if page:
                try:
                    await page.close()
                except PlaywrightError:
                    pass
            self._tasks.pop(product.id, None)
            if self.running and not self._tasks:
                self._set_running(False)
                self._log("All products finished")

    async def _buy(self, page: Page, product: Product) -> bool:
        """Add to cart and check out. Returns True when this product is finished."""
        name = short_name(product.url)
        origin = origin_of(product.url)
        self._alert("In stock!", f"{name} - adding to cart")
        if self._checkout_lock.locked():
            self._status(product, "In stock - waiting for another checkout to finish...")
        async with self._checkout_lock:
            added = False
            try:
                self._status(product, f"Adding {product.quantity} to cart...")
                await self._store.add_to_cart(page, product.quantity)
                added = True
                self._status(product, "Checking out...")
                order = await self._store.checkout(page, origin, self._settings.test_mode)
            except CheckoutError as e:
                self._status(product, f"Checkout failed: {e} - retrying")
                self._log(f"{name}: checkout failed: {e}", "warning")
                if added and self._settings.clear_cart:
                    await self._store.clear_cart(page, origin)
                return False

            if self._settings.test_mode:
                self._status(product, "TEST OK - reached Place Order (not clicked)")
                self._log(f"{name}: test run reached the Place Order button - no order placed")
                if self._settings.clear_cart:
                    await self._store.clear_cart(page, origin)
                return True

            label = f"#{order}" if order else ""
            self._status(product, f"ORDER PLACED {label}".strip())
            self._alert("Order placed!", f"{name} x{product.quantity} {label}".strip())
            return True
