'use strict';

// Session token comes in the URL fragment, so it never reaches server logs.
const TOKEN = new URLSearchParams(location.hash.slice(1)).get('t') || '';

const state = {
  db: null,
  selected: null,       // frame_id
  mux: {},              // frame_id -> selected multiplexer value
  highlight: null,      // signal name
  overrides: {},        // frame_id -> what-if cycle time
  baud: null,
};

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v) => (v === null || v === undefined) ? '' :
  (typeof v === 'number' ? (Number.isInteger(v) ? String(v) : String(+v.toPrecision(10))) : String(v));

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { 'X-Token': TOKEN, ...(opts.headers || {}) } });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function banner(text, kind = '') {
  const b = $('#banner');
  if (!text) { b.hidden = true; return; }
  b.className = 'banner ' + kind;
  b.textContent = text;
  b.hidden = false;
}

// ---------- signal colours ----------
function colourFor(index) {
  const hue = (index * 137.508) % 360;  // golden angle spreads neighbours apart
  return `hsl(${hue.toFixed(0)} var(--sig-s) var(--sig-l))`;
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.id === 'tab-' + name));
  if (name === 'busload') loadBusload();
  try { localStorage.setItem('binocan-dbc-tab', name); } catch (e) { /* storage blocked */ }
}

// ---------- messages ----------
function renderMessageList() {
  const q = $('#search').value.trim().toLowerCase();
  const list = $('#message-list');
  list.innerHTML = '';
  for (const m of state.db.messages) {
    const hay = [m.name, m.hex_id, String(m.frame_id), ...m.signals.map((s) => s.name)].join(' ').toLowerCase();
    if (q && !hay.includes(q)) continue;
    const li = document.createElement('li');
    li.innerHTML = `<span class="mono">${esc(m.hex_id)}</span><span>${esc(m.name)}</span>`;
    li.classList.toggle('active', m.frame_id === state.selected);
    li.onclick = () => { state.selected = m.frame_id; state.highlight = null; renderMessageList(); renderMessage(); };
    list.appendChild(li);
  }
}

function muxValues(m) {
  const vals = new Set();
  m.signals.forEach((s) => (s.multiplexer_ids || []).forEach((v) => vals.add(v)));
  return [...vals].sort((a, b) => a - b);
}

function visibleSignals(m) {
  const vals = muxValues(m);
  if (!vals.length) return m.signals;
  const cur = state.mux[m.frame_id] ?? vals[0];
  return m.signals.filter((s) => !s.multiplexer_ids || s.multiplexer_ids.includes(cur));
}

function bitGrid(m, signals) {
  const owner = new Map();       // bit -> [signal index]
  signals.forEach((s, i) => s.bits.forEach((b) => owner.set(b, [...(owner.get(b) || []), i])));
  const rows = [];
  let head = '<tr><th></th>';
  for (let bit = 7; bit >= 0; bit--) head += `<th>${bit}</th>`;
  rows.push(head + '</tr>');
  for (let byte = 0; byte < m.length; byte++) {
    let tr = `<tr><th>Byte ${byte}</th>`;
    for (let bit = 7; bit >= 0; bit--) {
      const abs = byte * 8 + bit;
      const owners = owner.get(abs) || [];
      if (!owners.length) {
        tr += `<td><span class="bitno">${abs}</span></td>`;
        continue;
      }
      const s = signals[owners[0]];
      const msb = s.byte_order === 'little_endian' ? s.bits[s.bits.length - 1] : s.bits[0];
      const lsb = s.byte_order === 'little_endian' ? s.bits[0] : s.bits[s.bits.length - 1];
      const cls = ['sig'];
      if (owners.length > 1) cls.push('clash');
      if (state.highlight === s.name) cls.push('hl');
      const label = (abs === lsb || s.length <= 2) ? esc(s.name.length > 9 ? s.name.slice(0, 8) + '…' : s.name) : '';
      tr += `<td class="${cls.join(' ')}" data-sig="${esc(s.name)}" style="background:${colourFor(m.signals.indexOf(s))}"
        title="${esc(s.name)} · bit ${abs}${owners.length > 1 ? ' · OVERLAP with ' + esc(owners.slice(1).map((i) => signals[i].name).join(', ')) : ''}">
        <span class="bitno">${abs}</span>${label}${abs === msb && s.length > 1 ? '<span class="msb">M</span>' : ''}${abs === lsb && s.length > 1 ? '<span class="lsb">L</span>' : ''}</td>`;
    }
    rows.push(tr + '</tr>');
  }
  return `<table class="bitgrid">${rows.join('')}</table>`;
}

