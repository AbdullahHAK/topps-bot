// Bot engine (service worker).
//
// topps.com is a Next.js storefront on top of Shopify:
// - Stock check: the product page HTML is fetched from inside a pinned topps.com tab (same-origin, the user's
//   own cookies and Cloudflare clearance) and the embedded variant data (availableForSale, price) is read.
// - Buy: a tab opens the product page; the page's own option / quantity / "Add to cart" controls are used,
//   then "Continue to Checkout" leads to Shopify checkout on shop.topps.com, driven by checkout.js.
//
// storage.local keys: products, settings, running, statuses, log, run (state of the current run).

import {
  CART_COOKIES, CHECKOUT_TIMEOUT_MS, DEFAULT_ORIGIN, DEFAULT_SELECTORS, DEFAULT_SETTINGS, MAX_FAILURES,
  originOf, productHandle, shortName, siteOf,
} from './common.js';

const LOG_MAX = 400;
let ticking = false;
let tickTimer = null;

// ---------- storage helpers ----------

const get = (keys) => chrome.storage.local.get(keys);

// All read-modify-write updates go through one chain so concurrent updates don't overwrite each other.
let writeChain = Promise.resolve();
function mutate(key, fn, fallback) {
  writeChain = writeChain
    .then(async () => {
      const current = (await get(key))[key] ?? fallback;
      await chrome.storage.local.set({ [key]: fn(current) });
    })
    .catch((e) => console.error('storage update failed', e));
  return writeChain;
}

const updateRun = (fn) => mutate('run', fn, {});
const log = (msg, level = 'info') =>
  mutate('log', (entries) => [...entries, { t: Date.now(), level, msg }].slice(-LOG_MAX), []);
const setStatus = (id, text, kind = '') =>
  mutate('statuses', (s) => ({ ...s, [id]: { text, kind, t: Date.now() } }), {});

async function getSettings() {
  const { settings } = await get('settings');
  return { ...DEFAULT_SETTINGS, ...settings };
}

async function getSelectors() {
  return { ...DEFAULT_SELECTORS, ...(await getSettings()).selectors };
}

async function getRun() {
  const { run } = await get('run');
  return { done: [], failures: {}, checkout: null, challenge: false, workerTabId: null, lastTick: 0, ...run };
}

// ---------- alerts ----------

async function alertUser(title, message) {
  await log(`${title}: ${message}`, 'warn');
  chrome.notifications.create({
    type: 'basic', iconUrl: 'icons/icon128.png', title, message, priority: 2, requireInteraction: true,
  });
  if ((await getSettings()).sound) beep();
}

async function beep() {
  try {
    if (!(await chrome.offscreen.hasDocument())) {
      await chrome.offscreen.createDocument({
        url: 'offscreen.html', reasons: ['AUDIO_PLAYBACK'], justification: 'Alert sound for stock and checkout events',
      });
    }
    await chrome.runtime.sendMessage({ type: 'beep' });
  } catch (e) {
    console.warn('beep failed', e);
  }
}

async function focusTab(tabId) {
  try {
    const tab = await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  } catch {
    // tab is gone
  }
}

// ---------- tabs ----------

function waitForTabLoad(tabId, timeout = 45000) {
  return new Promise((resolve) => {
    const finish = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === 'complete') finish();
    };
    const timer = setTimeout(finish, timeout);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => t.status === 'complete' && finish(), finish);
  });
}

