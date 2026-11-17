/**
 * Settings View (Admin)
 *
 * System overview, config.yaml editor, data directory browser, backup/restore.
 * Data source: /api/v1/admin/system, /api/v1/admin/config, /api/v1/admin/backup
 */

import { api } from '../api.js';
import { badge, formatBytes, icons, escapeHtml } from '../components.js';

let _system = null;
let _bkTimer = null;     // backup job poll handle
let _bkGen = 0;          // bumps on destroy so stale polls exit

export function render() {
    return `
        <div id="settings-grid" class="admin-grid">
            <!-- Left: Component status + data directory -->
            <div class="admin-col">
                <div class="card" id="comp-card">
                    <div class="card-head">
                        <span class="card-title">Components</span>
                    </div>
                    <div id="comp-body" style="padding:14px 16px">
                        <div class="loading">Loading…</div>
                    </div>
                </div>
                <div class="card" id="dir-card">
                    <div class="card-head">
                        <span class="card-title">Data Directory</span>
                        <code class="tag-mono" id="dir-path"></code>
                    </div>
                    <div id="dir-body" style="padding:0">
                        <div class="loading" style="padding:14px 16px">Loading…</div>
                    </div>
                </div>
                <div class="card" id="storage-card" style="display:none">
                    <div class="card-head">
                        <span class="card-title">Storage Tree</span>
                        <code class="tag-mono" id="storage-path"></code>
                    </div>
                    <div id="storage-body" style="padding:0"></div>
                </div>
            </div>

            <!-- Right: Config editor -->
            <div class="admin-col">
                <div class="card" id="config-card">
                    <div class="card-head" style="justify-content:space-between">
                        <div>
                            <span class="card-title">config.yaml</span>
                            <code class="tag-mono" id="config-path" style="margin-left:8px"></code>
                        </div>
                        <button class="btn btn-primary btn-sm" id="config-save" style="display:none">${icons.file} Save</button>
                    </div>
                    <div style="padding:0;position:relative">
                        <textarea class="config-editor" id="config-editor" spellcheck="false" placeholder="Loading…"></textarea>
                    </div>
                    <div id="config-status" style="padding:8px 16px;display:none"></div>
                </div>

                <div class="card" id="backup-card">
                    <div class="card-head" style="justify-content:space-between">
                        <span class="card-title">Backup &amp; Restore</span>
                        <code class="tag-mono" id="backup-dir"></code>
                    </div>
                    <div style="padding:12px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--border-light)">
                        <select class="select" id="bk-scope" style="padding:4px 8px;font-size:12px"
                                title="What to include besides config, definitions and databases">
                            <option value="none">State only (config, definitions, DBs)</option>
                            <option value="current">+ current snapshots</option>
                            <option value="all">+ all snapshots incl. history</option>
                        </select>
                        <input class="input" id="bk-label" placeholder="label (optional)" maxlength="40"
                               style="padding:4px 8px;font-size:12px;width:150px">
                        <button class="btn btn-primary btn-sm" id="bk-create">${icons.download} Back up now</button>
                        <label class="btn btn-sm" style="cursor:pointer;margin-left:auto" title="Upload a .tar.gz backup from another install">
                            ${icons.plus} Upload
                            <input type="file" id="bk-upload" accept=".gz,.tar.gz,application/gzip" style="display:none">
                        </label>
                    </div>
                    <div id="bk-banner"></div>
                    <div id="bk-list" style="padding:0"><div class="loading" style="padding:14px 16px">Loading…</div></div>
                    <div class="text-faint fs-11" style="padding:8px 16px 12px">
                        Archives contain the encrypted vault and user password hashes. A restore replaces
                        config, definitions, users, credentials and templates (and snapshots, if the archive
                        has them), takes a pre-restore backup first, and is applied on the next restart.
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    // Load system info
    try {
        _system = await api.admin.system();
    } catch (e) {
        document.getElementById('comp-body').innerHTML =
            `<div class="form-error">Failed to load: ${escapeHtml(e.message)}</div>`;
        return;
    }

    renderComponents();
    renderDataDir();
    renderStorageTree();
    await loadConfig();

    // Config editor events
    const editor = document.getElementById('config-editor');
    const saveBtn = document.getElementById('config-save');

    editor.addEventListener('input', () => {
        saveBtn.style.display = '';
    });

    saveBtn.addEventListener('click', saveConfig);

    // Backup & restore
    document.getElementById('bk-create').addEventListener('click', startBackup);
    document.getElementById('bk-upload').addEventListener('change', uploadBackup);
    await loadBackups();
}


// ══════════════════════════════════════════════════════════════
// Backup & Restore
// ══════════════════════════════════════════════════════════════

function _bkErr(e) {
    const msg = String(e && e.message || e);
    const i = msg.indexOf(': ');
    if (i >= 0) {
        try { const b = JSON.parse(msg.slice(i + 2)); if (b.detail || b.error) return b.detail || b.error; } catch { /* plain */ }
    }
    return msg;
}

async function loadBackups() {
    let st;
    try {
        st = await api.admin.backup.status();
    } catch (e) {
        document.getElementById('bk-list').innerHTML =
            `<div class="form-error" style="margin:12px 16px">Failed to load backups: ${escapeHtml(_bkErr(e))}</div>`;
        return;
    }
    document.getElementById('backup-dir').textContent = st.backups_dir;
    renderBackupBanner(st);
    renderBackupList(st);
    const running = st.job && st.job.status === 'running';
    setBackupBusy(running);
    if (running) pollBackupJob();
}

function setBackupBusy(busy) {
    for (const id of ['bk-create', 'bk-scope', 'bk-label', 'bk-upload']) {
        const el = document.getElementById(id);
        if (el) el.disabled = busy;
    }
    document.querySelectorAll('#bk-list [data-act]').forEach(b => { b.disabled = busy; });
}

function renderBackupBanner(st) {
    const el = document.getElementById('bk-banner');
    if (!el) return;
    const parts = [];
    const job = st.job;
    if (job && job.status === 'running') {
        parts.push(`<div style="padding:10px 16px;display:flex;gap:10px;align-items:center">
            ${badge('running')}<span class="fs-12">${escapeHtml(job.kind)}: ${escapeHtml(job.phase || '')}</span>
            <span class="mono text-faint fs-11" style="margin-left:auto">${job.elapsed != null ? job.elapsed + 's' : ''}</span></div>`);
    } else if (job && job.status === 'failed') {
        parts.push(`<div style="padding:10px 16px;display:flex;gap:10px;align-items:center">
            ${badge('failed')}<span class="fs-12" style="color:var(--red)">${escapeHtml(job.kind)} failed: ${escapeHtml(job.error || '')}</span></div>`);
    }
    const p = st.pending;
    if (p && p.archive_name) {
        const vault = p.vault_password === 'mismatch'
            ? `<div class="fs-12" style="color:var(--red);margin-top:4px">NETLAPSE_VAULT_PASSWORD does not unlock this archive's vault — collection will stay stopped after restart until it is set to that vault's password.</div>`
            : '';
        parts.push(`<div style="padding:10px 16px;background:var(--orange-bg);border-top:1px solid var(--orange-border);border-bottom:1px solid var(--orange-border)">
            <div style="display:flex;gap:10px;align-items:center">
                <span class="badge badge-warning"><span class="dot dot-warning"></span>staged</span>
                <span class="fs-12">Restore of <code class="code">${escapeHtml(p.archive_name)}</code> is staged —
                    <strong>restart netlapse to apply it</strong> (e.g. <code class="code">docker restart &lt;container&gt;</code>).</span>
                <button class="btn btn-sm" id="bk-cancel" style="margin-left:auto">Cancel restore</button>
            </div>${vault}</div>`);
    }
    const last = st.last_restore;
    if (last && !(p && p.archive_name)) {
        if (last.failed) {
            parts.push(`<div style="padding:10px 16px" class="fs-12"><span style="color:var(--red)">Last restore failed</span>
                (${escapeHtml(last.archive || '')}): ${escapeHtml(last.error || '')}</div>`);
        } else {
            parts.push(`<div style="padding:10px 16px" class="text-muted fs-12">Last restore: <code class="code">${escapeHtml(last.archive || '')}</code>
                applied ${escapeHtml(_shortDate(last.applied_at))}${last.pre_restore_backup ? ` · undo with <code class="code">${escapeHtml(last.pre_restore_backup)}</code>` : ''}
                ${last.previous_data_dir ? `<div>Previous data kept at <code class="code">${escapeHtml(last.previous_data_dir)}</code> — delete it when satisfied.</div>` : ''}</div>`);
        }
    }
    el.innerHTML = parts.join('');
    const cancel = document.getElementById('bk-cancel');
    if (cancel) cancel.addEventListener('click', async () => {
        try { await api.admin.backup.cancel(); } catch (e) { alert(_bkErr(e)); }
        loadBackups();
    });
}

function renderBackupList(st) {
    const el = document.getElementById('bk-list');
    if (!el) return;
    const rows = st.backups || [];
    if (!rows.length) {
        el.innerHTML = '<div class="text-muted fs-12" style="padding:14px 16px">No backups yet.</div>';
        return;
    }
    const scopeLabel = { none: 'state', current: 'state + current', all: 'state + history' };
    el.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>Backup</th><th>Contents</th><th>Size</th><th></th></tr></thead>
        <tbody>${rows.map(r => `
            <tr>
                <td><code class="mono fs-11">${escapeHtml(r.name)}</code>
                    <div class="text-faint fs-11">${escapeHtml(_shortDate(r.created_at || r.modified))}${r.host ? ' · ' + escapeHtml(r.host) : ''}${r.netlapse_version ? ' · v' + escapeHtml(r.netlapse_version) : ''}</div></td>
                <td class="fs-12">${r.error ? `<span style="color:var(--red)">${escapeHtml(r.error)}</span>`
                    : escapeHtml(scopeLabel[r.data_scope] || r.data_scope || '—') + (r.data_files ? ` <span class="text-faint">(${r.data_files} files)</span>` : '')}</td>
                <td class="mono fs-12" style="white-space:nowrap">${formatBytes(r.size)}</td>
                <td style="white-space:nowrap;text-align:right">
                    <a class="btn btn-sm" href="${api.admin.backup.downloadUrl(r.name)}" download>${icons.download}</a>
                    ${r.error ? '' : `<button class="btn btn-sm" data-act="restore" data-name="${escapeHtml(r.name)}">Restore…</button>`}
                    <button class="btn btn-sm" data-act="delete" data-name="${escapeHtml(r.name)}" title="Delete" style="color:var(--red)">${icons.trash}</button>
                </td>
            </tr>`).join('')}</tbody></table></div>`;

    el.querySelectorAll('[data-act="restore"]').forEach(b => b.addEventListener('click', () => restoreBackup(b.dataset.name, rows)));
    el.querySelectorAll('[data-act="delete"]').forEach(b => b.addEventListener('click', async () => {
        if (!confirm(`Delete ${b.dataset.name}? This cannot be undone.`)) return;
        try { await api.admin.backup.remove(b.dataset.name); } catch (e) { alert(_bkErr(e)); }
        loadBackups();
    }));
}

