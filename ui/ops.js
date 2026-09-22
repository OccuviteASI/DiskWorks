'use strict';
/* Operation dialogs, the pending queue (plan / preview), and Apply with live progress. */

const fsChoices = () => (S.fsinfo?.choices || []).map(id => ({ id, label: S.fsinfo.fs[id]?.label || id }));
const mibInput = (id, valueMiB, min, max) => `<input type="number" id="${id}" value="${Math.floor(valueMiB)}" min="${Math.ceil(min)}" max="${Math.floor(max)}" step="1">`;
/* Keep a MiB field inside [lo, hi] (whole MiB); returns the clamped value. */
function clampField(el, lo, hi) {
  let v = Math.floor(Number(el.value));
  if (!isFinite(v)) v = hi;
  if (v > hi) v = hi;
  if (v < lo) v = lo;
  el.value = v;
  return v;
}
const fsSelect = (id, cur) => `<select id="${id}">${fsChoices().map(f => `<option value="${f.id}" ${f.id === cur ? 'selected' : ''}>${esc(f.label)}</option>`).join('')}</select>`;
const letterSelect = (id, cur, allowNone = true) => {
  const used = new Set(); for (const d of S.inv.disks) for (const p of d.partitions) if (p.letter) used.add(p.letter);
  let html = `<select id="${id}">`;
  html += `<option value="auto" ${cur === 'auto' ? 'selected' : ''}>Next free letter</option>`;
  if (allowNone) html += `<option value="none" ${!cur || cur === 'none' ? 'selected' : ''}>No drive letter</option>`;
  for (const c of 'CDEFGHIJKLMNOPQRSTUVWXYZ') if (!used.has(c) || c === cur) html += `<option value="${c}" ${c === cur ? 'selected' : ''}>${c}:</option>`;
  return html + '</select>';
};
const FS_NOTES = {
  ntfs: 'Windows native. Files over 4 GB fine; Linux reads and writes it too.',
  exfat: 'Best for USB sticks shared between Windows, Linux and cameras. Cannot be resized later.',
  fat32: 'Works everywhere, but no file over 4 GB. Windows itself only formats FAT32 up to 32 GB.',
  fat16: 'Very old; only for tiny volumes.',
  refs: 'Windows Server / Pro for Workstations only. Cannot be shrunk.',
  ext4: 'Linux standard. Windows needs WSL or a driver to read it (see Access).',
  ext3: 'Older Linux filesystem.', ext2: 'Linux, no journal.',
  xfs: 'Linux, large files; can grow but never shrink.',
  btrfs: 'Linux, snapshots and checksums; grows and shrinks while mounted. Windows reads it with WinBtrfs.',
  f2fs: 'Linux, for flash media.', swap: 'Linux swap space, no files.',
};

