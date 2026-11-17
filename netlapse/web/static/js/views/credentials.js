/**
 * Credentials View (Admin)
 *
 * Manage vault credentials — add SSH creds, set defaults, bulk assign.
 * Data source: /api/v1/admin/vault/*
 */

import { api } from '../api.js';
import { badge, timeAgo, icons, escapeHtml } from '../components.js';

let _credentials = [];
let _sites = [];
let _modalOpen = false;

export function render() {
    return `
        <div id="creds-locked" style="display:none">
            <div class="card" style="padding:32px;text-align:center">
                <div class="stat-label" style="font-size:14px;margin-bottom:8px">Vault Locked</div>
                <div class="text-muted">
                    Set <code class="code">NETLAPSE_VAULT_PASSWORD</code> and restart
                    to unlock credential management.
                </div>
            </div>
        </div>
        <div id="creds-main">
            <div class="card">
                <div class="card-head" style="justify-content:space-between">
                    <span class="card-title">SSH Credentials</span>
                    <div style="display:flex;gap:8px">
                        <button class="btn btn-sm" id="btn-assign">${icons.play} Bulk Assign</button>
                        <button class="btn btn-primary btn-sm" id="btn-add-cred">+ Add Credential</button>
                    </div>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead><tr>
                            <th>Name</th><th>Username</th><th>Auth</th>
                            <th>Port</th><th>Default</th><th>Last Test</th>
                            <th>Created</th><th></th>
                        </tr></thead>
                        <tbody id="creds-body">
                            <tr><td colspan="8" class="loading">Loading…</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- Add credential modal -->
        <div class="modal-overlay" id="cred-modal" style="display:none">
            <div class="modal-container" style="max-width:520px">
                <div class="modal-header">
                    <div class="modal-title" id="cred-modal-title">Add SSH Credential</div>
                    <button class="modal-close" id="cred-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="cred-form-body"></div>
                    <div id="cred-form-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="cred-form-cancel">Cancel</button>
                        <button class="btn btn-primary" id="cred-form-save">Save</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Assign modal -->
        <div class="modal-overlay" id="assign-modal" style="display:none">
            <div class="modal-container" style="max-width:440px">
                <div class="modal-header">
                    <div class="modal-title">Bulk Assign Credential</div>
                    <button class="modal-close" id="assign-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div id="assign-form-body"></div>
                    <div id="assign-form-error" class="form-error" style="display:none"></div>
                    <div id="assign-form-result" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="assign-form-cancel">Cancel</button>
                        <button class="btn btn-primary" id="assign-form-go">Assign</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Delete confirm modal -->
        <div class="modal-overlay" id="delete-modal" style="display:none">
            <div class="modal-container" style="max-width:400px">
                <div class="modal-header">
                    <div class="modal-title">Delete Credential</div>
                    <button class="modal-close" id="delete-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <p id="delete-confirm-text" style="margin-bottom:4px"></p>
                    <div class="form-actions">
                        <button class="btn" id="delete-cancel">Cancel</button>
                        <button class="btn btn-danger" id="delete-confirm">Delete</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    // Check vault status
    try {
        const status = await api.admin.vaultStatus();
        if (!status.unlocked) {
            document.getElementById('creds-locked').style.display = '';
            document.getElementById('creds-main').style.display = 'none';
            return;
        }
    } catch {
        document.getElementById('creds-locked').style.display = '';
        document.getElementById('creds-main').style.display = 'none';
        return;
    }

    // Load sites for assign dropdown
    try { _sites = await api.sites(); } catch { _sites = []; }

    await loadCredentials();

    // Wire buttons
    document.getElementById('btn-add-cred').addEventListener('click', openAddModal);
    document.getElementById('btn-assign').addEventListener('click', openAssignModal);

    // Modal close handlers
    _wireModal('cred-modal', 'cred-modal-close', 'cred-form-cancel');
    _wireModal('assign-modal', 'assign-modal-close', 'assign-form-cancel');
    _wireModal('delete-modal', 'delete-modal-close', 'delete-cancel');

    document.getElementById('cred-form-save').addEventListener('click', saveCredential);
    document.getElementById('assign-form-go').addEventListener('click', runAssign);
}

async function loadCredentials() {
    try {
        _credentials = await api.admin.vaultCredentials();
    } catch {
        _credentials = [];
    }
    renderTable();
}

function renderTable() {
    const body = document.getElementById('creds-body');
    if (!_credentials.length) {
        body.innerHTML = '<tr><td colspan="8" class="text-muted" style="text-align:center;padding:20px">No credentials. Click "+ Add Credential" to create one.</td></tr>';
        return;
    }

    body.innerHTML = _credentials.map(c => {
        const defBadge = c.is_default
            ? '<span class="badge badge-info"><span class="dot dot-info"></span>default</span>'
            : `<button class="btn btn-sm" data-set-default="${c.id}" style="font-size:11px;padding:2px 8px">Set default</button>`;

        const testBadge = c.last_test_success === true
            ? badge('success')
            : c.last_test_success === false
                ? badge('failed')
                : '<span class="text-faint">—</span>';

        return `<tr>
            <td><strong>${escapeHtml(c.name)}</strong>${c.description ? `<br><span class="text-faint fs-11">${escapeHtml(c.description)}</span>` : ''}</td>
            <td class="mono">${escapeHtml(c.username || '—')}</td>
            <td>${badge(c.auth || 'none')}</td>
            <td class="mono text-muted">22</td>
            <td>${defBadge}</td>
            <td>${testBadge}</td>
            <td class="text-muted">${timeAgo(c.created_at)}</td>
            <td>
                <div class="row-actions">
                    <button class="btn btn-sm btn-icon btn-ghost" data-delete="${c.id}" data-name="${escapeHtml(c.name)}" title="Delete">${icons.trash}</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    // Wire row actions
    body.querySelectorAll('[data-set-default]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = parseInt(btn.dataset.setDefault);
            try {
                await api.admin.setDefaultCredential(id);
                await loadCredentials();
            } catch (e) {
                alert('Failed: ' + e.message);
            }
        });
    });

    body.querySelectorAll('[data-delete]').forEach(btn => {
        btn.addEventListener('click', () => {
            openDeleteModal(parseInt(btn.dataset.delete), btn.dataset.name);
        });
    });
}


