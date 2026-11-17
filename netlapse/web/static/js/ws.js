/**
 * Netlapse WebSocket Manager
 *
 * Wraps the /ws endpoint with auto-reconnect and event dispatch.
 * Views subscribe to events during init() and unsubscribe during destroy().
 *
 * Usage:
 *   import { ws } from '../ws.js';
 *   const unsub = ws.on('collection_progress', (data) => { ... });
 *   // later:
 *   unsub();
 */

/**
 * Resolve the WebSocket authority for the current page.
 *
 * The page URL is normally routable and used as-is. The exception is a
 * wildcard bind address: 0.0.0.0 and :: mean "listen on every interface"
 * to a server, but neither is a valid destination. Browsing to the address
 * copied out of config.yaml therefore yields a page that loads (navigation
 * is permitted) while the WebSocket is refused — Chrome 128+ blocks
 * 0.0.0.0 for subresource and WS connections outright.
 *
 * Rewriting the wildcard to the matching loopback preserves the port and
 * leaves every other host untouched, so hostnames, LAN addresses and
 * reverse-proxy deployments are unaffected.
 */
function resolveWsAuthority() {
    const hostname = location.hostname;
    const port = location.port ? `:${location.port}` : '';

    if (hostname === '0.0.0.0') {
        return `127.0.0.1${port}`;
    }
    // location.hostname strips the brackets that location.host retains,
    // so they are re-added for the IPv6 literal.
    if (hostname === '::' || hostname === '[::]') {
        return `[::1]${port}`;
    }
    return location.host;
}

class WebSocketManager {
    constructor() {
        this._ws = null;
        this._listeners = {};
        this._reconnectTimer = null;
        this._reconnectDelay = 2000;
        this._maxDelay = 90000;
        this._intentionalClose = false;
    }

    /**
     * Connect to the WebSocket endpoint.
     * Safe to call multiple times — no-ops if already connected.
     */
    connect() {
        if (this._ws && (this._ws.readyState === WebSocket.OPEN || this._ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        this._intentionalClose = false;
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${resolveWsAuthority()}/ws`;

        try {
            this._ws = new WebSocket(url);
        } catch (e) {
            console.warn('WebSocket connection failed:', e);
            this._scheduleReconnect();
            return;
        }

        this._ws.onopen = () => {
            this._reconnectDelay = 2000;
            this._emit('_connected', {});
        };

        this._ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                const type = data.type || 'message';
                this._emit(type, data);
            } catch {
                this._emit('message', { raw: event.data });
            }
        };

        this._ws.onclose = () => {
            if (!this._intentionalClose) {
                this._scheduleReconnect();
            }
            this._emit('_disconnected', {});
        };

        this._ws.onerror = () => {
            // onclose will fire after this
        };
    }

    /**
     * Disconnect intentionally (no auto-reconnect).
     */
    disconnect() {
        this._intentionalClose = true;
        clearTimeout(this._reconnectTimer);
        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }
    }

    /**
     * Send a message to the server.
     */
    send(data) {
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
            this._ws.send(typeof data === 'string' ? data : JSON.stringify(data));
        }
    }

    /**
     * Subscribe to an event type. Returns an unsubscribe function.
     *
     * @param {string} type - Event type to listen for
     * @param {function} callback - Handler function
     * @returns {function} Unsubscribe function
     */
    on(type, callback) {
        if (!this._listeners[type]) {
            this._listeners[type] = [];
        }
        this._listeners[type].push(callback);

        // Return unsubscribe function
        return () => {
            this._listeners[type] = this._listeners[type].filter(fn => fn !== callback);
        };
    }

    /** @returns {boolean} */
    get connected() {
        return this._ws && this._ws.readyState === WebSocket.OPEN;
    }

    // ── Internal ──

    _emit(type, data) {
        const handlers = this._listeners[type] || [];
        for (const fn of handlers) {
            try { fn(data); } catch (e) { console.error(`WS handler error [${type}]:`, e); }
        }
    }

    _scheduleReconnect() {
        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = setTimeout(() => {
            this.connect();
        }, this._reconnectDelay);
        // Exponential backoff with cap
        this._reconnectDelay = Math.min(this._reconnectDelay * 1.5, this._maxDelay);
    }
}

export const ws = new WebSocketManager();
