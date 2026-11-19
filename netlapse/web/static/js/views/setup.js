/**
 * Setup Wizard (Admin)
 *
 * First-run walkthrough after the admin account exists (that step lives on
 * the standalone /setup page). Every step reads live state from
 * /api/v1/setup/status, so the wizard can be reopened at any time and shows
 * what is already done.
 *
 *   vault       initialize + unlock for this run (scheduler starts in place);
 *               restarts need NETLAPSE_VAULT_PASSWORD
 *   credential  default SSH credential (POST /admin/vault/credentials)
 *   devices     SC2 map.json → preview → import (or skip)
 *   templates   tfsm_templates.db status + manual instructions
 *   finish      summary; marks setup complete
 */

import { api } from '../api.js';
import { escapeHtml, icons } from '../components.js';

const STEPS = [
    { key: 'vault',      label: 'Credential vault' },
    { key: 'credential', label: 'SSH credential' },
    { key: 'devices',    label: 'Devices' },
    { key: 'templates',  label: 'Template database' },
    { key: 'finish',     label: 'Finish' },
];

let _st = null;          // last /setup/status
let _step = null;        // current step key
let _mapFile = null;     // { name, text } of the chosen map.json
let _preview = null;     // last preview response

export function render() {
    return `
        <div style="max-width:980px">
            <div class="card" style="margin-bottom:16px">
                <div style="padding:14px 18px;display:flex;gap:6px;flex-wrap:wrap" id="wz-steps"></div>
            </div>
            <div class="card">
                <div id="wz-body" style="padding:20px 22px"><div class="loading">Loading…</div></div>
            </div>
        </div>`;
}

export async function init() {
    await refresh(true);
    // #/setup?step=devices (from Devices → Import map) opens that step
    // directly, even when setup is long complete.
    const q = new URLSearchParams((location.hash.split('?')[1]) || '');
    const want = q.get('step');
    if (want && _st && STEPS.some(s => s.key === want)) go(want);
}

export function destroy() {
    _st = null; _step = null; _mapFile = null; _preview = null;
}

// ══════════════════════════════════════════════════════════════
// State + navigation
// ══════════════════════════════════════════════════════════════

function errText(e) {
    const msg = String(e && e.message || e);
    const i = msg.indexOf(': ');
    if (i >= 0) {
        try { const b = JSON.parse(msg.slice(i + 2)); if (b.error || b.detail) return b.error || b.detail; } catch { /* plain */ }
    }
    return msg;
}

function done(key) {
    if (!_st || !_st.steps) return false;
    if (key === 'finish') return !!_st.complete;
    return !!_st.steps[key];
}

function firstOpenStep() {
    for (const s of STEPS) {
        if (s.key === 'finish') return 'finish';
        // devices and templates can be skipped; vault + credential cannot
        if (!done(s.key)) return s.key;
    }
    return 'finish';
}

async function refresh(pickStep = false) {
    try {
        _st = await api.setup.status();
    } catch (e) {
        document.getElementById('wz-body').innerHTML =
            `<div class="form-error">Could not load setup status: ${escapeHtml(errText(e))}</div>`;
        return;
    }
    if (pickStep || !_step) _step = firstOpenStep();
    renderSteps();
    renderStep();
}

function go(key) {
    _step = key;
    renderSteps();
    renderStep();
}

function locked(key) {
    // Credential needs an unlocked vault; everything after needs a credential
    // to be useful, but only the vault is a hard prerequisite.
    if (key === 'credential') return !done('vault');
    return false;
}

function renderSteps() {
    const el = document.getElementById('wz-steps');
    if (!el) return;
    const pill = (n, s) => {
        const ok = done(s.key);
        const cur = s.key === _step;
        const dis = locked(s.key);
        const bg = cur ? 'var(--blue)' : 'transparent';
        const fg = cur ? '#fff' : (dis ? 'var(--text-ghost)' : 'var(--text)');
        const mark = ok ? '✓' : String(n);
        return `<button class="btn btn-sm" data-step="${s.key}" ${dis ? 'disabled' : ''}
            style="background:${bg};color:${fg};border-color:${cur ? 'var(--blue)' : 'var(--border)'}">
            <span style="display:inline-flex;width:18px;height:18px;border-radius:50%;align-items:center;justify-content:center;
                font-size:10.5px;font-weight:700;background:${ok ? 'var(--green)' : (cur ? 'rgba(255,255,255,.25)' : 'var(--surface-alt)')};
                color:${ok || cur ? '#fff' : 'var(--text-muted)'}">${mark}</span>${escapeHtml(s.label)}</button>`;
    };
    el.innerHTML =
        `<span class="btn btn-sm" style="pointer-events:none;border-color:var(--border)">
            <span style="display:inline-flex;width:18px;height:18px;border-radius:50%;align-items:center;justify-content:center;font-size:10.5px;font-weight:700;background:var(--green);color:#fff">✓</span>Admin account</span>` +
        STEPS.map((s, i) => pill(i + 2, s)).join('');
    el.querySelectorAll('[data-step]').forEach(b => b.addEventListener('click', () => go(b.dataset.step)));
}

