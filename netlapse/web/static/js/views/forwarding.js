/**
 * Forwarding View
 *
 * The forwarding model over stored captures: every endpoint (leaf/TOR) to
 * every other, both directions, plus an offline trace from any device to any
 * address. Nothing here touches a device.
 *
 * Data sources:
 *   /api/v1/sites
 *   /api/v1/forwarding/matrix   ?site&as_of&refresh
 *   /api/v1/forwarding/trace    ?src&dest&site&as_of
 *   /api/v1/forwarding/report   standalone HTML (new tab)
 *   /api/v1/forwarding/pairs.csv
 *
 * Deep link: #/forwarding?site=iad1,iad2&as_of=2026-11-20T12:00:00Z
 *            &pair=tor424.iad2|tor5-6131.iad1&trace=tor424.iad2~10.7.22.10
 */

import { api } from '../api.js';
import { escapeHtml } from '../components.js';

let _siteList = [];
let _sites = [];
let _asOf = '';
let _pair = '';
let _trace = '';
let _mode = 'status';
let _filter = '';
let _data = null;
let _loadToken = 0;
let _timer = null;
let _sortKey = 'tor';
let _sortDir = 1;

const esc = (s) => escapeHtml(String(s ?? ''));
const short = (n) => String(n).replace(/\.[^.]+$/, '');


// ── hash ↔ state ─────────────────────────────────────────────────────

function readHash() {
    const q = new URLSearchParams((location.hash.split('?')[1]) || '');
    _sites = (q.get('site') || '').split(',').filter(Boolean);
    _asOf = q.get('as_of') || '';
    _pair = q.get('pair') || '';
    _trace = q.get('trace') || '';
    _mode = q.get('color') || 'status';
}

function writeHash() {
    const q = new URLSearchParams();
    if (_sites.length) q.set('site', _sites.join(','));
    if (_asOf) q.set('as_of', _asOf);
    if (_pair) q.set('pair', _pair);
    if (_trace) q.set('trace', _trace);
    if (_mode !== 'status') q.set('color', _mode);
    const s = q.toString();
    history.replaceState(null, '', `#/forwarding${s ? '?' + s : ''}`);
}

function scopeQuery(extra = {}) {
    const q = new URLSearchParams();
    if (_sites.length) q.set('site', _sites.join(','));
    if (_asOf) q.set('as_of', _asOf);
    for (const [k, v] of Object.entries(extra)) if (v !== '' && v != null) q.set(k, v);
    return q.toString();
}


// ── render ───────────────────────────────────────────────────────────

