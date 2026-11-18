/**
 * Topology View
 *
 * Network map built from stored LLDP captures, rendered by the viewer ported
 * from Secure Cartography (static/topology/viewer.html) inside an iframe.
 * Same origin, so this view drives window.TopologyViewer directly — no
 * postMessage protocol.
 *
 * Modes:
 *   Map      one point in time (latest by default)
 *   Compare  two points; the union map with added / removed / changed links
 *
 * Link styling (from the viewer addon):
 *   solid      confirmed by both ends
 *   dashed     one-sided: the far end has LLDP data but doesn't list us
 *   dotted     unverified (far end has no LLDP capture) or unmanaged
 *
 * Data sources:
 *   /api/v1/sites
 *   /api/v1/topology            ?site&as_of&unmanaged
 *   /api/v1/topology/diff       ?site&from&to&unmanaged
 *   /api/v1/topology/changes    ?days&site
 *   /api/v1/topology/artifact   (node dialog tabs, via the bridge shim)
 *   /api/v1/topology/mac
 *
 * Deep link: #/topology?site=fra1&as_of=2026-11-01T00:00:00Z&from=...&to=...
 */

import { api } from '../api.js';
import { escapeHtml, icons, downloadFile, exportFilename } from '../components.js';

const STATE_RANK = { confirmed: 0, unverified: 1, unmanaged: 2, one_sided: 3 };
const STATE_LABEL = {
    confirmed: 'confirmed', one_sided: 'one-sided', unverified: 'unverified', unmanaged: 'unmanaged',
};

let _sites = [];          // selected site slugs; empty = all
let _siteList = [];       // [{slug, name, device_count}] from /api/v1/sites
let _asOf = '';          // ISO UTC or ''
let _from = '';
let _to = '';
let _compare = false;
let _unmanaged = false;
let _last = null;        // last API payload (map or diff)
let _loadToken = 0;
let _frameReady = null;  // Promise resolved when the iframe's viewer is up
let _themeObserver = null;

// Device list
let _index = [];         // [{id, site, role, platform, managed, status, tag, tagCls, change}]
let _filter = 'all';     // all | issues | changed
let _query = '';
let _activeId = '';
let _closedGroups = new Set();
let _keyHandler = null;
let _siteCleanup = null;
let _siteNames = {};     // slug → display name
let _groupSites = (() => { try { return localStorage.getItem('netlapse.topology.group') !== 'off'; } catch (e) { return true; } })();


// ── hash ↔ state ─────────────────────────────────────────────────────

function readHash() {
    const hash = location.hash || '';
    const qi = hash.indexOf('?');
    if (qi < 0) return;
    const q = new URLSearchParams(hash.slice(qi + 1));
    _sites = (q.get('site') || '').split(',').map(x => x.trim()).filter(Boolean);
    _asOf = q.get('as_of') || '';
    _from = q.get('from') || '';
    _to = q.get('to') || '';
    _compare = !!_from;
    _unmanaged = q.get('unmanaged') === '1';
}

function writeHash() {
    const q = new URLSearchParams();
    if (_sites.length) q.set('site', _sites.join(','));
    if (_compare) {
        if (_from) q.set('from', _from);
        if (_to) q.set('to', _to);
    } else if (_asOf) {
        q.set('as_of', _asOf);
    }
    if (_unmanaged) q.set('unmanaged', '1');
    const s = q.toString();
    // replaceState: no hashchange, so the router doesn't re-render us
    history.replaceState(null, '', '#/topology' + (s ? '?' + s : ''));
}

