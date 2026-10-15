"""Tiny local shop that mimics Magento 2 / topps.com markup, for testing the bot end to end.

Run on its own:  python -m tests.mock_shop   ->  http://127.0.0.1:8765/p/test-box.html
Toggle stock:    http://127.0.0.1:8765/admin/stock?v=1  (or v=0)
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"stock": False, "price": "49.99", "cart": [], "orders": []}
_lock = threading.Lock()

PRODUCT = """<html><head><title>Test Box</title></head><body>
<div class="page messages"><div id="msgs"></div></div>
<div class="product-info-main">
  <h1>2026 Topps Test Box</h1>
  <span data-price-type="finalPrice" data-price-amount="{price}"><span class="price">${price}</span></span>
  {stock}
</div>
<script>
async function add() {{
  const qty = document.getElementById('qty').value;
  const r = await fetch('/checkout/cart/add', {{method: 'POST', body: 'qty=' + qty,
    headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}}});
  const j = await r.json();
  if (j.error) document.getElementById('msgs').innerHTML = '<div class="message-error">' + j.error + '</div>';
}}
</script></body></html>"""

IN_STOCK = """<div class="stock available">In stock</div>
<input id="qty" value="1"><button id="product-addtocart-button" onclick="add()"><span>Add to Cart</span></button>"""
OUT_OF_STOCK = '<div class="stock unavailable">Out of stock</div>'

CHECKOUT = """<html><head><title>Checkout</title></head><body>
<div class="loading-mask" id="lm" style="position:fixed;inset:0;background:#0003">Loading...</div>
<div id="shipping" style="display:none">
  <div id="checkout-shipping-method-load">
    <label><input type="radio" name="ko_unique_1" value="flat"> Flat rate</label>
    <label><input type="radio" name="ko_unique_2" value="free"> Free</label>
  </div>
  <div id="shipping-method-buttons-container"><button type="button" class="continue" onclick="toPayment()">Next</button></div>
</div>
<div id="payment" style="display:none"><div id="checkout-payment-method-load">
  <div class="payment-method" id="pm1">
    <label><input type="radio" name="payment[method]" value="checkmo" onclick="act('pm1')"> Check / money order</label>
    <div class="checkout-agreements"><input type="checkbox"> I agree</div>
    <div class="actions-toolbar"><button class="action primary checkout" onclick="place()">Place Order</button></div>
  </div>
  <div class="payment-method" id="pm2">
    <label><input type="radio" name="payment[method]" value="braintree_cc_vault_1" onclick="act('pm2')"> Saved Visa</label>
    <div class="checkout-agreements"><input type="checkbox"> I agree</div>
    <div class="actions-toolbar"><button class="action primary checkout" onclick="place()">Place Order</button></div>
  </div>
</div></div>
<script>
setTimeout(() => { lm.style.display = 'none'; shipping.style.display = 'block'; }, 800);
function toPayment() {
  if (!document.querySelector('#checkout-shipping-method-load input:checked')) return;
  lm.style.display = 'block';
  setTimeout(() => { lm.style.display = 'none'; shipping.style.display = 'none'; payment.style.display = 'block'; }, 600);
}
function act(id) { document.querySelectorAll('.payment-method').forEach(e => e.classList.toggle('_active', e.id === id)); }
async function place() {
  const agree = document.querySelector('.payment-method._active .checkout-agreements input');
  if (!agree || !agree.checked) return;
  const method = document.querySelector('.payment-method._active input').value;
  await fetch('/checkout/place', {method: 'POST', body: method});
  location.href = '/checkout/onepage/success/';
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: str, status: int = 200, ctype: str = "text/html") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        with _lock:
            if url.path.startswith("/p/"):
                stock = IN_STOCK if STATE["stock"] else OUT_OF_STOCK
                self._send(PRODUCT.format(price=STATE["price"], stock=stock))
            elif url.path == "/customer/account/":
                self._send("<html><head><title>My Account</title></head><body>My Account</body></html>")
            elif url.path == "/checkout/cart/":
                items = "".join(
                    f'<div class="cart item">Test Box x{qty} <a class="action action-delete" '
                    f'href="/checkout/cart/delete?i={i}">Remove</a></div>'
                    for i, qty in enumerate(STATE["cart"])
                )
                self._send(f"<html><head><title>Cart</title></head><body>{items or 'Cart is empty'}</body></html>")
            elif url.path == "/checkout/cart/delete":
                i = int(query.get("i", ["0"])[0])
                if i < len(STATE["cart"]):
                    STATE["cart"].pop(i)
                self._redirect("/checkout/cart/")
            elif url.path == "/checkout/":
                if STATE["cart"]:
                    self._send(CHECKOUT)
                else:
                    self._redirect("/checkout/cart/")
            elif url.path == "/checkout/onepage/success/":
                number = f"{len(STATE['orders']):09d}"
                self._send(
                    '<html><head><title>Success</title></head><body><div class="checkout-success">'
                    f'<p>Your order number is: <a class="order-number"><strong>{number}</strong></a>.</p>'
                    "</div></body></html>"
                )
            elif url.path == "/admin/stock":
                STATE["stock"] = query.get("v", ["1"])[0] == "1"
                self._send(f"stock={STATE['stock']}", ctype="text/plain")
            else:
                self._send("Not found", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        with _lock:
            if self.path == "/checkout/cart/add":
                if not STATE["stock"]:
                    self._send(json.dumps({"error": "Product is out of stock"}), ctype="application/json")
                    return
                STATE["cart"].append(int(parse_qs(body).get("qty", ["1"])[0]))
                self._send("{}", ctype="application/json")
            elif self.path == "/checkout/place":
                STATE["orders"].append({"items": list(STATE["cart"]), "payment": body})
                STATE["cart"].clear()
                self._send("{}", ctype="application/json")
            else:
                self._send("Not found", 404)


def serve(port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve()
    print("Mock shop on http://127.0.0.1:8765/p/test-box.html  (Ctrl+C to stop)")
    threading.Event().wait()