function renderStep() {
    const body = document.getElementById('wz-body');
    if (!body || !_st) return;
    const fn = { vault: stepVault, credential: stepCredential, devices: stepDevices,
                 templates: stepTemplates, finish: stepFinish }[_step];
    body.innerHTML = fn();
    wire[_step] && wire[_step]();
}

const h2 = (t, sub) => `
    <div style="margin-bottom:16px">
        <div class="fw-600" style="font-size:16px">${t}</div>
        ${sub ? `<div class="text-muted fs-12" style="margin-top:4px;line-height:1.5">${sub}</div>` : ''}
    </div>`;

const okLine = (t) => `<div style="display:flex;gap:8px;align-items:center;margin:6px 0">
    <span class="dot dot-success"></span><span class="fs-12">${t}</span></div>`;
const warnLine = (t) => `<div style="display:flex;gap:8px;align-items:flex-start;margin:6px 0">
    <span class="dot dot-warning" style="margin-top:5px"></span><span class="fs-12" style="line-height:1.5">${t}</span></div>`;

const nextBtn = (to, label = 'Continue') =>
    `<button class="btn btn-primary btn-sm" data-go="${to}">${label} →</button>`;

function wireGo() {
    document.querySelectorAll('#wz-body [data-go]').forEach(b =>
        b.addEventListener('click', () => go(b.dataset.go)));
}

function showError(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? '' : 'none';
}

// ══════════════════════════════════════════════════════════════
// Step: vault
// ══════════════════════════════════════════════════════════════

function stepVault() {
    const v = _st.vault;
    const envNote = v.env_password_set
        ? okLine('<code class="code">NETLAPSE_VAULT_PASSWORD</code> is set — the vault will unlock on restart.')
        : warnLine(`<strong>Before the next restart</strong>, set <code class="code">NETLAPSE_VAULT_PASSWORD</code> to the master
            password in the daemon's environment (shell profile, systemd unit, or your container's env).
            Without it the vault stays locked after a restart and <strong>scheduled collection stops</strong>.`);

    if (v.initialized && v.unlocked) {
        return h2('Credential vault', 'Device credentials are encrypted at rest under a master password.') +
            okLine('Vault initialized and unlocked') +
            okLine(_st.scheduler_running ? 'Scheduler running' : 'Scheduler not running') +
            envNote +
            `<div class="form-actions">${nextBtn('credential')}</div>`;
    }
    if (v.initialized && !v.unlocked) {
        return h2('Credential vault') +
            warnLine(`The vault already exists but is <strong>locked</strong>. Set
                <code class="code">NETLAPSE_VAULT_PASSWORD</code> to its master password and restart netlapse.
                (Vault unlock is environment-only by design.)`) +
            `<div class="form-actions"><button class="btn btn-sm" id="wz-recheck">${icons.refresh} Re-check</button></div>`;
    }
    return h2('Create the credential vault',
            'Device credentials are encrypted at rest under this master password. It is not stored anywhere — ' +
            'keep it in your password manager. There is no recovery if it is lost.') +
        `<div class="form-grid" style="max-width:520px">
            <div class="form-group"><label class="form-label">Master password (min 8)</label>
                <input class="input" type="password" id="wz-vpw" autocomplete="new-password"></div>
            <div class="form-group"><label class="form-label">Confirm</label>
                <input class="input" type="password" id="wz-vpw2" autocomplete="new-password"></div>
        </div>
        <div class="form-error" id="wz-verr" style="display:none"></div>
        <div class="form-actions"><button class="btn btn-primary btn-sm" id="wz-vinit">Create vault</button></div>`;
}

