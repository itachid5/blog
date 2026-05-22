function isMobileViewport() {
  return window.innerWidth <= 768;
}

function closeMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (!sidebar || !overlay) return;
  sidebar.classList.remove('open');
  overlay.classList.remove('show');
  document.body.classList.remove('sidebar-open');
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const mainContent = document.getElementById('mainContent');
  const overlay = document.getElementById('overlay');
  if (!sidebar || !mainContent || !overlay) return;

  if (isMobileViewport()) {
    sidebar.classList.toggle('open');
    overlay.classList.toggle('show', sidebar.classList.contains('open'));
    document.body.classList.toggle('sidebar-open', sidebar.classList.contains('open'));
    return;
  }

  sidebar.classList.toggle('closed');
  mainContent.classList.toggle('expanded');
  localStorage.setItem('nekotunnel-sidebar-collapsed', sidebar.classList.contains('closed') ? '1' : '0');
}

function setThemeIcon(theme) {
  const icon = document.querySelector('#themeToggleBtn i');
  if (!icon) return;
  icon.classList.toggle('ph-moon', theme === 'dark');
  icon.classList.toggle('ph-sun', theme !== 'dark');
}

function toggleTheme() {
  const html = document.documentElement;
  const nextTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', nextTheme);
  localStorage.setItem('nekotunnel-theme', nextTheme);
  setThemeIcon(nextTheme);
}

function copyLiteralText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text);
  }
}

function copyTextFromElement(elementId) {
  const element = document.getElementById(elementId);
  if (element) copyLiteralText(element.textContent.trim());
}

window.toggleSidebar = toggleSidebar;
window.toggleTheme = toggleTheme;
window.copyLiteralText = copyLiteralText;
window.copyTextFromElement = copyTextFromElement;

document.addEventListener('DOMContentLoaded', () => {
  const theme = localStorage.getItem('nekotunnel-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', theme);
  setThemeIcon(theme);

  const sidebar = document.getElementById('sidebar');
  const mainContent = document.getElementById('mainContent');
  if (sidebar && mainContent && !isMobileViewport() && localStorage.getItem('nekotunnel-sidebar-collapsed') === '1') {
    sidebar.classList.add('closed');
    mainContent.classList.add('expanded');
  }

  const overlay = document.getElementById('overlay');
  if (overlay) overlay.addEventListener('click', closeMobileSidebar);

  document.querySelectorAll('.sidebar a').forEach((link) => {
    link.addEventListener('click', () => {
      if (isMobileViewport()) closeMobileSidebar();
    });
  });
});

window.addEventListener('resize', () => {
  if (!isMobileViewport()) closeMobileSidebar();
});
