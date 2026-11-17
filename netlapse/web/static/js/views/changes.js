/**
 * Changes View
 *
 * Fleet-wide "what actually moved" across a time window. Every real change,
 * stamped with when it was detected — with volatility filtered out so an ARP
 * age counter or a config commit-timestamp never shows as drift.
 *
 * Two-phase, because deciding realness means diffing:
 *   1. GET /changes/candidates  — cheap enumeration (drives the progress total)
 *   2. POST /changes/verify      — batched semantic verdict; confirmed changes
 *      stream in with a progress bar as each batch resolves.
 *
 * Cost controls: defaults to the config capture (cheap), and a large candidate
 * set (e.g. the 'all' pill over a wide window) is gated behind an explicit
 * "Verify" click rather than auto-crawling. Changing a filter mid-verify warns
 * before discarding the running pass.
 *
 * Data sources:
 *   /api/v1/search/capture_types   (capture filter pills)
 *   /api/v1/sites                  (site filter)
 *   /api/v1/changes/candidates     (enumeration)
 *   /api/v1/changes/verify         (verdict)
 */

import { api } from '../api.js';
import { escapeHtml, icons, timeAgo } from '../components.js';

const DEFAULT_SINCE = '24h';   // module state + destroy() reset share this

let _captureTypes = [];
let _captureType = 'config';   // cheap, high-value default (not 'all')
let _sites = [];
let _since = DEFAULT_SINCE;
let _site = '';
let _kind = '';
let _rows = [];                // confirmed real changes
let _loadToken = 0;            // bumped per load to cancel an in-flight verify
let _busy = false;
let _verifying = false;        // true only during the verify loop
let _gatedCount = 0;           // candidates held behind the Verify gate, unverified
let _progDone = 0;
let _progTotal = 0;

const VERIFY_BATCH = 40;       // candidates per /verify call
const AUTO_VERIFY_LIMIT = 400; // above this, gate verification behind a click

const WINDOWS = [
    ['1h', 'last hour'],
    ['24h', 'last 24 hours'],
    ['48h', 'last 48 hours'],   // matches the dashboard config-change tile
    ['7d', 'last 7 days'],
    ['30d', 'last 30 days'],
];

const KIND_BADGE = {
    drift: ['badge-warning', 'drift'],
    new:   ['badge-neutral', 'new'],
    gone:  ['badge-error',   'gone'],
};

const KIND_RANK = { drift: 0, gone: 1, new: 2 };


// Read filter overrides off the hash query string, e.g. #/changes?since=48h.
// The router strips the query before matching the route, so it's ours to parse.
// This is what lets a caller (the dashboard config-change tile) hand off its
// own window instead of dumping the user on a default that reports a different
// number than the card they just clicked.
function applyHashFilters() {
    const hash = location.hash || '';
    const qi = hash.indexOf('?');
    if (qi < 0) return;
    const q = new URLSearchParams(hash.slice(qi + 1));

    const since = q.get('since');
    if (since && WINDOWS.some(([v]) => v === since)) _since = since;

    // Non-empty only: init() falls back to the first real capture when the
    // requested one doesn't exist, and '' ("all") would trip that same guard.
    const capture = q.get('capture');
    if (capture) _captureType = capture;

    const site = q.get('site');
    if (site) _site = site;

    const kind = q.get('kind');
    if (kind && ['drift', 'new', 'gone'].includes(kind)) _kind = kind;
}


