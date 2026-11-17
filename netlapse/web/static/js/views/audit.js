/**
 * Parse Audit View
 *
 * Collection-centric parse inspection. Pick a capture type and see how the
 * parser is doing across the whole fleet — matched template, score, records,
 * status — sorted worst-first. Then reparse stored raw output against the
 * current template database, with a dry-run preview before persisting.
 *
 * Data sources:
 *   /api/v1/search/capture_types   (type selector)
 *   /api/v1/sites                  (site filter)
 *   /api/v1/parse-audit            (fleet audit)
 *   /api/v1/parse-audit/reparse    (preview + apply)
 */

import { api } from '../api.js';
import { escapeHtml, icons } from '../components.js';

let _captureTypes = ['config'];
let _captureType = '';
let _sites = [];
let _rows = [];
let _summary = {};
let _preview = {};          // device_id → { new, delta }
let _selected = new Set();
let _problemsOnly = false;
let _busy = false;

const STATUS_BADGE = {
    ok:       ['badge-success', 'ok'],
    low:      ['badge-warning', 'low'],
    miss:     ['badge-error',   'miss'],
    raw_only: ['badge-neutral', 'raw'],
    no_data:  ['badge-neutral', 'no data'],
};

const DELTA_BADGE = {
    improved:   ['badge-success', 'improved'],
    recovered:  ['badge-success', 'recovered'],
    regressed:  ['badge-error',   'regressed'],
    cleared:    ['badge-warning', 'cleared'],
    unchanged:  ['badge-neutral', 'unchanged'],
    still_miss: ['badge-neutral', 'still miss'],
};

const PROBLEM_STATUSES = new Set(['miss', 'low', 'no_data']);


export function render() {
    return `
        <div class="pill-row" id="audit-pills" style="margin-bottom:14px">
            <span class="text-faint fs-11">Loading capture types…</span>
        </div>

        <div class="card-head" style="justify-content:space-between;margin-bottom:12px">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <select class="select" id="audit-site" style="width:160px">
                    <option value="">All sites</option>
                </select>
                <label class="form-label-inline" style="display:flex;gap:6px;align-items:center;font-size:12px;color:var(--text-faint)">
                    <input type="checkbox" id="audit-problems"> Problems only
                </label>
            </div>
            <div style="display:flex;gap:8px;align-items:center">
                <span class="mono text-faint fs-11" id="audit-scope"></span>
                <button class="btn btn-sm" id="audit-refresh">${icons.refresh} Refresh</button>
                <button class="btn btn-sm" id="audit-preview">${icons.play} Preview Reparse</button>
                <button class="btn btn-sm btn-primary" id="audit-apply">Apply Reparse</button>
            </div>
        </div>

        <div class="audit-summary" id="audit-summary"></div>

        <div class="card" style="margin-top:12px">
            <div class="table-wrap" style="max-height:560px;overflow:auto">
                <table>
                    <thead><tr>
                        <th style="width:28px"><input type="checkbox" id="audit-all"></th>
                        <th>Device</th>
                        <th>Site</th>
                        <th>Platform</th>
                        <th>Template</th>
                        <th style="text-align:right">Score</th>
                        <th style="text-align:right">Recs</th>
                        <th>Status</th>
                        <th id="audit-newcol" style="display:none">Reparse &rarr;</th>
                    </tr></thead>
                    <tbody id="audit-body">
                        <tr><td colspan="9" class="loading">Select a capture type…</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    `;
}


export async function init() {
    // Capture types
    try {
        const t = await api.captureTypes();
        if (Array.isArray(t) && t.length) _captureTypes = t;
    } catch { /* keep default */ }

    // Default to the first non-config type if there is one (config doesn't
    // parse), else the first type available.
    _captureType = _captureTypes.find(t => t !== 'config') || _captureTypes[0];

    renderPills();

    // Sites
    try {
        _sites = await api.sites();
        const sel = document.getElementById('audit-site');
        sel.innerHTML = '<option value="">All sites</option>' +
            _sites.map(s =>
                `<option value="${escapeHtml(s.slug)}">${escapeHtml(s.name || s.slug)}</option>`
            ).join('');
    } catch { /* no site filter */ }

    document.getElementById('audit-site').addEventListener('change', loadAudit);
    document.getElementById('audit-problems').addEventListener('change', (e) => {
        _problemsOnly = e.target.checked;
        renderTable();
    });
    document.getElementById('audit-refresh').addEventListener('click', loadAudit);
    document.getElementById('audit-preview').addEventListener('click', () => runReparse(false));
    document.getElementById('audit-apply').addEventListener('click', () => runReparse(true));
    document.getElementById('audit-all').addEventListener('change', toggleSelectAll);

    await loadAudit();
}


