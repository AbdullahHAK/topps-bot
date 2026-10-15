import { DEFAULT_SELECTORS, DEFAULT_SETTINGS, newId, productHandle, shortName, siteOf } from './common.js';

const $ = (id) => document.getElementById(id);
let products = [];
let settings = { ...DEFAULT_SETTINGS };
let statuses = {};
let running = false;

const time = (t) => new Date(t).toLocaleTimeString([], { hour12: false });
const saveProducts = () => chrome.storage.local.set({ products });
const saveSettings = () => chrome.storage.local.set({ settings });

// ---------- products ----------

function numberInput(value, attrs, onChange) {
  const input = Object.assign(document.createElement('input'), { type: 'number', ...attrs });
  input.value = value;
  input.addEventListener('change', () => onChange(input));
  return input;
}

function renderProducts() {
  const body = document.querySelector('#products tbody');
  body.replaceChildren();
  for (const p of products) {
    const row = document.createElement('tr');

    const name = document.createElement('td');
    const link = Object.assign(document.createElement('a'), { href: p.url, target: '_blank', textContent: shortName(p.url), title: p.url });
    name.append(link);

    const option = document.createElement('td');
    const optionInput = Object.assign(document.createElement('input'), {
      type: 'text', value: p.option || '', placeholder: 'Any in stock', className: 'option-input',
    });
    optionInput.addEventListener('change', () => {
      p.option = optionInput.value.trim();
      saveProducts();
    });
    option.append(optionInput);

    const qty = document.createElement('td');
    qty.append(numberInput(p.quantity, { min: 1, max: 99 }, (input) => {
      p.quantity = Math.min(99, Math.max(1, parseInt(input.value, 10) || 1));
      input.value = p.quantity;
      saveProducts();
    }));

    const max = document.createElement('td');
    max.append(numberInput(p.maxPrice || '', { min: 0, step: 0.01, placeholder: 'No limit' }, (input) => {
      p.maxPrice = Math.max(0, parseFloat(input.value) || 0);
      input.value = p.maxPrice || '';
      saveProducts();
    }));

    const status = Object.assign(document.createElement('td'), { id: `status-${p.id}` });
    const remove = document.createElement('td');
    const button = Object.assign(document.createElement('button'), { className: 'remove', textContent: 'Remove' });
    button.addEventListener('click', () => {
      products = products.filter((x) => x.id !== p.id);
      saveProducts();
      renderProducts();
    });
    remove.append(button);

    row.append(name, option, qty, max, status, remove);
    body.append(row);
  }
  $('empty').hidden = products.length > 0;
  renderStatuses();
}

function renderStatuses() {
  for (const p of products) {
    const cell = $(`status-${p.id}`);
    const s = statuses[p.id];
    cell.textContent = s ? `${time(s.t)}  ${s.text}` : 'Idle';
    cell.className = `status ${s?.kind || ''}`;
  }
}

$('add-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const url = $('url').value.trim();
  const error = (msg) => Object.assign($('form-error'), { textContent: msg, hidden: !msg });
  if (!productHandle(url)) return error('That is not a product URL - it should contain /products/...');
  if (products.some((p) => p.url === url)) return error('That product is already in the list.');
  if (products.length && siteOf(products[0].url) !== siteOf(url)) return error('All products must be on the same site.');
  error('');
  products.push({
    id: newId(),
    url,
    option: $('option').value.trim(),
    quantity: Math.min(99, Math.max(1, parseInt($('qty').value, 10) || 1)),
    maxPrice: Math.max(0, parseFloat($('max').value) || 0),
  });
  saveProducts();
  renderProducts();
  $('url').value = '';
  $('option').value = '';
});

// ---------- settings ----------

function renderSettings() {
  $('intervalMin').value = settings.intervalMin;
  $('intervalMax').value = settings.intervalMax;
  for (const key of ['testMode', 'clearCart', 'sound']) $(key).checked = settings[key];
  $('selectors').value = Object.keys(settings.selectors || {}).length ? JSON.stringify(settings.selectors, null, 2) : '';
  $('selectors').placeholder = JSON.stringify(DEFAULT_SELECTORS, null, 2);
}

for (const id of ['intervalMin', 'intervalMax']) {
  $(id).addEventListener('change', () => {
    const lo = Math.min(600, Math.max(5, parseInt($('intervalMin').value, 10) || 15));
    const hi = Math.min(600, Math.max(lo, parseInt($('intervalMax').value, 10) || lo));
    Object.assign(settings, { intervalMin: lo, intervalMax: hi });
    renderSettings();
    saveSettings();
  });
}
for (const key of ['testMode', 'clearCart', 'sound']) {
  $(key).addEventListener('change', () => {
    settings[key] = $(key).checked;
    saveSettings();
    renderRunning();
  });
}

$('saveSelectors').addEventListener('click', () => {
  const text = $('selectors').value.trim();
  try {
    const parsed = text ? JSON.parse(text) : {};
    if (typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('must be a JSON object');
    settings.selectors = parsed;
    saveSettings();
    $('selectorsMsg').textContent = 'Saved';
  } catch (e) {
    $('selectorsMsg').textContent = `Invalid JSON: ${e.message}`;
  }
});

// ---------- run controls ----------

function renderRunning() {
  $('start').disabled = running;
  $('stop').disabled = !running;
  const state = $('state');
  if (!running) Object.assign(state, { textContent: 'STOPPED', className: 'state' });
  else if (settings.testMode) Object.assign(state, { textContent: 'RUNNING - TEST MODE', className: 'state test' });
  else Object.assign(state, { textContent: 'RUNNING - LIVE', className: 'state live' });
}

$('start').addEventListener('click', () => {
  if (!products.length) return alert('Add at least one product URL first.');
  if (!settings.testMode && !confirm('Test mode is OFF - the bot will place real orders.\n\nContinue?')) return;
  chrome.runtime.sendMessage({ type: 'start' });
});
$('stop').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'stop' }));
$('login').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'openLogin' }));

// ---------- log ----------

function renderLog(entries) {
  const box = $('log');
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren(...entries.map((e) => {
    const line = document.createElement('div');
    line.className = e.level;
    line.textContent = `[${time(e.t)}] ${e.level === 'error' ? 'ERROR: ' : e.level === 'warn' ? '! ' : ''}${e.msg}`;
    return line;
  }));
  if (atBottom) box.scrollTop = box.scrollHeight;
}
$('clearLog').addEventListener('click', () => chrome.storage.local.set({ log: [] }));

// ---------- live updates from the background worker ----------

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  if (changes.statuses) {
    statuses = changes.statuses.newValue || {};
    renderStatuses();
  }
  if (changes.log) renderLog(changes.log.newValue || []);
  if (changes.running) {
    running = !!changes.running.newValue;
    renderRunning();
  }
});

const data = await chrome.storage.local.get(['products', 'settings', 'statuses', 'log', 'running']);
products = data.products || [];
settings = { ...DEFAULT_SETTINGS, ...data.settings };
statuses = data.statuses || {};
running = !!data.running;
renderSettings();
renderProducts();
renderLog(data.log || []);
renderRunning();