export function render() {
    applyHashFilters();     // before the selects are built, so they render selected
    return `
        <div class="pill-row" id="chg-pills" style="margin-bottom:14px">
            <span class="text-faint fs-11">Loading capture types…</span>
        </div>

        <div class="card-head" style="justify-content:space-between;margin-bottom:12px">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <select class="select" id="chg-window" style="width:150px">
                    ${WINDOWS.map(([v, label]) =>
                        `<option value="${v}"${v === _since ? ' selected' : ''}>${label}</option>`
                    ).join('')}
                </select>
                <select class="select" id="chg-site" style="width:150px">
                    <option value="">All sites</option>
                </select>
                <select class="select" id="chg-kind" style="width:140px">
                    <option value="">All changes</option>
                    <option value="drift">Drift only</option>
                    <option value="new">New only</option>
                    <option value="gone">Gone only</option>
                </select>
            </div>
            <div style="display:flex;gap:8px;align-items:center">
                <span class="mono text-faint fs-11" id="chg-scope"></span>
                <button class="btn btn-sm" id="chg-refresh">${icons.refresh} Refresh</button>
            </div>
        </div>

        <div id="chg-progress" style="display:none;margin-bottom:12px"></div>
        <div class="audit-summary" id="chg-summary"></div>

        <div class="card" style="margin-top:12px">
            <div class="table-wrap" style="max-height:600px;overflow:auto">
                <table>
                    <thead><tr>
                        <th style="width:110px">When</th>
                        <th>Device</th>
                        <th>Site</th>
                        <th>Capture</th>
                        <th>Change</th>
                        <th style="text-align:right">Magnitude</th>
                    </tr></thead>
                    <tbody id="chg-body">
                        <tr><td colspan="6" class="loading">Loading…</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    `;
}


export async function init() {
    try {
        const t = await api.captureTypes();
        if (Array.isArray(t)) _captureTypes = t;
    } catch { /* all-only */ }

    // Honor the cheap default only if it exists; else fall back to the first
    // real capture (avoid landing on the expensive 'all' automatically).
    if (!_captureTypes.includes(_captureType)) {
        _captureType = _captureTypes[0] || '';
    }
    renderPills();

    try {
        _sites = await api.sites();
        const sel = document.getElementById('chg-site');
        sel.innerHTML = '<option value="">All sites</option>' +
            _sites.map(s =>
                `<option value="${escapeHtml(s.slug)}">${escapeHtml(s.name || s.slug)}</option>`
            ).join('');
    } catch { /* no site filter */ }

    // The site options are built here, not in render(), so a deep-linked site
    // has to be re-applied once they exist. Same for kind, which render()
    // emits without a selected attribute.
    const siteSel = document.getElementById('chg-site');
    if (siteSel && _site) siteSel.value = _site;
    const kindSel = document.getElementById('chg-kind');
    if (kindSel && _kind) kindSel.value = _kind;

    // Window / site / kind: warn-and-revert if a verify is running.
    const win = document.getElementById('chg-window');
    win.addEventListener('change', () => requestFilterChange(
        () => { _since = win.value; loadFeed(); },
        () => { win.value = _since; },
    ));
    const site = document.getElementById('chg-site');
    site.addEventListener('change', () => requestFilterChange(
        () => { _site = site.value; loadFeed(); },
        () => { site.value = _site; },
    ));
    const kind = document.getElementById('chg-kind');
    kind.addEventListener('change', () => requestFilterChange(
        () => { _kind = kind.value; loadFeed(); },
        () => { kind.value = _kind; },
    ));
    document.getElementById('chg-refresh').addEventListener('click', () => requestFilterChange(
        () => loadFeed(),
        () => {},
    ));

    await loadFeed();
}


// Guard a filter change while verification is running: confirm, and revert the
// control if the user declines so the on-screen state stays truthful.
function requestFilterChange(apply, revert) {
    if (_verifying) {
        const ok = confirm(
            `Verification is still running (${_progDone} of ${_progTotal} checked).\n` +
            `Switching now discards it and starts over. Continue?`
        );
        if (!ok) { revert(); return; }
    }
    apply();
}


function renderPills() {
    const el = document.getElementById('chg-pills');
    const pills = [['', 'all'], ..._captureTypes.map(t => [t, t])];
    el.innerHTML = pills.map(([val, label]) =>
        `<button class="pill${val === _captureType ? ' active' : ''}" data-type="${escapeHtml(val)}">${escapeHtml(label)}</button>`
    ).join('');
    el.querySelectorAll('.pill').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.type === _captureType) return;
            requestFilterChange(
                () => { _captureType = btn.dataset.type; renderPills(); loadFeed(); },
                () => { renderPills(); },   // _captureType unchanged → restores active pill
            );
        });
    });
}


