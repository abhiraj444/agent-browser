// Agent Browser MV3 service worker.
// Real-browser mode: the runner asks for snapshots/actions over a websocket; we answer through chrome.debugger using
// the same passive CDP domains as the cloud runner (Accessibility, DOMSnapshot, Page) -- never Runtime.
// Teach mode: teach.js reports what the user clicks/types; we forward it as flow steps.
// Handoff: the runner asks for help -> system notification + badge; the user clears it in this Chrome and taps Resume.
let ws = null, cfg = {}, attached = null, teaching = false;

async function loadCfg() { cfg = (await chrome.storage.local.get(['url', 'key'])) || {}; }
function wsUrl() { const u = new URL(cfg.url); u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:'; u.pathname = '/ext'; u.search = '?k=' + encodeURIComponent(cfg.key); return u.toString(); }

async function connect() {
  await loadCfg();
  if (!cfg.url || !cfg.key || (ws && ws.readyState <= 1)) return;
  ws = new WebSocket(wsUrl());
  ws.onopen = () => { send({type: 'hello', ua: navigator.userAgent, version: chrome.runtime.getManifest().version}); setBadge(''); };
  ws.onclose = () => { ws = null; setBadge('off'); };
  ws.onmessage = async ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'saved') { notify('Flow saved', m.file); return; }
    if (m.cmd) {
      try { send({reply: m.id, data: await handle(m)}); }
      catch (e) { send({reply: m.id, error: String(e && e.message || e)}); }
    }
  };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function setBadge(t) { chrome.action.setBadgeText({text: t}); chrome.action.setBadgeBackgroundColor({color: t === '!' ? '#ffb020' : '#666'}); }
function notify(title, message) { chrome.notifications.create({type: 'basic', iconUrl: 'icon.png', title, message, priority: 2}); }

async function activeTab() { const [t] = await chrome.tabs.query({active: true, lastFocusedWindow: true}); return t; }
async function dbg(tabId) {
  if (attached !== tabId) {
    if (attached) { try { await chrome.debugger.detach({tabId: attached}); } catch (e) {} }
    await chrome.debugger.attach({tabId}, '1.3'); attached = tabId;
  }
  return (method, params = {}) => chrome.debugger.sendCommand({tabId}, method, params);
}
chrome.debugger.onDetach.addListener(() => { attached = null; });

async function handle(m) {
  const tab = await activeTab();
  const cdp = await dbg(tab.id);
  switch (m.cmd) {
    case 'snapshot': {
      const ax = (await cdp('Accessibility.getFullAXTree')).nodes;
      const snap = await cdp('DOMSnapshot.captureSnapshot', {computedStyles: [], includeDOMRects: true});
      const lm = await cdp('Page.getLayoutMetrics');
      const cw = lm.cssContentSize.width;
      return {url: tab.url, title: tab.title, ax, snap, scale: cw ? (snap.documents[0].contentWidth || cw) / cw : 1};
    }
    case 'navigate': await chrome.tabs.update(tab.id, {url: m.url}); return {ok: true};
    case 'quad': { const q = (await cdp('DOM.getContentQuads', {backendNodeId: m.backendNodeId})).quads[0];
      if (!q) return null; const xs = [q[0], q[2], q[4], q[6]], ys = [q[1], q[3], q[5], q[7]];
      return [Math.min(...xs), Math.min(...ys), Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)]; }
    case 'click': {  // in the user's own Chrome; the desktop Actor (OS input) replaces this when installed
      for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
        await cdp('Input.dispatchMouseEvent', {type, x: m.x, y: m.y, button: 'left', clickCount: 1});
      return {ok: true}; }
    case 'type': await cdp('Input.insertText', {text: m.text}); return {ok: true};
    case 'handoff': setBadge('!'); notify('Agent needs you: ' + (m.label || 'check'), m.needs || 'Clear it in this tab, then press Resume in the extension.'); return {ok: true};
    case 'teach': teaching = !!m.on; return {ok: true};
    default: throw new Error('unknown cmd ' + m.cmd);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === 'cfg') { await chrome.storage.local.set({url: msg.url, key: msg.key}); if (ws) ws.close(); ws = null; await connect(); reply({ok: true}); }
    else if (msg.type === 'status') reply({connected: !!(ws && ws.readyState === 1), teaching});
    else if (msg.type === 'teach_start') { teaching = true; send({type: 'teach_start', name: msg.name}); reply({ok: true}); }
    else if (msg.type === 'teach_stop') { teaching = false; send({type: 'teach_stop', check: msg.check}); reply({ok: true}); }
    else if (msg.type === 'teach_step') { if (teaching) send({type: 'teach_step', step: msg.step}); reply({ok: teaching}); }
    else if (msg.type === 'resume') { setBadge(''); send({type: 'resume'}); reply({ok: true}); }
    else if (msg.type === 'is_teaching') reply({teaching});
  })();
  return true;
});
chrome.alarms.create('keepalive', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
