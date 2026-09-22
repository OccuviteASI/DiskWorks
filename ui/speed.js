'use strict';
/* Speed tab: Blackmagic-style sequential write/read test with two dial gauges. */

const SP = { targets: [], running: false, since: 0, timer: null, scale: 500, runs: [], live: { write: null, read: null }, last: { write: null, read: null } };

/* Common video formats and what they need, in MB/s (decimal), one stream. */
const WILL = [
  ['H.264 1080p 30 (50 Mb/s)', 6.3], ['ProRes 422 1080p 30', 15.3], ['ProRes 422 HQ 1080p 30', 27.5],
  ['H.265 4K 60 (200 Mb/s)', 25], ['ProRes 422 HQ 4K 30', 110], ['ProRes 4444 4K 30', 165],
  ['BRAW 8:1 4K 60', 115], ['ProRes 422 HQ 4K 60', 220], ['ProRes 4444 XQ 4K 60', 495],
  ['Uncompressed 10-bit 4K 30', 995], ['ProRes RAW HQ 8K 30', 1790],
];

function fillSpeedTargets() {
  api('/api/speed/targets').then(r => {
    if (r.error) return;
    SP.targets = r.targets || [];
    const sel = $('#spTarget'); const cur = sel.value;
    sel.innerHTML = SP.targets.map(t => `<option value="${esc(t.root)}">${esc(t.root)} ${esc(t.title ? '· ' + t.title : '')} · ${esc(t.disk)}${t.model ? ' · ' + esc(t.model) : ''} · ${fmtBytes(t.free)} free${t.system ? ' · system' : ''}</option>`).join('')
      || '<option value="">No mounted drive with free space</option>';
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
    else { const ext = SP.targets.find(t => !t.system); if (ext) sel.value = ext.root; }
  });
}

/* --- dial gauge ---------------------------------------------------------- */
function arcPath(cx, cy, r, a0, a1) {
  const p = a => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}
const A0 = Math.PI * 1.0, A1 = Math.PI * 2.0;   // half circle, left to right
function buildGauge(el) {
  const svg = $('svg', el);
  const ticks = [];
  for (let i = 0; i <= 10; i++) {
    const a = A0 + (A1 - A0) * i / 10;
    const r0 = 78, r1 = i % 5 === 0 ? 68 : 72;
    ticks.push(`<line class="tick" x1="${(100 + r0 * Math.cos(a)).toFixed(1)}" y1="${(100 + r0 * Math.sin(a)).toFixed(1)}" x2="${(100 + r1 * Math.cos(a)).toFixed(1)}" y2="${(100 + r1 * Math.sin(a)).toFixed(1)}"/>`);
    if (i % 5 === 0) ticks.push(`<text class="ticklbl" data-i="${i}" x="${(100 + 58 * Math.cos(a)).toFixed(1)}" y="${(100 + 58 * Math.sin(a) + 3).toFixed(1)}"></text>`);
  }
  svg.innerHTML = `<path class="arc-bg" d="${arcPath(100, 100, 88, A0, A1)}"/><path class="arc-fg" d="${arcPath(100, 100, 88, A0, A1)}"/>${ticks.join('')}
    <line class="needle" x1="100" y1="100" x2="100" y2="24"/><circle class="hub" cx="100" cy="100" r="4"/>`;
  el._fg = $('.arc-fg', svg); el._needle = $('.needle', svg);
  el._len = Math.PI * 88;
  el._fg.style.strokeDasharray = `0 ${el._len}`;
}
function setGauge(el, mbps, sub) {
  const v = mbps == null ? 0 : Math.max(0, Math.min(1, mbps / SP.scale));
  el._fg.style.strokeDasharray = `${(v * el._len).toFixed(1)} ${el._len}`;
  el._fg.style.visibility = v > 0.002 ? '' : 'hidden';   // a zero-length dash with round caps would draw a dot
  el._needle.style.transform = `rotate(${(-90 + 180 * v).toFixed(1)}deg)`;
  $('.gval b', el).textContent = mbps == null ? '—' : (mbps >= 100 ? Math.round(mbps) : mbps.toFixed(1));
  if (sub != null) $('.gsub', el).textContent = sub;
  $$('.ticklbl', el).forEach(t => { t.textContent = Math.round(SP.scale * (+t.dataset.i) / 10); });
}
function rescale(mbps) {
  const steps = [50, 100, 200, 500, 1000, 2000, 4000, 8000, 16000];
  const want = steps.find(s => mbps <= s * 0.92) || steps[steps.length - 1];
  if (want !== SP.scale) { SP.scale = want; setGauge($('#gWrite'), SP.live.write); setGauge($('#gRead'), SP.live.read); }
}

