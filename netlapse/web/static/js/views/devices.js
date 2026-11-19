/**
 * Devices View
 *
 * Filterable device inventory table.
 * Data source: /api/v1/devices, /api/v1/sites
 */

import { api } from '../api.js';
import { badge, code, timeAgo, searchIcon, escapeHtml, icons } from '../components.js';

let allDevices = [];
let allSites = [];
let _refData = null;   // {sites, platforms, roles, credentials} — lazy, shared by Add modal

export function render() {
    return `
        <div class="filter-row">
            <div class="search-wrap">
                ${searchIcon()}
                <input class="input input-search" id="dev-search"
                    placeholder="Search hostname or IP…" style="width:100%">
            </div>
            <select class="select" id="dev-site-filter">
                <option value="">All sites</option>
            </select>
            <select class="select" id="dev-status-filter">
                <option value="">All statuses</option>
                <option value="active">Active</option>
                <option value="maintenance">Maintenance</option>
                <option value="failed">Failed</option>
            </select>
            <span class="ml-auto text-faint mono fs-12" id="dev-count"></span>
            <button class="btn btn-sm" id="dev-import-btn" style="display:none"
                title="Import or update devices from a Secure Cartography map.json (admin)">Import map</button>
            <button class="btn btn-sm btn-primary" id="dev-add-btn">${icons.plus} Add Device</button>
        </div>
        <div class="card" id="dev-table-card">
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Device</th><th>IP Address</th><th>Platform</th>
                        <th>Site</th><th>Role</th><th>Collected</th><th>Status</th>
                    </tr></thead>
                    <tbody id="dev-body"></tbody>
                </table>
            </div>
        </div>

        <!-- Add device modal -->
        <div class="modal-overlay" id="dev-add-modal" style="display:none">
            <div class="modal-container" style="max-width:640px">
                <div class="modal-header">
                    <div class="modal-title">Add Device</div>
                    <button class="modal-close" id="dev-add-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="dev-add-body"><div class="loading">Loading…</div></div>
                    <div id="dev-add-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="dev-add-cancel">Cancel</button>
                        <button class="btn btn-primary" id="dev-add-save">Create</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init(params) {
    // Check URL params for pre-set filters
    const hashParams = new URLSearchParams(location.hash.split('?')[1] || '');
    const presetSite = hashParams.get('site') || '';

    const [devices, sites] = await Promise.all([
        api.devices().catch(() => []),
        api.sites().catch(() => []),
    ]);

    allDevices = devices;
    allSites = sites;

    // Populate site filter
    const siteSelect = document.getElementById('dev-site-filter');
    sites.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.slug;
        opt.textContent = `${s.slug.toUpperCase()} — ${s.name || ''}`;
        siteSelect.appendChild(opt);
    });
    if (presetSite) siteSelect.value = presetSite;

    // Wire filters
    document.getElementById('dev-search').addEventListener('input', renderTable);
    siteSelect.addEventListener('change', renderTable);
    document.getElementById('dev-status-filter').addEventListener('change', renderTable);

    // Wire Add Device modal
    document.getElementById('dev-add-btn').addEventListener('click', openAddModal);

    // Map import lives in the setup wizard's Devices step (preview, then
    // import; re-running is idempotent). Admin-only, like the wizard.
    const importBtn = document.getElementById('dev-import-btn');
    importBtn.addEventListener('click', () => { location.hash = '#/setup?step=devices'; });
    api.auth.me().then(me => {
        const u = me && (me.user || me);   // /auth/me returns { user, auth }
        if (u && u.role === 'admin' && document.getElementById('dev-import-btn')) importBtn.style.display = '';
    }).catch(() => { /* not signed in or no auth: leave hidden */ });
    document.getElementById('dev-add-close').addEventListener('click', closeAddModal);
    document.getElementById('dev-add-cancel').addEventListener('click', closeAddModal);
    document.getElementById('dev-add-modal').addEventListener('click', (e) => {
        if (e.target.id === 'dev-add-modal') closeAddModal();
    });
    document.getElementById('dev-add-save').addEventListener('click', saveNewDevice);

    renderTable();
}

function renderTable() {
    const search = (document.getElementById('dev-search').value || '').toLowerCase();
    const site   = document.getElementById('dev-site-filter').value;
    const status = document.getElementById('dev-status-filter').value;

    const filtered = allDevices.filter(d => {
        if (site && (d.site_slug || '') !== site) return false;
        if (status && d.status !== status) return false;
        if (search) {
            const name = (d.name || '').toLowerCase();
            const ip = d.primary_ip4 || '';
            if (!name.includes(search) && !ip.includes(search)) return false;
        }
        return true;
    });

    document.getElementById('dev-count').textContent =
        `${filtered.length} device${filtered.length !== 1 ? 's' : ''}`;

    const body = document.getElementById('dev-body');
    if (!filtered.length) {
        body.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">No devices match filters</td></tr>';
        return;
    }

    body.innerHTML = filtered.map(d => `
        <tr class="row-link" data-device-id="${d.id}">
            <td class="fw-600">${escapeHtml(d.name)}</td>
            <td class="mono text-muted fs-12">${escapeHtml(d.primary_ip4 || '')}</td>
            <td>${code(d.platform_slug || d.platform || '')}</td>
            <td class="fs-12 fw-500">${escapeHtml(d.site || d.site_slug || '')}</td>
            <td class="text-muted fs-12">${escapeHtml(d.role || '')}</td>
            <td class="text-muted fs-12">${timeAgo(d.last_collected_at)}</td>
            <td>${badge(d.last_collection_status || d.status || 'unknown')}</td>
        </tr>
    `).join('');

    // Row click → device detail
    body.querySelectorAll('tr[data-device-id]').forEach(row => {
        row.addEventListener('click', () => {
            location.hash = `#/device/${row.dataset.deviceId}`;
        });
    });
}

