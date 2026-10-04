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

const post = (path, body = {}) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

// Sends one edit; the server validates it, so a refusal leaves everything as it was.
async function doOp(op) {
  try {
    const r = await post('/api/op', { op });
    if (r.select && r.select.frame_id !== undefined) {
      state.selected = r.select.frame_id;
      if (r.select.signal) state.highlight = r.select.signal;
    }
    banner('');
    await load();
    return true;
  } catch (e) {
    banner(e.message, 'error');
    await load();   // put the form fields back to the stored values
    return false;
  }
}

const numOrNull = (v) => (String(v).trim() === '' ? null : Number(v));

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

// Position of a bit along a signal: Intel signals run up the bit numbers,
// Motorola signals run down each byte and on into the next one.
const seqOfAbs = (abs, order) => (order === 'little_endian' ? abs : (abs >> 3) * 8 + (7 - (abs & 7)));
const absOfSeq = (seq, order) => (order === 'little_endian' ? seq : (seq >> 3) * 8 + (7 - (seq & 7)));
const bitsFor = (start, length, order) =>
  Array.from({ length }, (_, i) => absOfSeq(seqOfAbs(start, order) + i, order));

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
        tr += `<td data-abs="${abs}"><span class="bitno">${abs}</span></td>`;
        continue;
      }
      const s = signals[owners[0]];
      const msb = s.byte_order === 'little_endian' ? s.bits[s.bits.length - 1] : s.bits[0];
      const lsb = s.byte_order === 'little_endian' ? s.bits[0] : s.bits[s.bits.length - 1];
      const cls = ['sig'];
      if (owners.length > 1) cls.push('clash');
      if (state.highlight === s.name) cls.push('hl');
      const label = (abs === lsb || s.length <= 2) ? esc(s.name.length > 9 ? s.name.slice(0, 8) + '…' : s.name) : '';
      const endAbs = absOfSeq(seqOfAbs(s.start, s.byte_order) + s.length - 1, s.byte_order);
      tr += `<td class="${cls.join(' ')}" data-abs="${abs}" data-sig="${esc(s.name)}" style="background:${colourFor(m.signals.indexOf(s))}"
        title="${esc(s.name)} · bit ${abs}${owners.length > 1 ? ' · OVERLAP with ' + esc(owners.slice(1).map((i) => signals[i].name).join(', ')) : ''}">
        <span class="bitno">${abs}</span>${label}${abs === msb && s.length > 1 ? '<span class="msb">M</span>' : ''}${abs === lsb && s.length > 1 ? '<span class="lsb">L</span>' : ''}${abs === endAbs ? '<span class="rz" title="Drag to change the length"></span>' : ''}</td>`;
    }
    rows.push(tr + '</tr>');
  }
  return `<table class="bitgrid">${rows.join('')}</table>`;
}