function renderMessage() {
  const el = $('#message-detail');
  const m = state.db.messages.find((x) => x.frame_id === state.selected);
  if (!m) { el.innerHTML = '<p class="muted">Pick a message.</p>'; return; }
  const sigs = visibleSignals(m);
  const vals = muxValues(m);
  const curMux = state.mux[m.frame_id] ?? vals[0];
  const muxSig = m.signals.find((s) => s.is_multiplexer);
  const muxLabel = (v) => (muxSig && muxSig.choices && muxSig.choices[v]) ? `${v}: ${muxSig.choices[v]}` : String(v);

  el.innerHTML = `
    <h2>${esc(m.name)} <span class="muted mono">${esc(m.hex_id)}</span></h2>
    <div class="props">
      <div><label>Frame ID</label><span class="mono">${esc(m.hex_id)} (${m.frame_id})${m.is_extended ? ' · extended' : ''}</span></div>
      <div><label>Length (DLC)</label>${m.length} bytes${m.is_fd ? ' · CAN FD' : ''}</div>
      <div><label>Sender</label>${esc(m.senders.join(', ') || '—')}</div>
      <div><label>Send type</label>${esc(m.send_type || 'Cyclic (default)')}</div>
      <div><label>Cycle time</label>${m.cycle_time ? m.cycle_time + ' ms (' + fmt(1000 / m.cycle_time) + ' Hz)' : '—'}</div>
      <div><label>Signals</label>${m.signals.length}</div>
    </div>
    ${m.comment ? `<div class="comment">${esc(m.comment)}</div>` : ''}
    ${vals.length ? `<div class="mux-select">Multiplexer value:
        <select id="mux-select">${vals.map((v) => `<option value="${v}" ${v === curMux ? 'selected' : ''}>${esc(muxLabel(v))}</option>`).join('')}</select></div>` : ''}
    <div class="layout-wrap">
      ${bitGrid(m, sigs)}
      <div class="legend">${sigs.map((s) => `<span data-sig="${esc(s.name)}"><i class="swatch" style="background:${colourFor(m.signals.indexOf(s))}"></i>${esc(s.name)}</span>`).join('')}</div>
    </div>
    <div class="scroll"><table class="grid-table">
      <tr><th>Signal</th><th>Start</th><th>Len</th><th>Order</th><th>Type</th><th>Factor</th><th>Offset</th>
          <th>Min</th><th>Max</th><th>Unit</th><th>Start value</th><th>Receivers</th><th>Values</th><th>Comment</th></tr>
      ${sigs.map((s) => `<tr data-sig="${esc(s.name)}" class="${state.highlight === s.name ? 'hl' : ''}">
        <td><i class="swatch" style="background:${colourFor(m.signals.indexOf(s))}"></i> ${esc(s.name)}${s.is_multiplexer ? ' <b>(mux)</b>' : ''}</td>
        <td class="num">${s.start}</td><td class="num">${s.length}</td>
        <td>${s.byte_order === 'little_endian' ? 'Intel' : 'Motorola'}</td>
        <td>${s.is_float ? 'float' : (s.is_signed ? 'signed' : 'unsigned')}</td>
        <td class="num">${fmt(s.scale)}</td><td class="num">${fmt(s.offset)}</td>
        <td class="num">${fmt(s.minimum)}</td><td class="num">${fmt(s.maximum)}</td>
        <td>${esc(s.unit)}</td><td class="num">${fmt(s.initial)}</td>
        <td>${esc(s.receivers.join(', '))}</td>
        <td class="choices">${s.value_table ? '<b>' + esc(s.value_table) + '</b><br>' : ''}${s.choices ? Object.entries(s.choices).map(([k, v]) => esc(k + ' = ' + v)).join('<br>') : ''}</td>
        <td>${esc(s.comment)}</td></tr>`).join('')}
    </table></div>`;

  const sel = $('#mux-select');
  if (sel) sel.onchange = () => { state.mux[m.frame_id] = Number(sel.value); renderMessage(); };
  el.querySelectorAll('[data-sig]').forEach((n) => {
    n.onclick = () => { state.highlight = state.highlight === n.dataset.sig ? null : n.dataset.sig; renderMessage(); };
  });
}

