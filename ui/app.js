'use strict';
/* DiskWorks front end - shell: API helper, tabs, status/helper pill, version pill and
   release notes, About, Log tab. The Disks / Image / Access tabs live in their own files.
   No frameworks, no CDN. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  status: null, settings: {}, fsinfo: null,
  inv: null, invSeq: 0, invHash: null,
  sel: null,            // {kind: 'disk'|'part'|'gap', id}
  pending: [],          // queued operations (disks.js)
  preview: null,        // planned inventory preview from /api/ops/plan
  logSeq: 0, exact: false,
  windowed: true,
};

/* ------------------------------------------------------------------------ */
/* Formatting                                                                */
/* ------------------------------------------------------------------------ */
function fmtBytes(x, exact) {
  if (x == null || isNaN(x)) return '—';
  x = Number(x);
  if (exact || S.exact) return x.toLocaleString() + ' B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let i = 0; let v = x;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const n = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  return `${n} ${units[i]}`;
}
function fmtDec(x) {  // decimal GB, what the box says
  if (x == null || isNaN(x)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = Number(x);
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`;
}
const fmtPct = p => (p == null || isNaN(p)) ? '—' : `${Math.round(p)} %`;
const fmtWhen = ts => new Date(ts * 1000).toLocaleTimeString([], { timeStyle: 'medium' });
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const MiB = 1024 * 1024, GiB = 1024 * 1024 * 1024;

/* ------------------------------------------------------------------------ */
/* API                                                                       */
/* ------------------------------------------------------------------------ */
async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    });
  } catch (e) {
    return { error: 'DiskWorks is not responding. If you closed it, reopen the app.', offline: true };
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok && !data.error) data.error = `Server error (${res.status})`;
  return data;
}

/* ------------------------------------------------------------------------ */
/* Toast + clipboard + dialogs                                               */
/* ------------------------------------------------------------------------ */
let toastTimer;
function toast(msg, ms = 2400) {
  const t = $('#toast'); t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), ms);
}
async function copyText(text, what = 'Copied') {
  try { await navigator.clipboard.writeText(text); toast(what); }
  catch (e) {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast(what); } catch (e2) { toast('Could not copy'); }
    ta.remove();
  }
}
function openDlg(id) { const d = $(id); if (!d.open) d.showModal(); return d; }
function closeDlg(id) { const d = $(id); if (d.open) d.close(); }
$$('dialog [data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
function showText(title, text) {
  $('#txTitle').textContent = title; $('#txBody').textContent = text;
  $('#txCopy').onclick = () => copyText(text, 'Commands copied');
  openDlg('#dlgText');
}

/* Type-to-confirm dialog: resolves true when the user typed the expected word. */
function confirmTyped(title, html, expected, okLabel = 'Continue') {
  return new Promise(resolve => {
    $('#cfTitle').textContent = title;
    $('#cfBody').innerHTML = `${html}<div class="field" style="margin-top:12px"><label>Type <b>${esc(expected)}</b> to continue</label><input type="text" id="cfInput" autocomplete="off" spellcheck="false"></div>`;
    const ok = $('#cfOk'); ok.textContent = okLabel; ok.disabled = true;
    const inp = $('#cfInput');
    inp.oninput = () => { ok.disabled = inp.value.trim().toLowerCase() !== String(expected).trim().toLowerCase(); };
    const dlg = openDlg('#dlgConfirm');
    const done = v => { dlg.removeEventListener('close', onClose); closeDlg('#dlgConfirm'); resolve(v); };
    const onClose = () => done(false);
    dlg.addEventListener('close', onClose);
    ok.onclick = () => done(true);
    setTimeout(() => inp.focus(), 50);
  });
}

/* ------------------------------------------------------------------------ */
/* Tabs                                                                      */
/* ------------------------------------------------------------------------ */
function showTab(name) {
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $$('main > .tab').forEach(s => s.classList.toggle('hidden', s.id !== 'tab-' + name));
  S.settings.lastTab = name;
  api('/api/settings', { patch: { lastTab: name } });
  document.dispatchEvent(new CustomEvent('tab', { detail: name }));
}
$('#tabs').addEventListener('click', e => { const b = e.target.closest('button'); if (b) showTab(b.dataset.tab); });

/* ------------------------------------------------------------------------ */
/* Status, helper pill, version pill                                        */
/* ------------------------------------------------------------------------ */
const LOCK_SVG = {
  closed: '<svg class="lockico" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="3" y="7" width="10" height="7.5" rx="1.6" fill="currentColor"/><path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  open: '<svg class="lockico" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="3" y="7" width="10" height="7.5" rx="1.6" fill="currentColor"/><path d="M5 7V5a3 3 0 0 1 6 0" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
  wait: '<svg class="lockico" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><rect x="3" y="7" width="10" height="7.5" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="2 1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="2 1.5"/></svg>',
};
const lockIcon = (kind = 'closed') => LOCK_SVG[kind] || LOCK_SVG.closed;
function paintHelper(h) {
  const pill = $('#helperPill');
  const st = h?.state || 'absent';
  pill.className = 'pill withico ' + ({ ready: 'pill-good', starting: 'pill-warn', failed: 'pill-bad' }[st] || 'pill-muted');
  const icon = { ready: 'open', starting: 'wait' }[st] || 'closed';
  const text = { ready: 'Unlocked', starting: 'Asking for permission…', failed: 'Unlock failed · retry', absent: 'Locked · Unlock' }[st] || st;
  pill.innerHTML = lockIcon(icon) + `<span>${esc(text)}</span>`;
  pill.title = h?.message || ({ ready: 'Changes to disks are allowed for this session; the window itself still runs without administrator rights.',
    absent: 'Viewing never needs administrator rights. Click to allow changes; you will be asked once.' }[st] || '');
  pill.disabled = st === 'starting';
}
async function refreshStatus() {
  const s = await api('/api/status');
  if (s.error) { paintHelper({ state: 'absent', message: s.error }); return; }
  S.status = s; S.windowed = !!s.windowed;
  paintHelper(s.helper);
  $('#verPill').textContent = 'v' + s.version;
  $('#aboutVer').textContent = s.version;
  if (s.ui) onTrayRequest(s.ui);
  if (s.fixture) $('#invInfo').textContent = 'Showing a saved inventory (fixture) — not this computer.';
  document.dispatchEvent(new CustomEvent('status', { detail: s }));
}
$('#helperPill').addEventListener('click', async () => {
  const st = S.status?.helper?.state;
  if (st === 'ready') { toast('Already unlocked for this session'); return; }
  paintHelper({ state: 'starting' });
  const r = await api('/api/helper/start', {});
  if (r.error) { toast(r.error, 5000); }
  await refreshStatus();
});

/* Release notes: the section of CHANGELOG.md for the running version, others collapsed. */
function parseChangelog(md) {
  const sections = []; let s = null;
  for (const line of String(md || '').split(/\r?\n/)) {
    const m = line.match(/^## (\S+)(?:\s+-\s+(.*))?$/);
    if (m) { s = { ver: m[1], date: m[2] || '', lines: [] }; sections.push(s); continue; }
    if (s) s.lines.push(line);
  }
  return sections;
}
const mdInline = t => esc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
function renderNotesSection(sec) {
  let html = ''; let inList = false;
  for (const l of sec.lines) {
    const li = l.match(/^\s*-\s+(.*)$/);
    if (li) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${mdInline(li[1])}</li>`; continue; }
    if (inList && /^\s{2,}\S/.test(l)) { html = html.replace(/<\/li>$/, ' ' + mdInline(l.trim()) + '</li>'); continue; }
    if (inList) { html += '</ul>'; inList = false; }
    if (l.trim()) html += `<p>${mdInline(l.trim())}</p>`;
  }
  if (inList) html += '</ul>';
  return html;
}
async function showNotes() {
  const r = await fetch('/api/changelog'); const md = await r.text();
  const cur = S.status?.version || '';
  const sections = parseChangelog(md);
  const render = renderNotesSection;
  let out = '';
  const first = sections.find(x => x.ver === cur) || sections[0];
  if (first) out += `<h4>Version ${esc(first.ver)}${first.date ? ' <span class="soft">· ' + esc(first.date) + '</span>' : ''}</h4>${render(first)}`;
  const rest = sections.filter(x => x !== first);
  if (rest.length) out += `<details><summary>Earlier versions (${rest.length})</summary>${rest.map(x => `<h4>Version ${esc(x.ver)} <span class="soft">· ${esc(x.date)}</span></h4>${render(x)}`).join('')}</details>`;
  $('#notesBody').innerHTML = `<div class="notes">${out || '<p class="hint">No release notes found.</p>'}</div>`;
  openDlg('#dlgNotes');
}
$('#verPill').addEventListener('click', () => { showNotes(); updCheckNow(); });