const wire = {};

wire.vault = () => {
    wireGo();
    const re = document.getElementById('wz-recheck');
    if (re) re.addEventListener('click', () => refresh());
    const btn = document.getElementById('wz-vinit');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        const pw = document.getElementById('wz-vpw').value;
        const pw2 = document.getElementById('wz-vpw2').value;
        if (pw.length < 8) return showError('wz-verr', 'Master password must be at least 8 characters.');
        if (pw !== pw2) return showError('wz-verr', 'Passwords do not match.');
        btn.disabled = true; btn.textContent = 'Creating…';
        try {
            const st = await api.setup.vault(pw, pw2);
            if (st.scheduler_error) alert(`Vault created, but the scheduler did not start: ${st.scheduler_error}`);
            await refresh();
        } catch (e) {
            btn.disabled = false; btn.textContent = 'Create vault';
            showError('wz-verr', errText(e));
        }
    });
};

// ══════════════════════════════════════════════════════════════
// Step: SSH credential
// ══════════════════════════════════════════════════════════════

function stepCredential() {
    const creds = _st.credentials || [];
    const list = creds.length
        ? creds.map(c => okLine(`<code class="code">${escapeHtml(c.name)}</code> — ${escapeHtml(c.username || '')}
              ${c.is_default ? '<span class="badge badge-info" style="margin-left:6px">default</span>' : ''}`)).join('')
        : '';
    const haveDefault = !!_st.default_credential;
    return h2('SSH credential',
            'The default credential is tried first on every device. Add more (and a fallback order in config.yaml) on the Credentials page later.') +
        list +
        (haveDefault ? '' : `
        <div class="form-grid" style="max-width:620px;margin-top:8px">
            <div class="form-group"><label class="form-label">Name</label>
                <input class="input" id="wz-cname" value="${creds.length ? '' : 'default'}"></div>
            <div class="form-group"><label class="form-label">Username</label>
                <input class="input" id="wz-cuser" autocomplete="off"></div>
            <div class="form-group"><label class="form-label">Password</label>
                <input class="input" type="password" id="wz-cpw" autocomplete="new-password"></div>
            <div class="form-group"><label class="form-label">SSH port</label>
                <input class="input" id="wz-cport" value="22"></div>
        </div>
        <div class="form-group" style="max-width:620px">
            <label class="form-label">Private key (optional — PEM/OpenSSH, used instead of or with the password)</label>
            <textarea class="input" id="wz-ckey" rows="3" spellcheck="false" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>
        </div>
        <div class="form-group" style="max-width:620px">
            <label class="form-label">Key passphrase (if the key is encrypted)</label>
            <input class="input" type="password" id="wz-ckp" autocomplete="off">
        </div>
        <div class="form-error" id="wz-cerr" style="display:none"></div>`) +
        `<div class="form-actions">
            ${haveDefault ? nextBtn('devices') : `<button class="btn btn-primary btn-sm" id="wz-cadd">Save credential</button>`}
        </div>`;
}

wire.credential = () => {
    wireGo();
    const btn = document.getElementById('wz-cadd');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        const v = id => document.getElementById(id).value;
        const data = {
            name: v('wz-cname').trim(),
            username: v('wz-cuser').trim(),
            password: v('wz-cpw') || null,
            key_content: v('wz-ckey').trim() || null,
            key_passphrase: v('wz-ckp') || null,
            port: parseInt(v('wz-cport'), 10) || 22,
            is_default: true,
        };
        if (!data.name || !data.username) return showError('wz-cerr', 'Name and username are required.');
        if (!data.password && !data.key_content) return showError('wz-cerr', 'Provide a password or a private key.');
        btn.disabled = true; btn.textContent = 'Saving…';
        try {
            await api.admin.addCredential(data);
            await refresh();
            if (done('credential')) go('devices');
        } catch (e) {
            btn.disabled = false; btn.textContent = 'Save credential';
            showError('wz-cerr', errText(e));
        }
    });
};

// ══════════════════════════════════════════════════════════════
// Step: devices (SC2 map import)
// ══════════════════════════════════════════════════════════════