/* ------------------------------------------------------------------------ */
/* Dialogs                                                                   */
/* ------------------------------------------------------------------------ */
window.openOpDialog = async function (op, sel) {
  const inv = invNow();
  const body = $('#opBody'), title = $('#opTitle'), err = $('#opError'), ok = $('#opOk');
  err.classList.add('hidden'); ok.textContent = 'Add to pending'; ok.disabled = false;
  let build = null;   // () => op dict or throws
  if (op === 'create') {
    const g = findGap(sel.id), d = diskOf(g);
    // Partitions start and end on 1 MiB boundaries; the usable maximum is the aligned span,
    // which is also what Windows' own "largest free extent" reports.
    const alignedEnd = Math.floor((g.start + g.size) / MiB) * MiB;
    const maxFor = beforeMiB => Math.floor((alignedEnd - Math.ceil((g.start + beforeMiB * MiB) / MiB) * MiB) / MiB);
    let maxMiB = maxFor(0);
    if (maxMiB < 4) { toast('This free space is too small for a partition (less than 4 MiB usable).', 4000); return; }
    title.textContent = `New partition in ${fmtBytes(g.size)} of unallocated space on ${d.name}`;
    body.innerHTML = `
      <div class="two">
        <div class="field"><label>Size (MiB)</label>${mibInput('opSize', maxMiB, 4, maxMiB)}<div class="hint" id="opSizeHint"></div></div>
        <div class="field"><label>Leave free before it (MiB)</label>${mibInput('opBefore', 0, 0, Math.max(0, maxMiB - 4))}<div class="hint">0 = start at the beginning of the free space</div></div>
      </div>
      <div class="field"><input type="range" id="opRange" min="4" max="${maxMiB}" value="${maxMiB}" step="1"></div>
      <div class="two">
        <div class="field"><label>File system</label>${fsSelect('opFs', isWin() ? 'ntfs' : 'ext4')}<div class="hint" id="opFsNote"></div></div>
        <div class="field"><label>Label</label><input type="text" id="opLabel" maxlength="32" placeholder="optional"></div>
      </div>
      ${isWin() ? `<div class="field"><label>Drive letter</label>${letterSelect('opLetter', 'auto')}</div>` : ''}
      <label class="chk"><input type="checkbox" id="opQuick" checked> Quick format (do not scan for bad sectors)</label>`;
    const sync = () => {
      const v = Math.min(maxMiB, Math.max(0, Math.floor(Number($('#opSize').value) || 0)));
      $('#opSizeHint').textContent = `${fmtBytes(v * MiB)} — at most ${maxMiB} MiB (${fmtBytes(maxMiB * MiB)}) fits here`;
      $('#opFsNote').textContent = FS_NOTES[$('#opFs').value] || '';
    };
    const applyMax = () => {
      const before = clampField($('#opBefore'), 0, Math.max(0, maxFor(0) - 4));
      maxMiB = maxFor(before);
      $('#opSize').max = maxMiB; $('#opRange').max = maxMiB;
      clampField($('#opSize'), 4, maxMiB);
      $('#opRange').value = $('#opSize').value;
      sync();
    };
    $('#opRange').oninput = () => { $('#opSize').value = $('#opRange').value; sync(); };
    $('#opSize').oninput = () => { const v = Number($('#opSize').value); if (isFinite(v)) $('#opRange').value = Math.min(maxMiB, Math.max(4, v)); sync(); };
    $('#opSize').onchange = () => { clampField($('#opSize'), 4, maxMiB); $('#opRange').value = $('#opSize').value; sync(); };
    $('#opBefore').onchange = applyMax; $('#opBefore').oninput = applyMax;
    $('#opFs').onchange = sync; sync();
    build = () => {
      const before = clampField($('#opBefore'), 0, Math.max(0, maxFor(0) - 4));
      maxMiB = maxFor(before);
      const sizeMiB = clampField($('#opSize'), 4, maxMiB);
      if (sizeMiB < 4) throw new Error('Size must be at least 4 MiB.');
      const letter = isWin() ? $('#opLetter').value : null;
      return { op: 'create', gap: g.id, disk: d.id, start: g.start + before * MiB, size: sizeMiB * MiB, fs: $('#opFs').value, label: $('#opLabel').value,
        letter: letter === 'none' ? null : letter, quick: $('#opQuick').checked };
    };
  } else if (op === 'format') {
    const p = findPart(sel.id);
    title.textContent = `Format ${partTitle(p)} (${fmtBytes(p.size)})`;
    body.innerHTML = `<div class="warn">Everything on this partition will be erased.</div>
      <div class="two" style="margin-top:12px">
        <div class="field"><label>File system</label>${fsSelect('opFs', p.fs && fsChoices().some(f => f.id === p.fs) ? p.fs : (isWin() ? 'ntfs' : 'ext4'))}<div class="hint" id="opFsNote"></div></div>
        <div class="field"><label>Label</label><input type="text" id="opLabel" maxlength="32" value="${esc(p.label || '')}"></div>
      </div>
      <label class="chk"><input type="checkbox" id="opQuick" checked> Quick format</label>`;
    const sync = () => { $('#opFsNote').textContent = FS_NOTES[$('#opFs').value] || ''; }; $('#opFs').onchange = sync; sync();
    build = () => ({ op: 'format', part: p.id, fs: $('#opFs').value, label: $('#opLabel').value, quick: $('#opQuick').checked });
  } else if (op === 'delete') {
    const p = findPart(sel.id);
    title.textContent = `Delete ${partTitle(p)}`;
    body.innerHTML = `<div class="warn">The partition and everything on it (${fmtBytes(p.size)}${p.fs ? ', ' + esc(fsName(p.fs)) : ''}) will be removed when you Apply.</div>`;
    ok.textContent = 'Add delete to pending';
    build = () => ({ op: 'delete', part: p.id });
  } else if (op === 'label') {
    const p = findPart(sel.id);
    title.textContent = `Label for ${partTitle(p)}`;
    body.innerHTML = `<div class="field"><label>New label</label><input type="text" id="opLabel" maxlength="32" value="${esc(p.label || '')}"></div>`;
    build = () => ({ op: 'label', part: p.id, label: $('#opLabel').value });
  } else if (op === 'letter') {
    const p = findPart(sel.id);
    title.textContent = `Drive letter for ${partTitle(p)}`;
    body.innerHTML = `<div class="field"><label>Drive letter</label>${letterSelect('opLetter', p.letter || 'none')}</div>`;
    build = () => { const v = $('#opLetter').value; return { op: 'letter', part: p.id, letter: v === 'none' ? null : v }; };
  } else if (op === 'check') {
    const p = findPart(sel.id);
    title.textContent = `Check ${partTitle(p)}`;
    body.innerHTML = `<p class="hint">Runs the filesystem checker. With repair on, problems are fixed where the tool can.${isWin() ? ' Windows may only be able to schedule the check for the next restart.' : ''}</p>
      <label class="chk"><input type="checkbox" id="opRepair"> Repair problems found</label>`;
    build = () => ({ op: 'check', part: p.id, repair: $('#opRepair').checked });
  } else if (op === 'resize') {
    const p = findPart(sel.id), d = diskOf(p);
    title.textContent = `Resize ${partTitle(p)}`;
    body.innerHTML = `<p class="hint" id="opProbe">Finding the smallest size the filesystem allows…</p>`;
    openDlg('#dlgOp');
    const pr = await api('/api/fs/probe', { part: p.id });
    if (pr.error) { body.innerHTML = `<div class="error">${esc(pr.error)}</div>`; ok.disabled = true; return; }
    const minMiB = Math.max(4, Math.ceil((pr.minSize || 0) / MiB)), maxMiB = Math.floor(pr.maxSize / MiB), curMiB = Math.round(p.size / MiB);
    body.innerHTML = `
      <div class="kv"><div class="k">Now</div><div class="v">${fmtBytes(p.size)}</div>
        <div class="k">Smallest</div><div class="v">${fmtBytes(minMiB * MiB)} ${pr.probed ? '<span class="soft">(measured)</span>' : '<span class="soft">(used space; Unlock for the exact value)</span>'}</div>
        <div class="k">Largest</div><div class="v">${fmtBytes(maxMiB * MiB)} <span class="soft">(free space to the right)</span></div></div>
      ${pr.note ? `<div class="note">${esc(pr.note)}</div>` : ''}
      <div class="field" style="margin-top:12px"><label>New size (MiB)</label>${mibInput('opSize', curMiB, minMiB, maxMiB)}<div class="hint" id="opSizeHint"></div></div>
      <input type="range" id="opRange" min="${minMiB}" max="${maxMiB}" value="${curMiB}" step="1">
      ${p.fs === 'ntfs' && !isWin() ? '<div class="note">After resizing NTFS from Linux, boot Windows twice so it runs its own checks.</div>' : ''}`;
    const sync = () => { const v = +$('#opSize').value; $('#opSizeHint').textContent = (v * MiB > p.size ? `Grow by ${fmtBytes(v * MiB - p.size)}` : v * MiB < p.size ? `Shrink by ${fmtBytes(p.size - v * MiB)}` : 'No change') + ` — between ${minMiB} and ${maxMiB} MiB`; };
    $('#opRange').oninput = () => { $('#opSize').value = $('#opRange').value; sync(); };
    $('#opSize').oninput = () => { const v = Number($('#opSize').value); if (isFinite(v)) $('#opRange').value = Math.min(maxMiB, Math.max(minMiB, v)); sync(); };
    $('#opSize').onchange = () => { clampField($('#opSize'), minMiB, maxMiB); $('#opRange').value = $('#opSize').value; sync(); };
    sync();
    if (minMiB >= maxMiB && minMiB >= curMiB) { $('#opBody').innerHTML += '<div class="warn">This partition cannot be resized: nothing to shrink and no free space to the right.</div>'; }
    build = () => {
      const v = +$('#opSize').value;
      if (v < minMiB || v > maxMiB) throw new Error(`Choose a size between ${minMiB} and ${maxMiB} MiB.`);
      if (v === curMiB) throw new Error('The size did not change.');
      return { op: 'resize', part: p.id, size: v * MiB, minSize: minMiB * MiB };
    };
  } else if (op === 'table') {
    const d = findDisk(sel.id);
    title.textContent = `New partition table on ${d.name}`;
    body.innerHTML = `${d.partitions.length ? `<div class="warn">${d.partitions.length} partition${d.partitions.length > 1 ? 's' : ''} and all their data will be removed.</div>` : ''}
      <div class="field" style="margin-top:12px"><label>Table type</label>
        <label class="chk"><input type="radio" name="opTable" value="gpt" checked> GPT — modern, any size, more than 4 partitions (recommended)</label><br>
        <label class="chk"><input type="radio" name="opTable" value="mbr"> MBR — old computers and some appliances; 2 TB and 4 primary partitions at most</label></div>`;
    build = () => ({ op: 'table', disk: d.id, table: $('input[name=opTable]:checked').value });
  } else if (op === 'wipe') {
    const d = findDisk(sel.id);
    title.textContent = `Wipe ${d.name}`;
    body.innerHTML = `<div class="warn">Removes the partition table and every partition on ${esc(d.model || d.name)} (${fmtBytes(d.size)}). Data is not overwritten unless you tick the box, but Windows and Linux will see an empty disk.</div>
      <label class="chk" style="margin-top:10px"><input type="checkbox" id="opZero"> Also zero the first and last MiB (kills leftover filesystem signatures)</label>`;
    build = () => ({ op: 'wipe', disk: d.id, zero: $('#opZero').checked });
  } else { toast('Not available yet'); return; }
  openDlg('#dlgOp');
  ok.onclick = async () => {
    let o;
    try { o = build(); } catch (e) { err.textContent = e.message; err.classList.remove('hidden'); return; }
    S.pending.push(o);
    const r = await replan();
    if (r && r.errors?.length && r.errors[r.errors.length - 1].op === S.pending.length - 1) {
      S.pending.pop(); await replan();
      err.textContent = r.errors[r.errors.length - 1].message; err.classList.remove('hidden');
      return;
    }
    closeDlg('#dlgOp');
    toast('Added to pending operations');
  };
};

