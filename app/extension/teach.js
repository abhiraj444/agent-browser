// Teach mode recorder. Only reads what the user clicks/types (role + accessible name), only while teaching is on.
(() => {
  const ROLE = {A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button'};
  function role(el) {
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (el.tagName === 'INPUT') { const t = (el.type || 'text').toLowerCase();
      return {checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', search: 'searchbox', range: 'slider', number: 'spinbutton'}[t] || 'textbox'; }
    return ROLE[el.tagName] || null;
  }
  function name(el) {
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' ').trim();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.textContent.trim(); }
    const lab = el.closest('label'); if (lab) return lab.textContent.trim();
    return (el.innerText || el.value || el.title || el.placeholder || el.alt || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  }
  const target = e => { let el = e.target; while (el && el !== document.body) { if (role(el)) return el; el = el.parentElement; } return null; };
  const step = s => chrome.runtime.sendMessage({type: 'teach_step', step: Object.assign({url: location.href}, s)});
  document.addEventListener('click', e => { const el = target(e); if (!el || ['textbox', 'searchbox'].includes(role(el))) return;
    step({kind: 'click', target: {id: null, role: role(el), name: name(el), region: null}}); }, true);
  document.addEventListener('change', e => { const el = target(e); if (!el) return; const r = role(el);
    if (['textbox', 'searchbox', 'spinbutton'].includes(r))
      step({kind: 'type', target: {id: null, role: r, name: name(el), region: null}, text: el.type === 'password' ? '' : el.value, submit: false, secret: el.type === 'password'}); }, true);
  document.addEventListener('keydown', e => { if (e.key !== 'Enter') return; const el = target(e); if (!el) return;
    step({kind: 'type', target: {id: null, role: role(el), name: name(el), region: null}, text: el.type === 'password' ? '' : el.value, submit: true, secret: el.type === 'password'}); }, true);
})();