async function loadFeed() {
    const token = ++_loadToken;      // any prior verify loop sees a stale token and stops
    _busy = true;
    _verifying = false;
    _gatedCount = 0;
    _rows = [];

    const body = document.getElementById('chg-body');
    body.innerHTML = '<tr><td colspan="6" class="loading">Enumerating changes…</td></tr>';
    document.getElementById('chg-summary').innerHTML = '';
    hideProgress();
    updateScope();

    const params = { since: _since };
    if (_captureType) params.capture_type = _captureType;
    if (_site) params.site = _site;
    if (_kind) params.kind = _kind;

    let data;
    try {
        data = await api.changes.candidates(params);
    } catch (e) {
        if (token !== _loadToken) return;
        _busy = false;
        const msg = /501/.test(e.message)
            ? 'The change feed needs a versioned storage backend.'
            : escapeHtml(e.message);
        body.innerHTML = `<tr><td colspan="6" class="form-error" style="padding:14px">${msg}</td></tr>`;
        return;
    }
    if (token !== _loadToken) return;   // superseded while enumerating

    const cands = data.candidates || [];
    _rows = cands.filter(c => !c.needs_verify).map(toRow);   // new/gone are real by definition
    const toVerify = cands.filter(c => c.needs_verify);

    renderTable();
    renderSummary(data.truncated);

    if (!toVerify.length) {
        // renderTable() above ran while _busy was still true, which paints the
        // "Verifying changes…" placeholder. Nothing needs verifying, so clear
        // the flag and repaint — otherwise an empty result set is stuck showing
        // a spinner that will never resolve.
        _busy = false;
        hideProgress();
        renderTable();
        updateScope();
        return;
    }

    // Cost control: a big candidate set waits for an explicit click rather than
    // auto-crawling. Small sets verify seamlessly.
    if (toVerify.length > AUTO_VERIFY_LIMIT) {
        // Same repaint-after-clearing-_busy rule as the no-candidate path
        // above. Without it the table keeps the "Verifying changes…"
        // placeholder painted by the renderTable() call made while _busy was
        // still true, so a gate that is simply waiting for a click looks
        // indistinguishable from work in progress that never finishes.
        _busy = false;
        _gatedCount = toVerify.length;
        showVerifyGate(toVerify, token, data.truncated);
        renderTable();
        renderSummary(data.truncated);
        updateScope();
        return;
    }

    await runVerify(toVerify, token, data.truncated);
}


async function runVerify(toVerify, token, truncated) {
    _busy = true;
    _verifying = true;
    _gatedCount = 0;             // the gate has been answered
    _progTotal = toVerify.length;
    _progDone = 0;
    showProgress();

    for (let i = 0; i < toVerify.length; i += VERIFY_BATCH) {
        if (token !== _loadToken) return;   // filter changed mid-verify — abandon
        const batch = toVerify.slice(i, i + VERIFY_BATCH);
        try {
            const res = await api.changes.verify(batch.map(c => ({
                device_id: c.device_id,
                capture_type: c.capture_type,
                version_id: c.version_id,
                prev_version_id: c.prev_version_id,
            })));
            if (token !== _loadToken) return;

            const byKey = new Map(batch.map(c => [verifyKey(c), c]));
            (res.results || []).forEach(r => {
                if (!r.real) return;                 // volatility only — drop
                const cand = byKey.get(verifyKey(r));
                if (cand) _rows.push(toRow(cand, r));
            });
        } catch {
            /* a failed batch is skipped, not fatal — keep going */
        }
        _progDone += batch.length;
        showProgress();
        renderTable();
        renderSummary(truncated);
    }

    if (token !== _loadToken) return;
    // Same repaint-after-clearing-_busy rule as the no-candidate path: if every
    // candidate verified away as volatility, the last in-loop render left the
    // "Verifying changes…" placeholder on screen.
    _busy = false;
    _verifying = false;
    hideProgress();
    renderTable();
    renderSummary(truncated);
    updateScope();
}


