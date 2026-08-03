/**
 * Dark mode toggle logic for Alexandria.
 * Theme is driven by Bootstrap 5.3's `data-bs-theme` attribute on <html>.
 * The initial value is applied by an inline script in index.html (before first
 * paint) to avoid FOUC; this module wires up the toggle button and keeps the
 * persisted preference in localStorage in sync.
 */

const STORAGE_KEY = 'alexandria-theme';

/** Return the current theme: 'dark' | 'light' */
export function getTheme(): 'dark' | 'light' {
  return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
}

/** Apply a theme and persist the preference. */
export function setTheme(theme: 'dark' | 'light'): void {
  if (theme === 'dark') {
    document.documentElement.setAttribute('data-bs-theme', 'dark');
  } else {
    document.documentElement.removeAttribute('data-bs-theme');
  }
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch (e) {
    /* localStorage unavailable; theme still applies for this session */
  }
  updateIcon();
}

/** Update the toggle icon to reflect the current theme. */
function updateIcon(): void {
  const icon = document.getElementById('theme-toggle-icon');
  if (!icon) return;
  const dark = getTheme() === 'dark';
  // Sun icon -> click to switch to dark; Moon icon -> click to switch to light.
  icon.className = dark ? 'fas fa-moon' : 'fas fa-sun';
}

/** Initialize the toggle button behavior. Call once on DOMContentLoaded. */
export function initTheme(): void {
  updateIcon();
  const toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      setTheme(getTheme() === 'dark' ? 'light' : 'dark');
    });
  }
}
