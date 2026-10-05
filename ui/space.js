'use strict';
/* Space tab: DaisyDisk-style rings or a WizTree-style treemap of what uses a drive, the
   largest files and the per-file-type totals of the whole scan, with trash / delete. */

const SC = { root: null, path: '', node: null, map: null, sel: new Set(), known: new Map(), scanning: false, since: 0, timer: null, total: 0, capacity: null, hover: null,
             view: 'rings', types: null, largest: null, showFree: false };
const fmtDay = t => t ? (typeof t === 'number' ? new Date(t * 1000).toLocaleDateString() : String(t).slice(0, 10)) : '';
const tip = document.createElement('div'); tip.className = 'tooltip hidden'; document.body.appendChild(tip);

function fillSpaceTargets() {
  api('/api/space/targets').then(r => {
    if (r.error) return;
    const sel = $('#spcTarget'); const cur = sel.value;
    sel.innerHTML = (r.targets || []).map(t => `<option value="${esc(t.root)}" data-size="${t.size || ''}">${esc(t.root)}${t.title ? ' · ' + esc(t.title) : ''}${t.disk ? ' · ' + esc(t.disk) : ''}${t.used != null ? ' · ' + fmtBytes(t.used) + ' used' : ''}${t.system ? ' · system' : ''}</option>`).join('')
      || '<option value="">No drive found</option>';
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  });
}

/* --- scanning -------------------------------------------------------------- */
function setScanning(on) {
  SC.scanning = on;
  $('#spcScan').disabled = on; $('#spcStop').disabled = !on; $('#spcTarget').disabled = on;
}
async function startScan() {
  const root = $('#spcTarget').value;
  if (!root) { toast('Pick a drive or folder first'); return; }
  $('#spcError').classList.add('hidden');
  const opt = $('#spcTarget').selectedOptions[0];
  SC.capacity = opt && opt.dataset.size ? +opt.dataset.size : null;
  const r = await api('/api/space/scan', { root });
  if (r.error) { $('#spcError').textContent = r.error; $('#spcError').classList.remove('hidden'); return; }
  SC.root = r.root; SC.path = ''; SC.node = null; SC.map = null; SC.sel.clear(); SC.known.clear(); SC.types = null; SC.largest = null; SC.since = 0;
  $('#spcResult').classList.add('hidden');
  setScanning(true);
  $('#spcStatus').textContent = 'Scanning…';
  pollScan();
}
async function pollScan() {
  clearTimeout(SC.timer);
  const ev = await api(`/api/space/events?since=${SC.since}`);
  if (ev.error) { SC.timer = setTimeout(pollScan, 1000); return; }
  SC.since = ev.seq;
  let done = false;
  for (const x of ev.events || []) {
    if (x.type === 'progress') $('#spcStatus').textContent = `Scanning… ${x.files.toLocaleString()} files, ${fmtBytes(x.bytes)} · ${x.current}`;
    else if (x.type === 'error') { $('#spcError').textContent = x.message; $('#spcError').classList.remove('hidden'); }
    else if (x.type === 'done') {
      done = true;
      $('#spcStatus').textContent = `${x.stopped ? 'Stopped early' : 'Done'}: ${x.files.toLocaleString()} files in ${x.dirs.toLocaleString()} folders, ${fmtBytes(x.bytes)}${x.skipped ? ` · ${x.skipped} item${x.skipped > 1 ? 's' : ''} could not be read` : ''}`;
    }
  }
  if (done || !ev.state?.running) { setScanning(false); if (ev.state?.root) { SC.root = ev.state.root; SC.total = ev.state.bytes; loadNode(''); } return; }
  SC.timer = setTimeout(pollScan, 500);
}

/* --- tree ---------------------------------------------------------------- */
async function loadNode(rel, keepSel) {
  const r = await api(`/api/space/tree?path=${encodeURIComponent(rel)}&depth=2`);
  if (r.error) { toast(r.error, 5000); return; }
  SC.path = rel; SC.node = r.node; SC.map = null;
  if (!keepSel) SC.sel.clear();
  $('#spcResult').classList.remove('hidden');
  renderCrumb(); renderSunburst(); renderList();
  if (SC.view === 'treemap') loadMap();
  loadExtras();
}
/* The treemap wants more depth than the rings but only tiles big enough to see: the server
   prunes everything under 0.05 % of the folder being drawn and sums what it left out. */
