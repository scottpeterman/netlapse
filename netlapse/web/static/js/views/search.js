/**
 * Config Search View
 *
 * Regex search across all stored capture types — configs, ARP tables,
 * BGP summaries, route tables, interface status, and any other job output.
 *
 * Detail modal: click the expand icon on any device card to see the
 * full captured output with line numbers and highlighted matches.
 *
 * Data source: POST /api/v1/search, GET /api/v1/search/capture_types
 */

import { api } from '../api.js';
import {
    searchIcon, escapeHtml, code, badge, icons,
    recordsToTSV, copyText, downloadCSV, exportFilename, flashButton,
} from '../components.js';

let lastQuery = '';
let lastType = 'config';
let captureTypes = ['config'];

// ── Inline icon for the detail button ──
const expandIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>`;

export function render() {
    return `
        <div class="filter-row">
            <div class="search-type-wrap">
                <select class="input" id="conf-search-type" style="font-size:12px;min-width:190px;padding:7px 10px">
                    <option value="config">config</option>
                </select>
            </div>
            <div class="search-wrap" style="max-width:none;flex:1">
                ${searchIcon()}
                <input class="input input-search mono" id="conf-search-input"
                    placeholder="Search pattern (regex supported)…"
                    style="width:100%;font-size:13px" value="">
            </div>
            <button class="btn-primary" id="conf-search-btn">Search</button>
        </div>
        <div id="conf-search-results"></div>
        <div class="modal-overlay" id="search-modal" style="display:none">
            <div class="modal-container">
                <div class="modal-header">
                    <div class="modal-title" id="modal-title"></div>
                    <div class="modal-header-right">
                        <span class="mono text-faint fs-11" id="modal-match-count"></span>
                        <button class="modal-nav-btn" id="modal-prev" title="Previous match">▲</button>
                        <button class="modal-nav-btn" id="modal-next" title="Next match">▼</button>
                        <button class="modal-close" id="modal-close">&times;</button>
                    </div>
                </div>
                <div class="modal-body" id="modal-body">
                    <div class="loading">Loading…</div>
                </div>
            </div>
        </div>
    `;
}

export async function init() {
    const input = document.getElementById('conf-search-input');
    const btn = document.getElementById('conf-search-btn');
    const typeSelect = document.getElementById('conf-search-type');

    // Load available capture types
    try {
        const types = await api.captureTypes();
        if (types && types.length) {
            captureTypes = types;
            typeSelect.innerHTML = types.map(t =>
                `<option value="${escapeHtml(t)}"${t === lastType ? ' selected' : ''}>${escapeHtml(t)}</option>`
            ).join('');
        }
    } catch (e) {
        console.warn('Failed to load capture types:', e);
    }

    const doSearch = () => {
        const q = input.value.trim();
        if (q) {
            lastType = typeSelect.value;
            runSearch(q, lastType);
        }
    };

    btn.addEventListener('click', doSearch);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSearch();
    });

    // Update placeholder hint when capture type changes
    typeSelect.addEventListener('change', () => {
        const t = typeSelect.value;
        const hints = {
            config:     'e.g. ntp server|logging host',
            arp:        'e.g. 10\\.68\\.48\\.',
            bgp:        'e.g. Established|Idle',
            routes:     'e.g. 0\\.0\\.0\\.0|default',
            interfaces: 'e.g. GigabitEthernet|up',
        };
        input.placeholder = hints[t] || `Search pattern across ${t} data…`;
    });

    // Modal close handlers
    const modal = document.getElementById('search-modal');
    document.getElementById('modal-close').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
    });
    document.addEventListener('keydown', _escHandler);

    // Modal match navigation
    document.getElementById('modal-prev').addEventListener('click', () => navigateMatch(-1));
    document.getElementById('modal-next').addEventListener('click', () => navigateMatch(1));

    input.focus();
}

// ── Modal state ──
let _matchElements = [];
let _currentMatchIdx = -1;

function _escHandler(e) {
    if (e.key === 'Escape') closeModal();
}

function closeModal() {
    const modal = document.getElementById('search-modal');
    if (modal) modal.style.display = 'none';
    document.body.style.overflow = '';
    _matchElements = [];
    _currentMatchIdx = -1;
}

async function openDetailModal(deviceId, deviceName, site, captureType, query) {
    const modal = document.getElementById('search-modal');
    const body = document.getElementById('modal-body');
    const title = document.getElementById('modal-title');
    const matchCount = document.getElementById('modal-match-count');

    // Show modal immediately with loading state
    modal.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    title.innerHTML = `<strong>${escapeHtml(deviceName)}</strong> ${site ? code(site) : ''} <span class="text-faint">·</span> ${code(captureType)}`;
    matchCount.textContent = '';
    body.innerHTML = '<div class="loading">Loading full output…</div>';
    _matchElements = [];
    _currentMatchIdx = -1;

    try {
        const snap = await api.latest(deviceId, captureType);
        if (!snap || !snap.raw_text) {
            body.innerHTML = '<div class="text-muted" style="padding:20px">No data available</div>';
            return;
        }

        const lines = snap.raw_text.split(/\r?\n/);
        let regex;
        try { regex = new RegExp(`(${query})`, 'gi'); } catch { regex = null; }

        let matchTotal = 0;

        const lineHtml = lines.map((line, i) => {
            const lineNum = i + 1;
            const isMatch = regex && regex.test(line);
            regex && (regex.lastIndex = 0); // reset after .test()

            if (isMatch) matchTotal++;

            const highlighted = isMatch ? highlightMatch(line, query) : escapeHtml(line);
            const cls = isMatch ? ' modal-line-hit' : '';

            return `<div class="modal-line${cls}" ${isMatch ? 'data-match="1"' : ''}>` +
                `<span class="modal-ln">${lineNum}</span>` +
                `<span class="modal-line-text">${highlighted || '&nbsp;'}</span>` +
                `</div>`;
        }).join('');

        body.innerHTML = `<div class="modal-lines">${lineHtml}</div>`;

        matchCount.textContent = `${matchTotal} match${matchTotal !== 1 ? 'es' : ''}`;

        // Collect match elements for navigation
        _matchElements = Array.from(body.querySelectorAll('[data-match="1"]'));
        _currentMatchIdx = -1;

        // Scroll to first match
        if (_matchElements.length) {
            navigateMatch(1);
        }

    } catch (e) {
        body.innerHTML = `<div class="text-muted" style="padding:20px">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function navigateMatch(direction) {
    if (!_matchElements.length) return;

    // Remove current highlight
    if (_currentMatchIdx >= 0 && _matchElements[_currentMatchIdx]) {
        _matchElements[_currentMatchIdx].classList.remove('modal-line-focus');
    }

    // Advance
    _currentMatchIdx += direction;
    if (_currentMatchIdx >= _matchElements.length) _currentMatchIdx = 0;
    if (_currentMatchIdx < 0) _currentMatchIdx = _matchElements.length - 1;

    // Highlight and scroll
    const el = _matchElements[_currentMatchIdx];
    el.classList.add('modal-line-focus');
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });

    // Update counter
    const countEl = document.getElementById('modal-match-count');
    if (countEl) {
        countEl.textContent = `${_currentMatchIdx + 1} / ${_matchElements.length}`;
    }
}


