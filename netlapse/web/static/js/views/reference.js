/**
 * Reference Data View
 *
 * CRUD for the four DCIM attribute tables — sites, platforms, roles,
 * manufacturers — the things the map importer used to be the only way to
 * create. One table + modal, reused across all four kinds via per-kind
 * descriptors that mirror the backend's _REFERENCE_TABLES.
 *
 * Data source: /api/v1/admin/dcim/{kind}
 */

import { api } from '../api.js';
import { escapeHtml, icons } from '../components.js';

// ── Per-kind descriptors ──────────────────────────────────────────────────
// columns: what the table shows. fields: what the create/edit modal renders.
// ref_label: how the usage count column is titled (devices vs platforms).
const KINDS = {
    sites: {
        label: 'Sites', singular: 'site', ref_label: 'Devices',
        columns: ['slug', 'name', 'status', 'facility'],
        fields: [
            { key: 'name', label: 'Name', required: true, mono: true },
            { key: 'slug', label: 'Slug', mono: true, hint: 'auto from name; the storage path — locked once devices exist' },
            { key: 'status', label: 'Status', type: 'select', options: ['active', 'planned', 'staged', 'decommissioned'], default: 'active' },
            { key: 'facility', label: 'Facility' },
            { key: 'physical_address', label: 'Address' },
            { key: 'time_zone', label: 'Time zone' },
            { key: 'description', label: 'Description' },
        ],
    },
    platforms: {
        label: 'Platforms', singular: 'platform', ref_label: 'Devices',
        columns: ['slug', 'name', 'platform_profile', 'paging_disable_command', 'legacy_ssh'],
        fields: [
            { key: 'name', label: 'Name', required: true, mono: true },
            { key: 'slug', label: 'Slug', mono: true, hint: 'auto from name' },
            { key: 'platform_profile', label: 'Parse profile', mono: true, placeholder: 'cisco_ios, arista_eos, juniper_junos…' },
            { key: 'manufacturer_id', label: 'Manufacturer', type: 'ref-select', ref: 'manufacturers' },
            { key: 'paging_disable_command', label: 'Disable-paging cmd', mono: true, placeholder: 'terminal length 0' },
            { key: 'enable_command', label: 'Enable cmd', mono: true, placeholder: 'enable (blank = none)' },
            { key: 'prompt_regex', label: 'Prompt regex', mono: true },
            { key: 'legacy_ssh', label: 'Legacy SSH', type: 'select', options: [['0', 'Off'], ['1', 'On']], default: '0' },
            { key: 'description', label: 'Description' },
        ],
    },
    roles: {
        label: 'Roles', singular: 'role', ref_label: 'Devices',
        columns: ['slug', 'name', 'color'],
        fields: [
            { key: 'name', label: 'Name', required: true, mono: true },
            { key: 'slug', label: 'Slug', mono: true, hint: 'auto from name' },
            { key: 'color', label: 'Color (hex)', mono: true, placeholder: '9e9e9e' },
            { key: 'description', label: 'Description' },
        ],
    },
    manufacturers: {
        label: 'Manufacturers', singular: 'manufacturer', ref_label: 'Platforms',
        columns: ['slug', 'name'],
        fields: [
            { key: 'name', label: 'Name', required: true, mono: true },
            { key: 'slug', label: 'Slug', mono: true, hint: 'auto from name' },
            { key: 'description', label: 'Description' },
        ],
    },
};

const COL_LABELS = {
    slug: 'Slug', name: 'Name', status: 'Status', facility: 'Facility',
    platform_profile: 'Profile', paging_disable_command: 'Disable paging',
    legacy_ssh: 'Legacy SSH', color: 'Color',
};

let _kind = 'sites';
let _rows = [];
let _editingId = null;
let _mfrCache = null;   // manufacturers list, for the platform form's dropdown

// ══════════════════════════════════════════════════════════════ Render