async function startBackup() {
    const scope = document.getElementById('bk-scope').value;
    const label = document.getElementById('bk-label').value.trim();
    if (scope === 'all' && !confirm('Including full history can produce a very large archive and take several minutes. Continue?')) return;
    try {
        await api.admin.backup.create(scope, label);
    } catch (e) {
        alert(_bkErr(e));
        return;
    }
    document.getElementById('bk-label').value = '';
    loadBackups();
}

async function restoreBackup(name, rows) {
    const r = rows.find(x => x.name === name) || {};
    const dataNote = r.data_scope && r.data_scope !== 'none'
        ? 'The current data directory will be REPLACED with the archived snapshots (the old one is kept beside it).'
        : 'Collected snapshots are not in this archive and will be left as they are.';
    const typed = prompt(
        `Restore ${name}?\n\n` +
        'This replaces config.yaml, definitions, devices/jobs/history, users, vault credentials and templates ' +
        'with the archived versions. A pre-restore backup is taken first.\n' + dataNote + '\n\n' +
        'It is staged now and applied on the next restart.\n\nType RESTORE to continue.');
    if (typed !== 'RESTORE') return;
    try {
        await api.admin.backup.restore(name);
    } catch (e) {
        alert(_bkErr(e));
        return;
    }
    loadBackups();
}