async function runInTab(tabId, func, ...args) {
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
    return result?.result ?? { error: 'No result from the page' };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

// The pinned tab that runs the stock checks.
async function workerTab(origin) {
  const run = await getRun();
  if (run.workerTabId != null) {
    try {
      const tab = await chrome.tabs.get(run.workerTabId);
      if (siteOf(tab.url || tab.pendingUrl || '') === siteOf(origin)) return tab;
    } catch {
      // closed - make a new one
    }
  }
  const tab = await chrome.tabs.create({ url: `${origin}/`, active: false, pinned: true });
  await updateRun((r) => ({ ...r, workerTabId: tab.id }));
  await log('Opened a pinned Topps tab for monitoring - keep it open');
  await waitForTabLoad(tab.id);
  return tab;
}

async function inWorker(origin, func, ...args) {
  try {
    const tab = await workerTab(origin);
    if (tab.status !== 'complete') await waitForTabLoad(tab.id, 20000);
    return await runInTab(tab.id, func, ...args);
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

// Deleting the cart cookies makes topps.com start a fresh, empty cart.
// Empties the cart with the cart page's own "Remove" buttons (a logged-in cart lives on the account, so
// dropping the cart cookie alone is not enough), then drops the cart cookies too.
// Uses `tabId` if given, otherwise a temporary background tab.
async function clearCart(origin, tabId = null) {
  const tab = tabId ?? (await chrome.tabs.create({ url: 'about:blank', active: false })).id;
  try {
    await chrome.tabs.update(tab, { url: `${origin}/checkout/cart` });
    await waitForTabLoad(tab);
    let removed = 0;
    for (let i = 0; i < 40; i++) {
      const step = await runInTab(tab, pageRemoveOneCartItem, i === 0 ? 15000 : 8000);
      if (step.empty) break;
      if (step.removed) {
        removed += 1;
        await new Promise((r) => setTimeout(r, 1500)); // let the removal finish (and any reload start)
        await waitForTabLoad(tab, 15000);
        continue;
      }
      if (!/Frame|context|navigat|closed/i.test(step.error || '')) {
        await log(`Could not empty the cart: ${step.error}`, 'warn');
        break;
      }
      await waitForTabLoad(tab, 15000); // script was cut off by a reload - look again
    }
    if (removed) await log(`Cart emptied (${removed} item(s) removed)`);
  } finally {
    if (tabId == null) chrome.tabs.remove(tab).catch(() => {});
  }
  const cookies = await chrome.cookies.getAll({ domain: new URL(origin).hostname.replace(/^www\./, '') });
  for (const c of cookies.filter((x) => CART_COOKIES.includes(x.name))) {
    const host = c.domain.replace(/^\./, '');
    await chrome.cookies.remove({ url: `http${c.secure ? 's' : ''}://${host}${c.path}`, name: c.name });
  }
}

// ---------- functions injected into topps.com pages (must be self-contained) ----------

// Runs in the pinned tab: reads stock and price of every variant from the product page HTML.
async function pageProduct(path, option) {
  try {
    const r = await fetch(path, { cache: 'no-store', credentials: 'same-origin' });
    const html = await r.text();
    if ([403, 429, 503].includes(r.status) || /<title>\s*Just a moment/i.test(html)) return { challenge: true };
    if (r.status === 404) return { error: 'Product not found (404)' };
    if (!r.ok) return { error: `HTTP ${r.status}` };

    let text = html.replace(/\\"/g, '"');
    // Box products embed sibling products (Hobby / Jumbo box…) too: keep only this page's product.
    const productId = (html.match(/data-product-id="gid:\/\/shopify\/Product\/(\d+)"/) || [])[1];
    const start = productId ? text.indexOf(`"id":"gid://shopify/Product/${productId}"`) : -1;
    if (start >= 0) {
      const end = text.indexOf('"aggregatedVariants"', start);
      text = text.slice(start, end > start ? end : undefined);
    }
    const variants = new Map();
    const productRe = /"price":\{"amount":"([\d.]+)","currencyCode":"[A-Z]+"\},"compareAtPrice":(?:null|\{[^}]*\}),"id":"gid:\/\/shopify\/ProductVariant\/(\d+)","sku":"[^"]*","title":"([^"]*)","availableForSale":(true|false)/g;
    for (const m of text.matchAll(productRe)) {
      variants.set(m[2], { id: m[2], price: Number(m[1]), title: m[3], available: m[4] === 'true' });
    }
    if (!variants.size) {
      const buyBoxRe = /"title":"([^"]*)","price":([\d.]+),"option_type_id":"gid:\/\/shopify\/ProductVariant\/(\d+)","compareAtPrice":[^,]*,"sku":"[^"]*","availableForSale":(true|false)/g;
      for (const m of text.matchAll(buyBoxRe)) {
        variants.set(m[3], { id: m[3], price: Number(m[2]), title: m[1], available: m[4] === 'true' });
      }
    }
    if (!variants.size) return { error: 'Could not read product data - the page layout may have changed' };

    const all = [...variants.values()];
    let variant;
    if (option && all.length > 1) {
      const want = option.trim().toLowerCase();
      variant = all.find((v) => v.title.toLowerCase() === want);
      if (!variant) return { error: `Option "${option}" not found (options: ${all.map((v) => v.title).join(', ')})` };
    } else {
      variant = all.find((v) => v.available) || all[0];
    }
    const title = (html.match(/<title>([^<]*)<\/title>/i) || [])[1] || '';
    return { title: title.replace(/&amp;/g, '&').trim(), variant, optionCount: all.length };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

// Runs in the buy tab on the product page: picks the option, sets quantity and clicks "Add to cart".
async function pageAddToCart(option, quantity, S) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = async (fn, timeout) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = fn();
      if (value) return value;
      await sleep(200);
    }
    return null;
  };
  const addButton = () => document.querySelector(S.addToCartButton);
  const continueButton = () => [...document.querySelectorAll('a, button')]
    .find((e) => /continue to checkout/i.test(e.textContent) && e.getClientRects().length);

  if (!(await waitFor(addButton, 30000))) return { error: 'Add to cart button not found' };
  await sleep(1500); // let the page finish hydrating so clicks are handled

  // Card products use option radios ("1 card", "5 cards"). Products without them (e.g. a box, where
  // Hobby / Value / Jumbo are separate product pages) are bought as-is.
  const radios = [...document.querySelectorAll(S.optionRadio)];
  if (option && radios.length) {
    const want = option.trim().toLowerCase();
    const radio = radios
      .find((r) => (r.getAttribute('aria-label') || '').split(' - ')[0].trim().toLowerCase() === want);
    if (!radio) return { error: `Option "${option}" is not on the page` };
    if (!radio.checked) {
      (radio.closest('label') || radio).click();
      await sleep(500);
    }
  }

  const qty = document.querySelector(S.quantityInput);
  const plus = document.querySelector(S.quantityPlus);
  if (qty && plus) {
    for (let i = 0; i < 99 && Number(qty.value) < quantity && !plus.disabled; i++) {
      plus.click();
      await sleep(150);
    }
    if (Number(qty.value) !== quantity) return { error: `Could not set quantity to ${quantity} (page allows ${qty.value})` };
  }

  const button = addButton();
  if (button.disabled || /sold out|out of stock/i.test(button.textContent)) return { error: 'Sold out' };
  button.click();

  const result = await waitFor(() => {
    if (continueButton() || /added to cart/i.test(document.body.innerText)) return { ok: true };
    const error = document.body.innerText.match(
      /(You've hit the limit[^\n]*|You can only add[^\n]*|You have already ordered[^\n]*|Error adding to cart|Failed to add to cart|[^\n]*can't be added to the cart|You must sign in to add this item[^\n]*)/i,
    );
    return error ? { error: error[1].trim() } : null;
  }, 20000);
  return result || { error: 'No confirmation after clicking Add to cart' };
}

// Runs on /checkout/cart: clicks one "Remove" (including on unavailable items), or reports the cart is empty.
// One click per call, because removing may reload the page and end this script.
async function pageRemoveOneCartItem(timeout) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (/your cart's empty/i.test(document.body.innerText)) return { empty: true };
    const remove = [...document.querySelectorAll('[role="button"], button')]
      .find((e) => e.textContent.trim().toLowerCase() === 'remove' && e.getClientRects().length);
    if (remove) {
      remove.click();
      return { removed: true };
    }
    await sleep(250);
  }
  return { error: 'cart page did not show items or "Your cart is empty"' };
}

// Runs in the buy tab after adding: clicks "Continue to Checkout" in the cart pop-up or "Proceed to checkout" on the cart page.
async function pageGoToCheckout(timeout) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (/your cart's empty/i.test(document.body.innerText)) return { error: 'Cart is empty - the item was not added' };
    const button = [...document.querySelectorAll('a, button')]
      .find((e) => /continue to checkout|proceed to checkout/i.test(e.textContent) && e.getClientRects().length && !e.disabled);
    if (button) {
      button.click();
      return { ok: true };
    }
    await sleep(250);
  }
  return { error: 'Checkout button not found' };
}

