/**
 * Utility functions for the Alexandria frontend
 * Ported from app/static/index.html lines 1128-1181, 1981-1987
 */

// Extend Window interface for Bootstrap global
declare global {
  interface Window {
    bootstrap: {
      Toast: new (el: HTMLElement, options?: Record<string, unknown>) => {
        show(): void;
        hide(): void;
      } & { getInstance(el: HTMLElement): { hide(): void } | null };
      Modal: new (el: HTMLElement) => {
        show(): void;
        hide(): void;
      };
    };
  }
}

/**
 * Show a Bootstrap toast notification
 * @param message - Message to display
 * @param type - Toast type: 'success' | 'error' | 'warning' | 'info'
 * @param duration - Duration in milliseconds (default 4000)
 */
export function showToast(message: string, type: 'success' | 'error' | 'warning' | 'info' = 'info', duration: number = 4000): void {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const bgClass = type === 'success' ? 'bg-success' :
                 type === 'error' ? 'bg-danger' :
                 type === 'warning' ? 'bg-warning text-dark' : 'bg-info';
  const id = 'toast-' + Date.now();
  const html = `
    <div id="${id}" class="toast align-items-center text-white ${bgClass} border-0" role="alert">
      <div class="d-flex">
        <div class="toast-body">${escapeHtml(message)}</div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>
    </div>`;
  container.insertAdjacentHTML('beforeend', html);
  const el = document.getElementById(id);
  if (!el) return;

  // Bootstrap Toast requires bootstrap global - will be available from CDN
  const bootstrap = window.bootstrap;
  if (bootstrap) {
    const toast = new bootstrap.Toast(el, { delay: duration });
    toast.show();
    el.addEventListener('hidden.bs.toast', () => el.remove());
  } else {
    // Fallback: auto-remove after duration
    setTimeout(() => el.remove(), duration);
  }
}

/**
 * Show a confirmation modal and return a promise
 * @param message - Message to display in the modal
 * @returns Promise that resolves to true if OK clicked, false otherwise
 */
export function showConfirm(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    const body = document.getElementById('confirmModalBody');
    if (!body) {
      resolve(false);
      return;
    }
    
    body.textContent = message;
    const bootstrap = window.bootstrap;
    if (!bootstrap) {
      resolve(false);
      return;
    }

    const modalEl = document.getElementById('confirmModal');
    if (!modalEl) {
      resolve(false);
      return;
    }

    const modal = new bootstrap.Modal(modalEl);
    const okBtn = document.getElementById('confirmModalOk');
    const cancelBtn = document.getElementById('confirmModalCancel');

    function cleanup() {
      okBtn?.removeEventListener('click', onOk);
      cancelBtn?.removeEventListener('click', onCancel);
      modalEl?.removeEventListener('hidden.bs.modal', onHidden);
    }

    let resolved = false;
    function onOk() { resolved = true; cleanup(); modal.hide(); resolve(true); }
    function onCancel() { resolved = true; cleanup(); modal.hide(); resolve(false); }
    function onHidden() { if (!resolved) { cleanup(); resolve(false); } }

    okBtn?.addEventListener('click', onOk);
    cancelBtn?.addEventListener('click', onCancel);
    modalEl?.addEventListener('hidden.bs.modal', onHidden);
    modal.show();
  });
}

/**
 * Escape HTML special characters to prevent XSS
 * @param str - String to escape
 * @returns Escaped string safe for HTML insertion
 */
export function escapeHtml(str: unknown): string {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Check if any audio element is currently playing
 * @returns true if any <audio> element is playing
 */
export function isAudioPlaying(): boolean {
  const audios = document.querySelectorAll('audio');
  for (const audio of audios) {
    if (!audio.paused && !audio.ended) return true;
  }
  return false;
}