function verifyKey(o) {
    return `${o.device_id}|${o.capture_type}|${o.version_id}|${o.prev_version_id || ''}`;
}


function toRow(cand, verdict) {
    return {
        device: cand.device,
        device_id: cand.device_id,
        site: cand.site,
        capture_type: cand.capture_type,
        kind: cand.kind,
        at: cand.at,
        version_id: cand.version_id,
        prev_version_id: cand.prev_version_id,
        mode: verdict ? verdict.mode : cand.kind,
        added: verdict ? verdict.added : 0,
        removed: verdict ? verdict.removed : 0,
        changed: verdict ? verdict.changed : 0,
    };
}


// Gate an expensive verify behind an explicit click.
function showVerifyGate(toVerify, token, truncated) {
    const el = document.getElementById('chg-progress');
    el.style.display = '';
    el.innerHTML = `
        <div class="card" style="padding:12px 14px;display:flex;justify-content:space-between;align-items:center;gap:12px;background:var(--surface-2,transparent)">
            <span class="fs-11 text-faint">
                <strong class="mono">${toVerify.length.toLocaleString()}</strong> drift candidate${toVerify.length === 1 ? '' : 's'} to verify${_captureType ? '' : ' across all captures'} — this may take a while.
                ${_captureType ? '' : 'Tip: pick a capture type above to narrow it.'}
            </span>
            <button class="btn btn-sm btn-primary" id="chg-verify-go">Verify ${toVerify.length.toLocaleString()} change${toVerify.length === 1 ? '' : 's'}</button>
        </div>`;
    document.getElementById('chg-verify-go').addEventListener('click', () => {
        if (token !== _loadToken) return;   // scope changed since the gate rendered
        runVerify(toVerify, token, truncated);
    });
}


