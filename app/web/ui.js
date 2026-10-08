// Shared UI parts: API helpers, state words, the "needs your input" form, the files list.
window.UI = (() => {
  const esc = s => (s ?? '').toString().replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const post = (u, b) => fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
  const STATE = {
    queued: 'Waiting to start', running: 'Running', paused: 'Paused: you have control', needs_input: 'Needs your input',
    needs_you: 'Blocked: needs you', done: 'Done', failed: 'Failed', stopped: 'Stopped', cancelled: 'Cancelled'
  };
  const live = s => ['queued', 'running', 'paused', 'needs_input', 'needs_you'].includes(s);
  const host = u => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
  const size = n => n < 1024 ? `${n} bytes` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`;

  // one sentence about where a task is, for the row under its title
  function where(t) {
    if (t.status === 'needs_input' && t.input_req) {
      if (t.input_req.kind === 'confirm') return 'Waiting for your OK before a final button.';
      const n = t.input_req.fields.length;
      return t.input_req.takeover ? 'You are filling the page yourself. Tap Done, continue when finished.'
        : `Waiting for ${n} detail${n === 1 ? '' : 's'} from you.`;
    }
    if (t.status === 'needs_you' && t.blocker) return t.blocker.needs;
    if (t.status === 'running' || t.status === 'paused') {
      const s = (t.steps || []).filter(x => x.action !== 'human_takeover').length;
      const h = host(t.url);
      return `Step ${Math.min(s + 1, t.max_steps || 99)} of ${t.max_steps || '?'}${h ? ` on ${h}` : ''}.`;
    }
    if (t.status === 'queued') return 'Starts when the current task ends.';
    return '';
  }

  // ---------- the ask form ----------
  function control(f) {
    const k = esc(f.key), id = `v-${k}`, lab = esc(f.label);
    const opts = f.options || [];
    const sens = f.sensitive && !['select', 'radio', 'checkbox'].includes(f.type);
    if ((f.type === 'radio' || f.type === 'select') && opts.length && opts.length <= 4 && opts.join('').length < 60)
      return `<div class="seg" role="radiogroup" aria-label="${lab}">${opts.map(o => `<label><input type="radio" name="${id}" value="${esc(o)}"> ${esc(o)}</label>`).join('')}</div>`;
    if (opts.length)
      return `<select id="${id}" aria-label="${lab}"><option value="">Choose…</option>${opts.map(o => `<option>${esc(o)}</option>`).join('')}</select>`;
    if (f.type === 'checkbox')
      return `<div class="seg" role="radiogroup" aria-label="${lab}"><label><input type="radio" name="${id}" value="yes"> Yes</label><label><input type="radio" name="${id}" value="no"> No</label></div>`;
    if (f.type === 'date') return `<input id="${id}" type="date" aria-label="${lab}">`;
    if (f.type === 'textarea') return `<textarea id="${id}" aria-label="${lab}"></textarea>`;
    const numeric = ['number', 'otp'].includes(f.type);
    const attrs = [
      `id="${id}"`, `aria-label="${lab}"`, 'autocomplete="off"', 'autocapitalize="off"', 'spellcheck="false"',
      f.type === 'email' ? 'type="email" inputmode="email"' : f.type === 'tel' ? 'type="tel" inputmode="tel"' :
        f.type === 'password' ? 'type="password"' : 'type="text"',
      numeric ? 'inputmode="numeric" pattern="[0-9 ]*"' : '',
      f.type === 'otp' ? 'autocomplete="one-time-code"' : '',
      f.maxlength ? `maxlength="${esc(f.maxlength)}"` : ''
    ];
    const cls = ['value', numeric || f.type === 'tel' || sens ? '' : 'plain', sens && f.type !== 'password' ? 'secret' : ''].join(' ');
    const reveal = sens && f.type !== 'password' ? `<button type="button" class="quiet small" data-reveal="${id}" aria-pressed="false">Show</button>` : '';
    return `<div class="row"><input ${attrs.join(' ')} class="${cls} grow">${reveal}</div>`;
  }

  function askHTML(t) {
    const r = t.input_req;
    if (!r) return '';
    if (r.kind === 'confirm') {
      return `<section class="ask-sheet arrive" data-req="${r.id}" data-task="${t.id}" aria-live="polite">
        <h3>${esc(r.reason)}</h3>
        <p class="why">The agent filled a form and stopped before the final button. Check the values, then decide.</p>
        ${(r.summary || []).length ? `<ul class="summary">${r.summary.map(s => `<li><span>${esc(s.label)}</span><span>${esc(s.value)}</span></li>`).join('')}</ul>` : ''}
        <div class="ask-actions"><button class="primary" data-act="approve">Yes, press it</button>
        <button data-act="reject">No, not yet</button><button class="quiet" data-act="takeover">I'll do it myself</button></div></section>`;
    }
    if (r.takeover) {
      return `<section class="ask-sheet" data-req="${r.id}" data-task="${t.id}" aria-live="polite">
        <h3>You are filling it yourself</h3>
        <p class="why">Open the live screen, tap a field and type. When you are done, tap Done, continue and the agent reads what you filled.</p>
        <div class="ask-actions"><a class="btn ask" href="/live/${t.id}">Open live screen</a><button class="primary" data-act="resume">Done, continue</button></div></section>`;
    }
    const cap = r.fields.some(f => f.type === 'captcha');
    return `<form class="ask-sheet arrive" data-req="${r.id}" data-task="${t.id}" novalidate aria-live="polite">
      <h3>${esc(r.reason)}</h3>
      <p class="why">${esc(r.title ? `On ${r.title}. ` : '')}Values stay in this task only and are typed into the site's own fields.</p>
      ${cap ? `<img class="captcha-shot" alt="The page as it looks now, with the code to read" src="/api/shot/${t.id}.png?${Date.now()}">` : ''}
      ${r.fields.map(f => `<div class="ask-row" data-key="${esc(f.key)}">
        <div class="head"><b id="l-${esc(f.key)}">${esc(f.label)}${f.required ? ' <span class="muted">(required)</span>' : ''}</b>
        <button type="button" class="skip" aria-pressed="false">Skip</button></div>
        <div class="ctl">${control(f)}</div>${f.why ? `<p class="why">${esc(f.why)}</p>` : ''}</div>`).join('')}
      <div class="ask-actions"><button class="ask" type="submit" data-act="submit">Send details</button>
      <button type="button" data-act="takeover">I'll fill it myself</button></div>
      <p class="why" data-msg></p></form>`;
  }

  function collect(form) {
    const values = {}, skipped = [];
    form.querySelectorAll('.ask-row').forEach(row => {
      const k = row.dataset.key;
      if (row.classList.contains('skipped')) { skipped.push(k); return; }
      const checked = row.querySelector('input[type=radio]:checked');
      const el = row.querySelector('input:not([type=radio]),select,textarea');
      const v = checked ? checked.value : el ? el.value.trim() : '';
      if (v) values[k] = v; else skipped.push(k);
    });
    return {values, skipped};
  }

  // wire every ask sheet inside root (idempotent)
  function wireAsk(root, onDone) {
    root.querySelectorAll('.ask-sheet:not([data-wired])').forEach(el => {
      el.dataset.wired = '1';
      const task = el.dataset.task, req = el.dataset.req;
      el.addEventListener('click', async e => {
        const sk = e.target.closest('.skip');
        if (sk) { const row = sk.closest('.ask-row'); const on = !row.classList.contains('skipped');
          row.classList.toggle('skipped', on); sk.setAttribute('aria-pressed', on); sk.textContent = on ? 'Skipped' : 'Skip'; return; }
        const rv = e.target.closest('[data-reveal]');
        if (rv) { const i = document.getElementById(rv.dataset.reveal); const on = i.classList.toggle('secret');
          rv.textContent = on ? 'Show' : 'Hide'; rv.setAttribute('aria-pressed', !on); return; }
        const b = e.target.closest('[data-act]');
        if (!b || b.type === 'submit') return;
        e.preventDefault();
        const act = b.dataset.act;
        if (act === 'resume') { await post(`/api/tasks/${task}/resume`); onDone && onDone(act); return; }
        b.disabled = true;
        const label = b.textContent; b.textContent = 'Sending…';
        let r, j = {};
        try { r = await post(`/api/tasks/${task}/input`, {action: act, req}); j = await r.json().catch(() => ({})); } catch (err) { r = {ok: false}; }
        if (!r.ok) {
          b.disabled = false; b.textContent = label;
          let m = el.querySelector('[data-msg]');
          if (!m) { m = document.createElement('p'); m.className = 'why'; m.dataset.msg = ''; el.appendChild(m); }
          m.textContent = j.why || 'That did not reach the agent. Tap again.';
          return;
        }
        if (act === 'approve' || act === 'reject') el.innerHTML = `<h3>${act === 'approve' ? 'Pressing it now.' : 'Okay, it will not press it.'}</h3>`;
        if (act === 'takeover' && !location.pathname.startsWith('/live')) location.href = `/live/${task}`;
        onDone && onDone(act);
      });
      if (el.tagName === 'FORM') el.addEventListener('submit', async e => {
        e.preventDefault();
        const {values, skipped} = collect(el);
        const msg = el.querySelector('[data-msg]');
        const req = el.querySelectorAll('.ask-row').length;
        if (!Object.keys(values).length && skipped.length < req) { msg.textContent = 'Fill in at least one value, or skip them.'; return; }
        const btn = el.querySelector('[type=submit]'); btn.disabled = true; btn.textContent = 'Sending…';
        const r = await (await post(`/api/tasks/${task}/input`, {action: 'submit', req: el.dataset.req, values, skipped})).json().catch(() => ({}));
        if (r.ok) { el.innerHTML = '<h3>Sent. The agent is filling the form.</h3>'; onDone && onDone('submit'); }
        else { btn.disabled = false; btn.textContent = 'Send details'; msg.textContent = r.why || 'That did not go through. Send again.'; }
      });
    });
  }

  // keep an ask sheet the person is typing into when a list re-renders
  function keepAsks(container, render) {
    const kept = {};
    container.querySelectorAll('.ask-sheet[data-req]').forEach(el => kept[el.dataset.req + (el.tagName)] = el);
    render();
    container.querySelectorAll('.ask-sheet[data-req]').forEach(el => {
      const old = kept[el.dataset.req + el.tagName];
      if (old && old !== el) { old.classList.remove('arrive'); el.replaceWith(old); }
    });
  }

  // ---------- files ----------
  function ftype(name, mime) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    const k = mime === 'application/pdf' || ext === 'pdf' ? 'pdf' : ext.slice(0, 4) || 'file';
    return `<span class="ftype ${k === 'pdf' ? 'pdf' : ''}" aria-hidden="true">${esc(k.toUpperCase())}</span>`;
  }
  function filesHTML(task, files) {
    if (!files || !files.length) return '';
    const canShare = !!(navigator.canShare);
    return `<ul class="files">${files.map(f => {
      const url = `/api/tasks/${task}/files/${encodeURIComponent(f.name)}`;
      return `<li>${ftype(f.name, f.mime)}<div class="meta"><b>${esc(f.name)}</b><span class="muted small">${size(f.size)}${f.encrypted ? '. Password-protected PDF' : ''}</span></div>
      <div class="acts">${canShare ? `<button class="small primary" data-share="${esc(url)}" data-name="${esc(f.name)}" data-mime="${esc(f.mime || '')}">Share</button>` : ''}
      <a class="btn small ${canShare ? '' : 'primary'}" href="${url}" download="${esc(f.name)}">Download</a>
      <button class="small quiet" data-del="${esc(url)}" aria-label="Delete ${esc(f.name)}">Delete</button></div></li>`;
    }).join('')}</ul>`;
  }
  function wireFiles(root, onChange) {
    if (root.dataset.filesWired) return;
    root.dataset.filesWired = '1';
    root.addEventListener('click', async e => {
      const s = e.target.closest('[data-share]');
      if (s) {
        e.preventDefault();
        try {
          const blob = await (await fetch(s.dataset.share)).blob();
          const file = new File([blob], s.dataset.name, {type: s.dataset.mime || blob.type});
          if (navigator.canShare && navigator.canShare({files: [file]})) { await navigator.share({files: [file], title: s.dataset.name}); return; }
        } catch (err) { if (err && err.name === 'AbortError') return; }
        const a = document.createElement('a'); a.href = s.dataset.share; a.download = s.dataset.name; a.click();
        return;
      }
      const d = e.target.closest('[data-del]');
      if (d && confirm('Delete this file? It cannot be brought back.')) {
        await post(d.dataset.del + '/delete'); onChange && onChange();
      }
    });
  }

  return {esc, post, STATE, live, host, size, where, askHTML, wireAsk, keepAsks, filesHTML, wireFiles};
})();