export function render() {
    const tabs = Object.entries(KINDS).map(([k, d]) =>
        `<button class="btn btn-sm${k === _kind ? ' btn-primary' : ''}" data-kind="${k}">${d.label}</button>`
    ).join('');

    return `
        <div class="filter-row">
            <div class="ref-tabs">${tabs}</div>
            <span class="ml-auto text-faint mono fs-12" id="ref-count"></span>
            <button class="btn btn-sm btn-primary" id="ref-add-btn">${icons.plus} Add</button>
        </div>
        <div class="card">
            <div class="table-wrap">
                <table>
                    <thead><tr id="ref-head"></tr></thead>
                    <tbody id="ref-body"></tbody>
                </table>
            </div>
        </div>

        <div class="modal-overlay" id="ref-modal" style="display:none">
            <div class="modal-container" style="max-width:560px">
                <div class="modal-header">
                    <div class="modal-title" id="ref-modal-title">Add</div>
                    <button class="modal-close" id="ref-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="ref-modal-body"></div>
                    <div id="ref-modal-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="ref-modal-cancel">Cancel</button>
                        <button class="btn btn-primary" id="ref-modal-save">Save</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    document.querySelectorAll('[data-kind]').forEach(btn =>
        btn.addEventListener('click', () => switchKind(btn.dataset.kind)));
    document.getElementById('ref-add-btn').addEventListener('click', () => openModal(null));
    document.getElementById('ref-modal-close').addEventListener('click', closeModal);
    document.getElementById('ref-modal-cancel').addEventListener('click', closeModal);
    document.getElementById('ref-modal-save').addEventListener('click', save);
    document.getElementById('ref-modal').addEventListener('click', (e) => {
        if (e.target.id === 'ref-modal') closeModal();
    });

    await loadKind();
}

export function destroy() {
    _rows = []; _editingId = null; _mfrCache = null; _kind = 'sites';
}

// ══════════════════════════════════════════════════════════════ Data + table

function switchKind(kind) {
    if (kind === _kind || !KINDS[kind]) return;
    _kind = kind;
    document.querySelectorAll('[data-kind]').forEach(btn =>
        btn.classList.toggle('btn-primary', btn.dataset.kind === kind));
    loadKind();
}

async function loadKind() {
    const body = document.getElementById('ref-body');
    body.innerHTML = '<tr><td colspan="9" class="loading">Loading…</td></tr>';
    try {
        const data = await api.admin.dcim.list(_kind);
        _rows = data[_kind] || [];
        renderTable();
    } catch (e) {
        body.innerHTML = `<tr><td colspan="9" class="form-error">${escapeHtml(e.message)}</td></tr>`;
    }
}

function renderTable() {
    const desc = KINDS[_kind];
    const cols = desc.columns;

    document.getElementById('ref-count').textContent =
        `${_rows.length} ${desc.label.toLowerCase()}`;

    document.getElementById('ref-head').innerHTML =
        cols.map(c => `<th>${escapeHtml(COL_LABELS[c] || c)}</th>`).join('') +
        `<th>${desc.ref_label}</th><th></th>`;

    const body = document.getElementById('ref-body');
    if (!_rows.length) {
        body.innerHTML = `<tr><td colspan="${cols.length + 2}" class="text-muted" style="text-align:center;padding:20px">No ${desc.label.toLowerCase()} yet. Click Add to create one.</td></tr>`;
        return;
    }

    body.innerHTML = _rows.map(r => `
        <tr>
            ${cols.map(c => `<td class="${c === 'name' ? 'fw-600' : 'fs-12'}">${cellHtml(c, r[c])}</td>`).join('')}
            <td class="text-muted fs-12">${r.reference_count ?? 0}</td>
            <td><div class="row-actions">
                <button class="btn btn-sm btn-icon" data-edit="${r.id}" title="Edit">${icons.edit}</button>
                <button class="btn btn-sm btn-icon btn-ghost" data-del="${r.id}" data-name="${escapeHtml(r.name)}" title="Delete">${icons.trash}</button>
            </div></td>
        </tr>
    `).join('');

    body.querySelectorAll('[data-edit]').forEach(btn =>
        btn.addEventListener('click', () => openModal(
            _rows.find(r => r.id === parseInt(btn.dataset.edit)))));
    body.querySelectorAll('[data-del]').forEach(btn =>
        btn.addEventListener('click', () => remove(parseInt(btn.dataset.del), btn.dataset.name)));
}

function cellHtml(col, val) {
    if (val === null || val === undefined || val === '') return '<span class="text-faint">—</span>';
    if (col === 'color') {
        const c = String(val).replace(/[^0-9a-fA-F]/g, '').slice(0, 6) || '9e9e9e';
        return `<span class="ref-swatch" style="background:#${c}"></span><span class="mono fs-11">${escapeHtml(val)}</span>`;
    }
    if (col === 'legacy_ssh') return val ? '<span class="mono fs-11">on</span>' : '<span class="text-faint">—</span>';
    const mono = ['slug', 'platform_profile', 'paging_disable_command'].includes(col);
    return `<span class="${mono ? 'mono fs-11' : ''}">${escapeHtml(String(val))}</span>`;
}

// ══════════════════════════════════════════════════════════════ Modal CRUD

async function openModal(row) {
    _editingId = row ? row.id : null;
    const desc = KINDS[_kind];

    document.getElementById('ref-modal-title').textContent =
        (row ? 'Edit ' : 'Add ') + desc.singular;
    document.getElementById('ref-modal-error').style.display = 'none';
    document.getElementById('ref-modal').style.display = 'flex';

    const bodyEl = document.getElementById('ref-modal-body');
    bodyEl.innerHTML = '<div class="loading">Loading…</div>';

    // Platform form needs the manufacturer list for its dropdown.
    if (desc.fields.some(f => f.type === 'ref-select' && f.ref === 'manufacturers')) {
        if (!_mfrCache) {
            try { _mfrCache = (await api.admin.dcim.list('manufacturers')).manufacturers || []; }
            catch { _mfrCache = []; }
        }
    }

    bodyEl.innerHTML = `<div class="ref-form">${desc.fields.map(f => fieldHtml(f, row)).join('')}</div>`;
    setTimeout(() => { const n = document.getElementById('ref-f-name'); if (n) n.focus(); }, 80);
}

function fieldHtml(f, row) {
    const id = `ref-f-${f.key}`;
    const val = row ? row[f.key] : (f.default ?? '');
    const reqMark = f.required ? ' <span class="text-error">*</span>' : '';
    const hint = f.hint ? `<span class="text-faint fs-11"> — ${escapeHtml(f.hint)}</span>` : '';

    // Lock a site's slug once devices reference it (storage path is immutable).
    const slugLocked = (f.key === 'slug' && _kind === 'sites' && row && (row.reference_count ?? 0) > 0);

    let control;
    if (f.type === 'select') {
        const opts = f.options.map(o => {
            const [ov, ol] = Array.isArray(o) ? o : [o, o];
            const sel = String(val) === String(ov) ? ' selected' : '';
            return `<option value="${escapeHtml(String(ov))}"${sel}>${escapeHtml(ol)}</option>`;
        }).join('');
        control = `<select class="select" id="${id}">${opts}</select>`;
    } else if (f.type === 'ref-select') {
        const items = _mfrCache || [];
        const opts = ['<option value="">— none —</option>'].concat(
            items.map(it => `<option value="${it.id}"${row && row[f.key] == it.id ? ' selected' : ''}>${escapeHtml(it.name)}</option>`)
        ).join('');
        control = `<select class="select" id="${id}">${opts}</select>`;
    } else {
        control = `<input class="input${f.mono ? ' mono' : ''}" id="${id}" type="text"`
            + ` value="${escapeHtml(val == null ? '' : String(val))}"`
            + ` placeholder="${escapeHtml(f.placeholder || '')}" autocomplete="off"`
            + (slugLocked ? ' disabled' : '') + '>';
    }

    const lockNote = slugLocked
        ? '<span class="text-faint fs-11"> — locked: devices present</span>' : '';

    return `
        <div class="form-group">
            <label class="form-label">${escapeHtml(f.label)}${reqMark}${hint}${lockNote}</label>
            ${control}
        </div>`;
}

function collectForm() {
    const out = {};
    for (const f of KINDS[_kind].fields) {
        const el = document.getElementById(`ref-f-${f.key}`);
        if (!el || el.disabled) continue;          // skip locked (e.g. site slug)
        let v = el.value;
        if (typeof v === 'string') v = v.trim();
        if (f.key === 'legacy_ssh') v = v === '' ? 0 : parseInt(v);
        else if (f.key === 'manufacturer_id') v = v === '' ? null : parseInt(v);
        else if (v === '') v = null;
        out[f.key] = v;
    }
    return out;
}

async function save() {
    const errEl = document.getElementById('ref-modal-error');
    errEl.style.display = 'none';

    const payload = collectForm();
    if (!payload.name) {
        errEl.textContent = 'Name is required.';
        errEl.style.display = 'block';
        return;
    }

    const btn = document.getElementById('ref-modal-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
        if (_editingId) await api.admin.dcim.update(_kind, _editingId, payload);
        else await api.admin.dcim.create(_kind, payload);
        // A new/edited manufacturer invalidates the platform-form cache.
        if (_kind === 'manufacturers') _mfrCache = null;
        closeModal();
        await loadKind();
    } catch (e) {
        errEl.textContent = e.message || 'Save failed';
        errEl.style.display = 'block';
    } finally {
        btn.disabled = false; btn.textContent = 'Save';
    }
}

async function remove(id, name) {
    if (!confirm(`Delete ${KINDS[_kind].singular} "${name}"?`)) return;
    try {
        await api.admin.dcim.remove(_kind, id);
        if (_kind === 'manufacturers') _mfrCache = null;
        await loadKind();
    } catch (e) {
        // 409 = still referenced; the message explains how many and what to do.
        alert('Delete failed: ' + e.message);
    }
}

function closeModal() {
    document.getElementById('ref-modal').style.display = 'none';
    _editingId = null;
}