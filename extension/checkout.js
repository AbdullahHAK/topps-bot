// Runs on topps.com pages, but only acts in the checkout tab the bot opened (it asks the background first).
// Checkout is Shopify's one-page checkout on shop.topps.com.
(() => {
  if (!/\/checkouts\/|\/orders\//.test(location.pathname)) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const send = (type, extra = {}) => chrome.runtime.sendMessage({ type, ...extra }).catch(() => null);
  const usable = (el) =>
    !!el && el.getClientRects().length > 0 && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const pageText = () => document.body?.innerText || '';

  async function waitFor(fn, timeout) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = fn();
      if (value) return value;
      await sleep(250);
    }
    return null;
  }

  // Required "I agree" boxes (e.g. Topps' product agreement); never marketing opt-ins.
  function tickAgreements() {
    for (const box of document.querySelectorAll('input[type="checkbox"]')) {
      if (box.checked || !box.getClientRects().length) continue;
      const label = (document.querySelector(`label[for="${CSS.escape(box.id)}"]`)?.textContent
        || box.closest('label')?.textContent || box.getAttribute('aria-label') || '').toLowerCase();
      if (/agree|acknowledge|terms/.test(label) && !/news|offers|text me|save this/.test(label)) box.click();
    }
  }

  (async () => {
    const cfg = await send('checkout:hello');
    if (!cfg?.drive) return;
    const S = cfg.selectors;

    if (new RegExp(S.thankYouUrl, 'i').test(location.pathname)) {
      await sleep(2000); // let the confirmation render
      const match = pageText().match(/(?:order|confirmation)\s*(?:number|no\.?)?\s*#\s*([A-Z0-9][A-Z0-9-]{3,})/i);
      return send('checkout:success', { order: match ? match[1] : '' });
    }

    const payButton = () => {
      const byId = document.querySelector(S.payButton);
      if (byId) return byId;
      return [...document.querySelectorAll('button')]
        .find((b) => /^(pay now|complete order|place order)$/i.test(b.textContent.trim()));
    };

    send('checkout:status', { text: 'Checkout open - waiting for the Pay button...' });
    let found = await waitFor(() => usable(payButton()), 60000);
    if (!found && /you're in line|in the queue|waiting room/i.test(pageText())) {
      send('checkout:status', { text: 'In the Topps checkout queue - waiting...' });
      found = await waitFor(() => usable(payButton()), 10 * 60000);
    }
    if (!found) return send('checkout:needs_user', { reason: 'Pay button not found' });

    // Shopify fills in shipping and payment asynchronously: wait until the button has stayed usable for 2 s.
    let stableFor = 0;
    const end = Date.now() + 20000;
    while (stableFor < 2000 && Date.now() < end) {
      await sleep(250);
      stableFor = usable(payButton()) ? stableFor + 250 : 0;
    }

    if (cfg.testMode) return send('checkout:test_ok');

    tickAgreements();
    await sleep(500);
    send('checkout:status', { text: 'Clicking Pay now...' });
    payButton().click();
    // On success the page navigates to the thank-you page and this script starts over there.
    await sleep(25000);
    send('checkout:needs_user', { reason: 'Order not confirmed yet (card details, captcha or card check needed)' });
  })();
})();