// ══════════════════════════════════════════════════════════════
// Add Credential Modal
// ══════════════════════════════════════════════════════════════

function openAddModal() {
    const form = document.getElementById('cred-form-body');
    form.innerHTML = `
        <div class="form-grid">
            <div class="form-group">
                <label class="form-label">Name</label>
                <input class="input" id="cf-name" placeholder="e.g. prod, lab, border-key" autocomplete="off">
            </div>
            <div class="form-group">
                <label class="form-label">Username</label>
                <input class="input" id="cf-username" placeholder="SSH username" autocomplete="off">
            </div>
        </div>
        <div class="form-group">
            <label class="form-label">Password</label>
            <input class="input" id="cf-password" type="password" placeholder="SSH password (or enable password with key auth)" autocomplete="new-password">
        </div>
        <details class="form-details">
            <summary class="form-summary">Key authentication</summary>
            <div class="form-group" style="margin-top:8px">
                <label class="form-label">Private Key (PEM content)</label>
                <textarea class="input" id="cf-key" rows="4" placeholder="Paste the contents of your private key file" style="font-family:var(--font-mono);font-size:11px"></textarea>
            </div>
            <div class="form-group">
                <label class="form-label">Key Passphrase</label>
                <input class="input" id="cf-key-pass" type="password" placeholder="If key is encrypted" autocomplete="new-password">
            </div>
        </details>
        <div class="form-group" style="margin-top:12px">
            <label class="form-label">Description</label>
            <input class="input" id="cf-desc" placeholder="Optional">
        </div>
        <label class="form-label-inline" style="margin-top:8px">
            <input type="checkbox" id="cf-default">
            Set as default credential
        </label>
    `;
    document.getElementById('cred-form-error').style.display = 'none';
    _showModal('cred-modal');
}

