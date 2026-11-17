/**
 * Jobs View
 *
 * All collection jobs with status, filters, and run triggers.
 * Full CRUD: create, edit, enable/disable, delete.
 * Data source: /api/v1/jobs
 * Auto-refreshes every 10s to pick up scheduler updates.
 * Subscribes to WebSocket for immediate status changes.
 */

import { api } from '../api.js';
import { ws } from '../ws.js';
import { badge, code, formatInterval, timeAgo, icons, escapeHtml } from '../components.js';

let refreshTimer = null;
let unsubs = [];
let _sites = [];       // cached for filter dropdowns
let _platforms = [];   // cached for command map editor
let _captureTypes = [];// configured capture types (for the Capture Type select)
let _modalOpen = false; // pause auto-refresh while editing

// ══════════════════════════════════════════════════════════════
// Render
// ══════════════════════════════════════════════════════════════

export function render() {
    return `
        <div class="card">
            <div class="filter-row" style="justify-content:flex-end;margin-bottom:0;padding:12px 16px;border-bottom:1px solid var(--border)">
                <button class="btn btn-primary btn-sm" id="btn-new-job">+ New Job</button>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Job</th><th>Capture</th><th>Commands</th>
                        <th>Interval</th><th>Filters</th><th>Last Run</th>
                        <th>Collected</th><th>Duration</th><th>Status</th><th></th>
                    </tr></thead>
                    <tbody id="jobs-body">
                        <tr><td colspan="10" class="loading">Loading…</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Job edit/create modal -->
        <div class="modal-overlay" id="job-modal" style="display:none">
            <div class="modal-container" style="max-width:640px">
                <div class="modal-header">
                    <div class="modal-title" id="job-modal-title">New Job</div>
                    <button class="modal-close" id="job-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="job-form-body"></div>
                    <div id="job-form-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="job-form-cancel">Cancel</button>
                        <button class="btn btn-primary" id="job-form-save">Save</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Confirm delete modal -->
        <div class="modal-overlay" id="delete-modal" style="display:none">
            <div class="modal-container" style="max-width:420px">
                <div class="modal-header">
                    <div class="modal-title">Delete Job</div>
                    <button class="modal-close" id="delete-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <p id="delete-msg" style="margin:0 0 16px 0;font-size:13px;color:var(--text-muted)"></p>
                    <div class="form-actions">
                        <button class="btn" id="delete-cancel">Cancel</button>
                        <button class="btn btn-danger" id="delete-confirm">Delete</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}


// ══════════════════════════════════════════════════════════════
// Init
// ══════════════════════════════════════════════════════════════

export async function init() {
    // Pre-fetch sites and platforms for form dropdowns
    _sites = await api.sites().catch(() => []);
    _platforms = await api.platforms().catch(() => []);
    // Configured capture types (distinct capture_type across jobs) — the
    // assignable set, distinct from /search/capture_types (collected on disk).
    _captureTypes = await api.jobCaptureTypes().catch(() => []);

    await populateTable();

    // Auto-refresh every 10s (paused when modal is open)
    refreshTimer = setInterval(() => {
        if (!_modalOpen) populateTable();
    }, 10000);

    // Immediate refresh on collection events
    ws.connect();
    unsubs.push(ws.on('collection_start', () => { if (!_modalOpen) populateTable(); }));
    unsubs.push(ws.on('collection_complete', () => { if (!_modalOpen) populateTable(); }));

    // New job button
    document.getElementById('btn-new-job').addEventListener('click', () => openJobModal(null));

    // Modal chrome
    document.getElementById('job-modal-close').addEventListener('click', closeJobModal);
    document.getElementById('job-form-cancel').addEventListener('click', closeJobModal);
    document.getElementById('job-modal').addEventListener('click', (e) => {
        if (e.target.id === 'job-modal') closeJobModal();
    });
    document.getElementById('job-form-save').addEventListener('click', saveJob);

    // Delete modal chrome
    document.getElementById('delete-modal-close').addEventListener('click', closeDeleteModal);
    document.getElementById('delete-cancel').addEventListener('click', closeDeleteModal);
    document.getElementById('delete-modal').addEventListener('click', (e) => {
        if (e.target.id === 'delete-modal') closeDeleteModal();
    });
}


// ══════════════════════════════════════════════════════════════
// Table population
// ══════════════════════════════════════════════════════════════

async function populateTable() {
    const jobs = await api.jobs().catch(() => []);
    const body = document.getElementById('jobs-body');
    if (!body) return;

    if (!jobs.length) {
        body.innerHTML = '<tr><td colspan="10" class="text-muted" style="text-align:center;padding:20px">No jobs configured</td></tr>';
        return;
    }

    body.innerHTML = jobs.map(j => {
        const enabled = j.enabled !== false && j.enabled !== 0;
        const collected = j.devices_collected || 0;
        const failed = j.devices_failed || 0;
        const isFile = j.source === 'file';
        const fileBadge = isFile
            ? `<span class="filter-pill filter-pill-blue" style="margin-left:6px;font-size:9px" title="Defined in the YAML catalog — read-only here; edit the files and re-sync">file</span>`
            : '';

        // Parse commands — may be JSON string or array
        let cmds = j.commands;
        if (typeof cmds === 'string') {
            try { cmds = JSON.parse(cmds); } catch { cmds = [cmds]; }
        }
        const cmdStr = Array.isArray(cmds) ? cmds.join(', ') : String(cmds || '—');

        // Check for per-platform command overrides
        let cmdMapInfo = '';
        if (j.command_map) {
            let map = j.command_map;
            if (typeof map === 'string') { try { map = JSON.parse(map); } catch { map = null; } }
            if (map && Object.keys(map).length) {
                cmdMapInfo = `<span class="filter-pill filter-pill-blue" style="margin-left:4px;font-size:9px" title="${Object.keys(map).join(', ')}">${Object.keys(map).length} platform${Object.keys(map).length > 1 ? 's' : ''}</span>`;
            }
        }

        // Filters
        let filters = '';
        if (j.site_filter) filters += `<span class="filter-pill filter-pill-blue">site:${escapeHtml(j.site_filter)}</span>`;
        if (j.platform_filter) filters += `<span class="filter-pill filter-pill-purple">platform:${escapeHtml(j.platform_filter)}</span>`;
        if (j.role_filter) filters += `<span class="filter-pill filter-pill-green">role:${escapeHtml(j.role_filter)}</span>`;
        if (!filters) filters = '<span class="text-ghost">—</span>';

        return `
            <tr style="${enabled ? '' : 'opacity:0.45'}" data-slug="${escapeHtml(j.slug)}">
                <td>
                    <div class="fw-600">${escapeHtml(j.name || j.slug)}${fileBadge}</div>
                    <code class="mono text-faint" style="font-size:10px">${escapeHtml(j.slug)}</code>
                </td>
                <td>${code(j.capture_type)}</td>
                <td class="mono text-muted fs-11" style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(cmdStr)}">${escapeHtml(cmdStr)}${cmdMapInfo}</td>
                <td class="mono fs-12">${formatInterval(j.interval_seconds)}</td>
                <td class="fs-11">${filters}</td>
                <td class="text-muted fs-12">${timeAgo(j.last_run)}</td>
                <td class="mono fs-12">${collected}<span class="text-ghost">/${collected + failed}</span></td>
                <td class="mono fs-12">${j.duration_seconds != null ? j.duration_seconds + 's' : '—'}</td>
                <td>${badge(enabled ? (j.last_status || 'unknown') : 'disabled')}</td>
                <td class="row-actions">
                    <button class="btn btn-sm job-run-btn" data-slug="${escapeHtml(j.slug)}" title="Run now">${icons.play}</button>
                    <button class="btn btn-sm job-edit-btn" data-slug="${escapeHtml(j.slug)}" title="${isFile ? 'View (file-managed)' : 'Edit'}">${icons.edit}</button>
                    <button class="btn btn-sm btn-icon job-toggle-btn ${enabled ? 'toggle-on' : ''}" data-slug="${escapeHtml(j.slug)}" data-enabled="${enabled ? '1' : '0'}" title="${enabled ? 'Disable' : 'Enable'}">${enabled ? icons.toggleOn : icons.toggleOff}</button>
                    ${isFile
                        ? `<button class="btn btn-sm btn-icon btn-ghost" disabled style="opacity:0.3;cursor:not-allowed" title="File-managed — remove it from jobs.yaml and re-sync to retire it">${icons.trash}</button>`
                        : `<button class="btn btn-sm btn-icon btn-ghost job-delete-btn" data-slug="${escapeHtml(j.slug)}" data-name="${escapeHtml(j.name || j.slug)}" title="Delete">${icons.trash}</button>`}
                </td>
            </tr>`;
    }).join('');

    wireRowButtons(body);
}

function wireRowButtons(body) {
    // Run buttons
    body.querySelectorAll('.job-run-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            btn.disabled = true;
            btn.textContent = '…';
            try { await api.triggerJob(btn.dataset.slug); }
            catch (err) { alert('Error: ' + err.message); }
            setTimeout(() => { btn.disabled = false; btn.innerHTML = `${icons.play}`; }, 9000);
        });
    });

    // Edit buttons
    body.querySelectorAll('.job-edit-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const slug = btn.dataset.slug;
            try {
                const job = await api.job(slug);
                openJobModal(job);
            } catch (err) {
                alert('Failed to load job: ' + err.message);
            }
        });
    });

    // Enable/disable toggle
    body.querySelectorAll('.job-toggle-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const slug = btn.dataset.slug;
            const currentlyEnabled = btn.dataset.enabled === '1';
            try {
                await api.setJobEnabled(slug, !currentlyEnabled);
                await populateTable();
            } catch (err) {
                alert('Error: ' + err.message);
            }
        });
    });

    // Delete buttons
    body.querySelectorAll('.job-delete-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            openDeleteModal(btn.dataset.slug, btn.dataset.name);
        });
    });
}


// ══════════════════════════════════════════════════════════════
// Job edit/create modal
// ══════════════════════════════════════════════════════════════

let _editingSlug = null; // null = creating new

function openJobModal(job) {
    _editingSlug = job ? job.slug : null;
    _modalOpen = true;
    const readOnly = !!(job && job.source === 'file');

    const title = document.getElementById('job-modal-title');
    title.textContent = job ? (readOnly ? `View: ${job.name}` : `Edit: ${job.name}`) : 'New Job';

    // Parse commands
    let cmds = '';
    if (job && job.commands) {
        let arr = job.commands;
        if (typeof arr === 'string') {
            try { arr = JSON.parse(arr); } catch { arr = [arr]; }
        }
        cmds = Array.isArray(arr) ? arr.join('\n') : String(arr);
    }

    // Parse command_map
    let cmdMap = {};
    if (job && job.command_map) {
        let raw = job.command_map;
        if (typeof raw === 'string') { try { raw = JSON.parse(raw); } catch { raw = {}; } }
        if (raw && typeof raw === 'object') cmdMap = raw;
    }
    const hasOverrides = Object.keys(cmdMap).length > 0;

    // Interval → human-friendly
    const interval = job ? job.interval_seconds || 3600 : 3600;

    // Site dropdown options
    const siteOpts = _sites.map(s =>
        `<option value="${escapeHtml(s.slug)}" ${job && job.site_filter === s.slug ? 'selected' : ''}>${escapeHtml(s.slug)} (${s.device_count || 0})</option>`
    ).join('');

    // Platform dropdown options for command map (exclude already-mapped ones later)
    const platformOptsFn = (exclude = []) => _platforms
        .filter(p => !exclude.includes(p.slug))
        .map(p => `<option value="${escapeHtml(p.slug)}">${escapeHtml(p.slug)} (${p.device_count || 0})</option>`)
        .join('');

    // Build existing override rows
    const overrideRows = Object.entries(cmdMap).map(([slug, arr]) => {
        const cmdText = Array.isArray(arr) ? arr.join('\n') : String(arr);
        return buildOverrideRow(slug, cmdText);
    }).join('');

    const formBody = document.getElementById('job-form-body');
    formBody.innerHTML = `
        <div class="form-grid">
            <div class="form-group">
                <label class="form-label">Name</label>
                <input class="input" id="jf-name" type="text" value="${escapeHtml(job ? job.name : '')}" placeholder="Config Backup">
            </div>
            <div class="form-group">
                <label class="form-label">Slug <span class="text-faint fs-11">(URL-safe ID${job ? ', read-only' : ''})</span></label>
                <input class="input mono" id="jf-slug" type="text" value="${escapeHtml(job ? job.slug : '')}" placeholder="config-backup" ${job ? 'readonly style="opacity:0.6;cursor:not-allowed"' : ''}>
            </div>
        </div>

        <div class="form-grid">
            <div class="form-group">
                <label class="form-label">Capture Type</label>
                <select class="select" id="jf-capture">
                    ${(() => {
                        const cur = job ? (job.capture_type || '') : '';
                        const opts = [..._captureTypes];
                        // Guarantee the current value is selectable even if no
                        // other job uses it yet; fall back to 'config' if empty.
                        if (cur && !opts.includes(cur)) opts.unshift(cur);
                        if (!opts.length) opts.push('config');
                        return opts.map(t =>
                            `<option value="${escapeHtml(t)}"${t === cur ? ' selected' : ''}>${escapeHtml(t)}</option>`
                        ).join('');
                    })()}
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">Interval</label>
                <div class="interval-row">
                    <input class="input" id="jf-interval-val" type="number" min="1" value="${intervalToUnit(interval).value}" style="width:80px">
                    <select class="select" id="jf-interval-unit">
                        <option value="60"    ${intervalToUnit(interval).unit === 60    ? 'selected' : ''}>minutes</option>
                        <option value="3600"  ${intervalToUnit(interval).unit === 3600  ? 'selected' : ''}>hours</option>
                        <option value="86400" ${intervalToUnit(interval).unit === 86400 ? 'selected' : ''}>days</option>
                    </select>
                </div>
            </div>
        </div>

        <div class="form-group">
            <label class="form-label">Commands <span class="text-faint fs-11">(one per line — default for all platforms)</span></label>
            <textarea class="input mono" id="jf-commands" rows="3" placeholder="show running-config">${escapeHtml(cmds)}</textarea>
        </div>

        <details class="form-details" ${hasOverrides ? 'open' : ''}>
            <summary class="form-summary">Per-Platform Overrides</summary>
            <p class="text-muted fs-11" style="margin:8px 0">Override commands for specific platforms. Devices on unlisted platforms use the default commands above.</p>
            <div id="jf-overrides">${overrideRows}</div>
            <button class="btn btn-sm" id="btn-add-override" type="button" style="margin-top:8px">+ Add Platform</button>
        </details>

        <details class="form-details" ${job && (job.site_filter || job.platform_filter || job.role_filter || job.device_filter) ? 'open' : ''}>
            <summary class="form-summary">Device Filters</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Site</label>
                    <select class="select" id="jf-site">
                        <option value="">All sites</option>
                        ${siteOpts}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Platform</label>
                    <input class="input" id="jf-platform" type="text" value="${escapeHtml(job ? job.platform_filter || '' : '')}" placeholder="cisco_ios, arista_eos">
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Role</label>
                    <input class="input" id="jf-role" type="text" value="${escapeHtml(job ? job.role_filter || '' : '')}" placeholder="router, core">
                </div>
                <div class="form-group">
                    <label class="form-label">Device Pattern <span class="text-faint fs-11">(SQL LIKE)</span></label>
                    <input class="input mono" id="jf-device" type="text" value="${escapeHtml(job ? job.device_filter || '' : '')}" placeholder="border%">
                </div>
            </div>
        </details>

        <div class="form-group" style="margin-top:12px">
            <label class="form-label-inline">
                <input type="checkbox" id="jf-enabled" ${!job || job.enabled ? 'checked' : ''}> Enabled
            </label>
        </div>
    `;

    // Wire "Add Platform" button
    document.getElementById('btn-add-override').addEventListener('click', () => {
        const container = document.getElementById('jf-overrides');
        const existing = Array.from(container.querySelectorAll('.override-platform'))
            .map(sel => sel.value).filter(Boolean);
        const row = document.createElement('div');
        row.innerHTML = buildOverrideRow('', '', existing);
        // Append only the inner children
        while (row.firstChild) container.appendChild(row.firstChild);
        wireOverrideRemove(container);
    });

    // Wire existing remove buttons
    wireOverrideRemove(document.getElementById('jf-overrides'));

    // Auto-generate slug from name for new jobs
    if (!job) {
        const nameInput = document.getElementById('jf-name');
        const slugInput = document.getElementById('jf-slug');
        nameInput.addEventListener('input', () => {
            slugInput.value = nameInput.value
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, '-')
                .replace(/(^-|-$)/g, '');
        });
    }

    // File-managed jobs are read-only here: their definition is authoritative
    // in the YAML catalog and a sync would overwrite any edit made in the UI.
    // Lock every input, hide Save, and explain where edits actually go. The
    // enable/disable toggle still lives on the table row (its own endpoint).
    const saveBtn = document.getElementById('job-form-save');
    if (readOnly) {
        const banner = document.createElement('div');
        banner.className = 'form-banner';
        banner.style.cssText = 'margin-bottom:14px;padding:10px 12px;border-radius:6px;'
            + 'background:rgba(80,140,255,0.12);border:1px solid rgba(80,140,255,0.35);'
            + 'font-size:12px;line-height:1.5';
        banner.innerHTML = 'This job is <strong>file-managed</strong> — its definition '
            + 'comes from the YAML catalog. To change it, edit '
            + '<code class="mono">captures.yaml</code> / <code class="mono">jobs.yaml</code> '
            + 'and run <code class="mono">netlapse definitions sync</code> (or restart). '
            + 'Enable/disable is available from the table toggle.';
        formBody.prepend(banner);
        formBody.querySelectorAll('input, textarea, select, button').forEach(el => {
            el.disabled = true;
        });
        saveBtn.style.display = 'none';
    } else {
        saveBtn.style.display = '';
    }

    document.getElementById('job-form-error').style.display = 'none';
    document.getElementById('job-modal').style.display = 'flex';

    // Focus first field
    setTimeout(() => {
        const first = document.getElementById('jf-name');
        if (first && !readOnly) first.focus();
    }, 100);
}

function buildOverrideRow(slug, cmdsText, excludeSlugs = []) {
    const opts = _platforms
        .filter(p => p.slug === slug || !excludeSlugs.includes(p.slug))
        .map(p => `<option value="${escapeHtml(p.slug)}" ${p.slug === slug ? 'selected' : ''}>${escapeHtml(p.slug)} (${p.device_count || 0})</option>`)
        .join('');
    return `
        <div class="override-row" style="display:flex;gap:8px;align-items:flex-start;margin-top:8px">
            <select class="select override-platform" style="min-width:140px;flex:0 0 auto">
                <option value="">Select…</option>
                ${opts}
            </select>
            <textarea class="input mono override-cmds" rows="2" placeholder="show ip bgp summary" style="flex:1">${escapeHtml(cmdsText)}</textarea>
            <button class="btn btn-sm btn-ghost override-remove" type="button" title="Remove" style="padding:4px 8px;flex:0 0 auto">&times;</button>
        </div>`;
}

function wireOverrideRemove(container) {
    container.querySelectorAll('.override-remove').forEach(btn => {
        btn.onclick = () => btn.closest('.override-row').remove();
    });
}

function closeJobModal() {
    document.getElementById('job-modal').style.display = 'none';
    _modalOpen = false;
    _editingSlug = null;
}

async function saveJob() {
    const btn = document.getElementById('job-form-save');
    const errEl = document.getElementById('job-form-error');
    errEl.style.display = 'none';

    // Gather values
    const name = document.getElementById('jf-name').value.trim();
    const slug = document.getElementById('jf-slug').value.trim();
    const capture_type = document.getElementById('jf-capture').value.trim();
    const commandsRaw = document.getElementById('jf-commands').value.trim();
    const intervalVal = parseInt(document.getElementById('jf-interval-val').value, 10) || 1;
    const intervalUnit = parseInt(document.getElementById('jf-interval-unit').value, 10) || 3600;
    const interval_seconds = intervalVal * intervalUnit;
    const site_filter = document.getElementById('jf-site').value || null;
    const platform_filter = document.getElementById('jf-platform').value.trim() || null;
    const role_filter = document.getElementById('jf-role').value.trim() || null;
    const device_filter = document.getElementById('jf-device').value.trim() || null;
    const enabled = document.getElementById('jf-enabled').checked;

    // Validate
    if (!name) return showFormError(errEl, 'Name is required');
    if (!slug) return showFormError(errEl, 'Slug is required');
    if (!capture_type) return showFormError(errEl, 'Capture type is required');
    if (!commandsRaw) return showFormError(errEl, 'At least one command is required');

    // Parse commands — one per line
    const commands = commandsRaw.split('\n').map(s => s.trim()).filter(Boolean);

    // Build command_map from override rows
    const overrideRows = document.querySelectorAll('#jf-overrides .override-row');
    let command_map = null;
    if (overrideRows.length) {
        const map = {};
        for (const row of overrideRows) {
            const plat = row.querySelector('.override-platform').value.trim();
            const cmdsRaw = row.querySelector('.override-cmds').value.trim();
            if (plat && cmdsRaw) {
                map[plat] = cmdsRaw.split('\n').map(s => s.trim()).filter(Boolean);
            }
        }
        if (Object.keys(map).length) {
            command_map = JSON.stringify(map);
        }
    }

    const payload = {
        name, capture_type, commands: JSON.stringify(commands),
        command_map,
        interval_seconds, enabled,
        site_filter, platform_filter, role_filter, device_filter,
    };

    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
        if (_editingSlug) {
            // Update — don't send slug in payload
            await api.updateJob(_editingSlug, payload);
        } else {
            // Create — include slug
            payload.slug = slug;
            await api.createJob(payload);
        }
        closeJobModal();
        await populateTable();
    } catch (err) {
        showFormError(errEl, err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save';
    }
}

function showFormError(el, msg) {
    // Try to parse API error detail
    try {
        const parsed = JSON.parse(msg.split(': ').slice(1).join(': '));
        if (parsed.detail) msg = parsed.detail;
    } catch {}
    el.textContent = msg;
    el.style.display = 'block';
}


// ══════════════════════════════════════════════════════════════
// Delete confirmation modal
// ══════════════════════════════════════════════════════════════

let _deleteSlug = null;

function openDeleteModal(slug, name) {
    _deleteSlug = slug;
    _modalOpen = true;
    document.getElementById('delete-msg').textContent =
        `Delete "${name}"? Job history will be preserved, but the job definition will be removed. This cannot be undone.`;
    document.getElementById('delete-modal').style.display = 'flex';

    const confirmBtn = document.getElementById('delete-confirm');
    // Rebind to avoid stale closures
    confirmBtn.onclick = async () => {
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Deleting…';
        try {
            await api.deleteJob(_deleteSlug);
            closeDeleteModal();
            await populateTable();
        } catch (err) {
            alert('Delete failed: ' + err.message);
        } finally {
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Delete';
        }
    };
}

function closeDeleteModal() {
    document.getElementById('delete-modal').style.display = 'none';
    _modalOpen = false;
    _deleteSlug = null;
}


// ══════════════════════════════════════════════════════════════
// Helpers
// ══════════════════════════════════════════════════════════════

function intervalToUnit(seconds) {
    if (seconds >= 86400 && seconds % 86400 === 0) return { value: seconds / 86400, unit: 86400 };
    if (seconds >= 3600 && seconds % 3600 === 0)   return { value: seconds / 3600, unit: 3600 };
    return { value: seconds / 60, unit: 60 };
}


// ══════════════════════════════════════════════════════════════
// Destroy
// ══════════════════════════════════════════════════════════════

export function destroy() {
    if (refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
    }
    unsubs.forEach(fn => fn());
    unsubs = [];
    _modalOpen = false;
}