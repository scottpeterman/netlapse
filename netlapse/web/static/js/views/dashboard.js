/**
 * Dashboard View
 *
 * Displays: health stats (incl. a 48h config-change counter), site summary,
 * recent collection jobs.
 * Data sources: /api/v1/health, /api/v1/sites, /api/v1/jobs,
 *               /api/v1/changes/{candidates,verify}
 */

import { api } from '../api.js';
import { statCard, badge, code, formatInterval, timeAgo, icons } from '../components.js';

// Config-change counter tuning. The counter reuses the same two-phase pipeline
// as the Changes view (cheap enumerate, then batched semantic verify) so its
// number can never disagree with what the user sees on click-through.
const CFG_WINDOW = '48h';    // the counter's fixed window
const CFG_BATCH = 40;        // candidates per verify call
const CFG_VERIFY_CAP = 240;  // past this many drift candidates, show a verified
                             // floor ("N+") instead of firing a long verify loop
                             // from the dashboard — a full pass belongs on the
                             // Changes view.

export function render() {
    return `
        <div class="stat-row" id="dash-stats"></div>
        <div class="card" id="dash-sites">
            <div class="card-head">
                <span class="card-title">Sites</span>
                <a href="#/devices" class="btn-link">View all devices →</a>
            </div>
            <div class="site-grid" id="dash-sites-grid"></div>
        </div>
        <div class="card" id="dash-jobs" style="margin-top:16px">
            <div class="card-head">
                <span class="card-title">Recent Collections</span>
                <a href="#/jobs" class="btn-link">All jobs →</a>
            </div>
            <div class="table-wrap">
                <table>
                    <thead><tr>
                        <th>Job</th><th>Capture</th><th>Interval</th>
                        <th>Last Run</th><th>Devices</th><th>Duration</th><th>Status</th>
                    </tr></thead>
                    <tbody id="dash-jobs-body"></tbody>
                </table>
            </div>
        </div>
    `;
}

export async function init() {
    // Fetch all data in parallel
    const [health, sites, jobs] = await Promise.all([
        api.health().catch(() => null),
        api.sites().catch(() => []),
        api.jobs().catch(() => []),
    ]);

    // Stats
    const statsEl = document.getElementById('dash-stats');
    if (health) {
        const lastJob = jobs.filter(j => j.last_run).sort((a, b) =>
            new Date(b.last_run) - new Date(a.last_run)
        )[0];
        const lastSub = lastJob
            ? `${lastJob.name || lastJob.slug} · ${timeAgo(lastJob.last_run)}`
            : '—';
        statsEl.innerHTML =
            statCard('Devices', health.devices_active, `${health.devices_total} total`, true) +
            statCard('Sites', sites.length, sites.map(s => s.slug.toUpperCase()).join(' · ')) +
            statCard('Jobs', health.jobs_enabled, `${health.jobs_total} total`) +
            statCard('Last Run', lastJob ? timeAgo(lastJob.last_run) : '—', lastSub) +
            configChangeCard(null);   // placeholder → filled async by loadConfigChanges()
        loadConfigChanges();
    } else {
        statsEl.innerHTML = statCard('Status', 'Offline', 'API unreachable', true);
    }

    // Sites grid
    const sitesGrid = document.getElementById('dash-sites-grid');
    if (sites.length) {
        sitesGrid.innerHTML = sites.map(s => {
            const total = s.device_count || 0;
            const active = s.active_count || s.active || total;
            const inactive = total - active;
            return `
                <a class="site-cell" href="#/devices?site=${s.slug}">
                    <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:2px">
                        <span style="font-size:14px;font-weight:700">${s.slug.toUpperCase()}</span>
                        <span class="text-faint fs-11">${s.name || ''}</span>
                    </div>
                    <div style="display:flex;gap:14px;margin-top:8px;align-items:center">
                        <span class="mono" style="font-size:20px;font-weight:700">${total}</span>
                        <div style="display:flex;flex-direction:column;gap:1px">
                            <span class="text-success fs-11 fw-500">${active} active</span>
                            ${inactive > 0 ? `<span class="text-warning fs-11 fw-500">${inactive} inactive</span>` : ''}
                        </div>
                    </div>
                </a>`;
        }).join('');
    } else {
        sitesGrid.innerHTML = '<div style="padding:20px" class="text-muted">No sites found</div>';
    }

    // Jobs table
    const jobsBody = document.getElementById('dash-jobs-body');
    const enabledJobs = jobs.filter(j => j.enabled !== false).slice(0, 6);
    if (enabledJobs.length) {
        jobsBody.innerHTML = enabledJobs.map(j => {
            const collected = j.devices_collected || 0;
            const failed = j.devices_failed || 0;
            return `
                <tr class="row-link" onclick="location.hash='#/jobs'">
                    <td class="fw-500">${j.name || j.slug}</td>
                    <td>${code(j.capture_type)}</td>
                    <td class="mono text-muted fs-12">${formatInterval(j.interval_seconds)}</td>
                    <td class="text-muted fs-12">${timeAgo(j.last_run)}</td>
                    <td class="mono">${collected}<span class="text-ghost">/${collected + failed}</span></td>
                    <td class="mono fs-12">${j.duration_seconds || '—'}s</td>
                    <td>${badge(j.last_status || 'unknown')}</td>
                </tr>`;
        }).join('');
    } else {
        jobsBody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:20px">No jobs configured</td></tr>';
    }
}

