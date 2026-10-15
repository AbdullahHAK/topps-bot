// Shared by the background worker and the dashboard.

export const DEFAULT_ORIGIN = 'https://www.topps.com';

export const DEFAULT_SETTINGS = {
  intervalMin: 15,
  intervalMax: 30,
  testMode: true,
  clearCart: true,
  sound: true,
  selectors: {}, // overrides for DEFAULT_SELECTORS, edited in the dashboard's Advanced section
};

// Page markup the bot relies on. Overridable from the dashboard if Topps/Shopify changes it.
export const DEFAULT_SELECTORS = {
  addToCartButton: '[data-testid="product-add-to-cart"]',
  optionRadio: 'input[name="variant-options"]',
  quantityInput: 'input[name="quantity"]',
  quantityPlus: 'button[aria-label="Increase quantity by one"]',
  payButton: '#checkout-pay-button',
  thankYouUrl: 'thank[-_]you|/orders/',
};

// Cookies that hold the topps.com cart; deleting them gives an empty cart.
export const CART_COOKIES = ['cart_id', 'last_cart_items_ids'];

export const MAX_FAILURES = 5;
export const CHECKOUT_TIMEOUT_MS = 15 * 60 * 1000;

export function productHandle(url) {
  try {
    const match = new URL(url).pathname.match(/\/products\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  } catch {
    return null;
  }
}

export function variantFromUrl(url) {
  try {
    return new URL(url).searchParams.get('variant');
  } catch {
    return null;
  }
}

export function siteOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

export function originOf(url) {
  return new URL(url).origin;
}

export function shortName(url) {
  const handle = productHandle(url);
  return handle ? handle.replace(/-/g, ' ').slice(0, 70) : url;
}

export function newId() {
  return crypto.randomUUID().slice(0, 8);
}