// ---------- nodes ----------
function renderNodes() {
  const nodes = state.db.nodes.map((n) => n.name);
  let html = `<tr><th>Message</th>${nodes.map((n) => `<th>${esc(n)}</th>`).join('')}</tr>`;
  for (const m of state.db.messages) {
    const rx = new Set(m.signals.flatMap((s) => s.receivers));
    html += `<tr><td><span class="mono">${esc(m.hex_id)}</span> ${esc(m.name)}</td>` +
      nodes.map((n) => `<td>${m.senders.includes(n) ? '<span class="tx">TX</span>' : (rx.has(n) ? '<span class="rx">RX</span>' : '')}</td>`).join('') +
      '</tr>';
  }
  $('#node-matrix').innerHTML = html;
  $('#node-list').innerHTML = '<tr><th>Node</th><th>Sends</th><th>Comment</th></tr>' + state.db.nodes.map((n) =>
    `<tr><td><b>${esc(n.name)}</b></td><td class="num">${state.db.messages.filter((m) => m.senders.includes(n.name)).length}</td><td>${esc(n.comment)}</td></tr>`).join('');
}

// ---------- value tables ----------
function renderValueTables() {
  const users = {};
  for (const m of state.db.messages) for (const s of m.signals) if (s.value_table) (users[s.value_table] ||= []).push(`${m.name}.${s.name}`);
  const names = Object.keys(state.db.value_tables);
  $('#value-tables').innerHTML = names.length ? names.map((name) => `
    <div class="vt"><h3>${esc(name)}</h3>
      <table class="grid-table"><tr><th>Value</th><th>Label</th></tr>
        ${Object.entries(state.db.value_tables[name]).sort((a, b) => a[0] - b[0]).map(([k, v]) => `<tr><td class="num">${esc(k)}</td><td>${esc(v)}</td></tr>`).join('')}
      </table>
      <p class="muted">Used by ${users[name] ? esc(users[name].join(', ')) : 'no signal'}</p>
    </div>`).join('') : '<p class="muted">No global value tables.</p>';
}