// ---------- run control ----------

async function start() {
  const { products = [], running } = await get(['products', 'running']);
  if (running) return;
  if (!products.length) return log('Add at least one product URL first', 'warn');
  const invalid = products.find((p) => !productHandle(p.url));
  if (invalid) return log(`Not a product URL (needs /products/...): ${invalid.url}`, 'error');

  const settings = await getSettings();
  await updateRun((r) => ({
    done: [], failures: {}, checkout: null, challenge: false, lastTick: 0, workerTabId: r.workerTabId ?? null,
  }));
  await chrome.storage.local.set({ running: true, statuses: {} });
  const mode = settings.testMode ? 'TEST MODE (will not place orders)' : 'LIVE (will place orders)';
  await log(`Started monitoring ${products.length} product(s) - ${mode}`);
  for (const p of products) setStatus(p.id, 'Starting...');
  tick();
}

async function stop(reason) {
  clearTimeout(tickTimer);
  chrome.alarms.clear('tick');
  const { running, products = [] } = await get(['running', 'products']);
  if (!running) return;
  await chrome.storage.local.set({ running: false });
  const run = await getRun();
  for (const p of products) if (!run.done.includes(p.id)) setStatus(p.id, 'Stopped');
  await updateRun((r) => ({ ...r, checkout: null }));
  await log(reason);
}