function stepDevices() {
    const have = _st.devices > 0;
    return h2('Devices',
            'Import a Secure Cartography <code class="code">map.json</code>. Sites and roles are created as needed; ' +
            'roles come from roles.yaml. Re-importing is safe — existing devices are matched by name and site, never duplicated; ' +
            'the preview lists every field an import would change.') +
        (have ? okLine(`${_st.devices} device(s) across ${_st.sites} site(s) in inventory`) : '') +
        `<div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-top:10px">
            <div class="form-group" style="margin:0">
                <label class="form-label">map.json</label>
                <label class="btn btn-sm" style="cursor:pointer">${icons.file}
                    <span id="wz-mname">${_mapFile ? escapeHtml(_mapFile.name) : 'Choose file…'}</span>
                    <input type="file" id="wz-mfile" accept=".json,application/json" style="display:none"></label>
            </div>
            <div class="form-group" style="margin:0">
                <label class="form-label">Force site (optional)</label>
                <input class="input" id="wz-msite" placeholder="from hostname" style="width:140px;padding:5px 8px">
            </div>
            <div class="form-group" style="margin:0">
                <label class="form-label">Exclude prefixes (comma-separated)</label>
                <input class="input" id="wz-mexcl" placeholder="e.g. oob,lab" style="width:180px;padding:5px 8px">
            </div>
            <label class="form-label-inline" style="margin-bottom:6px">
                <input type="checkbox" id="wz-mcoll" checked> Enable collection</label>
            <label class="form-label-inline" style="margin-bottom:6px" title="Skip nodes whose platform isn't in Reference Data → Platforms (e.g. discovered by IP with no platform). They would import but never collect.">
                <input type="checkbox" id="wz-mskip"> Skip unrecognized platforms</label>
            <label class="form-label-inline" style="margin-bottom:6px" title="Off: only devices not already in inventory are created; existing devices keep their IP, platform and role (useful when you've edited them by hand).">
                <input type="checkbox" id="wz-mupd" checked> Update existing devices</label>
            <button class="btn btn-sm" id="wz-mprev" ${_mapFile ? '' : 'disabled'}>Preview</button>
        </div>
        <div class="form-error" id="wz-merr" style="display:none"></div>
        <div id="wz-mout" style="margin-top:14px"></div>
        <div class="form-actions">
            <span class="text-faint fs-12" style="margin-right:auto">You can also add devices one at a time on the Devices page.</span>
            ${nextBtn('templates', have ? 'Continue' : 'Skip for now')}
        </div>`;
}

function mapOpts() {
    return {
        site: document.getElementById('wz-msite').value.trim(),
        exclude: document.getElementById('wz-mexcl').value.trim(),
        collection: document.getElementById('wz-mcoll').checked,
        skip_unknown: document.getElementById('wz-mskip').checked,
        update_existing: document.getElementById('wz-mupd').checked,
    };
}

