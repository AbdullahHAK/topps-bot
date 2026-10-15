"""Tiny local shop that mimics topps.com (Next.js storefront + Shopify checkout) for end-to-end tests.

The product page embeds variant data the same way topps.com does (escaped JSON inside self.__next_f.push),
and uses the same controls: option radios, a "+" quantity button, data-testid="product-add-to-cart",
a cart pop-up with "Continue to Checkout", and a cart_id cookie.

Run on its own:  python -m tests.mock_shop   ->  http://127.0.0.1:8766/products/test-box
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VARIANTS = [  # (variant id, title, price)
    ("111", "1 card", "8.99"),
    ("555", "5 cards", "34.99"),
]
STATE: dict = {}


def reset() -> None:
    STATE.update(stock=False, carts={}, orders=[], challenge=False, payment_ready=True, limit=0)


reset()
_lock = threading.Lock()

CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"


def product_page() -> str:
    data = {"product": {"title": "2026 Topps Test Box", "variants": [
        {"price": {"amount": price, "currencyCode": "USD"}, "compareAtPrice": None,
         "id": f"gid://shopify/ProductVariant/{vid}", "sku": f"SKU{vid}", "title": title,
         "availableForSale": STATE["stock"]}
        for vid, title, price in VARIANTS
    ]}}
    rsc = json.dumps("6:" + json.dumps(data, separators=(",", ":")))
    radios = "".join(
        f'<label for="SKU{vid}"><input type="radio" id="SKU{vid}" name="variant-options" value="SKU{vid}" '
        f'data-vid="{vid}" aria-label="{title} - ${price}" {"checked" if i == 0 else ""}> {title}</label>'
        for i, (vid, title, price) in enumerate(VARIANTS)
    )
    button = ('<button data-testid="product-add-to-cart" onclick="add()">Add to cart</button>' if STATE["stock"]
              else '<button data-testid="product-add-to-cart" disabled>Sold out</button>')
    return f"""<html><head><title>2026 Topps Test Box</title></head><body>
<h2>2026 Topps Test Box</h2>{radios}
<button aria-label="Decrease quantity by one" onclick="q(-1)">-</button>
<input name="quantity" value="1">
<button aria-label="Increase quantity by one" onclick="q(1)">+</button>
{button}
<p id="msg"></p>
<div id="popover" style="display:none">Added to cart <button onclick="location.href=checkoutUrl">Continue to Checkout</button></div>
<script>self.__next_f = []; self.__next_f.push([1,{rsc}])</script>
<script>
let checkoutUrl;
function q(d) {{ const i = document.querySelector('input[name=quantity]'); i.value = Math.max(1, Number(i.value) + d); }}
async function add() {{
  const variant = document.querySelector('input[name=variant-options]:checked').dataset.vid;
  const qty = Number(document.querySelector('input[name=quantity]').value);
  const r = await fetch('/api/cart/add', {{method: 'POST', body: JSON.stringify({{variant, qty}})}});
  const j = await r.json();
  if (j.error) {{ document.getElementById('msg').textContent = j.error; return; }}
  checkoutUrl = j.checkoutUrl;
  document.getElementById('popover').style.display = 'block';
}}
</script></body></html>"""


CHECKOUT = """<html><head><title>Checkout - Topps</title></head><body>
<h1>Checkout</h1><p id="msg"></p>
<label><input type="checkbox" id="marketing_opt_in"> Email me with news and offers</label>
<label for="agree"><input type="checkbox" id="agree"> I agree to the Topps product terms</label>
<button id="checkout-pay-button" type="submit" disabled>Pay now</button>
<script>
const btn = document.getElementById('checkout-pay-button');
setTimeout(() => { btn.disabled = false; }, 800);   // Shopify enables the button once totals load
btn.addEventListener('click', async () => {
  if (!document.getElementById('agree').checked) { msg.textContent = 'Please check the box to continue.'; return; }
  const base = location.pathname.replace(/\\/en-us$/, '');
  const r = await fetch(base + '/complete', {method: 'POST',
    body: JSON.stringify({marketing: document.getElementById('marketing_opt_in').checked})});
  if (r.ok) location.href = location.pathname + '/thank-you';
  else msg.textContent = 'Enter a card number';
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8", headers=()) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status: int = 200, headers=()) -> None:
        self._send(json.dumps(obj), status, "application/json", headers)

    def _cart_id(self) -> str | None:
        match = re.search(r"(?:^|;\s*)cart_id=([^;]+)", self.headers.get("Cookie") or "")
        return match.group(1) if match else None

    def do_GET(self):
        path = urlparse(self.path).path
        with _lock:
            if STATE["challenge"] and not path.startswith("/checkouts"):
                self._send(CHALLENGE, 403)
            elif path in ("/", "/customer/account/login"):
                self._send(f"<html><head><title>Topps</title></head><body>{path}</body></html>")
            elif path == "/products/test-box":
                self._send(product_page())
            elif path == "/checkout/cart":
                cart = STATE["carts"].get(self._cart_id() or "", [])
                body = ("<button onclick=\"location.href='/checkouts/cn/%s/en-us'\">Proceed to checkout</button>"
                        % self._cart_id()) if cart else "Your cart's empty"
                self._send(f"<html><head><title>Cart</title></head><body>{body}</body></html>")
            elif re.fullmatch(r"/checkouts/cn/[\w-]+/en-us", path):
                self._send(CHECKOUT)
            elif re.fullmatch(r"/checkouts/cn/[\w-]+/en-us/thank-you", path):
                self._send("<html><head><title>Thank you</title></head><body><h1>Thank you!</h1>"
                           f"<p>Confirmation #T0PPS{len(STATE['orders'])}</p></body></html>")
            else:
                self._send("Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with _lock:
            if path == "/api/cart/add":
                if not STATE["stock"]:
                    return self._json({"error": "Failed to add to cart"})
                if STATE["limit"] and body["qty"] > STATE["limit"]:
                    return self._json({"error": f"You've hit the limit — only {STATE['limit']} of this product per household."})
                cart_id = self._cart_id()
                headers = []
                if not cart_id or cart_id not in STATE["carts"]:
                    cart_id = uuid.uuid4().hex[:10]
                    STATE["carts"][cart_id] = []
                    headers.append(("Set-Cookie", f"cart_id={cart_id}; Path=/"))
                STATE["carts"][cart_id].append((body["variant"], body["qty"]))
                self._json({"ok": True, "checkoutUrl": f"/checkouts/cn/{cart_id}/en-us"}, headers=headers)
            elif match := re.fullmatch(r"/checkouts/cn/([\w-]+)/complete", path):
                if not STATE["payment_ready"]:
                    return self._json({"error": "payment"}, 400)
                STATE["orders"].append({"items": list(STATE["carts"].get(match.group(1), [])),
                                        "marketing": body.get("marketing")})
                STATE["carts"].pop(match.group(1), None)
                self._json({})
            else:
                self._send("Not found", 404)


def serve(port: int = 8766) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    STATE["stock"] = True
    serve()
    print("Mock shop on http://127.0.0.1:8766/products/test-box  (Ctrl+C to stop)")
    threading.Event().wait()