export function render() {
    readHash();
    return `
        <style>
            .fw-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
            .fw-sites { display:flex; gap:6px; flex-wrap:wrap; }
            .fw-sites .pill { cursor:pointer; }
            .fw-status { font-size:12px; color:var(--text-muted); }
            .fw-wrap { overflow:auto; max-height:72vh; border:1px solid var(--border); border-radius:var(--r); }
            table.fw-mx { border-collapse:collapse; width:auto; }
            table.fw-mx thead tr { border-bottom:none; }
            table.fw-mx th { position:sticky; background:var(--surface); z-index:2; font-weight:500;
                             color:var(--text-muted); font-size:10px; white-space:nowrap; font-family:var(--font-mono);
                             text-transform:none; letter-spacing:0; }
            table.fw-mx thead th { top:0; height:96px; vertical-align:bottom; padding:0 0 4px; }
            table.fw-mx thead th div { writing-mode:vertical-rl; transform:rotate(180deg); margin:0 auto; }
            table.fw-mx tbody th { left:0; text-align:right; padding:0 6px; z-index:1; }
            table.fw-mx thead th.corner { left:0; z-index:3; }
            table.fw-mx td { width:13px; height:13px; min-width:13px; padding:0; font-size:0; border:1px solid var(--surface);
                             cursor:pointer; position:relative; }
            table.fw-mx td.self { background:var(--border-light); cursor:default; }
            table.fw-mx td.asym::after { content:""; position:absolute; right:1px; top:1px; width:4px; height:4px;
                                         background:#111; border-radius:50%;
                                         box-shadow:0 0 0 1px rgba(255,255,255,.75); }
            table.fw-mx td.sel { outline:2px solid var(--blue); outline-offset:-1px; z-index:1; }
            table.fw-mx .site-l { border-left:2px solid var(--text-faint) !important; }
            table.fw-mx .site-t { border-top:2px solid var(--text-faint) !important; }
            table.fw-mx tbody tr:hover th { color:var(--blue); }
            .fw-legend { display:flex; gap:12px; flex-wrap:wrap; font-size:12px; color:var(--text-muted); }
            .fw-sw { display:inline-block; width:12px; height:12px; border-radius:2px; vertical-align:-2px;
                     margin-right:4px; border:1px solid rgba(0,0,0,.08); position:relative; }
            .fw-layers { display:flex; flex-wrap:wrap; gap:10px; padding-bottom:4px; }
            .fw-layer { display:flex; flex-direction:column; gap:6px; width:260px; }
            .fw-hop { font-size:11px; color:var(--text-faint); }
            .fw-node { border:1px solid var(--border); border-left:4px solid var(--blue); border-radius:var(--r);
                       padding:6px 8px; background:var(--surface-alt); font-size:12px; }
            .fw-node .n { font-weight:600; }
            .fw-node .r { color:var(--text-muted); font-size:11px; }
            .fw-node .x { font-size:11px; font-family:var(--font-mono); }
            .fw-node.delivers { border-left-color:var(--green); }
            .fw-node.blackhole, .fw-node.noroute, .fw-node.unknown { border-left-color:var(--red); }
            .fw-node.loop { border-left-color:var(--orange); }
            .fw-node.overlay, .fw-node.exit { border-left-color:var(--purple); }
            .fw-badge { display:inline-block; font-size:10px; padding:0 5px; border-radius:8px; margin-left:4px;
                        border:1px solid var(--border); }
            .fw-badge.default { background:var(--orange-bg); border-color:var(--orange-border); color:var(--orange); }
            .fw-badge.aggregate { background:var(--purple-bg); color:var(--purple); }
            .fw-badge.specific { background:var(--green-bg); border-color:var(--green-border); color:var(--green); }
            .fw-dir { margin-bottom:14px; }
            .fw-dir h4 { margin:0 0 4px; font-size:13px; }
            .fw-sum { font-size:12px; color:var(--text-muted); margin-bottom:6px; }
            .fw-q div { font-size:12px; margin:3px 0; }
            table.fw-list th { cursor:pointer; user-select:none; white-space:nowrap; }
            table.fw-list td { font-size:12px; padding:6px 14px; }
            table.fw-list tr.warn td { background:var(--orange-bg); }
            .fw-spin { display:inline-block; width:12px; height:12px; border:2px solid var(--border);
                       border-top-color:var(--blue); border-radius:50%; animation:fwspin .8s linear infinite;
                       vertical-align:-2px; margin-right:6px; }
            @keyframes fwspin { to { transform:rotate(360deg); } }
        </style>

        <div class="card" style="margin-bottom:12px">
            <div class="card-head" style="flex-direction:column;align-items:stretch;gap:10px">
                <div class="fw-row" style="justify-content:space-between">
                    <div class="fw-sites" id="fw-sites"><span class="text-faint fs-11">Loading sites…</span></div>
                    <div class="fw-row">
                        <input class="input" id="fw-asof" style="width:270px" placeholder="as of (UTC ISO) — blank = latest"
                               value="${esc(_asOf)}">
                        <button class="btn btn-primary btn-sm" id="fw-build">Build</button>
                        <button class="btn btn-sm" id="fw-rebuild" title="Rebuild even if no capture changed">Rebuild</button>
                    </div>
                </div>
                <div class="fw-row" style="justify-content:space-between">
                    <div class="fw-row">
                        <select class="select" id="fw-mode" style="width:230px">
                            <option value="status">Color: status + hops</option>
                            <option value="asym">Color: asymmetry</option>
                            <option value="dep">Color: default / aggregate dependence</option>
                            <option value="fanout">Color: first-hop fanout</option>
                            <option value="paths">Color: ECMP path count</option>
                        </select>
                        <input class="input" id="fw-filter" style="width:180px" placeholder="filter endpoints">
                    </div>
                    <div class="fw-row">
                        <span class="fw-status" id="fw-status"></span>
                        <a class="btn btn-sm" id="fw-report" target="_blank" rel="noopener">Report</a>
                        <a class="btn btn-sm" id="fw-csv">CSV</a>
                    </div>
                </div>
            </div>
        </div>

        <div class="stat-row" id="fw-cards"></div>

        <div class="card" style="margin-bottom:12px">
            <div class="card-head" style="justify-content:space-between">
                <div class="card-title">Endpoint matrix</div>
                <div class="fw-legend" id="fw-legend"></div>
            </div>
            <div style="padding:12px">
                <div class="fw-wrap"><table class="fw-mx" id="fw-mx"></table></div>
                <div class="fw-status" style="margin-top:6px">Rows are sources, columns are destinations. A dot marks pairs whose
                    reverse path crosses a different set of devices. Click a cell for both directions.</div>
            </div>
        </div>

        <div class="card" id="fw-detail" style="display:none;margin-bottom:12px"></div>

        <div class="card" style="margin-bottom:12px">
            <div class="card-head"><div class="card-title">Trace</div></div>
            <div style="padding:12px">
                <div class="fw-row" style="margin-bottom:10px">
                    <select class="select" id="fw-src" style="width:260px"></select>
                    <span class="text-faint">→</span>
                    <input class="input" id="fw-dest" list="fw-dests" style="width:260px"
                           placeholder="address, prefix or endpoint">
                    <datalist id="fw-dests"></datalist>
                    <button class="btn btn-sm btn-primary" id="fw-trace-go">Trace</button>
                    <span class="fw-status" id="fw-trace-status"></span>
                </div>
                <div id="fw-trace-out"></div>
            </div>
        </div>

        <div class="card" style="margin-bottom:12px">
            <div class="card-head"><div class="card-title">Data quality</div></div>
            <div class="fw-q" id="fw-quality" style="padding:12px"></div>
        </div>

        <div class="card">
            <div class="card-head"><div class="card-title">Per-endpoint summary</div></div>
            <div class="table-wrap"><table class="fw-list" id="fw-sum"></table></div>
            <div class="fw-status" style="padding:8px 14px">Fanout is the number of distinct first-hop devices toward each
                destination. A maximum of 1 means the endpoint forwards on a single uplink to every destination.</div>
        </div>
    `;
}


