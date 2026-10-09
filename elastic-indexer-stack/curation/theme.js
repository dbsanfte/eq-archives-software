'use strict';
// Runs before the stylesheet so saved preferences apply on the first paint.
(() => {
  const key = 'curation-theme';
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = null;
  const valid = value => ['light', 'dark'].includes(value) ? value : null;
  try { preference = valid(localStorage.getItem(key)); } catch (_) {}

  function apply() {
    const dark = (preference || (system.matches ? 'dark' : 'light')) === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]').content = dark ? '#142b23' : '#173f32';
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
      toggle.setAttribute('aria-pressed', String(dark));
      toggle.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
      document.getElementById('theme-label').textContent = dark ? 'Light' : 'Dark';
    }
  }
  apply();
  system.addEventListener('change', apply);
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) {
      preference = valid(event.newValue);
      apply();
    }
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-toggle').addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, preference); } catch (_) {}
      apply();
    });
  });
})();