export function destroy() {
    allDevices = [];
    allSites = [];
}

// ══════════════════════════════════════════════════════════════
// Add Device modal
// ══════════════════════════════════════════════════════════════

async function loadRefData() {
    if (_refData) return _refData;
    const [sites, platforms, roles, credentials] = await Promise.all([
        api.sites().catch(() => []),
        api.platforms().catch(() => []),
        api.roles().catch(() => []),
        // Vault is the real credential store; device.credential_id is a vault ID.
        api.admin.vaultCredentials().catch(() => []),
    ]);
    _refData = {
        sites, platforms, roles,
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

async function openAddModal() {
    const modal = document.getElementById('dev-add-modal');
    modal.style.display = 'flex';
    document.getElementById('dev-add-error').style.display = 'none';

    const body = document.getElementById('dev-add-body');
    body.innerHTML = '<div class="loading">Loading…</div>';

    const ref = await loadRefData();

    // Default the site selector to whatever the list is currently filtered to.
    const presetSiteSlug = document.getElementById('dev-site-filter').value || '';
    const presetSite = ref.sites.find(s => s.slug === presetSiteSlug);

    const statusOpts = ['active', 'planned', 'staged', 'decommissioned'].map(s =>
        `<option value="${s}"${s === 'active' ? ' selected' : ''}>${s}</option>`
    ).join('');

    body.innerHTML = `
        <details class="form-details" open>
            <summary class="form-summary">Identity</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Name <span class="text-error">*</span></label>
                    <input class="input mono" id="da-name" type="text" placeholder="border01.site1" autocomplete="off">
                </div>
                <div class="form-group">
                    <label class="form-label">Status</label>
                    <select class="select" id="da-status">${statusOpts}</select>
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Site <span class="text-error">*</span></label>
                    <select class="select" id="da-site">
                        ${selectOpts(ref.sites, 'id', s => `${s.slug} (${s.device_count || 0})`,
                            presetSite ? presetSite.id : '', ref.sites.length ? null : '— no sites —')}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">Platform</label>
                    <select class="select" id="da-platform">
                        ${selectOpts(ref.platforms, 'id', p => `${p.slug} (${p.device_count})`, '', '— none —')}
                    </select>
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Management IP</label>
                    <input class="input mono" id="da-ip4" type="text" placeholder="10.0.0.1" autocomplete="off">
                </div>
                <div class="form-group">
                    <label class="form-label">Role</label>
                    <select class="select" id="da-role">
                        ${selectOpts(ref.roles, 'id', r => `${r.slug} (${r.device_count})`, '', '— none —')}
                    </select>
                </div>
            </div>
        </details>

        <details class="form-details" open>
            <summary class="form-summary">Collection</summary>
            <div class="form-grid" style="margin-top:12px">
                <div class="form-group">
                    <label class="form-label">Credential</label>
                    <select class="select" id="da-credential">
                        <option value="">Shared default</option>
                        ${selectOpts(ref.credentials, 'id',
                            c => `${c.name} (${c.username})${c.is_default ? ' ★' : ''}`, '')}
                    </select>
                </div>
                <div class="form-group">
                    <label class="form-label">SSH Port</label>
                    <input class="input mono" id="da-ssh-port" type="number" min="1" max="65535"
                        value="22" style="width:100px">
                </div>
            </div>
            <div class="form-grid">
                <div class="form-group">
                    <label class="form-label">Legacy SSH</label>
                    <select class="select" id="da-legacy-ssh">
                        <option value="" selected>Platform default</option>
                        <option value="1">Force on</option>
                        <option value="0">Force off</option>
                    </select>
                </div>
                <div class="form-group" style="display:flex;align-items:flex-end">
                    <label class="form-label-inline">
                        <input type="checkbox" id="da-collection-enabled" checked> Collection Enabled
                    </label>
                </div>
            </div>
        </details>

        <details class="form-details">
            <summary class="form-summary">Metadata</summary>
            <div class="form-group" style="margin-top:12px">
                <label class="form-label">Description</label>
                <input class="input" id="da-desc" type="text" placeholder="added manually" autocomplete="off">
            </div>
        </details>
    `;

    setTimeout(() => document.getElementById('da-name').focus(), 100);
}

function closeAddModal() {
    document.getElementById('dev-add-modal').style.display = 'none';
}

async function saveNewDevice() {
    const errEl = document.getElementById('dev-add-error');
    errEl.style.display = 'none';

    const name = document.getElementById('da-name').value.trim();
    if (!name) {
        errEl.textContent = 'Name is required.';
        errEl.style.display = 'block';
        return;
    }
    const siteId = document.getElementById('da-site').value;
    if (!siteId) {
        errEl.textContent = 'Site is required. Create a site (or import a map) first.';
        errEl.style.display = 'block';
        return;
    }

    const platformId = document.getElementById('da-platform').value;
    const roleId = document.getElementById('da-role').value;
    const credId = document.getElementById('da-credential').value;
    const legacyVal = document.getElementById('da-legacy-ssh').value;

    const payload = {
        name,
        site_id: parseInt(siteId),
        status: document.getElementById('da-status').value,
        primary_ip4: document.getElementById('da-ip4').value.trim() || null,
        platform_id: platformId ? parseInt(platformId) : null,
        role_id: roleId ? parseInt(roleId) : null,
        credential_id: credId ? parseInt(credId) : null,
        ssh_port: parseInt(document.getElementById('da-ssh-port').value) || 22,
        legacy_ssh: legacyVal === '' ? null : parseInt(legacyVal),
        collection_enabled: document.getElementById('da-collection-enabled').checked,
        description: document.getElementById('da-desc').value.trim() || null,
    };

    const saveBtn = document.getElementById('dev-add-save');
    saveBtn.disabled = true;
    saveBtn.textContent = 'Creating…';

    try {
        const created = await api.createDevice(payload);
        closeAddModal();
        // Straight to the new device's detail page.
        location.hash = `#/device/${created.id}`;
    } catch (e) {
        errEl.textContent = e.message || 'Create failed';
        errEl.style.display = 'block';
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Create';
    }
}