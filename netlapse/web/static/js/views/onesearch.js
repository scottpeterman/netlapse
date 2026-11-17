/**
 * One Search View
 *
 * NetBrain-style "one IP search": type a single token — an IP, MAC, ASN,
 * hostname, serial, VLAN — and get everything that references it, grouped
 * by where it was found.
 *
 * Two answers, side by side:
 *   Identity    which managed device(s) this token IS (mgmt/OOB address,
 *               name, serial, asset tag, description) — the "what is this"
 *   References  where it's SEEN — every stored capture type that mentions
 *               it, grouped by capture type, then by device, with the
 *               matching lines
 *
 * Token classification happens server-side, so an IP is octet-anchored
 * (10.0.0.1 won't surface 10.0.0.10), a MAC matches in every vendor format
 * regardless of how it was typed, and an ASN matches with or without AS.
 *
 * The lookup is a filesystem walk across the whole working tree, so this
 * view shows a spinner while it runs. Deep-linkable: #/onesearch/<token>.
 *
 * Data source: POST /api/v1/search/global
 */

import { api } from '../api.js';
import {
    searchIcon, escapeHtml, code, badge, icons,
    recordsToTSV, copyText, downloadCSV, exportFilename, flashButton,
} from '../components.js';

let _sites = [];
let _lastResult = null;

// Friendlier labels for known capture types; anything else shows raw.
const TYPE_LABELS = {
    config: 'Configuration',
    arp: 'ARP table',
    mac: 'MAC address table',
    'mac-table': 'MAC address table',
    bgp: 'BGP neighbors',
    routes: 'Routing table',
    route: 'Routing table',
    interfaces: 'Interfaces',
    ospf: 'OSPF',
    lldp: 'LLDP neighbors',
    cdp: 'CDP neighbors',
    inventory: 'Hardware inventory',
    version: 'Version',
};

const KIND_LABELS = {
    ipv4: 'IPv4 address',
    ipv6: 'IPv6 address',
    mac: 'MAC address',
    asn: 'AS number',
    number: 'number',
    text: 'text',
};

const expandIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>`;

export function render() {
    return `
        <div class="onesearch-intro text-muted fs-12" style="margin-bottom:10px">
            One token across everything — inventory identity plus every stored
            capture that references it. Type an IP, MAC, ASN, hostname, or serial.
        </div>
        <div class="filter-row">
            <div class="search-wrap" style="max-width:none;flex:1">
                ${searchIcon()}
                <input class="input input-search mono" id="one-search-input"
                    placeholder="10.0.0.1   ·   aabb.ccdd.eeff   ·   AS64512   ·   core-rtr-01"
                    style="width:100%;font-size:14px" value="">
            </div>
            <select class="input" id="one-search-site" style="font-size:12px;min-width:150px;padding:7px 10px">
                <option value="">All sites</option>
            </select>
            <button class="btn-primary" id="one-search-btn">Search</button>
        </div>
        <div id="one-search-results"></div>

        <div class="modal-overlay" id="one-modal" style="display:none">
            <div class="modal-container">
                <div class="modal-header">
                    <div class="modal-title" id="one-modal-title"></div>
                    <div class="modal-header-right">
                        <span class="mono text-faint fs-11" id="one-modal-count"></span>
                        <button class="modal-nav-btn" id="one-modal-prev" title="Previous match">▲</button>
                        <button class="modal-nav-btn" id="one-modal-next" title="Next match">▼</button>
                        <button class="btn btn-sm" id="one-modal-copy" title="Copy the full capture output to the clipboard">${icons.copy} Copy</button>
                        <button class="modal-close" id="one-modal-close">&times;</button>
                    </div>
                </div>
                <div class="modal-body" id="one-modal-body">
                    <div class="loading">Loading…</div>
                </div>
            </div>
        </div>
    `;
}

export async function init(params) {
    const input = document.getElementById('one-search-input');
    const btn = document.getElementById('one-search-btn');
    const siteSel = document.getElementById('one-search-site');

    // Populate site filter (best effort)
    try {
        _sites = await api.sites();
        if (_sites && _sites.length) {
            siteSel.innerHTML = '<option value="">All sites</option>' +
                _sites.map(s =>
                    `<option value="${escapeHtml(s.slug)}">${escapeHtml(s.name || s.slug)}</option>`
                ).join('');
        }
    } catch (e) {
        console.warn('Failed to load sites:', e);
    }

    const doSearch = () => {
        const q = input.value.trim();
        if (q) runSearch(q, siteSel.value || null);
    };

    btn.addEventListener('click', doSearch);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

    // Modal wiring
    const modal = document.getElementById('one-modal');
    document.getElementById('one-modal-close').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    document.addEventListener('keydown', _escHandler);
    document.getElementById('one-modal-prev').addEventListener('click', () => navigateMatch(-1));
    document.getElementById('one-modal-next').addEventListener('click', () => navigateMatch(1));
    const modalCopyBtn = document.getElementById('one-modal-copy');
    modalCopyBtn.addEventListener('click', async () => {
        if (!_modalRawText) return;
        const ok = await copyText(_modalRawText);
        flashButton(modalCopyBtn, ok ? `${icons.check} Copied` : 'Copy failed');
    });

    // Deep link: #/onesearch/<token>
    const initial = params && params.length ? decodeURIComponent(params[0]) : '';
    if (initial) {
        input.value = initial;
        runSearch(initial, null);
    } else {
        input.focus();
    }
}

async function runSearch(token, site) {
    const el = document.getElementById('one-search-results');
    el.innerHTML = `<div class="loading" style="padding:32px 0">
        Searching inventory and every capture for ${code(token)}…
    </div>`;

    let data;
    try {
        data = await api.globalSearch(token, site);
    } catch (e) {
        el.innerHTML = `<div class="text-muted" style="padding:20px">
            Search error: ${escapeHtml(e.message)}
        </div>`;
        return;
    }
    _lastResult = data;

    const inv = data.inventory || [];
    const refs = data.references || [];
    const c = data.counts || { devices: 0, capture_types: 0, lines: 0 };

    if (!inv.length && !refs.length) {
        el.innerHTML = `<div class="text-muted" style="padding:24px">
            Nothing references ${code(token)} in the inventory or any stored capture.
        </div>`;
        return;
    }

    const variantHint = (data.variants && data.variants.length > 1)
        ? ` <span class="text-faint fs-11">(matching ${data.variants.map(code).join(', ')})</span>`
        : '';

    const summary = `<div class="search-summary">
        <div>
            ${code(token)} <span class="text-faint">·</span>
            <span class="badge badge-neutral">${escapeHtml(KIND_LABELS[data.kind] || data.kind)}</span>${variantHint}
            <span class="text-faint" style="margin-left:6px">
                ${inv.length} identity match${inv.length !== 1 ? 'es' : ''},
                referenced on <strong>${c.devices}</strong> device${c.devices !== 1 ? 's' : ''}
                across <strong>${c.capture_types}</strong> capture type${c.capture_types !== 1 ? 's' : ''}
                (<strong>${c.lines}</strong> line${c.lines !== 1 ? 's' : ''})
            </span>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
            <button class="btn btn-sm" id="one-copy-btn" title="Copy all references as a tab-separated table — paste into Excel or Sheets">${icons.copy} Copy</button>
            <button class="btn btn-sm" id="one-csv-btn" title="Download all references as CSV, one row per matched line">${icons.download} CSV</button>
        </div>
    </div>`;

    el.innerHTML = summary + identitySection(inv) + referencesSection(refs);

    wireExports(token, refs);
    wireDetailButtons();
}

// ── Identity (inventory) ────────────────────────────────────────────
function identitySection(inv) {
    if (!inv.length) return '';

    const cards = inv.map(d => {
        const link = `<a href="#/device/${d.id}" class="fw-600 text-blue" style="font-size:14px">${escapeHtml(d.name)}</a>`;
        const fields = (d.matched_fields || []).map(f =>
            `<span class="badge badge-neutral fs-11">${escapeHtml(prettyField(f))}</span>`
        ).join(' ');
        const meta = [
            d.primary_ip4 ? `mgmt ${code(d.primary_ip4)}` : '',
            d.oob_ip ? `oob ${code(d.oob_ip)}` : '',
            d.site_name ? code(d.site_name) : '',
            d.role_name ? escapeHtml(d.role_name) : '',
            d.platform_name ? escapeHtml(d.platform_name) : '',
        ].filter(Boolean).join(' <span class="text-faint">·</span> ');

        return `<div class="card onesearch-identity-card">
            <div class="search-result-header">
                ${link}
                ${badge(d.status || 'active')}
                <span class="ml-auto">${fields}</span>
            </div>
            <div class="text-muted fs-12" style="margin-top:4px">${meta || '—'}</div>
        </div>`;
    }).join('');

    return `<div class="onesearch-group">
        <div class="onesearch-group-head">
            <span class="onesearch-group-icon">${idIcon}</span>
            <span class="onesearch-group-title">Identity</span>
            <span class="text-faint fs-11">this token names a managed device</span>
        </div>
        ${cards}
    </div>`;
}

// ── References, grouped by capture type ─────────────────────────────
function referencesSection(refs) {
    if (!refs.length) return '';

    // Group device entries by capture type.
    const byType = new Map();
    for (const r of refs) {
        if (!byType.has(r.capture_type)) byType.set(r.capture_type, []);
        byType.get(r.capture_type).push(r);
    }

    // Stable order: known types first in label order, then the rest alpha.
    const knownOrder = Object.keys(TYPE_LABELS);
    const types = Array.from(byType.keys()).sort((a, b) => {
        const ia = knownOrder.indexOf(a), ib = knownOrder.indexOf(b);
        if (ia !== -1 && ib !== -1) return ia - ib;
        if (ia !== -1) return -1;
        if (ib !== -1) return 1;
        return a.localeCompare(b);
    });

    return types.map(type => {
        const entries = byType.get(type);
        const deviceCount = entries.length;
        const lineCount = entries.reduce((s, e) => s + e.matches.length, 0);

        const deviceCards = entries.map(e => {
            const matchHtml = e.matches.map(m => `
                <div class="search-match-line">
                    ${m.line_number != null ? `<span class="search-ln">${m.line_number}</span>` : ''}
                    <span class="search-line-text">${highlightVariants(m.text, _lastResult.variants)}</span>
                </div>`).join('');

            const devLink = e.device_id
                ? `<a href="#/device/${e.device_id}" class="fw-600 text-blue" style="font-size:13px">${escapeHtml(e.device)}</a>`
                : `<span class="fw-600" style="font-size:13px">${escapeHtml(e.device)}</span>`;

            const detailBtn = e.device_id
                ? `<button class="btn btn-sm one-detail-btn ml-auto"
                       data-device-id="${e.device_id}"
                       data-device-name="${escapeHtml(e.device)}"
                       data-site="${escapeHtml(e.site || '')}"
                       data-capture-type="${escapeHtml(type)}"
                       title="View full ${escapeHtml(type)} output">${expandIcon} Full Output</button>`
                : '';

            return `<div class="card search-result-card">
                <div class="search-result-header">
                    ${devLink}
                    ${e.site ? code(e.site) : ''}
                    <span class="mono text-faint fs-11">${e.matches.length} line${e.matches.length !== 1 ? 's' : ''}</span>
                    ${detailBtn}
                </div>
                <div class="search-result-lines">${matchHtml}</div>
            </div>`;
        }).join('');

        return `<div class="onesearch-group">
            <div class="onesearch-group-head">
                <span class="onesearch-group-icon">${icons.file}</span>
                <span class="onesearch-group-title">${escapeHtml(TYPE_LABELS[type] || type)}</span>
                <span class="mono text-faint fs-11">${code(type)}</span>
                <span class="text-faint fs-11 ml-auto">
                    ${deviceCount} device${deviceCount !== 1 ? 's' : ''}, ${lineCount} line${lineCount !== 1 ? 's' : ''}
                </span>
            </div>
            ${deviceCards}
        </div>`;
    }).join('');
}

// ── Exports ─────────────────────────────────────────────────────────
function wireExports(token, refs) {
    const keys = ['device', 'site', 'capture_type', 'line', 'match'];
    const rows = [];
    for (const r of refs) {
        for (const m of (r.matches || [])) {
            rows.push({
                device: r.device || '',
                site: r.site || '',
                capture_type: r.capture_type || '',
                line: m.line_number != null ? m.line_number : '',
                match: m.text || '',
            });
        }
    }

    const copyBtn = document.getElementById('one-copy-btn');
    const csvBtn = document.getElementById('one-csv-btn');

    copyBtn?.addEventListener('click', async () => {
        const ok = await copyText(recordsToTSV(rows, keys));
        flashButton(copyBtn, ok ? `${icons.check} Copied` : 'Copy failed');
    });
    csvBtn?.addEventListener('click', () => {
        downloadCSV(exportFilename(['onesearch', token], 'csv'), rows, keys);
        flashButton(csvBtn, `${icons.check} Saved`);
    });
}

function wireDetailButtons() {
    document.querySelectorAll('.one-detail-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            openDetailModal(
                btn.dataset.deviceId,
                btn.dataset.deviceName,
                btn.dataset.site,
                btn.dataset.captureType,
            );
        });
    });
}

// ── Detail modal (full capture output, matches highlighted) ─────────
let _matchElements = [];
let _currentMatchIdx = -1;
let _modalRawText = '';

function _escHandler(e) { if (e.key === 'Escape') closeModal(); }

function closeModal() {
    const modal = document.getElementById('one-modal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
    _matchElements = [];
    _currentMatchIdx = -1;
    _modalRawText = '';
}

async function openDetailModal(deviceId, deviceName, site, captureType) {
    const modal = document.getElementById('one-modal');
    const body = document.getElementById('one-modal-body');
    const title = document.getElementById('one-modal-title');
    const count = document.getElementById('one-modal-count');

    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    title.innerHTML = `<strong>${escapeHtml(deviceName)}</strong> ${site ? code(site) : ''} <span class="text-faint">·</span> ${code(captureType)}`;
    count.textContent = '';
    body.innerHTML = '<div class="loading">Loading full output…</div>';
    _matchElements = [];
    _currentMatchIdx = -1;
    _modalRawText = '';

    try {
        const snap = await api.latest(deviceId, captureType);
        if (!snap || !snap.raw_text) {
            body.innerHTML = '<div class="text-muted" style="padding:20px">No data available</div>';
            return;
        }

        _modalRawText = snap.raw_text;
        const variants = (_lastResult && _lastResult.variants) || [];
        const matchRe = buildVariantRegex(variants);
        const lines = snap.raw_text.split(/\r?\n/);
        let total = 0;

        const lineHtml = lines.map((line, i) => {
            const isMatch = matchRe ? matchRe.test(line) : false;
            if (matchRe) matchRe.lastIndex = 0;
            if (isMatch) total++;
            const text = isMatch ? highlightVariants(line, variants) : escapeHtml(line);
            return `<div class="modal-line${isMatch ? ' modal-line-hit' : ''}" ${isMatch ? 'data-match="1"' : ''}>` +
                `<span class="modal-ln">${i + 1}</span>` +
                `<span class="modal-line-text">${text || '&nbsp;'}</span></div>`;
        }).join('');

        body.innerHTML = `<div class="modal-lines">${lineHtml}</div>`;
        count.textContent = `${total} match${total !== 1 ? 'es' : ''}`;
        _matchElements = Array.from(body.querySelectorAll('[data-match="1"]'));
        _currentMatchIdx = -1;
        if (_matchElements.length) navigateMatch(1);
    } catch (e) {
        body.innerHTML = `<div class="text-muted" style="padding:20px">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function navigateMatch(direction) {
    if (!_matchElements.length) return;
    if (_currentMatchIdx >= 0 && _matchElements[_currentMatchIdx]) {
        _matchElements[_currentMatchIdx].classList.remove('modal-line-focus');
    }
    _currentMatchIdx += direction;
    if (_currentMatchIdx >= _matchElements.length) _currentMatchIdx = 0;
    if (_currentMatchIdx < 0) _currentMatchIdx = _matchElements.length - 1;
    const elem = _matchElements[_currentMatchIdx];
    elem.classList.add('modal-line-focus');
    elem.scrollIntoView({ block: 'center', behavior: 'smooth' });
    const countEl = document.getElementById('one-modal-count');
    if (countEl) countEl.textContent = `${_currentMatchIdx + 1} / ${_matchElements.length}`;
}

