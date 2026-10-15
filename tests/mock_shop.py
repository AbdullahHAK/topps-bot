"""Tiny local shop that mimics the Shopify endpoints/markup topps.com uses, for end-to-end tests.

Run on its own:  python -m tests.mock_shop   ->  http://127.0.0.1:8766/products/test-box
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

VARIANT_ID = 4242
STATE: dict = {}


def reset() -> None:
    STATE.update(stock=False, price=4999, cart=[], orders=[], challenge=False, payment_ready=True)


reset()
_lock = threading.Lock()

CHALLENGE = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"

CHECKOUT = """<html><head><title>Checkout - Topps</title></head><body>
<h1>Checkout</h1><p id="msg"></p>
<button id="checkout-pay-button" type="submit" disabled>Pay now</button>
<script>
const btn = document.getElementById('checkout-pay-button');
setTimeout(() => { btn.disabled = false; }, 800);   // Shopify enables the button once totals load
btn.addEventListener('click', async () => {
  const r = await fetch('/checkouts/cn/tok123/complete', {method: 'POST'});
  if (r.ok) location.href = '/checkouts/cn/tok123/thank-you';
  else document.getElementById('msg').textContent = 'Enter a card number';
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj), status, "application/json; charset=utf-8")

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        with _lock:
            if STATE["challenge"] and not path.startswith("/checkouts"):
                self._send(CHALLENGE, 403)
            elif path == "/products/test-box.js":
                self._json({
                    "title": "2026 Topps Test Box",
                    "available": STATE["stock"],
                    "variants": [{"id": VARIANT_ID, "title": "Default Title", "available": STATE["stock"],
                                  "price": STATE["price"]}],
                })
            elif path in ("/", "/products/test-box", "/account/login"):
                self._send(f"<html><head><title>Topps</title></head><body>{path}</body></html>")
            elif path == "/checkout":
                self._redirect("/checkouts/cn/tok123" if STATE["cart"] else "/cart")
            elif path == "/checkouts/cn/tok123":
                self._send(CHECKOUT)
            elif path == "/checkouts/cn/tok123/thank-you":
                self._send("<html><head><title>Thank you</title></head><body><h1>Thank you!</h1>"
                           f"<p>Confirmation #T0PPS{len(STATE['orders'])}</p></body></html>")
            else:
                self._send("Not found", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        with _lock:
            if path == "/cart/add.js":
                if not STATE["stock"]:
                    self._json({"status": 422, "description": "Sold out"}, 422)
                    return
                for item in json.loads(body)["items"]:
                    STATE["cart"].append((item["id"], item["quantity"]))
                self._json({"items": []})
            elif path == "/cart/clear.js":
                STATE["cart"].clear()
                self._json({"item_count": 0})
            elif path == "/checkouts/cn/tok123/complete":
                if not STATE["payment_ready"]:
                    self._json({"error": "payment"}, 400)
                    return
                STATE["orders"].append(list(STATE["cart"]))
                STATE["cart"].clear()
                self._json({})
            else:
                self._send("Not found", 404)


def serve(port: int = 8766) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    serve()
    print("Mock shop on http://127.0.0.1:8766/products/test-box  (Ctrl+C to stop)")
    threading.Event().wait()
