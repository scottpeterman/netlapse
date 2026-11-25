/**
 * Device Detail View
 *
 * Four tabs: Raw Output, Parsed Data, Snapshots, Semantic Diff
 * Capture type pill selector (config, arp, bgp, interfaces, routes...)
 * drives Raw, Parsed, and Diff tabs.
 *
 * Data sources: /api/v1/devices/{id}, snapshots, latest, diff, capture_types
 *
 * Collect Now: the scope picker lists what would run on this device
 * (/devices/{id}/collect/plan). Clicking starts a tracked request; the status
 * panel polls /collect/requests/{rid} until it finishes, then refreshes the
 * device info and the open tab. An in-flight request is picked back up on
 * navigation/reload via /devices/{id}/collect/latest.
 */

import { api } from '../api.js';
import {
    badge, code, timeAgo, formatTimestamp, escapeHtml, icons,
    recordsToTSV, copyText, downloadCSV, exportFilename, flashButton,
} from '../components.js';

let device = null;
let currentTab = 'raw';
let captureType = 'config';
let captureTypes = ['config'];
let _snapCache = {};       // cache latest snapshots by capture type
let _parsedView = null;    // { all, records, keys, snap, shown, filter }: parsed table state; records = filtered + sorted (export source)
let _diffMask = false;     // mask volatile lines (config-change timestamps, etc.)
let _diffIgnore = null;    // semantic-diff field exclusions: null = server defaults, array = explicit
let _lastDiff = { from: null, to: null };  // versions behind the current diff, for re-fetch on mask toggle
let _diffMountEl = null;   // element the current diff is rendered into (for mask re-fetch)
let _pendingDiff = null;   // deep-link target {capture,to,from} from #/device/{id}/diff/...; consumed once

// Reference data for edit modal (lazy-loaded once)
let _refData = null;

// Collect Now state
let _collectPlan = [];     // [{capture_type, commands, enabled, matched, ...}]
let _collectReq = null;    // last-seen request object from the server
let _collectGen = 0;       // bumps on destroy/new request; stale poll loops exit
let _collectTick = null;   // setTimeout handle for the poll loop
const COLLECT_POLL_MS = 1000;
const COLLECT_RESUME_WINDOW_S = 120;   // show a finished request this long after