async function uploadBackup(ev) {
    const file = ev.target.files && ev.target.files[0];
    ev.target.value = '';
    if (!file) return;
    setBackupBusy(true);
    const banner = document.getElementById('bk-banner');
    banner.innerHTML = `<div style="padding:10px 16px" class="fs-12">${badge('running')} Uploading ${escapeHtml(file.name)} (${formatBytes(file.size)})…</div>`;
    try {
        await api.admin.backup.upload(file);
    } catch (e) {
        alert(`Upload failed: ${_bkErr(e)}`);
    }
    loadBackups();
}

function pollBackupJob() {
    const gen = _bkGen;
    clearTimeout(_bkTimer);
    _bkTimer = setTimeout(async () => {
        if (gen !== _bkGen) return;
        try {
            const { job } = await api.admin.backup.job();
            if (gen !== _bkGen) return;
            if (job && job.status === 'running') {
                renderBackupBanner({ job, pending: null, last_restore: null });
                pollBackupJob();
                return;
            }
        } catch { /* transient — reload below */ }
        if (gen === _bkGen) loadBackups();
    }, 1000);
}

function renderComponents() {
    const c = _system.components;
    const body = document.getElementById('comp-body');

    const row = (label, status, detail) => `
        <div class="comp-row">
            <span class="dot dot-${status ? 'success' : 'error'}"></span>
            <span class="comp-label">${label}</span>
            <span class="comp-detail">${detail}</span>
        </div>
    `;

    body.innerHTML = `
        <div class="comp-section">
            <div class="comp-section-title">Version</div>
            ${row('Netlapse', true, `v${escapeHtml(_system.version)}`)}
        </div>
        <div class="comp-section">
            <div class="comp-section-title">Vault</div>
            ${row('Initialized', c.vault.available, c.vault.available ? 'Yes' : 'No')}
            ${row('Unlocked', c.vault.unlocked, c.vault.unlocked ? 'Yes' : 'Locked')}
            ${c.vault.path ? row('Path', true, `<code class="code">${escapeHtml(c.vault.path)}</code>`) : ''}
        </div>
        <div class="comp-section">
            <div class="comp-section-title">Parse Engine</div>
            ${row('Available', c.parser.available, c.parser.available ? `${c.parser.template_count} templates` : 'Not found')}
            ${c.parser.db_path ? row('DB', true, `<code class="code">${escapeHtml(c.parser.db_path)}</code>`) : ''}
            ${c.parser.min_score != null ? row('Min Score', true, c.parser.min_score) : ''}
        </div>
        <div class="comp-section">
            <div class="comp-section-title">Scheduler</div>
            ${row('Running', c.scheduler.running, c.scheduler.running ? 'Active' : 'Stopped')}
            ${c.scheduler.poll_interval != null ? row('Poll', true, `${c.scheduler.poll_interval}s`) : ''}
            ${c.scheduler.worker_count != null ? row('Workers', true, c.scheduler.worker_count) : ''}
        </div>
        <div class="comp-section">
            <div class="comp-section-title">Storage</div>
            ${row('Backend', true, escapeHtml(c.storage.backend || '—'))}
            ${c.storage.path ? row('Path', true, `<code class="code">${escapeHtml(c.storage.path)}</code>`) : ''}
        </div>
    `;
}

