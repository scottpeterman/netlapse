/**
 * Netlapse Shared Components
 *
 * Pure functions that return HTML strings.
 * No DOM manipulation — views handle insertion.
 */

// ══════════════════════════════════════════════════════════════
// Formatters
// ══════════════════════════════════════════════════════════════

export function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return d > 0 ? `${d}d ${h}h ${m}m` : `${h}h ${m}m`;
}

export function formatInterval(seconds) {
    if (seconds >= 86400) return `${seconds / 86400}d`;
    if (seconds >= 3600)  return `${seconds / 3600}h`;
    if (seconds >= 60)    return `${seconds / 60}m`;
    return `${seconds}s`;
}

export function timeAgo(iso) {
    if (!iso) return '—';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 0)     return 'just now';
    if (diff < 60)    return 'just now';
    if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}

export function formatBytes(bytes) {
    if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
    if (bytes >= 1024)    return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
}

export function formatTimestamp(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString('en-US', {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit', hour12: false,
        });
    } catch { return iso; }
}

export function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}


// ══════════════════════════════════════════════════════════════
// Components
// ══════════════════════════════════════════════════════════════

/**
 * Status badge.
 * Maps status strings to color classes.
 */
const STATUS_MAP = {
    active: 'success', success: 'success', ok: 'success',
    Established: 'success', up: 'success',
    maintenance: 'warning', timeout: 'warning', warning: 'warning',
    failed: 'error', auth_error: 'error', Idle: 'error', down: 'error',
    disabled: 'neutral',
    scheduled: 'info', queued: 'info',
    manual: 'purple',
};

export function badge(status) {
    const cls = STATUS_MAP[status] || 'neutral';
    const label = String(status).replace(/_/g, ' ');
    return `<span class="badge badge-${cls}"><span class="dot dot-${cls}"></span>${escapeHtml(label)}</span>`;
}

/**
 * Stat card for dashboard.
 */
export function statCard(label, value, sub = '', dark = false) {
    return `
        <div class="stat-card${dark ? ' dark' : ''}">
            <div class="stat-label">${escapeHtml(label)}</div>
            <div class="stat-value">${escapeHtml(String(value))}</div>
            ${sub ? `<div class="stat-sub">${escapeHtml(sub)}</div>` : ''}
        </div>`;
}

/**
 * Code tag.
 */
export function code(text) {
    return `<code class="code">${escapeHtml(text)}</code>`;
}

/**
 * SVG icon strings for reuse.
 */
export const icons = {
    play: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    back: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>`,
    search: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>`,
    refresh: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-6.22-8.56"/><path d="M21 3v6h-6"/></svg>`,
    file: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>`,
    diff: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v18"/><path d="M3 12h18"/></svg>`,
    git: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><line x1="12" y1="8" x2="12" y2="16"/></svg>`,
    data: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>`,
    edit: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`,
    plus: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`,
    trash: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
    toggleOn: `<svg width="18" height="13" viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="1" y="1" width="22" height="12" rx="6" fill="var(--blue)" stroke="var(--blue)"/><circle cx="17" cy="7" r="4" fill="white" stroke="none"/></svg>`,
    toggleOff: `<svg width="18" height="13" viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="1" y="1" width="22" height="12" rx="6" fill="var(--border)" stroke="var(--border)"/><circle cx="7" cy="7" r="4" fill="white" stroke="none"/></svg>`,
    copy: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    download: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
    check: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
};

/**
 * Inline search icon for search inputs.
 */
export function searchIcon() {
    return icons.search;
}


// ══════════════════════════════════════════════════════════════
// Tabular export (shared by parsed-data + config-search views)
// ══════════════════════════════════════════════════════════════

/**
 * Tab-separated values. Pastes into Excel / Sheets / Numbers as a real
 * grid. Tabs and newlines inside a cell are flattened to spaces so the
 * row/column structure survives the paste.
 */
export function recordsToTSV(rows, keys) {
    const cell = v => String(v ?? '').replace(/[\t\r\n]+/g, ' ');
    const out = [keys.map(cell).join('\t')];
    for (const r of rows) out.push(keys.map(k => cell(r[k])).join('\t'));
    return out.join('\n');
}

/**
 * RFC 4180 CSV. Quotes any field containing a comma, quote, or newline and
 * doubles embedded quotes. CRLF line endings for broad tool compatibility.
 */
export function recordsToCSV(rows, keys) {
    const cell = v => {
        const s = String(v ?? '');
        return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const out = [keys.map(cell).join(',')];
    for (const r of rows) out.push(keys.map(k => cell(r[k])).join(','));
    return out.join('\r\n');
}

/**
 * Copy text to the clipboard. Prefers the async Clipboard API (secure
 * contexts: https / localhost) and falls back to a hidden textarea +
 * execCommand for plain-HTTP LAN access by IP, where navigator.clipboard
 * is undefined. Returns true on success.
 */
export async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch { /* fall through to legacy path */ }
    }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '0';
        ta.style.left = '0';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
    } catch {
        return false;
    }
}

export function downloadFile(filename, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Download rows as CSV, BOM-prefixed so Excel reads non-ASCII correctly. */
export function downloadCSV(filename, rows, keys) {
    downloadFile(filename, '\ufeff' + recordsToCSV(rows, keys), 'text/csv;charset=utf-8');
}


export function exportFilename(parts, ext) {
    const safe = s => String(s || '')
        .replace(/[^A-Za-z0-9._-]+/g, '_')
        .replace(/^_+|_+$/g, '')
        .slice(0, 40);
    const stem = parts.map(safe).filter(Boolean).join('_') || 'export';
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    const ts = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
    return `${stem}_${ts}.${ext}`;
}

/** Brief inline confirmation on a button (no global toast system). */
export function flashButton(btn, label, ms = 1300) {
    if (!btn) return;
    const original = btn.dataset.label || btn.innerHTML;
    btn.dataset.label = original;
    btn.innerHTML = label;
    btn.disabled = true;
    setTimeout(() => {
        btn.innerHTML = original;
        btn.disabled = false;
    }, ms);
}