/* ------------------------------------------------------------------------ */
/* Updates and tray, to LinkTest's standard: launch check with Update now /  */
/* Skip for now, a check every 30 min, a pulsing Update now button next to   */
/* the version, the result at the top of What's new, and the Settings dialog */
/* (cog) with Updates and Window.                                            */
/* ------------------------------------------------------------------------ */
const UPD = { st: null, busy: false };
function updAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}
function updAvailable(st) { return !!(st && st.canInstall && st.phase === 'available'); }
function updText(st) {
  if (!st) return '';
  if (UPD.busy || st.phase === 'checking') return 'Checking GitHub…';
  if (st.phase === 'error') return `DiskWorks ${st.current}. Could not check for updates: ${st.error}`;
  if (['downloading', 'installing', 'restarting'].includes(st.phase)) return `Updating to DiskWorks ${st.latest}…`;
  if (st.phase === 'available') return st.canInstall
    ? `DiskWorks ${st.latest} is available (you have ${st.current}). Updating downloads it from GitHub, checks it, replaces DiskWorks and opens it again.`
    : `DiskWorks ${st.latest} is available (you have ${st.current}). ${st.why}`;
  if (!st.checked) return `DiskWorks ${st.current}. Not checked yet.`;
  return `DiskWorks ${st.current} is up to date (checked ${updAgo(st.checked)}).`;
}
function updNotesHtml(st) {
  if (!st || !st.notes) return '';
  return parseChangelog(st.notes).map(x => `<h4>Version ${esc(x.ver)}${x.date ? ' <span class="soft">· ' + esc(x.date) + '</span>' : ''}</h4>${renderNotesSection(x)}`).join('');
}
function updRender() {
  const st = UPD.st; if (!st) return;
  updRenderTop();
  const text = updText(st), avail = updAvailable(st);
  // Settings > Updates
  $('#setUpdState').textContent = text;
  $('#setUpdNotes').innerHTML = avail ? updNotesHtml(st) : '';
  $('#setUpdNotes').classList.toggle('hidden', !avail || !st.notes);
  $('#setUpdCheck').disabled = UPD.busy;
  $('#setUpdGo').classList.toggle('hidden', !avail);
  $('#setUpdAuto').checked = !!st.updateCheck;
  // Settings > Window
  $('#setWinBox').classList.toggle('hidden', !st.windowed);
  $('#setTrayMin').checked = !!st.trayMinimize;
  $('#setTrayClose').checked = !!st.trayClose;
  $('#setTrayMin').disabled = $('#setTrayClose').disabled = !st.tray;
  $('#setTrayNote').textContent = st.tray
    ? 'Clicking the tray icon opens DiskWorks again' + (st.trayMenu ? '; right-click it for Update and Quit.' : '. Update and Quit are here in Settings.')
    : (st.platform && st.platform.startsWith('mac') ? 'On a Mac, DiskWorks stays in the Dock when its window is minimized or closed.'
      : `No system tray was found on this desktop, so minimizing and closing work as usual${st.trayWhy ? ` (${st.trayWhy})` : ''}.`);
  // What's new: the result at the top
  $('#notesUpdState').textContent = text;
  $('#notesUpdCheck').disabled = UPD.busy;
  $('#notesUpdGo').classList.toggle('hidden', !avail);
}
// The pulsing "Update now" next to the version, whenever an update is waiting.
function updRenderTop() {
  const st = UPD.st, btn = $('#updNowBtn');
  if (btn.dataset.busy === '1') return;
  const avail = updAvailable(st);
  btn.hidden = !avail;
  btn.title = avail ? `DiskWorks ${st.latest} is available (you have ${st.current}). Click to update now; DiskWorks restarts with the new version.` : '';
}
async function updLoad() {
  const st = await api('/api/update');
  if (!st.error) { UPD.st = st; updRender(); if (st.prompt) updPrompt(); }
  return st;
}
async function updCheckNow() {
  if (UPD.busy) return;
  UPD.busy = true; updRender();
  const r = await api('/api/update/check', {});
  UPD.busy = false;
  if (!r.error && !r.offline) UPD.st = r;
  updRender();
}
async function updStartNow() {
  const btn = $('#updNowBtn');
  btn.dataset.busy = '1'; btn.classList.add('busy'); btn.textContent = 'Updating…'; btn.hidden = false;
  for (const d of ['#dlgUpdPrompt', '#dlgSettings', '#dlgNotes']) closeDlg(d);
  const r = await api('/api/update/install', {});
  if (r.error) {
    toast(r.error, 6000);
    btn.dataset.busy = ''; btn.classList.remove('busy'); btn.textContent = 'Update now'; updRender();
    return r;
  }
  openDlg('#dlgUpdate'); renderUpdate(r);
  return r;
}
function updPrompt() {
  const dlg = $('#dlgUpdPrompt'), st = UPD.st;
  if (dlg.open || $('#dlgSettings').open || $('#dlgUpdate').open) return;
  api('/api/update/prompted', {});   // one prompt per launch, even with several windows
  $('#updDlgTitle').textContent = `DiskWorks ${st.latest} is available`;
  $('#updDlgText').textContent = `You have ${st.current}.${st.size ? ` The download is ${fmtDec(st.size)}.` : ''}${st.notes ? ' What changed:' : ''}`;
  $('#updDlgNotes').innerHTML = updNotesHtml(st);
  $('#updDlgNotes').classList.toggle('hidden', !st.notes);
  openDlg('#dlgUpdPrompt');
}
// Right after launch the check takes a few seconds: look again until it has run.
async function updWaitLaunchCheck() {
  for (let i = 0; i < 40; i++) {
    const st = await updLoad();
    if (st.error || !st.updateCheck || st.checked) return;
    await new Promise(r => setTimeout(r, 3000));
  }
}
function onTrayRequest(req) {
  if (req === 'progress') { openDlg('#dlgUpdate'); api('/api/update').then(renderUpdate); }
  else if (req === 'check') { openDlg('#dlgSettings'); updCheckNow(); }
}