function scheduleNext(settings) {
  const lo = Math.max(1, Number(settings.intervalMin) || 15);
  const hi = Math.max(lo, Number(settings.intervalMax) || lo);
  const delay = lo + Math.random() * (hi - lo);
  // setTimeout is fine while the worker is awake (<30 s); longer waits use an alarm that survives worker shutdown.
  if (delay < 28) tickTimer = setTimeout(tick, delay * 1000);
  else chrome.alarms.create('tick', { when: Date.now() + delay * 1000 });
}

async function tick() {
  if (ticking) return;
  ticking = true;
  clearTimeout(tickTimer);
  let settings = DEFAULT_SETTINGS;
  try {
    const { running, products = [] } = await get(['running', 'products']);
    if (!running) return;
    settings = await getSettings();
    await updateRun((r) => ({ ...r, lastTick: Date.now() }));
    await checkProducts(products, settings);
  } catch (e) {
    await log(`Error: ${e.message || e}`, 'error');
  } finally {
    ticking = false;
    if ((await get('running')).running) scheduleNext(settings);
  }
}

// ---------- monitoring ----------

async function checkProducts(products, settings) {
  let run = await getRun();
  const active = products.filter((p) => !run.done.includes(p.id));
  if (!active.length) return stop('All products finished');

  if (run.checkout && Date.now() - run.checkout.started > CHECKOUT_TIMEOUT_MS) {
    const p = products.find((x) => x.id === run.checkout.productId);
    if (p) await failed(p, 'Checkout was not finished in time');
    else await updateRun((r) => ({ ...r, checkout: null }));
  }

  const origin = originOf(active[0].url);
  for (const p of active) {
    run = await getRun();
    if (run.checkout?.productId === p.id) continue; // its checkout tab is open
    const result = await inWorker(origin, pageProduct, new URL(p.url).pathname, p.option || '');

    if (result.challenge) return onChallenge(origin, active);
    if (run.challenge) {
      await updateRun((r) => ({ ...r, challenge: false }));
      await log('Cloudflare check passed - monitoring resumed');
    }
    if (result.error) {
      setStatus(p.id, `Error: ${result.error}`, 'bad');
      continue;
    }

    const { variant } = result;
    const label = `${variant.title} $${variant.price.toFixed(2)}`;
    if (!variant.available) {
      setStatus(p.id, `Out of stock (${label})`);
    } else if (p.maxPrice > 0 && variant.price > p.maxPrice) {
      setStatus(p.id, `In stock but $${variant.price.toFixed(2)} is over max $${Number(p.maxPrice).toFixed(2)}`, 'bad');
    } else if (run.checkout) {
      setStatus(p.id, `In stock (${label}) - waiting for the current checkout to finish`, 'good');
    } else {
      await log(`${result.title || shortName(p.url)}: IN STOCK (${label})`);
      await buy(p, { ...variant, optionCount: result.optionCount }, result.title || shortName(p.url), origin, settings);
    }
  }
}

