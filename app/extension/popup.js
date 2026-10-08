const $ = s => document.querySelector(s);
const msg = m => chrome.runtime.sendMessage(m);
async function refresh() {
  const c = await chrome.storage.local.get(['url', 'key']); $('#url').value = c.url || ''; $('#key').value = c.key || '';
  const s = await msg({type: 'status'});
  $('#st').textContent = (s.connected ? 'Connected to runner' : 'Not connected') + (s.teaching ? ' · recording' : '');
  $('#teach').textContent = s.teaching ? 'Stop teaching & save flow' : 'Start teaching';
}
$('#save').onclick = async () => { await msg({type: 'cfg', url: $('#url').value.trim().replace(/\/$/, ''), key: $('#key').value.trim()}); setTimeout(refresh, 800); };
$('#resume').onclick = async () => { await msg({type: 'resume'}); $('#st').textContent = 'Resume sent'; };
$('#teach').onclick = async () => { const s = await msg({type: 'status'});
  await msg(s.teaching ? {type: 'teach_stop'} : {type: 'teach_start', name: 'taught ' + new Date().toLocaleString()}); refresh(); };
refresh();
