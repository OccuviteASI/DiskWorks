'use strict';
/* Access tab: the foreign-filesystem ladder for one partition, and the read-only file browser. */

const AC = { part: null, ev: null, busy: false };
const BR = { part: null, path: '', entries: [], sel: new Set(), source: '', copying: false, seq: 0 };

function fillAccessSelect() {
  const sel = $('#accSel'); if (!sel || !S.inv) return;
  const cur = sel.value || AC.part;
  sel.innerHTML = '';
  for (const d of visibleDisks(S.inv)) for (const p of d.partitions) {
    const o = document.createElement('option'); o.value = p.id;
    const word = fsWord(p) || p.typeName;
    o.textContent = `${d.name} · ${partTitle(p)} · ${fmtBytes(p.size)} · ${word}${p.mountpoints?.length ? ' · open' : ''}`;
    sel.appendChild(o);
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  else {
    // default to the first partition this computer cannot open by itself
    const native = { win32: ['ntfs', 'exfat', 'fat32', 'fat16', 'refs'], linux: ['ext4', 'ext3', 'ext2', 'btrfs', 'xfs', 'f2fs', 'exfat', 'fat32', 'fat16', 'ntfs'], darwin: ['apfs', 'hfsplus', 'exfat', 'fat32', 'fat16'] }[S.status?.platform] || [];
    const foreign = p => (p.fs && !native.includes(p.fs)) || (!p.fs && (p.typeName || '').startsWith('Linux'));
    const cand = S.inv.disks.flatMap(d => d.partitions).find(p => foreign(p) && !p.letter && !p.mountpoints?.length);
    if (cand) sel.value = cand.id;
  }
  AC.part = sel.value;
}

function dot(state) { return { ok: '<span class="dot ok">✓</span>', done: '<span class="dot ok">●</span>', no: '<span class="dot no">✕</span>', later: '<span class="dot maybe">…</span>', info: '<span class="dot">i</span>' }[state] || '<span class="dot">?</span>'; }

function rungLinks(r) {
  const links = [];
  if (r.url) links.push(r.url);
  for (const u of r.urls || []) links.push(u);
  if (!links.length) return '';
  return `<span class="links">${links.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https?:\/\//, ''))}</a>`).join('')}</span>`;
}

function renderLadder() {
  const box = $('#accBody'); const ev = AC.ev;
  if (!ev) { box.innerHTML = ''; return; }
  let html = `<h3 class="sub">${esc(ev.title)} <span class="soft">· ${esc(ev.fsLabel)}</span></h3><p class="hint">${esc(ev.summary)}</p>`;
  html += `<ol class="rungs">${ev.rungs.map(r => {
    let btns = '';
    if (r.state === 'ok' && r.browse) btns += `<button class="btn small primary" data-browse="1" ${AC.busy ? 'disabled' : ''}>Browse files</button>`;
    else if (r.state === 'ok') btns += `<button class="btn small primary" data-open="${esc(r.id)}" ${AC.busy ? 'disabled' : ''}>Open</button>`;
    if (r.state === 'done' && r.path) btns += `<button class="btn small" data-copy="${esc(r.path)}">Copy path</button>`;
    if (r.state === 'done' && r.browse) btns += `<button class="btn small" data-browse="1">Browse files</button>`;
    if (r.cmd) btns += `<button class="btn small" data-copy="${esc(r.cmd)}" title="${esc(r.cmd)}">Copy command</button>`;
    return `<li>${dot(r.state)}<div><b>${esc(r.title)}</b><span>${esc(r.why)} ${rungLinks(r)}</span></div><div>${btns}</div></li>`;
  }).join('')}</ol>`;
  if (ev.opened) html += `<div class="note">Opened by DiskWorks at <code>${esc(ev.opened.path || '')}</code> <button class="btn small" id="accClose" ${AC.busy ? 'disabled' : ''}>${ev.opened.kind === 'wsl' ? 'Detach from WSL' : ev.opened.kind === 'letter' ? 'Done' : 'Unmount'}</button>${ev.opened.kind === 'wsl' ? '<div class="hint" style="margin-top:6px">While attached, the whole disk belongs to WSL; Windows sees it again after Detach.</div>' : ''}</div>`;
  box.innerHTML = html;
  $$('[data-open]', box).forEach(b => b.addEventListener('click', () => openRung(b.dataset.open)));
  $$('[data-copy]', box).forEach(b => b.addEventListener('click', () => copyText(b.dataset.copy, 'Copied')));
  $$('[data-browse]', box).forEach(b => b.addEventListener('click', () => openBrowser(AC.part)));
  $('#accClose')?.addEventListener('click', closeRung);
}

async function evaluateLadder() {
  if (!S.status?.features?.access) { $('#accBody').innerHTML = '<p class="hint">The Access ladder is being wired up in this build.</p>'; return; }
  AC.part = $('#accSel').value;
  if (!AC.part) return;
  $('#accBody').innerHTML = '<p class="hint">Checking…</p>';
  const r = await api('/api/access/ladder', { part: AC.part });
  if (r.error) { $('#accBody').innerHTML = `<div class="error">${esc(r.error)}</div>`; return; }
  AC.ev = r; renderLadder();
}

function needUnlock() {
  if (S.status?.helper?.state === 'ready') return false;
  toast('Unlock first: opening a partition needs administrator rights.', 4000); $('#helperPill').click();
  return true;
}

async function openRung(id) {
  if (needUnlock()) return;
  AC.busy = true; renderLadder();
  const r = await api('/api/access/open', { part: AC.part, rung: id });
  AC.busy = false;
  if (r.error) { toast(r.error, 6000); renderLadder(); return; }
  if (r.browse) { openBrowser(AC.part); return; }
  toast(`Opened at ${r.path}`, 5000);
  await evaluateLadder();
}

async function closeRung() {
  AC.busy = true; renderLadder();
  const r = await api('/api/access/close', { part: AC.part });
  AC.busy = false;
  if (r.error) { toast(r.error, 6000); renderLadder(); return; }
  toast('Closed');
  await evaluateLadder();
}

/* ------------------------------------------------------------------------ */
/* Read-only file browser (7-Zip in the helper)                              */
/* ------------------------------------------------------------------------ */
function crumbsHtml(path, onRoot) {
  const parts = path.split('/').filter(Boolean);
  let acc = '';
  const items = [`<button data-path="">${esc(onRoot)}</button>`];
  for (const p of parts) { acc += (acc ? '/' : '') + p; items.push(`<span class="sepc">›</span><button data-path="${esc(acc)}" title="${esc(acc)}">${esc(p)}</button>`); }
  return items.join('');
}

async function openBrowser(partId, path = '') {
  if (needUnlock()) return;
  const p = findPart(partId, S.inv); if (!p) return;
  BR.part = partId; BR.path = path; BR.sel.clear();
  const box = $('#accBrowser'); box.classList.remove('hidden');
  $('#brList tbody').innerHTML = `<tr><td colspan="4" class="hint">Reading ${esc(path || 'the top folder')} of ${esc(partTitle(p))}… the first look at a partition can take a while.</td></tr>`;
  $('#brCrumb').innerHTML = crumbsHtml(path, partTitle(p));
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const r = await api('/api/access/browse', { part: partId, path });
  if (BR.part !== partId || BR.path !== path) return;
  if (r.error) { $('#brList tbody').innerHTML = `<tr><td colspan="4"><div class="error">${esc(r.error)}</div></td></tr>`; return; }
  BR.entries = r.entries || []; BR.source = r.source || '';
  $('#brSource').textContent = BR.source ? `· read through ${BR.source}` : '';
  renderBrowser(r.total);
}

function renderBrowser(total) {
  const tb = $('#brList tbody');
  if (!BR.entries.length) { tb.innerHTML = '<tr><td colspan="4" class="hint">This folder is empty (or the filesystem is not readable by the browser engine).</td></tr>'; }
  else tb.innerHTML = BR.entries.map((e, i) => `<tr class="${e.dir ? 'folder' : 'file'}" data-i="${i}">
      <td class="chkcol"><input type="checkbox" data-i="${i}" ${BR.sel.has(e.path) ? 'checked' : ''}></td>
      <td class="name" data-i="${i}"><span class="ico">${e.dir ? '▸' : '·'}</span>${esc(e.name)}</td>
      <td class="num">${e.dir ? '' : fmtBytes(e.size)}</td><td>${esc((e.mtime || '').slice(0, 19))}</td></tr>`).join('') +
    (total > BR.entries.length ? `<tr><td colspan="4" class="hint">Showing ${BR.entries.length} of ${total} items.</td></tr>` : '');
  $('#brAll').checked = BR.entries.length > 0 && BR.sel.size === BR.entries.length;
  $('#brCopy').disabled = !BR.sel.size || BR.copying;
  $('#brCopy').textContent = BR.sel.size ? `Copy out ${BR.sel.size} item${BR.sel.size > 1 ? 's' : ''}…` : 'Copy out…';
  $('#brUp').disabled = !BR.path;
}

$('#brList').addEventListener('change', e => {
  const cb = e.target.closest('input[type=checkbox]'); if (!cb) return;
  const ent = BR.entries[+cb.dataset.i]; if (!ent) return;
  if (cb.checked) BR.sel.add(ent.path); else BR.sel.delete(ent.path);
  renderBrowser(BR.entries.length);
});
$('#brList').addEventListener('click', e => {
  const td = e.target.closest('td.name'); if (!td) return;
  const ent = BR.entries[+td.dataset.i]; if (!ent || !ent.dir) return;
  openBrowser(BR.part, ent.path);
});
$('#brAll').addEventListener('change', e => { BR.sel = new Set(e.target.checked ? BR.entries.map(x => x.path) : []); renderBrowser(BR.entries.length); });
$('#brCrumb').addEventListener('click', e => { const b = e.target.closest('button'); if (b) openBrowser(BR.part, b.dataset.path); });
$('#brUp').addEventListener('click', () => openBrowser(BR.part, BR.path.split('/').slice(0, -1).join('/')));
$('#brClose').addEventListener('click', () => { $('#accBrowser').classList.add('hidden'); BR.part = null; });

$('#brCopy').addEventListener('click', async () => {
  if (!BR.sel.size || BR.copying) return;
  const pick = await api('/api/access/pickdir', {});
  if (pick.error) { toast(pick.error, 5000); return; }
  let dest = pick.path;
  if (!dest && pick.manual) dest = prompt('Copy to which folder on this computer?');
  if (!dest) return;
  BR.copying = true; renderBrowser(BR.entries.length);
  const prog = $('#brProgress'); prog.classList.remove('hidden'); $('#brBar').style.width = '0%'; $('#brMsg').textContent = 'Starting…';
  const r = await api('/api/access/copyout', { part: BR.part, paths: [...BR.sel], dest });
  if (r.error) { toast(r.error, 6000); BR.copying = false; prog.classList.add('hidden'); renderBrowser(BR.entries.length); return; }
  let since = 0;
  const tick = async () => {
    const ev = await api(`/api/access/events?since=${since}`);
    if (ev.error) { setTimeout(tick, 800); return; }
    since = ev.seq;
    for (const x of ev.events || []) {
      if (x.type === 'progress') { if (x.percent != null) $('#brBar').style.width = x.percent + '%'; $('#brMsg').textContent = (x.percent != null ? x.percent + ' % ' : '') + (x.message || ''); }
      if (x.type === 'done') {
        BR.copying = false;
        if (x.ok) { $('#brBar').style.width = '100%'; $('#brMsg').textContent = `Copied to ${dest}`; toast(`Copied ${BR.sel.size} item${BR.sel.size > 1 ? 's' : ''} to ${dest}`, 5000); BR.sel.clear(); }
        else { $('#brMsg').textContent = x.error || 'Copy failed'; toast(x.error || 'Copy failed', 6000); }
        renderBrowser(BR.entries.length);
        setTimeout(() => prog.classList.add('hidden'), 4000);
        return;
      }
    }
    if (BR.copying) setTimeout(tick, 400);
  };
  tick();
});

/* Entry point for the Disks tab's right-click menu / Actions strip. */
window.openAccessFor = function (partId) {
  AC.part = partId; AC.ev = null;
  showTab('access');
  fillAccessSelect();
  const sel = $('#accSel');
  if ([...sel.options].some(o => o.value === partId)) sel.value = partId;
  AC.part = sel.value;
  evaluateLadder();
};

document.addEventListener('redraw', fillAccessSelect);
document.addEventListener('tab', e => { if (e.detail === 'access') { fillAccessSelect(); if (AC.part) evaluateLadder(); } });
$('#btnAccEval').addEventListener('click', evaluateLadder);
$('#accSel').addEventListener('change', () => { AC.part = $('#accSel').value; AC.ev = null; $('#accBrowser').classList.add('hidden'); evaluateLadder(); });