// ── Helpers ─────────────────────────────────────────────────────────
function prettyField(f) {
    const map = {
        name: 'hostname', primary_ip4: 'mgmt IPv4', primary_ip6: 'mgmt IPv6',
        oob_ip: 'OOB IP', serial_number: 'serial', asset_tag: 'asset tag',
        description: 'description',
    };
    return map[f] || f;
}

/** Build a single regex that matches any of the server-provided variants. */
function buildVariantRegex(variants) {
    if (!variants || !variants.length) return null;
    const esc = variants
        .map(v => v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .sort((a, b) => b.length - a.length); // longest first so we never half-match
    try { return new RegExp(`(${esc.join('|')})`, 'gi'); }
    catch { return null; }
}

/** Highlight every variant occurrence in a line. */
function highlightVariants(text, variants) {
    const re = buildVariantRegex(variants);
    if (!re) return escapeHtml(text);
    const parts = text.split(re);
    return parts.map((part, i) =>
        i % 2 === 1 ? `<mark>${escapeHtml(part)}</mark>` : escapeHtml(part)
    ).join('');
}

const idIcon = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M15 8h3M15 12h3M7 16h10"/></svg>`;

export function destroy() {
    _lastResult = null;
    closeModal();
    document.removeEventListener('keydown', _escHandler);
}