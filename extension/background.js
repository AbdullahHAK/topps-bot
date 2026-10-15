// Bot engine (service worker).
//
// Stock checks and cart calls run *inside a pinned topps.com tab* (chrome.scripting), so they are
// ordinary same-origin requests with the user's own cookies and Cloudflare clearance.
// Checkout runs in a separate tab, driven by checkout.js.
//
// storage.local keys: products, settings, running, statuses, log, run (state of the current run).

import {
  CHECKOUT_TIMEOUT_MS, DEFAULT_ORIGIN, DEFAULT_SELECTORS, DEFAULT_SETTINGS, MAX_FAILURES,
  originOf, productHandle, shortName, siteOf, variantFromUrl,
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

// ---------- worker tab (runs the stock / cart requests) ----------

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
    const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
    return result?.result ?? { error: 'No result from the Topps tab' };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

// --- These run inside the topps.com page; they must be self-contained. ---

async function pageProduct(handle, variantId) {
  try {
    const r = await fetch(`/products/${encodeURIComponent(handle)}.js`, {
      cache: 'no-store', headers: { Accept: 'application/json' },
    });
    const type = r.headers.get('content-type') || '';
    if (r.status === 404) return { error: 'Product not found (404)' };
    if (!type.includes('json')) {
      const blocked = [403, 429, 503].includes(r.status) || /just a moment/i.test(document.title);
      return blocked ? { challenge: true } : { error: `Unexpected response (HTTP ${r.status})` };
    }
    if (!r.ok) return { error: `HTTP ${r.status}` };
    const p = await r.json();
    const variants = p.variants || [];
    const v = variants.find((x) => String(x.id) === String(variantId))
      || variants.find((x) => x.available) || variants[0];
    if (!v) return { error: 'Product has no variants' };
    return { title: p.title, variant: { id: v.id, title: v.title, available: !!v.available, price: v.price } };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

async function pageCartAdd(variantId, quantity) {
  try {
    const r = await fetch('/cart/add.js', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ items: [{ id: variantId, quantity }] }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { error: body.description || body.message || `HTTP ${r.status}` };
    return { ok: true };
  } catch (e) {
    return { error: String(e.message || e) };
  }
}

async function pageCartClear() {
  try {
    const r = await fetch('/cart/clear.js', { method: 'POST', headers: { Accept: 'application/json' } });
    return r.ok ? { ok: true } : { error: `HTTP ${r.status}` };
  } catch (e) {
    return { error: String(e.message || e) };
  }
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
    const result = await inWorker(origin, pageProduct, productHandle(p.url), variantFromUrl(p.url));

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
    const price = variant.price / 100;
    const priceText = ` ($${price.toFixed(2)})`;
    if (!variant.available) {
      setStatus(p.id, `Out of stock${priceText}`);
    } else if (p.maxPrice > 0 && price > p.maxPrice) {
      setStatus(p.id, `In stock but $${price.toFixed(2)} is over max $${Number(p.maxPrice).toFixed(2)}`, 'bad');
    } else if (run.checkout) {
      setStatus(p.id, `In stock${priceText} - waiting for the current checkout to finish`, 'good');
    } else {
      await log(`${result.title || shortName(p.url)}: IN STOCK${priceText}`);
      await buy(p, variant, result.title || shortName(p.url), origin, settings);
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
  await updateRun((r) => ({ ...r, checkout: { productId: p.id, tabId: null, started: Date.now() } }));
  alertUser('In stock!', `${name} - adding ${p.quantity} to cart`);
  setStatus(p.id, `Adding ${p.quantity} to cart...`, 'good');

  if (settings.clearCart) await inWorker(origin, pageCartClear);
  const added = await inWorker(origin, pageCartAdd, variant.id, Number(p.quantity) || 1);
  if (!added.ok) return failed(p, `Add to cart failed: ${added.error}`);

  await log(`${name}: added ${p.quantity} to cart - opening checkout`);
  setStatus(p.id, 'In cart - checking out...', 'good');
  // Create the tab first so its id is stored before checkout.js asks whether to drive it.
  const tab = await chrome.tabs.create({ url: 'about:blank', active: true });
  await updateRun((r) => ({ ...r, checkout: { ...r.checkout, tabId: tab.id } }));
  await chrome.tabs.update(tab.id, { url: `${origin}/checkout` });
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
      return { drive: true, testMode: settings.testMode, selectors: { ...DEFAULT_SELECTORS, ...settings.selectors } };
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
      if (settings.clearCart) await inWorker(originOf(p.url), pageCartClear);
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
      await chrome.tabs.create({ url: `${origin}/account/login`, active: true });
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