async function loadMap() {
  const rel = SC.path;
  const r = await api(`/api/space/tree?path=${encodeURIComponent(rel)}&depth=4&min=0.0005`);
  if (r.error || rel !== SC.path) return;
  SC.map = r.node;
  renderMap();
}
async function loadExtras() {
  const [t, l] = await Promise.all([api('/api/space/types'), api('/api/space/largest?n=200')]);
  if (!t.error) { SC.types = t; renderTypes(); }
  if (!l.error) { SC.largest = l; renderLargest(); }
}
function renderCrumb() {
  const parts = SC.path.split('/').filter(Boolean);
  let acc = '';
  const items = [`<button data-path="" title="${esc(SC.root)}">${esc(SC.root)}</button>`];
  for (const p of parts) { acc += (acc ? '/' : '') + p; items.push(`<span class="sepc">›</span><button data-path="${esc(acc)}">${esc(p)}</button>`); }
  $('#spcCrumb').innerHTML = items.join('');
  $('#spcUp').disabled = !SC.path;
}

/* Everything shown in one node: its subfolders, its own files (top list + "other files"), and free space at the root. */
function slices(node) {
  const out = [];
  for (const c of node.children || []) out.push({ kind: 'dir', name: c.name, rel: c.rel, size: c.size, files: c.files, dirs: c.dirs, node: c });
  if (node.moreDirs) out.push({ kind: 'more', name: `${node.moreDirs.count} more folders`, size: node.moreDirs.size });
  for (const f of node.top || []) out.push({ kind: 'file', name: f.name, rel: f.rel, size: f.size, mtime: f.mtime });
  if (node.other) out.push({ kind: 'other', name: `${node.otherCount.toLocaleString()} smaller files`, size: node.other });
  return out;
}
function hue(i, n) { return `hsl(${Math.round(210 + 300 * i / Math.max(1, n)) % 360} 62% 52%)`; }
function wedge(r0, r1, a0, a1) {
  if (a1 - a0 >= Math.PI * 2 - 1e-4) a1 = a0 + Math.PI * 2 - 1e-4;
  const p = (r, a) => `${(r * Math.cos(a)).toFixed(2)} ${(r * Math.sin(a)).toFixed(2)}`;
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M ${p(r0, a0)} L ${p(r1, a0)} A ${r1} ${r1} 0 ${large} 1 ${p(r1, a1)} L ${p(r0, a1)} A ${r0} ${r0} 0 ${large} 0 ${p(r0, a0)} Z`;
}
function renderSunburst() {
  const node = SC.node; const svg = $('#sunburst');
  const showFree = SC.showFree && !SC.path && SC.capacity && SC.capacity > node.size;
  const total = showFree ? SC.capacity : Math.max(1, node.size);
  const R0 = 62, R1 = 108, R2 = 152;
  const items = slices(node);
  let a = -Math.PI / 2; const paths = [];
  const minA = 0.004;
  items.forEach((it, i) => {
    const span = 2 * Math.PI * it.size / total;
    if (span < minA) { a += span; return; }
    const color = it.kind === 'dir' ? hue(i, items.length) : it.kind === 'file' ? hue(i, items.length) : 'var(--soft)';
    paths.push(`<path d="${wedge(R0, R1, a, a + span)}" fill="${color}" data-i="${i}" data-ring="1"/>`);
    if (it.kind === 'dir' && it.node && it.node.children) {
      // the outer ring shows what is inside each subfolder
      let b = a; const inner = slices(it.node);
      inner.forEach((jt, j) => {
        const s2 = span * jt.size / Math.max(1, it.size);
        if (s2 >= minA) paths.push(`<path d="${wedge(R1 + 2, R2, b, b + s2)}" fill="${color}" style="opacity:${0.85 - 0.35 * (j % 2)}" data-i="${i}" data-j="${j}" data-ring="2"/>`);
        b += s2;
      });
    }
    a += span;
  });
  if (showFree) paths.push(`<path class="free" d="${wedge(R0, R1, a, -Math.PI / 2 + 2 * Math.PI)}" data-free="1"/>`);
  const upTo = SC.path ? (SC.path.split('/').slice(0, -1).pop() || SC.root) : null;
  svg.innerHTML = `<circle class="hub${SC.path ? ' up' : ''}" r="${R0 - 3}" data-up="1"/>` + paths.join('');
  $('#sunCenter').innerHTML = `${SC.path ? '<span class="uparrow" aria-hidden="true">↑</span>' : ''}<b title="${esc(SC.root + (SC.path ? '/' + SC.path : ''))}">${esc(SC.path ? SC.path.split('/').pop() : SC.root)}</b>${fmtBytes(node.size)}<br>${(node.files || 0).toLocaleString()} files`;
  $('#sunburst').querySelector('circle.hub').innerHTML = upTo ? `<title>Back up to ${esc(upTo)}</title>` : '';
  svg._items = items;
}
function itemAt(pathEl) {
  const items = $('#sunburst')._items || [];
  const it = items[+pathEl.dataset.i]; if (!it) return null;
  if (pathEl.dataset.ring === '2' && it.node) { const inner = slices(it.node)[+pathEl.dataset.j]; return inner ? { ...inner, parent: it } : it; }
  return it;
}
function goUp() { if (SC.path) loadNode(SC.path.split('/').slice(0, -1).join('/')); }
$('#sunburst').addEventListener('mousemove', e => {
  const p = e.target.closest('path'); if (!p || p.dataset.free) { tip.classList.add('hidden'); return; }
  const it = itemAt(p); if (!it) return;
  const base = SC.node.size || 1;
  tip.innerHTML = `<b>${esc(it.parent ? it.parent.name + ' / ' : '')}${esc(it.name)}</b>${fmtBytes(it.size)} · ${(100 * it.size / base).toFixed(1)} % of this level${it.kind === 'dir' ? ` · ${(it.files || 0).toLocaleString()} files` : ''}`;
  tip.style.left = Math.min(window.innerWidth - 330, e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 16) + 'px';
  tip.classList.remove('hidden');
});
$('#sunburst').addEventListener('mouseleave', () => tip.classList.add('hidden'));
$('#sunburst').addEventListener('click', e => {
  if (e.target.closest('circle.hub')) { goUp(); return; }
  const p = e.target.closest('path'); if (!p || p.dataset.free) return;
  const it = itemAt(p); if (!it) return;
  if (it.kind === 'dir') loadNode(it.rel);
  else if (it.kind === 'file') { SC.sel.clear(); SC.sel.add(it.rel); renderList(); }
});

function renderList() {
  const node = SC.node; const items = slices(node); const base = node.size || 1;
  for (const it of items) if (it.rel != null) SC.known.set(it.rel, it.size);
  $('#spcList tbody').innerHTML = items.map((it, i) => {
    const selectable = it.kind === 'dir' || it.kind === 'file';
    return `<tr class="${it.kind === 'dir' ? 'folder' : 'file'} ${SC.sel.has(it.rel) ? 'sel-row' : ''}" data-i="${i}">
      <td class="chkcol">${selectable ? `<input type="checkbox" data-rel="${esc(it.rel)}" ${SC.sel.has(it.rel) ? 'checked' : ''}>` : ''}</td>
      <td class="name" data-i="${i}"><span class="ico" style="color:${it.kind === 'dir' || it.kind === 'file' ? hue(i, items.length) : 'var(--soft)'}">${it.kind === 'dir' ? '▸' : '●'}</span>${esc(it.name)}</td>
      <td class="num">${fmtBytes(it.size)}</td>
      <td class="num">${(100 * it.size / base).toFixed(1)} %<span class="share"><i style="width:${Math.min(100, 100 * it.size / base).toFixed(1)}%"></i></span></td>
      <td class="num">${it.kind === 'dir' ? `${(it.files || 0).toLocaleString()} files${it.dirs ? `, ${it.dirs.toLocaleString()} folders` : ''}` : it.kind === 'file' ? esc(fmtDay(it.mtime)) : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="hint">This folder is empty.</td></tr>';
  paintSelButtons();
  $('#spcAll').checked = SC.sel.size > 0 && items.filter(x => x.kind === 'dir' || x.kind === 'file').every(x => SC.sel.has(x.rel));
  if (SC.view === 'treemap' && SC.map) renderMap();
}
function paintSelButtons() {
  const n = SC.sel.size;
  $('#spcTrash').disabled = !n; $('#spcDelete').disabled = !n; $('#spcReveal').disabled = n !== 1;
  $('#spcTrash').textContent = n ? `Move ${n} to ${S.status?.platform === 'win32' ? 'Recycle Bin' : 'Trash'}` : `Move to ${S.status?.platform === 'win32' ? 'Recycle Bin' : 'Trash'}`;
}
$('#spcList').addEventListener('change', e => {
  const cb = e.target.closest('input[type=checkbox]'); if (!cb) return;
  if (cb.checked) SC.sel.add(cb.dataset.rel); else SC.sel.delete(cb.dataset.rel);
  renderList(); renderLargest();
});
$('#spcList').addEventListener('click', e => {
  const td = e.target.closest('td.name'); if (!td) return;
  const it = slices(SC.node)[+td.dataset.i]; if (!it) return;
  if (it.kind === 'dir') loadNode(it.rel);
});
$('#spcAll').addEventListener('change', e => {
  SC.sel = new Set(e.target.checked ? slices(SC.node).filter(x => x.kind === 'dir' || x.kind === 'file').map(x => x.rel) : []);
  renderList();
});
$('#spcCrumb').addEventListener('click', e => { const b = e.target.closest('button'); if (b) loadNode(b.dataset.path); });
$('#spcUp').addEventListener('click', () => goUp());
$('#spcReveal').addEventListener('click', () => { const rel = [...SC.sel][0]; if (rel != null) api('/api/space/reveal', { path: rel }).then(r => { if (r.error) toast(r.error, 5000); }); });

async function removeSelected(mode) {
  const rels = [...SC.sel]; if (!rels.length) return;
  const bytes = rels.reduce((a, r) => a + (SC.known.get(r) || 0), 0);
  const list = rels.slice(0, 12).map(r => `<li><code>${esc(r.split('/').pop())}</code></li>`).join('') + (rels.length > 12 ? `<li>… and ${rels.length - 12} more</li>` : '');
  if (mode === 'permanent') {
    const ok = await confirmTyped('Delete permanently?', `<p>These ${rels.length} item${rels.length > 1 ? 's' : ''} (${fmtBytes(bytes)}) will be deleted for good — they do <b>not</b> go to the Recycle Bin / Trash and cannot be brought back.</p><ul>${list}</ul><p>Type <b>delete</b> to continue.</p>`, 'delete', 'Delete permanently');
    if (!ok) return;
  } else if (rels.length > 5 || bytes > 5 * GiB) {
    const ok = await confirmTyped('Move to the Recycle Bin / Trash?', `<p>${rels.length} item${rels.length > 1 ? 's' : ''}, ${fmtBytes(bytes)}. You can restore them from the Recycle Bin / Trash later.</p><ul>${list}</ul><p>Type <b>trash</b> to continue.</p>`, 'trash', 'Move');
    if (!ok) return;
  }
  $('#spcStatus').textContent = mode === 'permanent' ? 'Deleting…' : 'Moving to the Recycle Bin / Trash…';
  const r = await api('/api/space/delete', { paths: rels, mode });
  if (r.error) { toast(r.error, 6000); $('#spcStatus').textContent = ''; return; }
  const n = (r.deleted || []).length;
  $('#spcStatus').textContent = `${mode === 'permanent' ? 'Deleted' : 'Moved'} ${n} item${n !== 1 ? 's' : ''}, ${fmtBytes(r.freed || 0)} freed${r.errors?.length ? ` · ${r.errors.length} failed` : ''}`;
  if (r.errors?.length) toast(r.errors[0].error || r.errors[0], 6000);
  else toast(`${fmtBytes(r.freed || 0)} freed`);
  SC.sel.clear();
  loadNode(SC.path);
}

/* --- treemap (WizTree style) ------------------------------------------------- */
/* Files are coloured by what they are; folders are frames with a name strip. */
const TYPE_GROUPS = [
  ['Video', '#d9534f', 'mp4 mkv avi mov wmv flv m4v webm mpg mpeg ts m2ts mts vob 3gp ogv divx'],
  ['Pictures', '#f0a030', 'jpg jpeg png gif bmp tif tiff heic heif webp raw cr2 cr3 nef arw dng psd ai svg ico'],
  ['Music', '#b05bd6', 'mp3 flac wav aac m4a ogg opus wma aiff alac ape'],
  ['Archives', '#8a6d3b', 'zip rar 7z gz bz2 xz zst tar tgz cab lz lzma deb rpm pkg'],
  ['Documents', '#3b8ad9', 'pdf doc docx xls xlsx ppt pptx odt ods odp txt rtf md epub mobi csv one'],
  ['Code', '#2ab0a0', 'c h cpp hpp cs java py js ts jsx tsx go rs rb php html css json xml yml yaml toml sh ps1 bat sql ipynb'],
  ['Programs & system', '#6f788c', 'exe dll sys msi so dylib bin app pak dat lib obj pdb o a cache idx etl log tmp pyc pyd jmod jsa jar class bc debug ko elf'],
  ['Disk images & VMs', '#5b5fd6', 'iso img vhd vhdx vmdk vdi qcow2 dmg wim esd ova'],
  ['Databases', '#c99a1e', 'db sqlite sqlite3 mdb accdb ldb pst ost edb'],
];
const EXT_COLOR = new Map();
for (const [, color, exts] of TYPE_GROUPS) for (const x of exts.split(' ')) EXT_COLOR.set(x, color);
const OTHER_COLOR = '#9aa3b5';
const extOf = name => { const i = name.lastIndexOf('.'); const e = i > 0 && name.length - i <= 12 ? name.slice(i + 1).toLowerCase() : ''; return /^\d+$/.test(e) ? '' : e; };
const colorFor = name => EXT_COLOR.get(extOf(name)) || OTHER_COLOR;
const groupOf = ext => (TYPE_GROUPS.find(g => g[2].split(' ').includes(ext)) || [])[0] || 'Other';

/* Squarified layout (Bruls, Huizing, van Wijk): items sorted by size, laid in rows along the
   shorter side so tiles stay close to square. Returns one rectangle per item, in order. */
function squarify(items, x, y, w, h) {
  const out = new Array(items.length);
  const total = items.reduce((a, it) => a + it.size, 0);
  if (!total || w <= 0 || h <= 0) return out;
  const scale = (w * h) / total;
  let i = 0;
  while (i < items.length) {
    const horizontal = w >= h;            // the row runs along the shorter side
    const side = horizontal ? h : w;
    const row = []; let area = 0; let worst = Infinity;
    while (i < items.length) {
      const a = Math.max(items[i].size * scale, 1e-9);
      const na = area + a; const t = na / side;
      let wr = 0;
      for (const b of row.concat(a)) { const l = b / t; wr = Math.max(wr, l / t, t / l); }
      if (row.length && wr > worst) break;
      row.push(a); area = na; worst = wr; i++;
    }
    const thick = area / side;
    let off = 0;
    for (let k = 0; k < row.length; k++) {
      const len = row[k] / thick; const idx = i - row.length + k;
      out[idx] = horizontal ? { x, y: y + off, w: thick, h: len } : { x: x + off, y, w: len, h: thick };
      off += len;
    }
    if (horizontal) { x += thick; w -= thick; } else { y += thick; h -= thick; }
  }
  return out;
}

const MAP = { rects: [], dpr: 1, w: 0, h: 0 };
const HEADER = 16, PAD = 2;
function mapItems(node) {
  const items = slices(node).filter(it => it.size > 0);
  if (node.prunedFiles) items.push({ kind: 'pruned', name: 'smaller files', size: node.prunedFiles });
  return items.sort((a, b) => b.size - a.size);
}
function drawTiles(ctx, node, x, y, w, h, depth, path) {
  const items = mapItems(node);
  const rects = squarify(items, x, y, w, h);
  const css = getComputedStyle(document.documentElement);
  const line = css.getPropertyValue('--card').trim() || '#fff';
  const ink = css.getPropertyValue('--text').trim() || '#222';
  items.forEach((it, i) => {
    const r = rects[i]; if (!r || r.w < 0.5 || r.h < 0.5) return;
    if (it.kind === 'dir') {
      const deeper = it.node && it.node.children && r.w > 28 && r.h > HEADER + 10 && depth < 4;
      ctx.fillStyle = deeper ? 'rgba(127,140,170,.18)' : 'rgba(127,140,170,.45)';
      ctx.fillRect(r.x, r.y, r.w, r.h);
      ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.strokeRect(r.x + .5, r.y + .5, r.w - 1, r.h - 1);
      if (r.w > 36 && r.h > HEADER) {
        ctx.fillStyle = ink; ctx.globalAlpha = .85;
        ctx.font = '600 11px system-ui, sans-serif'; ctx.textBaseline = 'middle';
        ctx.fillText(clip(ctx, it.name, r.w - 8), r.x + 4, r.y + HEADER / 2 + 1);
        ctx.globalAlpha = 1;
      }
      MAP.rects.push({ ...r, it, depth, header: deeper ? HEADER : 0 });
      if (deeper) drawTiles(ctx, it.node, r.x + PAD, r.y + HEADER, r.w - 2 * PAD, r.h - HEADER - PAD, depth + 1, it.rel);
      return;
    }
    const color = it.kind === 'file' ? colorFor(it.name) : it.kind === 'free' ? line : OTHER_COLOR;
    ctx.fillStyle = color; ctx.globalAlpha = it.kind === 'file' ? 1 : .55;
    if (it.kind === 'file' && SC.sel.has(it.rel)) ctx.globalAlpha = 1;
    ctx.fillRect(r.x, r.y, r.w, r.h); ctx.globalAlpha = 1;
    ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.strokeRect(r.x + .5, r.y + .5, r.w - 1, r.h - 1);
    if (it.kind === 'file' && SC.sel.has(it.rel)) { ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.strokeRect(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3); }
    if (r.w > 44 && r.h > 16) {
      ctx.fillStyle = '#fff'; ctx.font = '11px system-ui, sans-serif'; ctx.textBaseline = 'middle';
      ctx.shadowColor = 'rgba(0,0,0,.55)'; ctx.shadowBlur = 2;
      ctx.fillText(clip(ctx, it.name, r.w - 8), r.x + 4, r.y + (r.h > 30 ? 10 : r.h / 2));
      if (r.h > 30) ctx.fillText(fmtBytes(it.size), r.x + 4, r.y + 24);
      ctx.shadowBlur = 0;
    }
    MAP.rects.push({ ...r, it, depth, header: 0 });
  });
}
function clip(ctx, text, maxW) {
  if (ctx.measureText(text).width <= maxW) return text;
  let lo = 0, hi = text.length;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ctx.measureText(text.slice(0, mid) + '…').width <= maxW) lo = mid; else hi = mid - 1; }
  return lo ? text.slice(0, lo) + '…' : '';
}
function renderMap() {
  const node = SC.map || SC.node; if (!node) return;
  const canvas = $('#treemap'); const wrap = $('#mapWrap');
  const w = Math.max(200, wrap.clientWidth || 600); const h = Math.round(Math.min(600, Math.max(340, w * 0.62)));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  MAP.rects = []; MAP.w = w; MAP.h = h;
  // at the drive root the free space is part of the picture, as in the rings
  const showFree = SC.showFree && !SC.path && SC.capacity && SC.capacity > node.size;
  if (showFree) {
    const items = [{ kind: 'used', size: node.size }, { kind: 'free', size: SC.capacity - node.size, name: 'Free space' }];
    const rects = squarify(items, 0, 0, w, h);
    const used = rects[0], free = rects[1];
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--line').trim() || '#ddd';
    ctx.fillRect(free.x, free.y, free.w, free.h);
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#666';
    ctx.font = '12px system-ui, sans-serif'; ctx.textBaseline = 'middle';
    if (free.w > 60 && free.h > 20) ctx.fillText(clip(ctx, `Free space · ${fmtBytes(SC.capacity - node.size)}`, free.w - 10), free.x + 6, free.y + Math.min(free.h / 2, 14));
    MAP.rects.push({ ...free, it: { kind: 'free', name: 'Free space', size: SC.capacity - node.size }, depth: 0, header: 0 });
    drawTiles(ctx, node, used.x, used.y, used.w, used.h, 1, SC.path);
  } else drawTiles(ctx, node, 0, 0, w, h, 1, SC.path);
  $('#mapLegend').innerHTML = TYPE_GROUPS.map(([name, color]) => `<span><i style="background:${color}"></i>${esc(name)}</span>`).join('') +
    `<span><i style="background:${OTHER_COLOR}"></i>Other files</span><span><i class="dirsw"></i>Folder</span>`;
}
function mapHit(e) {
  const rect = $('#treemap').getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  let best = null;
  for (const r of MAP.rects) if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h && (!best || r.depth >= best.depth)) best = r;
  return best ? { hit: best, x, y } : null;
}
$('#treemap').addEventListener('mousemove', e => {
  const m = mapHit(e); if (!m || m.hit.it.kind === 'free') { tip.classList.add('hidden'); $('#treemap').style.cursor = 'default'; return; }
  const it = m.hit.it; const base = (SC.map || SC.node).size || 1;
  const where = it.rel ? it.rel.split('/').slice(0, -1).join('/') : '';
  tip.innerHTML = `<b>${esc(it.name)}</b>${where && where !== SC.path ? `<span class="soft">${esc(where)}</span><br>` : ''}${fmtBytes(it.size)} · ${(100 * it.size / base).toFixed(1)} % of this view${it.kind === 'dir' ? ` · ${(it.files || 0).toLocaleString()} files` : it.kind === 'file' ? ` · ${esc(groupOf(extOf(it.name)))}` : ''}${it.kind === 'dir' ? '<br><span class="soft">Click the name strip or double-click to open</span>' : ''}`;
  tip.style.left = Math.min(window.innerWidth - 330, e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 16) + 'px';
  tip.classList.remove('hidden');
  $('#treemap').style.cursor = it.kind === 'dir' || it.kind === 'file' ? 'pointer' : 'default';
});
$('#treemap').addEventListener('mouseleave', () => tip.classList.add('hidden'));
$('#treemap').addEventListener('click', e => {
  const m = mapHit(e); if (!m) return;
  const it = m.hit.it;
  if (it.kind === 'dir') {
    // a click on the name strip opens the folder; a click inside it is handled by the deeper tile
    const onHeader = !m.hit.header || m.y < m.hit.y + m.hit.header;
    if (onHeader) loadNode(it.rel);
    return;
  }
  if (it.kind === 'file') {
    const parent = it.rel.split('/').slice(0, -1).join('/');
    if (SC.sel.has(it.rel)) SC.sel.delete(it.rel); else SC.sel.add(it.rel);
    SC.known.set(it.rel, it.size);
    if (parent !== SC.path) loadNode(parent, true); else { renderList(); renderLargest(); }
  }
});
$('#treemap').addEventListener('dblclick', e => {
  const m = mapHit(e); if (!m) return;
  // the deepest folder under the pointer
  const rect = $('#treemap').getBoundingClientRect(); const x = e.clientX - rect.left, y = e.clientY - rect.top;
  let dir = null;
  for (const r of MAP.rects) if (r.it.kind === 'dir' && x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h && (!dir || r.depth >= dir.depth)) dir = r;
  if (dir) loadNode(dir.it.rel);
});
function setView(v) {
  SC.view = v;
  $$('#spcView button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
  $('.sunwrap').classList.toggle('hidden', v !== 'rings');
  $('#mapWrap').classList.toggle('hidden', v !== 'treemap');
  $('#spcLayout').classList.toggle('map', v === 'treemap');
  $('#spcUp').classList.toggle('hidden', v !== 'treemap');
  api('/api/settings', { patch: { spaceView: v } });
  if (v === 'treemap' && SC.node) { if (SC.map) renderMap(); else loadMap(); }
}
/* Free space is hidden unless ticked: it would otherwise dwarf what is actually on the drive. */
$('#spcFree').addEventListener('change', e => {
  SC.showFree = e.target.checked;
  api('/api/settings', { patch: { spaceShowFree: SC.showFree } });
  if (SC.node) { renderSunburst(); if (SC.view === 'treemap') renderMap(); }
});
$('#spcView').addEventListener('click', e => { const b = e.target.closest('button[data-view]'); if (b) setView(b.dataset.view); });
window.addEventListener('resize', () => { if (SC.view === 'treemap' && !$('#tab-space').classList.contains('hidden') && SC.node) renderMap(); });

/* --- file types + largest files ------------------------------------------------ */
function renderTypes() {
  const t = SC.types; if (!t) return;
  const base = t.total || 1;
  $('#spcTypesInfo').textContent = t.types.length ? `${fmtBytes(t.total)} in ${t.types.filter(x => x.ext !== null).length}${t.types.some(x => x.ext === null) ? '+' : ''} types${t.running ? ' · still scanning' : ''}` : '';
  $('#spcTypes tbody').innerHTML = t.types.map(r => {
    const ext = r.ext === null ? `${r.types} other types` : r.ext === '' ? '(no extension)' : '.' + r.ext;
    const color = r.ext ? (EXT_COLOR.get(r.ext) || OTHER_COLOR) : OTHER_COLOR;
    const grp = r.ext ? groupOf(r.ext) : '';
    return `<tr><td><span class="ico" style="color:${color}">■</span>${esc(ext)}${grp && grp !== 'Other' ? ` <span class="soft">${esc(grp)}</span>` : ''}</td>
      <td class="num">${fmtBytes(r.size)}</td>
      <td class="num">${(100 * r.size / base).toFixed(1)} %<span class="share"><i style="width:${Math.min(100, 100 * r.size / base).toFixed(1)}%;background:${color}"></i></span></td>
      <td class="num">${r.count.toLocaleString()}</td></tr>`;
  }).join('') || '<tr><td colspan="4" class="hint">No files yet.</td></tr>';
}
function renderLargest() {
  const l = SC.largest; if (!l) return;
  for (const f of l.files) SC.known.set(f.rel, f.size);
  $('#spcLargestInfo').textContent = l.files.length ? `top ${l.files.length}${l.running ? ' · still scanning' : ''}` : '';
  $('#spcLargest tbody').innerHTML = l.files.map(f => `<tr class="file ${SC.sel.has(f.rel) ? 'sel-row' : ''}">
      <td class="chkcol"><input type="checkbox" data-rel="${esc(f.rel)}" ${SC.sel.has(f.rel) ? 'checked' : ''}></td>
      <td class="name"><span class="ico" style="color:${colorFor(f.name)}">●</span>${esc(f.name)}<span class="soft path"> ${esc(f.rel.split('/').slice(0, -1).join('/'))}</span></td>
      <td class="num">${fmtBytes(f.size)}</td><td>${esc(fmtDay(f.mtime))}</td></tr>`).join('') || '<tr><td colspan="4" class="hint">No files yet.</td></tr>';
  paintSelButtons();
}
$('#spcLargest').addEventListener('change', e => {
  const cb = e.target.closest('input[type=checkbox]'); if (!cb) return;
  if (cb.checked) SC.sel.add(cb.dataset.rel); else SC.sel.delete(cb.dataset.rel);
  renderLargest(); renderList();
});
$('#spcLargest').addEventListener('click', e => {
  const td = e.target.closest('td.name'); if (!td) return;
  const rel = $('input', td.parentElement).dataset.rel;
  loadNode(rel.split('/').slice(0, -1).join('/'), true);
});
$('#spcTrash').addEventListener('click', () => removeSelected('trash'));
$('#spcDelete').addEventListener('click', () => removeSelected('permanent'));
$('#spcScan').addEventListener('click', startScan);
$('#spcStop').addEventListener('click', () => api('/api/space/stop', {}));
document.addEventListener('tab', e => {
  if (e.detail !== 'space') return;
  fillSpaceTargets();
  SC.showFree = !!S.settings?.spaceShowFree; $('#spcFree').checked = SC.showFree;
  if (S.settings?.spaceView && S.settings.spaceView !== SC.view) setView(S.settings.spaceView);
  else if (SC.view === 'treemap' && SC.node) renderMap();
  api('/api/space/events?since=0').then(ev => {
    if (ev.error || !ev.state) return;
    if (ev.state.running && !SC.scanning) { setScanning(true); SC.since = 0; pollScan(); }
    else if (ev.state.done && !SC.node && ev.state.root) { SC.root = ev.state.root; SC.total = ev.state.bytes; loadNode(''); }
  });
});
document.addEventListener('redraw', () => { if (!$('#tab-space').classList.contains('hidden') && !SC.scanning) fillSpaceTargets(); });