// ── Config-change counter ──────────────────────────────────────────────────

/**
 * Render the config-change stat card. It's an <a> styled as a stat-card so the
 * whole tile deep-links into the Changes view (which itself defaults to config).
 *
 *   count === null   → still loading   ("…")
 *   opts.unavailable → metric N/A      ("—", e.g. non-versioned backend)
 *   opts.floor       → verified floor  ("N+", the drift set was capped)
 */
function configChangeCard(count, opts = {}) {
    let value, sub;
    if (opts.unavailable) {
        value = '—'; sub = 'unavailable';
    } else if (count === null || count === undefined) {
        value = '…'; sub = `last ${CFG_WINDOW}`;
    } else {
        value = opts.floor ? `${count}+` : String(count);
        sub = `last ${CFG_WINDOW}`;
    }
    // Draw the eye only when there's actually something to look at.
    const accent = (!opts.unavailable && count > 0) ? ' style="color:var(--blue)"' : '';
    return `
        <a id="dash-config-changes" class="stat-card stat-card-link"
           href="#/changes?since=${CFG_WINDOW}&capture=config"
           title="Config changes in the last ${CFG_WINDOW} — click for the full change feed">
            <div class="stat-label">Config Changes</div>
            <div class="stat-value"${accent}>${value}</div>
            <div class="stat-sub">${sub}</div>
        </a>`;
}

function setConfigChangeCard(count, opts = {}) {
    const el = document.getElementById('dash-config-changes');
    if (el) el.outerHTML = configChangeCard(count, opts);   // absent → navigated away
}

/**
 * Count config changes in the window the honest way: 'new'/'gone' captures are
 * real by definition, 'drift' only counts once the semantic verdict confirms it
 * moved (so a commit-confirm banner or a volatile timestamp never inflates it).
 * Runs after the synchronous stat render and fills the placeholder in place.
 */
async function loadConfigChanges() {
    let data;
    try {
        data = await api.changes.candidates({ since: CFG_WINDOW, capture_type: 'config' });
    } catch (e) {
        // 501 = backend isn't versioned (no change feed); anything else is
        // transient. Either way, show N/A rather than a misleading number.
        setConfigChangeCard(null, { unavailable: true });
        return;
    }

    const cands = data.candidates || [];
    let real = cands.filter(c => c.kind !== 'drift').length;         // new + gone
    // Only drift needs verifying, and only rows we can resolve to a device_id
    // (the verify endpoint keys off the DB, never a client-supplied path).
    const drift = cands.filter(c => c.needs_verify && c.device_id != null);

    if (!drift.length) { setConfigChangeCard(real); return; }

    const toVerify = drift.slice(0, CFG_VERIFY_CAP);
    const floor = drift.length > CFG_VERIFY_CAP || !!data.truncated;

    for (let i = 0; i < toVerify.length; i += CFG_BATCH) {
        // Bail out cleanly if the user left the dashboard mid-verify.
        if (!document.getElementById('dash-config-changes')) return;
        try {
            const res = await api.changes.verify(toVerify.slice(i, i + CFG_BATCH).map(c => ({
                device_id: c.device_id,
                capture_type: c.capture_type,
                version_id: c.version_id,
                prev_version_id: c.prev_version_id,
            })));
            real += (res.results || []).filter(r => r.real).length;
        } catch {
            /* a failed batch is skipped, not fatal — the count becomes a floor */
        }
        setConfigChangeCard(real, { floor });   // progressive update as batches land
    }
    setConfigChangeCard(real, { floor });
}

export function destroy() {}