async function onChallenge(origin, active) {
  const run = await getRun();
  if (!run.challenge) {
    await updateRun((r) => ({ ...r, challenge: true }));
    const tab = await workerTab(origin);
    await chrome.tabs.reload(tab.id);
    await focusTab(tab.id);
    await alertUser('Cloudflare check', 'Topps wants a human check - solve it in the pinned Topps tab. Monitoring resumes after.');
  }
  for (const p of active) setStatus(p.id, 'Waiting for you to solve the Cloudflare check in the Topps tab', 'bad');
}

async function buy(p, variant, name, origin, settings) {
  const qty = Number(p.quantity) || 1;
  // Create the tab first so its id is stored before checkout.js asks whether to drive it.
  const tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  await updateRun((r) => ({ ...r, checkout: { productId: p.id, tabId: tab.id, started: Date.now() } }));
  alertUser('In stock!', `${name} - adding ${qty} x ${variant.title} to cart`);
  if (settings.clearCart) await clearCart(origin, tab.id);

  setStatus(p.id, `Adding ${qty} x ${variant.title} to cart...`, 'good');
  await chrome.tabs.update(tab.id, { url: p.url });
  await waitForTabLoad(tab.id);
  const selectors = await getSelectors();
  const giveUp = async (reason) => {
    await failed(p, reason);
    chrome.tabs.remove(tab.id).catch(() => {});
  };
  const added = await runInTab(tab.id, pageAddToCart, variant.optionCount > 1 ? (p.option || variant.title) : '', qty, selectors);
  if (!added.ok) return giveUp(`Add to cart failed: ${added.error}`);

  await log(`${name}: added ${qty} x ${variant.title} to cart - going to checkout`);
  setStatus(p.id, 'In cart - opening checkout...', 'good');
  let toCheckout = await runInTab(tab.id, pageGoToCheckout, 5000);
  if (!toCheckout.ok) {
    await chrome.tabs.update(tab.id, { url: `${origin}/checkout/cart` });
    await waitForTabLoad(tab.id);
    toCheckout = await runInTab(tab.id, pageGoToCheckout, 20000);
  }
  if (!toCheckout.ok) return giveUp(`Could not open checkout: ${toCheckout.error}`);
  // From here checkout.js takes over in the same tab.
}

async function failed(p, reason) {
  let count = 0;
  await updateRun((r) => {
    count = (r.failures?.[p.id] || 0) + 1;
    return { ...r, checkout: null, failures: { ...r.failures, [p.id]: count } };
  });
  await log(`${shortName(p.url)}: ${reason}`, 'warn');
  if (count >= MAX_FAILURES) {
    setStatus(p.id, `Gave up after ${count} failed attempts: ${reason}`, 'bad');
    await alertUser('Gave up', `${shortName(p.url)}: ${count} attempts failed - see the log`);
    await finishProduct(p.id);
  } else {
    setStatus(p.id, `${reason} - retrying (${count}/${MAX_FAILURES})`, 'bad');
  }
}