function renderPreview(p) {
    const out = document.getElementById('wz-mout');
    if (!out) return;
    const chips = (obj) => Object.entries(obj || {}).map(([k, n]) =>
        `<span class="badge badge-neutral" style="margin:0 4px 4px 0">${escapeHtml(k)} · ${n}</span>`).join('');
    const unknown = (p.unknown_platforms || []).length
        ? warnLine(`Platforms with no match in Reference Data → Platforms: ${p.unknown_platforms.map(x => `<code class="code">${escapeHtml(x)}</code>`).join(' ')}.
            Those devices import without a platform and won't collect until one is set.`)
        : '';
    // What happens to each device, most consequential first; unchanged
    // devices sink to the bottom.
    const ORDER = { update: 0, create: 1, kept: 2, unchanged: 3 };
    const ACTION = {
        create: '<span class="text-success">new</span>',
        update: '<span style="color:var(--orange)">change</span>',
        kept: '<span class="text-muted" title="Differs from the map, left as-is (Update existing devices is off)">kept</span>',
        unchanged: '<span class="text-faint">unchanged</span>',
    };
    const changeText = (c) => Object.entries(c || {}).map(([k, v]) =>
        `${escapeHtml(k)} <span class="text-faint">${escapeHtml(v[0] || '—')}</span> → <strong>${escapeHtml(v[1])}</strong>`).join('<br>');
    const sorted = [...(p.devices || [])].sort((a, b) =>
        (ORDER[a.action] ?? 9) - (ORDER[b.action] ?? 9) || a.name.localeCompare(b.name, undefined, { numeric: true }));
    const rows = sorted.slice(0, 300).map(d => `
        <tr>
            <td class="mono fs-12">${escapeHtml(d.name)}</td>
            <td class="fs-12">${escapeHtml(d.site)}</td>
            <td class="mono fs-12">${escapeHtml(d.ip || '—')}</td>
            <td class="fs-12" ${d.platform_known ? '' : 'style="color:var(--orange)"'}>${escapeHtml(d.platform || '—')}</td>
            <td class="fs-12">${escapeHtml(d.role)}</td>
            <td class="fs-12">${ACTION[d.action] || escapeHtml(d.action)}${d.legacy_ssh_hint ? ' <span class="text-faint" title="old firmware — legacy SSH algorithms likely">legacy ssh</span>' : ''}</td>
            <td class="fs-12 mono">${changeText(d.changes)}</td>
        </tr>`).join('');
    const nChange = p.would_update || 0, nKeep = p.would_keep || 0;
    const writes = (p.would_create || 0) + nChange;
    out.innerHTML = `
        <div class="fs-12" style="margin-bottom:8px"><strong>${p.total}</strong> device(s) in the map:
            <span class="text-success">${p.would_create} new</span>,
            <span ${nChange ? 'style="color:var(--orange)"' : ''}>${nChange} will change</span>,
            ${p.unchanged || 0} unchanged${nKeep ? `, ${nKeep} differ but kept as-is` : ''}${
            p.skipped_unknown_platform ? ` · <span class="text-muted">${p.skipped_unknown_platform} skipped (unrecognized platform)</span>` : ''}${
            p.site_override ? ` · site <code class="code">${escapeHtml(p.site_override)}</code>` : ''}</div>
        <div style="margin-bottom:6px"><span class="text-faint fs-11" style="margin-right:6px">Sites</span>${chips(p.by_site)}</div>
        <div style="margin-bottom:6px"><span class="text-faint fs-11" style="margin-right:6px">Platforms</span>${chips(p.by_platform)}</div>
        <div style="margin-bottom:8px"><span class="text-faint fs-11" style="margin-right:6px">Roles</span>${chips(p.by_role)}</div>
        ${unknown}
        ${nChange ? warnLine(`${nChange} existing device(s) will be changed to match the map — check the Changes column. ` +
            'Untick <strong>Update existing devices</strong> to keep IP, platform and role edits you made by hand.') : ''}
        <div class="table-wrap" style="max-height:320px;overflow:auto;margin-top:8px">
            <table><thead><tr><th>Device</th><th>Site</th><th>IP</th><th>Platform</th><th>Role</th><th></th><th>Changes</th></tr></thead>
            <tbody>${rows}</tbody></table></div>
        ${sorted.length > 300 ? `<div class="text-faint fs-11" style="margin-top:4px">Showing 300 of ${sorted.length}.</div>` : ''}
        <div style="margin-top:12px"><button class="btn btn-primary btn-sm" id="wz-mimp" ${writes ? '' : 'disabled'}>${
            writes ? `Import: create ${p.would_create}, change ${nChange}` : 'Nothing to import'}</button></div>`;
    document.getElementById('wz-mimp').addEventListener('click', doImport);
}

async function doImport() {
    const btn = document.getElementById('wz-mimp');
    btn.disabled = true; btn.textContent = 'Importing…';
    try {
        const r = await api.setup.mapImport(_mapFile.text, mapOpts());
        _preview = null;
        await refresh();
        const out = document.getElementById('wz-mout');
        if (out) out.innerHTML = okLine(`Imported: ${r.created} created, ${r.updated} changed, ${r.skipped} left as-is` +
            (r.sites_created.length ? ` · sites created: ${r.sites_created.map(escapeHtml).join(', ')}` : '') +
            (r.roles_created.length ? ` · roles created: ${r.roles_created.map(escapeHtml).join(', ')}` : '')) +
            (r.errors.length ? warnLine(`${r.errors.length} error(s): ${r.errors.slice(0, 5).map(escapeHtml).join('; ')}`) : '');
    } catch (e) {
        btn.disabled = false; btn.textContent = 'Import';
        showError('wz-merr', errText(e));
    }
}