async function saveCredential() {
    const name = document.getElementById('cf-name').value.trim();
    const username = document.getElementById('cf-username').value.trim();
    const password = document.getElementById('cf-password').value || null;
    const keyContent = document.getElementById('cf-key').value.trim() || null;
    const keyPassphrase = document.getElementById('cf-key-pass').value || null;
    const description = document.getElementById('cf-desc').value.trim() || null;
    const isDefault = document.getElementById('cf-default').checked;

    if (!name || !username) {
        _showError('cred-form-error', 'Name and username are required.');
        return;
    }
    if (!password && !keyContent) {
        _showError('cred-form-error', 'Provide either a password or a private key.');
        return;
    }

    const btn = document.getElementById('cred-form-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
        await api.admin.addCredential({
            name, username, password,
            key_content: keyContent,
            key_passphrase: keyPassphrase,
            description,
            is_default: isDefault,
        });
        _hideModal('cred-modal');
        await loadCredentials();
    } catch (e) {
        _showError('cred-form-error', e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save';
    }
}


// ══════════════════════════════════════════════════════════════
// Delete Modal
// ══════════════════════════════════════════════════════════════

let _deleteTarget = null;

function openDeleteModal(id, name) {
    _deleteTarget = id;
    document.getElementById('delete-confirm-text').textContent =
        `Delete credential "${name}"? This cannot be undone. Devices using it will need a new credential assigned.`;
    _showModal('delete-modal');

    const btn = document.getElementById('delete-confirm');
    // Replace listener to avoid stacking
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', async () => {
        newBtn.disabled = true;
        newBtn.textContent = 'Deleting…';
        try {
            await api.admin.deleteCredential(_deleteTarget);
            _hideModal('delete-modal');
            await loadCredentials();
        } catch (e) {
            alert('Delete failed: ' + e.message);
        } finally {
            newBtn.disabled = false;
            newBtn.textContent = 'Delete';
        }
    });
}


// ══════════════════════════════════════════════════════════════
// Bulk Assign Modal
// ══════════════════════════════════════════════════════════════

function openAssignModal() {
    const form = document.getElementById('assign-form-body');
    const credOpts = _credentials.map(c =>
        `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)} (${escapeHtml(c.username || '')})</option>`
    ).join('');
    const siteOpts = '<option value="">(all sites)</option>' +
        _sites.map(s =>
            `<option value="${escapeHtml(s.slug)}">${escapeHtml(s.name || s.slug)}</option>`
        ).join('');

    form.innerHTML = `
        <div class="form-group">
            <label class="form-label">Credential</label>
            <select class="select" id="af-cred">${credOpts}</select>
        </div>
        <div class="form-group">
            <label class="form-label">Site Filter</label>
            <select class="select" id="af-site">${siteOpts}</select>
        </div>
    `;
    document.getElementById('assign-form-error').style.display = 'none';
    document.getElementById('assign-form-result').style.display = 'none';
    _showModal('assign-modal');
}

async function runAssign() {
    const credName = document.getElementById('af-cred').value;
    const site = document.getElementById('af-site').value || null;

    const btn = document.getElementById('assign-form-go');
    btn.disabled = true;
    btn.textContent = 'Assigning…';

    try {
        const result = await api.admin.assignCredential({
            credential_name: credName,
            site_filter: site,
        });
        const resultEl = document.getElementById('assign-form-result');
        resultEl.style.display = '';
        resultEl.innerHTML = `
            <div class="badge badge-success" style="margin-top:12px;padding:8px 12px;font-size:12px">
                Assigned "${escapeHtml(credName)}" to ${result.assigned} device(s)
            </div>
        `;
    } catch (e) {
        _showError('assign-form-error', e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Assign';
    }
}


// ══════════════════════════════════════════════════════════════
// Modal helpers
// ══════════════════════════════════════════════════════════════

function _showModal(id) {
    document.getElementById(id).style.display = '';
    _modalOpen = true;
}

function _hideModal(id) {
    document.getElementById(id).style.display = 'none';
    _modalOpen = false;
}

function _wireModal(overlayId, closeId, cancelId) {
    const overlay = document.getElementById(overlayId);
    document.getElementById(closeId).addEventListener('click', () => _hideModal(overlayId));
    if (cancelId) {
        document.getElementById(cancelId).addEventListener('click', () => _hideModal(overlayId));
    }
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) _hideModal(overlayId);
    });
}

function _showError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.style.display = '';
}

export function destroy() {
    _credentials = [];
    _sites = [];
    _modalOpen = false;
}
