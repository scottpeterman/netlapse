/**
 * Template Lab View (Admin) — author / test / save workbench.
 *
 * Two tabs:
 *   Author & Test    — edit a TextFSM template and run it against raw output
 *                      WITHOUT saving (ad-hoc), or against the whole database.
 *                      Surfaces compile errors and State Errors (with the
 *                      offending rule line + input line) and a live score
 *                      breakdown. Editing and testing share one screen.
 *   Template Browser — list / filter / delete; "Edit" loads into Author.
 *
 * Data sources: /api/v1/admin/templates/*, /api/v1/devices, snapshots.
 */

import { api } from '../api.js';
import { escapeHtml, icons } from '../components.js';

let _platforms = [];
let _templates = [];
let _editingId = null;       // null = new template, else editing existing
let _lastCompileOk = null;   // tracks last ad-hoc test compile state for Save guard

const MAXES = { records: 90, fields: 90, population: 25, consistency: 15 };

export function render() {
    return `
        <div class="tab-row" style="margin-bottom:16px">
            <button class="tab-item active" id="tab-author">Author &amp; Test</button>
            <button class="tab-item" id="tab-browse">Template Browser</button>
        </div>

        <!-- ═══ Author & Test ═══ -->
        <div id="panel-author">
            <div class="lab-meta">
                <input class="input mono" id="lab-name" placeholder="template_name (e.g. juniper_junos_show_bgp_summary)" style="flex:2;min-width:280px">
                <select class="select" id="lab-platform" style="flex:1;min-width:140px">
                    <option value="">platform…</option>
                </select>
                <input class="input" id="lab-command" placeholder="command (optional)" style="flex:1;min-width:140px">
                <label class="lab-check"><input type="checkbox" id="lab-clean" checked> clean preamble</label>
                <button class="btn btn-sm" id="lab-load-snap" title="Load raw output from a device snapshot">${icons.file} From Snapshot</button>
            </div>

            <div class="lab-author">
                <!-- Editor column -->
                <div class="lab-edit-col">
                    <div class="lab-panel">
                        <div class="lab-panel-head"><span class="lab-panel-title">Raw CLI Output</span></div>
                        <textarea class="lab-input" id="lab-raw" placeholder="Paste raw CLI output here…" spellcheck="false" style="min-height:150px"></textarea>
                    </div>
                    <div class="lab-panel">
                        <div class="lab-panel-head">
                            <span class="lab-panel-title">TextFSM Template</span>
                            <span class="fs-11 text-faint" id="lab-edit-state">New template</span>
                        </div>
                        <textarea class="lab-input" id="lab-tpl" placeholder="Value FIELD (\\S+)&#10;&#10;Start&#10;  ^\${FIELD} -> Record" spellcheck="false" style="min-height:320px"></textarea>
                    </div>
                    <div class="lab-actions">
                        <button class="btn btn-primary" id="lab-test">${icons.play} Test Template</button>
                        <button class="btn" id="lab-test-db">${icons.data} Test vs Database</button>
                        <span style="flex:1"></span>
                        <button class="btn" id="lab-save">Save to DB</button>
                        <button class="btn btn-ghost" id="lab-clear">Clear</button>
                    </div>
                </div>

                <!-- Result column -->
                <div class="lab-result-col">
                    <div class="lab-panel">
                        <div class="lab-panel-head">
                            <span class="lab-panel-title">Result</span>
                            <span class="mono fs-11" id="lab-score-pill"></span>
                        </div>
                        <div id="lab-result" class="lab-result-body">
                            <div class="text-muted" style="padding:18px;text-align:center">
                                Paste output, write or load a template, and hit <strong>Test Template</strong>.
                                Nothing is saved until you click Save.
                            </div>
                        </div>
                    </div>
                    <div class="lab-panel">
                        <div class="lab-panel-head">
                            <span class="lab-panel-title">Parsed Data</span>
                            <span class="mono fs-11 text-faint" id="lab-reccount"></span>
                        </div>
                        <div id="lab-parsed" class="lab-parsed-body">
                            <div class="text-muted" style="padding:18px;text-align:center">Parsed records will appear here.</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ═══ Template Browser ═══ -->
        <div id="panel-browse" style="display:none">
            <div class="card">
                <div class="card-head" style="justify-content:space-between">
                    <div style="display:flex;gap:8px;align-items:center">
                        <select class="select" id="tpl-platform-filter" style="width:180px">
                            <option value="">All platforms</option>
                        </select>
                        <input class="input" id="tpl-search" placeholder="Search templates…" style="width:220px">
                    </div>
                    <div style="display:flex;gap:8px;align-items:center">
                        <span class="mono text-faint fs-11" id="tpl-count"></span>
                        <button class="btn btn-primary btn-sm" id="btn-add-tpl">+ New Template</button>
                    </div>
                </div>
                <div class="table-wrap" style="max-height:520px;overflow-y:auto">
                    <table>
                        <thead><tr><th>ID</th><th>Template Name</th><th></th></tr></thead>
                        <tbody id="tpl-body"><tr><td colspan="3" class="loading">Loading…</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- ═══ Snapshot Loader Modal ═══ -->
        <div class="modal-overlay" id="snap-modal" style="display:none">
            <div class="modal-container" style="max-width:520px">
                <div class="modal-header">
                    <div class="modal-title">Load from Snapshot</div>
                    <button class="modal-close" id="snap-modal-close">&times;</button>
                </div>
                <div class="modal-body" style="padding:20px">
                    <div class="form-group">
                        <label class="form-label">Device</label>
                        <select class="select" id="snap-device"><option value="">Select device…</option></select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">Capture Type</label>
                        <input class="input" id="snap-type" value="bgp" placeholder="e.g. config, arp, bgp">
                    </div>
                    <div id="snap-error" class="form-error" style="display:none"></div>
                    <div class="form-actions">
                        <button class="btn" id="snap-cancel">Cancel</button>
                        <button class="btn btn-primary" id="snap-load">Load</button>
                    </div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    try { _platforms = await api.admin.templatePlatforms(); } catch { _platforms = []; }

    const opts = _platforms.map(p =>
        `<option value="${escapeHtml(p.platform)}">${escapeHtml(p.platform)} (${p.count})</option>`
    ).join('');
    document.getElementById('lab-platform').innerHTML = '<option value="">platform…</option>' + opts;
    document.getElementById('tpl-platform-filter').innerHTML = '<option value="">All platforms</option>' + opts;

    document.getElementById('tab-author').addEventListener('click', () => switchTab('author'));
    document.getElementById('tab-browse').addEventListener('click', () => switchTab('browse'));

    document.getElementById('lab-test').addEventListener('click', runAdhocTest);
    document.getElementById('lab-test-db').addEventListener('click', runDbTest);
    document.getElementById('lab-save').addEventListener('click', saveTemplate);
    document.getElementById('lab-clear').addEventListener('click', clearEditor);
    document.getElementById('lab-load-snap').addEventListener('click', openSnapshotModal);
    // Auto-fill the name's platform/command hints aren't bidirectional; keep simple.

    document.getElementById('tpl-platform-filter').addEventListener('change', loadTemplates);
    document.getElementById('tpl-search').addEventListener('input', _debounce(loadTemplates, 900));
    document.getElementById('btn-add-tpl').addEventListener('click', () => { clearEditor(); switchTab('author'); });

    document.getElementById('snap-modal-close').addEventListener('click', () => closeModal('snap-modal'));
    document.getElementById('snap-cancel').addEventListener('click', () => closeModal('snap-modal'));
    document.getElementById('snap-modal').addEventListener('click', (e) => { if (e.target.id === 'snap-modal') closeModal('snap-modal'); });
    document.getElementById('snap-load').addEventListener('click', loadSnapshot);
}


// ══════════════════════════════════════════════════════════════ Tabs
function switchTab(tab) {
    document.getElementById('tab-author').classList.toggle('active', tab === 'author');
    document.getElementById('tab-browse').classList.toggle('active', tab === 'browse');
    document.getElementById('panel-author').style.display = tab === 'author' ? '' : 'none';
    document.getElementById('panel-browse').style.display = tab === 'browse' ? '' : 'none';
    if (tab === 'browse' && !_templates.length) loadTemplates();
}


// ══════════════════════════════════════════════════════════════ Ad-hoc test
async function runAdhocTest() {
    const raw = document.getElementById('lab-raw').value;
    const content = document.getElementById('lab-tpl').value;
    const command = document.getElementById('lab-command').value.trim()
        || document.getElementById('lab-name').value.trim();
    const clean = document.getElementById('lab-clean').checked;

    if (!content.trim()) return showResult('<div class="form-error">Write or load a TextFSM template first.</div>');
    if (!raw.trim()) return showResult('<div class="form-error">Paste raw CLI output to test against.</div>');

    const btn = document.getElementById('lab-test');
    btn.disabled = true; btn.textContent = 'Testing…';
    try {
        const r = await api.admin.testTemplate({ raw_output: raw, textfsm_content: content, command, clean });
        renderAdhocResult(r);
    } catch (e) {
        showResult(`<div class="form-error">${escapeHtml(e.message)}</div>`);
    } finally {
        btn.disabled = false; btn.innerHTML = `${icons.play} Test Template`;
    }
}

function renderAdhocResult(r) {
    _lastCompileOk = r.compiled;
    const pill = document.getElementById('lab-score-pill');

    // Compile error
    if (!r.compiled) {
        pill.textContent = '';
        showResult(`
            <div class="lab-match lab-match-fail">
                <div class="lab-match-label">Compile Error</div>
                <div class="mono fs-11" style="white-space:pre-wrap">${escapeHtml(r.error || 'template did not compile')}</div>
            </div>`);
        clearParsed();
        return;
    }

    // Runtime / State error
    if (!r.success) {
        pill.textContent = 'no match';
        let html = `
            <div class="lab-match lab-match-fail">
                <div class="lab-match-label">${r.error_type === 'runtime' ? 'State Error' : 'Parse Error'}</div>
                <div class="mono fs-11" style="white-space:pre-wrap">${escapeHtml(r.error || '')}</div>`;
        if (r.rule_line || r.input_line) {
            html += `<div class="lab-err-grid">`;
            if (r.rule_line) html += `<div><span class="text-faint">rule line</span> <code class="code">${r.rule_line}</code></div>`;
            if (r.input_line) html += `<div><span class="text-faint">choked on</span> <code class="code">${escapeHtml(r.input_line)}</code></div>`;
            html += `</div>`;
        }
        html += `<div class="lab-match-meta">A strict <code class="code">-&gt; Error</code> rule turns an unexpected line into a hard failure. Loosen the rule or add a skip for this line.</div></div>`;
        showResult(html);
        clearParsed();
        return;
    }

    // Success
    pill.textContent = `score ${r.score}`;
    const b = r.breakdown || {};
    const bars = Object.keys(MAXES).map(k => {
        const val = b[k] || 0, max = MAXES[k];
        const pct = Math.round((val / max) * 100);
        return `
            <div class="lab-bar-row">
                <span class="lab-bar-label">${k}</span>
                <div class="lab-bar"><div class="lab-bar-fill" style="width:${pct}%"></div></div>
                <span class="lab-bar-val mono">${val}<span class="text-faint">/${max}</span></span>
            </div>`;
    }).join('');

    showResult(`
        <div class="lab-match lab-match-success">
            <div class="lab-match-label">Parsed OK${document.getElementById('lab-clean').checked ? ' · preamble cleaned' : ''}</div>
            <div class="lab-match-meta">
                <strong>${r.score}</strong> / 100 ·
                ${r.record_count} record${r.record_count !== 1 ? 's' : ''} ·
                ${r.field_count} field${r.field_count !== 1 ? 's' : ''}
            </div>
        </div>
        <div class="lab-score-bars">${bars}</div>`);
    renderParsedTable(r.records);
}


// ══════════════════════════════════════════════════════════════ DB sweep test
async function runDbTest() {
    const raw = document.getElementById('lab-raw').value;
    const platform = document.getElementById('lab-platform').value;
    const command = document.getElementById('lab-command').value.trim();
    if (!raw.trim()) return showResult('<div class="form-error">Paste raw CLI output first.</div>');
    if (!platform) return showResult('<div class="form-error">Select a platform for the database sweep.</div>');

    const btn = document.getElementById('lab-test-db');
    btn.disabled = true; btn.textContent = 'Sweeping…';
    try {
        const res = await api.admin.testParse({ raw_output: raw, platform, command });
        renderDbResult(res);
    } catch (e) {
        showResult(`<div class="form-error">${escapeHtml(e.message)}</div>`);
    } finally {
        btn.disabled = false; btn.innerHTML = `${icons.data} Test vs Database`;
    }
}

function renderDbResult(res) {
    const p = res.primary, fb = res.fallback, candidates = res.candidates || [];
    const best = p.success ? p : (fb && fb.success ? fb : p);
    const isFallback = !p.success && fb && fb.success;
    const myName = document.getElementById('lab-name').value.trim();
    document.getElementById('lab-score-pill').textContent = best.success ? `db best ${best.score}` : 'no db match';

    let html = '';
    if (best.success) {
        const iWin = myName && best.template === myName;
        html += `
            <div class="lab-match ${isFallback ? 'lab-match-fallback' : 'lab-match-success'}">
                <div class="lab-match-label">Database Sweep — ${isFallback ? 'Fallback' : 'Best'} Match</div>
                <code class="mono fw-500">${escapeHtml(best.template)}</code>
                <div class="lab-match-meta">Score <strong>${best.score}</strong> · ${best.record_count} record${best.record_count !== 1 ? 's' : ''}
                    ${myName ? (iWin ? ' · <span class="text-success">your template wins</span>' : ` · your template (<code class="code">${escapeHtml(myName)}</code>) is not the winner`) : ''}
                </div>
            </div>`;
    } else {
        html += `<div class="lab-match lab-match-fail"><div class="lab-match-label">No Database Match</div>
            ${p.template ? `<div class="text-muted">Best attempt: <code class="code">${escapeHtml(p.template)}</code> (${p.score})</div>` : ''}</div>`;
    }
    if (candidates.length) {
        html += `<details class="form-details" style="margin-top:10px"><summary class="form-summary">${candidates.length} candidate${candidates.length !== 1 ? 's' : ''} in scope</summary>
            <div class="lab-candidates">${candidates.map(c => `
                <div class="lab-candidate ${c.cli_command === best.template ? 'lab-candidate-winner' : ''}">
                    <code class="mono fs-11">${escapeHtml(c.cli_command)}</code>
                    ${c.cli_command === best.template ? '<span class="badge badge-success" style="font-size:10px">winner</span>' : ''}
                </div>`).join('')}</div></details>`;
    }
    showResult(html);
    renderParsedTable(best.success ? best.records : null);
}


// ══════════════════════════════════════════════════════════════ Parsed table
function renderParsedTable(records) {
    const el = document.getElementById('lab-parsed');
    const countEl = document.getElementById('lab-reccount');
    if (!records || !records.length) {
        countEl.textContent = '0 records';
        el.innerHTML = '<div class="text-muted" style="padding:18px;text-align:center">No parsed records.</div>';
        return;
    }
    countEl.textContent = `${records.length} record${records.length !== 1 ? 's' : ''}`;
    const headers = Object.keys(records[0]);
    el.innerHTML = `
        <div class="table-wrap" style="max-height:380px;overflow:auto">
            <table>
                <thead><tr>${headers.map(h => `<th>${escapeHtml(h)}</th>`).join('')}</tr></thead>
                <tbody>${records.slice(0, 200).map(r => `<tr>${headers.map(h => {
                    const v = r[h]; const d = Array.isArray(v) ? v.join(', ') : String(v ?? '');
                    return `<td class="mono fs-11">${escapeHtml(d)}</td>`;
                }).join('')}</tr>`).join('')}</tbody>
            </table>
        </div>
        ${records.length > 200 ? `<div class="text-muted" style="padding:8px 12px;font-size:11px">Showing first 200 of ${records.length}</div>` : ''}`;
}

function clearParsed() {
    document.getElementById('lab-reccount').textContent = '';
    document.getElementById('lab-parsed').innerHTML = '<div class="text-muted" style="padding:18px;text-align:center">No parsed records.</div>';
}
function showResult(html) { document.getElementById('lab-result').innerHTML = html; }


// ══════════════════════════════════════════════════════════════ Save / clear / load
async function saveTemplate() {
    const name = document.getElementById('lab-name').value.trim();
    const content = document.getElementById('lab-tpl').value;
    if (!name) return showResult('<div class="form-error">Template name is required to save.</div>');
    if (!content.trim()) return showResult('<div class="form-error">Template content is empty.</div>');
    if (_lastCompileOk === false &&
        !confirm('The last test showed a compile error. Save anyway?')) return;

    const btn = document.getElementById('lab-save');
    btn.disabled = true; btn.textContent = 'Saving…';
    try {
        if (_editingId) {
            await api.admin.updateTemplate(_editingId, { cli_command: name, textfsm_content: content });
        } else {
            const created = await api.admin.createTemplate({ cli_command: name, textfsm_content: content });
            _editingId = created.id;
            document.getElementById('lab-edit-state').textContent = `Editing #${_editingId}`;
        }
        _templates = []; // force browser reload next visit
        btn.textContent = 'Saved ✓';
        setTimeout(() => { btn.textContent = 'Save to DB'; btn.disabled = false; }, 1500);
    } catch (e) {
        showResult(`<div class="form-error">Save failed: ${escapeHtml(e.message)}</div>`);
        btn.textContent = 'Save to DB'; btn.disabled = false;
    }
}

