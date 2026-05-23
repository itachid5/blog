function isMobileViewport() {
  return window.innerWidth <= 768;
}

function getSidebarElements() {
  return {
    sidebar: document.getElementById('sidebar'),
    overlay: document.getElementById('overlay'),
    openButton: document.getElementById('openMenuBtn'),
  };
}

function setSidebarOpen(isOpen) {
  const { sidebar, overlay, openButton } = getSidebarElements();
  if (!sidebar || !overlay) return;
  sidebar.classList.toggle('open', isOpen);
  overlay.classList.toggle('show', isOpen);
  document.body.classList.toggle('sidebar-open', isOpen);
  sidebar.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
  if (openButton) openButton.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
}

function closeMobileSidebar() {
  setSidebarOpen(false);
}

function toggleSidebar() {
  const { sidebar } = getSidebarElements();
  if (!sidebar) return;
  setSidebarOpen(!sidebar.classList.contains('open'));
}

function syncThemeIcon(theme) {
  const button = document.getElementById('themeToggleBtn');
  if (!button) return;
  button.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
}

function toggleTheme() {
  const html = document.documentElement;
  const nextTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', nextTheme);
  localStorage.setItem('nekotunnel-theme', nextTheme);
  syncThemeIcon(nextTheme);
}

function copyWithTextareaFallback(text) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '-1000px';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

function copyLiteralText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).catch(() => copyWithTextareaFallback(text));
  }
  copyWithTextareaFallback(text);
  return Promise.resolve();
}

function copyTextFromElement(elementId) {
  const element = document.getElementById(elementId);
  if (element) return copyLiteralText(element.innerText.trim());
  return Promise.resolve();
}

function bindCopyButtons() {
  document.querySelectorAll('[data-copy-target]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      const originalText = button.textContent;
      copyLiteralText(target.innerText.trim()).then(() => {
        button.textContent = 'Copied';
        window.setTimeout(() => {
          button.textContent = originalText;
        }, 1500);
      });
    });
  });
}

function createLucideIcons() {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
}

window.toggleSidebar = toggleSidebar;
window.toggleTheme = toggleTheme;
window.copyLiteralText = copyLiteralText;
window.copyTextFromElement = copyTextFromElement;

document.addEventListener('DOMContentLoaded', () => {
  const theme = localStorage.getItem('nekotunnel-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  syncThemeIcon(theme);
  createLucideIcons();

  const overlay = document.getElementById('overlay');
  if (overlay) overlay.addEventListener('click', closeMobileSidebar);

  const closeButton = document.getElementById('closeMenuBtn');
  if (closeButton) closeButton.addEventListener('click', closeMobileSidebar);

  document.querySelectorAll('.sidebar a').forEach((link) => {
    link.addEventListener('click', closeMobileSidebar);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMobileSidebar();
  });

  bindCopyButtons();
});

window.addEventListener('resize', () => {
  if (!isMobileViewport()) closeMobileSidebar();
});
