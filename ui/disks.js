'use strict';
/* Disks tab: Disk-Management-style volume table + per-disk bars, the detail pane with
   its Actions strip, the right-click context menu, and the system-disk filter.
   Operation dialogs and the pending queue live in ops.js. */

const OPS = {
  create:  { label: 'New partition…', kinds: ['gap'] },
  format:  { label: 'Format…', kinds: ['part'] },
  resize:  { label: 'Resize…', kinds: ['part'] },
  label:   { label: 'Label…', kinds: ['part'] },
  letter:  { label: 'Drive letter…', kinds: ['part'], win: true },
  check:   { label: 'Check', kinds: ['part'] },
  delete:  { label: 'Delete', kinds: ['part'], danger: true },
  table:   { label: 'New partition table…', kinds: ['disk'], danger: true },
  wipe:    { label: 'Wipe disk…', kinds: ['disk'], danger: true },
};
/* Shortcuts into the other tabs, listed after a separator. */
const LINKS = {
  access: { label: () => (isWin() ? 'Open in Windows…' : 'Open in Linux…'), kinds: ['part'] },
  backup: { label: () => 'Back up…', kinds: ['part', 'disk'] },
  write:  { label: () => 'Write an image to this disk…', kinds: ['disk'] },
};

/* ------------------------------------------------------------------------ */
/* Lookups                                                                   */
/* ------------------------------------------------------------------------ */
const invNow = () => S.preview || S.inv;
const isSystemDisk = d => !!(d.system || d.boot);
function visibleDisks(inv = invNow()) { return (inv?.disks || []).filter(d => S.showSystem || !isSystemDisk(d)); }
function findDisk(id, inv = invNow()) { return (inv?.disks || []).find(d => d.id === id); }
function findPart(id, inv = invNow()) { for (const d of inv?.disks || []) { const p = d.partitions.find(x => x.id === id); if (p) return p; } return null; }
function findGap(id, inv = invNow()) { for (const d of inv?.disks || []) { const g = (d.gaps || []).find(x => x.id === id); if (g) return g; } return null; }
function diskOf(obj) { return findDisk(obj.disk); }
const fsName = fs => fs ? ((S.fsinfo?.fs?.[fs]?.label) || fs) : '';
const isWin = () => (S.inv?.platform || S.status?.platform) === 'win32';

