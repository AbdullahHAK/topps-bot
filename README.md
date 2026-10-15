# Topps Bot (Chrome extension)

Monitors topps.com (Shopify, US) product pages from inside your normal Chrome. When a product comes in stock it adds it to the cart and checks out.

## How it works

- **Stock checks** run inside a pinned topps.com tab. Every 15–30 s (randomised) it reads Shopify's product data (`/products/<handle>.js`): availability and price per variant. This is a normal same-origin request with your own cookies, so a Cloudflare check you solve applies to it.
- **In stock** → empties the cart (optional) → adds the quantity with Shopify's `/cart/add.js` → opens checkout in a new tab.
- **Checkout** (`checkout.js`) waits for Shopify's **Pay now** button. In **test mode** it stops there. In live mode it clicks it. If the order isn't confirmed within 25 s (card not filled in, captcha, 3-D Secure…), it alerts you and waits for you to finish in that tab. The thank-you page is detected and the order number is logged.
- **Cloudflare check** → sound + notification, the Topps tab is brought to the front. You solve it and monitoring resumes by itself.
- **Not done:** captchas are never solved automatically, and card details are never stored by the bot.
- A product is skipped while its price is above its *Max price*. Checkout is retried up to 5 times. One checkout runs at a time. The bot never resumes by itself after Chrome restarts.

## Install (client)

1. Unzip `topps-bot-extension.zip` to a folder you'll keep (e.g. `Documents\TOPPS Bot`).
2. Chrome → `chrome://extensions` → turn on **Developer mode** (top right) → **Load unpacked** → pick that folder.
3. Pin the extension (puzzle icon → pin), then click its icon to open the dashboard.

## Use

1. **Open Topps / Log in** → sign in to your Topps account. To have *Pay now* complete by itself, the checkout must show your payment ready to go (e.g. Shop Pay with a saved card). Otherwise the bot gets you to checkout and you click pay.
2. Paste product URLs (`https://www.topps.com/products/...`). Set Qty and an optional Max price. A URL with `?variant=` buys that variant.
3. Keep **Test mode** on for a first run on an in-stock product → **Start**. It should stop at the Pay button with "TEST OK".
4. Untick Test mode for live drops. Keep Chrome open; the pinned Topps tab must stay open (the bot reopens it if closed).

## If Topps changes its checkout page

Dashboard → *Advanced* → selector overrides, e.g. `{"payButton": "button#pay-now"}`. Defaults are in [extension/common.js](extension/common.js).

## Development

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m tests.e2e_extension   & rem loads the extension into Chromium against a mock Shopify shop
powershell -File package.ps1                  & rem -> dist\topps-bot-extension.zip
```