function clearEditor() {
    _editingId = null;
    _lastCompileOk = null;
    document.getElementById('lab-name').value = '';
    document.getElementById('lab-tpl').value = '';
    document.getElementById('lab-command').value = '';
    document.getElementById('lab-edit-state').textContent = 'New template';
    document.getElementById('lab-score-pill').textContent = '';
    showResult('<div class="text-muted" style="padding:18px;text-align:center">Cleared. Write or load a template to test.</div>');
    clearParsed();
}

async function loadIntoEditor(id) {
    try {
        const tpl = await api.admin.template(id);
        _editingId = id;
        _lastCompileOk = null;
        document.getElementById('lab-name').value = tpl.cli_command || '';
        document.getElementById('lab-tpl').value = tpl.textfsm_content || '';
        document.getElementById('lab-edit-state').textContent = `Editing #${id}`;
        const guessPlat = (tpl.cli_command || '').split('_').slice(0, 2).join('_');
        const sel = document.getElementById('lab-platform');
        if ([...sel.options].some(o => o.value === guessPlat)) sel.value = guessPlat;
        switchTab('author');
        showResult('<div class="text-muted" style="padding:18px;text-align:center">Loaded. Paste output and hit Test Template.</div>');
        clearParsed();
    } catch (e) {
        alert('Load failed: ' + e.message);
    }
}