function partTitle(p) {
  const bits = [];
  if (p.letter) bits.push(p.letter + ':');
  if (p.label) bits.push(p.label);
  else if (p.name) bits.push(p.name);
  if (!bits.length) bits.push(p.typeName || 'Partition');
  return bits.join(' ');
}
function partStatus(p) {
  const f = p.flags || {}; const s = [];
  if (p.health) s.push(p.health);
  if (f.boot) s.push('Boot'); if (f.system) s.push('System'); if (f.esp) s.push('EFI');
  if (p.pagefile) s.push('Page file'); if (f.active) s.push('Active'); if (f.hidden) s.push('Hidden');
  if (p.swapActive) s.push('Swap on'); if (p.mountpoints?.length && !isWin()) s.push('Mounted');
  if (p.pendingOp) s.push('Pending: ' + p.pendingOp);
  return s.join(', ');
}
function segClass(p) {
  const t = (p.typeGuid || '').toLowerCase();
  if (t === 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b') return 'type-esp';
  if (t === 'e3c9e316-0b5c-4db8-817d-f92df00215ae') return 'type-msr';
  if (t === 'de94bba4-06d1-4d40-a16a-bfd50179d6ac') return 'type-recovery';
  return 'fs-' + (p.fs || 'none');
}
function fsWord(p) {
  if (p.fs) return fsName(p.fs);
  if (p.fsSource === 'raw') return 'RAW';
  if ((p.typeName || '').startsWith('Linux') || (p.typeName || '') === 'Unknown type') return isWin() ? 'Not readable by Windows' : 'Unknown';
  return p.typeName ? '' : 'Unknown';
}
function selTitle(sel) {
  if (!sel) return '';
  if (sel.kind === 'disk') { const d = findDisk(sel.id); return d ? `${d.name} · ${d.model || 'disk'}` : ''; }
  if (sel.kind === 'gap') { const g = findGap(sel.id); return g ? `Unallocated space on ${diskOf(g)?.name || ''}` : ''; }
  const p = findPart(sel.id); return p ? partTitle(p) : '';
}

/* ------------------------------------------------------------------------ */
/* What can be done with the selected item (buttons and the context menu)   */
/* ------------------------------------------------------------------------ */
function opsFor(sel) {
  const out = [];
  let obj, disk;
  if (sel.kind === 'disk') { obj = findDisk(sel.id); disk = obj; }
  else if (sel.kind === 'gap') { obj = findGap(sel.id); disk = obj && diskOf(obj); }
  else { obj = findPart(sel.id); disk = obj && diskOf(obj); }
  if (!obj || !disk) return out;
  const enabled = !!S.status?.features?.ops;
  for (const [id, op] of Object.entries(OPS)) {
    if (!op.kinds.includes(sel.kind)) continue;
    if (op.win && !isWin()) continue;
    let why = '';
    if (!enabled) why = 'Operations are not available in this build.';
    else if (sel.kind === 'part' && obj.locked?.length && !(obj.allow || []).includes(id)) why = obj.locked.join('; ');
    else if (sel.kind !== 'part' && disk.locked?.length) why = disk.locked.join('; ');
    else if (S.inv?.fixture) why = 'This is a saved inventory, not a real disk.';
    else if (sel.kind === 'part' && obj.new && id === 'delete') why = 'Remove the pending "create" from the queue instead.';
    out.push({ id, label: op.label, disabled: !!why, why, danger: !!op.danger });
  }
  for (const [id, l] of Object.entries(LINKS)) {
    if (!l.kinds.includes(sel.kind)) continue;
    let why = '';
    if (id === 'access' && !S.status?.features?.access) why = 'Not available in this build.';
    if (id !== 'access' && !S.status?.features?.image) why = 'Not available in this build.';
    if (sel.kind === 'part' && obj.new) why = 'This partition does not exist yet; apply the queue first.';
    if (id === 'write' && isSystemDisk(disk)) why = 'Never onto the disk that runs this computer.';
    if (S.inv?.fixture) why = 'This is a saved inventory, not a real disk.';
    out.push({ id, label: l.label(), disabled: !!why, why, link: true });
  }
  return out;
}

function runAction(id, sel) {
  if (OPS[id]) { window.openOpDialog && window.openOpDialog(id, sel); return; }
  if (id === 'access') { window.openAccessFor && window.openAccessFor(sel.id); return; }
  if (id === 'backup') {
    const p = sel.kind === 'part' ? findPart(sel.id) : null;
    window.openImageFor && window.openImageFor('backup', p ? p.disk : sel.id, p ? p.id : null);
    return;
  }
  if (id === 'write') { window.openImageFor && window.openImageFor('write', sel.id, null); }
}

/* ------------------------------------------------------------------------ */
/* Rendering                                                                 */
/* ------------------------------------------------------------------------ */
function widths(sizes, minPct = 7) {
  const total = sizes.reduce((a, b) => a + b, 0) || 1;
  let w = sizes.map(s => Math.max(minPct, 100 * s / total));
  const sum = w.reduce((a, b) => a + b, 0);
  if (sum > 100) {  // shrink the big ones so everything fits
    const big = w.filter(x => x > minPct).reduce((a, b) => a + b, 0);
    const over = sum - 100;
    w = w.map(x => x > minPct ? Math.max(minPct, x - over * (x / big)) : x);
  }
  return w;
}

function render() {
  const inv = invNow();
  if (!inv) return;
  const disks = visibleDisks(inv);
  const hiddenCount = (inv.disks || []).filter(isSystemDisk).length;
  $('#sysHint').textContent = !S.showSystem && hiddenCount ? `${hiddenCount} system disk${hiddenCount > 1 ? 's' : ''} hidden` : '';
  // a selection on a disk that just got hidden is dropped
  if (S.sel) {
    const owner = S.sel.kind === 'disk' ? findDisk(S.sel.id) : S.sel.kind === 'gap' ? diskOf(findGap(S.sel.id) || {}) : diskOf(findPart(S.sel.id) || {});
    if (!owner || !disks.includes(owner)) S.sel = null;
  }
  // volume table
  const rows = [];
  for (const d of disks) for (const p of d.partitions) {
    if (!p.fs && !p.letter && !p.label && !p.mountpoints?.length && !(p.typeName || '').startsWith('Linux')) continue;
    const pct = p.volSize ? 100 * (p.free ?? 0) / p.volSize : null;
    rows.push(`<tr data-kind="part" data-id="${esc(p.id)}" class="${S.sel?.id === p.id ? 'sel' : ''}">
      <td><span class="sw ${segClass(p)}"></span>${esc(partTitle(p))}</td><td>${esc(d.name)}</td>
      <td>${esc(p.typeName || '')}</td><td>${esc(fsWord(p))}</td><td>${esc(partStatus(p))}</td>
      <td class="num">${fmtBytes(p.size)}</td><td class="num">${p.free != null ? fmtBytes(p.free) : '—'}</td><td class="num">${pct != null ? fmtPct(pct) : '—'}</td></tr>`);
  }
  $('#volTable tbody').innerHTML = rows.join('') || `<tr><td colspan="8" class="hint">${disks.length ? 'No volumes found.' : (hiddenCount ? 'Only the system disk is present. Tick "Show system disks" to see it.' : 'No disks found.')}</td></tr>`;

  // disk bars
  const list = $('#diskList'); list.innerHTML = '';
  for (const d of disks) {
    const wrap = document.createElement('div'); wrap.className = 'disk';
    const status = d.offline ? 'Offline' : (d.health || 'Online');
    const kind = d.removable ? 'Removable' : (d.bus === 'Virtual' ? 'Virtual' : 'Basic');
    wrap.innerHTML = `<div class="dhead ${S.sel?.id === d.id ? 'sel' : ''}" data-kind="disk" data-id="${esc(d.id)}" title="Right-click for disk options">
        <b>${esc(d.name)}</b><span class="hint">${esc(kind)} · ${esc(d.table.toUpperCase())}</span>
        <span class="hint">${fmtBytes(d.size)}</span><span class="hint">${esc(status)}${isSystemDisk(d) ? ' · <span class="lockmark">System</span>' : ''}</span>
        <span class="hint">${esc(d.model || '')}</span></div><div class="dbar"></div>`;
    const bar = $('.dbar', wrap);
    const segs = d.segments || [];
    const w = widths(segs.map(s => s.size));
    segs.forEach((s, i) => {
      const el = document.createElement('div');
      el.style.width = w[i].toFixed(2) + '%';
      el.dataset.kind = s.kind; el.dataset.id = s.id;
      if (s.kind === 'gap') {
        el.className = 'seg gap' + (S.sel?.id === s.id ? ' sel' : '');
        el.title = 'Right-click to create a partition here';
        el.innerHTML = `<b>Unallocated</b><span>${fmtBytes(s.size)}</span>`;
      } else {
        const p = d.partitions.find(x => x.id === s.id);
        el.className = 'seg ' + segClass(p) + (S.sel?.id === s.id ? ' sel' : '') + (p.pendingOp ? ' pending' : '');
        const lock = p.locked?.length ? ' ' + lockIcon('closed') : '';
        el.title = partTitle(p) + ' — right-click for options';
        el.innerHTML = `<b>${esc(partTitle(p))}${lock}</b><span>${fmtBytes(p.size)} · ${esc(fsWord(p) || p.typeName || '')}</span><span class="flags">${esc(partStatus(p))}</span>`;
        const ri = resizeInfo(p, d);
        if (ri) { el.insertAdjacentHTML('beforeend', `<div class="grip" title="${ri.gap ? 'Drag to grow into the free space or to shrink' : 'Drag to shrink'}"></div>`); el.classList.add('resizable'); }
      }
      bar.appendChild(el);
    });
    list.appendChild(wrap);
  }
  if (!disks.length) list.innerHTML = `<p class="hint">${hiddenCount ? 'The only disk in this computer is the system disk. Tick "Show system disks" to see it.' : 'No disks found.'}</p>`;
  // legend
  const seen = new Map();
  for (const d of disks) for (const p of d.partitions) {
    const c = segClass(p);
    if (!seen.has(c)) seen.set(c, c.startsWith('type-') ? p.typeName : (fsWord(p) || p.typeName || 'Unknown'));
  }
  $('#legend').innerHTML = [...seen.entries()].map(([c, n]) => `<span><span class="sw seg ${c}"></span>${esc(n)}</span>`).join('') +
    `<span><span class="sw seg gap"></span>Unallocated</span>`;
  const info = $('#invInfo');
  if (!inv.fixture) info.textContent = `${inv.disks.length} disk${inv.disks.length === 1 ? '' : 's'} · updated ${fmtWhen(inv.ts)}${inv.refreshMs ? ' in ' + inv.refreshMs + ' ms' : ''}${inv.elevated ? ' · running as administrator' : ''}${inv.note ? ' · ' + inv.note : ''}`;
  $('#invError').classList.toggle('hidden', !inv.error);
  if (inv.error) $('#invError').textContent = inv.error;
  renderDetail();
  renderPending();
}

function kv(pairs) {
  return pairs.filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v, cls]) => `<div class="fact ${cls || ''}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join('');
}
function sizeCell(n) { return `${fmtBytes(n)} <span class="soft">(${fmtDec(n)} · ${Number(n).toLocaleString()} bytes)</span>`; }

function renderDetail() {
  const title = $('#detailTitle'), body = $('#detailBody'), ops = $('#detailOps');
  const sel = S.sel;
  ops.innerHTML = '';
  $('#detailActions').classList.toggle('hidden', !sel);
  if (!sel) { title.textContent = 'Select a disk, a partition or unallocated space — or right-click one'; body.innerHTML = ''; return; }
  if (sel.kind === 'disk') {
    const d = findDisk(sel.id); if (!d) { S.sel = null; return renderDetail(); }
    title.textContent = `${d.name} — ${d.model || 'disk'}`;
    body.innerHTML = kv([
      ['Model', esc(d.model)], ['Serial number', esc(d.serial), 'mono'], ['Connection', esc(d.bus) + (d.media ? ' · ' + esc(d.media) : '')],
      ['Size', sizeCell(d.size)], ['Sector size', `${d.logicalSector} B logical · ${d.physicalSector} B physical`],
      ['Partition table', esc(d.table.toUpperCase()) + (d.guid ? ` <span class="soft mono">${esc(d.guid)}</span>` : '')],
      ['Partitions', String(d.partitions.length)], ['Largest free space', d.largestFree ? fmtBytes(d.largestFree) : '—'],
      ['Removable', d.removable ? 'Yes' : 'No'], ['Hot-plug', d.hotplug ? 'Yes' : 'No'],
      ['Holds the running system', isSystemDisk(d) ? 'Yes' : 'No'], ['Status', esc(d.offline ? 'Offline' : (d.health || 'Online'))],
      ['Device path', `<code>${esc(d.path)}</code>`], ['Live system', d.live ? 'Yes (booted from removable media)' : undefined],
    ]);
    if (d.locked?.length) body.innerHTML += `<div class="lockbox warn">Protected: ${esc(d.locked.join('; '))}. Disk-level changes are not offered.</div>`;
    renderOps(sel);
    return;
  }
  if (sel.kind === 'gap') {
    const g = findGap(sel.id); if (!g) { S.sel = null; return renderDetail(); }
    const d = diskOf(g);
    title.textContent = `Unallocated space on ${d.name}`;
    body.innerHTML = kv([['Size', sizeCell(g.size)], ['Starts at', fmtBytes(g.start) + ` <span class="soft">(sector ${Math.floor(g.start / d.logicalSector)})</span>`],
      ['Ends at', fmtBytes(g.start + g.size)], ['Disk', esc(d.name) + ' · ' + esc(d.model)]]);
    renderOps(sel);
    return;
  }
  const p = findPart(sel.id); if (!p) { S.sel = null; return renderDetail(); }
  const d = diskOf(p);
  title.innerHTML = esc(partTitle(p)) + (p.locked?.length ? ' ' + lockIcon('closed') : '');
  const f = p.flags || {};
  body.innerHTML = kv([
    ['Disk', esc(d.name) + ' · partition ' + p.number],
    ['File system', esc(fsWord(p)) + (p.fsVersion ? ' ' + esc(p.fsVersion) : '') + (p.fsSource === 'signature' ? ' <span class="soft">(read from the partition itself)</span>' : '')],
    ['Label', esc(p.label)], ['Partition name', esc(p.name)],
    ['Drive letter / mount', esc([p.letter ? p.letter + ':' : null,
      ...(p.mountpoints || []).filter(m => !p.letter || m.replace(/\\$/, '').toUpperCase() !== p.letter + ':')].filter(Boolean).join(', '))],
    ['Size', sizeCell(p.size)],
    ['Used', p.used != null ? `${fmtBytes(p.used)} <span class="soft">(${fmtPct(100 * p.used / (p.volSize || p.size))})</span>` : undefined],
    ['Free', p.free != null ? fmtBytes(p.free) : undefined],
    ['Starts at', fmtBytes(p.start) + ` <span class="soft">(sector ${Math.floor(p.start / d.logicalSector)}${p.start % MiB === 0 ? ', 1 MiB aligned' : ''})</span>`],
    ['Ends at', fmtBytes(p.end)],
    ['Type', esc(p.typeName) + (p.typeGuid ? ` <span class="soft mono">${esc(p.typeGuid)}</span>` : p.mbrType != null ? ` <span class="soft">0x${Number(p.mbrType).toString(16).padStart(2, '0')}</span>` : '')],
    ['Partition GUID', p.guid ? `<span class="soft">${esc(p.guid)}</span>` : undefined, 'mono'],
    ['Filesystem UUID', p.uuid ? `<span class="soft">${esc(p.uuid)}</span>` : undefined, 'mono'],
    ['Cluster size', p.clusterSize ? fmtBytes(p.clusterSize, true) : undefined],
    ['BitLocker', p.bitlocker ? esc(`${p.bitlocker.protection === 'On' ? 'On' : 'Off'} · ${p.bitlocker.lock || ''} · ${p.bitlocker.conversion || ''}`) : undefined],
    ['Detected as', p.fsDetail ? esc(p.fsDetail) : undefined],
    ['Flags', esc(Object.entries(f).filter(([, v]) => v).map(([k]) => k).join(', ') || 'none')],
    ['Status', esc(partStatus(p) || '—')], ['Device', p.device ? `<code>${esc(p.device)}</code>` : undefined, 'mono'],
  ]);
  if (p.locked?.length) body.innerHTML += `<div class="lockbox warn">Protected: ${esc(p.locked.join('; '))}.${p.allow?.length ? ' Allowed here: ' + esc(p.allow.join(', ')) + '.' : ' No changes are offered for this partition.'}</div>`;
  renderOps(sel);
}

function renderOps(sel) {
  const ops = $('#detailOps');
  ops.innerHTML = '';
  let sep = false;
  for (const it of opsFor(sel)) {
    if (it.link && !sep) { const s = document.createElement('span'); s.className = 'sep'; ops.appendChild(s); sep = true; }
    const b = document.createElement('button');
    b.className = 'btn op' + (it.danger ? ' danger-ish' : '') + (it.link ? ' link' : '');
    b.innerHTML = (it.disabled ? lockIcon('closed') + ' ' : '') + esc(it.label);
    b.dataset.act = it.id;
    if (it.disabled) { b.disabled = true; b.title = it.why; }
    b.addEventListener('click', () => runAction(it.id, sel));
    ops.appendChild(b);
  }
}

function renderPending() {
  const n = S.pending.length;
  $('#pendingCount').textContent = n ? `(${n})` : '';
  $('#btnApply').disabled = !n; $('#btnClearOps').disabled = !n; $('#btnPreviewCmds').disabled = !n;
  $('#pendingList').innerHTML = S.pending.map((o, i) => `<li>${esc(o.text || o.op)} <button class="btn small rm" data-i="${i}" title="Remove">✕</button></li>`).join('');
  $$('#tabs button').forEach(b => { if (b.dataset.tab === 'disks') b.innerHTML = 'Disks' + (n ? `<span class="badge">${n}</span>` : ''); });
}

/* ------------------------------------------------------------------------ */
/* Drag-resize on the bars                                                   */
/* ------------------------------------------------------------------------ */
function platKey() { const pf = S.inv?.platform || S.status?.platform; return pf === 'win32' ? 'win' : pf === 'darwin' ? 'mac' : 'linux'; }
/* A partition gets a grip when it may be resized and there is somewhere to go: free space to its right (grow) or a shrinkable filesystem. */
function resizeInfo(p, d) {
  const op = opsFor({ kind: 'part', id: p.id }).find(o => o.id === 'resize');
  if (!op || op.disabled) return null;
  const segs = d.segments || [];
  const i = segs.findIndex(s => s.id === p.id);
  const next = segs[i + 1];
  const gap = next && next.kind === 'gap' ? next : null;
  const m = S.fsinfo?.fs?.[p.fs]?.[platKey()];
  const shrink = !!(m && m.shrink);
  if (!gap && !shrink) return null;
  return { gap, shrink };
}
const DR = { a: null };
const dragLabel = $('#dragLabel');
function dragStart(e, segEl) {
  const p = findPart(segEl.dataset.id); if (!p) return;
  const d = diskOf(p); const ri = resizeInfo(p, d); if (!ri) return;
  const gapEl = ri.gap ? segEl.nextElementSibling : null;
  const gapPx = gapEl ? gapEl.getBoundingClientRect().width + 3 : 0;   // +3 = the flex gap between segments
  const a = { p, d, ri, segEl, gapEl, x0: e.clientX, w0: segEl.getBoundingClientRect().width, pxSpan: 0, byteSpan: 0, size: p.size,
              min: Math.max(MiB * 4, p.used || 0), max: p.size + (ri.gap ? ri.gap.size : 0), probed: false, note: '' };
  a.pxSpan = a.w0 + gapPx;
  a.byteSpan = a.max;
  if (!ri.shrink) a.min = p.size;                 // grow only
  DR.a = a;
  segEl.classList.add('dragging');
  document.body.classList.add('resizing');
  dragLabel.classList.remove('hidden');
  dragMove(e);
  // the exact minimum comes from the filesystem; until then the used space is the floor
  if (ri.shrink) api('/api/fs/probe', { part: p.id }).then(pr => {
    if (DR.a !== a || pr.error) return;
    if (pr.minSize) a.min = Math.max(a.min, Math.ceil(pr.minSize / MiB) * MiB);
    a.probed = !!pr.probed; a.note = pr.probed ? '' : 'Unlock for the exact minimum';
    dragMove({ clientX: a.lastX ?? e.clientX, clientY: a.lastY ?? e.clientY });
  });
  e.preventDefault();
}
function dragMove(e) {
  const a = DR.a; if (!a) return;
  a.lastX = e.clientX; a.lastY = e.clientY;
  const dx = e.clientX - a.x0;
  const px = Math.max(8, Math.min(a.pxSpan, a.w0 + dx));
  let size = Math.round(a.byteSpan * px / a.pxSpan / MiB) * MiB;
  size = Math.max(a.min, Math.min(a.max, size));
  a.size = size;
  const segPx = a.pxSpan * size / a.byteSpan;
  a.segEl.style.width = segPx + 'px';
  a.segEl.style.flex = '0 0 auto';
  if (a.gapEl) {
    const rest = a.pxSpan - segPx - 3;
    a.gapEl.style.width = Math.max(0, rest) + 'px'; a.gapEl.style.flex = '0 0 auto';
    a.gapEl.style.visibility = rest < 6 ? 'hidden' : '';
  }
  const delta = size - a.p.size;
  const word = delta > 0 ? `grow by ${fmtBytes(delta)}` : delta < 0 ? `shrink by ${fmtBytes(-delta)}` : 'no change';
  dragLabel.innerHTML = `<b>${fmtBytes(a.p.size)} → ${fmtBytes(size)}</b><span>${esc(word)}${size === a.min && a.min > MiB * 4 && delta < 0 ? ' · at the smallest size' : ''}${size === a.max && a.ri.gap ? ' · fills the free space' : ''}${a.note ? ' · ' + esc(a.note) : ''}</span><span class="soft">Release to queue · Esc to cancel</span>`;
  dragLabel.style.left = Math.min(window.innerWidth - 300, e.clientX + 14) + 'px';
  dragLabel.style.top = (e.clientY + 18) + 'px';
}
function dragEnd(commit) {
  const a = DR.a; if (!a) return;
  DR.a = null;
  dragLabel.classList.add('hidden');
  document.body.classList.remove('resizing');
  a.segEl.classList.remove('dragging');
  if (commit && Math.abs(a.size - a.p.size) >= MiB) {
    // one pending resize per partition: a new drag replaces the earlier one
    S.pending = S.pending.filter(o => !(o.op === 'resize' && o.part === a.p.id));
    S.pending.push({ op: 'resize', part: a.p.id, size: a.size, minSize: a.min });
    replan().then(r => {
      if (r && r.errors?.length && r.errors[r.errors.length - 1].op === S.pending.length - 1) {
        S.pending.pop(); replan(); toast(r.errors[r.errors.length - 1].message, 5000);
      } else toast(`Resize queued: ${partTitle(a.p)} → ${fmtBytes(a.size)}. Press Apply when ready.`, 3500);
    });
  } else render();
}
$('#diskList').addEventListener('pointerdown', e => {
  const grip = e.target.closest('.grip'); if (!grip || e.button !== 0) return;
  e.stopPropagation();
  dragStart(e, grip.closest('.seg'));
});
document.addEventListener('pointermove', e => { if (DR.a) dragMove(e); });
document.addEventListener('pointerup', () => { if (DR.a) dragEnd(true); });
document.addEventListener('pointercancel', () => { if (DR.a) dragEnd(false); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && DR.a) dragEnd(false); });
// a click that ends a drag must not toggle the selection
$('#diskList').addEventListener('click', e => { if (e.target.closest('.grip')) { e.stopPropagation(); e.preventDefault(); } }, true);

/* ------------------------------------------------------------------------ */
/* Selection + context menu                                                  */
/* ------------------------------------------------------------------------ */
function select(kind, id) {
  S.sel = (S.sel && S.sel.id === id) ? null : { kind, id };
  render();
}
function selectOnly(kind, id) {
  if (!S.sel || S.sel.id !== id) { S.sel = { kind, id }; render(); }
}
document.addEventListener('click', e => {
  const t = e.target.closest('[data-kind][data-id]');
  if (t && $('#tab-disks').contains(t) && !e.target.closest('#ctxMenu')) select(t.dataset.kind, t.dataset.id);
});

const ctx = $('#ctxMenu');
function showContextMenu(x, y, sel) {
  const items = opsFor(sel);
  if (!items.length) { hideContextMenu(); return; }
  let html = `<div class="ctx-title">${esc(selTitle(sel))}</div>`;
  let sep = false;
  for (const it of items) {
    if (it.link && !sep) { html += '<hr>'; sep = true; }
    html += `<button role="menuitem" data-act="${esc(it.id)}" class="${it.danger ? 'danger' : ''}" ${it.disabled ? 'disabled' : ''}>
      <span class="lbl">${esc(it.label)}</span>${it.disabled && it.why ? `<span class="why">${esc(it.why)}</span>` : ''}</button>`;
  }
  ctx.innerHTML = html;
  ctx.dataset.kind = sel.kind; ctx.dataset.id = sel.id;
  ctx.style.left = '0px'; ctx.style.top = '0px';
  ctx.classList.remove('hidden');
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 8)) + 'px';
  ctx.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 8)) + 'px';
}
function hideContextMenu() { if (!ctx.classList.contains('hidden')) { ctx.classList.add('hidden'); ctx.innerHTML = ''; } }
ctx.addEventListener('click', e => {
  const b = e.target.closest('button[data-act]');
  if (!b || b.disabled) return;
  const sel = { kind: ctx.dataset.kind, id: ctx.dataset.id };
  hideContextMenu();
  runAction(b.dataset.act, sel);
});
document.addEventListener('contextmenu', e => {
  const t = e.target.closest('[data-kind][data-id]');
  if (!t || !$('#tab-disks').contains(t) || $('#tab-disks').classList.contains('hidden')) { hideContextMenu(); return; }
  e.preventDefault();
  selectOnly(t.dataset.kind, t.dataset.id);
  showContextMenu(e.clientX, e.clientY, { kind: t.dataset.kind, id: t.dataset.id });
});
document.addEventListener('mousedown', e => { if (!e.target.closest('#ctxMenu')) hideContextMenu(); }, true);
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideContextMenu(); });
window.addEventListener('scroll', hideContextMenu, true);
window.addEventListener('resize', hideContextMenu);
window.addEventListener('blur', hideContextMenu);
document.addEventListener('tab', hideContextMenu);

/* ------------------------------------------------------------------------ */
/* Toolbar + inventory polling                                               */
/* ------------------------------------------------------------------------ */
S.showSystem = false;   // every launch starts with the system disk out of reach
$('#chkSystem').addEventListener('change', e => {
  S.showSystem = e.target.checked;
  render();
  document.dispatchEvent(new CustomEvent('redraw'));
});

async function loadInventory(refresh) {
  const inv = await api('/api/inventory' + (refresh ? '?refresh=1' : ''));
  if (inv.error && !inv.disks) { $('#invError').textContent = inv.error; $('#invError').classList.remove('hidden'); return; }
  S.inv = inv; S.invHash = inv.hash;
  if (S.pending.length && window.replan) window.replan(); else render();
}
async function pollInventory() {
  const r = await api(`/api/inventory/events?since=${S.invSeq}`);
  if (r.error || !r.events) return;
  let changed = false;
  for (const e of r.events) { S.invSeq = e.seq; if (e.type === 'changed') changed = true; }
  if (changed || (r.hash && r.hash !== S.invHash)) await loadInventory(false);
}
$('#btnRefresh').addEventListener('click', () => loadInventory(true));
document.addEventListener('booted', () => { loadInventory(false); setInterval(pollInventory, 1500); });
document.addEventListener('redraw', () => { if (!$('#tab-disks').classList.contains('hidden')) render(); });
let lastStatusKey = '';
document.addEventListener('status', e => {
  // Only redraw when something the Disks tab shows has changed (feature flags, helper state).
  const s = e.detail || {};
  const key = JSON.stringify([s.features, s.helper?.state, s.fixture]);
  if (key !== lastStatusKey) { lastStatusKey = key; render(); }
});