async function runSearch(query, captureType) {
    lastQuery = query;
    const el = document.getElementById('conf-search-results');
    el.innerHTML = '<div class="loading">Searching…</div>';

    try {
        const results = await api.search(query, captureType);

        if (!results || !results.length) {
            el.innerHTML = `<div class="text-muted" style="padding:20px">
                No matches found for ${code(query)} in <strong>${escapeHtml(captureType)}</strong> data
            </div>`;
            return;
        }

        // Total match count
        const totalMatches = results.reduce((s, r) => s + (r.matches ? r.matches.length : 0), 0);

        // Flatten to one row per matched line, each carrying its device so the
        // export stands on its own once the per-device grouping is gone.
        const exportKeys = ['device', 'site', 'capture_type', 'line', 'match'];
        const exportRows = [];
        for (const r of results) {
            const dev = r.device || '';
            const site = r.site || '';
            for (const m of (r.matches || [])) {
                const isObj = m && typeof m === 'object';
                exportRows.push({
                    device: dev,
                    site,
                    capture_type: captureType,
                    line: isObj && m.line_number != null ? m.line_number : '',
                    match: isObj ? (m.text || '') : String(m),
                });
            }
        }

        const summary = `<div class="search-summary">
            <div>
                <strong>${results.length} device${results.length !== 1 ? 's' : ''}</strong>,
                <strong>${totalMatches} line${totalMatches !== 1 ? 's' : ''}</strong>
                matched ${code(query)} in <strong>${escapeHtml(captureType)}</strong> data
            </div>
            <div style="display:flex;align-items:center;gap:8px">
                <button class="btn btn-sm" id="search-copy-btn" title="Copy all ${totalMatches} matched line${totalMatches !== 1 ? 's' : ''} as a tab-separated table (device, site, line, match) — paste into Excel or Sheets">${icons.copy} Copy</button>
                <button class="btn btn-sm" id="search-csv-btn" title="Download all matched lines as a CSV file, one row per match with its device">${icons.download} CSV</button>
            </div>
        </div>`;

        const cards = results.map(r => {
            const device = r.device || '—';
            const site = r.site || '';
            const deviceId = r.device_id;
            const matches = r.matches || [];

            const matchHtml = matches.map(m => {
                const lineNum = m.line_number != null ? m.line_number : '';
                const lineText = m.text || (typeof m === 'string' ? m : '');
                const highlighted = highlightMatch(lineText, query);
                return `<div class="search-match-line">
                    ${lineNum ? `<span class="search-ln">${lineNum}</span>` : ''}
                    <span class="search-line-text">${highlighted}</span>
                </div>`;
            }).join('');

            const deviceLink = deviceId
                ? `<a href="#/device/${deviceId}" class="fw-600 text-blue" style="font-size:13px">${escapeHtml(device)}</a>`
                : `<span class="fw-600" style="font-size:13px">${escapeHtml(device)}</span>`;

            // Detail button — only if we have a device_id to fetch from
            const detailBtn = deviceId
                ? `<button class="btn btn-sm search-detail-btn ml-auto"
                       data-device-id="${deviceId}"
                       data-device-name="${escapeHtml(device)}"
                       data-site="${escapeHtml(site)}"
                       title="View full ${escapeHtml(captureType)} output">${expandIcon} Full Output</button>`
                : '';

            return `
                <div class="card search-result-card">
                    <div class="search-result-header">
                        ${deviceLink}
                        ${site ? code(site) : ''}
                        <span class="mono text-faint fs-11">${matches.length} match${matches.length !== 1 ? 'es' : ''}</span>
                        ${detailBtn}
                    </div>
                    <div class="search-result-lines">${matchHtml}</div>
                </div>`;
        }).join('');

        el.innerHTML = summary + cards;

        // Wire export buttons (whole result set, flattened)
        const copyBtn = document.getElementById('search-copy-btn');
        const csvBtn = document.getElementById('search-csv-btn');

        copyBtn?.addEventListener('click', async () => {
            const ok = await copyText(recordsToTSV(exportRows, exportKeys));
            flashButton(copyBtn, ok ? `${icons.check} Copied` : 'Copy failed');
        });

        csvBtn?.addEventListener('click', () => {
            downloadCSV(exportFilename(['search', captureType, query], 'csv'), exportRows, exportKeys);
            flashButton(csvBtn, `${icons.check} Saved`);
        });

        // Wire up detail buttons
        el.querySelectorAll('.search-detail-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                openDetailModal(
                    btn.dataset.deviceId,
                    btn.dataset.deviceName,
                    btn.dataset.site,
                    captureType,
                    query,
                );
            });
        });

    } catch (e) {
        el.innerHTML = `<div class="text-muted" style="padding:20px">
            Search error: ${escapeHtml(e.message)}
        </div>`;
    }
}

/**
 * Highlight regex matches in a line of text.
 * Falls back to plain text if regex is invalid.
 */
function highlightMatch(text, pattern) {
    try {
        const re = new RegExp(`(${pattern})`, 'gi');
        const parts = text.split(re);
        return parts.map((part, i) => {
            const safe = escapeHtml(part);
            return i % 2 === 1 ? `<mark>${safe}</mark>` : safe;
        }).join('');
    } catch {
        return escapeHtml(text);
    }
}

export function destroy() {
    lastQuery = '';
    closeModal();
    document.removeEventListener('keydown', _escHandler);
}