// ── init / load ──────────────────────────────────────────────────────

export async function init() {
    document.getElementById('fw-mode').value = _mode;
    document.getElementById('fw-mode').addEventListener('change', (e) => { _mode = e.target.value; writeHash(); renderMatrix(); });
    document.getElementById('fw-filter').addEventListener('input', (e) => { _filter = e.target.value.trim().toLowerCase(); renderMatrix(); });
    document.getElementById('fw-build').addEventListener('click', () => { readControls(); load(false); });
    document.getElementById('fw-rebuild').addEventListener('click', () => { readControls(); load(true); });
    document.getElementById('fw-asof').addEventListener('keydown', (e) => { if (e.key === 'Enter') { readControls(); load(false); } });
    document.getElementById('fw-trace-go').addEventListener('click', runTrace);
    document.getElementById('fw-dest').addEventListener('keydown', (e) => { if (e.key === 'Enter') runTrace(); });
    document.getElementById('fw-mx').addEventListener('click', (e) => {
        const td = e.target.closest('td[data-k]');
        if (!td) return;
        _pair = td.dataset.k;
        writeHash();
        document.querySelectorAll('#fw-mx td.sel').forEach(x => x.classList.remove('sel'));
        td.classList.add('sel');
        showPair();
    });
    document.getElementById('fw-sum').addEventListener('click', (e) => {
        const th = e.target.closest('th[data-k]');
        if (!th) return;
        if (_sortKey === th.dataset.k) _sortDir = -_sortDir; else { _sortKey = th.dataset.k; _sortDir = 1; }
        renderSummary();
    });

    try {
        _siteList = await api.sites();
    } catch (e) { _siteList = []; }
    renderSites();
    await load(false);
}

