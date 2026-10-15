# Topps Bot (Chrome extension)

Monitors topps.com (US) product pages from inside your normal Chrome. When a product comes in stock it adds it to the cart and checks out.

topps.com is a Next.js storefront (Vercel) on top of Shopify; checkout is Shopify checkout on `shop.topps.com`.

## How it works

- **Stock checks** run inside a pinned topps.com tab. Every 15–30 s (randomised) it fetches the product page and reads the variant data embedded in it (`availableForSale` and price for each option, e.g. "1 card", "5 cards"). This is a normal same-origin request with your own cookies, so a Cloudflare check you solve applies to it.
- **In stock** → empties the cart (deletes the `cart_id` cookie, optional) → opens the product page, picks the option, sets the quantity with the "+" button, clicks **Add to cart** → **Continue to Checkout**. Site errors such as purchase limits are shown in the status.
- **Checkout** (`checkout.js`) waits for Shopify's **Pay now** button (and through the checkout queue, if there is one). In **test mode** it stops there. In live mode it ticks required "I agree" boxes (never marketing opt-ins) and clicks Pay. Topps' checkout has **no Shop Pay or saved cards**, so unless the card is already filled in, the order won't go through by itself: the bot alerts you and waits for you to enter the card (Chrome autofill works) and click Pay. The thank-you page is detected and the order number is logged.
- **Cloudflare check** → sound + notification, the Topps tab is brought to the front. You solve it and monitoring resumes by itself.
- **Not done:** captchas are never solved automatically, and card details are never stored by the bot.
- A product is skipped while its price is above its *Max price*. Checkout is retried up to 5 times. One checkout runs at a time. The bot never resumes by itself after Chrome restarts.

## Install (client)

1. Unzip `topps-bot-extension.zip` to a folder you'll keep (e.g. `Documents\TOPPS Bot`).
2. Chrome → `chrome://extensions` → turn on **Developer mode** (top right) → **Load unpacked** → pick that folder.
3. Pin the extension (puzzle icon → pin), then click its icon to open the dashboard.

## Use

1. **Open Topps / Log in** → sign in to your Topps account (some products require it, and your address is then pre-filled at checkout). Save your card in Chrome (Settings → Payment methods) so filling it at checkout is one click.
2. Paste product URLs (`https://www.topps.com/products/...`). Set the **Option** exactly as shown on the page (e.g. `5 cards`; empty = first in-stock option), the **Qty** and an optional **Max price**.
3. Keep **Test mode** on for a first run on an in-stock product → **Start**. It should stop at the Pay button with "TEST OK".
4. Untick Test mode for live drops. Keep Chrome open; the pinned Topps tab must stay open (the bot reopens it if closed).

## If Topps changes its checkout page

Dashboard → *Advanced* → selector overrides, e.g. `{"addToCartButton": "button.add-to-cart"}`. Defaults are in [extension/common.js](extension/common.js).

## Development

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m tests.e2e_extension   & rem loads the extension into Chromium against a mock Shopify shop
powershell -File package.ps1                  & rem -> dist\topps-bot-extension.zip
```
