/**
 * Netlapse App — Hash Router + Shell Wiring
 *
 * Manages view lifecycle: render → init → destroy
 * Dynamic imports so each view JS loads on demand.
 */

import { api } from './api.js';
import { formatUptime } from './components.js';

// ══════════════════════════════════════════════════════════════
// View registry — lazy-loaded ES modules
// ══════════════════════════════════════════════════════════════

const views = {
    dashboard:   () => import('./views/dashboard.js'),
    devices:     () => import('./views/devices.js'),
    device:      () => import('./views/device.js'),
    jobs:        () => import('./views/jobs.js'),
    search:      () => import('./views/search.js'),
    onesearch:   () => import('./views/onesearch.js'),
    collection:  () => import('./views/collection.js'),
    settings:    () => import('./views/settings.js'),
    credentials: () => import('./views/credentials.js'),
    templates:   () => import('./views/templates.js'),
    reference:   () => import('./views/reference.js'),
    audit:       () => import('./views/audit.js'),
    changes:     () => import('./views/changes.js'),
    topology:    () => import('./views/topology.js'),
    users:       () => import('./views/users.js'),
    setup:       () => import('./views/setup.js'),
};

// Maps view names to sidebar nav data-view attributes
const VIEW_TO_NAV = {
    dashboard: 'dashboard',
    devices: 'devices',
    device: 'devices',     // device detail highlights "Devices" in nav
    jobs: 'jobs',
    search: 'search',
    onesearch: 'onesearch',
    collection: 'collection',
    settings: 'settings',
    credentials: 'credentials',
    templates: 'templates',
    reference: 'reference',
    audit: 'audit',
    changes: 'changes',
    topology: 'topology',
    users: 'users',
    setup: 'setup',
};

const VIEW_TITLES = {
    dashboard: 'Dashboard',
    devices: 'Devices',
    device: '',            // set dynamically by the view
    jobs: 'Jobs',
    search: 'Config Search',
    onesearch: 'One Search',
    collection: 'Live Collection',
    settings: 'Settings',
    credentials: 'Credentials',
    templates: 'Template Lab',
    reference: 'Reference Data',
    audit: 'Parse Audit',
    changes: 'Changes',
    topology: 'Topology',
    users: 'Users',
    setup: 'Setup',
};


// ══════════════════════════════════════════════════════════════
// Router
// ══════════════════════════════════════════════════════════════

let currentView = null;
let currentViewName = '';
let currentUser = null;

// Views only an admin may open.
const ADMIN_ONLY_VIEWS = new Set(['settings', 'credentials', 'users', 'setup']);

function isAdmin() {
    return !!(currentUser && currentUser.role === 'admin');
}

/**
 * Parse hash into view name and params.
 * #/devices        → { name: 'devices', params: [] }
 * #/device/42      → { name: 'device', params: ['42'] }
 * #/devices?site=site1 → { name: 'devices', params: [] } (query in hash)
 */