function nodeChecklist(id, selected, attr) {
  const names = state.db.nodes.map((n) => n.name);
  const label = selected.length ? selected.join(', ') : '—';
  return `<details class="pick"><summary>${esc(label)}</summary><div class="pick-list" ${attr}>
    ${names.map((n) => `<label><input type="checkbox" value="${esc(n)}" ${selected.includes(n) ? 'checked' : ''}> ${esc(n)}</label>`).join('')}
    ${names.length ? '' : '<span class="muted">No nodes yet (Nodes tab).</span>'}</div></details>`;
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
  const tableNames = Object.keys(state.db.value_tables);
  const sendTypes = state.db.send_types || [];

  el.innerHTML = `
    <div class="msg-head">
      <h2>${esc(m.name)} <span class="muted mono">${esc(m.hex_id)}</span></h2>
      <span class="spacer"></span>
      <button id="msg-dup">Duplicate</button>
      <button id="msg-del" class="danger">Delete</button>
    </div>
    <div class="props edit">
      <div><label>Name</label><input type="text" data-m="name" value="${esc(m.name)}"></div>
      <div><label>Frame ID (hex)</label><input type="text" class="mono" data-m="frame_id" value="${m.frame_id.toString(16).toUpperCase()}"></div>
      <div><label>Extended ID</label><input type="checkbox" data-m="is_extended" ${m.is_extended ? 'checked' : ''}></div>
      <div><label>Length (bytes)</label><input type="number" min="0" max="${m.is_fd ? 64 : 8}" data-m="length" value="${m.length}"></div>
      <div><label>Sender</label>${nodeChecklist('s', m.senders, 'data-pick="senders"')}</div>
      <div><label>Send type</label><select data-m="send_type">
        ${m.send_type ? '' : '<option value="">Cyclic (default)</option>'}
        ${sendTypes.map((t) => `<option ${t === m.send_type ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>
      <div><label>Cycle time (ms)${m.cycle_time ? ' · ' + fmt(1000 / m.cycle_time) + ' Hz' : ''}</label>
        <input type="number" min="1" data-m="cycle_time" value="${m.cycle_time ?? ''}"></div>
      <div><label>Signals</label>${m.signals.length}${m.is_fd ? ' · CAN FD' : ''}</div>
    </div>
    <label class="block">Comment <textarea data-m="comment" rows="2">${esc(m.comment)}</textarea></label>
    ${vals.length ? `<div class="mux-select">Multiplexer value:
        <select id="mux-select">${vals.map((v) => `<option value="${v}" ${v === curMux ? 'selected' : ''}>${esc(muxLabel(v))}</option>`).join('')}</select></div>` : ''}
    <div class="layout-wrap">
      ${bitGrid(m, sigs)}
      <div class="legend">${sigs.map((s) => `<span data-sig="${esc(s.name)}"><i class="swatch" style="background:${colourFor(m.signals.indexOf(s))}"></i>${esc(s.name)}</span>`).join('')}</div>
    </div>
    <div class="msg-head"><h3>Signals</h3><span class="spacer"></span><button id="sig-add">Add signal</button></div>
    <div class="scroll"><table class="grid-table edit-table">
      <tr><th>Signal</th><th>Start</th><th>Len</th><th>Order</th><th>Type</th><th>Factor</th><th>Offset</th>
          <th>Min</th><th>Max</th><th>Unit</th><th>Start value (raw)</th><th>Receivers</th><th>Values</th><th>Comment</th><th></th></tr>
      ${sigs.map((s) => `<tr data-sig="${esc(s.name)}" class="${state.highlight === s.name ? 'hl' : ''}">
        <td class="name-cell"><i class="swatch" style="background:${colourFor(m.signals.indexOf(s))}"></i>
          <input type="text" data-s="name" value="${esc(s.name)}" class="w-name">${s.is_multiplexer ? ' <b>(mux)</b>' : ''}</td>
        <td><input type="number" data-s="start" value="${s.start}" class="w-num"></td>
        <td><input type="number" data-s="length" value="${s.length}" class="w-num"></td>
        <td><select data-s="byte_order"><option value="little_endian" ${s.byte_order === 'little_endian' ? 'selected' : ''}>Intel</option>
            <option value="big_endian" ${s.byte_order === 'big_endian' ? 'selected' : ''}>Motorola</option></select></td>
        <td><select data-s="type"><option ${!s.is_float && !s.is_signed ? 'selected' : ''} value="unsigned">unsigned</option>
            <option ${!s.is_float && s.is_signed ? 'selected' : ''} value="signed">signed</option>
            <option ${s.is_float ? 'selected' : ''} value="float">float</option></select></td>
        <td><input type="number" step="any" data-s="scale" value="${fmt(s.scale)}" class="w-num"></td>
        <td><input type="number" step="any" data-s="offset" value="${fmt(s.offset)}" class="w-num"></td>
        <td><input type="number" step="any" data-s="minimum" value="${fmt(s.minimum)}" class="w-num" title="raw range gives ${fmt(s.raw_min)}…${fmt(s.raw_max)} before factor and offset"></td>
        <td><input type="number" step="any" data-s="maximum" value="${fmt(s.maximum)}" class="w-num"></td>
        <td><input type="text" data-s="unit" value="${esc(s.unit)}" class="w-unit"></td>
        <td><input type="number" step="any" data-s="initial" value="${fmt(s.initial)}" class="w-num"></td>
        <td>${nodeChecklist('r', s.receivers, 'data-pick="receivers"')}</td>
        <td class="choices">${s.value_table ? '<b>' + esc(s.value_table) + '</b><br>' : ''}
          <textarea data-s="choices" rows="${Math.max(1, Math.min(6, Object.keys(s.choices || {}).length))}" placeholder="0 = label">${esc(Object.entries(s.choices || {}).map(([k, v]) => k + ' = ' + v).join('\n'))}</textarea>
          ${tableNames.length ? `<select data-s="apply_table"><option value="">use table…</option>${tableNames.map((t) => `<option>${esc(t)}</option>`).join('')}</select>` : ''}</td>
        <td><textarea data-s="comment" rows="1">${esc(s.comment)}</textarea></td>
        <td><button data-s-del title="Delete signal" class="danger">✕</button></td></tr>`).join('')}
    </table></div>`;

  const sel = $('#mux-select');
  if (sel) sel.onchange = () => { state.mux[m.frame_id] = Number(sel.value); renderMessage(); };
  el.querySelectorAll('.legend [data-sig], .bitgrid [data-sig]').forEach((n) => {
    n.onclick = () => {
      if (n.closest('.bitgrid') && n.closest('.bitgrid').dataset.dragged) return; state.highlight = state.highlight === n.dataset.sig ? null : n.dataset.sig; renderMessage(); };
  });

  // drag in the bit grid: move a signal by its body, resize it by the handle on its last bit
  const grid = el.querySelector('.bitgrid');
  const cellAt = (x, y) => { const n = document.elementFromPoint(x, y); return n && n.closest ? n.closest('td[data-abs]') : null; };
  let drag = null;
  const clearGhost = () => grid.querySelectorAll('.ghost, .ghost-bad').forEach((c) => c.classList.remove('ghost', 'ghost-bad'));
  const plan = (target) => {
    const s = drag.sig;
    const total = m.length * 8;
    const t = seqOfAbs(Number(target.dataset.abs), s.byte_order);
    const first = seqOfAbs(s.start, s.byte_order);
    let start = s.start, length = s.length;
    if (drag.mode === 'move') {
      const ns = first + (t - drag.grab);
      if (ns < 0 || ns + s.length > total) return null;
      start = absOfSeq(ns, s.byte_order);
    } else {
      length = t - first + 1;
      if (length < 1 || first + length > total) return null;
    }
    return { start, length, bits: bitsFor(start, length, s.byte_order) };
  };
  grid.onpointerdown = (e) => {
    const cell = e.target.closest('td[data-sig]');
    if (!cell || e.button !== 0) return;
    const sig = m.signals.find((x) => x.name === cell.dataset.sig);
    if (!sig) return;
    drag = { sig, mode: e.target.classList.contains('rz') ? 'resize' : 'move',
      grab: seqOfAbs(Number(cell.dataset.abs), sig.byte_order), moved: false };
    grid.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  grid.onpointermove = (e) => {
    if (!drag) return;
    const target = cellAt(e.clientX, e.clientY);
    clearGhost();
    drag.plan = target ? plan(target) : null;
    if (!drag.plan) return;
    const others = new Set(sigs.filter((x) => x.name !== drag.sig.name).flatMap((x) => x.bits));
    drag.plan.bits.forEach((b) => {
      const c = grid.querySelector(`td[data-abs="${b}"]`);
      if (c) c.classList.add(others.has(b) ? 'ghost-bad' : 'ghost');
    });
    if (drag.plan.start !== drag.sig.start || drag.plan.length !== drag.sig.length) drag.moved = true;
  };
  grid.onpointerup = () => {
    if (!drag) return;
    const d = drag; drag = null; clearGhost();
    if (!d.moved || !d.plan) {   // a plain click: pointer capture swallowed it, so toggle here
      state.highlight = state.highlight === d.sig.name ? null : d.sig.name;
      renderMessage();
      return;
    }
    const base = { op: 'signal.set', frame_id: m.frame_id, name: d.sig.name };
    state.highlight = d.sig.name;
    doOp(d.mode === 'move' ? { ...base, field: 'start', value: d.plan.start }
                           : { ...base, field: 'length', value: d.plan.length });
  };
  grid.onpointercancel = () => { drag = null; clearGhost(); };

  // message fields
  el.querySelectorAll('[data-m]').forEach((inp) => {
    inp.onchange = () => {
      const field = inp.dataset.m;
      let value = inp.type === 'checkbox' ? inp.checked : inp.value;
      if (field === 'frame_id') value = parseInt(value, 16);
      else if (field === 'length' || field === 'cycle_time') value = Number(value);
      doOp({ op: 'message.set', frame_id: m.frame_id, field, value });
    };
  });
  el.querySelectorAll('.props [data-pick]').forEach((box) => {
    box.onchange = () => doOp({ op: 'message.set', frame_id: m.frame_id, field: 'senders',
      value: [...box.querySelectorAll('input:checked')].map((i) => i.value) });
  });
  $('#msg-dup').onclick = () => {
    const name = prompt('Name of the copy', m.name + '_copy');
    if (!name) return;
    const id = prompt('Frame ID of the copy (hex)', (m.frame_id + 1).toString(16).toUpperCase());
    if (!id) return;
    doOp({ op: 'message.duplicate', frame_id: m.frame_id, name, new_frame_id: parseInt(id, 16) });
  };
  $('#msg-del').onclick = () => {
    if (!confirm(`Delete ${m.name} and its ${m.signals.length} signals? Undo can bring it back.`)) return;
    state.selected = null;
    doOp({ op: 'message.delete', frame_id: m.frame_id });
  };
  $('#sig-add').onclick = () => {
    const name = prompt('Name of the new signal');
    if (!name) return;
    const used = new Set(m.signals.flatMap((s) => s.bits));
    let start = 0;
    while (used.has(start) && start < m.length * 8) start++;
    doOp({ op: 'signal.add', frame_id: m.frame_id, name, start: Math.min(start, Math.max(m.length * 8 - 1, 0)), length: 1 });
  };

  // signal fields
  el.querySelectorAll('tr[data-sig]').forEach((row) => {
    const sig = row.dataset.sig;
    const base = { frame_id: m.frame_id, name: sig };
    row.querySelectorAll('[data-s]').forEach((inp) => {
      inp.onfocus = () => { if (state.highlight !== sig) { state.highlight = sig; } };
      inp.onchange = async () => {
        const f = inp.dataset.s;
        if (f === 'type') {
          if (inp.value === 'float') return void doOp({ op: 'signal.set', ...base, field: 'is_float', value: true });
          if (!(await doOp({ op: 'signal.set', ...base, field: 'is_float', value: false }))) return;
          return void doOp({ op: 'signal.set', ...base, field: 'is_signed', value: inp.value === 'signed' });
        }
        if (f === 'choices') {
          const choices = {};
          for (const line of inp.value.split('\n')) {
            if (!line.trim()) continue;
            const [k, ...rest] = line.split('=');
            choices[k.trim()] = rest.join('=').trim();
          }
          return void doOp({ op: 'signal.set_choices', ...base, choices });
        }
        if (f === 'apply_table') {
          if (inp.value) doOp({ op: 'signal.apply_table', ...base, table: inp.value });
          return;
        }
        let value = inp.value;
        if (['start', 'length', 'scale', 'offset'].includes(f)) value = Number(value);
        if (['minimum', 'maximum', 'initial'].includes(f)) value = numOrNull(value);
        const r = await doOp({ op: 'signal.set', ...base, field: f, value });
        if (r && f === 'name') state.highlight = inp.value;
      };
    });
    row.querySelector('[data-pick]').onchange = (ev) => doOp({ op: 'signal.set', ...base, field: 'receivers',
      value: [...ev.currentTarget.querySelectorAll('input:checked')].map((i) => i.value) });
    row.querySelector('[data-s-del]').onclick = () => {
      if (confirm(`Delete signal ${sig}?`)) doOp({ op: 'signal.delete', ...base });
    };
  });
}

// ---------- nodes ----------
function renderNodes() {
  const nodes = state.db.nodes.map((n) => n.name);
  let html = `<tr><th>Message</th>${nodes.map((n) => `<th>${esc(n)}</th>`).join('')}</tr>`;
  for (const m of state.db.messages) {
    const rx = new Set(m.signals.flatMap((s) => s.receivers));
    html += `<tr><td><span class="mono">${esc(m.hex_id)}</span> ${esc(m.name)}</td>` +
      nodes.map((n) => {
        const role = m.senders.includes(n) ? 'tx' : (rx.has(n) ? 'rx' : 'none');
        return `<td class="cell-role" data-id="${m.frame_id}" data-node="${esc(n)}" data-role="${role}">${
          role === 'tx' ? '<span class="tx">TX</span>' : (role === 'rx' ? '<span class="rx">RX</span>' : '')}</td>`;
      }).join('') + '</tr>';
  }
  const mx = $('#node-matrix');
  mx.innerHTML = html;
  const next = { none: 'rx', rx: 'tx', tx: 'none' };
  mx.querySelectorAll('.cell-role').forEach((td) => {
    td.onclick = () => doOp({ op: 'message.set_node_role', frame_id: Number(td.dataset.id),
      node: td.dataset.node, role: next[td.dataset.role] });
  });

  $('#node-list').innerHTML = '<tr><th>Node</th><th>Sends</th><th>Comment</th><th></th></tr>' + state.db.nodes.map((n) =>
    `<tr data-node="${esc(n.name)}"><td><input type="text" data-n="name" value="${esc(n.name)}"></td>
      <td class="num">${state.db.messages.filter((m) => m.senders.includes(n.name)).length}</td>
      <td><input type="text" data-n="comment" value="${esc(n.comment)}" class="w-wide"></td>
      <td><button class="danger" data-n-del>Delete</button></td></tr>`).join('');
  $('#node-list').querySelectorAll('tr[data-node]').forEach((row) => {
    const name = row.dataset.node;
    row.querySelector('[data-n=name]').onchange = (e) => doOp({ op: 'node.rename', name, new_name: e.target.value });
    row.querySelector('[data-n=comment]').onchange = (e) => doOp({ op: 'node.set_comment', name, comment: e.target.value });
    row.querySelector('[data-n-del]').onclick = async () => {
      if (!confirm(`Delete node ${name}?`)) return;
      try {
        await post('/api/op', { op: { op: 'node.delete', name } });
        banner(''); await load();
      } catch (e) {
        if (confirm(e.message + '\n\nRemove it from all messages and signals as well?')) {
          doOp({ op: 'node.delete', name, force: true });
        }
      }
    };
  });
}

// ---------- value tables ----------
const parseEntries = (text) => {
  const out = {};
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    const [k, ...rest] = line.split('=');
    out[k.trim()] = rest.join('=').trim();
  }
  return out;
};

function renderValueTables() {
  const users = {};
  for (const m of state.db.messages) for (const s of m.signals) if (s.value_table) (users[s.value_table] ||= []).push(`${m.name}.${s.name}`);
  const names = Object.keys(state.db.value_tables);
  const box = $('#value-tables');
  box.innerHTML = names.length ? names.map((name) => `
    <div class="vt" data-table="${esc(name)}">
      <div class="msg-head"><input type="text" data-t="name" value="${esc(name)}" class="w-name">
        <span class="spacer"></span><button class="danger" data-t-del>Delete</button></div>
      <textarea data-t="entries" rows="${Math.min(12, Object.keys(state.db.value_tables[name]).length + 1)}">${esc(Object.entries(state.db.value_tables[name]).sort((a, b) => a[0] - b[0]).map(([k, v]) => k + ' = ' + v).join('\n'))}</textarea>
      <p class="muted">One “value = label” per line. Used by ${users[name] ? esc(users[name].join(', ')) : 'no signal'}; saving edits also updates those signals.</p>
    </div>`).join('') : '<p class="muted">No global value tables.</p>';
  box.querySelectorAll('.vt').forEach((vt) => {
    const name = vt.dataset.table;
    vt.querySelector('[data-t=name]').onchange = (e) => doOp({ op: 'table.rename', name, new_name: e.target.value });
    vt.querySelector('[data-t=entries]').onchange = (e) => doOp({ op: 'table.set', name, entries: parseEntries(e.target.value), apply_to_signals: true });
    vt.querySelector('[data-t-del]').onclick = () => { if (confirm(`Delete value table ${name}?`)) doOp({ op: 'table.delete', name }); };
  });
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

// ---------- history, save ----------
function renderStatus() {
  const st = state.db.status;
  $('#dirty').hidden = !st.dirty;
  $('#undo-btn').disabled = !st.can_undo;
  $('#redo-btn').disabled = !st.can_redo;
  $('#save-btn').disabled = !st.dirty;
  $('#generate-btn').disabled = st.dirty;
  $('#generate-btn').title = st.dirty ? 'Save first: C is generated from the saved file' : 'Run cantools generate_c_source into src/';
  if (st.disk_changed) banner('The DBC file changed on disk while you were editing. Saving is refused; export your work by noting it, then Reload.', 'error');
}

async function history(kind) {
  try { await post('/api/' + kind); await load(); } catch (e) { banner(e.message, 'error'); }
}

async function save() {
  try {
    const r = await post('/api/save');
    const parts = [`Saved ${r.saved}.`];
    if (r.docs.length) parts.push('Updated cycle-time tables in ' + r.docs.join(' and ') + '.');
    if (r.c_stale.length) parts.push('C sources are out of date (' + r.c_stale.join(', ') + '); press Generate C.');
    if (r.warnings.length) parts.push('Note: ' + r.warnings.join(' '));
    await load();
    banner(parts.join(' '), r.warnings.length ? '' : 'ok');
  } catch (e) {
    banner('Not saved: ' + e.message, 'error');
  }
}

// ---------- boot ----------
async function load() {
  const active = document.activeElement;
  state.db = await api('/api/db');
  $('#file-name').textContent = state.db.file;
  document.title = (state.db.status.dirty ? '• ' : '') + state.db.file + ' · Binocan DBC Editor';
  if (!state.db.messages.some((m) => m.frame_id === state.selected)) {
    state.selected = state.db.messages.length ? state.db.messages[0].frame_id : null;
  }
  renderStatus();
  renderMessageList();
  renderMessage();
  renderNodes();
  renderValueTables();
  await loadCheck();
  if ($('#tab-busload').classList.contains('active')) loadBusload();
  void active;
}

async function generate() {
  const btn = $('#generate-btn');
  btn.disabled = true;
  try {
    const r = await post('/api/generate');
    const written = [...r.created, ...r.changed];
    banner(written.length ? 'Generated: ' + written.join(', ') : 'C sources already match the DBC; nothing written.', 'ok');
  } catch (e) {
    banner('Generate failed: ' + e.message, 'error');
  } finally {
    renderStatus();
  }
}

document.querySelectorAll('#tabs button').forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });
$('#search').oninput = renderMessageList;
$('#reload-btn').onclick = async () => {
  if (state.db.status.dirty && !confirm('Discard all unsaved edits and read the file again?')) return;
  try { await post('/api/reload'); await load(); banner('Reloaded from disk.', 'ok'); } catch (e) { banner(e.message, 'error'); }
};
$('#generate-btn').onclick = generate;
$('#undo-btn').onclick = () => history('undo');
$('#redo-btn').onclick = () => history('redo');
$('#save-btn').onclick = save;
$('#msg-add').onclick = () => {
  const name = prompt('Name of the new message');
  if (!name) return;
  const id = prompt('Frame ID (hex)');
  if (!id) return;
  doOp({ op: 'message.add', name, frame_id: parseInt(id, 16), length: 8 });
};
$('#node-add').onclick = () => {
  const name = prompt('Name of the new node');
  if (name) doOp({ op: 'node.add', name });
};
$('#table-add').onclick = () => {
  const name = prompt('Name of the new value table');
  if (name) doOp({ op: 'table.add', name, entries: { 0: 'Off', 1: 'On' } });
};
$('#baud').onchange = () => { state.baud = parseInt($('#baud').value, 10) || null; loadBusload(); };
$('#busload-reset').onclick = () => { state.overrides = {}; state.baud = null; loadBusload(); };
window.addEventListener('keydown', (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.target.matches('input[type=text], textarea')) return;
  if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); history('undo'); }
  else if (e.key === 'y' || (e.key === 'z' && e.shiftKey)) { e.preventDefault(); history('redo'); }
  else if (e.key === 's') { e.preventDefault(); save(); }
});
window.addEventListener('beforeunload', (e) => {
  if (state.db && state.db.status.dirty) { e.preventDefault(); e.returnValue = ''; }
});

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
