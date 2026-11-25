"""
Forwarding matrix report: one self-contained, light-theme HTML page with the
data embedded as JSON. No external assets, so it opens from disk or an email.
"""

import json

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Forwarding Matrix</title>
<style>
  :root {
    --bg: #f5f7fa; --panel: #ffffff; --line: #d9dee5; --text: #1f2933; --muted: #5f6b7a;
    --accent: #1f5fa8; --ok0: #e6eef8; --ok9: #1f4e8a; --partial: #e8a33d; --fail: #c8372d;
    --else: #7b5ea7; --asym: #111;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); }
  header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 14px 22px; }
  h1 { margin: 0; font-size: 18px; font-weight: 600; }
  .meta { color: var(--muted); margin-top: 3px; }
  main { padding: 16px 22px 40px; }
  .cards { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 9px 14px; min-width: 120px; }
  .card .v { font-size: 20px; font-weight: 600; }
  .card .k { color: var(--muted); font-size: 12px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; margin-bottom: 14px; }
  .panel h2 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
  .controls { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .controls label { color: var(--muted); }
  select, input[type=text] { font: inherit; padding: 3px 6px; border: 1px solid var(--line); border-radius: 4px; background: #fff; }
  .legend { display: flex; gap: 12px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: -2px; margin-right: 4px; border: 1px solid rgba(0,0,0,.08); }
  .matrix-wrap { overflow: auto; max-height: 78vh; border: 1px solid var(--line); border-radius: 4px; }
  table.mx { border-collapse: collapse; }
  table.mx th { position: sticky; background: var(--panel); z-index: 2; font-weight: 500; color: var(--muted); font-size: 10px; white-space: nowrap; }
  table.mx thead th { top: 0; height: 92px; vertical-align: bottom; padding: 0 0 4px; }
  table.mx thead th div { writing-mode: vertical-rl; transform: rotate(180deg); margin: 0 auto; }
  table.mx tbody th { left: 0; text-align: right; padding: 0 6px; z-index: 1; }
  table.mx thead th.corner { left: 0; z-index: 3; }
  table.mx td { width: 13px; height: 13px; min-width: 13px; padding: 0; border: 1px solid #fff; cursor: pointer; position: relative; }
  table.mx td.self { background: #eceff3; cursor: default; }
  table.mx td.asym::after { content: ""; position: absolute; right: 1px; top: 1px; width: 4px; height: 4px; background: var(--asym); border-radius: 50%; }
  table.mx td.sel { outline: 2px solid var(--accent); outline-offset: -1px; z-index: 1; }
  table.mx .site-l { border-left: 2px solid var(--muted) !important; }
  table.mx .site-t { border-top: 2px solid var(--muted) !important; }
  table.mx tbody tr:hover th { color: var(--accent); }
  #detail { display: none; }
  .dir { margin-bottom: 14px; }
  .dir h3 { font-size: 13px; margin: 0 0 6px; }
  .dir .sum { color: var(--muted); margin-bottom: 6px; }
  .layers { display: flex; flex-wrap: wrap; gap: 10px; padding-bottom: 4px; }
  .layer { display: flex; flex-direction: column; gap: 6px; width: 260px; }
  .layer .hop { color: var(--muted); font-size: 11px; }
  .node { border: 1px solid var(--line); border-left: 4px solid var(--accent); border-radius: 4px; padding: 5px 7px; background: #fbfcfe; }
  .node .n { font-weight: 600; }
  .node .r { color: var(--muted); font-size: 11px; }
  .node .x { font-size: 11px; font-family: ui-monospace, Menlo, monospace; }
  .node.delivers { border-left-color: #2e7d4f; }
  .node.blackhole, .node.noroute, .node.unknown { border-left-color: var(--fail); }
  .node.loop { border-left-color: var(--partial); }
  .node.exit { border-left-color: var(--else); }
  .badge { display: inline-block; font-size: 10px; padding: 0 5px; border-radius: 8px; margin-left: 4px; border: 1px solid var(--line); color: var(--muted); }
  .badge.default { background: #fff3dc; border-color: #f0c674; color: #8a5a00; }
  .badge.aggregate { background: #f3ecfb; border-color: #c9b2e6; color: #5b3f86; }
  .badge.specific { background: #e8f4ec; border-color: #a9d4b8; color: #2e6b46; }
  .diff { font-size: 12px; margin-top: 4px; }
  .diff b { font-weight: 600; }
  table.list { border-collapse: collapse; width: 100%; font-size: 12px; }
  table.list th, table.list td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  table.list th { color: var(--muted); font-weight: 500; cursor: pointer; user-select: none; }
  table.list tr.warn td { background: #fff7e8; }
  .muted { color: var(--muted); }
  .note { color: var(--muted); font-size: 12px; margin-top: 6px; }
</style>
</head>
<body>
<header>
  <h1>Forwarding Matrix</h1>
  <div class="meta" id="meta"></div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <div class="panel">
    <div class="controls">
      <label>Color by
        <select id="mode">
          <option value="status">Status + hops</option>
          <option value="asym">Asymmetry</option>
          <option value="dep">Default / aggregate dependence</option>
          <option value="fanout">First-hop ECMP fanout</option>
          <option value="paths">ECMP path count</option>
        </select>
      </label>
      <label>Filter <input type="text" id="filter" placeholder="tor name / site"></label>
      <div class="legend" id="legend"></div>
    </div>
    <div class="matrix-wrap"><table class="mx" id="mx"></table></div>
    <div class="note">Rows are sources, columns are destinations. A dot marks pairs whose reverse path crosses a different set of devices. Click a cell for both directions.</div>
  </div>

  <div class="panel" id="detail"></div>

  <div class="panel" id="quality"></div>

  <div class="panel">
    <h2>Per-endpoint summary</h2>
    <table class="list" id="torsum"></table>
    <div class="note">Fanout is the number of distinct first-hop devices toward each destination. A maximum of 1 means the endpoint forwards on a single uplink to every destination.</div>
  </div>
</main>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const tors = D.tors.map(t => t.name);
const site = Object.fromEntries(D.tors.map(t => [t.name, t.site]));
const P = D.pairs;
// short labels for matrix headers only when unique; node boxes always use full names
const _keepFull = new Set(tors.map(t => t.replace(/\.[^.]+$/, ''))).size < tors.length;
const short = n => _keepFull ? n : n.replace(/\.[^.]+$/, '');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---------- header + cards
const m = D.meta;
document.getElementById('meta').textContent =
  `Sites: ${(m.sites || []).join(', ') || 'all'} · as of ${m.as_of || 'latest'} · newest route capture ${(m.skew && m.skew.newest) || '-'} · built ${m.built_at} · ${m.devices} devices modeled`;
const st = m.status || {};
const cards = [
  ['Endpoints', m.tors], ['Directed pairs', m.pairs], ['Delivered (all paths)', st.ok || 0],
  ['Partial', st.partial || 0], ['Failed', st.fail || 0], ['Delivered elsewhere', st.elsewhere || 0],
  ['Asymmetric', m.asymmetric], ['Path signatures', m.signatures],
];
document.getElementById('cards').innerHTML = cards.map(([k, v]) =>
  `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

// ---------- colors
function mix(a, b, t) {
  const pa = a.match(/\w\w/g).map(h => parseInt(h, 16)), pb = b.match(/\w\w/g).map(h => parseInt(h, 16));
  return '#' + pa.map((x, i) => Math.round(x + (pb[i] - x) * t).toString(16).padStart(2, '0')).join('');
}
const maxH = Math.max(1, ...Object.values(P).map(p => p.maxh));
const maxNP = Math.max(1, ...Object.values(P).map(p => p.np));
const maxDep = Math.max(1, ...Object.values(P).map(p => p.dep));
const BLUE0 = 'e6eef8', BLUE9 = '1f4e8a';
function color(p, mode) {
  if (mode === 'status') {
    if (p.status === 'fail') return '#c8372d';
    if (p.status === 'partial') return '#e8a33d';
    if (p.status === 'elsewhere') return '#7b5ea7';
    return mix(BLUE0, BLUE9, (p.maxh - 1) / Math.max(1, maxH - 1));
  }
  if (mode === 'asym') return p.sym === false ? '#d9822b' : (p.sym ? '#dfe8d8' : '#eceff3');
  if (mode === 'dep') return p.dep === 0 ? '#e8f4ec' : mix('fff3dc', '8a5a00', p.dep / maxDep);
  if (mode === 'fanout') return p.fanout <= 1 ? '#d9822b' : mix(BLUE0, BLUE9, Math.min(1, (p.fanout - 1) / 3));
  if (mode === 'paths') return mix(BLUE0, BLUE9, Math.log(p.np) / Math.log(Math.max(2, maxNP)));
}
function legend(mode) {
  const L = {
    status: [['#e6eef8', 'delivered, few hops'], ['#1f4e8a', `delivered, ${maxH} hops`], ['#e8a33d', 'partial'], ['#c8372d', 'failed'], ['#7b5ea7', 'delivered elsewhere']],
    asym: [['#dfe8d8', 'symmetric'], ['#d9822b', 'asymmetric']],
    dep: [['#e8f4ec', 'specific routes only'], ['#8a5a00', `${maxDep} devices on default/aggregate`]],
    fanout: [['#d9822b', 'single first hop'], ['#1f4e8a', '4+ first hops']],
    paths: [['#e6eef8', '1 path'], ['#1f4e8a', `${maxNP} paths`]],
  }[mode];
  document.getElementById('legend').innerHTML = L.map(([c, t]) => `<span><span class="sw" style="background:${c}"></span>${t}</span>`).join('') +
    `<span><span class="sw" style="background:#fff;position:relative"><span style="position:absolute;right:1px;top:1px;width:4px;height:4px;background:#111;border-radius:50%"></span></span>asymmetric</span>`;
}

// ---------- matrix
let selected = null;
function render() {
  const mode = document.getElementById('mode').value;
  const f = document.getElementById('filter').value.trim().toLowerCase();
  const rows = tors.filter(t => !f || t.toLowerCase().includes(f) || (site[t] || '').toLowerCase().includes(f));
  const cols = tors;
  let h = '<thead><tr><th class="corner"></th>';
  cols.forEach((c, i) => {
    const sl = i > 0 && site[c] !== site[cols[i - 1]] ? ' site-l' : '';
    h += `<th class="${sl}" title="${esc(c)}"><div>${esc(short(c))}</div></th>`;
  });
  h += '</tr></thead><tbody>';
  rows.forEach((r, ri) => {
    const stt = ri > 0 && site[r] !== site[rows[ri - 1]] ? ' site-t' : '';
    h += `<tr><th class="${stt}" title="${esc(r)}">${esc(short(r))}</th>`;
    cols.forEach((c, i) => {
      const sl = i > 0 && site[c] !== site[cols[i - 1]] ? ' site-l' : '';
      if (r === c) { h += `<td class="self${sl}${stt}"></td>`; return; }
      const p = P[r + '|' + c];
      const cls = `${p.sym === false ? 'asym ' : ''}${selected === r + '|' + c ? 'sel ' : ''}${sl}${stt}`;
      const tip = `${r} → ${c}\n${p.status} · hops ${p.minh}-${p.maxh} · ${p.np} paths · fanout ${p.fanout}` +
                  (p.sym === false ? '\nasymmetric' : '') + (p.dep ? `\n${p.dep} devices on default/aggregate` : '');
      h += `<td class="${cls}" style="background:${color(p, mode)}" data-k="${esc(r + '|' + c)}" title="${esc(tip)}"></td>`;
    });
    h += '</tr>';
  });
  h += '</tbody>';
  const mx = document.getElementById('mx');
  mx.innerHTML = h;
  legend(mode);
}
document.getElementById('mx').addEventListener('click', e => {
  const td = e.target.closest('td[data-k]');
  if (!td) return;
  selected = td.dataset.k;
  document.querySelectorAll('#mx td.sel').forEach(x => x.classList.remove('sel'));
  td.classList.add('sel');
  showDetail(...selected.split('|'));
});
document.getElementById('mode').addEventListener('change', render);
document.getElementById('filter').addEventListener('input', render);

// ---------- detail
function layers(src, dst) {
  const A = D.actions[dst] || {};
  const depth = new Map([[src, 0]]);
  const order = [[src]];
  let frontier = [src];
  while (frontier.length) {
    const next = [];
    for (const n of frontier) {
      const a = A[n];
      if (!a || a.t !== 'fwd') continue;
      for (const x of a.x) {
        if (x.k !== 'dev' || depth.has(x.n)) continue;
        depth.set(x.n, depth.get(n) + 1);
        next.push(x.n);
      }
    }
    if (next.length) order.push(next);
    frontier = next;
    if (order.length > 40) break;
  }
  return order.map(layer => layer.map(n => ({ n, a: A[n] || { t: 'unknown' } })));
}
function nodeHtml(n, a) {
  const cls = a.t === 'fwd' ? '' : a.t;
  let h = `<div class="node ${cls}"><div class="n">${esc(n)}</div>`;
  if (a.p) h += `<div class="r">${esc(a.p)} ${esc(a.proto || '')}<span class="badge ${esc(a.c)}">${esc(a.c)}</span></div>`;
  if (a.t === 'fwd') {
    const seen = new Set();
    for (const x of a.x) {
      const key = x.nh + x.if;
      if (seen.has(key)) continue;
      seen.add(key);
      const tgt = x.k === 'dev' ? x.n : `EXIT ${x.n}`;
      h += `<div class="x">${esc(x.nh || '')} ${esc(x.if || '')} → ${esc(tgt)} <span class="muted">(${esc(x.how)})</span></div>`;
    }
  } else {
    h += `<div class="x">${esc(a.t)}${a.if ? ' ' + esc(a.if) : ''}</div>`;
  }
  return h + '</div>';
}
function dirHtml(src, dst, title) {
  const p = P[src + '|' + dst];
  const L = layers(src, dst);
  let h = `<div class="dir"><h3>${esc(title)}: ${esc(src)} → ${esc(dst)}</h3>`;
  h += `<div class="sum">${esc(p.status)} · terminals ${esc(p.terms.join(', '))} · hops ${p.minh}-${p.maxh} · ${p.np} ECMP paths · first-hop fanout ${p.fanout}` +
       (p.dep ? ` · ${p.dep} devices forwarding on default/aggregate (${esc(p.dep_devs.join(', '))})` : '') + `</div>`;
  if (p.exits.length) h += `<div class="sum">exits: ${esc(p.exits.join('; '))}</div>`;
  h += '<div class="layers">' + L.map((layer, i) =>
    `<div class="layer"><div class="hop">hop ${i}</div>${layer.map(x => nodeHtml(x.n, x.a)).join('')}</div>`).join('') + '</div></div>';
  return h;
}
function showDetail(src, dst) {
  const p = P[src + '|' + dst];
  let h = `<h2>${esc(src)} ⇄ ${esc(dst)}</h2>`;
  if (p.sym === false) {
    h += `<div class="diff"><b>Asymmetric.</b> Only forward: ${esc(p.only_fwd.join(', ') || 'none')} · ` +
         `only reverse: ${esc(p.only_rev.join(', ') || 'none')}</div>`;
  } else if (p.sym) {
    h += `<div class="diff">Symmetric: both directions cross the same devices.</div>`;
  }
  h += dirHtml(src, dst, 'Forward') + dirHtml(dst, src, 'Reverse');
  const el = document.getElementById('detail');
  el.innerHTML = h;
  el.style.display = 'block';
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ---------- per-TOR summary
const cols2 = [['tor', 'Endpoint'], ['site', 'Site'], ['subnet', 'Subnet'], ['out_ok', 'Out delivered'], ['in_ok', 'In delivered'],
               ['asym', 'Asymmetric'], ['fanout_min', 'Fanout min'], ['fanout_max', 'Fanout max'],
               ['single_homed_pairs', 'Single-uplink dests'], ['maxh', 'Max hops']];
let sortKey = 'tor', sortDir = 1;
function renderSum() {
  const rows = [...D.tor_summary].sort((a, b) => (a[sortKey] > b[sortKey] ? 1 : a[sortKey] < b[sortKey] ? -1 : 0) * sortDir);
  const n = tors.length - 1;
  let h = '<thead><tr>' + cols2.map(([k, t]) => `<th data-k="${k}">${t}${sortKey === k ? (sortDir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('') + '</tr></thead><tbody>';
  for (const r of rows) {
    const warn = r.fanout_max <= 1 || r.out_ok < n || r.in_ok < n;
    h += `<tr class="${warn ? 'warn' : ''}">` + cols2.map(([k]) => `<td>${esc(r[k])}</td>`).join('') + '</tr>';
  }
  document.getElementById('torsum').innerHTML = h + '</tbody>';
}
document.getElementById('torsum').addEventListener('click', e => {
  const th = e.target.closest('th[data-k]');
  if (!th) return;
  if (sortKey === th.dataset.k) sortDir = -sortDir; else { sortKey = th.dataset.k; sortDir = 1; }
  renderSum();
});

// ---------- data quality
(function () {
  const q = [];
  const sk = m.skew || {};
  if ((sk.stale || []).length)
    q.push(`<b>Stale route captures</b> (behind the newest by more than the configured limit): ` +
           sk.stale.map(x => `${esc(x.device)} ${x.minutes_behind}m`).join(', '));
  const pr = Object.entries(m.problems || {});
  if (pr.length) q.push(`<b>Route captures that can't be modeled</b>: ` + pr.map(([d, p]) => `${esc(d)}: ${esc(p)}`).join('; '));
  if ((m.no_dest_tors || []).length) q.push(`<b>Endpoints without a destination subnet</b>: ${esc(m.no_dest_tors.join(', '))}`);
  const amb = Object.entries(m.ambiguous_ips || {});
  if (amb.length) q.push(`<b>Addresses owned by more than one device</b>: ` + amb.map(([ip, o]) => `${esc(ip)} (${esc(o.join(', '))})`).join('; '));
  if ((m.excluded || []).length) q.push(`<b>Excluded</b>: ${esc(m.excluded.join(', '))}`);
  q.push(`<span class="muted">Config: ${esc(m.config || '')}</span>`);
  document.getElementById('quality').innerHTML = '<h2>Data quality</h2>' + q.map(x => `<div class="diff">${x}</div>`).join('');
})();

render();
renderSum();
</script>
</body>
</html>
"""


def render_html(data):
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob)