export function destroy() {
    _loadToken++;
    if (_timer) { clearInterval(_timer); _timer = null; }
}

function readControls() {
    _asOf = document.getElementById('fw-asof').value.trim();
    writeHash();
}

function renderSites() {
    const el = document.getElementById('fw-sites');
    const list = Array.isArray(_siteList) ? _siteList : (_siteList.sites || []);
    const pills = [`<span class="pill${_sites.length ? '' : ' active'}" data-site="">All sites</span>`]
        .concat(list.map(s => `<span class="pill${_sites.includes(s.slug) ? ' active' : ''}" data-site="${esc(s.slug)}">${esc(s.slug)}</span>`));
    el.innerHTML = pills.join('');
    el.onclick = (e) => {
        const p = e.target.closest('.pill');
        if (!p) return;
        const slug = p.dataset.site;
        if (!slug) _sites = [];
        else _sites = _sites.includes(slug) ? _sites.filter(x => x !== slug) : [..._sites, slug];
        renderSites();
        writeHash();
    };
}

function setStatus(html) { document.getElementById('fw-status').innerHTML = html; }

async function load(refresh) {
    const token = ++_loadToken;
    const t0 = Date.now();
    if (_timer) clearInterval(_timer);
    const tick = () => setStatus(`<span class="fw-spin"></span>Building model · ${Math.round((Date.now() - t0) / 1000)}s
        <span class="text-faint">(the first build after a collection reads every route table)</span>`);
    tick();
    _timer = setInterval(tick, 1000);
    document.getElementById('fw-report').href = `/api/v1/forwarding/report?${scopeQuery()}`;
    document.getElementById('fw-csv').href = `/api/v1/forwarding/pairs.csv?${scopeQuery()}`;
    try {
        const data = await api.forwarding.matrix(scopeQuery(refresh ? { refresh: 1 } : {}));
        if (token !== _loadToken) return;
        _data = data;
        const c = data.cache || {};
        setStatus(`${c.cached ? 'Cached model' : 'Built'} · ${esc(c.built_at || '')}${c.cached ? '' : ` in ${c.build_seconds}s`}
            · ${esc(data.meta.as_of ? 'as of ' + data.meta.as_of : 'latest')}`);
        renderAll();
    } catch (e) {
        if (token !== _loadToken) return;
        setStatus(`<span style="color:var(--red)">${esc(e.message)}</span>`);
    } finally {
        if (token === _loadToken && _timer) { clearInterval(_timer); _timer = null; }
    }
}

function renderAll() {
    renderCards();
    renderMatrix();
    renderQuality();
    renderSummary();
    fillTraceControls();
    if (_pair && _data.pairs[_pair]) showPair(); else document.getElementById('fw-detail').style.display = 'none';
    if (_trace) {
        const [s, d] = _trace.split('~');
        document.getElementById('fw-src').value = s || '';
        document.getElementById('fw-dest').value = d || '';
        if (s && d) runTrace();
    }
}


// ── cards / colors ───────────────────────────────────────────────────