function renderDataDir() {
    document.getElementById('dir-path').textContent = _system.data_dir;
    const body = document.getElementById('dir-body');
    const listing = _system.data_dir_listing;

    if (!listing.length) {
        body.innerHTML = '<div class="text-muted" style="padding:14px 16px">Empty</div>';
        return;
    }

    body.innerHTML = `<table class="dir-table">
        ${listing.map(f => `
            <tr>
                <td class="dir-icon">${f.is_dir ? '📁' : '📄'}</td>
                <td class="mono fw-500">${escapeHtml(f.name)}</td>
                <td class="mono text-faint text-right">${f.size != null ? formatBytes(f.size) : '—'}</td>
                <td class="text-faint">${_shortDate(f.modified)}</td>
            </tr>
        `).join('')}
    </table>`;
}

function renderStorageTree() {
    const c = _system.components.storage;
    if (!c.sites || !c.sites.length) return;

    const card = document.getElementById('storage-card');
    card.style.display = '';
    document.getElementById('storage-path').textContent = c.path;

    document.getElementById('storage-body').innerHTML = `<table class="dir-table">
        ${c.sites.map(s => `
            <tr>
                <td class="dir-icon">📁</td>
                <td class="mono fw-500">${escapeHtml(s.name)}</td>
                <td class="text-muted">${s.device_count} device${s.device_count !== 1 ? 's' : ''}</td>
            </tr>
        `).join('')}
    </table>`;
}

