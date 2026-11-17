/**
 * Users View (Admin)
 *
 * Manage application login accounts — create, reset password, enable/disable,
 * delete. Backed by /api/v1/admin/users. The "admin" role is the only role
 * today; the column is shown so the model reads correctly when more land.
 * Self-affecting destructive actions are hidden in the UI and also blocked
 * server-side.
 */

import { api } from '../api.js';
import { badge, timeAgo, formatTimestamp, icons, escapeHtml } from '../components.js';

let _users = [];
let _me = null;
let _mode = {};
let _pwTargetId = null;
let _delTarget = null;

export function render() {
    return `
        <div id="auth-mode-banner"></div>
        <div class="card">
            <div class="card-head" style="justify-content:space-between">
                <span class="card-title">Application Users</span>
                <button class="btn btn-primary btn-sm" id="btn-add-user">${icons.plus} Add User</button>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Username</th><th>Role</th><th>Status</th>
                        <th>Last Login</th><th>Created</th><th style="text-align:right"></th>
                    </tr></thead>
                    <tbody id="users-body">
                        <tr><td colspan="6" class="loading">Loading…</td></tr>
                    </tbody>
                </table>
            </div>
            <div style="padding:12px 16px;border-top:1px solid var(--border);font-size:12px"
                 class="text-muted">
                Passwords are hashed (scrypt) and never stored in the vault.
                Disabling or deleting a user immediately revokes their sessions.
            </div>
        </div>

        <!-- Add user modal -->
        <div class="modal-overlay" id="user-modal" style="display:none">
            <div class="modal-container" style="max-width:440px">
                <div class="modal-header">
                    <div class="modal-title">Add User</div>
                    <button class="modal-close" id="user-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div class="form-group">
                        <label class="form-label">Username</label>
                        <input class="input" id="nu-username" autocomplete="off" spellcheck="false">
                    </div>
                    <div class="form-group">
                        <label class="form-label">Password</label>
                        <input class="input" id="nu-password" type="password" autocomplete="new-password">
                        <div class="text-muted" style="font-size:12px">Minimum 8 characters.</div>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Role</label>
                        <select class="select" id="nu-role">
                            <option value="admin">admin — full control</option>
                            <option value="viewer">viewer — read-only</option>
                        </select>
                    </div>
                    <div id="user-form-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="user-form-cancel">Cancel</button>
                        <button class="btn btn-primary" id="user-form-save">Create</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Reset password modal -->
        <div class="modal-overlay" id="pw-modal" style="display:none">
            <div class="modal-container" style="max-width:440px">
                <div class="modal-header">
                    <div class="modal-title" id="pw-modal-title">Reset Password</div>
                    <button class="modal-close" id="pw-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <label class="form-label">New password</label>
                    <input class="input" id="pw-value" type="password" autocomplete="new-password">
                    <div class="text-muted" style="font-size:12px;margin-top:6px">
                        Minimum 8 characters. Resetting signs the user out everywhere.
                    </div>
                    <div id="pw-form-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="pw-form-cancel">Cancel</button>
                        <button class="btn btn-primary" id="pw-form-save">Set Password</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Delete confirm modal -->
        <div class="modal-overlay" id="del-modal" style="display:none">
            <div class="modal-container" style="max-width:400px">
                <div class="modal-header">
                    <div class="modal-title">Delete User</div>
                    <button class="modal-close" id="del-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <p id="del-msg" style="margin:0 0 4px"></p>
                    <div id="del-form-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="del-form-cancel">Cancel</button>
                        <button class="btn btn-danger" id="del-form-go">Delete</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    document.getElementById('btn-add-user').addEventListener('click', openAddUser);
    wireModal('user-modal', ['user-modal-close', 'user-form-cancel']);
    wireModal('pw-modal', ['pw-modal-close', 'pw-form-cancel']);
    wireModal('del-modal', ['del-modal-close', 'del-form-cancel']);
    document.getElementById('user-form-save').addEventListener('click', saveUser);
    document.getElementById('pw-form-save').addEventListener('click', savePassword);
    document.getElementById('del-form-go').addEventListener('click', confirmDelete);

    try {
        const meResp = await api.auth.me();
        _me = meResp.user;
        _mode = meResp.auth || {};
    } catch { _me = null; _mode = {}; }
    applyMode();
    await reload();
}

export function destroy() {
    ['user-modal', 'pw-modal', 'del-modal'].forEach(closeModal);
}

// Reflect the active auth mode: a banner explaining the role model, and an
// "Add User" button that's disabled when local accounts have no login path.
function applyMode() {
    const banner = document.getElementById('auth-mode-banner');
    if (banner) banner.innerHTML = modeBannerHtml();

    const add = document.getElementById('btn-add-user');
    if (add) {
        if (_mode.ldap_only) {
            add.disabled = true;
            add.title = 'LDAP-only mode: local accounts cannot log in. ' +
                        'Add users in the directory instead.';
        } else {
            add.disabled = false;
            add.title = '';
        }
    }
}

function modeBannerHtml() {
    if (!_mode.directory_authoritative) return '';   // pure local: nothing to explain
    const mock = _mode.ldap_mock
        ? ' <strong style="color:var(--warning,#d97706)">MOCK directory (testing — not a real DC).</strong>'
        : '';
    const fallback = _mode.local_enabled
        ? 'Local accounts remain a valid break-glass fallback.'
        : 'Local accounts have <strong>no login path</strong> in this mode — keep a break-glass plan (see auth docs).';
    return `
        <div class="card" style="margin-bottom:12px;border-left:3px solid var(--accent,#3b82f6)">
            <div style="padding:12px 16px;font-size:13px" class="text-muted">
                <strong>Authentication: ${escapeHtml(_mode.provider)}</strong> —
                roles are <strong>directory-authoritative</strong>: group membership
                is re-evaluated on every login, so role edits here do not persist
                for directory users. ${fallback}${mock}
            </div>
        </div>`;
}

async function reload() {
    const body = document.getElementById('users-body');
    try {
        _users = (await api.admin.users.list()).users;
        body.innerHTML = _users.map(rowHtml).join('') ||
            '<tr><td colspan="6" class="text-muted" style="padding:16px">No users.</td></tr>';
        wireRowActions();
    } catch (e) {
        body.innerHTML = `<tr><td colspan="6" class="form-error">${escapeHtml(e.message)}</td></tr>`;
    }
}

function rowHtml(u) {
    const isMe = _me && u.id === _me.id;
    const isDirectory = u.auth_source === 'ldap';
    // A local account can only log in when a local provider is in the chain.
    const localCanLogin = !isDirectory && _mode.local_enabled !== false
                          && !_mode.ldap_only;
    const inertLocal = !isDirectory && _mode.ldap_only;

    let status = u.is_active ? badge('active') : badge('disabled');
    if (inertLocal) {
        // Active in the DB, but no login path while LDAP-only. Don't fake a
        // "disabled" pill (that means a deliberate admin action); annotate.
        status += ` <span class="text-muted" style="font-size:11px"
                     title="LDAP-only mode: this local account cannot log in.">· no login path</span>`;
    }

    const you = isMe ? ' <span class="text-muted" style="font-size:11px">(you)</span>' : '';
    const src = _mode.directory_authoritative ? sourceTag(isDirectory) : '';
    const lastLogin = u.last_login_at ? timeAgo(u.last_login_at) : '<span class="text-muted">never</span>';
    const created = u.created_at ? formatTimestamp(u.created_at) : '—';

    // Self-protective: no disable/delete on your own row.
    const toggle = isMe ? '' : `
        <button class="btn btn-sm" data-act="toggle" data-id="${u.id}" data-active="${u.is_active ? 1 : 0}"
                title="${u.is_active ? 'Disable' : 'Enable'}">
            ${u.is_active ? icons.toggleOn : icons.toggleOff}
        </button>`;
    const del = isMe ? '' : `
        <button class="btn btn-sm" data-act="del" data-id="${u.id}" style="color:#ef4444"
                data-name="${escapeHtml(u.username)}" title="Delete">${icons.trash}</button>`;
    // Password reset only applies to a local account that can actually log in.
    const pw = localCanLogin ? `
        <button class="btn btn-sm" data-act="pw" data-id="${u.id}"
                data-name="${escapeHtml(u.username)}" title="Reset password">${icons.edit}</button>` : '';

    return `
        <tr>
            <td><strong>${escapeHtml(u.username)}</strong>${you}${src}</td>
            <td>${roleCellHtml(u, isDirectory)}</td>
            <td>${status}</td>
            <td>${lastLogin}</td>
            <td>${created}</td>
            <td style="text-align:right;white-space:nowrap">
                ${pw}${toggle}${del}
            </td>
        </tr>`;
}

function sourceTag(isDirectory) {
    const label = isDirectory ? 'Directory' : 'Local';
    const title = isDirectory
        ? 'Provisioned from the directory on login; role managed by group membership.'
        : 'Local account stored in app_users.';
    return ` <span class="badge badge-neutral" style="font-size:10px;vertical-align:middle"
               title="${title}">${label}</span>`;
}

// Role is a live control only for local accounts that can log in. Directory
// users (and inert local accounts under LDAP-only) get a static, explained role.
function roleCellHtml(u, isDirectory) {
    const editable = !isDirectory && _mode.local_enabled !== false && !_mode.ldap_only;
    if (editable) return roleSelectHtml(u);
    const why = isDirectory
        ? 'Managed by directory group membership — edit in the directory.'
        : 'LDAP-only mode: this local account cannot log in.';
    return `<span title="${why}">${escapeHtml(u.role)}
              <span class="text-muted" style="font-size:11px">(managed)</span></span>`;
}

function roleSelectHtml(u) {
    const opts = ['admin', 'viewer'].map(r =>
        `<option value="${r}" ${u.role === r ? 'selected' : ''}>${r}</option>`).join('');
    return `<select class="select" data-act="role" data-id="${u.id}"
              data-prev="${escapeHtml(u.role)}"
              style="padding:3px 8px;font-size:12px;width:auto">${opts}</select>`;
}

function wireRowActions() {
    document.querySelectorAll('[data-act="pw"]').forEach(b =>
        b.addEventListener('click', () => openPassword(b.dataset.id, b.dataset.name)));
    document.querySelectorAll('[data-act="toggle"]').forEach(b =>
        b.addEventListener('click', () => toggleActive(+b.dataset.id, b.dataset.active === '0')));
    document.querySelectorAll('[data-act="del"]').forEach(b =>
        b.addEventListener('click', () => openDelete(+b.dataset.id, b.dataset.name)));
    document.querySelectorAll('[data-act="role"]').forEach(s =>
        s.addEventListener('change', () => changeRole(s)));
}

async function changeRole(sel) {
    const id = +sel.dataset.id, role = sel.value, prev = sel.dataset.prev;
    if (role === prev) return;
    sel.disabled = true;
    try {
        await api.admin.users.update(id, { role });
    } catch (e) {
        alert(cleanErr(e));
    }
    sel.disabled = false;
    await reload();  // reflect canonical state (and any guard rejection)
}

// ── Add user ──
function openAddUser() {
    document.getElementById('nu-username').value = '';
    document.getElementById('nu-password').value = '';
    document.getElementById('nu-role').value = 'admin';
    hideErr('user-form-error');
    openModal('user-modal');
    document.getElementById('nu-username').focus();
}

async function saveUser() {
    const username = document.getElementById('nu-username').value.trim();
    const password = document.getElementById('nu-password').value;
    const role = document.getElementById('nu-role').value;
    if (!username || !password) return showErr('user-form-error', 'Username and password are required.');
    const btn = document.getElementById('user-form-save');
    btn.disabled = true; btn.textContent = 'Creating…';
    try {
        await api.admin.users.create({ username, password, role });
        closeModal('user-modal');
        await reload();
    } catch (e) {
        showErr('user-form-error', cleanErr(e));
    } finally {
        btn.disabled = false; btn.textContent = 'Create';
    }
}

// ── Reset password ──
function openPassword(id, name) {
    _pwTargetId = +id;
    document.getElementById('pw-modal-title').textContent = `Reset Password — ${name}`;
    document.getElementById('pw-value').value = '';
    hideErr('pw-form-error');
    openModal('pw-modal');
    document.getElementById('pw-value').focus();
}

async function savePassword() {
    const password = document.getElementById('pw-value').value;
    if (!password) return showErr('pw-form-error', 'Password is required.');
    const btn = document.getElementById('pw-form-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
        await api.admin.users.setPassword(_pwTargetId, password);
        closeModal('pw-modal');
    } catch (e) {
        showErr('pw-form-error', cleanErr(e));
    } finally {
        btn.disabled = false; btn.textContent = 'Set Password';
    }
}

// ── Enable / disable ──
async function toggleActive(id, makeActive) {
    try {
        await api.admin.users.update(id, { is_active: makeActive });
    } catch (e) {
        alert(cleanErr(e));
    }
    await reload();
}

// ── Delete ──
function openDelete(id, name) {
    _delTarget = id;
    document.getElementById('del-msg').textContent =
        `Permanently delete "${name}"? This revokes their sessions and cannot be undone.`;
    hideErr('del-form-error');
    openModal('del-modal');
}

async function confirmDelete() {
    const btn = document.getElementById('del-form-go');
    btn.disabled = true; btn.textContent = 'Deleting…';
    try {
        await api.admin.users.remove(_delTarget);
        closeModal('del-modal');
        await reload();
    } catch (e) {
        showErr('del-form-error', cleanErr(e));
    } finally {
        btn.disabled = false; btn.textContent = 'Delete';
    }
}

// ── helpers ──
function openModal(id) { document.getElementById(id).style.display = 'flex'; }
function closeModal(id) { const m = document.getElementById(id); if (m) m.style.display = 'none'; }
function wireModal(id, closers) {
    closers.forEach(c => {
        const el = document.getElementById(c);
        if (el) el.addEventListener('click', () => closeModal(id));
    });
}
function showErr(id, msg) { const e = document.getElementById(id); e.textContent = msg; e.style.display = ''; }
function hideErr(id) { const e = document.getElementById(id); e.style.display = 'none'; }
function cleanErr(e) {
    // request() throws "400 Bad Request: {\"error\":\"...\"}" — surface the message.
    const m = String(e.message || e);
    const j = m.indexOf('{');
    if (j !== -1) { try { return JSON.parse(m.slice(j)).error || m; } catch {} }
    return m;
}