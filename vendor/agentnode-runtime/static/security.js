/* Trust boundaries shared by streaming text, replay, and Surface annotations. */
(() => {
  'use strict';
  const safeImage = value => typeof value === 'string' && value.length <= 2800000 && /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$/.test(value);
  function markdown(text) {
    text = String(text || '');
    if (!window.marked || !window.DOMPurify) {
      const el = document.createElement('span'); el.textContent = text;
      return el.innerHTML.replace(/\n/g, '<br>');
    }
    const fragment = DOMPurify.sanitize(marked.parse(text, {breaks: true}), {
      RETURN_DOM_FRAGMENT: true, USE_PROFILES: {html: true},
      FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'video', 'audio'],
      FORBID_ATTR: ['style', 'id', 'name'], ALLOW_DATA_ATTR: false,
    });
    for (const el of fragment.querySelectorAll('[href], [src]')) {
      const attr = el.hasAttribute('href') ? 'href' : 'src';
      const value = el.getAttribute(attr);
      let allowed = false;
      try {
        const u = new URL(value, location.href);
        allowed = ['https:', 'http:'].includes(u.protocol) || (attr === 'href' && u.protocol === 'mailto:') || (attr === 'src' && safeImage(value));
      } catch {}
      if (!allowed) el.removeAttribute(attr);
      if (el.tagName === 'A') { el.rel = 'noopener noreferrer'; el.target = '_blank'; }
      if (el.tagName === 'IMG') { el.loading = 'lazy'; el.referrerPolicy = 'no-referrer'; }
    }
    const box = document.createElement('div'); box.appendChild(fragment); return box.innerHTML;
  }
  function annotation(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    if (value.imageDataUrl && !safeImage(value.imageDataUrl)) return null;
    if (!Array.isArray(value.marks) || value.marks.length > 100) return null;
    const marks = [];
    for (const m of value.marks) {
      if (!m || !['x', 'y', 'w', 'h'].every(k => Number.isFinite(m[k]) && m[k] >= 0 && m[k] <= 1)) return null;
      if (m.x + m.w > 1.001 || m.y + m.h > 1.001) return null;
      marks.push({x:m.x, y:m.y, w:m.w, h:m.h});
    }
    return {componentId: String(value.componentId || '').slice(0, 128), title: String(value.title || '').slice(0, 200),
      componentType: String(value.componentType || 'panel').slice(0, 40), marks, imageDataUrl: value.imageDataUrl || ''};
  }
  window.ConductorSecurity = Object.freeze({markdown, annotation, safeImage});
})();
