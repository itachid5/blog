const html = document.documentElement;
const body = document.body;
const drawer = document.querySelector('#drawer');
const drawerOverlay = document.querySelector('.drawer-overlay');
const menuButtons = document.querySelectorAll('[data-open-menu]');
const searchOverlay = document.querySelector('#search-overlay');
const searchInput = document.querySelector('#search-input');
const searchForm = document.querySelector('#search-form');
const searchTags = document.querySelector('#search-tags');
const searchButtons = document.querySelectorAll('[data-open-search]');
const closeSearchButtons = document.querySelectorAll('[data-close-search]');
const themeButtons = document.querySelectorAll('[data-theme-toggle]');
const themeIcons = document.querySelectorAll('[data-theme-icon]');

let isMenuOpen = false;
let isSearchOpen = false;

const lockBody = () => { body.style.overflow = 'hidden'; };
const unlockBody = () => { if (!isMenuOpen && !isSearchOpen) body.style.overflow = ''; };

const setDrawerOpen = (open) => {
  isMenuOpen = open;
  drawer?.classList.toggle('open', open);
  drawer?.setAttribute('aria-hidden', String(!open));
  drawerOverlay?.toggleAttribute('hidden', !open);
  body.classList.toggle('drawer-open', open);
  menuButtons.forEach((button) => button.setAttribute('aria-expanded', String(open)));
  if (open) lockBody(); else unlockBody();
};

const setSearchOpen = (open) => {
  isSearchOpen = open;
  searchOverlay?.classList.toggle('open', open);
  searchOverlay?.setAttribute('aria-hidden', String(!open));
  searchButtons.forEach((button) => button.setAttribute('aria-expanded', String(open)));
  if (open) {
    lockBody();
    setTimeout(() => {
      searchInput?.focus();
      searchTags?.classList.add('ready');
    }, 90);
  } else {
    searchTags?.classList.remove('ready');
    if (searchInput) searchInput.value = '';
    unlockBody();
  }
};

const setTheme = (theme) => {
  html.dataset.theme = theme;
  localStorage.setItem('theme', theme);
  const isDark = theme === 'dark';
  themeButtons.forEach((button) => {
    button.setAttribute('aria-pressed', String(isDark));
    button.setAttribute('aria-label', isDark ? 'Switch to light mode' : 'Switch to dark mode');
  });
  themeIcons.forEach((icon) => { icon.textContent = isDark ? 'light_mode' : 'dark_mode'; });
};

setTheme(html.dataset.theme || 'light');

menuButtons.forEach((button) => button.addEventListener('click', () => setDrawerOpen(true)));
document.querySelectorAll('[data-close-menu]').forEach((button) => button.addEventListener('click', () => setDrawerOpen(false)));
searchButtons.forEach((button) => button.addEventListener('click', () => setSearchOpen(true)));
closeSearchButtons.forEach((button) => button.addEventListener('click', () => setSearchOpen(false)));

document.querySelectorAll('[data-drawer-toggle]').forEach((button) => {
  button.addEventListener('click', () => {
    const section = button.closest('[data-drawer-section]');
    const submenu = section?.querySelector('.drawer-submenu');
    const open = !section?.classList.contains('open');
    section?.classList.toggle('open', open);
    submenu?.toggleAttribute('hidden', !open);
    button.setAttribute('aria-expanded', String(open));
  });
});

themeButtons.forEach((button) => {
  button.addEventListener('click', () => {
    setTheme(html.dataset.theme === 'dark' ? 'light' : 'dark');
  });
});

searchForm?.addEventListener('submit', (event) => {
  const query = searchInput?.value.trim() || '';
  if (!query) {
    event.preventDefault();
    searchInput?.focus();
  }
});

searchInput?.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  const query = searchInput.value.trim();
  if (query) window.location.href = `/search?q=${encodeURIComponent(query)}`;
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (isSearchOpen) setSearchOpen(false);
  if (isMenuOpen) setDrawerOpen(false);
});
