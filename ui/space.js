'use strict';
/* Space tab: DaisyDisk-style rings of what uses a drive, with trash / delete. */

const SC = { root: null, path: '', node: null, sel: new Set(), scanning: false, since: 0, timer: null, total: 0, capacity: null, hover: null };
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
  SC.root = r.root; SC.path = ''; SC.node = null; SC.sel.clear(); SC.since = 0;
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
async function loadNode(rel) {
  const r = await api(`/api/space/tree?path=${encodeURIComponent(rel)}&depth=2`);
  if (r.error) { toast(r.error, 5000); return; }
  SC.path = rel; SC.node = r.node; SC.sel.clear();
  $('#spcResult').classList.remove('hidden');
  renderCrumb(); renderSunburst(); renderList();
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
  const showFree = !SC.path && SC.capacity && SC.capacity > node.size;
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
  svg.innerHTML = paths.join('');
  $('#sunCenter').innerHTML = `<b title="${esc(SC.root + (SC.path ? '/' + SC.path : ''))}">${esc(SC.path ? SC.path.split('/').pop() : SC.root)}</b>${fmtBytes(node.size)}<br>${(node.files || 0).toLocaleString()} files`;
  svg._items = items;
}
function itemAt(pathEl) {
  const items = $('#sunburst')._items || [];
  const it = items[+pathEl.dataset.i]; if (!it) return null;
  if (pathEl.dataset.ring === '2' && it.node) { const inner = slices(it.node)[+pathEl.dataset.j]; return inner ? { ...inner, parent: it } : it; }
  return it;
}
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
  const p = e.target.closest('path'); if (!p || p.dataset.free) return;
  const it = itemAt(p); if (!it) return;
  if (it.kind === 'dir') loadNode(it.rel);
  else if (it.kind === 'file') { SC.sel.clear(); SC.sel.add(it.rel); renderList(); }
});

function renderList() {
  const node = SC.node; const items = slices(node); const base = node.size || 1;
  $('#spcList tbody').innerHTML = items.map((it, i) => {
    const selectable = it.kind === 'dir' || it.kind === 'file';
    return `<tr class="${it.kind === 'dir' ? 'folder' : 'file'} ${SC.sel.has(it.rel) ? 'sel-row' : ''}" data-i="${i}">
      <td class="chkcol">${selectable ? `<input type="checkbox" data-rel="${esc(it.rel)}" ${SC.sel.has(it.rel) ? 'checked' : ''}>` : ''}</td>
      <td class="name" data-i="${i}"><span class="ico" style="color:${it.kind === 'dir' || it.kind === 'file' ? hue(i, items.length) : 'var(--soft)'}">${it.kind === 'dir' ? '▸' : '●'}</span>${esc(it.name)}</td>
      <td class="num">${fmtBytes(it.size)}</td>
      <td class="num">${(100 * it.size / base).toFixed(1)} %<span class="share"><i style="width:${Math.min(100, 100 * it.size / base).toFixed(1)}%"></i></span></td>
      <td class="num">${it.kind === 'dir' ? `${(it.files || 0).toLocaleString()} files${it.dirs ? `, ${it.dirs.toLocaleString()} folders` : ''}` : it.kind === 'file' ? esc(fmtDay(it.mtime)) : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="hint">This folder is empty.</td></tr>';
  const n = SC.sel.size;
  $('#spcTrash').disabled = !n; $('#spcDelete').disabled = !n; $('#spcReveal').disabled = n !== 1;
  $('#spcTrash').textContent = n ? `Move ${n} to ${S.status?.platform === 'win32' ? 'Recycle Bin' : 'Trash'}` : `Move to ${S.status?.platform === 'win32' ? 'Recycle Bin' : 'Trash'}`;
  $('#spcAll').checked = n > 0 && items.filter(x => x.kind === 'dir' || x.kind === 'file').every(x => SC.sel.has(x.rel));
}
$('#spcList').addEventListener('change', e => {
  const cb = e.target.closest('input[type=checkbox]'); if (!cb) return;
  if (cb.checked) SC.sel.add(cb.dataset.rel); else SC.sel.delete(cb.dataset.rel);
  renderList();
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
$('#spcUp').addEventListener('click', () => loadNode(SC.path.split('/').slice(0, -1).join('/')));
$('#spcReveal').addEventListener('click', () => { const rel = [...SC.sel][0]; if (rel != null) api('/api/space/reveal', { path: rel }).then(r => { if (r.error) toast(r.error, 5000); }); });

async function removeSelected(mode) {
  const rels = [...SC.sel]; if (!rels.length) return;
  const bytes = slices(SC.node).filter(x => SC.sel.has(x.rel)).reduce((a, x) => a + x.size, 0);
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
$('#spcTrash').addEventListener('click', () => removeSelected('trash'));
$('#spcDelete').addEventListener('click', () => removeSelected('permanent'));
$('#spcScan').addEventListener('click', startScan);
$('#spcStop').addEventListener('click', () => api('/api/space/stop', {}));
document.addEventListener('tab', e => {
  if (e.detail !== 'space') return;
  fillSpaceTargets();
  api('/api/space/events?since=0').then(ev => {
    if (ev.error || !ev.state) return;
    if (ev.state.running && !SC.scanning) { setScanning(true); SC.since = 0; pollScan(); }
    else if (ev.state.done && !SC.node && ev.state.root) { SC.root = ev.state.root; SC.total = ev.state.bytes; loadNode(''); }
  });
});
document.addEventListener('redraw', () => { if (!$('#tab-space').classList.contains('hidden') && !SC.scanning) fillSpaceTargets(); });