function renderPills() {
    const el = document.getElementById('audit-pills');
    el.innerHTML = _captureTypes.map(t =>
        `<button class="pill${t === _captureType ? ' active' : ''}" data-type="${escapeHtml(t)}">${escapeHtml(t)}</button>`
    ).join('');
    el.querySelectorAll('.pill').forEach(btn => {
        btn.addEventListener('click', () => {
            if (_busy || btn.dataset.type === _captureType) return;
            _captureType = btn.dataset.type;
            renderPills();
            loadAudit();
        });
    });
}


async function loadAudit() {
    // Switching scope invalidates any preview / selection
    _preview = {};
    _selected.clear();

    const site = document.getElementById('audit-site').value;
    const body = document.getElementById('audit-body');
    body.innerHTML = '<tr><td colspan="9" class="loading">Loading…</td></tr>';
    document.getElementById('audit-newcol').style.display = 'none';

    try {
        const params = { capture_type: _captureType };
        if (site) params.site = site;
        const data = await api.parseAudit.audit(params);
        _rows = data.rows || [];
        _summary = data.summary || {};
        renderSummary(data);
        renderTable();
    } catch (e) {
        _rows = [];
        body.innerHTML = `<tr><td colspan="9" class="form-error">${escapeHtml(e.message)}</td></tr>`;
        document.getElementById('audit-summary').innerHTML = '';
    }
    updateScopeHint();
}


function renderSummary(data) {
    const s = data.summary || {};
    const chips = [
        ['ok',       'OK',      'badge-success'],
        ['low',      'Low',     'badge-warning'],
        ['miss',     'Miss',    'badge-error'],
        ['raw_only', 'Raw',     'badge-neutral'],
        ['no_data',  'No data', 'badge-neutral'],
    ];
    const th = data.thresholds || {};
    document.getElementById('audit-summary').innerHTML = `
        <div class="audit-chips">
            <span class="mono fw-500" style="margin-right:4px">${data.total} devices</span>
            ${chips.map(([k, label, cls]) =>
                `<span class="badge ${cls}">${label} ${s[k] || 0}</span>`
            ).join('')}
            <span class="text-faint fs-11" style="margin-left:auto">
                ok &ge; ${th.ok_score} &middot; miss &lt; ${th.min_score}
            </span>
        </div>
    `;
}


function visibleRows() {
    return _problemsOnly
        ? _rows.filter(r => PROBLEM_STATUSES.has(r.status))
        : _rows;
}