/* --- tables --------------------------------------------------------------- */
function renderWill() {
  const w = SP.last.write, r = SP.last.read;
  const cell = (v, need) => v == null ? '<span class="will-na">—</span>' : v >= need ? '<span class="will-ok">✓</span>' : '<span class="will-no">✕</span>';
  $('#spWill tbody').innerHTML = WILL.map(([name, need]) => `<tr><td>${esc(name)}</td><td class="num">${need} MB/s</td><td>${cell(w, need)}</td><td>${cell(r, need)}</td></tr>`).join('');
}
function renderRuns() {
  const rows = SP.runs.slice().reverse().slice(0, 30);
  $('#spRuns tbody').innerHTML = rows.map((x, i) => `<tr><td>${SP.runs.length - i}</td><td class="num">${x.write}</td><td class="num">${x.read}</td><td>${fmtWhen(x.ts)}</td></tr>`).join('') || '<tr><td colspan="4" class="hint">No complete runs yet.</td></tr>';
  if (SP.runs.length) {
    const avg = k => (SP.runs.reduce((a, x) => a + x[k], 0) / SP.runs.length).toFixed(1);
    $('#spRunCount').textContent = `· ${SP.runs.length} run${SP.runs.length > 1 ? 's' : ''} · average write ${avg('write')} / read ${avg('read')} MB/s`;
  } else $('#spRunCount').textContent = '';
}

/* --- control -------------------------------------------------------------- */
function setRunning(on) {
  SP.running = on;
  $('#spStart').disabled = on; $('#spStop').disabled = !on;
  $('#spTarget').disabled = on; $('#spSize').disabled = on; $('#spBlock').disabled = on;
}
async function startSpeed() {
  const root = $('#spTarget').value;
  if (!root) { toast('Pick a drive first'); return; }
  $('#spError').classList.add('hidden');
  const r = await api('/api/speed/start', { root, sizeMB: +$('#spSize').value, block: +$('#spBlock').value, loop: $('#spLoop').checked });
  if (r.error) { $('#spError').textContent = r.error; $('#spError').classList.remove('hidden'); return; }
  SP.runs = []; SP.since = 0; SP.live = { write: null, read: null }; SP.last = { write: null, read: null };
  setGauge($('#gWrite'), null, ''); setGauge($('#gRead'), null, ''); renderRuns(); renderWill();
  setRunning(true);
  $('#spStatus').textContent = `Testing ${root} — writing…`;
  pollSpeed();
}
async function pollSpeed() {
  clearTimeout(SP.timer);
  const ev = await api(`/api/speed/events?since=${SP.since}`);
  if (ev.error) { SP.timer = setTimeout(pollSpeed, 1000); return; }
  SP.since = ev.seq;
  for (const x of ev.events || []) {
    if (x.type === 'tick') {
      SP.live[x.phase] = x.mbps; rescale(x.mbps);
      setGauge(x.phase === 'write' ? $('#gWrite') : $('#gRead'), x.mbps, `${x.pct} %`);
      const where = ev.state.folder && ev.state.folder.replace(/[\/]+$/, '') !== ev.state.target.replace(/[\/]+$/, '') ? ` (test file in ${ev.state.folder})` : '';
      $('#spStatus').textContent = `Testing ${ev.state.target}${where} — ${x.phase === 'write' ? 'writing' : 'reading'} ${ev.state.sizeMB} MB in ${ev.state.block / 1024} KiB blocks`;
    } else if (x.type === 'phase') {
      SP.last[x.phase] = x.mbps; SP.live[x.phase] = x.mbps; rescale(x.mbps);
      setGauge(x.phase === 'write' ? $('#gWrite') : $('#gRead'), x.mbps, `${x.mbps} MB/s over ${x.seconds} s`);
      renderWill();
    } else if (x.type === 'run') {
      SP.runs.push(x.run); renderRuns();
    } else if (x.type === 'error') {
      $('#spError').textContent = x.message; $('#spError').classList.remove('hidden');
    } else if (x.type === 'done') {
      setRunning(false);
      $('#spStatus').textContent = SP.runs.length ? `Finished: ${SP.runs.length} run${SP.runs.length > 1 ? 's' : ''} on ${ev.state.target}` : 'Stopped';
      return;
    }
  }
  if (ev.state?.running) SP.timer = setTimeout(pollSpeed, 300);
  else if (SP.running) { setRunning(false); $('#spStatus').textContent = 'Finished'; }
}

buildGauge($('#gWrite')); buildGauge($('#gRead'));
setGauge($('#gWrite'), null, ''); setGauge($('#gRead'), null, '');
renderWill(); renderRuns();
$('#spStart').addEventListener('click', startSpeed);
$('#spStop').addEventListener('click', () => api('/api/speed/stop', {}));
document.addEventListener('tab', e => {
  if (e.detail !== 'speed') return;
  fillSpeedTargets();
  // pick up a test that is still running (for example after switching tabs)
  api('/api/speed/state').then(s => { if (s && s.running && !SP.running) { setRunning(true); pollSpeed(); } });
});
document.addEventListener('redraw', () => { if (!$('#tab-speed').classList.contains('hidden') && !SP.running) fillSpeedTargets(); });