/* The progress dialog after Update now (download, check, install, restart). */
const UP = { timer: null };
const PHASE_TEXT = { downloading: 'Downloading…', installing: 'Installing…', restarting: 'Restarting DiskWorks with the new version…' };
function renderUpdate(st) {
  clearTimeout(UP.timer);
  const body = $('#upBody');
  if (!st || st.error && st.phase !== 'error') { body.innerHTML = `<div class="error">${esc(st?.error || 'No update is running.')}</div>`; return; }
  if (st.phase === 'error') { body.innerHTML = `<div class="error">${esc(st.error || 'The update failed.')}</div><p class="hint">You are still on DiskWorks ${esc(st.current)}; nothing was changed.</p>`; resetUpdBtn(); return; }
  if (!['downloading', 'installing', 'restarting'].includes(st.phase)) { closeDlg('#dlgUpdate'); resetUpdBtn(); return; }
  const pct = st.size ? Math.min(100, 100 * (st.done || 0) / st.size) : 0;
  body.innerHTML = `<p><b>Updating to DiskWorks ${esc(st.latest)}</b> <span class="soft">(from ${esc(st.current)})</span></p>` +
    `<div class="progress"><div class="bar" style="width:${st.phase === 'downloading' ? pct.toFixed(1) : 100}%"></div><span class="msg">${esc(PHASE_TEXT[st.phase])}${st.phase === 'downloading' && st.size ? ` ${fmtDec(st.done || 0)} of ${fmtDec(st.size)}` : ''}</span></div>` +
    '<p class="hint">DiskWorks closes and opens again by itself when the new version is in place.</p>';
  if (st.phase === 'restarting') { waitForRestart(st.latest); return; }
  UP.timer = setTimeout(async () => renderUpdate(await api('/api/update')), 500);
}
function resetUpdBtn() { const b = $('#updNowBtn'); b.dataset.busy = ''; b.classList.remove('busy'); b.textContent = 'Update now'; updRender(); }
/* In the native window this page closes with the old process and the new one opens its own
   window; in a browser tab (server mode) the page reloads once the new version answers. */