function renderCards() {
    const m = _data.meta, st = m.status || {};
    const cards = [
        ['Endpoints', m.tors], ['Directed pairs', m.pairs], ['Delivered', st.ok || 0, 'every ECMP branch'],
        ['Partial', st.partial || 0], ['Failed', st.fail || 0], ['Elsewhere', st.elsewhere || 0, 'delivered by another device'],
        ['Asymmetric', m.asymmetric, `${m.pairs ? Math.round(100 * m.asymmetric / m.pairs) : 0}% of pairs`],
        ['Path signatures', m.signatures],
    ];
    document.getElementById('fw-cards').innerHTML = cards.map(([k, v, sub]) => `
        <div class="stat-card" style="min-width:120px"><div class="stat-label">${k}</div>
        <div class="stat-value">${v}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ''}</div>`).join('');
}

function mix(a, b, t) {
    t = Math.max(0, Math.min(1, isFinite(t) ? t : 0));
    const pa = a.match(/\w\w/g).map(h => parseInt(h, 16)), pb = b.match(/\w\w/g).map(h => parseInt(h, 16));
    return '#' + pa.map((x, i) => Math.round(x + (pb[i] - x) * t).toString(16).padStart(2, '0')).join('');
}
const B0 = 'dbe6f5', B9 = '1f4e8a';

function scales() {
    const ps = Object.values(_data.pairs);
    return {
        maxH: Math.max(1, ...ps.map(p => p.maxh)),
        maxNP: Math.max(2, ...ps.map(p => p.np)),
        maxDep: Math.max(1, ...ps.map(p => p.dep)),
    };
}

function color(p, sc) {
    switch (_mode) {
        case 'asym': return p.sym === false ? '#d9822b' : '#cfe3c9';
        case 'dep': return p.dep === 0 ? '#cfe8d6' : mix('fff0cc', '8a5a00', p.dep / sc.maxDep);
        case 'fanout': return p.fanout <= 1 ? '#d9822b' : mix(B0, B9, (p.fanout - 1) / 3);
        case 'paths': return mix(B0, B9, Math.log(p.np) / Math.log(sc.maxNP));
        default:
            if (p.status === 'fail') return '#c8372d';
            if (p.status === 'partial') return '#e8a33d';
            if (p.status === 'elsewhere') return '#7b5ea7';
            return mix(B0, B9, (p.maxh - 1) / Math.max(1, sc.maxH - 1));
    }
}

function legend(sc) {
    const L = {
        status: [['#dbe6f5', 'delivered, few hops'], ['#1f4e8a', `delivered, ${sc.maxH} hops`], ['#e8a33d', 'partial'],
                 ['#c8372d', 'failed'], ['#7b5ea7', 'elsewhere']],
        asym: [['#cfe3c9', 'symmetric'], ['#d9822b', 'asymmetric']],
        dep: [['#cfe8d6', 'specific routes only'], ['#8a5a00', `${sc.maxDep} devices on default/aggregate`]],
        fanout: [['#d9822b', 'single first hop'], ['#1f4e8a', '4+ first hops']],
        paths: [['#dbe6f5', '1 path'], ['#1f4e8a', `${sc.maxNP} paths`]],
    }[_mode];
    document.getElementById('fw-legend').innerHTML = L.map(([c, t]) =>
        `<span><span class="fw-sw" style="background:${c}"></span>${t}</span>`).join('') +
        `<span><span class="fw-sw" style="background:var(--surface)"><span style="position:absolute;right:1px;top:1px;width:4px;height:4px;background:#111;border-radius:50%;box-shadow:0 0 0 1px rgba(255,255,255,.75)"></span></span>asymmetric</span>`;
}


// ── matrix ───────────────────────────────────────────────────────────

function labels() {
    // Drop the site suffix only when every short name stays unique; otherwise
    // keep full names everywhere so the axis reads consistently.
    const tors = _data.tors.map(t => t.name);
    const shorts = new Set(tors.map(short));
    const keepFull = shorts.size < tors.length;
    return Object.fromEntries(tors.map(t => [t, keepFull ? t : short(t)]));
}

