'use strict';
/* Image tab: write an image / hybrid ISO to a drive, back a drive or partition up to a
   compressed image with a manifest, restore (= write with checks), verify. */

const IM = { mode: 'write', targets: [], hash: null, path: '', info: null, disk: null, part: null, dest: '',
  showAll: false, verify: true, compress: 'zstd', seq: 0, timer: null, running: false };

const slug = s => String(s || 'disk').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'disk';
const stamp = () => { const d = new Date(); const p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`; };
const fmtSpeed = b => b ? fmtBytes(b) + '/s' : '';
const fmtEta = s => (s == null || !isFinite(s)) ? '' : s < 60 ? `${Math.round(s)} s left` : s < 3600 ? `${Math.round(s / 60)} min left` : `${(s / 3600).toFixed(1)} h left`;

async function loadTargets() {
  const r = await api('/api/image/targets');
  if (r.error) return;
  IM.targets = r.targets; IM.hash = r.hash;
  if (IM.disk && !IM.targets.some(t => t.id === IM.disk)) IM.disk = null;
}

function targetRows(opts = {}) {
  const list = IM.targets.filter(t => IM.showAll || t.removable || opts.all);
  if (!list.length) return `<p class="hint">No removable drives found. Plug in a USB stick, or tick "Show all disks".</p>`;
  return `<div class="targets">${list.map(t => {
    const dis = t.system && !opts.readOnly;
    return `<label class="target ${dis ? 'dis' : ''} ${IM.disk === t.id ? 'sel' : ''}">
      <input type="radio" name="imgTarget" value="${esc(t.id)}" ${IM.disk === t.id ? 'checked' : ''} ${dis ? 'disabled' : ''}>
      <span class="tmain"><b>${esc(t.name)} · ${esc(t.model || 'disk')}</b>
        <span class="hint">${fmtBytes(t.size)} · ${esc(t.bus || '')}${t.removable ? ' · removable' : ''}${t.serial ? ' · S/N ' + esc(t.serial) : ''}${t.letters?.length ? ' · ' + esc(t.letters.join(' ')) : ''}</span>
        ${t.system ? '<span class="hint" style="color:var(--warn)">Holds the running operating system — never written</span>' : ''}
        ${!t.removable && !t.system ? '<span class="hint" style="color:var(--warn)">Internal disk: check twice</span>' : ''}
      </span></label>`; }).join('')}</div>
    <label class="chk" style="margin-top:8px"><input type="checkbox" id="imgShowAll" ${IM.showAll ? 'checked' : ''}> Show all disks (internal drives too)</label>`;
}

function infoCard(info) {
  if (!info) return '';
  const rows = [
    ['File', esc(info.name)], ['File size', fmtBytes(info.fileSize)],
    ['Image size', info.rawSize != null ? fmtBytes(info.rawSize) + (info.rawSizeEstimate ? ' <span class="soft">(estimate)</span>' : '') : 'unknown until written'],
    ['Compression', info.compression || 'none'],
    ['Kind', { raw: 'Raw disk image', iso: 'ISO image', 'windows-iso': 'Windows installation ISO', vhd: 'Fixed VHD', 'vhd-dynamic': 'Dynamic VHD' }[info.kind] || info.kind],
    ['Boots from USB', info.iso ? (info.hybrid ? 'Yes (hybrid ISO)' : 'No') : (info.table !== 'none' ? 'Has a partition table' : 'Unknown')],
  ];
  if (info.volumeId) rows.push(['Volume name', esc(info.volumeId)]);
  if (info.manifest) {
    const s = info.manifest.source || {};
    rows.push(['Backup of', `${esc(s.model || s.disk || '')}${s.partition ? ` · partition ${s.partition.number} (${esc(s.partition.fs || '')})` : ' · whole disk'} · ${fmtBytes(info.manifest.image?.rawSize)} · ${esc(info.manifest.created || '')}`]);
  }
  return `<div class="kv" style="margin-top:10px">${rows.map(([k, v]) => `<div class="k">${k}</div><div class="v">${v}</div>`).join('')}</div>` +
    (info.notes || []).map(n => `<div class="${/NOT boot|not in this build|not a disk image|dynamic/.test(n) ? 'warn' : 'note'}">${esc(n)}</div>`).join('');
}

function renderImage() {
  const body = $('#imgBody');
  if (!S.status?.features?.image) { body.innerHTML = '<p class="hint">Imaging is being wired up in this build.</p>'; return; }
  const manual = !S.windowed;
  const pathField = (id, label, val, hint) => `<div class="field"><label>${label}</label><div class="row"><input type="text" id="${id}" value="${esc(val)}" placeholder="${manual ? 'Type the full path' : 'Choose a file…'}" spellcheck="false"><button class="btn" id="${id}Browse">Browse…</button></div>${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
  let html = '';
  if (IM.mode === 'write' || IM.mode === 'restore') {
    html += pathField('imgPath', IM.mode === 'write' ? 'Image or ISO file' : 'Backup image (.img, .img.zst) or any raw image', IM.path,
      IM.mode === 'write' ? 'ISO, IMG, RAW, DD, fixed VHD; compressed as .gz, .xz, .zst, .bz2' : 'The .json manifest beside a DiskWorks backup is read automatically.');
    html += `<div id="imgInfo">${infoCard(IM.info)}</div>`;
    html += `<h3 class="sub">Write to</h3>${targetRows()}`;
    html += `<label class="chk" style="margin-top:8px"><input type="checkbox" id="imgVerify" ${IM.verify ? 'checked' : ''}> Read everything back afterwards and verify</label>`;
    html += `<div class="row" style="margin-top:14px"><button class="btn danger" id="imgGo" ${IM.path && IM.disk && !IM.running ? '' : 'disabled'}>${IM.mode === 'write' ? 'Write image to drive…' : 'Restore to drive…'}</button><span class="hint">Everything on the drive is replaced.</span></div>`;
  } else {
    const all = IM.targets;
    html += `<div class="field"><label>What to back up</label><select id="imgSrc">${all.map(t => `<option value="${esc(t.id)}|" ${IM.disk === t.id && !IM.part ? 'selected' : ''}>${esc(t.name)} · ${esc(t.model || '')} · whole disk · ${fmtBytes(t.size)}</option>` +
      t.partitions.map(p => `<option value="${esc(t.id)}|${esc(p.id)}" ${IM.part === p.id ? 'selected' : ''}>&nbsp;&nbsp;↳ partition ${p.number}${p.letter ? ' (' + p.letter + ':)' : ''} · ${esc(p.title || '')} · ${esc(fsName(p.fs) || '')} · ${fmtBytes(p.size)}</option>`).join('')).join('')}</select></div>`;
    html += pathField('imgDest', 'Save as', IM.dest, 'A .json manifest with the checksum and the layout is written next to it.');
    html += `<div class="field"><label>Compression</label><label class="chk"><input type="radio" name="imgComp" value="zstd" ${IM.compress === 'zstd' ? 'checked' : ''}> zstd (.img.zst) — smaller, still fast</label> &nbsp; <label class="chk"><input type="radio" name="imgComp" value="none" ${IM.compress === 'none' ? 'checked' : ''}> none (.img) — sparse raw image, mountable by other tools</label></div>`;
    html += `<div class="row" style="margin-top:14px"><button class="btn primary" id="imgGo" ${IM.dest && IM.disk && !IM.running ? '' : 'disabled'}>Start backup</button><span class="hint">Reading only; the source is not changed.</span></div>`;
  }
  html += `<div id="imgProgress" class="${IM.running || IM.result ? '' : 'hidden'}"><div class="progress"><div class="bar" id="imgBar"></div><span class="msg" id="imgMsg"></span></div><div id="imgNotes" class="hint"></div><div id="imgResult"></div><div class="row" style="margin-top:8px"><button class="btn" id="imgCancel" ${IM.running ? '' : 'disabled'}>Cancel</button></div></div>`;
  body.innerHTML = html;
  wireImage(manual);
  if (IM.result) paintResult(IM.result);
}

function wireImage(manual) {
  const pathInput = $('#imgPath'), destInput = $('#imgDest');
  $('#imgShowAll')?.addEventListener('change', e => { IM.showAll = e.target.checked; renderImage(); });
  $$('input[name=imgTarget]').forEach(r => r.addEventListener('change', () => { IM.disk = r.value; renderImage(); }));
  $$('input[name=imgComp]').forEach(r => r.addEventListener('change', () => { IM.compress = r.value; IM.dest = IM.dest.replace(/\.img(\.zst)?$/, IM.compress === 'zstd' ? '.img.zst' : '.img'); renderImage(); }));
  $('#imgVerify')?.addEventListener('change', e => { IM.verify = e.target.checked; });
  $('#imgSrc')?.addEventListener('change', e => { const [d, p] = e.target.value.split('|'); IM.disk = d; IM.part = p || null; IM.dest = defaultDest(); renderImage(); });
  if (pathInput) {
    pathInput.addEventListener('change', () => inspectPath(pathInput.value.trim()));
    pathInput.addEventListener('keydown', e => { if (e.key === 'Enter') inspectPath(pathInput.value.trim()); });
    $('#imgPathBrowse').addEventListener('click', async () => {
      const r = await api('/api/image/pick', { kind: 'open' });
      if (r.error) { toast(r.error, 4000); return; }
      if (r.manual) { toast('Type the full path of the file, then press Enter'); pathInput.focus(); return; }
      if (r.path) inspectPath(r.path);
    });
  }
  if (destInput) {
    destInput.addEventListener('change', () => { IM.dest = destInput.value.trim(); renderImage(); });
    $('#imgDestBrowse').addEventListener('click', async () => {
      const r = await api('/api/image/pick', { kind: 'save', name: defaultDest().split(/[\\/]/).pop() });
      if (r.error) { toast(r.error, 4000); return; }
      if (r.manual) { toast('Type the full path to save to'); destInput.focus(); return; }
      if (r.path) { IM.dest = r.path; renderImage(); }
    });
    if (!IM.disk && IM.targets.length) { IM.disk = IM.targets[0].id; IM.part = null; }
    if (!IM.dest && IM.disk) { IM.dest = defaultDest(); renderImage(); return; }
  }
  $('#imgGo')?.addEventListener('click', startImageJob);
  $('#imgCancel')?.addEventListener('click', () => api('/api/image/cancel', {}));
}

function defaultDest() {
  const t = IM.targets.find(x => x.id === IM.disk); if (!t) return '';
  const p = IM.part ? t.partitions.find(x => x.id === IM.part) : null;
  // keep the folder of the previous choice (if it had one); a bare file name has no folder
  const m = (IM.dest || '').match(/^(.*[\\/])[^\\/]*$/);
  const dir = m ? m[1] : '';
  const name = `${slug(t.model || t.name)}${p ? '-p' + p.number : ''}-${stamp()}${IM.compress === 'zstd' ? '.img.zst' : '.img'}`;
  return dir + name;
}

async function inspectPath(path) {
  IM.path = path; IM.info = null; IM.result = null;
  if (!path) { renderImage(); return; }
  const r = await api('/api/image/inspect', { path });
  if (r.error) { toast(r.error, 4000); IM.info = { name: path, notes: [r.error], fileSize: 0 }; renderImage(); return; }
  IM.info = r;
  renderImage();
}

async function startImageJob() {
  const t = IM.targets.find(x => x.id === IM.disk);
  if (!t) return;
  if (S.status?.helper?.state !== 'ready') { toast('Unlock first: imaging needs administrator rights.', 4000); $('#helperPill').click(); return; }
  let body, route;
  if (IM.mode === 'backup') {
    route = '/api/image/backup';
    body = { disk: IM.disk, part: IM.part || undefined, dest: IM.dest, compress: IM.compress, level: 3 };
  } else {
    const info = IM.info || {};
    if (info.rawSize && info.rawSize > t.size && !info.rawSizeEstimate) { toast(`The image (${fmtBytes(info.rawSize)}) is larger than the drive (${fmtBytes(t.size)}).`, 5000); return; }
    const ok = await confirmTyped(`Erase ${t.name} and write the image?`,
      `<p><b>${esc(t.model || t.name)}</b> · ${fmtBytes(t.size)}${t.letters?.length ? ' · ' + esc(t.letters.join(' ')) : ''}</p><p>Everything on it will be replaced by <b>${esc(info.name || IM.path)}</b>${info.rawSize ? ' (' + fmtBytes(info.rawSize) + ')' : ''}.</p>` +
      (info.notes || []).filter(n => /NOT boot|not in this build/.test(n)).map(n => `<div class="warn">${esc(n)}</div>`).join(''),
      t.model || t.name, 'Write now');
    if (!ok) return;
    route = '/api/image/write';
    body = { path: IM.path, disk: IM.disk, verify: IM.verify, confirm: true };
  }
  const r = await api(route, body);
  if (r.error) { toast(r.error, 6000); return; }
  IM.running = true; IM.result = null; IM.seq = 0;
  renderImage();
  clearInterval(IM.timer); IM.timer = setInterval(pollImage, 400);
}

async function pollImage() {
  const r = await api(`/api/image/events?since=${IM.seq}`);
  if (r.error || !r.events) return;
  for (const e of r.events) {
    IM.seq = e.seq;
    if (e.type === 'progress') {
      const pct = e.total ? Math.min(100, 100 * e.bytes / e.total) : null;
      $('#imgBar').style.width = (pct ?? 30) + '%';
      const ph = { write: 'Writing', verify: 'Verifying', read: 'Reading' }[e.phase] || e.phase;
      $('#imgMsg').textContent = `${ph} · ${fmtBytes(e.bytes)}${e.total ? ' of ' + fmtBytes(e.total) : ''}${pct != null ? ' · ' + Math.round(pct) + ' %' : ''} · ${fmtSpeed(e.speed)} ${fmtEta(e.eta)}`;
    } else if (e.type === 'note') {
      $('#imgNotes').textContent = e.msg;
    } else if (e.type === 'done') {
      clearInterval(IM.timer); IM.running = false;
      IM.result = e;
      $('#imgCancel').disabled = true;
      paintResult(e);
      loadTargets();
    }
  }
}

function paintResult(e) {
  const box = $('#imgResult'); if (!box) return;
  $('#imgProgress').classList.remove('hidden');
  if (!e.ok) { $('#imgBar').style.width = '100%'; $('#imgMsg').textContent = 'Stopped'; box.innerHTML = `<div class="error">${esc(e.error)}</div>`; return; }
  const r = e.result || {};
  $('#imgBar').style.width = '100%';
  const t = IM.targets.find(x => x.id === IM.disk);
  if (IM.mode === 'backup') {
    $('#imgMsg').textContent = 'Backup finished';
    box.innerHTML = `<div class="note">Read ${fmtBytes(r.read)} in ${(r.elapsed || 0).toFixed(0)} s → ${fmtBytes(r.fileSize)} on disk${r.zeroBytes ? ` (${fmtBytes(r.zeroBytes)} of empty space skipped)` : ''}.<br>SHA-256 <code>${esc(r.sha256)}</code><br>Manifest: <code>${esc(r.manifest)}</code></div>`;
  } else {
    $('#imgMsg').textContent = r.verified === false ? 'Written, but verification found differences' : 'Written' + (r.verified ? ' and verified' : '');
    const left = t ? t.size - r.written : 0;
    box.innerHTML = `<div class="${r.verified === false ? 'error' : 'note'}">Wrote ${fmtBytes(r.written)} in ${(r.elapsed || 0).toFixed(0)} s. SHA-256 <code>${esc(r.sha256)}</code>${r.verified ? ' — read back and matched.' : r.verified === false ? ` — ${r.mismatches.length} block(s) differ, first at ${fmtBytes(r.mismatches[0])}. The drive may be faulty.` : ''}</div>` +
      (left > 4 * MiB ? `<div class="hint" style="margin-top:6px">${fmtBytes(left)} of the drive is beyond the image and stays unallocated. To use the whole drive again later, wipe it from the Disks tab and create a new partition.</div>` : '');
  }
}

/* Entry point for the Disks tab's right-click menu / Actions strip. */
window.openImageFor = async function (mode, diskId, partId) {
  IM.mode = mode; IM.result = null; IM.info = null; IM.path = '';
  IM.disk = diskId; IM.part = partId || null; IM.dest = '';
  const r = $(`input[name=imgmode][value=${mode}]`); if (r) r.checked = true;
  showTab('image');                  // the tab handler loads targets and renders
  await loadTargets();
  IM.disk = diskId; IM.part = partId || null;
  if (mode === 'backup') IM.dest = defaultDest();
  renderImage();
};

/* wiring */
$$('input[name=imgmode]').forEach(r => r.addEventListener('change', async () => { IM.mode = r.value; IM.result = null; IM.info = null; IM.path = ''; if (IM.mode === 'backup' && !IM.dest && IM.disk) IM.dest = defaultDest(); await loadTargets(); renderImage(); }));
document.addEventListener('tab', async e => { if (e.detail === 'image') { await loadTargets(); renderImage(); } });
document.addEventListener('status', e => { if (!$('#tab-image').classList.contains('hidden') && !IM.targets.length) { loadTargets().then(renderImage); } });
