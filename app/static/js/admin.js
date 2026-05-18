document.querySelectorAll('[data-confirm]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (!confirm('Are you sure you want to delete this item?')) event.preventDefault();
  });
});

const source = document.querySelector('[data-slug-source]');
const target = document.querySelector('[data-slug-target]');
source?.addEventListener('input', () => {
  if (!target || target.dataset.touched) return;
  target.value = source.value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
});
target?.addEventListener('input', () => { target.dataset.touched = 'true'; });

const adminSidebar = document.querySelector('#admin-sidebar');
const adminOverlay = document.querySelector('.admin-overlay');
const adminMenuButton = document.querySelector('[data-admin-menu]');

const setAdminMenuOpen = (open) => {
  adminSidebar?.classList.toggle('open', open);
  adminSidebar?.setAttribute('aria-hidden', String(!open && window.matchMedia('(max-width: 900px)').matches));
  adminOverlay?.toggleAttribute('hidden', !open);
  adminMenuButton?.setAttribute('aria-expanded', String(open));
  document.body.classList.toggle('admin-menu-open', open);
};

adminMenuButton?.addEventListener('click', () => setAdminMenuOpen(true));
document.querySelectorAll('[data-admin-close]').forEach((element) => element.addEventListener('click', () => setAdminMenuOpen(false)));

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setAdminMenuOpen(false);
});

document.querySelector('[data-admin-theme-toggle]')?.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('admin-theme', next);
});

const quillEditors = new Map();
document.querySelectorAll('[data-rich-form]').forEach((form) => {
  const textarea = form.querySelector('[data-rich-textarea]');
  const editorElement = form.querySelector('[data-rich-editor]');
  if (textarea && editorElement && window.Quill) {
    const quill = new Quill(editorElement, {
      theme: 'snow',
      modules: {
        toolbar: [
          [{ header: [1, 2, 3, false] }],
          ['bold', 'italic', 'underline', 'strike'],
          [{ list: 'ordered' }, { list: 'bullet' }],
          ['blockquote', 'code-block'],
          ['link', 'image', 'video'],
          ['clean'],
        ],
      },
    });
    quill.root.innerHTML = textarea.value;
    textarea.hidden = true;
    quillEditors.set(textarea.id || textarea.name, quill);
    form.addEventListener('submit', () => {
      textarea.value = quill.root.innerHTML;
    });
  }
});

document.querySelectorAll('[data-media-url]').forEach((button) => {
  button.addEventListener('click', () => {
    const input = document.querySelector('[data-featured-image-url]');
    const preview = document.querySelector('[data-featured-preview]');
    if (!input) return;
    input.value = button.dataset.mediaUrl || '';
    document.querySelectorAll('[data-media-url]').forEach((item) => item.classList.remove('selected'));
    button.classList.add('selected');
    if (preview && input.value) preview.src = input.value;
  });
});

document.querySelectorAll('[data-copy-value]').forEach((button) => {
  button.addEventListener('click', async () => {
    const value = button.dataset.copyValue || '';
    if (!value) return;
    await navigator.clipboard?.writeText(value);
    const original = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = original; }, 1400);
  });
});

document.querySelectorAll('[data-edit-page]').forEach((button) => {
  button.addEventListener('click', () => {
    const id = document.querySelector('#page_id');
    const title = document.querySelector('#page_title');
    const slug = document.querySelector('#page_slug');
    const status = document.querySelector('#page_status');
    const content = document.querySelector('#page_content');
    if (id) id.value = button.dataset.pageId || '';
    if (title) title.value = button.dataset.pageTitle || '';
    if (slug) {
      slug.value = button.dataset.pageSlug || '';
      slug.dataset.touched = 'true';
    }
    if (status) status.value = button.dataset.pageStatus || 'draft';
    if (content) {
      content.value = button.dataset.pageContent || '';
      quillEditors.get(content.id || content.name)?.root && (quillEditors.get(content.id || content.name).root.innerHTML = content.value);
    }
    document.querySelector('[data-page-form]')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

document.querySelectorAll('.form-grid[enctype="multipart/form-data"]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (!button) return;
    button.dataset.originalText = button.textContent;
    button.textContent = 'Uploading...';
    button.setAttribute('aria-busy', 'true');
  });
});