function renderTable() {
    const body = document.getElementById('audit-body');
    const rows = visibleRows();
    const showNew = Object.keys(_preview).length > 0;
    document.getElementById('audit-newcol').style.display = showNew ? '' : 'none';

    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="9" class="text-muted" style="text-align:center;padding:16px">No ${_problemsOnly ? 'problem ' : ''}rows for <strong>${escapeHtml(_captureType)}</strong>.</td></tr>`;
        return;
    }

    body.innerHTML = rows.map(r => {
        const [scls, slabel] = STATUS_BADGE[r.status] || ['badge-neutral', r.status];
        const checked = _selected.has(r.device_id) ? 'checked' : '';
        const score = r.template_score != null ? r.template_score : '—';
        const tplCell = r.template_name
            ? `<code class="mono fs-11">${escapeHtml(r.template_name)}</code>${
                r.family_mismatch
                    ? ` <span class="badge badge-warning" title="Template family doesn't match capture type — likely a false positive" style="font-size:10px">mismatch</span>`
                    : ''}`
            : '<span class="text-faint">—</span>';

        let newCell = '';
        if (showNew) {
            const pv = _preview[r.device_id];
            if (pv) {
                const [dcls, dlabel] = DELTA_BADGE[pv.delta] || ['badge-neutral', pv.delta];
                const nt = pv.new.template
                    ? `<code class="mono fs-11">${escapeHtml(pv.new.template)}</code>`
                    : '<span class="text-faint">no match</span>';
                const ns = pv.new.score != null ? pv.new.score : '—';
                newCell = `<td>${nt} <span class="text-faint fs-11">(${ns})</span> <span class="badge ${dcls}" style="font-size:10px">${dlabel}</span></td>`;
            } else {
                newCell = '<td class="text-faint">—</td>';
            }
        }

        return `
            <tr class="${r.family_mismatch ? 'audit-row-warn' : ''}">
                <td><input type="checkbox" class="audit-check" data-id="${r.device_id}" ${checked}></td>
                <td><a href="#/device/${r.device_id}" class="mono fw-500" style="color:var(--blue)">${escapeHtml(r.device)}</a></td>
                <td class="text-faint">${escapeHtml(r.site || '')}</td>
                <td class="text-faint fs-11">${escapeHtml(r.platform || '')}</td>
                <td>${tplCell}</td>
                <td class="mono" style="text-align:right">${score}</td>
                <td class="mono text-faint" style="text-align:right">${r.record_count}</td>
                <td><span class="badge ${scls}">${slabel}</span></td>
                ${newCell}
            </tr>
        `;
    }).join('');

    body.querySelectorAll('.audit-check').forEach(cb => {
        cb.addEventListener('change', () => {
            const id = parseInt(cb.dataset.id);
            if (cb.checked) _selected.add(id); else _selected.delete(id);
            updateScopeHint();
        });
    });
}


function toggleSelectAll(e) {
    const on = e.target.checked;
    _selected.clear();
    if (on) visibleRows().forEach(r => _selected.add(r.device_id));
    document.querySelectorAll('.audit-check').forEach(cb => {
        cb.checked = on;
    });
    updateScopeHint();
}


function updateScopeHint() {
    const el = document.getElementById('audit-scope');
    if (!el) return;
    el.textContent = _selected.size
        ? `${_selected.size} selected`
        : 'all devices';
}


async function runReparse(persist) {
    if (_busy) return;
    if (persist && !confirm(
        `Apply reparse for "${_captureType}"` +
        (_selected.size ? ` on ${_selected.size} selected device(s)` : ' across all devices') +
        `?\n\nThis overwrites stored parsed data (raw output is untouched).`
    )) return;

    _busy = true;
    const previewBtn = document.getElementById('audit-preview');
    const applyBtn = document.getElementById('audit-apply');
    const activeBtn = persist ? applyBtn : previewBtn;
    const original = activeBtn.innerHTML;
    previewBtn.disabled = true;
    applyBtn.disabled = true;
    activeBtn.textContent = persist ? 'Applying…' : 'Previewing…';

    try {
        const payload = { capture_type: _captureType, persist };
        if (_selected.size) payload.device_ids = [..._selected];
        const site = document.getElementById('audit-site').value;
        if (site && !_selected.size) payload.site = site;

        const data = await api.parseAudit.reparse(payload);

        if (persist) {
            await loadAudit();
            flashSummary(data, true);
        } else {
            _preview = {};
            (data.results || []).forEach(rr => {
                _preview[rr.device_id] = { new: rr.new, delta: rr.delta };
            });
            renderTable();
            flashSummary(data, false);
        }
    } catch (e) {
        alert('Reparse failed: ' + e.message);
    } finally {
        _busy = false;
        previewBtn.disabled = false;
        applyBtn.disabled = false;
        activeBtn.innerHTML = original;
    }
}


function flashSummary(data, persisted) {
    const s = data.summary || {};
    const parts = [];
    if (s.improved)  parts.push(`${s.improved} improved`);
    if (s.recovered) parts.push(`${s.recovered} recovered`);
    if (s.cleared)   parts.push(`${s.cleared} cleared`);
    if (s.regressed) parts.push(`${s.regressed} regressed`);
    if (s.unchanged) parts.push(`${s.unchanged} unchanged`);
    if (s.still_miss) parts.push(`${s.still_miss} still miss`);
    if (s.skipped)   parts.push(`${s.skipped} skipped`);

    const el = document.getElementById('audit-summary');
    const verb = persisted
        ? `Applied — ${data.persisted} written.`
        : `Preview only (nothing written).`;
    const banner = document.createElement('div');
    banner.className = 'audit-flash';
    banner.innerHTML = `<strong>${verb}</strong> ${parts.join(' · ') || 'no changes'}`;
    el.prepend(banner);
    setTimeout(() => banner.remove(), 6000);
}


export function destroy() {
    _captureType = '';
    _rows = [];
    _summary = {};
    _preview = {};
    _selected = new Set();
    _problemsOnly = false;
    _busy = false;
}