// ---------- busload ----------
async function loadBusload() {
  const o = Object.entries(state.overrides).map(([k, v]) => `${k}:${v}`).join(',');
  const q = new URLSearchParams();
  if (state.baud) q.set('baud', state.baud);
  if (o) q.set('o', o);
  const r = await api('/api/busload?' + q.toString());
  if (!state.baud) $('#baud').value = r.baudrate;
  $('#busload-summary').innerHTML = `
    <div>Load<b>${r.total_busload_pct.toFixed(2)} %</b></div>
    <div>Throughput<b>${Math.round(r.total_bps).toLocaleString()} bit/s</b></div>
    <div>Frames<b>${r.total_fps.toFixed(1)} /s</b></div>
    <div>Counted messages<b>${r.message_count}</b></div>`;
  const maxBps = Math.max(1, ...r.messages.map((x) => x.bitrate_bps));
  $('#busload-table').innerHTML = `<tr><th>ID</th><th>Message</th><th>Send type</th><th>DLC</th><th>Cycle ms</th><th>Hz</th><th>bit/s</th><th>Load %</th><th></th></tr>` +
    r.messages.map((x) => `<tr>
      <td class="mono">${esc(x.hex_id)}</td><td>${esc(x.name)}</td><td>${esc(x.send_type)}</td><td class="num">${x.dlc}</td>
      <td><input class="cycle-input" type="number" min="1" data-id="${x.id}" value="${x.cycle_time_ms}" ${x.overridden ? 'style="border-color:var(--accent)"' : ''}></td>
      <td class="num">${x.frequency_hz.toFixed(1)}</td><td class="num">${x.bitrate_bps.toFixed(0)}</td><td class="num">${x.busload_pct.toFixed(2)}</td>
      <td style="width:120px"><div class="bar" style="width:${(x.bitrate_bps / maxBps * 100).toFixed(1)}%"></div></td></tr>`).join('');
  $('#busload-excluded').textContent = r.excluded.length
    ? 'Excluded: ' + r.excluded.map((e) => `${e.name} (0x${e.id.toString(16).toUpperCase()}, ${e.reason || e.send_type})`).join(', ')
    : '';
  document.querySelectorAll('.cycle-input').forEach((inp) => {
    inp.onchange = () => {
      const v = parseInt(inp.value, 10);
      if (v > 0) state.overrides[inp.dataset.id] = v; else delete state.overrides[inp.dataset.id];
      loadBusload();
    };
  });
}

// ---------- check ----------
async function loadCheck() {
  const issues = await api('/api/check');
  const errors = issues.filter((i) => i.level === 'error').length;
  const badge = $('#check-badge');
  badge.hidden = issues.length === 0;
  badge.textContent = issues.length;
  badge.classList.toggle('warn', errors === 0);
  $('#check-list').innerHTML = issues.length
    ? issues.map((i) => `<div class="issue ${i.level}"><b>${esc(i.level)}</b> ${esc(i.where)}: ${esc(i.text)}</div>`).join('')
    : '<p>No problems found.</p>';
}

// ---------- deps ----------
async function loadDeps() {
  try {
    const d = await api('/api/deps');
    if (d.outdated) banner(`cantools ${d.installed} is installed; ${d.latest} is available. Run "python -m pip install -U cantools" and restart. Regenerated C will then carry the new version stamp.`);
  } catch (e) { /* offline is fine */ }
}

// ---------- boot ----------
async function load() {
  state.db = await api('/api/db');
  $('#file-name').textContent = state.db.file;
  document.title = state.db.file + ' · Binocan DBC Editor';
  if (state.selected === null && state.db.messages.length) state.selected = state.db.messages[0].frame_id;
  renderMessageList();
  renderMessage();
  renderNodes();
  renderValueTables();
  await loadCheck();
  if ($('#tab-busload').classList.contains('active')) loadBusload();
}

async function generate() {
  const btn = $('#generate-btn');
  btn.disabled = true;
  try {
    const r = await api('/api/generate', { method: 'POST' });
    const written = [...r.created, ...r.changed];
    banner(written.length ? 'Generated: ' + written.join(', ') : 'C sources already match the DBC; nothing written.', 'ok');
  } catch (e) {
    banner('Generate failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

document.querySelectorAll('#tabs button').forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
$('#search').oninput = renderMessageList;
$('#reload-btn').onclick = () => load().then(() => banner('Reloaded from disk.', 'ok')).catch((e) => banner(e.message, 'error'));
$('#generate-btn').onclick = generate;
$('#baud').onchange = () => { state.baud = parseInt($('#baud').value, 10) || null; loadBusload(); };
$('#busload-reset').onclick = () => { state.overrides = {}; state.baud = null; loadBusload(); };

if (!TOKEN) {
  banner('No session token in the URL. Open the link printed by "python -m binocan_dbc edit".', 'error');
} else {
  load().then(() => {
    let tab = null;
    try { tab = localStorage.getItem('binocan-dbc-tab'); } catch (e) { /* storage blocked */ }
    if (tab && document.getElementById('tab-' + tab)) showTab(tab);
    loadDeps();
  }).catch((e) => banner('Could not load the DBC: ' + e.message, 'error'));
}
