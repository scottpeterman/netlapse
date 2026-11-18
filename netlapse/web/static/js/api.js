/**
 * Netlapse API Client
 *
 * Centralizes all /api/v1 and Oxidized-compat endpoint calls.
 * Views import { api } and never construct URLs directly.
 */

const BASE = '/api/v1';

function _redirectToLogin() {
    const next = encodeURIComponent(location.pathname + location.hash);
    location.href = `/login?next=${next}`;
}

async function request(path, opts = {}) {
    const res = await fetch(`${BASE}${path}`, opts);
    if (res.status === 401) {
        _redirectToLogin();
        throw new Error('401: not authenticated');
    }
    if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`${res.status} ${res.statusText}: ${text}`);
    }
    return res.json();
}

export const api = {
    // ── Auth ──
    auth: {
        me:       ()     => request('/auth/me'),
        logout:   ()     => request('/auth/logout', { method: 'POST' }),
        changePassword: (current_password, new_password) =>
            request('/auth/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password, new_password }),
            }),
        setTheme: (theme) =>
            request('/auth/theme', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ theme }),
            }),
    },

    // ── First-run setup wizard ──
    setup: {
        status:  ()           => request('/setup/status'),
        vault:   (password, confirm) => request('/setup/vault', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password, confirm }),
        }),
        // map body is the raw map.json text; opts → query string
        mapPreview: (text, opts={}) => request('/setup/map/preview?' + new URLSearchParams(
            Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== '' && v != null))), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: text,
        }),
        mapImport:  (text, opts={}) => request('/setup/map/import?' + new URLSearchParams(
            Object.fromEntries(Object.entries(opts).filter(([, v]) => v !== '' && v != null))), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: text,
        }),
        complete: () => request('/setup/complete', { method: 'POST' }),
    },

    // ── Native API ──
    health:      ()         => request('/health'),
    devices:     (params={})=> request('/devices?' + new URLSearchParams(params)),
    device:      (id)       => request(`/devices/${id}`),
    updateDevice:(id, data) => request(`/devices/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }),
    createDevice:(data)     => request('/devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }),
    deleteDevice:(id)       => request(`/devices/${id}`, { method: 'DELETE' }),
    sites:       ()         => request('/sites'),
    platforms:   ()         => request('/platforms'),
    roles:       ()         => request('/roles'),
    credentials: ()         => request('/credentials'),
    snapshots:   (id, opts={}) => request(`/devices/${id}/snapshots?` + new URLSearchParams(opts)),
    latest:      (id, type='config') => request(`/devices/${id}/snapshots/latest?capture_type=${type}`),
    diff:        (id, type='config', from, to, mask=false, ignore=null) => {
        const p = new URLSearchParams({ capture_type: type });
        if (from) p.set('from_commit', from);
        if (to)   p.set('to_commit', to);
        if (mask) p.set('mask', 'true');
        // null = let the server apply per-capture defaults; an array (even
        // empty) is an explicit choice and is always sent.
        if (ignore !== null) p.set('ignore', ignore.join(','));
        return request(`/devices/${id}/diff?${p}`);
    },
    jobs:        (enabledOnly=false) => request(`/jobs?enabled_only=${enabledOnly}`),
    job:         (slug)     => request(`/jobs/${slug}`),
    triggerJob:  (slug)     => request(`/jobs/${slug}/run`, { method: 'POST' }),
    createJob:   (data)     => request('/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }),
    updateJob:   (slug, data) => request(`/jobs/${slug}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }),
    deleteJob:   (slug)     => request(`/jobs/${slug}`, { method: 'DELETE' }),
    setJobEnabled: (slug, enabled) => request(`/jobs/${slug}/enabled`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
    }),
        jobCaptureTypes: () => request('/jobs/capture-types'),

    jobHistory:  (slug, limit=25) => request(`/jobs/${slug}/history?limit=${limit}`),
    // Collect Now. types: null/'' = all applicable captures, else a capture
    // type or comma-separated list. Returns a tracked request to poll.
    collect:     (id, types=null) => request(
        `/collect/${id}` + (types ? `?capture_type=${encodeURIComponent(types)}` : ''),
        { method: 'POST' }),
    collectRequest: (rid) => request(`/collect/requests/${encodeURIComponent(rid)}`),
    collectPlan:    (id)  => request(`/devices/${id}/collect/plan`),
    collectLatest:  (id)  => request(`/devices/${id}/collect/latest`),
    testAuth:    (id) => request(`/devices/${id}/test-auth`, { method: 'POST' }),
    // ── Oxidized Compat ──
    confSearch: (pattern) => fetch('/nodes/conf_search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: `search_in_conf_textbox=${encodeURIComponent(pattern)}`,
    }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
    }),
    nodes:       (group)    => fetch('/nodes' + (group ? `?group=${group}` : '')).then(r => r.json()),
    nodeConfig:  (node)     => fetch(`/node/fetch/${node}`).then(r => r.text()),

    // ── Native Search ──
    search: (pattern, captureType = 'config', site = null) => {
        const body = { pattern, capture_type: captureType };
        if (site) body.site = site;
        return request('/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    },
    captureTypes: () => request('/search/capture_types'),

    // ── Change Feed (two-phase: cheap enumerate, then batched verify) ──
    // ── Topology (map from stored LLDP) — q is a URLSearchParams ──
    topology: {
        map:     (q) => request('/topology?' + q),
        diff:    (q) => request('/topology/diff?' + q),
        changes: (q) => request('/topology/changes?' + q),
    },

    changes: {
        candidates: (params = {}) =>
            request('/changes/candidates?' + new URLSearchParams(params)),
        verify: (items) => request('/changes/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items }),
        }),
    },

    // One Search — single token across inventory + all captures
    globalSearch: (token, site = null) => {
        const body = { token };
        if (site) body.site = site;
        return request('/search/global', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    },

    // ── Parse Audit ──
    parseAudit: {
        audit: (params = {}) =>
            request('/parse-audit?' + new URLSearchParams(params)),
        reparse: (data) => request('/parse-audit/reparse', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
    },

    // ── Admin API ──
    admin: {
        // ── Backup / restore ──
        backup: {
            status:  ()            => request('/admin/backup'),
            create:  (data, label) => request('/admin/backup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ data, label: label || null }),
            }),
            job:     ()            => request('/admin/backup/job'),
            remove:  (name)        => request(`/admin/backup/files/${encodeURIComponent(name)}`, { method: 'DELETE' }),
            restore: (name)        => request(`/admin/backup/files/${encodeURIComponent(name)}/restore`, { method: 'POST' }),
            cancel:  ()            => request('/admin/backup/pending', { method: 'DELETE' }),
            upload:  (file)        => request(`/admin/backup/upload?filename=${encodeURIComponent(file.name)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/octet-stream' },
                body: file,
            }),
            downloadUrl: (name)    => `${BASE}/admin/backup/files/${encodeURIComponent(name)}`,
        },
        system:       ()     => request('/admin/system'),
        config:       ()     => request('/admin/config'),
        saveConfig:   (content) => request('/admin/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        }),

        // ── App users ──
        users: {
            list:   ()           => request('/admin/users'),
            create: (data)       => request('/admin/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            }),
            update: (id, data)   => request(`/admin/users/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            }),
            setPassword: (id, password) => request(`/admin/users/${id}/password`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password }),
            }),
            remove: (id)         => request(`/admin/users/${id}`, { method: 'DELETE' }),
        },
        vaultStatus:      () => request('/admin/vault/status'),
        vaultCredentials: () => request('/admin/vault/credentials'),
        addCredential:    (data) => request('/admin/vault/credentials', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
        deleteCredential: (id) => request(`/admin/vault/credentials/${id}`, { method: 'DELETE' }),
        setDefaultCredential: (id) => request(`/admin/vault/credentials/${id}/default`, { method: 'PUT' }),
        assignCredential: (data) => request('/admin/vault/credentials/assign', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
        templates:        (params={}) => request('/admin/templates?' + new URLSearchParams(params)),
        templatePlatforms:()          => request('/admin/templates/platforms'),
        template:         (id)        => request(`/admin/templates/${id}`),
        updateTemplate:   (id, data)  => request(`/admin/templates/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
        createTemplate:   (data) => request('/admin/templates', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
        deleteTemplate:   (id) => request(`/admin/templates/${id}`, { method: 'DELETE' }),
        testParse:        (data) => request('/admin/templates/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),
        testTemplate:     (data) => request('/admin/templates/test-template', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }),

        // ── DCIM reference data (sites / platforms / roles / manufacturers) ──
        // `kind` is the plural path segment. list() returns { <kind>: [...] }.
        dcim: {
            list:   (kind)          => request(`/admin/dcim/${kind}`),
            create: (kind, data)    => request(`/admin/dcim/${kind}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            }),
            update: (kind, id, data)=> request(`/admin/dcim/${kind}/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            }),
            remove: (kind, id)      => request(`/admin/dcim/${kind}/${id}`, { method: 'DELETE' }),
            assign: (data)          => request('/admin/dcim/devices/assign', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            }),
        },
    },
};