function renderMatrix() {
    if (!_data) return;
    const sc = scales();
    legend(sc);
    const lab = labels();
    const site = Object.fromEntries(_data.tors.map(t => [t.name, t.site]));
    const cols = _data.tors.map(t => t.name);
    const rows = cols.filter(t => !_filter || t.toLowerCase().includes(_filter) || (site[t] || '').toLowerCase().includes(_filter));
    let h = '<thead><tr><th class="corner"></th>';
    cols.forEach((c, i) => {
        const sl = i > 0 && site[c] !== site[cols[i - 1]] ? ' site-l' : '';
        h += `<th class="${sl}" title="${esc(c)}"><div>${esc(lab[c])}</div></th>`;
    });
    h += '</tr></thead><tbody>';
    rows.forEach((r, ri) => {
        const stt = ri > 0 && site[r] !== site[rows[ri - 1]] ? ' site-t' : '';
        h += `<tr><th class="${stt}" title="${esc(r)}">${esc(lab[r])}</th>`;
        cols.forEach((c, i) => {
            const sl = i > 0 && site[c] !== site[cols[i - 1]] ? ' site-l' : '';
            if (r === c) { h += `<td class="self${sl}${stt}"></td>`; return; }
            const k = r + '|' + c, p = _data.pairs[k];
            if (!p) { h += `<td class="self${sl}${stt}"></td>`; return; }
            const cls = `${p.sym === false ? 'asym ' : ''}${_pair === k ? 'sel ' : ''}${sl}${stt}`;
            const tip = `${r} → ${c}\n${p.status} · hops ${p.minh}-${p.maxh} · ${p.np} paths · fanout ${p.fanout}` +
                (p.sym === false ? '\nasymmetric' : '') + (p.dep ? `\n${p.dep} devices on default/aggregate` : '');
            h += `<td class="${cls}" style="background:${color(p, sc)}" data-k="${esc(k)}" title="${esc(tip)}"></td>`;
        });
        h += '</tr>';
    });
    document.getElementById('fw-mx').innerHTML = h + '</tbody>';
}


// ── hop views ────────────────────────────────────────────────────────

function devLink(name) {
    const d = (_data && _data.devices[name]) || {};
    return d.id ? `<a href="#/device/${d.id}">${esc(name)}</a>` : esc(name);
}

function nodeHtml(name, a) {
    const cls = a.t === 'fwd' ? '' : a.t;
    let h = `<div class="fw-node ${cls}"><div class="n">${devLink(name)}</div>`;
    if (a.p) h += `<div class="r">${esc(a.p)} ${esc(a.proto || '')}<span class="fw-badge ${esc(a.c)}">${esc(a.c)}</span></div>`;
    if (a.t === 'fwd') {
        const seen = new Set();
        for (const x of a.x) {
            const key = (x.nh || '') + (x.if || '');
            if (seen.has(key)) continue;
            seen.add(key);
            const tgt = x.k === 'dev' ? esc(x.n) : `EXIT ${esc(x.n)}`;
            h += `<div class="x">${esc(x.nh || '')} ${esc(x.if || '')} → ${tgt} <span class="text-faint">(${esc(x.how)})</span></div>`;
        }
    } else {
        h += `<div class="x">${esc(a.t)}${a.if ? ' ' + esc(a.if) : ''}</div>`;
    }
    return h + '</div>';
}

function layersFromActions(actions, src) {
    const depth = new Map([[src, 0]]), order = [[src]];
    let frontier = [src];
    while (frontier.length && order.length < 64) {
        const next = [];
        for (const n of frontier) {
            const a = actions[n];
            if (!a || a.t !== 'fwd') continue;
            for (const x of a.x) {
                if (x.k !== 'dev' || depth.has(x.n)) continue;
                depth.set(x.n, depth.get(n) + 1);
                next.push(x.n);
            }
        }
        if (next.length) order.push(next);
        frontier = next;
    }
    return order.map(l => l.map(n => [n, actions[n] || { t: 'unknown' }]));
}