// datetime-local works in browser-local time; the API wants UTC.
function localInputToIso(v) {
    if (!v) return '';
    const d = new Date(v);
    return isNaN(d) ? '' : d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function isoToLocalInput(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtLocal(iso) {
    if (!iso) return 'latest';
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
}


// ── render ───────────────────────────────────────────────────────────

export function render() {
    readHash();
    return `
        <div class="card-head" style="justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <div class="tp-ms" id="topo-sites">
                    <button class="select tp-ms-btn" id="topo-sites-btn" type="button">All sites</button>
                    <div class="tp-ms-pop" id="topo-sites-pop" hidden>
                        <input class="input" id="topo-sites-filter" placeholder="Filter sites" autocomplete="off">
                        <div class="tp-ms-acts">
                            <button class="btn btn-sm" type="button" data-ms="all">All sites</button>
                            <button class="btn btn-sm" type="button" data-ms="apply">Apply</button>
                        </div>
                        <div class="tp-ms-list" id="topo-sites-list"></div>
                    </div>
                </div>
                <div class="pill-row" style="margin:0">
                    <button class="pill${_compare ? '' : ' active'}" data-mode="map">Map</button>
                    <button class="pill${_compare ? ' active' : ''}" data-mode="compare">Compare</button>
                </div>
                <span id="topo-map-ctl" style="display:${_compare ? 'none' : 'flex'};gap:6px;align-items:center">
                    <label class="text-faint fs-11">As of</label>
                    <input class="input" type="datetime-local" id="topo-asof" style="width:190px">
                    <button class="btn btn-sm" id="topo-latest" title="Back to latest">Latest</button>
                </span>
                <span id="topo-cmp-ctl" style="display:${_compare ? 'flex' : 'none'};gap:6px;align-items:center;flex-wrap:wrap">
                    <label class="text-faint fs-11">From</label>
                    <input class="input" type="datetime-local" id="topo-from" style="width:190px">
                    <label class="text-faint fs-11">To</label>
                    <input class="input" type="datetime-local" id="topo-to" style="width:190px" placeholder="latest">
                    <select class="select" id="topo-points" style="width:230px">
                        <option value="">LLDP change points…</option>
                    </select>
                </span>
                <label class="fs-11" style="display:flex;gap:4px;align-items:center" title="Draw each site as its own box">
                    <input type="checkbox" id="topo-group"${_groupSites ? ' checked' : ''}> Group by site
                </label>
                <label class="fs-11" style="display:flex;gap:4px;align-items:center">
                    <input type="checkbox" id="topo-unmanaged"${_unmanaged ? ' checked' : ''}> Unmanaged neighbors
                </label>
            </div>
            <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                <button class="btn btn-sm" data-layout="dagre">Hierarchical</button>
                <button class="btn btn-sm" data-layout="cose">Force</button>
                <button class="btn btn-sm" id="topo-fit">Fit</button>
                <button class="btn btn-sm" id="topo-png">PNG</button>
                <button class="btn btn-sm" id="topo-json" title="SC2-compatible map.json">map.json</button>
                <button class="btn btn-sm" id="topo-refresh">${icons.refresh} Refresh</button>
            </div>
        </div>

        <div id="topo-summary" class="fs-11" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:8px;min-height:18px"></div>
        <div id="topo-missing" class="fs-11 text-faint" style="margin-bottom:8px"></div>

        <style>
            .tp-wrap { display:flex; height:calc(100vh - 250px); min-height:460px; padding:0; overflow:hidden; }
            .tp-panel { width:270px; flex:0 0 270px; display:flex; flex-direction:column;
                        border-right:1px solid var(--border); background:var(--surface); }
            .tp-wrap.tp-collapsed .tp-panel { display:none; }
            .tp-panel-head { flex:0 0 auto; padding:10px; border-bottom:1px solid var(--border-light); display:flex; flex-direction:column; gap:8px; }
            .tp-panel-head .search-wrap { flex:0 0 auto; max-width:none; }
            .tp-filters { display:flex; gap:4px; flex-wrap:wrap; }
            .tp-filters .pill { padding:2px 9px; font-size:11px; }
            .tp-list { flex:1; overflow-y:auto; font-size:12px; }
            .tp-group-h { position:sticky; top:0; z-index:1; display:flex; justify-content:space-between;
                          padding:6px 10px; background:var(--surface-alt); border-bottom:1px solid var(--border-light);
                          font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
                          color:var(--text-muted); cursor:pointer; user-select:none; }
            .tp-group-h .tp-caret { display:inline-block; width:10px; transition:transform .12s; }
            .tp-group.tp-closed .tp-caret { transform:rotate(-90deg); }
            .tp-group.tp-closed .tp-rows { display:none; }
            .tp-row { display:flex; align-items:center; gap:7px; padding:4px 10px 4px 14px; cursor:pointer;
                      border-left:3px solid transparent; }
            .tp-row:hover { background:var(--blue-bg); }
            .tp-row.tp-active { background:var(--blue-light); border-left-color:var(--blue); }
            .tp-dot { width:8px; height:8px; border-radius:50%; flex:0 0 8px; background:var(--text-ghost); }
            .tp-dot.ok { background:var(--green); }
            .tp-dot.warn { background:var(--orange); }
            .tp-dot.bad { background:var(--red); }
            .tp-dot.none { background:transparent; border:1.5px dashed var(--text-faint); }
            .tp-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
            .tp-name mark { background:var(--blue-light); color:inherit; padding:0; }
            .tp-tag { font-size:10px; color:var(--text-faint); white-space:nowrap; }
            .tp-tag.warn { color:var(--orange); } .tp-tag.bad { color:var(--red); } .tp-tag.ok { color:var(--green); }
            .tp-empty { padding:16px 10px; color:var(--text-faint); }
            .tp-toggle { position:absolute; left:6px; top:6px; z-index:2; }
            .tp-canvas { position:relative; flex:1; min-width:0; }
            .tp-ms { position:relative; }
            .tp-ms-btn { width:190px; text-align:left; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; cursor:pointer;
                         padding-right:26px; position:relative;
                         background-image:linear-gradient(45deg, transparent 50%, var(--text-faint) 50%), linear-gradient(135deg, var(--text-faint) 50%, transparent 50%);
                         background-position:calc(100% - 15px) 50%, calc(100% - 10px) 50%; background-size:5px 5px; background-repeat:no-repeat; }
            .tp-ms-pop { position:absolute; z-index:50; top:calc(100% + 4px); left:0; width:260px;
                         background:var(--surface); border:1px solid var(--border); border-radius:8px;
                         box-shadow:0 8px 24px rgba(0,0,0,.14); padding:8px; display:flex; flex-direction:column; gap:6px; }
            .tp-ms-pop[hidden] { display:none; }
            .tp-ms-acts { display:flex; justify-content:space-between; }
            .tp-ms-list { max-height:320px; overflow-y:auto; }
            .tp-ms-row { display:flex; align-items:center; gap:6px; padding:4px 4px; border-radius:4px; font-size:12px; cursor:pointer; }
            .tp-ms-row:hover { background:var(--blue-bg); }
            .tp-ms-row .tp-ms-n { margin-left:auto; color:var(--text-faint); font-size:11px; }
            .tp-ms-row .tp-ms-only { display:none; font-size:10px; color:var(--blue); background:none; border:0; cursor:pointer; padding:0 2px; }
            .tp-ms-row:hover .tp-ms-only { display:inline; }
        </style>
        <div class="card tp-wrap" id="tp-wrap">
            <div class="tp-panel">
                <div class="tp-panel-head">
                    <div class="search-wrap">
                        ${icons.search}
                        <input class="input input-search" id="tp-search" placeholder="Find device  ( / )" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="tp-filters" id="tp-filters"></div>
                </div>
                <div class="tp-list" id="tp-list"><div class="tp-empty">Loading…</div></div>
            </div>
            <div class="tp-canvas">
                <button class="btn btn-sm tp-toggle" id="tp-toggle" title="Show / hide device list">☰</button>
                <iframe id="topo-frame" src="/static/topology/viewer.html"
                        style="display:block;width:100%;height:100%;border:0"></iframe>
            </div>
        </div>
    `;
}


// ── init / destroy ───────────────────────────────────────────────────

export async function init() {
    const frame = document.getElementById('topo-frame');
    _frameReady = new Promise(resolve => {
        const check = () => {
            const tv = frame.contentWindow && frame.contentWindow.TopologyViewer;
            if (tv && tv.cy) { resolve(frame.contentWindow); return; }
            setTimeout(check, 50);
        };
        check();   // polls until the viewer has initialised cytoscape
    });
    _frameReady.then(win => {
        installBridge(win);
        applyTheme(win);
    });

    // Re-theme the frame if the user flips light/dark (the shell swaps the
    // stylesheet link, which changes the computed vars).
    _themeObserver = new MutationObserver(() => _frameReady.then(applyTheme));
    const link = document.getElementById('theme-css');
    if (link) _themeObserver.observe(link, { attributes: true, attributeFilter: ['href'] });

    // Sites
    try {
        const sites = await api.sites();
        _siteList = sites;
        for (const st of sites) _siteNames[st.slug] = st.name || st.slug;
        _sites = sites.map(st => st.slug).filter(sl => _sites.includes(sl));   // DCIM order, unknown slugs dropped
        renderSitePicker();
    } catch (e) {
        console.error('sites', e);
    }

    document.getElementById('topo-asof').value = isoToLocalInput(_asOf);
    document.getElementById('topo-from').value = isoToLocalInput(_from);
    document.getElementById('topo-to').value = isoToLocalInput(_to);

    wireControls();
    wirePanel();
    if (_compare) loadChangePoints();
    load();
}

export function destroy() {
    _loadToken++;
    if (_keyHandler) document.removeEventListener('keydown', _keyHandler);
    if (_siteCleanup) _siteCleanup();
    _siteCleanup = null;
    _keyHandler = null;
    _index = [];
    _activeId = '';
    if (_themeObserver) _themeObserver.disconnect();
    _themeObserver = null;
    _frameReady = null;
    _last = null;
}


// ── controls ─────────────────────────────────────────────────────────

function wireControls() {
    const $ = id => document.getElementById(id);

    wireSitePicker();

    document.querySelectorAll('[data-mode]').forEach(btn => btn.addEventListener('click', () => {
        _compare = btn.dataset.mode === 'compare';
        document.querySelectorAll('[data-mode]').forEach(b =>
            b.classList.toggle('active', b === btn));
        $('topo-map-ctl').style.display = _compare ? 'none' : 'flex';
        $('topo-cmp-ctl').style.display = _compare ? 'flex' : 'none';
        if (_compare) {
            loadChangePoints();
            if (!_from) {
                // A sensible first comparison: the last 24 hours.
                _from = new Date(Date.now() - 86400e3).toISOString().replace(/\.\d{3}Z$/, 'Z');
                $('topo-from').value = isoToLocalInput(_from);
            }
        }
        load();
    }));

    $('topo-asof').addEventListener('change', e => { _asOf = localInputToIso(e.target.value); load(); });
    $('topo-latest').addEventListener('click', () => { _asOf = ''; $('topo-asof').value = ''; load(); });
    $('topo-from').addEventListener('change', e => { _from = localInputToIso(e.target.value); load(); });
    $('topo-to').addEventListener('change', e => { _to = localInputToIso(e.target.value); load(); });

    // A change point sets the window to exactly that collection cycle: one
    // second before its first commit versus its last.
    $('topo-points').addEventListener('change', e => {
        const opt = e.target.selectedOptions[0];
        if (!opt || !opt.value) return;
        const first = new Date(opt.dataset.first), last = new Date(opt.dataset.last);
        _from = new Date(first.getTime() - 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
        _to = last.toISOString().replace(/\.\d{3}Z$/, 'Z');
        $('topo-from').value = isoToLocalInput(_from);
        $('topo-to').value = isoToLocalInput(_to);
        load();
    });

    $('topo-unmanaged').addEventListener('change', e => { _unmanaged = e.target.checked; load(); });
    $('topo-group').addEventListener('change', async e => {
        _groupSites = e.target.checked;
        try { localStorage.setItem('netlapse.topology.group', _groupSites ? 'on' : 'off'); } catch (err) { /* storage off */ }
        if (!_last || !_frameReady) return;
        // Regrouping re-creates the moved elements, so decorations go on again.
        const w = await _frameReady;
        applyGrouping(w.TopologyViewer, _last);
        const { edges, nodes } = _compare ? diffDecorations(_last) : mapDecorations(_last);
        w.TopologyViewer.nlDecorate(edges, nodes);
        if (_activeId) w.TopologyViewer.nlFocus(_activeId, { zoom: false });
    });
    $('topo-refresh').addEventListener('click', load);

    document.querySelectorAll('[data-layout]').forEach(btn => btn.addEventListener('click', () =>
        _frameReady && _frameReady.then(w => w.TopologyViewer.applyLayout(btn.dataset.layout))));
    $('topo-fit').addEventListener('click', () =>
        _frameReady && _frameReady.then(w => w.TopologyViewer.fitView()));

    $('topo-png').addEventListener('click', async () => {
        const w = await _frameReady;
        const b64 = w.TopologyViewer.exportPNG();
        if (!b64) return;
        const a = document.createElement('a');
        a.href = 'data:image/png;base64,' + b64;
        a.download = exportFilename(['topology', siteTag(), stampForFile()], 'png');
        a.click();
    });

    $('topo-json').addEventListener('click', () => {
        if (!_last) return;
        downloadFile(exportFilename(['map', siteTag(), stampForFile()], 'json'),
                     JSON.stringify(_last.map, null, 2), 'application/json');
    });
}

function stampForFile() {
    const iso = _compare ? (_to || 'latest') : (_asOf || 'latest');
    return iso.replace(/[:]/g, '').replace(/\.\d+Z$/, 'Z');
}

async function loadChangePoints() {
    const sel = document.getElementById('topo-points');
    if (!sel) return;
    sel.innerHTML = '<option value="">LLDP change points…</option>';
    try {
        const q = new URLSearchParams({ days: '30' });
        if (_sites.length) q.set('site', _sites.join(','));
        const res = await api.topology.changes(q);
        if (!res.supported) {
            sel.innerHTML = '<option value="">(not supported by this backend)</option>';
            return;
        }
        sel.insertAdjacentHTML('beforeend', res.changes.map(c => {
            const n = c.devices.length;
            const who = n === 1 ? c.devices[0].split(':')[0] : `${n} devices`;
            return `<option value="${escapeHtml(c.last)}" data-first="${escapeHtml(c.first)}" data-last="${escapeHtml(c.last)}">` +
                   `${escapeHtml(fmtLocal(c.last))} — ${escapeHtml(who)}</option>`;
        }).join(''));
    } catch (e) {
        console.error('change points', e);
    }
}


// ── load + draw ──────────────────────────────────────────────────────

async function load() {
    writeHash();
    const token = ++_loadToken;
    const summary = document.getElementById('topo-summary');
    summary.innerHTML = '<span class="text-faint">Building map…</span>';
    document.getElementById('topo-missing').textContent = '';

    const q = new URLSearchParams();
    if (_sites.length) q.set('site', _sites.join(','));
    if (_unmanaged) q.set('unmanaged', 'true');

    let data;
    try {
        if (_compare) {
            if (!_from) { summary.innerHTML = '<span class="text-faint">Pick a From time.</span>'; return; }
            q.set('from', _from);
            if (_to) q.set('to', _to);
            data = await api.topology.diff(q);
        } else {
            if (_asOf) q.set('as_of', _asOf);
            data = await api.topology.map(q);
        }
    } catch (e) {
        if (token !== _loadToken) return;
        summary.innerHTML = `<span style="color:var(--red)">${escapeHtml(e.message)}</span>`;
        return;
    }
    if (token !== _loadToken) return;
    _last = data;

    const win = await _frameReady;
    if (token !== _loadToken || !win) return;

    const tv = win.TopologyViewer;
    tv.loadTopology(JSON.stringify(data.map));
    applyGrouping(tv, data);
    const { edges, nodes } = _compare ? diffDecorations(data) : mapDecorations(data);
    tv.nlDecorate(edges, nodes);
    _index = buildIndex(data);
    renderFilters();
    renderList();
    if (_activeId && !tv.nlFocus(_activeId, { zoom: false })) _activeId = '';

    summary.innerHTML = _compare ? diffSummary(data) : mapSummary(data);
    const missing = _compare ? [] : (data.missing_lldp || []);
    document.getElementById('topo-missing').innerHTML = missing.length
        ? `No LLDP capture at this time (${missing.length}): ` +
          missing.slice(0, 40).map(escapeHtml).join(', ') + (missing.length > 40 ? ', …' : '')
        : '';
}

function linkLine(l, extra = '') {
    return `${l.a}:${l.a_if} ↔ ${l.b}:${l.b_if} — ${STATE_LABEL[l.state] || l.state}${extra}`;
}

function mapDecorations(data) {
    const edges = {};
    for (const l of data.links || []) {
        const e = edges[l.edge_id] || (edges[l.edge_id] = { worst: 'confirmed', lines: [] });
        if (STATE_RANK[l.state] > STATE_RANK[e.worst]) e.worst = l.state;
        // Unmanaged links to plain servers carry the LLDP system description
        // as detail; only show detail where it explains the state.
        const why = l.detail && (l.state === 'one_sided' || / matches .* in another site/.test(l.detail));
        e.lines.push(linkLine(l, why ? ` (${l.detail})` : ''));
    }
    for (const e of Object.values(edges)) e.cls = e.worst === 'confirmed' ? '' : 'nl-' + e.worst;

    const nodes = {};
    for (const id of data.missing_lldp || []) nodes[id] = 'nl-nolldp';
    return { edges, nodes };
}

function diffDecorations(data) {
    const edges = {};
    for (const l of data.links || []) {
        const e = edges[l.edge_id] || (edges[l.edge_id] = { changes: new Set(), lines: [] });
        e.changes.add(l.change);
        const what = l.change === 'changed' ? `${STATE_LABEL[l.state_from]} → ${STATE_LABEL[l.state_to]}` : l.change;
        e.lines.push(`[${what}] ${l.a}:${l.a_if} ↔ ${l.b}:${l.b_if}`);
    }
    for (const e of Object.values(edges)) {
        const c = e.changes;
        if (c.size === 1 && c.has('unchanged')) e.cls = 'nl-faded';
        else if (c.size === 1 && c.has('removed')) e.cls = 'nl-removed';
        else if (c.size === 1 && c.has('added')) e.cls = 'nl-added';
        else e.cls = 'nl-changed';   // mixed or a state change on the same pair
    }
    // Fade everything the window didn't touch — DCIM devices and unmanaged
    // neighbors (which only exist as link endpoints) alike.
    const nodes = {};
    const touched = new Set(), all = new Set(Object.keys(data.nodes || {}));
    for (const l of data.links || []) {
        all.add(l.a); all.add(l.b);
        if (l.change !== 'unchanged') { touched.add(l.a); touched.add(l.b); }
    }
    for (const id of all) {
        const change = (data.nodes || {})[id];
        if (change === 'added') nodes[id] = 'nl-added';
        else if (change === 'removed') nodes[id] = 'nl-removed';
        else if (!touched.has(id)) nodes[id] = 'nl-faded';
    }
    return { edges, nodes };
}

function chip(label, n, color) {
    if (!n) return '';
    return `<span style="display:inline-flex;gap:5px;align-items:center">` +
           `<span style="width:18px;border-top:3px ${color};display:inline-block"></span>` +
           `${escapeHtml(label)} <strong>${n}</strong></span>`;
}

function mapSummary(d) {
    const s = d.stats || {}, b = s.by_state || {};
    return [
        `<span><strong>${s.devices || 0}</strong> devices · <strong>${s.links || 0}</strong> links</span>`,
        chip('confirmed', b.confirmed, 'solid var(--text-faint)'),
        chip('one-sided', b.one_sided, 'dashed var(--orange)'),
        chip('unverified', b.unverified, 'dotted var(--text-faint)'),
        chip('unmanaged', b.unmanaged, 'dotted var(--text-faint)'),
        s.unmanaged_hidden ? `<span class="text-faint">${s.unmanaged_hidden} unmanaged hidden</span>` : '',
        `<span class="text-faint">as of ${escapeHtml(fmtLocal(d.as_of))}` +
            (d.version ? ` · <span class="mono">${escapeHtml(d.version.slice(0, 8))}</span>` : '') + `</span>`,
    ].filter(Boolean).join('');
}

function diffSummary(d) {
    const l = (d.summary && d.summary.links) || {}, n = (d.summary && d.summary.nodes) || {};
    const none = !l.added && !l.removed && !l.changed && !n.added && !n.removed;
    return [
        chip('added', l.added, 'solid var(--green)'),
        chip('removed', l.removed, 'dashed var(--red)'),
        chip('changed', l.changed, 'solid var(--orange)'),
        n.added ? `<span>${n.added} device(s) added</span>` : '',
        n.removed ? `<span>${n.removed} device(s) removed</span>` : '',
        none ? '<span>No LLDP changes in this window</span>' : '',
        `<span class="text-faint">${escapeHtml(fmtLocal(d.from.as_of))} → ${escapeHtml(fmtLocal(d.to.as_of))}</span>`,
    ].filter(Boolean).join('');
}


// ── frame plumbing ───────────────────────────────────────────────────

// The SC2 viewer calls bridge.getDeviceArtifact / bridge.getMacTable with a
// callback taking a JSON string. Answer from the API at the map's own time.
function installBridge(win) {
    const at = () => (_compare ? _to : _asOf) || '';
    const call = (path, params, cb) => {
        const q = new URLSearchParams(params);
        if (at()) q.set('as_of', at());
        fetch(`/api/v1/topology/${path}?${q}`)
            .then(r => r.ok ? r.json() : r.text().then(t => ({ ok: false, error: `${r.status}: ${t}` })))
            .then(j => cb(JSON.stringify(j)))
            .catch(e => cb(JSON.stringify({ ok: false, error: String(e) })));
    };
    // SC2 kinds → netlapse capture types
    const kindMap = { config: 'config', inventory: 'inventory' };
    win.TopologyViewer.bridge = {
        getDeviceArtifact: (deviceDir, kind, cb) =>
            call('artifact', { device_dir: deviceDir, kind: kindMap[kind] || kind }, cb),
        getMacTable: (deviceDir, cb) => call('mac', { device_dir: deviceDir }, cb),
        getNodeDetail: (deviceDir, cb) => call('node', { device_dir: deviceDir }, cb),
        // Canvas → list: a node tap selects its row (no zoom; the user is
        // already looking at it). Background tap clears.
        onNodeSelected: (json) => {
            try {
                const d = JSON.parse(json);
                if (!d.nlGroup) setActive(d.id, { zoom: false, scroll: true });
            } catch (e) { /* ignore */ }
        },
        onFocusCleared: () => setActive('', {}),
        onSlash: () => {
            const ev = new KeyboardEvent('keydown', { key: '/' });
            if (_keyHandler) _keyHandler(ev);
        },
    };
}

function applyTheme(win) {
    if (!win || !win.TopologyViewer) return;
    const css = getComputedStyle(document.documentElement);
    const v = k => css.getPropertyValue(k).trim();
    const theme = {
        '--bg-primary': v('--bg'),
        '--bg-surface': v('--surface'),
        '--text-primary': v('--text'),
        '--text-secondary': v('--text-muted'),
        '--accent-primary': v('--blue'),
        '--accent-secondary': v('--blue'),
        '--border-color': v('--border'),
        '--node-border': v('--blue'),
        '--edge-color': v('--text-faint'),
        '--nl-warn': v('--orange'),
        '--nl-add': v('--green'),
        '--nl-del': v('--red'),
        '--nl-chg': v('--orange'),
    };
    for (const k of Object.keys(theme)) if (!theme[k]) delete theme[k];
    win.TopologyViewer.setTheme(JSON.stringify(theme));
}


// ── device list ──────────────────────────────────────────────────────

const STATUS_ORDER = { bad: 0, warn: 1, none: 2, ok: 3, '': 4 };

function buildIndex(data) {
    const out = new Map();
    const stats = id => out.get(id) || out.set(id, {
        id, site: '', role: '', platform: '', managed: false, lldp: '',
        one_sided: 0, unverified: 0, links: 0, change: '', changed: 0,
    }).get(id);

    for (const [id, entry] of Object.entries(data.map || {})) {
        const nd = entry.node_details || {}, md = nd.metadata || {};
        const r = stats(id);
        r.managed = true;
        r.site = md.site || '';
        r.role = md.role || '';
        r.platform = nd.platform || '';
        r.lldp = md.lldp || '';
    }
    for (const l of data.links || []) {
        for (const id of [l.a, l.b]) {
            const r = stats(id);
            r.links++;
            if (l.state === 'one_sided') r.one_sided++;
            if (l.state === 'unverified') r.unverified++;
            if (l.change && l.change !== 'unchanged') r.changed++;
        }
    }
    for (const [id, change] of Object.entries(data.nodes || {})) {
        if (out.has(id) && change !== 'unchanged') out.get(id).change = change;
    }

    for (const r of out.values()) {
        if (!r.managed) { r.status = ''; r.tag = 'unmanaged'; r.tagCls = ''; }
        else if (_compare) {
            if (r.change === 'added') { r.status = 'ok'; r.tag = 'added'; r.tagCls = 'ok'; }
            else if (r.change === 'removed') { r.status = 'bad'; r.tag = 'removed'; r.tagCls = 'bad'; }
            else if (r.changed) { r.status = 'warn'; r.tag = `${r.changed} link change${r.changed > 1 ? 's' : ''}`; r.tagCls = 'warn'; }
            else { r.status = 'ok'; r.tag = r.role || ''; r.tagCls = ''; }
        } else if (r.lldp === 'missing') { r.status = 'none'; r.tag = 'no LLDP'; r.tagCls = ''; }
        else if (r.one_sided) { r.status = 'warn'; r.tag = `${r.one_sided} one-sided`; r.tagCls = 'warn'; }
        else if (r.unverified) { r.status = 'ok'; r.tag = `${r.unverified} unverified`; r.tagCls = ''; }
        else { r.status = 'ok'; r.tag = r.role || ''; r.tagCls = ''; }
        r.isIssue = r.managed && (r.lldp === 'missing' || r.one_sided > 0 || r.unverified > 0);
        r.isChanged = !!(r.change || r.changed);
        r.group = r.managed ? (r.site || '(no site)') : 'Unmanaged';
    }
    return [...out.values()].sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
}

function visibleRows() {
    const q = _query.trim().toLowerCase();
    return _index.filter(r =>
        (_filter === 'all' || (_filter === 'issues' && r.isIssue) || (_filter === 'changed' && r.isChanged)) &&
        (!q || r.id.toLowerCase().includes(q) || r.platform.toLowerCase().includes(q) ||
               r.role.toLowerCase().includes(q) || r.site.toLowerCase().includes(q)));
}

function renderFilters() {
    const el = document.getElementById('tp-filters');
    if (!el) return;
    const n = pred => _index.filter(pred).length;
    const opts = [['all', 'All', _index.length], ['issues', 'Issues', n(r => r.isIssue)]];
    if (_compare) opts.push(['changed', 'Changed', n(r => r.isChanged)]);
    if (!opts.some(o => o[0] === _filter)) _filter = 'all';
    el.innerHTML = opts.map(([k, label, c]) =>
        `<button class="pill${_filter === k ? ' active' : ''}" data-tpf="${k}">${label} ${c}</button>`).join('');
}

function highlight(text) {
    const q = _query.trim();
    const safe = escapeHtml(text);
    if (!q) return safe;
    const i = text.toLowerCase().indexOf(q.toLowerCase());
    if (i < 0) return safe;
    return escapeHtml(text.slice(0, i)) + '<mark>' + escapeHtml(text.slice(i, i + q.length)) + '</mark>' +
           escapeHtml(text.slice(i + q.length));
}

function renderList() {
    const list = document.getElementById('tp-list');
    if (!list) return;
    const rows = visibleRows();
    if (!rows.length) {
        list.innerHTML = `<div class="tp-empty">${_index.length ? 'No matching devices' : 'No devices'}</div>`;
        return;
    }
    const groups = new Map();
    for (const r of rows) (groups.get(r.group) || groups.set(r.group, []).get(r.group)).push(r);
    const names = [...groups.keys()].sort((a, b) =>
        (a === 'Unmanaged') - (b === 'Unmanaged') || a.localeCompare(b));
    const searching = !!_query.trim();
    list.innerHTML = names.map(g => {
        const items = groups.get(g);
        const closed = !searching && _closedGroups.has(g);
        const issues = items.filter(r => r.status === 'warn' || r.status === 'bad').length;
        return `<div class="tp-group${closed ? ' tp-closed' : ''}" data-group="${escapeHtml(g)}">
            <div class="tp-group-h"><span><span class="tp-caret">▾</span> ${escapeHtml(g)}</span>
                <span>${issues ? `<span style="color:var(--orange)">${issues}</span> / ` : ''}${items.length}</span></div>
            <div class="tp-rows">${items.map(r => `
                <div class="tp-row${r.id === _activeId ? ' tp-active' : ''}" data-id="${escapeHtml(r.id)}"
                     title="${escapeHtml([r.id, r.platform, r.role].filter(Boolean).join(' · '))}">
                    <span class="tp-dot ${r.status}"></span>
                    <span class="tp-name">${highlight(r.id)}</span>
                    <span class="tp-tag ${r.tagCls}">${escapeHtml(r.tag)}</span>
                </div>`).join('')}</div>
        </div>`;
    }).join('');
}

function setActive(id, opts) {
    _activeId = id || '';
    document.querySelectorAll('#tp-list .tp-row').forEach(el =>
        el.classList.toggle('tp-active', el.dataset.id === _activeId));
    if (_activeId && opts.scroll) {
        const row = document.querySelector(`#tp-list .tp-row[data-id="${CSS.escape(_activeId)}"]`);
        if (row) row.scrollIntoView({ block: 'nearest' });
    }
    if (!_frameReady) return;
    _frameReady.then(w => {
        if (_activeId) w.TopologyViewer.nlFocus(_activeId, { zoom: opts.zoom !== false });
        else w.TopologyViewer.nlClearFocus();
    });
}

function wirePanel() {
    const wrap = document.getElementById('tp-wrap');
    try { if (localStorage.getItem('netlapse.topology.panel') === 'closed') wrap.classList.add('tp-collapsed'); } catch (e) { /* storage off */ }
    document.getElementById('tp-toggle').addEventListener('click', () => {
        wrap.classList.toggle('tp-collapsed');
        try { localStorage.setItem('netlapse.topology.panel', wrap.classList.contains('tp-collapsed') ? 'closed' : 'open'); } catch (e) { /* storage off */ }
        if (_frameReady) _frameReady.then(w => w.TopologyViewer.cy && w.TopologyViewer.cy.resize());
    });

    const search = document.getElementById('tp-search');
    search.addEventListener('input', () => { _query = search.value; renderList(); });
    search.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            const first = visibleRows()[0];
            if (first) setActive(first.id, { scroll: true });
        } else if (e.key === 'Escape') {
            search.value = ''; _query = ''; renderList(); setActive('', {});
            search.blur();
        }
    });

    document.getElementById('tp-filters').addEventListener('click', e => {
        const b = e.target.closest('[data-tpf]');
        if (!b) return;
        _filter = b.dataset.tpf;
        renderFilters();
        renderList();
    });

    const list = document.getElementById('tp-list');
    list.addEventListener('click', e => {
        const h = e.target.closest('.tp-group-h');
        if (h) {
            const g = h.parentElement.dataset.group;
            _closedGroups.has(g) ? _closedGroups.delete(g) : _closedGroups.add(g);
            h.parentElement.classList.toggle('tp-closed');
            return;
        }
        const row = e.target.closest('.tp-row');
        if (row) setActive(row.dataset.id, {});
    });
    // Double-click a row: open the device dialog (same as tapping the node).
    list.addEventListener('dblclick', e => {
        const row = e.target.closest('.tp-row');
        if (!row || !_frameReady) return;
        _frameReady.then(w => {
            const n = w.TopologyViewer.cy.getElementById(row.dataset.id);
            if (n && n.length) w.TopologyViewer.showNodeInfo(n.data());
        });
    });

    // "/" jumps to the search box from anywhere in the view.
    _keyHandler = e => {
        const t = e.target;
        if (e.key === '/' && !(t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA'))) {
            e.preventDefault();
            const w = document.getElementById('tp-wrap');
            if (w && w.classList.contains('tp-collapsed')) document.getElementById('tp-toggle').click();
            search.focus();
            search.select();
        }
    };
    document.addEventListener('keydown', _keyHandler);
}


// ── site grouping ────────────────────────────────────────────────────

// Managed devices go in their DCIM site; an unmanaged neighbor goes in the
// site of the first device that hears it (a server belongs with its TOR).
function siteGroups(data) {
    const groups = {};
    for (const [id, entry] of Object.entries(data.map || {})) {
        const site = ((entry.node_details || {}).metadata || {}).site;
        if (site) groups[id] = site;
    }
    for (const l of data.links || []) {
        if (!groups[l.b] && groups[l.a]) groups[l.b] = groups[l.a];
        if (!groups[l.a] && groups[l.b]) groups[l.a] = groups[l.b];
    }
    return groups;
}

function applyGrouping(tv, data) {
    if (!_groupSites) { tv.nlGroupBySite(null); return; }
    const groups = siteGroups(data);
    const counts = {};
    for (const [id, k] of Object.entries(groups)) if ((data.map || {})[id]) counts[k] = (counts[k] || 0) + 1;
    const labels = {};
    for (const k of new Set(Object.values(groups))) {
        labels[k] = `${_siteNames[k] || k} · ${counts[k] || 0}`;
    }
    tv.nlGroupBySite(groups, labels);
}


// ── site multi-select ────────────────────────────────────────────────
// Selection is staged in the popover and applied when it closes (Apply,
// Enter, click outside, Esc keeps nothing) so ticking three sites is one
// rebuild, not three. "only" on a row selects just that site and applies.

let _sitesDraft = null;

function siteTag() {
    if (!_sites.length) return 'all';
    return _sites.length <= 3 ? _sites.join('+') : `${_sites.length}-sites`;
}

function renderSitePicker() {
    const btn = document.getElementById('topo-sites-btn');
    if (!btn) return;
    const names = _sites.map(sl => _siteNames[sl] || sl);
    btn.textContent = !names.length ? 'All sites'
        : names.length <= 2 ? names.join(', ') : `${names.length} sites`;
    btn.title = names.length ? names.join(', ') : 'All sites';

    const list = document.getElementById('topo-sites-list');
    const draft = new Set(_sitesDraft || _sites);
    const f = (document.getElementById('topo-sites-filter').value || '').trim().toLowerCase();
    const rows = _siteList.filter(st => !f || st.slug.toLowerCase().includes(f) ||
                                         (st.name || '').toLowerCase().includes(f));
    list.innerHTML = rows.length ? rows.map(st => `
        <label class="tp-ms-row">
            <input type="checkbox" value="${escapeHtml(st.slug)}"${draft.has(st.slug) ? ' checked' : ''}>
            <span>${escapeHtml(st.name || st.slug)}</span>
            <button type="button" class="tp-ms-only" data-only="${escapeHtml(st.slug)}">only</button>
            <span class="tp-ms-n">${st.device_count != null ? st.device_count : ''}</span>
        </label>`).join('') : '<div class="text-faint fs-11" style="padding:4px">No sites match</div>';
}

function openSitePicker() {
    _sitesDraft = [..._sites];
    document.getElementById('topo-sites-filter').value = '';
    renderSitePicker();
    document.getElementById('topo-sites-pop').hidden = false;
    document.getElementById('topo-sites-filter').focus();
}

function closeSitePicker(apply) {
    const pop = document.getElementById('topo-sites-pop');
    if (!pop || pop.hidden) return;
    pop.hidden = true;
    const draft = _sitesDraft;
    _sitesDraft = null;
    if (!apply || !draft) { renderSitePicker(); return; }
    // Keep the DCIM order so the hash and labels are stable.
    const next = _siteList.map(st => st.slug).filter(sl => draft.includes(sl));
    const changed = next.join(',') !== _sites.join(',');
    _sites = next.length === _siteList.length ? [] : next;   // everything = all
    renderSitePicker();
    if (changed) {
        if (_compare) loadChangePoints();
        load();
    }
}

function wireSitePicker() {
    const root = document.getElementById('topo-sites');
    const pop = document.getElementById('topo-sites-pop');
    document.getElementById('topo-sites-btn').addEventListener('click', () =>
        pop.hidden ? openSitePicker() : closeSitePicker(true));

    document.getElementById('topo-sites-filter').addEventListener('input', renderSitePicker);
    pop.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); closeSitePicker(true); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeSitePicker(false); }
    });

    pop.addEventListener('change', e => {
        const cb = e.target.closest('input[type=checkbox]');
        if (!cb) return;
        const d = new Set(_sitesDraft);
        cb.checked ? d.add(cb.value) : d.delete(cb.value);
        _sitesDraft = [...d];
    });

    pop.addEventListener('click', e => {
        const only = e.target.closest('[data-only]');
        if (only) {
            e.preventDefault();
            _sitesDraft = [only.dataset.only];
            closeSitePicker(true);
            return;
        }
        const act = e.target.closest('[data-ms]');
        if (!act) return;
        if (act.dataset.ms === 'all') { _sitesDraft = []; closeSitePicker(true); }
        if (act.dataset.ms === 'apply') closeSitePicker(true);
    });

    // Click anywhere else applies — including on the map, whose clicks land
    // in the iframe's document, not this one (same origin, so listen there).
    const outside = e => { if (!root.contains(e.target)) closeSitePicker(true); };
    const inFrame = () => closeSitePicker(true);
    document.addEventListener('mousedown', outside);
    let frameDoc = null;
    if (_frameReady) _frameReady.then(w => { frameDoc = w.document; frameDoc.addEventListener('mousedown', inFrame); });
    _siteCleanup = () => {
        document.removeEventListener('mousedown', outside);
        if (frameDoc) frameDoc.removeEventListener('mousedown', inFrame);
    };
}