/* ------------------------------------------------------------------------ */
/* Planning                                                                  */
/* ------------------------------------------------------------------------ */
window.replan = async function () {
  if (!S.pending.length) { S.preview = null; S.plan = null; render(); return null; }
  const r = await api('/api/ops/plan', { ops: S.pending, hash: S.inv?.hash });
  if (r.error) { toast(r.error, 4000); return r; }
  S.plan = r;
  S.preview = r.preview;
  if (!r.hashOk) toast('The disks changed; the queue was re-checked against the new layout.', 4000);
  render();
  return r;
};

function pendingText(i) {
  const t = S.plan?.texts?.[i];
  const err = S.plan?.errors?.find(e => e.op === i);
  return (t || S.pending[i].op) + (err ? ` — <span class="bad">${esc(err.message)}</span>` : '');
}
const _renderPending = renderPending;
renderPending = function () {
  const n = S.pending.length;
  $('#pendingCount').textContent = n ? `(${n})` : '';
  const bad = !!(S.plan?.errors?.length);
  $('#btnApply').disabled = !n || bad || !S.status?.features?.ops;
  $('#btnClearOps').disabled = !n; $('#btnPreviewCmds').disabled = !n || !S.plan;
  $('#pendingList').innerHTML = S.pending.map((o, i) => `<li>${pendingText(i)} <button class="btn small rm" data-i="${i}" title="Remove">✕</button></li>`).join('');
  $$('#tabs button').forEach(b => { if (b.dataset.tab === 'disks') b.innerHTML = 'Disks' + (n ? `<span class="badge">${n}</span>` : ''); });
  if (S.plan?.warnings?.length) $('#pendingHint').textContent = S.plan.warnings.join(' ');
  else $('#pendingHint').textContent = 'Nothing is changed on the disk until you press Apply. Queue as many changes as you like; the bars above preview the result.';
};
$('#pendingList').addEventListener('click', async e => {
  const b = e.target.closest('button.rm'); if (!b) return;
  S.pending.splice(+b.dataset.i, 1); await replan();
});
$('#btnClearOps').addEventListener('click', async () => { S.pending = []; await replan(); });
$('#btnPreviewCmds').addEventListener('click', () => {
  if (!S.plan) return;
  const text = S.plan.steps.map(s => `# ${s.n}. ${s.title}\n${s.cmd}`).join('\n\n');
  showText('Commands that Apply will run', text || '(nothing)');
});