function summaryLine(p) {
    return `${esc(p.status)} · terminals ${esc(p.terms.join(', '))} · hops ${p.minh}-${p.maxh} · ${p.np} ECMP paths · ` +
        `first-hop fanout ${p.fanout}` +
        (p.dep ? ` · ${p.dep} device${p.dep === 1 ? '' : 's'} on default/aggregate (${esc(p.dep_devs.join(', '))})` : '');
}

function layersHtml(layers) {
    return '<div class="fw-layers">' + layers.map((layer, i) =>
        `<div class="fw-layer"><div class="fw-hop">hop ${i}</div>${layer.map(([n, a]) => nodeHtml(n, a)).join('')}</div>`
    ).join('') + '</div>';
}

function dirHtml(src, dst, title) {
    const p = _data.pairs[src + '|' + dst];
    let h = `<div class="fw-dir"><h4>${title}: ${devLink(src)} → ${devLink(dst)}</h4><div class="fw-sum">${summaryLine(p)}</div>`;
    if (p.exits.length) h += `<div class="fw-sum">exits: ${esc(p.exits.join('; '))}</div>`;
    return h + layersHtml(layersFromActions(_data.actions[dst] || {}, src)) + '</div>';
}

function showPair() {
    const [src, dst] = _pair.split('|');
    const p = _data.pairs[_pair];
    if (!p) return;
    let h = `<div class="card-head" style="justify-content:space-between"><div class="card-title">${esc(src)} ⇄ ${esc(dst)}</div>
             <button class="btn btn-sm" id="fw-pair-close">Close</button></div><div style="padding:12px">`;
    if (p.sym === false) {
        h += `<div class="fw-sum"><b>Asymmetric.</b> Only forward: ${esc(p.only_fwd.join(', ') || 'none')} · only reverse: ${esc(p.only_rev.join(', ') || 'none')}</div>`;
    } else if (p.sym) {
        h += `<div class="fw-sum">Symmetric: both directions cross the same devices.</div>`;
    }
    h += dirHtml(src, dst, 'Forward') + dirHtml(dst, src, 'Reverse') + '</div>';
    const el = document.getElementById('fw-detail');
    el.innerHTML = h;
    el.style.display = 'block';
    document.getElementById('fw-pair-close').onclick = () => {
        el.style.display = 'none';
        _pair = '';
        writeHash();
        document.querySelectorAll('#fw-mx td.sel').forEach(x => x.classList.remove('sel'));
    };
}


// ── trace ────────────────────────────────────────────────────────────

function fillTraceControls() {
    const sel = document.getElementById('fw-src');
    const cur = sel.value;
    const devs = Object.entries(_data.devices)
        .sort(([a, da], [b, db]) => (db.is_tor - da.is_tor) || (da.site || '').localeCompare(db.site || '') || a.localeCompare(b));
    sel.innerHTML = devs.map(([n, d]) => `<option value="${esc(n)}">${esc(n)}${d.is_tor ? '' : ` (${esc(d.role || 'transit')})`}</option>`).join('');
    if (cur) sel.value = cur;
    document.getElementById('fw-dests').innerHTML = _data.tors.map(t =>
        `<option value="${esc(t.name)}">${esc(t.subnet)}</option>`).join('');
}