export function render(params) {
    return `
        <div class="card" id="dev-info" style="margin-bottom:18px">
            <div class="info-grid" id="dev-info-grid">
                <div><div class="info-label">Loading…</div></div>
            </div>
        </div>
        <div class="card" id="dev-collect-status" style="display:none;margin-bottom:18px"></div>
        <div class="tab-row" id="dev-tabs">
            <button class="tab-item active" data-tab="raw">${icons.file} Raw Output</button>
            <button class="tab-item" data-tab="parsed">${icons.data} Parsed Data</button>
            <button class="tab-item" data-tab="snapshots">${icons.git} Snapshots</button>
            <button class="tab-item" data-tab="diff">${icons.diff} Semantic Diff</button>
            <div class="tab-actions">
                <button class="btn btn-sm" id="dev-edit-btn">${icons.edit || '✎'} Edit</button>
                <select class="select" id="dev-collect-scope" title="What Collect Now will run on this device"
                        style="padding:3px 8px;font-size:11px;max-width:220px">
                    <option value="">All captures</option>
                </select>
                <button class="btn btn-sm" id="dev-collect-btn">${icons.play} Collect Now</button>
                <button class="btn btn-sm btn-danger" id="dev-delete-btn">${icons.trash} Delete</button>
            </div>
        </div>
        <div id="dev-tab-content">
            <div class="loading">Loading…</div>
        </div>

        <!-- Device edit modal -->
        <div class="modal-overlay" id="dev-edit-modal" style="display:none">
            <div class="modal-container" style="max-width:640px">
                <div class="modal-header">
                    <div class="modal-title" id="dev-edit-title">Edit Device</div>
                    <button class="modal-close" id="dev-edit-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="dev-edit-body"><div class="loading">Loading…</div></div>
                    <div id="dev-edit-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="dev-edit-cancel">Cancel</button>
                        <button class="btn btn-primary" id="dev-edit-save">Save</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Delete confirmation modal -->
        <div class="modal-overlay" id="dev-delete-modal" style="display:none">
            <div class="modal-container" style="max-width:460px">
                <div class="modal-header">
                    <div class="modal-title">Delete Device</div>
                    <button class="modal-close" id="dev-delete-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <p id="dev-delete-prompt" style="margin:0 0 12px"></p>
                    <p class="text-faint fs-12" style="margin:0 0 4px">
                        Removes the inventory record only. Past job history is kept,
                        and stored captures on disk are left in place.
                    </p>
                    <p class="text-faint fs-12" style="margin:0">
                        If this device is still in the discovery map, the next
                        <code class="mono">sync-map</code> will recreate it.
                    </p>
                    <div id="dev-delete-error" class="form-error" style="display:none;margin-top:10px"></div>
                    <div class="form-actions">
                        <button class="btn" id="dev-delete-cancel">Cancel</button>
                        <button class="btn btn-danger" id="dev-delete-confirm">Delete</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init(params) {
    const deviceId = params[0];
    if (!deviceId) return;

    // Update topbar
    const backBtn = document.createElement('button');
    backBtn.className = 'topbar-back';
    backBtn.innerHTML = `${icons.back} Devices`;
    backBtn.onclick = () => { location.hash = '#/devices'; };

    const topbarLeft = document.querySelector('.topbar-left');
    const title = document.getElementById('topbar-title');
    topbarLeft.insertBefore(backBtn, title);

    // Fetch device
    try {
        device = await api.device(deviceId);
    } catch (e) {
        document.getElementById('dev-tab-content').innerHTML =
            `<div class="loading">Device not found (${escapeHtml(String(e))})</div>`;
        return;
    }

    // Update topbar title
    title.textContent = device.name || `Device ${deviceId}`;
    if (device.status) {
        title.insertAdjacentHTML('afterend', ` ${badge(device.status)}`);
    }

    // Info grid
    const grid = document.getElementById('dev-info-grid');
    const fields = [
        ['IP Address', device.primary_ip4, true],
        ['Platform', device.platform_name || device.platform || '—'],
        ['Site', device.site_name || device.site_slug || '—'],
        ['Role', device.role_name || device.role || '—'],
        ['Last Collected', timeAgo(device.last_collected_at)],
        ['Collection', device.collection_enabled !== false ? 'Enabled' : 'Disabled'],
    ];
    grid.innerHTML = fields.map(([label, val, isMono]) => `
        <div>
            <div class="info-label">${label}</div>
            <div class="info-value${isMono ? ' mono' : ''}">${escapeHtml(String(val || '—'))}</div>
        </div>
    `).join('');

    // Fetch available capture types
    try {
        captureTypes = await api.captureTypes();
        if (!captureTypes.length) captureTypes = ['config'];
    } catch { captureTypes = ['config']; }

    // Tab wiring
    document.querySelectorAll('#dev-tabs .tab-item').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#dev-tabs .tab-item').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTab = btn.dataset.tab;
            loadTab();
        });
    });

    // Collect Now — scope picker, button, and resume of an in-flight request
    document.getElementById('dev-collect-btn').addEventListener('click', startCollect);
    loadCollectPlan();
    resumeCollect();

    // Edit button + modal wiring
    document.getElementById('dev-edit-btn').addEventListener('click', openEditModal);
    document.getElementById('dev-edit-close').addEventListener('click', closeEditModal);
    document.getElementById('dev-edit-cancel').addEventListener('click', closeEditModal);
    document.getElementById('dev-edit-modal').addEventListener('click', (e) => {
        if (e.target.id === 'dev-edit-modal') closeEditModal();
    });
    document.getElementById('dev-edit-save').addEventListener('click', saveDevice);

    // Delete button + confirm modal wiring
    document.getElementById('dev-delete-btn').addEventListener('click', openDeleteModal);
    document.getElementById('dev-delete-close').addEventListener('click', closeDeleteModal);
    document.getElementById('dev-delete-cancel').addEventListener('click', closeDeleteModal);
    document.getElementById('dev-delete-modal').addEventListener('click', (e) => {
        if (e.target.id === 'dev-delete-modal') closeDeleteModal();
    });
    document.getElementById('dev-delete-confirm').addEventListener('click', confirmDelete);

    // Optional deep-link: #/device/{id}/diff/{capture}/{toVersion}/{fromVersion}
    // Lets the Changes feed open the Semantic Diff for one exact change. When
    // no version pair is given, the diff tab falls back to latest-vs-current,
    // which for a recent change is the change itself. Absent params[1] (every
    // ordinary device link) skips this and preserves the default 'raw' tab.
    if (params[1] === 'diff') {
        currentTab = 'diff';
        const cap = params[2] ? decodeURIComponent(params[2]) : null;
        if (cap && captureTypes.includes(cap)) captureType = cap;
        if (params[3]) {
            _pendingDiff = { to: params[3], from: params[4] || null };
        }
        document.querySelectorAll('#dev-tabs .tab-item').forEach(b =>
            b.classList.toggle('active', b.dataset.tab === 'diff'));
    }

    loadTab();
}

// ══════════════════════════════════════════════════════════════
// Device edit modal
// ══════════════════════════════════════════════════════════════

async function loadRefData() {
    if (_refData) return _refData;
    const [sites, platforms, roles, credentials] = await Promise.all([
        api.sites().catch(() => []),
        api.platforms().catch(() => []),
        api.roles().catch(() => []),
        // Source the real credentials from the vault (the same list the
        // Credentials page uses), not the vestigial DCIM credentials table.
        // device.credential_id resolves against vault IDs in bridge.resolve_credentials.
        api.admin.vaultCredentials().catch(() => []),
    ]);
    _refData = {
        sites, platforms, roles,
        // Only SSH credentials are selectable: get_ssh_credential() filters by
        // type, so an SNMP credential here would silently fall back to default.
        credentials: (credentials || []).filter(c => c.credential_type === 'ssh'),
    };
    return _refData;
}

function selectOpts(items, valueKey, labelFn, selectedVal, emptyLabel) {
    let html = emptyLabel ? `<option value="">${escapeHtml(emptyLabel)}</option>` : '';
    html += items.map(item => {
        const val = item[valueKey];
        const sel = val == selectedVal ? ' selected' : '';
        return `<option value="${val}"${sel}>${escapeHtml(labelFn(item))}</option>`;
    }).join('');
    return html;
}

async function openEditModal() {
    const modal = document.getElementById('dev-edit-modal');
    modal.style.display = 'flex';
    document.getElementById('dev-edit-title').textContent = `Edit: ${device.name}`;
    document.getElementById('dev-edit-error').style.display = 'none';

    const body = document.getElementById('dev-edit-body');
    body.innerHTML = '<div class="loading">Loading…</div>';

    const ref = await loadRefData();

    const statusOpts = ['active', 'planned', 'staged', 'decommissioned'].map(s =>
        `<option value="${s}"${s === device.status ? ' selected' : ''}>${s}</option>`
    ).join('');

    body.innerHTML = `
        <details class="form-details" open>
            <summary class="form-summary">Identity</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Name</label>
                    <input class="input mono" id="de-name" type="text" value="${escapeHtml(device.name || '')}">
                </div>
                <div class="form-group">
                    <label class="form-label">Status</label>
                    <select class="select" id="de-status">${statusOpts}</select>
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Management IP</label>
                    <input class="input mono" id="de-ip4" type="text" value="${escapeHtml(device.primary_ip4 || '')}">
                </div>
                <div class="form-group">
                    <label class="form-label">OOB IP</label>
                    <input class="input mono" id="de-oob" type="text" value="${escapeHtml(device.oob_ip || '')}">
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Platform</label>
                    <select class="select" id="de-platform">
                        ${selectOpts(ref.platforms, 'id', p => `${p.slug} (${p.device_count})`, device.platform_id, '— none —')}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Site</label>
                    <select class="select" id="de-site">
                        ${selectOpts(ref.sites, 'id', s => `${s.slug} (${s.device_count || 0})`, device.site_id)}
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label class="form-label">Role</label>
                <select class="select" id="de-role">
                    ${selectOpts(ref.roles, 'id', r => `${r.slug} (${r.device_count})`, device.role_id, '— none —')}
                </select>
            </div>
        </details>

        <details class="form-details" open>
            <summary class="form-summary">Collection</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Credential</label>
                    <select class="select" id="de-credential">
                        <option value="">Shared default</option>
                        ${selectOpts(ref.credentials, 'id',
                            c => `${c.name} (${c.username})${c.is_default ? ' ★' : ''}`,
                            device.credential_id)}
                    </select>
                    <span class="text-faint fs-11" style="margin-top:4px;display:block">
                        Override the shared credential for this device only.
                        ${device.credential_name
                            ? `Currently: <strong>${escapeHtml(device.credential_name)}</strong> (${escapeHtml(device.credential_username || '')})`
                            : 'Using shared default.'}
                    </span>
                </div>
                <div class="form-group">
                    <label class="form-label">SSH Port</label>
                    <input class="input mono" id="de-ssh-port" type="number" min="1" max="65535"
                        value="${device.ssh_port || 22}" style="width:100px">
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Legacy SSH</label>
                    <select class="select" id="de-legacy-ssh">
                        <option value="" ${device.device_legacy_ssh == null ? 'selected' : ''}>Platform default${device.legacy_ssh ? ' (on)' : ' (off)'}</option>
                        <option value="1" ${device.device_legacy_ssh === 1 ? 'selected' : ''}>Force on</option>
                        <option value="0" ${device.device_legacy_ssh === 0 ? 'selected' : ''}>Force off</option>
                    </select>
                    <span class="text-faint fs-11" style="margin-top:4px;display:block">
                        Enables old KEX algorithms and forces ssh-rsa signatures for pre-2014 SSH servers.
                    </span>
                </div>
                <div></div>
            </div>
            <div class="form-group">
                <label class="form-label-inline">
                    <input type="checkbox" id="de-collection-enabled"
                        ${device.collection_enabled !== false && device.collection_enabled !== 0 ? 'checked' : ''}> Collection Enabled
                </label>
            </div>
            <div style="margin-top:12px;display:flex;align-items:center;gap:10px">
                <button class="btn btn-sm" id="de-test-auth" type="button">Test Authentication</button>
                <span id="de-test-status" class="fs-12"></span>
            </div>
            <div id="de-test-result" style="display:none;margin-top:10px"></div>
            ${device.credential_test_result ? `
                <div class="text-faint fs-11" style="margin-top:8px">
                    Last credential test: <strong>${escapeHtml(device.credential_test_result)}</strong>
                    ${device.credential_tested_at ? ` (${escapeHtml(device.credential_tested_at)})` : ''}
                </div>
            ` : ''}
        </details>

        <details class="form-details">
            <summary class="form-summary">Metadata</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Serial Number</label>
                    <input class="input mono" id="de-serial" type="text" value="${escapeHtml(device.serial_number || '')}">
                </div>
                <div class="form-group">
                    <label class="form-label">Asset Tag</label>
                    <input class="input mono" id="de-asset" type="text" value="${escapeHtml(device.asset_tag || '')}">
                </div>
            </div>
            <div class="form-group">
                <label class="form-label">Description</label>
                <input class="input" id="de-desc" type="text" value="${escapeHtml(device.description || '')}">
            </div>
            <div class="form-group">
                <label class="form-label">Comments</label>
                <textarea class="input" id="de-comments" rows="2">${escapeHtml(device.comments || '')}</textarea>
            </div>
        </details>
    `;

    setTimeout(() => document.getElementById('de-name').focus(), 100);

    // Wire Test Auth button
    document.getElementById('de-test-auth').addEventListener('click', runAuthTest);
}

function closeEditModal() {
    document.getElementById('dev-edit-modal').style.display = 'none';
}

// ══════════════════════════════════════════════════════════════
// Delete device
// ══════════════════════════════════════════════════════════════

function openDeleteModal() {
    document.getElementById('dev-delete-error').style.display = 'none';
    document.getElementById('dev-delete-prompt').innerHTML =
        `Delete <strong>${escapeHtml(device.name)}</strong>${
            device.site_slug ? ` <span class="text-faint">(${escapeHtml(device.site_slug)})</span>` : ''
        }?`;
    document.getElementById('dev-delete-modal').style.display = 'flex';
}

function closeDeleteModal() {
    document.getElementById('dev-delete-modal').style.display = 'none';
}

async function confirmDelete() {
    const errEl = document.getElementById('dev-delete-error');
    const btn = document.getElementById('dev-delete-confirm');
    errEl.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Deleting…';

    try {
        await api.deleteDevice(device.id);
        location.hash = '#/devices';
    } catch (e) {
        errEl.textContent = e.message || 'Delete failed';
        errEl.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Delete';
    }
}

async function runAuthTest() {
    const btn = document.getElementById('de-test-auth');
    const statusEl = document.getElementById('de-test-status');
    const resultEl = document.getElementById('de-test-result');

    btn.disabled = true;
    btn.textContent = 'Saving…';
    statusEl.textContent = '';
    statusEl.className = 'fs-12';
    resultEl.style.display = 'none';

    // Save pending changes first (e.g. legacy_ssh toggle) without closing
    const payload = buildDevicePayload();
    if (payload === null) {
        // validation error
        btn.disabled = false;
        btn.textContent = 'Test Authentication';
        return;
    }
    if (Object.keys(payload).length) {
        try {
            device = await api.updateDevice(device.id, payload);
            refreshInfoGrid();
        } catch (e) {
            statusEl.textContent = `✗ Save failed: ${e.message}`;
            statusEl.className = 'fs-12 text-error';
            btn.disabled = false;
            btn.textContent = 'Test Authentication';
            return;
        }
    }

    btn.textContent = 'Testing…';

    try {
        const r = await api.testAuth(device.id);

        if (r.success) {
            statusEl.textContent = `✓ Auth OK — ${r.duration_seconds}s`;
            statusEl.className = 'fs-12 text-success fw-600';
        } else {
            statusEl.textContent = `✗ ${r.error_category || 'failed'} — ${r.duration_seconds}s`;
            statusEl.className = 'fs-12 text-error fw-600';
        }

        // Build result panel
        let infoRows = '';
        if (r.success) {
            infoRows = [
                ['Prompt', r.prompt],
                ['Hostname', r.hostname],
                ['Auth Method', r.auth_method],
                ['SSH Banner', r.ssh_banner],
                ['KEX', r.kex_algorithm],
                ['Cipher', r.cipher],
            ].filter(([, v]) => v)
             .map(([k, v]) => `<div class="fs-11"><strong>${k}:</strong> <code class="mono">${escapeHtml(String(v))}</code></div>`)
             .join('');
        } else {
            infoRows = `
                <div class="fs-11"><strong>Category:</strong> ${escapeHtml(r.error_category || '—')}</div>
                <div class="fs-11"><strong>Error:</strong> ${escapeHtml(r.error_message || '—')}</div>
            `;
        }

        // Debug log
        const debugLines = (r.debug_log || [])
            .map(line => escapeHtml(line))
            .join('\n');

        resultEl.innerHTML = `
            <div style="background:var(--bg-alt,#f8f9fa);border:1px solid var(--border,#e2e5e9);border-radius:6px;padding:10px">
                ${infoRows}
                <details style="margin-top:8px">
                    <summary class="fs-11 text-muted" style="cursor:pointer">Debug log (${r.debug_log?.length || 0} lines)</summary>
                    <pre class="mono fs-10" style="margin-top:6px;max-height:200px;overflow:auto;white-space:pre-wrap;background:var(--bg,#fff);padding:8px;border-radius:4px;border:1px solid var(--border,#e2e5e9)">${debugLines || '(empty)'}</pre>
                </details>
            </div>
        `;
        resultEl.style.display = 'block';

    } catch (e) {
        statusEl.textContent = `✗ Request failed: ${e.message}`;
        statusEl.className = 'fs-12 text-error';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Test Authentication';
    }
}

/**
 * Build the diff-only payload from form values vs current device state.
 * Returns null if validation fails (error displayed in form).
 * Returns {} if nothing changed.
 */
function buildDevicePayload() {
    const errEl = document.getElementById('dev-edit-error');
    errEl.style.display = 'none';
    const payload = {};

    // Identity
    const name = document.getElementById('de-name').value.trim();
    if (!name) { errEl.textContent = 'Name is required.'; errEl.style.display = 'block'; return null; }
    if (name !== device.name) payload.name = name;

    const status = document.getElementById('de-status').value;
    if (status !== device.status) payload.status = status;

    const ip4 = document.getElementById('de-ip4').value.trim();
    if (ip4 !== (device.primary_ip4 || '')) payload.primary_ip4 = ip4 || null;

    const oob = document.getElementById('de-oob').value.trim();
    if (oob !== (device.oob_ip || '')) payload.oob_ip = oob || null;

    const platformId = document.getElementById('de-platform').value;
    const newPlatformId = platformId ? parseInt(platformId) : null;
    if (newPlatformId !== device.platform_id) payload.platform_id = newPlatformId;

    const siteId = document.getElementById('de-site').value;
    if (siteId && parseInt(siteId) !== device.site_id) payload.site_id = parseInt(siteId);

    const roleId = document.getElementById('de-role').value;
    const newRoleId = roleId ? parseInt(roleId) : null;
    if (newRoleId !== device.role_id) payload.role_id = newRoleId;

    // Collection
    const credId = document.getElementById('de-credential').value;
    const newCredId = credId ? parseInt(credId) : null;
    if (newCredId !== device.credential_id) payload.credential_id = newCredId;

    const sshPort = parseInt(document.getElementById('de-ssh-port').value) || 22;
    if (sshPort !== (device.ssh_port || 22)) payload.ssh_port = sshPort;

    const legacySshVal = document.getElementById('de-legacy-ssh').value;
    const newLegacy = legacySshVal === '' ? null : parseInt(legacySshVal);
    if (newLegacy !== device.device_legacy_ssh) payload.legacy_ssh = newLegacy;

    const collEnabled = document.getElementById('de-collection-enabled').checked;
    const wasEnabled = device.collection_enabled !== false && device.collection_enabled !== 0;
    if (collEnabled !== wasEnabled) payload.collection_enabled = collEnabled;

    // Metadata
    const serial = document.getElementById('de-serial').value.trim();
    if (serial !== (device.serial_number || '')) payload.serial_number = serial || null;

    const asset = document.getElementById('de-asset').value.trim();
    if (asset !== (device.asset_tag || '')) payload.asset_tag = asset || null;

    const desc = document.getElementById('de-desc').value.trim();
    if (desc !== (device.description || '')) payload.description = desc || null;

    const comments = document.getElementById('de-comments').value.trim();
    if (comments !== (device.comments || '')) payload.comments = comments || null;

    return payload;
}

async function saveDevice() {
    const payload = buildDevicePayload();
    if (payload === null) return; // validation error

    // Nothing changed?
    if (!Object.keys(payload).length) {
        closeEditModal();
        return;
    }

    const errEl = document.getElementById('dev-edit-error');
    const saveBtn = document.getElementById('dev-edit-save');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    try {
        device = await api.updateDevice(device.id, payload);
        closeEditModal();
        refreshInfoGrid();
        // Update topbar title if name changed
        if (payload.name) {
            const title = document.getElementById('topbar-title');
            if (title) title.textContent = device.name;
        }
    } catch (e) {
        errEl.textContent = e.message || 'Save failed';
        errEl.style.display = 'block';
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
    }
}

function refreshInfoGrid() {
    const grid = document.getElementById('dev-info-grid');
    if (!grid || !device) return;
    const fields = [
        ['IP Address', device.primary_ip4, true],
        ['Platform', device.platform_name || device.platform || '—'],
        ['Site', device.site_name || device.site_slug || '—'],
        ['Role', device.role_name || device.role || '—'],
        ['Last Collected', timeAgo(device.last_collected_at)],
        ['Collection', device.collection_enabled !== false && device.collection_enabled !== 0 ? 'Enabled' : 'Disabled'],
    ];
    grid.innerHTML = fields.map(([label, val, isMono]) => `
        <div>
            <div class="info-label">${label}</div>
            <div class="info-value${isMono ? ' mono' : ''}">${escapeHtml(String(val || '—'))}</div>
        </div>
    `).join('');
}


// ══════════════════════════════════════════════════════════════
// Collect Now
// ══════════════════════════════════════════════════════════════

const COLLECT_FINAL = new Set(['success', 'partial', 'failed']);

function apiErrorDetail(e) {
    // api.request() throws "STATUS Text: <body>"; FastAPI bodies are {"detail": ...}
    const msg = String(e && e.message || e);
    const idx = msg.indexOf(': ');
    if (idx >= 0) {
        try {
            const body = JSON.parse(msg.slice(idx + 2));
            if (body && body.detail) return String(body.detail);
        } catch { /* not JSON */ }
    }
    return msg;
}

async function loadCollectPlan() {
    const sel = document.getElementById('dev-collect-scope');
    if (!sel || !device) return;
    try {
        const res = await api.collectPlan(device.id);
        _collectPlan = (res.captures || []).filter(c => c.commands && c.commands.length);
    } catch {
        _collectPlan = [];
    }
    const total = _collectPlan.reduce((n, c) => n + c.commands.length, 0);
    sel.innerHTML = `<option value="">All captures (${_collectPlan.length})</option>` +
        _collectPlan.map(c => `<option value="${escapeHtml(c.capture_type)}"
            title="${escapeHtml(c.commands.join(' ; '))}">${escapeHtml(c.capture_type)} — ${escapeHtml(c.commands[0])}${c.commands.length > 1 ? ` (+${c.commands.length - 1})` : ''}</option>`).join('');
    sel.title = _collectPlan.length
        ? `All captures runs ${total} command(s) in one SSH session`
        : 'No enabled job applies to this device';
}

function setCollectBusy(busy) {
    const btn = document.getElementById('dev-collect-btn');
    const sel = document.getElementById('dev-collect-scope');
    if (btn) {
        btn.disabled = busy;
        btn.innerHTML = busy ? `${icons.refresh} Collecting…` : `${icons.play} Collect Now`;
    }
    if (sel) sel.disabled = busy;
}

async function startCollect() {
    if (!device) return;
    const sel = document.getElementById('dev-collect-scope');
    const scope = sel ? sel.value : '';
    setCollectBusy(true);
    try {
        const req = await api.collect(device.id, scope || null);
        trackCollect(req);
    } catch (e) {
        setCollectBusy(false);
        renderCollectError(apiErrorDetail(e));
    }
}

async function resumeCollect() {
    if (!device) return;
    try {
        const { request } = await api.collectLatest(device.id);
        if (!request) return;
        const recent = !COLLECT_FINAL.has(request.status) ||
            (request.finished_at &&
             (Date.now() - Date.parse(request.finished_at)) / 1000 < COLLECT_RESUME_WINDOW_S);
        if (recent) trackCollect(request, /*resumed*/ true);
    } catch { /* scheduler down or older server — nothing to resume */ }
}

function trackCollect(req, resumed = false) {
    const gen = ++_collectGen;
    clearTimeout(_collectTick);
    _collectReq = req;
    renderCollectStatus(req);
    if (COLLECT_FINAL.has(req.status)) {
        setCollectBusy(false);
        if (!resumed) onCollectFinished(req);
        return;
    }
    setCollectBusy(true);

    const poll = async () => {
        if (gen !== _collectGen || !device) return;
        try {
            const cur = await api.collectRequest(req.id);
            if (gen !== _collectGen || !device) return;
            _collectReq = cur;
            renderCollectStatus(cur);
            if (COLLECT_FINAL.has(cur.status)) {
                setCollectBusy(false);
                onCollectFinished(cur);
                return;
            }
        } catch (e) {
            if (gen !== _collectGen) return;
            if (String(e.message || '').startsWith('404')) {
                setCollectBusy(false);
                renderCollectError('Lost track of this request — the server restarted or it expired. ' +
                    'Check Last Collected / Snapshots for the outcome.');
                return;
            }
            // transient (server busy, network blip) — keep polling
        }
        _collectTick = setTimeout(poll, COLLECT_POLL_MS);
    };
    _collectTick = setTimeout(poll, COLLECT_POLL_MS);
}

async function onCollectFinished(req) {
    if (!device) return;
    // Snapshots on disk changed — drop cached latest-snapshot responses.
    _snapCache = {};
    try {
        device = await api.device(device.id);
        refreshInfoGrid();
    } catch { /* keep old info */ }
    try {
        const types = await api.captureTypes();
        if (types && types.length) captureTypes = types;
    } catch { /* keep old list */ }
    if (req.status !== 'failed') loadTab();
}

function collectBadge(status) {
    const map = { success: 'success', partial: 'warning', failed: 'error',
                  queued: 'info', running: 'info' };
    const cls = map[status] || 'neutral';
    return `<span class="badge badge-${cls}"><span class="dot dot-${cls}"></span>${escapeHtml(status)}</span>`;
}

function collectRowDot(status) {
    const map = { ok: 'success', failed: 'error', running: 'info', pending: 'neutral' };
    return `<span class="dot dot-${map[status] || 'neutral'}"></span>`;
}

function fmtBytes(n) {
    if (n == null) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function renderCollectStatus(req) {
    const el = document.getElementById('dev-collect-status');
    if (!el) return;
    const final = COLLECT_FINAL.has(req.status);
    const rows = req.captures || [];
    const done = rows.filter(r => r.status === 'ok' || r.status === 'failed').length;
    const elapsed = req.elapsed != null ? `${Number(req.elapsed).toFixed(1)}s` : '';
    const phase = final
        ? (req.error_message ? escapeHtml(req.error_message)
                             : `${done}/${rows.length} command(s) collected`)
        : escapeHtml(req.phase || req.status);
    const errCat = final && req.error_category && req.status !== 'success'
        ? ` <code class="code">${escapeHtml(req.error_category)}</code>` : '';
    const unresolved = (req.unresolved || []).length
        ? `<div class="text-faint fs-11" style="margin-top:4px">No job defines: ${req.unresolved.map(t => `<code class="code">${escapeHtml(t)}</code>`).join(' ')}</div>`
        : '';

    el.style.display = '';
    el.innerHTML = `
        <div class="card-head-alt" style="display:flex;align-items:center;gap:10px">
            <span class="fw-600 fs-12">Collect Now</span>
            ${collectBadge(req.status)}
            <span class="text-muted fs-12" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${phase}${errCat}</span>
            <span class="mono text-faint fs-11">${rows.length ? `${done}/${rows.length}` : ''}${elapsed ? ` · ${elapsed}` : ''}</span>
            ${final ? `<button class="btn btn-sm" id="dev-collect-dismiss" title="Dismiss">&times;</button>` : ''}
        </div>
        <div style="padding:6px 14px 10px">
            ${rows.map(r => `
                <div style="display:flex;align-items:center;gap:10px;padding:3px 0;font-size:12px">
                    ${collectRowDot(r.status)}
                    <code class="code" style="min-width:88px;text-align:center">${escapeHtml(r.capture_type)}</code>
                    <span class="mono${r.status === 'pending' ? ' text-faint' : ''}" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(r.command)}</span>
                    ${r.error ? `<span class="fs-11" style="color:var(--red);max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(r.error)}">${escapeHtml(r.error)}</span>` : ''}
                    ${r.parsed === true ? `<span class="text-faint fs-11" title="${escapeHtml(r.template || '')}">parsed</span>` : ''}
                    ${r.parsed === false ? `<span class="text-faint fs-11">no template match</span>` : ''}
                    <span class="mono text-faint fs-11" style="min-width:56px;text-align:right">${fmtBytes(r.bytes)}</span>
                    <span class="mono text-faint fs-11" style="min-width:40px;text-align:right">${r.duration != null ? `${r.duration}s` : ''}</span>
                </div>`).join('')}
            ${unresolved}
        </div>`;

    const dismiss = document.getElementById('dev-collect-dismiss');
    if (dismiss) dismiss.addEventListener('click', () => {
        el.style.display = 'none';
        el.innerHTML = '';
    });
}

function renderCollectError(message) {
    const el = document.getElementById('dev-collect-status');
    if (!el) return;
    el.style.display = '';
    el.innerHTML = `
        <div class="card-head-alt" style="display:flex;align-items:center;gap:10px">
            <span class="fw-600 fs-12">Collect Now</span>
            ${collectBadge('failed')}
            <span class="fs-12" style="flex:1;color:var(--red)">${escapeHtml(message)}</span>
            <button class="btn btn-sm" id="dev-collect-dismiss" title="Dismiss">&times;</button>
        </div>`;
    document.getElementById('dev-collect-dismiss').addEventListener('click', () => {
        el.style.display = 'none';
        el.innerHTML = '';
    });
}


// ══════════════════════════════════════════════════════════════
// Tab router
// ══════════════════════════════════════════════════════════════

async function loadTab() {
    const el = document.getElementById('dev-tab-content');
    el.innerHTML = '<div class="loading">Loading…</div>';

    if (currentTab === 'raw') await loadRaw(el);
    else if (currentTab === 'parsed') await loadParsed(el);
    else if (currentTab === 'snapshots') await loadSnapshots(el);
    else if (currentTab === 'diff') await loadDiff(el);
}


// ══════════════════════════════════════════════════════════════
// Capture type pill selector (shared across tabs)
// ══════════════════════════════════════════════════════════════

function renderPills(activeType) {
    return `<div class="pill-row" id="capture-pills">
        ${captureTypes.map(t => `
            <button class="pill${t === activeType ? ' active' : ''}" data-type="${escapeHtml(t)}">${escapeHtml(t)}</button>
        `).join('')}
    </div>`;
}

function wirePills(callback) {
    document.querySelectorAll('#capture-pills .pill').forEach(btn => {
        btn.addEventListener('click', () => {
            captureType = btn.dataset.type;
            callback();
        });
    });
}


// ══════════════════════════════════════════════════════════════
// Fetch helper with cache
// ══════════════════════════════════════════════════════════════

async function fetchLatest(type) {
    const key = `${device.id}:${type}`;
    if (_snapCache[key]) return _snapCache[key];
    const snap = await api.latest(device.id, type);
    _snapCache[key] = snap;
    return snap;
}


// ══════════════════════════════════════════════════════════════
// Raw Output Tab
// ══════════════════════════════════════════════════════════════

async function loadRaw(el) {
    el.innerHTML = renderPills(captureType) + '<div id="raw-content"><div class="loading">Loading…</div></div>';
    wirePills(() => loadRaw(el));

    const target = document.getElementById('raw-content');
    try {
        const snap = await fetchLatest(captureType);
        target.innerHTML = `
            <div class="card">
                <div class="card-head-alt">
                    <div style="display:flex;align-items:center;gap:10px">
                        <span class="text-muted fs-12">
                            Latest snapshot — ${snap.collected_at ? formatTimestamp(snap.collected_at) : '—'}
                        </span>
                        ${snap.command ? `<code class="code">${escapeHtml(snap.command)}</code>` : ''}
                    </div>
                    <span class="text-faint fs-11">
                        ${snap.template_name ? `${escapeHtml(snap.template_name)} · score ${snap.template_score}` : 'no template match'}
                    </span>
                </div>
                <pre class="config-pre">${escapeHtml(snap.raw_text || '(empty)')}</pre>
            </div>`;
    } catch {
        target.innerHTML = `<div class="card"><div class="empty-state">No ${escapeHtml(captureType)} data collected for this device yet.</div></div>`;
    }
}


// ══════════════════════════════════════════════════════════════
// Parsed Data Tab
// ══════════════════════════════════════════════════════════════

async function loadParsed(el) {
    el.innerHTML = renderPills(captureType) + '<div id="parsed-content"><div class="loading">Loading…</div></div>';
    wirePills(() => loadParsed(el));

    const target = document.getElementById('parsed-content');
    try {
        const snap = await fetchLatest(captureType);
        const records = snap.parsed_data?.records;

        if (!records || !records.length) {
            // No parsed data — show helpful message
            const reason = captureType === 'config'
                ? 'Running configs are free-form text — no TextFSM template applies.'
                : snap.template_name
                    ? `Template matched (${snap.template_name}) but produced no records.`
                    : 'No matching TextFSM template found for this output.';

            target.innerHTML = `
                <div class="card">
                    <div class="empty-state">
                        <div style="margin-bottom:6px">No structured data for <strong>${escapeHtml(captureType)}</strong></div>
                        <div class="text-faint fs-11">${reason}</div>
                    </div>
                </div>`;
            return;
        }

        // Render structured table. Route tables run to tens of thousands of
        // records, so rows go in pages: filter and sort work on the whole set,
        // the DOM only ever holds what has been shown.
        const keys = [...new Set(records.flatMap(r => Object.keys(r)))];
        const recordCount = records.length;
        _sortCol = -1;
        _sortAsc = true;

        // Source for the export buttons: every record matching the filter,
        // in the order shown.
        _parsedView = { all: records, records, keys, snap, shown: 0, filter: '' };

        target.innerHTML = `
            <div class="card">
                <div class="card-head-alt">
                    <div style="display:flex;align-items:center;gap:10px">
                        ${snap.command ? `<code class="code">${escapeHtml(snap.command)}</code>` : ''}
                        <span class="text-faint fs-11">
                            ${escapeHtml(snap.template_name || '—')} · score ${snap.template_score || '—'} · ${recordCount} record${recordCount !== 1 ? 's' : ''} · ${keys.length} fields
                        </span>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px">
                        <input class="input" id="parsed-filter" placeholder="Filter records…" autocomplete="off" spellcheck="false" style="width:220px">
                        <button class="btn btn-sm" id="parsed-copy-btn" title="Copy every record matching the filter as a tab-separated table — paste straight into Excel, Sheets, or Numbers">${icons.copy} Copy</button>
                        <button class="btn btn-sm" id="parsed-csv-btn" title="Download every record matching the filter as a CSV file">${icons.download} CSV</button>
                        <span class="text-muted fs-12" style="margin-left:4px">
                            ${snap.collected_at ? formatTimestamp(snap.collected_at) : '—'}
                        </span>
                    </div>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr>${keys.map(k => `<th class="parsed-th">${escapeHtml(k)}</th>`).join('')}</tr></thead>
                        <tbody id="parsed-tbody"></tbody>
                    </table>
                </div>
                <div class="summary-row">
                    <span id="parsed-shown"></span>
                    <span id="parsed-more-wrap"></span>
                    <span>${keys.length} fields</span>
                    <span>Template: ${escapeHtml(snap.template_name || '—')}</span>
                    <span>Score: ${snap.template_score || '—'}</span>
                </div>
            </div>`;

        showParsedRows(true);

        // Wire column header click to sort
        document.querySelectorAll('.parsed-th').forEach((th, colIdx) => {
            th.style.cursor = 'pointer';
            th.title = 'Click to sort';
            th.addEventListener('click', () => sortTable(keys, colIdx, th));
        });

        // Filter: substring match across every field, case-insensitive.
        let filterTimer = null;
        document.getElementById('parsed-filter').addEventListener('input', e => {
            clearTimeout(filterTimer);
            filterTimer = setTimeout(() => {
                _parsedView.filter = e.target.value.trim().toLowerCase();
                applyParsedView();
            }, 200);
        });

        // Wire export buttons (the filtered set, in display order)
        const copyBtn = document.getElementById('parsed-copy-btn');
        const csvBtn = document.getElementById('parsed-csv-btn');

        copyBtn?.addEventListener('click', async () => {
            const v = _parsedView;
            if (!v) return;
            const ok = await copyText(recordsToTSV(v.records, v.keys));
            flashButton(copyBtn, ok ? `${icons.check} Copied` : 'Copy failed');
        });

        csvBtn?.addEventListener('click', () => {
            const v = _parsedView;
            if (!v) return;
            downloadCSV(exportFilename([device?.name, captureType], 'csv'), v.records, v.keys);
            flashButton(csvBtn, `${icons.check} Saved`);
        });

    } catch {
        target.innerHTML = `<div class="card"><div class="empty-state">No ${escapeHtml(captureType)} data collected for this device yet.</div></div>`;
    }
}

const PARSED_PAGE = 500;

function renderRecords(records, keys) {
    return records.map(r => `<tr>${keys.map(k => {
        const val = String(r[k] ?? '');
        // Color-code common status values
        const cls = statusClass(val);
        return `<td class="mono fs-12${cls ? ' ' + cls : ''}">${escapeHtml(val)}</td>`;
    }).join('')}</tr>`).join('');
}

// Append the next page of rows (or start over) and update the footer.
function showParsedRows(reset) {
    const v = _parsedView;
    const tbody = document.getElementById('parsed-tbody');
    if (!v || !tbody) return;
    if (reset) {
        tbody.innerHTML = '';
        v.shown = 0;
    }
    const next = v.records.slice(v.shown, v.shown + PARSED_PAGE);
    tbody.insertAdjacentHTML('beforeend', renderRecords(next, v.keys));
    v.shown += next.length;

    const total = v.records.length;
    const of = v.filter ? `${total} matching of ${v.all.length}` : `${total}`;
    document.getElementById('parsed-shown').textContent =
        v.shown < total ? `Showing ${v.shown} of ${of} records` : `${of} record${total !== 1 ? 's' : ''}`;
    const more = document.getElementById('parsed-more-wrap');
    if (v.shown < total) {
        more.innerHTML = `<button class="btn btn-sm" id="parsed-more">Show ${Math.min(PARSED_PAGE, total - v.shown)} more</button>`;
        document.getElementById('parsed-more').addEventListener('click', () => showParsedRows(false));
    } else {
        more.innerHTML = '';
    }
}

// Rebuild the filtered, sorted record list and redraw from the first page.
function applyParsedView() {
    const v = _parsedView;
    if (!v) return;
    let recs = v.all;
    if (v.filter) {
        const f = v.filter;
        recs = recs.filter(r => v.keys.some(k => String(r[k] ?? '').toLowerCase().includes(f)));
    }
    if (_sortCol >= 0) {
        const key = v.keys[_sortCol];
        recs = [...recs].sort((a, b) => {
            let va = a[key] ?? '', vb = b[key] ?? '';
            // Try numeric sort
            const na = Number(va), nb = Number(vb);
            if (va !== '' && vb !== '' && !isNaN(na) && !isNaN(nb)) return _sortAsc ? na - nb : nb - na;
            // String sort
            va = String(va).toLowerCase();
            vb = String(vb).toLowerCase();
            if (va < vb) return _sortAsc ? -1 : 1;
            if (va > vb) return _sortAsc ? 1 : -1;
            return 0;
        });
    }
    v.records = recs;
    showParsedRows(true);
}

let _sortCol = -1;
let _sortAsc = true;

function sortTable(keys, colIdx, th) {
    if (_sortCol === colIdx) {
        _sortAsc = !_sortAsc;
    } else {
        _sortCol = colIdx;
        _sortAsc = true;
    }

    // Update sort indicators
    document.querySelectorAll('.parsed-th').forEach(h => {
        h.textContent = h.textContent.replace(/ [▲▼]$/, '');
    });
    th.textContent = `${keys[colIdx]} ${_sortAsc ? '▲' : '▼'}`;

    applyParsedView();
}

function statusClass(val) {
    const v = val.toLowerCase();
    if (v === 'established' || v === 'up' || v === 'connected') return 'text-success';
    if (v === 'idle' || v === 'down' || v === 'disabled' || v === 'notconnect') return 'text-error';
    return '';
}


// ══════════════════════════════════════════════════════════════
// Snapshots Tab — browse versions, pick two, diff them
// ══════════════════════════════════════════════════════════════

// Up to two picked rows, each { version_id, collected_at }. Array order
// is click order (A then B); the diff itself always runs older→newer so
// the unified diff reads in the direction time actually moved.
let _snapSel = [];

async function loadSnapshots(el) {
    el.innerHTML = renderPills(captureType) +
        '<div id="snap-content"><div class="loading">Loading…</div></div>';
    // A version picked for one capture type may not even contain another,
    // so switching the pill resets the selection and reloads.
    wirePills(() => { _snapSel = []; _diffMask = false; _diffIgnore = null; loadSnapshots(el); });

    const target = document.getElementById('snap-content');
    try {
        const all = await api.snapshots(device.id, { limit: 50 });
        // list_versions is device-level; keep only the versions that
        // actually hold the selected capture type so every row is diffable.
        const snaps = all.filter(s =>
            s.capture_types && s.capture_types.includes(captureType)
        );

        if (snaps.length < 2) {
            target.innerHTML = `<div class="card"><div class="empty-state">
                ${snaps.length === 0
                    ? `No ${escapeHtml(captureType)} snapshots yet.`
                    : `Only one ${escapeHtml(captureType)} snapshot so far.`}
                At least two collection cycles are needed before you can diff.
            </div></div>`;
            return;
        }

        target.innerHTML = `
            <div class="snap-actionbar">
                <span class="text-muted fs-12" id="snap-hint">Pick two snapshots to compare</span>
                <div style="flex:1"></div>
                <button class="btn btn-sm" id="snap-clear-btn" disabled>Clear</button>
                <button class="btn btn-sm btn-primary" id="snap-diff-btn" disabled>${icons.diff} Compare</button>
            </div>
            <div class="card" style="margin-top:12px">${
                snaps.map((s, i) => renderSnapRow(s, i === 0)).join('')
            }</div>
            <div id="snap-diff-result" style="margin-top:14px"></div>`;

        wireSnapshotRows(target);
    } catch {
        target.innerHTML = `<div class="card"><div class="empty-state">Could not load snapshots.</div></div>`;
    }
}

function renderSnapRow(s, isCurrent) {
    const vid = s.version_id || '—';
    const order = _snapSel.findIndex(x => x.version_id === vid);
    return `
        <div class="snap-row selectable${order >= 0 ? ' sel' : ''}"
             data-vid="${escapeHtml(vid)}" data-at="${escapeHtml(s.collected_at || '')}">
            <div class="snap-line"><div class="snap-dot${isCurrent ? ' current' : ''}"></div></div>
            <code class="mono text-blue fw-600 fs-12" style="min-width:65px">${escapeHtml(vid)}</code>
            <div style="flex:1">
                <div class="fw-500">${escapeHtml(s.message || '—')}</div>
                <div class="text-faint fs-11">${formatTimestamp(s.collected_at)}
                    ${s.capture_types?.length ? ' · ' + s.capture_types.map(t => `<code class="code" style="font-size:9px">${escapeHtml(t)}</code>`).join(' ') : ''}
                </div>
            </div>
            ${order >= 0 ? `<span class="snap-pick">${order === 0 ? 'A' : 'B'}</span>` : ''}
            ${badge(s.trigger || 'scheduled')}
        </div>`;
}

function wireSnapshotRows(target) {
    const diffBtn  = target.querySelector('#snap-diff-btn');
    const clearBtn = target.querySelector('#snap-clear-btn');
    const hint     = target.querySelector('#snap-hint');

    function refresh() {
        target.querySelectorAll('.snap-row.selectable').forEach(row => {
            const order = _snapSel.findIndex(x => x.version_id === row.dataset.vid);
            row.classList.toggle('sel', order >= 0);
            row.querySelector('.snap-pick')?.remove();
            if (order >= 0) {
                const chip = document.createElement('span');
                chip.className = 'snap-pick';
                chip.textContent = order === 0 ? 'A' : 'B';
                row.insertBefore(chip, row.lastElementChild);  // before trailing badge
            }
        });
        diffBtn.disabled  = _snapSel.length !== 2;
        clearBtn.disabled = _snapSel.length === 0;
        hint.textContent =
            _snapSel.length === 0 ? 'Pick two snapshots to compare'
          : _snapSel.length === 1 ? 'Pick one more'
          : 'Ready — Compare runs older → newer';
    }

    target.querySelectorAll('.snap-row.selectable').forEach(row => {
        row.addEventListener('click', () => {
            const vid = row.dataset.vid;
            const idx = _snapSel.findIndex(x => x.version_id === vid);
            if (idx >= 0) {
                _snapSel.splice(idx, 1);                  // toggle off
            } else if (_snapSel.length < 2) {
                _snapSel.push({ version_id: vid, collected_at: row.dataset.at });
            } else {
                _snapSel.shift();                          // FIFO: drop oldest pick
                _snapSel.push({ version_id: vid, collected_at: row.dataset.at });
            }
            refresh();
        });
    });

    clearBtn.addEventListener('click', () => {
        _snapSel = [];
        target.querySelector('#snap-diff-result').innerHTML = '';
        refresh();
    });

    diffBtn.addEventListener('click', () => runSnapshotDiff(target));
    refresh();
}

async function runSnapshotDiff(target) {
    if (_snapSel.length !== 2) return;
    // Order older → newer by collected_at regardless of click order.
    const [a, b] = [..._snapSel].sort((x, y) =>
        String(x.collected_at).localeCompare(String(y.collected_at))
    );
    const out = target.querySelector('#snap-diff-result');
    out.innerHTML = '<div class="loading">Computing diff…</div>';
    _lastDiff = { from: a.version_id, to: b.version_id };
    try {
        const result = await api.diff(device.id, captureType, a.version_id, b.version_id, _diffMask, _diffIgnore);
        renderDiffResult(out, result);
    } catch {
        out.innerHTML = `<div class="card"><div class="empty-state">
            Could not diff those two ${escapeHtml(captureType)} snapshots.
        </div></div>`;
    }
}


// ══════════════════════════════════════════════════════════════
// Semantic Diff Tab
// ══════════════════════════════════════════════════════════════

async function loadDiff(el) {
    el.innerHTML = renderPills(captureType) + '<div id="diff-content"><div class="loading">Loading…</div></div>';
    wirePills(() => { _diffMask = false; _diffIgnore = null; loadDiff(el); });

    const target = document.getElementById('diff-content');
    // A deep-link may pin an exact version pair for the first render; after
    // that, behave normally (latest history vs current).
    const pin = _pendingDiff; _pendingDiff = null;
    _lastDiff = pin ? { from: pin.from, to: pin.to } : { from: null, to: null };
    try {
        const result = await api.diff(
            device.id, captureType, _lastDiff.from, _lastDiff.to, _diffMask, _diffIgnore);
        renderDiffResult(target, result);
    } catch {
        target.innerHTML = `
            <div class="card"><div class="empty-state">
                Not enough ${escapeHtml(captureType)} snapshots to diff. At least two collection cycles are needed.
            </div></div>`;
    }
}

// Render a parsed record (added/removed) as a compact field line.
// Records are flat dicts of TextFSM columns, e.g. {NEIGHBOR_ID, STATE, ...}.
function fmtRecordLine(rec) {
    if (rec == null) return '';
    if (typeof rec !== 'object') return escapeHtml(String(rec));
    return Object.entries(rec)
        .map(([k, v]) => `<b style="font-weight:600">${escapeHtml(k)}</b>=${escapeHtml(String(v ?? ''))}`)
        .join('  ');
}

// Render the identifying key of a changed record, e.g. {NEIGHBOR_ID: "1.1.1.1"}.
function fmtChangeKey(keyObj) {
    if (keyObj == null) return '';
    if (typeof keyObj !== 'object') return escapeHtml(String(keyObj));
    return Object.entries(keyObj)
        .map(([k, v]) => `${escapeHtml(k)} ${escapeHtml(String(v ?? ''))}`)
        .join(' · ');
}

function renderDiffResult(el, result) {
    _diffMountEl = el;
    const added = result.added || [];
    const removed = result.removed || [];
    const changed = result.changed || [];
    const hasChanges = added.length || removed.length || changed.length;

    // The record diff only runs for capture types with a defined record
    // identity (storage/diff.py DIFF_KEY_FIELDS). A running config has none —
    // it's a document, not a record set — so the backend returns empty
    // added/removed/changed AND an empty schema for it. Without this check the
    // pane claims "no changes detected" while the line-aligned diff below is
    // showing real edits, which reads as the engine being broken rather than
    // not applicable. `schema` is the same parseable-vs-raw discriminator
    // classify_change() uses, so the two never disagree.
    const hasRecordIdentity = Array.isArray(result.schema) && result.schema.length > 0;

    let changesHtml = '';
    if (!hasRecordIdentity) {
        changesHtml = `<div class="text-muted" style="padding:12px">
            Record-level diff doesn't apply to <code class="mono">${escapeHtml(captureType)}</code> —
            this capture has no per-record identity, so there's nothing to match on.
            The line-aligned diff below is authoritative for it.
        </div>`;
    } else if (!hasChanges) {
        changesHtml = '<div class="text-muted" style="padding:12px">No changes detected between these versions.</div>';
    } else {
        changesHtml =
            added.map(rec => `<div class="diff-added">+ ${fmtRecordLine(rec)}</div>`).join('') +
            removed.map(rec => `<div class="diff-removed">− ${fmtRecordLine(rec)}</div>`).join('') +
            changed.map(c => {
                // Backend contract (storage/diff.py): { key: {...}, changes: { field: {from, to} } }
                if (c && typeof c === 'object' && c.changes) {
                    const head = fmtChangeKey(c.key);
                    const rows = Object.entries(c.changes).map(([field, ft]) => {
                        const from = (ft && typeof ft === 'object') ? ft.from : undefined;
                        const to = (ft && typeof ft === 'object') ? ft.to : undefined;
                        return `<div class="diff-cfield">
                            <b style="font-weight:600">${escapeHtml(field)}</b>
                            <span class="text-error">${escapeHtml(String(from ?? ''))}</span>
                            <span class="text-ghost"> → </span>
                            <span class="text-success">${escapeHtml(String(to ?? ''))}</span>
                        </div>`;
                    }).join('');
                    return `<div class="diff-changed">
                        ${head ? `<div class="diff-path">${head}</div>` : ''}
                        ${rows}
                    </div>`;
                }
                // Backward-compat for any older {path, from/to} shape
                if (c && typeof c === 'object' && c.path) {
                    return `<div class="diff-changed">
                        <div class="diff-path">${escapeHtml(c.path)}</div>
                        <div class="text-error">− ${escapeHtml(String(c.from || c.old || ''))}</div>
                        <div class="text-success">+ ${escapeHtml(String(c.to || c.new || ''))}</div>
                    </div>`;
                }
                return `<div class="diff-changed">${escapeHtml(JSON.stringify(c))}</div>`;
            }).join('');
    }

    el.innerHTML = `
        <div class="card">
            <div class="card-head-alt">
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <code class="mono fw-600" style="background:var(--surface-alt);padding:2px 8px;border-radius:3px">${escapeHtml(result.from_version || '—')}</code>
                    <span class="text-ghost">→</span>
                    <code class="mono fw-600" style="background:var(--surface-alt);padding:2px 8px;border-radius:3px">${escapeHtml(result.to_version || '—')}</code>
                </div>
                <span class="text-muted fs-12">
                    ${added.length ? `<span style="background:var(--green-bg);color:var(--green);padding:2px 8px;border-radius:4px;font-weight:600;margin-right:6px">+${added.length} added</span>` : ''}
                    ${removed.length ? `<span style="background:var(--red-bg);color:var(--red);padding:2px 8px;border-radius:4px;font-weight:600;margin-right:6px">−${removed.length} removed</span>` : ''}
                    ${changed.length ? `<span style="background:var(--orange-bg);color:var(--orange);padding:2px 8px;border-radius:4px;font-weight:600">~${changed.length} changed</span>` : ''}
                </span>
            </div>
            ${fieldChipsHtml(result)}
            <div style="padding:18px">${changesHtml}</div>
        </div>
        ${textDiffCardHtml(result)}`;

    wireTextDiff(el, result);
    wireIgnoreChips(el, result);
}

// Per-field exclusion chips for the semantic diff. Built from the record
// schema (TextFSM columns) minus the key field(s), which identify a record
// and can never be excluded. A chip is "on" when that field is currently
// excluded from the comparison. Absent for capture types with no parsed
// records (e.g. raw config), which simply show no chips.
function fieldChipsHtml(result) {
    const keys = new Set(result.key_fields || []);
    const ignored = new Set(result.ignored || []);
    const fields = (result.schema || []).filter(f => !keys.has(f));
    if (!fields.length) return '';
    return `
        <div class="ignore-row">
            <span class="ignore-label">Ignore fields:</span>
            ${fields.map(f =>
                `<button class="ignore-chip${ignored.has(f) ? ' on' : ''}" data-field="${escapeHtml(f)}">${escapeHtml(f)}</button>`
            ).join('')}
        </div>`;
}

function wireIgnoreChips(el, result) {
    el.querySelectorAll('.ignore-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            chip.classList.toggle('on');
            // Becomes an explicit set (may be empty = compare everything),
            // sent on every subsequent fetch until the capture type changes.
            _diffIgnore = [...el.querySelectorAll('.ignore-chip.on')].map(c => c.dataset.field);
            rerunDiff(_diffMountEl || el);
        });
    });
}

// ── Text diff card: native side-by-side aligned view (config_diff row
//    contract), with a unified-diff fallback when no aligned payload. ──

function textDiffCardHtml(result) {
    if (result.aligned && result.aligned.rows && result.aligned.rows.length) {
        return `
            <div class="card" style="margin-top:14px">
                <div class="card-head-alt">
                    <span class="text-faint fw-600 fs-12">Text Diff</span>
                    <span class="cdiff-controls">
                        <label class="cdiff-ctl"><input type="checkbox" id="cdiff-fold" checked> fold unchanged</label>
                        <input type="number" id="cdiff-ctx" class="cdiff-ctx" value="3" min="0" max="20" title="context lines around each change">
                        <label class="cdiff-ctl" title="ignore config-change timestamps and other volatile lines"><input type="checkbox" id="cdiff-mask" ${_diffMask ? 'checked' : ''}> mask volatile lines</label>
                    </span>
                </div>
                <div class="cdiff-wrap" id="cdiff-wrap">${renderAlignedTable(result.aligned.rows, 3, true)}</div>
            </div>`;
    }
    if (result.raw_diff) {
        return `
            <div class="card" style="margin-top:14px">
                <div class="card-head-alt" style="border-bottom:none"><span class="text-faint fw-600 fs-12">Raw Diff</span></div>
                <pre class="config-pre rawdiff" style="max-height:320px">${renderRawDiff(result.raw_diff)}</pre>
            </div>`;
    }
    return '';
}

function wireTextDiff(el, result) {
    _diffMountEl = el;
    const wrap   = el.querySelector('#cdiff-wrap');
    const foldCb = el.querySelector('#cdiff-fold');
    const ctxIn  = el.querySelector('#cdiff-ctx');
    const maskCb = el.querySelector('#cdiff-mask');
    if (!wrap) return;

    const redraw = () => {
        wrap.innerHTML = renderAlignedTable(
            result.aligned.rows, parseInt(ctxIn.value, 10) || 0, foldCb.checked
        );
        wireFolds(wrap);
    };
    foldCb?.addEventListener('change', redraw);
    ctxIn?.addEventListener('change', redraw);
    // Masking changes the comparison key, so it must re-run server-side.
    maskCb?.addEventListener('change', () => { _diffMask = maskCb.checked; rerunDiff(el); });
    wireFolds(wrap);
}

async function rerunDiff(el) {
    el.innerHTML = '<div class="loading">Computing diff…</div>';
    try {
        const result = await api.diff(device.id, captureType, _lastDiff.from, _lastDiff.to, _diffMask, _diffIgnore);
        renderDiffResult(el, result);
    } catch {
        el.innerHTML = `<div class="card"><div class="empty-state">Could not compute ${escapeHtml(captureType)} diff.</div></div>`;
    }
}

// ── Aligned-row renderer (ported from the config_diff viewer; consumes
//    the row contract, computes nothing). ──

function renderAlignedTable(rows, ctx, fold) {
    const hasChange = rows.some(r => r.type !== 'equal');
    const inner = (fold && hasChange)
        ? alignedWithFolds(rows, ctx)
        : rows.map(alignedRowHtml).join('');
    if (!hasChange) return `<div class="cdiff-identical">No textual changes between these versions.</div>`;
    return `<table class="cdiff"><tbody>${inner}</tbody></table>`;
}

// One cell, using intra-line word segments when present (replace rows).
function alignedCellHtml(cell, side) {
    if (!cell || cell.num === null) return '';
    if (cell.segments) {
        const cls = side === 'l' ? 'cdiff-chg-l' : 'cdiff-chg-r';
        return cell.segments.map(s =>
            s.changed ? `<span class="${cls}">${escapeHtml(s.text)}</span>` : escapeHtml(s.text)
        ).join('');
    }
    return escapeHtml(cell.text ?? '');
}

function alignedRowHtml(r) {
    const lnum = r.left.num  ?? '';
    const rnum = r.right.num ?? '';
    const lgap = r.left.num  === null ? ' cdiff-gap' : '';
    const rgap = r.right.num === null ? ' cdiff-gap' : '';
    return `<tr class="cdiff-${r.type}">`
        + `<td class="cdiff-num">${lnum}</td>`
        + `<td class="cdiff-txt cdiff-l${lgap}">${alignedCellHtml(r.left, 'l')}</td>`
        + `<td class="cdiff-num">${rnum}</td>`
        + `<td class="cdiff-txt cdiff-r${rgap}">${alignedCellHtml(r.right, 'r')}</td>`
        + `</tr>`;
}

// Collapse long unchanged runs, keeping `ctx` lines around each change.
// Hidden rows carry a group id (no nested tbody — invalid HTML); the fold
// marker toggles every row in its group.
function alignedWithFolds(rows, ctx) {
    const keep = new Array(rows.length).fill(false);
    rows.forEach((r, i) => {
        if (r.type !== 'equal') {
            for (let k = Math.max(0, i - ctx); k <= Math.min(rows.length - 1, i + ctx); k++) keep[k] = true;
        }
    });
    const out = [];
    let i = 0, group = 0;
    while (i < rows.length) {
        if (keep[i]) { out.push(alignedRowHtml(rows[i])); i++; continue; }
        let j = i;
        while (j < rows.length && !keep[j]) j++;
        const n = j - i;
        const gid = 'cg' + (group++);
        out.push(`<tr class="cdiff-fold" data-group="${gid}"><td colspan="4">··· ${n} unchanged line${n > 1 ? 's' : ''} (click to expand) ···</td></tr>`);
        for (let k = i; k < j; k++) {
            out.push(alignedRowHtml(rows[k]).replace('<tr ', `<tr data-fold="${gid}" hidden `));
        }
        i = j;
    }
    return out.join('');
}

function wireFolds(container) {
    container.querySelectorAll('tr.cdiff-fold').forEach(tr => {
        tr.addEventListener('click', () => {
            const gid = tr.getAttribute('data-group');
            container.querySelectorAll(`tr[data-fold="${gid}"]`).forEach(r => { r.hidden = !r.hidden; });
        });
    });
}

// Colorize a unified diff (difflib.unified_diff output) line-by-line.
// +/- get add/del coloring; @@ hunk headers and ---/+++ file headers are
// dimmed so the eye lands on actual content changes.
function renderRawDiff(text) {
    return (text || '').split('\n').map(line => {
        const safe = escapeHtml(line);
        if (line.startsWith('+++') || line.startsWith('---'))
            return `<span class="rawdiff-meta">${safe}</span>`;
        if (line.startsWith('@@'))
            return `<span class="rawdiff-hunk">${safe}</span>`;
        if (line.startsWith('+'))
            return `<span class="rawdiff-add">${safe}</span>`;
        if (line.startsWith('-'))
            return `<span class="rawdiff-del">${safe}</span>`;
        return safe;
    }).join('\n');
}


// ══════════════════════════════════════════════════════════════
// Destroy
// ══════════════════════════════════════════════════════════════

export function destroy() {
    _collectGen++;                 // stops any in-flight poll loop
    clearTimeout(_collectTick);
    _collectTick = null;
    _collectReq = null;
    _collectPlan = [];
    device = null;
    currentTab = 'raw';
    captureType = 'config';
    _pendingDiff = null;
    _snapCache = {};
    _snapSel = [];
    _diffMask = false;
    _diffIgnore = null;
    _lastDiff = { from: null, to: null };
    _diffMountEl = null;
    _refData = null;
    _sortCol = -1;
    _sortAsc = true;
    document.querySelectorAll('.topbar-back').forEach(el => el.remove());
    document.querySelectorAll('.topbar-left .badge').forEach(el => el.remove());
}