// ══════════════════════════════════════════════════════════════ Snapshot loader
async function openSnapshotModal() {
    const select = document.getElementById('snap-device');
    select.innerHTML = '<option value="">Loading devices…</option>';
    document.getElementById('snap-error').style.display = 'none';
    document.getElementById('snap-modal').style.display = '';
    try {
        const devices = await api.devices({ status: 'active' });
        select.innerHTML = '<option value="">Select device…</option>' +
            devices.map(d => `<option value="${d.id}" data-platform="${escapeHtml(d.platform_slug || '')}">${escapeHtml(d.name)} (${escapeHtml(d.site_slug || '?')})</option>`).join('');
    } catch { select.innerHTML = '<option value="">Failed to load devices</option>'; }
}

async function loadSnapshot() {
    const deviceId = document.getElementById('snap-device').value;
    const captureType = document.getElementById('snap-type').value.trim() || 'config';
    if (!deviceId) return _showError('snap-error', 'Select a device.');

    const btn = document.getElementById('snap-load');
    btn.disabled = true; btn.textContent = 'Loading…';
    try {
        const snap = await api.latest(parseInt(deviceId), captureType);
        if (!snap.raw_text && !snap.content) throw new Error('No snapshot content');
        document.getElementById('lab-raw').value = snap.raw_text || snap.content;
        const opt = document.getElementById('snap-device').selectedOptions[0];
        const platform = opt?.dataset?.platform || '';
        if (platform) document.getElementById('lab-platform').value = platform;
        if (captureType !== 'config') document.getElementById('lab-command').value = captureType.replace(/[-_]/g, ' ');
        closeModal('snap-modal');
        switchTab('author');
    } catch (e) {
        _showError('snap-error', `Failed to load snapshot: ${e.message}`);
    } finally {
        btn.disabled = false; btn.textContent = 'Load';
    }
}


