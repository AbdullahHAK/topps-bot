// Runs on topps.com pages, but only acts in the checkout tab the bot opened (it asks the background first).
(() => {
  if (!/\/checkouts?(\/|$)|\/orders\//.test(location.pathname)) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const send = (type, extra = {}) => chrome.runtime.sendMessage({ type, ...extra }).catch(() => null);
  const usable = (el) =>
    !!el && el.getClientRects().length > 0 && !el.disabled && el.getAttribute('aria-disabled') !== 'true';

  async function waitFor(fn, timeout) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = fn();
      if (value) return value;
      await sleep(250);
    }
    return null;
  }

  (async () => {
    const cfg = await send('checkout:hello');
    if (!cfg?.drive) return;
    const S = cfg.selectors;

    if (new RegExp(S.thankYouUrl, 'i').test(location.pathname)) {
      await sleep(1500); // let the confirmation render
      const text = document.body.innerText;
      const match = text.match(/(?:order|confirmation)\s*(?:number|no\.?)?\s*#\s*([A-Z0-9][A-Z0-9-]{3,})/i);
      return send('checkout:success', { order: match ? match[1] : '' });
    }

    send('checkout:status', { text: 'Checkout open - waiting for the Pay button...' });
    const payButton = () => document.querySelector(S.payButton);
    if (!(await waitFor(() => usable(payButton()), 60000))) {
      return send('checkout:needs_user', { reason: 'Pay button not found' });
    }
    // Shopify fills in shipping and payment asynchronously: wait until the button has stayed usable for 2 s.
    let stableFor = 0;
    const end = Date.now() + 20000;
    while (stableFor < 2000 && Date.now() < end) {
      await sleep(250);
      stableFor = usable(payButton()) ? stableFor + 250 : 0;
    }

    if (cfg.testMode) return send('checkout:test_ok');

    send('checkout:status', { text: 'Clicking Pay now...' });
    payButton().click();
    // On success the page navigates to the thank-you page and this script starts over there.
    await sleep(25000);
    send('checkout:needs_user', { reason: 'Order not confirmed yet (payment details, captcha or card check)' });
  })();
})();
