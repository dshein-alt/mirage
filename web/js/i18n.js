let locale = 'en';
let messages = {};
let fallback = {};

function selectedLocale() {
  const manual = localStorage.getItem('mirage.locale') || 'auto';
  if (manual !== 'auto') return manual;
  for (const language of navigator.languages || [navigator.language || 'en']) {
    const base = language.toLowerCase().split('-')[0];
    if (['en', 'ru'].includes(base)) return base;
  }
  return 'en';
}

export function t(key, variables = {}) {
  let text = messages[key] ?? fallback[key] ?? key;
  for (const [name, value] of Object.entries(variables)) text = text.replaceAll(`{${name}}`, String(value));
  return text;
}

export function applyTranslations(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(element => { element.textContent = t(element.dataset.i18n); });
  root.querySelectorAll('[data-i18n-placeholder]').forEach(element => { element.placeholder = t(element.dataset.i18nPlaceholder); });
  root.querySelectorAll('[data-i18n-title]').forEach(element => { element.title = t(element.dataset.i18nTitle); });
  root.querySelectorAll('[data-i18n-aria]').forEach(element => { element.setAttribute('aria-label', t(element.dataset.i18nAria)); });
}

export async function initI18n() {
  fallback = await fetch('/locales/en.json').then(response => response.json());
  await setLocale(localStorage.getItem('mirage.locale') || 'auto', false);
}

export async function setLocale(preference, notify = true) {
  localStorage.setItem('mirage.locale', preference);
  locale = selectedLocale();
  messages = locale === 'en' ? fallback : await fetch(`/locales/${locale}.json`).then(response => response.json());
  document.documentElement.lang = locale;
  applyTranslations();
  document.querySelectorAll('#language-setting, #language-quick').forEach(control => { control.value = preference; });
  if (notify) document.dispatchEvent(new CustomEvent('mirage:locale'));
}

export function currentLocale() { return locale; }
export function formatDate(value) { return new Intl.DateTimeFormat(locale, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value)); }
export function formatBytes(value) { return new Intl.NumberFormat(locale, {maximumFractionDigits: 1, style: 'unit', unit: 'gigabyte'}).format(value / 1e9); }
