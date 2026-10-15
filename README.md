# Topps Bot

Windows desktop app that monitors topps.com (US) product pages and, when a product comes in stock, adds it to the cart and checks out with the logged-in account's saved address and card.

## How it works

- Drives the **installed Google Chrome** (Playwright) with a dedicated profile in `%LOCALAPPDATA%\ToppsBot\chrome-profile`, so the Topps login is remembered.
- One tab per product, reloaded every *min–max* seconds (randomised).
- In stock → add to cart (quantity per product) → `/checkout/` → shipping method → payment → agreements → **Place Order**.
- Checkouts run one at a time (the cart is shared).
- Captchas, Cloudflare checks, 3-D Secure etc. are **not** solved automatically: the bot plays a sound, shows a notification, brings Chrome to the front and waits (up to 10 min) for the user to finish, then continues.
- **Test mode** (on by default) runs the whole flow but stops before clicking Place Order.
- A product is retried up to 5 times if checkout fails; it is skipped if its price is above its "Max price".

## Using it

1. Install Google Chrome.
2. Run `ToppsBot.exe` → **Open Chrome / Log in** → sign in to topps.com in that Chrome window (once).
3. Paste product URLs (set Qty / Max price) → **Start**. **Stop** halts everything.
4. Do a test-mode run on an in-stock product first, then untick Test mode for live drops.

Settings and the product list: `%APPDATA%\ToppsBot\settings.json`. Log file: `%APPDATA%\ToppsBot\toppsbot.log`.

## If Topps changes its site

All page selectors are in `DEFAULT_SELECTORS` in [toppsbot/store.py](toppsbot/store.py). Any of them can be overridden without rebuilding by adding a `"selectors"` object to `settings.json`, e.g.

```json
"selectors": {
  "add_to_cart_button": "button.add-to-cart",
  "preferred_payment_method": "braintree_cc_vault"
}
```

## Development

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py             & rem run the GUI
.venv\Scripts\python -m tests.e2e_mock  & rem end-to-end test against a local mock Magento shop
build.bat                                & rem -> dist\ToppsBot.exe
```