function showProgress() {
    const el = document.getElementById('chg-progress');
    el.style.display = '';
    const pct = _progTotal ? Math.round((_progDone / _progTotal) * 100) : 100;
    const reals = _rows.filter(r => r.kind === 'drift').length;
    el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span class="fs-11 text-faint">Verifying changes — ${_progDone} / ${_progTotal} checked${reals ? ` · ${reals} real so far` : ''}</span>
            <span class="fs-11 text-faint mono">${pct}%</span>
        </div>
        <div style="height:5px;border-radius:3px;background:var(--border);overflow:hidden">
            <div style="height:100%;width:${pct}%;background:var(--blue);transition:width .2s ease"></div>
        </div>`;
}

function hideProgress() {
    const el = document.getElementById('chg-progress');
    if (el) { el.style.display = 'none'; el.innerHTML = ''; }
}


function renderSummary(truncated) {
    const byType = {}, byKind = {};
    for (const r of _rows) {
        byType[r.capture_type] = (byType[r.capture_type] || 0) + 1;
        byKind[r.kind] = (byKind[r.kind] || 0) + 1;
    }
    const devices = new Set(_rows.map(r => `${r.site}/${r.device}`)).size;

    const kindChips = Object.keys(KIND_BADGE)
        .filter(k => byKind[k])
        .map(k => {
            const [cls, label] = KIND_BADGE[k];
            return `<span class="badge ${cls}">${label} ${byKind[k]}</span>`;
        }).join('');

    const typeChips = Object.entries(byType)
        .sort((a, b) => b[1] - a[1])
        .map(([t, n]) => `<code class="mono fs-11">${escapeHtml(t)}</code> ${n}`)
        .join('<span class="text-faint" style="margin:0 6px">·</span>');

    document.getElementById('chg-summary').innerHTML = `
        <div class="audit-chips">
            <span class="mono fw-500" style="margin-right:4px">${_rows.length} change${_rows.length === 1 ? '' : 's'}</span>
            <span class="text-faint fs-11" style="margin-right:8px">across ${devices} device${devices === 1 ? '' : 's'}</span>
            ${kindChips}
            ${typeChips ? `<span class="text-faint fs-11" style="margin-left:auto">${typeChips}</span>` : ''}
        </div>
        ${truncated ? `<div class="text-faint fs-11" style="margin-top:6px">Showing the newest ${_rows.length}+ — narrow the window or filter by capture to see the rest.</div>` : ''}
    `;
}


function renderTable() {
    const body = document.getElementById('chg-body');

    if (!_rows.length) {
        if (_busy) {
            body.innerHTML = '<tr><td colspan="6" class="loading">Verifying changes…</td></tr>';
            return;
        }
        if (_gatedCount) {
            // Gated, not finished: nothing has been verified yet, so claiming
            // "no real changes" would be a lie and leaving the spinner up
            // implies work is happening. Say what is actually true.
            body.innerHTML = `<tr><td colspan="6" class="text-muted" style="text-align:center;padding:16px">${_gatedCount.toLocaleString()} candidate${_gatedCount === 1 ? '' : 's'} not yet verified — use <strong>Verify</strong> above.</td></tr>`;
            return;
        }
        const scope = _captureType ? ` for <strong>${escapeHtml(_captureType)}</strong>` : '';
        body.innerHTML = `<tr><td colspan="6" class="text-muted" style="text-align:center;padding:16px">No real changes${scope} in the ${escapeHtml(windowLabel())} (volatility filtered out).</td></tr>`;
        return;
    }

    const rows = [..._rows].sort((a, b) =>
        (b.at || '').localeCompare(a.at || '') ||
        (KIND_RANK[a.kind] ?? 9) - (KIND_RANK[b.kind] ?? 9)
    );

    body.innerHTML = rows.map(r => {
        const [kcls, klabel] = KIND_BADGE[r.kind] || ['badge-neutral', r.kind];
        const devCell = (r.device_id != null)
            ? `<a href="${diffHref(r)}" class="mono fw-500" style="color:var(--blue)">${escapeHtml(r.device)}</a>`
            : `<span class="mono fw-500" title="not in inventory">${escapeHtml(r.device)}</span>`;

        return `
            <tr>
                <td class="text-faint fs-11" title="${escapeHtml(r.at || '')}">${timeAgo(r.at)}</td>
                <td>${devCell}</td>
                <td class="text-faint">${escapeHtml(r.site || '')}</td>
                <td><code class="mono fs-11">${escapeHtml(r.capture_type)}</code></td>
                <td><span class="badge ${kcls}">${klabel}</span></td>
                <td class="mono fs-11" style="text-align:right">${magnitude(r)}</td>
            </tr>
        `;
    }).join('');
}


// Compact change magnitude. Structured mode counts records; text mode counts
// lines — the tooltip disambiguates.
function magnitude(r) {
    if (r.kind !== 'drift') return '<span class="text-faint">—</span>';
    const unit = r.mode === 'text' ? 'lines' : 'records';
    const parts = [];
    if (r.added)   parts.push(`<span style="color:var(--green,#16a34a)">+${r.added}</span>`);
    if (r.removed) parts.push(`<span style="color:var(--red,#dc2626)">−${r.removed}</span>`);
    if (r.changed) parts.push(`<span style="color:var(--orange,#d97706)">~${r.changed}</span>`);
    if (!parts.length) return '<span class="text-faint">—</span>';
    return `<span title="${unit}">${parts.join(' ')}</span>`;
}


function diffHref(r) {
    const parts = ['#/device', r.device_id, 'diff', encodeURIComponent(r.capture_type)];
    if (r.version_id) {
        parts.push(r.version_id);
        if (r.prev_version_id) parts.push(r.prev_version_id);
    }
    return parts.join('/');
}


function windowLabel() {
    const w = WINDOWS.find(([v]) => v === _since);
    return w ? w[1] : _since;
}


function updateScope() {
    const el = document.getElementById('chg-scope');
    if (el) el.textContent = _busy ? 'working…' : windowLabel();
}


export function destroy() {
    _loadToken++;      // stop any running verify loop
    _captureType = 'config';
    _since = DEFAULT_SINCE;
    _site = '';
    _kind = '';
    _rows = [];
    _busy = false;
    _verifying = false;
    _progDone = 0;
    _progTotal = 0;
}