async function finishProduct(productId) {
  await updateRun((r) => ({ ...r, checkout: r.checkout?.productId === productId ? null : r.checkout,
    done: [...new Set([...(r.done || []), productId])] }));
  const { products = [] } = await get('products');
  const run = await getRun();
  if (products.every((p) => run.done.includes(p.id))) await stop('All products finished');
}

// ---------- checkout tab events (from checkout.js) ----------

async function checkoutProduct(sender) {
  const { running, products = [] } = await get(['running', 'products']);
  const run = await getRun();
  if (!running || !run.checkout || run.checkout.tabId !== sender.tab?.id) return null;
  return products.find((p) => p.id === run.checkout.productId) || null;
}

async function onCheckoutMessage(msg, sender) {
  const p = await checkoutProduct(sender);
  if (!p) return { drive: false };
  const settings = await getSettings();
  const name = shortName(p.url);

  switch (msg.type) {
    case 'checkout:hello':
      return { drive: true, testMode: settings.testMode, selectors: await getSelectors() };
    case 'checkout:status':
      setStatus(p.id, msg.text, 'good');
      return {};
    case 'checkout:needs_user':
      setStatus(p.id, `Waiting for you to finish checkout: ${msg.reason}`, 'bad');
      await focusTab(sender.tab.id);
      await alertUser('Action needed', `${name}: ${msg.reason}. Finish the checkout in the open tab.`);
      return {};
    case 'checkout:test_ok':
      setStatus(p.id, 'TEST OK - checkout reached, Pay button found (not clicked)', 'good');
      await log(`${name}: test run reached the Pay button - no order placed`);
      if (settings.clearCart) await clearCart(originOf(p.url));
      await finishProduct(p.id);
      return {};
    case 'checkout:success': {
      const label = msg.order ? ` #${msg.order}` : '';
      setStatus(p.id, `ORDER PLACED${label}`, 'good');
      await alertUser('Order placed!', `${name} x${p.quantity}${label}`);
      await finishProduct(p.id);
      return {};
    }
    default:
      return {};
  }
}

// ---------- wiring ----------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handlers = {
    start: () => start(),
    stop: () => stop('Stopped'),
    openLogin: async () => {
      const { products = [] } = await get('products');
      const origin = products[0] ? originOf(products[0].url) : DEFAULT_ORIGIN;
      await chrome.tabs.create({ url: `${origin}/customer/account/login`, active: true });
    },
  };
  const work = msg.type?.startsWith('checkout:') ? onCheckoutMessage(msg, sender) : handlers[msg.type]?.();
  if (!work) return false;
  Promise.resolve(work).then((r) => sendResponse(r ?? {}), (e) => {
    log(`Error: ${e.message || e}`, 'error');
    sendResponse({ error: String(e) });
  });
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const run = await getRun();
  if (run.workerTabId === tabId) await updateRun((r) => ({ ...r, workerTabId: null }));
  if (run.checkout?.tabId === tabId) {
    const { products = [] } = await get('products');
    const p = products.find((x) => x.id === run.checkout.productId);
    if (p) await failed(p, 'Checkout tab was closed before the order finished');
  }
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'tick') return tick();
  if (alarm.name === 'watchdog') {
    // Restart the loop if the service worker was shut down while a short setTimeout was pending.
    const { running } = await get('running');
    const run = await getRun();
    const settings = await getSettings();
    if (running && !ticking && Date.now() - run.lastTick > (settings.intervalMax + 45) * 1000) tick();
  }
});

chrome.alarms.get('watchdog').then((a) => a || chrome.alarms.create('watchdog', { periodInMinutes: 1 }));

// Never resume buying by itself after Chrome restarts.
chrome.runtime.onStartup.addListener(() => {
  chrome.storage.local.set({ running: false });
  updateRun((r) => ({ ...r, checkout: null, workerTabId: null }));
});

chrome.action.onClicked.addListener(async () => {
  const url = chrome.runtime.getURL('dashboard.html');
  const [existing] = await chrome.tabs.query({ url });
  if (existing) await focusTab(existing.id);
  else await chrome.tabs.create({ url });
});