async function waitForRestart(latest) {
  for (let i = 0; i < 120; i++) {
    await new Promise(r => setTimeout(r, 1000));
    const p = await api('/api/ping');
    if (p.version && p.version === latest) { location.reload(); return; }
  }
}
async function afterUpdateNotice() {
  const from = S.settings.updatedFrom; if (!from) return;
  api('/api/settings', { patch: { updatedFrom: null } });
  delete S.settings.updatedFrom;
  if (from !== S.status?.version) { toast(`Updated from ${from} to ${S.status?.version}`, 5000); showNotes(); }
}
function wireUpdates() {
  const save = async patch => { await api('/api/settings', { patch }); Object.assign(S.settings, patch); await updLoad(); };
  $('#setUpdCheck').onclick = updCheckNow;
  $('#notesUpdCheck').onclick = updCheckNow;
  $('#setUpdGo').onclick = updStartNow;
  $('#notesUpdGo').onclick = updStartNow;
  $('#updNowBtn').onclick = updStartNow;
  $('#setUpdAuto').onchange = e => save({ updateCheck: e.target.checked });
  $('#setTrayMin').onchange = e => save({ trayMinimize: e.target.checked });
  $('#setTrayClose').onchange = e => save({ trayClose: e.target.checked });
  $('#setQuit').onclick = async () => { await api('/api/quit', {}); };
  $('#updSkip').onclick = () => closeDlg('#dlgUpdPrompt');
  $('#updNow').onclick = () => { closeDlg('#dlgUpdPrompt'); updStartNow(); };
  $('#btnSettings').addEventListener('click', () => { updLoad(); openDlg('#dlgSettings'); });
  updWaitLaunchCheck();
  setInterval(updLoad, 60 * 1000);   // cheap: reads the result of the app's own background check
}
$('#btnAbout').addEventListener('click', () => openDlg('#dlgAbout'));