function parseHash(hash) {
    const clean = (hash || '#/dashboard').replace(/^#\/?/, '');
    const [path] = clean.split('?');
    const parts = path.split('/').filter(Boolean);
    return {
        name: parts[0] || 'dashboard',
        params: parts.slice(1),
    };
}

async function navigate(hash) {
    const { name, params } = parseHash(hash);
    const loader = views[name];

    if (!loader) {
        // Unknown route — redirect to dashboard
        location.hash = '#/dashboard';
        return;
    }

    // Admin-only views are off-limits to viewers — bounce to dashboard.
    if (ADMIN_ONLY_VIEWS.has(name) && !isAdmin()) {
        if (name !== 'dashboard') location.hash = '#/dashboard';
        return;
    }

    // Teardown current view
    if (currentView && currentView.destroy) {
        try { currentView.destroy(); } catch (e) {
            console.error('View destroy error:', e);
        }
    }

    // Update sidebar active state
    document.querySelectorAll('.nav-item').forEach(el => {
        const navView = el.dataset.view;
        el.classList.toggle('active', navView === (VIEW_TO_NAV[name] || name));
    });

    // Update topbar title (views can override this in init)
    const title = document.getElementById('topbar-title');
    title.textContent = VIEW_TITLES[name] || name;

    // Clean topbar extras from previous view
    document.querySelectorAll('.topbar-back').forEach(el => el.remove());
    document.querySelectorAll('.topbar-left .badge').forEach(el => el.remove());

    // Load, render, init
    const content = document.getElementById('content');
    content.innerHTML = '<div class="loading">Loading…</div>';

    try {
        const mod = await loader();
        content.innerHTML = mod.render(params);
        if (mod.init) await mod.init(params);
        currentView = mod;
        currentViewName = name;
    } catch (e) {
        console.error('View load error:', e);
        content.innerHTML = `<div class="loading">Error loading view: ${e.message}</div>`;
    }
}


// ══════════════════════════════════════════════════════════════
// Sidebar health polling
// ══════════════════════════════════════════════════════════════

let healthTimer = null;

async function updateSidebarHealth() {
    try {
        const h = await api.health();
        document.getElementById('sidebar-version').textContent =
            `v${h.version} · ${h.storage_backend.replace('Backend', '')}`;
        document.getElementById('sidebar-footer').innerHTML = `
            <div class="status-dot-row">
                <span class="dot dot-success"></span>
                <span>System healthy</span>
            </div>
            <div>${formatUptime(h.uptime_seconds)} uptime</div>
            <div>${h.devices_active}/${h.devices_total} devices</div>
        `;
    } catch {
        document.getElementById('sidebar-footer').innerHTML = `
            <div class="status-dot-row">
                <span class="dot dot-error"></span>
                <span>API unreachable</span>
            </div>
        `;
    }
}


// ══════════════════════════════════════════════════════════════
// Account chrome (current user + sign out)
// ══════════════════════════════════════════════════════════════

async function initUserChrome() {
    try {
        const { user } = await api.auth.me();
        currentUser = user;
        const right = document.querySelector('.topbar-right');
        if (right && user) {
            const el = document.createElement('div');
            el.className = 'topbar-user';
            el.style.cssText = 'display:flex;align-items:center;gap:10px;margin-left:6px';
            // Single source of truth: read the stylesheet the server injected,
            // so the button can never disagree with what's on screen.
            const themeLink = document.getElementById('theme-css');
            const href = themeLink ? themeLink.getAttribute('href') : '';
            const isDark = href.includes('netlapse-dark');
            const moon = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
            const sun = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
            el.innerHTML = `
                <button class="btn btn-sm btn-icon" id="btn-theme" title="Switch to ${isDark ? 'light' : 'dark'} theme" aria-label="Toggle theme">${isDark ? sun : moon}</button>
                <span class="text-muted" title="${user.role}">${user.username}</span>
                <button class="btn btn-sm" id="btn-logout">Sign out</button>`;
            right.appendChild(el);
            document.getElementById('btn-theme').addEventListener('click', async (e) => {
                const btn = e.currentTarget;
                btn.disabled = true;
                const next = isDark ? 'light' : 'dark';  // flip what's rendered
                try { await api.auth.setTheme(next); } catch {}
                location.reload();  // server re-renders with the matching link
            });
            document.getElementById('btn-logout').addEventListener('click', async () => {
                try { await api.auth.logout(); } catch {}
                location.href = '/login';
            });
        }
        // Hide admin-only nav entries from non-admins.
        if (!isAdmin()) {
            document.querySelectorAll('.nav-item[data-admin="1"]')
                .forEach(el => { el.style.display = 'none'; });
        }
    } catch { /* gate will redirect on 401 */ }
}


// ══════════════════════════════════════════════════════════════
// Bootstrap
// ══════════════════════════════════════════════════════════════

window.addEventListener('hashchange', () => navigate(location.hash));

// Initial load — resolve the current user (and role) before first navigation
// so admin-only route guards and nav visibility are correct on first paint.
(async function boot() {
    await initUserChrome();
    updateSidebarHealth();
    healthTimer = setInterval(updateSidebarHealth, 90000);
    // Unfinished first run → open the setup wizard (admins only).
    if (isAdmin() && !(location.hash || '').startsWith('#/setup')) {
        try {
            const st = await api.setup.status();
            if (st.should_open) { location.hash = '#/setup'; return; }  // hashchange navigates
        } catch { /* older server or transient — just continue */ }
    }
    navigate(location.hash || '#/dashboard');
})();