async function loadConfig() {
    const editor = document.getElementById('config-editor');
    const pathEl = document.getElementById('config-path');

    try {
        const cfg = await api.admin.config();
        pathEl.textContent = cfg.path;

        if (cfg.exists) {
            editor.value = cfg.content;
        } else {
            // No config yet: the server returns a fully-documented starter
            // template. Save writes it verbatim. Fall back to a minimal stub
            // only if the server sent nothing.
            editor.value = cfg.content && cfg.content.trim()
                ? cfg.content
                : '# Config file not found — saving will create it\n# Restart required after changes\n\nlisten:\n  host: 0.0.0.0\n  port: 8888\n\nstorage:\n  backend: file\n  path: ~/.netlapse/data\n\ndcim_db: ~/.netlapse/netlapse.db\n';
            document.getElementById('config-save').style.display = '';
        }
    } catch (e) {
        editor.value = `# Failed to load: ${e.message}`;
    }
}

async function saveConfig() {
    const editor = document.getElementById('config-editor');
    const status = document.getElementById('config-status');
    const btn = document.getElementById('config-save');

    btn.disabled = true;
    btn.textContent = 'Saving…';

    try {
        const result = await api.admin.saveConfig(editor.value);
        status.style.display = '';
        status.className = 'badge badge-success';
        status.style.padding = '8px 16px';
        status.textContent = `Saved — ${result.note}`;
        btn.style.display = 'none';

        setTimeout(() => { status.style.display = 'none'; }, 5000);
    } catch (e) {
        status.style.display = '';
        status.className = 'form-error';
        status.textContent = `Save failed: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = `${icons.file} Save`;
    }
}

function _shortDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleDateString('en-US', {
            month: 'short', day: 'numeric', hour: '2-digit',
            minute: '2-digit', hour12: false,
        });
    } catch { return iso; }
}

export function destroy() {
    _bkGen++;
    clearTimeout(_bkTimer);
    _bkTimer = null;
    _system = null;
}