/* ------------------------------------------------------------------------ */
/* Apply                                                                     */
/* ------------------------------------------------------------------------ */
let applySeq = 0, applyTimer = null;
$('#btnApply').addEventListener('click', async () => {
  if (!S.plan || !S.pending.length) return;
  if (S.status?.helper?.state !== 'ready') {
    toast('Unlock first: changing disks needs administrator rights.', 4000);
    $('#helperPill').click();
    return;
  }
  let confirm = false;
  if (S.plan.destructive) {
    const disks = S.plan.touched.map(id => findDisk(id, S.inv)).filter(Boolean);
    const expected = disks.length === 1 ? (disks[0].model || disks[0].name) : 'erase';
    const list = S.plan.texts.map(t => `<li>${esc(t)}</li>`).join('');
    confirm = await confirmTyped('This will destroy data', `<p>These operations delete or overwrite data on <b>${esc(disks.map(d => d.name + ' (' + (d.model || '') + ', ' + fmtBytes(d.size) + ')').join(', '))}</b>:</p><ul>${list}</ul>`, expected, 'Apply now');
    if (!confirm) return;
  }
  const r = await api('/api/ops/apply', { ops: S.pending, hash: S.inv.hash, confirm });
  if (r.error) { toast(r.error, 5000); return; }
  applySeq = 0;
  $('#applyPanel').classList.remove('hidden'); $('#btnApplyClose').classList.add('hidden'); $('#btnApplyCancel').classList.remove('hidden');
  $('#applySteps').innerHTML = S.plan.steps.map(s => `<li id="step-${s.n}"><span class="st">${esc(s.title)}</span> <span class="soft" id="stepmsg-${s.n}"></span></li>`).join('');
  $('#applyBar').style.width = '0%'; $('#applyMsg').textContent = 'Starting…';
  $('#btnApply').disabled = true; $('#btnClearOps').disabled = true;
  clearInterval(applyTimer); applyTimer = setInterval(pollApply, 500);
});
$('#btnApplyCancel').addEventListener('click', () => api('/api/ops/cancel', {}));
$('#btnApplyClose').addEventListener('click', () => $('#applyPanel').classList.add('hidden'));

