"""Topps.com storefront flow (Magento 2): stock check, cart and checkout.

Topps can change its page markup at any time. Every selector lives in
DEFAULT_SELECTORS and can be overridden from settings.json ("selectors")
without rebuilding the app.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

DEFAULT_SELECTORS = {
    # Product page
    "add_to_cart_button": "#product-addtocart-button",
    "qty_input": "#qty",
    "out_of_stock": ".product-info-main .stock.unavailable, .product-info-main .out-of-stock",
    "price": ".product-info-main [data-price-type='finalPrice']",
    "add_to_cart_request": "/checkout/cart/add",
    "message_error": ".page.messages .message-error, [data-ui-id='message-error']",
    # Cart page
    "cart_delete_item": ".cart.item .action-delete, #shopping-cart-table .action-delete",
    # Checkout
    "loader": ".loading-mask, #checkout-loader",
    "shipping_continue": "#shipping-method-buttons-container button",
    "shipping_method_radio": "#checkout-shipping-method-load input[type='radio']",
    "payment_list": "#checkout-payment-method-load .payment-method",
    "payment_method_radio": "#checkout-payment-method-load input[name='payment[method]']",
    # Optional: value prefix of the payment method to use, e.g. "braintree_cc_vault". Empty = keep Magento's default.
    "preferred_payment_method": "",
    "agreements": ".payment-method._active .checkout-agreements input[type='checkbox']",
    "place_order": ".payment-method._active button.action.primary.checkout",
    "success_url": "/checkout/onepage/success",
    "order_number": ".checkout-success .order-number strong, .checkout-success .order-number",
    # Cloudflare full-page challenge
    "challenge": "#challenge-form, #challenge-running, #cf-challenge-running",
}

SOLD_OUT_WORDS = ("sold out", "out of stock", "unavailable", "notify me", "coming soon")
CHALLENGE_TITLES = ("just a moment", "attention required")
AUTO_PASS_SECONDS = 15  # Cloudflare often clears a challenge by itself in a real browser
HUMAN_TIMEOUT = 600  # how long to wait for the user to solve a captcha / finish a step


class CheckoutError(Exception):
    """A purchase step failed; the product goes back to monitoring."""


@dataclass
class StockInfo:
    in_stock: bool
    price: float | None
    reason: str


def origin_of(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


class ToppsStore:
    def __init__(
        self,
        selectors: dict[str, str],
        ask_human: Callable[[Page, str], Awaitable[None]],
        log: Callable[[str], None],
    ):
        self.s = {**DEFAULT_SELECTORS, **selectors}
        self._ask_human = ask_human
        self._log = log

    # ---------- helpers ----------

    def _visible(self, page: Page, key: str):
        return page.locator(f"{self.s[key]} >> visible=true")

    @staticmethod
    async def _wait_until(predicate: Callable[[], Awaitable[bool]], timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                if await predicate():
                    return True
            except PlaywrightError:
                pass  # page was mid-navigation
            await asyncio.sleep(1)
        return False

    async def _wait_idle(self, page: Page, timeout: int = 30000) -> None:
        """Wait for Magento's checkout spinners to disappear."""
        await page.wait_for_timeout(300)
        try:
            await page.wait_for_function(
                "sel => ![...document.querySelectorAll(sel)].some("
                "e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')",
                arg=self.s["loader"],
                timeout=timeout,
            )
        except PlaywrightTimeout:
            pass

    async def _in_challenge(self, page: Page) -> bool:
        title = (await page.title()).lower()
        if any(t in title for t in CHALLENGE_TITLES):
            return True
        return await page.locator(self.s["challenge"]).count() > 0

    async def pass_challenge(self, page: Page) -> None:
        """If Cloudflare shows a challenge page, give it time to clear, then hand it to the user."""
        if not await self._in_challenge(page):
            return

        async def cleared() -> bool:
            return not await self._in_challenge(page)

        if await self._wait_until(cleared, AUTO_PASS_SECONDS):
            return
        await self._ask_human(page, "Cloudflare check - please complete it in the Chrome window")
        if not await self._wait_until(cleared, HUMAN_TIMEOUT):
            raise CheckoutError("Cloudflare check was not completed")

    async def _on_success_page(self, page: Page) -> bool:
        return self.s["success_url"] in page.url

    async def _order_number(self, page: Page) -> str:
        try:
            loc = page.locator(self.s["order_number"]).first
            if await loc.count():
                return (await loc.inner_text()).strip()
            match = re.search(r"order\s*(?:number|#)[^0-9]{0,15}(\d{5,})", await page.inner_text("body"), re.I)
        except PlaywrightError:
            return ""
        return match.group(1) if match else ""

    async def _hand_over(self, page: Page, reason: str, test_mode: bool) -> str:
        """Ask the user to finish the order by hand and wait for the success page."""
        if test_mode:
            raise CheckoutError(reason)
        await self._ask_human(page, f"{reason}. Finish the order in the Chrome window - the bot is waiting.")
        if await self._wait_until(lambda: self._on_success_page(page), HUMAN_TIMEOUT):
            return await self._order_number(page)
        raise CheckoutError(f"{reason} (not finished within {HUMAN_TIMEOUT // 60} min)")

    async def _price(self, page: Page) -> float | None:
        loc = page.locator(self.s["price"]).first
        if not await loc.count():
            return None
        amount = await loc.get_attribute("data-price-amount")
        text = amount or await loc.inner_text()
        match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
        return float(match.group().replace(",", "")) if match else None

    # ---------- account / cart ----------

    async def is_logged_in(self, page: Page, origin: str) -> bool:
        await page.goto(f"{origin}/customer/account/", wait_until="domcontentloaded", timeout=60000)
        await self.pass_challenge(page)
        return "/customer/account/login" not in page.url

    async def clear_cart(self, page: Page, origin: str) -> None:
        await page.goto(f"{origin}/checkout/cart/", wait_until="domcontentloaded", timeout=60000)
        await self.pass_challenge(page)
        removed = 0
        for _ in range(30):
            button = self._visible(page, "cart_delete_item").first
            if not await button.count():
                break
            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
                    await button.click()
            except PlaywrightTimeout:
                await page.wait_for_timeout(1000)  # theme removed the item without reloading
            removed += 1
        if removed:
            self._log(f"Cart cleared ({removed} item(s) removed)")

    # ---------- product ----------

    async def check_product(self, page: Page, url: str) -> StockInfo:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self.pass_challenge(page)
        try:
            await page.locator(f"{self.s['add_to_cart_button']}, {self.s['out_of_stock']}").first.wait_for(
                state="attached", timeout=10000
            )
        except PlaywrightTimeout:
            pass
        price = await self._price(page)
        if await self._visible(page, "out_of_stock").count():
            return StockInfo(False, price, "Out of stock")
        button = self._visible(page, "add_to_cart_button").first
        if not await button.count():
            return StockInfo(False, price, "No Add to Cart button")
        label = (await button.inner_text()).strip().lower()
        if not await button.is_enabled() or any(word in label for word in SOLD_OUT_WORDS):
            return StockInfo(False, price, "Out of stock")
        return StockInfo(True, price, "In stock")

    async def add_to_cart(self, page: Page, quantity: int) -> None:
        qty_input = self._visible(page, "qty_input").first
        if await qty_input.count():
            await qty_input.fill(str(quantity))
        button = self._visible(page, "add_to_cart_button").first
        request_part = self.s["add_to_cart_request"]
        try:
            async with page.expect_response(
                lambda r: request_part in r.url and r.request.method == "POST", timeout=20000
            ) as response_info:
                await button.click()
            response = await response_info.value
        except PlaywrightTimeout:
            raise CheckoutError("Add to cart did not respond") from None
        if response.status >= 400:
            raise CheckoutError(f"Add to cart failed (HTTP {response.status})")
        await page.wait_for_timeout(1000)
        error = self._visible(page, "message_error").first
        if await error.count():
            raise CheckoutError("Add to cart: " + (await error.inner_text()).strip()[:200])

    # ---------- checkout ----------

    async def checkout(self, page: Page, origin: str, test_mode: bool) -> str:
        """Run checkout. Returns the order number ("" if not shown); in test mode stops before Place Order."""
        await page.goto(f"{origin}/checkout/", wait_until="domcontentloaded", timeout=60000)
        await self.pass_challenge(page)
        if "/customer/account/login" in page.url:
            raise CheckoutError("Not logged in - use 'Open Chrome / Log in' first")
        if "/checkout/cart" in page.url:
            raise CheckoutError("Cart is empty - the item was not added")

        try:
            await page.locator(f"{self.s['shipping_continue']}, {self.s['payment_list']} >> visible=true").first.wait_for(
                timeout=60000
            )
            await self._wait_idle(page)

            # Shipping step: logged-in accounts have a default address; make sure a shipping method is picked.
            if await self._visible(page, "shipping_continue").count():
                radios = page.locator(self.s["shipping_method_radio"])
                if await radios.count() and not await radios.evaluate_all("els => els.some(e => e.checked)"):
                    await radios.first.check(force=True)
                    await self._wait_idle(page)
                await self._visible(page, "shipping_continue").first.click()
                await self._wait_idle(page)

            # Payment step
            await self._visible(page, "payment_list").first.wait_for(timeout=60000)
            await self._wait_idle(page)
            radios = page.locator(self.s["payment_method_radio"])
            preferred = self.s["preferred_payment_method"].strip()
            if preferred:
                choice = page.locator(f"{self.s['payment_method_radio']}[value^='{preferred}']")
                if await choice.count():
                    await choice.first.check(force=True)
                    await self._wait_idle(page)
                else:
                    self._log(f"Payment method '{preferred}' not found - using the default")
            elif await radios.count() and not await radios.evaluate_all("els => els.some(e => e.checked)"):
                await radios.first.check(force=True)
                await self._wait_idle(page)

            agreements = self._visible(page, "agreements")
            for i in range(await agreements.count()):
                await agreements.nth(i).check(force=True)
        except PlaywrightTimeout:
            return await self._hand_over(page, "Checkout page did not load as expected", test_mode)

        place_order = self._visible(page, "place_order").first
        if not await place_order.count():
            return await self._hand_over(page, "Place Order button not found", test_mode)
        if test_mode:
            return ""

        await place_order.click()
        if await self._wait_until(lambda: self._on_success_page(page), 30):
            return await self._order_number(page)
        return await self._hand_over(page, "Order not confirmed yet (captcha or card check?)", test_mode=False)