wire.devices = () => {
    wireGo();
    document.getElementById('wz-mfile').addEventListener('change', async (ev) => {
        const f = ev.target.files && ev.target.files[0];
        if (!f) return;
        _mapFile = { name: f.name, text: await f.text() };
        document.getElementById('wz-mname').textContent = f.name;
        document.getElementById('wz-mprev').disabled = false;
        document.getElementById('wz-mout').innerHTML = '';
        showError('wz-merr', '');
    });
    // Options change what the import would do; refresh an open preview.
    ['wz-mupd', 'wz-mskip'].forEach(id => document.getElementById(id).addEventListener('change', () => {
        if (_preview) document.getElementById('wz-mprev').click();
    }));
    document.getElementById('wz-mprev').addEventListener('click', async () => {
        const btn = document.getElementById('wz-mprev');
        btn.disabled = true; btn.textContent = 'Parsing…';
        showError('wz-merr', '');
        try {
            _preview = await api.setup.mapPreview(_mapFile.text, mapOpts());
            renderPreview(_preview);
        } catch (e) {
            showError('wz-merr', errText(e));
        }
        btn.disabled = false; btn.textContent = 'Preview';
    });
};

// ══════════════════════════════════════════════════════════════
// Step: templates
// ══════════════════════════════════════════════════════════════

function stepTemplates() {
    const t = _st.templates;
    if (t.loaded) {
        return h2('Template database', 'TextFSM templates turn raw CLI output into structured records.') +
            okLine(`${t.count} templates loaded from <code class="code">${escapeHtml(t.path)}</code>`) +
            `<div class="form-actions">${nextBtn('finish')}</div>`;
    }
    return h2('Template database',
            'Without it, collection still runs and stores raw output, but nothing is parsed (no Parsed Data, no semantic diff).') +
        warnLine(`Not loaded${t.error ? ` — ${escapeHtml(t.error)}` : ''}.`) +
        `<div class="fs-12" style="line-height:1.7;margin-top:10px">
            Place <code class="code">tfsm_templates.db</code> at <code class="code">${escapeHtml(t.path)}</code>, then
            <strong>restart netlapse</strong> — the parse engine loads at startup. Either copy it from an existing install, or on the host run:
            <pre class="config-pre" style="margin:8px 0;padding:8px 12px">python -m netlapse fetch-templates</pre>
        </div>
        <div class="form-actions">${nextBtn('finish', 'Skip for now')}</div>`;
}

wire.templates = () => wireGo();

// ══════════════════════════════════════════════════════════════
// Step: finish
// ══════════════════════════════════════════════════════════════

function stepFinish() {
    const s = _st.steps;
    const line = (ok, text, fix) => ok ? okLine(text)
        : warnLine(`${text}${fix ? ` — <a href="javascript:void 0" data-go="${fix}">fix</a>` : ''}`);
    const ready = s.vault && s.credential;
    return h2(_st.complete ? 'Setup complete' : 'Review',
            'Everything here can be changed later from Settings, Credentials, Devices and Jobs.') +
        line(true, 'Admin account') +
        line(s.vault, s.vault ? 'Vault initialized and unlocked' : 'Vault not ready', 'vault') +
        line(_st.vault.env_password_set, _st.vault.env_password_set
            ? 'NETLAPSE_VAULT_PASSWORD set for restarts'
            : 'NETLAPSE_VAULT_PASSWORD is not set — collection stops after a restart until it is', null) +
        line(s.credential, s.credential ? `Default credential: ${escapeHtml(_st.default_credential)}` : 'No default SSH credential', 'credential') +
        line(s.devices, s.devices ? `${_st.devices} device(s), ${_st.sites} site(s)` : 'No devices yet', 'devices') +
        line(s.templates, s.templates ? `${_st.templates.count} parse templates` : 'Template database not loaded', 'templates') +
        line(_st.scheduler_running, _st.scheduler_running ? `Scheduler running · ${_st.enabled_jobs} enabled job(s)` : 'Scheduler not running', 'vault') +
        `<div class="form-actions">
            ${s.devices ? `<a class="btn btn-sm" href="#/devices">Open Devices</a>` : ''}
            ${_st.complete
                ? `<a class="btn btn-primary btn-sm" href="#/dashboard">Go to dashboard</a>`
                : `<button class="btn btn-primary btn-sm" id="wz-finish" ${ready ? '' : 'disabled title="Vault and a default credential are required"'}>Finish setup</button>`}
        </div>`;
}

wire.finish = () => {
    wireGo();
    const btn = document.getElementById('wz-finish');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
            await api.setup.complete();
            location.hash = '#/dashboard';
        } catch (e) {
            btn.disabled = false;
            alert(errText(e));
        }
    });
};