async function pollApply() {
  const r = await api(`/api/ops/events?since=${applySeq}`);
  if (r.error || !r.events) return;
  const total = S.plan?.steps?.length || 1;
  for (const e of r.events) {
    applySeq = e.seq;
    if (e.type === 'step') {
      const li = $(`#step-${e.n}`); if (!li) continue;
      li.className = e.state;
      if (e.state === 'run') { $('#applyMsg').textContent = `Step ${e.n} of ${total}: ${e.title}`; $('#applyBar').style.width = `${Math.round(100 * (e.n - 1) / total)}%`; }
      if (e.state === 'done') $(`#stepmsg-${e.n}`).textContent = `done in ${(e.ms / 1000).toFixed(1)} s`;
      if (e.state === 'fail') { $(`#stepmsg-${e.n}`).textContent = e.message; li.className = 'fail'; }
    } else if (e.type === 'progress') {
      if (e.percent != null) $('#applyMsg').textContent = `Step ${e.n} of ${total}: ${Math.round(e.percent)} %${e.message ? ' · ' + e.message : ''}`;
    } else if (e.type === 'log' && e.output && e.code) {
      const li = $(`#step-${e.n}`); if (li && !$('pre', li)) li.insertAdjacentHTML('beforeend', `<pre>${esc(e.output)}</pre>`);
    } else if (e.type === 'error') {
      $('#applyMsg').textContent = 'Stopped: ' + e.message;
    } else if (e.type === 'done') {
      clearInterval(applyTimer);
      $('#applyBar').style.width = '100%';
      if (e.ok) $('#applyMsg').textContent = 'All operations finished.';
      $('#btnApplyCancel').classList.add('hidden'); $('#btnApplyClose').classList.remove('hidden');
      S.pending = []; S.preview = null; S.plan = null;
      await loadInventory(true);
      toast(e.ok ? 'Done' : 'Stopped with an error', 3000);
    }
  }
}
