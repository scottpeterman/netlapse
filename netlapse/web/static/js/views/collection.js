/**
 * Live Collection View
 *
 * Start collection runs and watch real-time progress via WebSocket.
 * Data sources: /api/v1/jobs (for job selector), /ws (progress)
 */

import { api } from '../api.js';
import { ws } from '../ws.js';
import { badge, formatBytes, icons, escapeHtml } from '../components.js';

let unsubs = [];
let jobs = [];

export function render() {
    return `
        <div class="filter-row" style="margin-bottom:16px">
            <button class="btn-primary" id="coll-start-btn">${icons.play} Start Collection</button>
            <select class="select" id="coll-job-select">
                <option value="">Loading jobs…</option>
            </select>
            <span class="mono text-blue fs-12" id="coll-progress-label"></span>
        </div>
        <div id="coll-output"></div>
    `;
}

export async function init() {
    // Load jobs for the selector
    try {
        jobs = await api.jobs(true); // enabled only
    } catch {
        jobs = [];
    }

    const select = document.getElementById('coll-job-select');
    if (jobs.length) {
        select.innerHTML = jobs.map(j => {
            const label = `${j.name || j.slug} (${j.capture_type})`;
            const scope = j.site_filter ? ` — ${j.site_filter}` : ' — all sites';
            return `<option value="${escapeHtml(j.slug)}">${escapeHtml(label + scope)}</option>`;
        }).join('');
    } else {
        select.innerHTML = '<option value="">No jobs available</option>';
    }

    // Start button
    document.getElementById('coll-start-btn').addEventListener('click', startCollection);

    // Connect WebSocket and subscribe to events
    ws.connect();

    const u1 = ws.on('collection_start', onCollectionStart);
    const u2 = ws.on('collection_progress', onCollectionProgress);
    const u3 = ws.on('collection_complete', onCollectionComplete);
    const u4 = ws.on('device_collected', onDeviceCollected);
    unsubs.push(u1, u2, u3, u4);
}

async function startCollection() {
    const btn = document.getElementById('coll-start-btn');
    const select = document.getElementById('coll-job-select');
    const slug = select.value;

    if (!slug) return;

    btn.disabled = true;
    btn.innerHTML = `${icons.refresh} Collecting…`;

    // Reset output area
    const output = document.getElementById('coll-output');
    output.innerHTML = `
        <div class="card" id="coll-card">
            <div class="card-head" id="coll-header">
                <span class="card-title">Collection starting…</span>
                <span class="mono text-blue fs-12" id="coll-percent"></span>
            </div>
            <div class="progress-track"><div class="progress-fill" id="coll-bar" style="width:0%"></div></div>
            <div id="coll-log" style="max-height:400px;overflow:auto"></div>
        </div>
    `;

    // Trigger the job via API
    try {
        await api.triggerJob(slug);
        document.getElementById('coll-progress-label').textContent = 'Queued';
    } catch (e) {
        document.getElementById('coll-header').querySelector('.card-title').textContent =
            `Error: ${e.message}`;
        btn.disabled = false;
        btn.innerHTML = `${icons.play} Start Collection`;
    }
}

// ── WebSocket event handlers ──
// These wire to whatever event schema your scheduler emits.
// The structure is designed to match the natural output of your collector.

function onCollectionStart(data) {
    const header = document.getElementById('coll-header');
    if (header) {
        const total = data.device_count || '?';
        header.querySelector('.card-title').textContent =
            `Collecting from ${total} devices…`;
    }
}

function onDeviceCollected(data) {
    const log = document.getElementById('coll-log');
    if (!log) return;

    const status = data.status || 'success';
    const dotClass = status === 'success' ? 'dot-success' : 'dot-error';
    const bytes = data.bytes ? formatBytes(data.bytes) : '—';
    const duration = data.duration != null ? `${data.duration}s` : '—';

    log.insertAdjacentHTML('beforeend', `
        <div class="coll-row">
            <span class="dot ${dotClass}"></span>
            <code class="mono fw-500" style="min-width:180px;font-size:12px">${escapeHtml(data.device || '—')}</code>
            <span class="text-muted">${escapeHtml(data.command || 'show running-config')}</span>
            <span class="mono text-faint ml-auto">${bytes}</span>
            <span class="mono text-faint">${duration}</span>
        </div>
    `);

    // Auto-scroll
    log.scrollTop = log.scrollHeight;
}

function onCollectionProgress(data) {
    const bar = document.getElementById('coll-bar');
    const pct = document.getElementById('coll-percent');
    if (!bar || !pct) return;

    const collected = data.collected || 0;
    const total = data.total || 1;
    const percent = Math.round((collected / total) * 100);

    bar.style.width = `${percent}%`;
    pct.textContent = `${percent}% (${collected}/${total})`;
}

function onCollectionComplete(data) {
    const bar = document.getElementById('coll-bar');
    const header = document.getElementById('coll-header');
    const btn = document.getElementById('coll-start-btn');
    const pct = document.getElementById('coll-percent');

    if (bar) {
        bar.style.width = '100%';
        bar.classList.add('done');
    }
    if (pct) pct.textContent = data.duration ? `${data.duration}s` : 'Done';
    if (header) {
        const total = data.collected || data.device_count || '—';
        const failed = data.failed || 0;
        header.querySelector('.card-title').textContent =
            `Collection complete — ${total} collected, ${failed} failed`;
    }

    if (btn) {
        btn.disabled = false;
        btn.innerHTML = `${icons.play} Start Collection`;
    }

    // Summary row
    const card = document.getElementById('coll-card');
    if (card) {
        card.insertAdjacentHTML('beforeend', `
            <div class="summary-row">
                <span><strong>${data.collected || '—'}</strong> collected</span>
                <span><strong>${data.failed || 0}</strong> failed</span>
                ${data.duration ? `<span>Duration: <strong class="mono">${data.duration}s</strong></span>` : ''}
            </div>
        `);
    }
}

export function destroy() {
    // Unsubscribe from all WS events
    unsubs.forEach(fn => fn());
    unsubs = [];
    jobs = [];
}