async function runTrace() {
    const src = document.getElementById('fw-src').value;
    const dest = document.getElementById('fw-dest').value.trim();
    const out = document.getElementById('fw-trace-out');
    const st = document.getElementById('fw-trace-status');
    if (!src || !dest) return;
    _trace = `${src}~${dest}`;
    writeHash();
    st.innerHTML = '<span class="fw-spin"></span>Tracing…';
    try {
        const t = await api.forwarding.trace(scopeQuery({ src, dest }));
        const m = t.model || {};
        st.textContent = m.one_off_build_seconds
            ? `address outside the endpoint subnets — loaded its routes in ${m.one_off_build_seconds}s`
            : 'from the cached model';
        let h = `<div class="fw-sum"><b>${esc(t.src)} → ${esc(t.dest)}</b></div><div class="fw-sum">${summaryLine(t)}</div>`;
        if (t.exits.length) h += `<div class="fw-sum">exits: ${esc(t.exits.join('; '))}</div>`;
        h += layersHtml(t.layers.map(l => l.map(a => [a.device, a])));
        out.innerHTML = h;
    } catch (e) {
        st.textContent = '';
        out.innerHTML = `<div style="color:var(--red);font-size:12px">${esc(e.message)}</div>`;
    }
}


// ── quality / summary ────────────────────────────────────────────────

function renderQuality() {
    const m = _data.meta, q = [];
    const sk = m.skew || {};
    if ((sk.stale || []).length)
        q.push(`<b>Stale route captures</b> (behind the newest by more than the configured limit): ` +
            sk.stale.map(x => `${devLink(x.device)} ${x.minutes_behind}m`).join(', '));
    const pr = Object.entries(m.problems || {});
    if (pr.length) q.push(`<b>Route captures that can't be modeled</b>: ` + pr.map(([d, p]) => `${devLink(d)}: ${esc(p)}`).join('; '));
    if ((m.no_dest_tors || []).length) q.push(`<b>Endpoints without a destination subnet</b>: ${m.no_dest_tors.map(devLink).join(', ')}`);
    const amb = Object.entries(m.ambiguous_ips || {});
    if (amb.length) q.push(`<b>Addresses owned by more than one device</b>: ` + amb.map(([ip, o]) => `${esc(ip)} (${esc(o.join(', '))})`).join('; '));
    if ((m.excluded || []).length) q.push(`<b>Excluded</b>: ${esc(m.excluded.join(', '))}`);
    if (sk.newest) q.push(`<span class="text-faint">Route captures from ${esc(sk.oldest)} to ${esc(sk.newest)} UTC</span>`);
    q.push(`<span class="text-faint">Config: ${esc(m.config || '')}</span>`);
    document.getElementById('fw-quality').innerHTML = q.map(x => `<div>${x}</div>`).join('');
}

const SUM_COLS = [['tor', 'Endpoint'], ['site', 'Site'], ['subnet', 'Subnet'], ['out_ok', 'Out delivered'],
    ['in_ok', 'In delivered'], ['asym', 'Asymmetric'], ['fanout_min', 'Fanout min'], ['fanout_max', 'Fanout max'],
    ['single_homed_pairs', 'Single-uplink dests'], ['maxh', 'Max hops']];

function renderSummary() {
    if (!_data) return;
    const n = _data.tors.length - 1;
    const rows = [..._data.tor_summary].sort((a, b) =>
        (a[_sortKey] > b[_sortKey] ? 1 : a[_sortKey] < b[_sortKey] ? -1 : 0) * _sortDir);
    let h = '<thead><tr>' + SUM_COLS.map(([k, t]) =>
        `<th data-k="${k}">${t}${_sortKey === k ? (_sortDir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('') + '</tr></thead><tbody>';
    for (const r of rows) {
        const warn = r.fanout_max <= 1 || r.out_ok < n || r.in_ok < n;
        h += `<tr class="${warn ? 'warn' : ''}">` + SUM_COLS.map(([k]) =>
            `<td${k === 'tor' || k === 'subnet' ? ' class="mono"' : ''}>${k === 'tor' ? devLink(r[k]) : esc(r[k])}</td>`).join('') + '</tr>';
    }
    document.getElementById('fw-sum').innerHTML = h + '</tbody>';
}
