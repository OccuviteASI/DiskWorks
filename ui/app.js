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
async function showNotes() {
  const r = await fetch('/api/changelog'); const md = await r.text();
  const cur = S.status?.version || '';
  const sections = []; let s = null;
  for (const line of md.split(/\r?\n/)) {
    const m = line.match(/^## (\S+)(?:\s+-\s+(.*))?$/);
    if (m) { s = { ver: m[1], date: m[2] || '', lines: [] }; sections.push(s); continue; }
    if (s) s.lines.push(line);
  }
  const render = sec => {
    let html = ''; let inList = false;
    for (const l of sec.lines) {
      const li = l.match(/^\s*-\s+(.*)$/);
      if (li) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
      if (inList && /^\s{2,}\S/.test(l)) { html = html.replace(/<\/li>$/, ' ' + inline(l.trim()) + '</li>'); continue; }
      if (inList) { html += '</ul>'; inList = false; }
      if (l.trim()) html += `<p>${inline(l.trim())}</p>`;
    }
    if (inList) html += '</ul>';
    return html;
  };
  const inline = t => esc(t).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  let out = '';
  const first = sections.find(x => x.ver === cur) || sections[0];
  if (first) out += `<h4>Version ${esc(first.ver)}${first.date ? ' <span class="soft">· ' + esc(first.date) + '</span>' : ''}</h4>${render(first)}`;
  const rest = sections.filter(x => x !== first);
  if (rest.length) out += `<details><summary>Earlier versions (${rest.length})</summary>${rest.map(x => `<h4>Version ${esc(x.ver)} <span class="soft">· ${esc(x.date)}</span></h4>${render(x)}`).join('')}</details>`;
  $('#notesBody').innerHTML = `<div class="notes">${out || '<p class="hint">No release notes found.</p>'}</div>`;
  openDlg('#dlgNotes');
}
$('#verPill').addEventListener('click', showNotes);
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