/* ------------------------------------------------------------------------ */
/* Log tab                                                                   */
/* ------------------------------------------------------------------------ */
async function pollLog() {
  const r = await api(`/api/log?since=${S.logSeq}`);
  if (r.error || !r.events) return;
  const box = $('#logList');
  for (const e of r.events) {
    S.logSeq = e.seq;
    const div = document.createElement('div'); div.className = 'ent';
    const ts = `<span class="ts">${fmtWhen(e.ts)}</span>`;
    if (e.type === 'cmd') {
      const ok = e.code === 0 || e.code === null;
      div.innerHTML = `${ts}<span class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'}</span> ${e.title ? '<b>' + esc(e.title) + '</b> · ' : ''}<span class="cmd">${esc(e.cmd)}</span> <span class="soft">exit ${e.code ?? '—'} · ${e.ms} ms</span>` +
        (e.output ? `<details><summary>output</summary><pre>${esc(e.output)}</pre></details>` : '');
    } else {
      div.innerHTML = `${ts}${esc(e.msg || '')}`;
    }
    box.prepend(div);
  }
}
$('#btnLogExport').addEventListener('click', async () => {
  const r = await api('/api/log/export', {});
  if (r.error) toast(r.error, 4000); else if (r.ok) toast('Log saved to ' + r.path, 4000);
});

/* ------------------------------------------------------------------------ */
/* Boot                                                                      */
/* ------------------------------------------------------------------------ */
async function boot() {
  S.settings = await api('/api/settings') || {};
  S.exact = !!S.settings.exactSizes;
  $('#chkExact').checked = S.exact;
  S.fsinfo = await api('/api/fs');
  await refreshStatus();
  showTab(S.settings.lastTab || 'disks');
  document.dispatchEvent(new CustomEvent('booted'));
  wireUpdates();
  afterUpdateNotice();
  pollLog();
  setInterval(refreshStatus, 6000);
  setInterval(pollLog, 1500);
  setInterval(() => api('/api/heartbeat', {}), 2000);
}
$('#chkExact').addEventListener('change', e => {
  S.exact = e.target.checked; api('/api/settings', { patch: { exactSizes: S.exact } });
  document.dispatchEvent(new CustomEvent('redraw'));
});
boot();