// ══════════════════════════════════════════════════════════════ Browser
async function loadTemplates() {
    const platform = document.getElementById('tpl-platform-filter').value;
    const q = document.getElementById('tpl-search').value.trim();
    try {
        const data = await api.admin.templates({ platform, q });
        _templates = data.templates;
        document.getElementById('tpl-count').textContent = `${data.total} template${data.total !== 1 ? 's' : ''}`;
        renderTemplateTable();
    } catch (e) {
        document.getElementById('tpl-body').innerHTML = `<tr><td colspan="3" class="form-error">${escapeHtml(e.message)}</td></tr>`;
    }
}

function renderTemplateTable() {
    const body = document.getElementById('tpl-body');
    if (!_templates.length) {
        body.innerHTML = '<tr><td colspan="3" class="text-muted" style="text-align:center;padding:16px">No templates found.</td></tr>';
        return;
    }
    body.innerHTML = _templates.map(t => `
        <tr>
            <td class="mono text-faint">${t.id}</td>
            <td class="mono fw-500">${escapeHtml(t.cli_command)}</td>
            <td><div class="row-actions">
                <button class="btn btn-sm btn-icon" data-edit="${t.id}" title="Edit in workbench">${icons.edit}</button>
                <button class="btn btn-sm btn-icon btn-ghost" data-del="${t.id}" data-name="${escapeHtml(t.cli_command)}" title="Delete">${icons.trash}</button>
            </div></td>
        </tr>`).join('');
    body.querySelectorAll('[data-edit]').forEach(btn =>
        btn.addEventListener('click', () => loadIntoEditor(parseInt(btn.dataset.edit))));
    body.querySelectorAll('[data-del]').forEach(btn =>
        btn.addEventListener('click', async () => {
            if (!confirm(`Delete template "${btn.dataset.name}"?`)) return;
            try { await api.admin.deleteTemplate(parseInt(btn.dataset.del)); await loadTemplates(); }
            catch (e) { alert('Delete failed: ' + e.message); }
        }));
}


// ══════════════════════════════════════════════════════════════ Helpers
function closeModal(id) { document.getElementById(id).style.display = 'none'; }
function _showError(id, msg) { const el = document.getElementById(id); el.textContent = msg; el.style.display = ''; }
function _debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

export function destroy() {
    _platforms = []; _templates = []; _editingId = null; _lastCompileOk = null;
}