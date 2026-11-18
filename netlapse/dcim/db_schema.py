"""
Netlapse DCIM Database Schema

Ported from VelocityCollector's NetBox-aligned DCIM schema.
Tables prefixed with 'dcim_' to mirror NetBox's app.model pattern.

Changes from VelocityCollector:
  - netmiko_device_type → platform_profile (no netmiko dependency)
  - Added prompt_regex, enable_command to platform (SC2 SSH client needs)
  - Added legacy_ssh to platform (for devices requiring DH group1, etc.)
  - Collector tables (vault, credentials, jobs, job_history) in same DB
  - Job definitions are persistent (daemon model, not one-shot files)
  - Schema version bumped to 3

Key design decisions:
  - slug fields for URL-friendly identifiers and storage paths
  - netbox_id for sync, but NetBox is never required
  - site_slug is the git repo top-level directory (= Oxidized group)
  - UNIQUE(name, site_id) prevents collision in storage paths
"""

import sqlite3
import threading
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 10

SCHEMA_SQL = """
-- ============================================================================
-- Schema version tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================================
-- DCIM TABLES — NetBox Compatible Subset
-- ============================================================================

-- Sites - Physical locations (data centers, POPs, offices)
-- site.slug = Oxidized group = git repo top-level directory
CREATE TABLE IF NOT EXISTS dcim_site (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    description TEXT,
    physical_address TEXT,
    facility TEXT,
    time_zone TEXT,
    netbox_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dcim_site_slug ON dcim_site(slug);
CREATE INDEX IF NOT EXISTS idx_dcim_site_status ON dcim_site(status);

-- Manufacturers - Hardware vendors
CREATE TABLE IF NOT EXISTS dcim_manufacturer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    netbox_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dcim_manufacturer_slug ON dcim_manufacturer(slug);

-- Platforms - OS / software
-- Critical for collection: determines SSH behavior, paging, prompt matching,
-- and tfsm-fire template selection.
CREATE TABLE IF NOT EXISTS dcim_platform (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    manufacturer_id INTEGER,
    description TEXT,

    -- SSH collection profile (replaces netmiko_device_type)
    platform_profile TEXT,              -- e.g., 'cisco_ios', 'arista_eos'
    paging_disable_command TEXT,         -- e.g., 'terminal length 0'
    prompt_regex TEXT,                   -- override for prompt detection (optional)
    enable_command TEXT,                 -- e.g., 'enable' (NULL = no enable needed)
    legacy_ssh INTEGER DEFAULT 0,       -- 1 = enable legacy algorithms (DH group1, etc.)

    netbox_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (manufacturer_id) REFERENCES dcim_manufacturer(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_dcim_platform_slug ON dcim_platform(slug);
CREATE INDEX IF NOT EXISTS idx_dcim_platform_profile ON dcim_platform(platform_profile);

-- Device Roles - Functional roles (router, switch, firewall, etc.)
CREATE TABLE IF NOT EXISTS dcim_device_role (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    color TEXT DEFAULT '9e9e9e',
    description TEXT,
    netbox_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dcim_device_role_slug ON dcim_device_role(slug);

-- Devices — the main event
CREATE TABLE IF NOT EXISTS dcim_device (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,

    -- Relationships
    site_id INTEGER NOT NULL,
    platform_id INTEGER,
    role_id INTEGER,

    -- Device attributes
    status TEXT NOT NULL DEFAULT 'active',
    serial_number TEXT,
    asset_tag TEXT UNIQUE,

    -- Network identity (critical for collection)
    primary_ip4 TEXT,
    primary_ip6 TEXT,
    oob_ip TEXT,

    -- Collection settings
    credential_id INTEGER,              -- FK to credentials (NULL = use default)
    ssh_port INTEGER DEFAULT 22,
    legacy_ssh INTEGER,                 -- NULL = platform default, 0 = off, 1 = on

    -- Credential testing
    credential_tested_at TEXT,
    credential_test_result TEXT DEFAULT 'untested',

    -- Collection state
    last_collected_at TEXT,
    last_collection_status TEXT,         -- success, failed, timeout, auth_error
    collection_enabled INTEGER DEFAULT 1,

    -- Metadata
    description TEXT,
    comments TEXT,
    netbox_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),

    -- Constraints
    FOREIGN KEY (site_id) REFERENCES dcim_site(id) ON DELETE CASCADE,
    FOREIGN KEY (platform_id) REFERENCES dcim_platform(id) ON DELETE SET NULL,
    FOREIGN KEY (role_id) REFERENCES dcim_device_role(id) ON DELETE SET NULL,
    -- NOTE: credential_id intentionally has NO foreign key. It holds a VAULT
    -- credential id (a separate database file), resolved in bridge.resolve_credentials.
    -- An FK to the DCIM `credentials` table here could never be satisfied.

    UNIQUE (name, site_id)
);

CREATE INDEX IF NOT EXISTS idx_dcim_device_name ON dcim_device(name);
CREATE INDEX IF NOT EXISTS idx_dcim_device_site ON dcim_device(site_id);
CREATE INDEX IF NOT EXISTS idx_dcim_device_platform ON dcim_device(platform_id);
CREATE INDEX IF NOT EXISTS idx_dcim_device_role ON dcim_device(role_id);
CREATE INDEX IF NOT EXISTS idx_dcim_device_status ON dcim_device(status);
CREATE INDEX IF NOT EXISTS idx_dcim_device_primary_ip4 ON dcim_device(primary_ip4);
CREATE INDEX IF NOT EXISTS idx_dcim_device_collection ON dcim_device(collection_enabled);

-- ============================================================================
-- COLLECTOR TABLES — Vault, Credentials, Jobs
-- ============================================================================

-- Vault metadata (PBKDF2 salt, check value, iterations)
CREATE TABLE IF NOT EXISTS vault_metadata (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    value TEXT
);

-- Encrypted credentials
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    username TEXT NOT NULL,
    password_encrypted TEXT,
    ssh_key_encrypted TEXT,
    ssh_key_passphrase_encrypted TEXT,
    is_default INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_credentials_name ON credentials(name);
CREATE INDEX IF NOT EXISTS idx_credentials_default ON credentials(is_default);

-- Job definitions (persistent, unlike VelocityCollector's JSON job files)
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    enabled INTEGER DEFAULT 1,

    -- What to collect
    capture_type TEXT NOT NULL,           -- config, arp, bgp, interfaces, etc.
    commands TEXT NOT NULL,               -- JSON array of CLI commands (default/fallback)
    command_map TEXT,                     -- JSON object: {platform_slug: [commands]} (per-platform override)

    -- Which devices (filters, all optional — NULL = all devices)
    platform_filter TEXT,                 -- platform slug or comma-separated list
    site_filter TEXT,                     -- site slug or comma-separated list
    role_filter TEXT,                     -- role slug or comma-separated list
    device_filter TEXT,                   -- device name pattern (SQL LIKE)
    status_filter TEXT DEFAULT 'active',  -- device status filter

    -- Schedule
    interval_seconds INTEGER DEFAULT 3600,
    last_run_at TEXT,
    next_run_at TEXT,

    -- Provenance, three states:
    --   'file' = synced from the definition registry (jobs.yaml), managed by
    --            sync_definitions(); read-only in the UI/API, tombstoned when
    --            removed from the catalog.
    --   'seed' = a shipped default no file owns yet; editable like a normal
    --            job, never tombstoned, and ADOPTED (promoted seed→file) by
    --            sync the moment a jobs.yaml binding with its slug appears.
    --   NULL/'api' = created via API/UI; never touched by sync.
    -- Lets file-authority, shipped defaults, and hand-made jobs coexist
    -- without sync clobbering or tombstoning anything it doesn't own.
    source TEXT,

    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_slug ON jobs(slug);
CREATE INDEX IF NOT EXISTS idx_jobs_enabled ON jobs(enabled);
CREATE INDEX IF NOT EXISTS idx_jobs_next_run ON jobs(next_run_at);

-- Job execution history
CREATE TABLE IF NOT EXISTS job_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    job_slug TEXT NOT NULL,
    trigger TEXT DEFAULT 'scheduled',     -- scheduled, syslog, manual, api
    started_at TEXT NOT NULL,
    completed_at TEXT,
    total_devices INTEGER,
    success_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    status TEXT,                          -- running, completed, failed, cancelled
    error_message TEXT,

    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_job_history_job ON job_history(job_slug);
CREATE INDEX IF NOT EXISTS idx_job_history_started ON job_history(started_at);
CREATE INDEX IF NOT EXISTS idx_job_history_status ON job_history(status);

-- Per-device collection results within a job run
CREATE TABLE IF NOT EXISTS job_device_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_history_id INTEGER NOT NULL,
    device_id INTEGER,
    device_name TEXT NOT NULL,
    status TEXT NOT NULL,                 -- success, failed, timeout, auth_error, skipped
    error_category TEXT,                  -- from executor error categorization
    error_message TEXT,
    duration_seconds REAL,
    version_id TEXT,                      -- git SHA or timestamp from storage backend
    started_at TEXT NOT NULL,
    completed_at TEXT,

    FOREIGN KEY (job_history_id) REFERENCES job_history(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES dcim_device(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_job_device_results_history ON job_device_results(job_history_id);
CREATE INDEX IF NOT EXISTS idx_job_device_results_device ON job_device_results(device_name);
CREATE INDEX IF NOT EXISTS idx_job_device_results_status ON job_device_results(status);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- Full device detail (avoids repeated JOINs in app code)
CREATE VIEW IF NOT EXISTS v_device_detail AS
SELECT
    d.id,
    d.name,
    d.status,
    d.primary_ip4,
    d.primary_ip6,
    d.oob_ip,
    d.ssh_port,
    d.serial_number,
    d.asset_tag,
    d.credential_id,
    d.credential_tested_at,
    d.credential_test_result,
    d.description,
    d.last_collected_at,
    d.last_collection_status,
    d.collection_enabled,
    d.legacy_ssh AS device_legacy_ssh,
    d.netbox_id,
    d.created_at,
    d.updated_at,
    -- Site
    s.id AS site_id,
    s.name AS site_name,
    s.slug AS site_slug,
    -- Platform
    p.id AS platform_id,
    p.name AS platform_name,
    p.slug AS platform_slug,
    p.platform_profile,
    p.paging_disable_command,
    p.prompt_regex,
    p.enable_command,
    p.legacy_ssh,
    -- Manufacturer (via platform)
    m.id AS manufacturer_id,
    m.name AS manufacturer_name,
    m.slug AS manufacturer_slug,
    -- Role
    r.id AS role_id,
    r.name AS role_name,
    r.slug AS role_slug,
    -- Credential
    c.name AS credential_name,
    c.username AS credential_username
FROM dcim_device d
LEFT JOIN dcim_site s ON d.site_id = s.id
LEFT JOIN dcim_platform p ON d.platform_id = p.id
LEFT JOIN dcim_manufacturer m ON p.manufacturer_id = m.id
LEFT JOIN dcim_device_role r ON d.role_id = r.id
LEFT JOIN credentials c ON d.credential_id = c.id;

-- Site summary with device counts
CREATE VIEW IF NOT EXISTS v_site_summary AS
SELECT
    s.id,
    s.name,
    s.slug,
    s.status,
    COUNT(d.id) AS device_count,
    COUNT(CASE WHEN d.status = 'active' THEN 1 END) AS active_devices,
    COUNT(CASE WHEN d.collection_enabled = 1 THEN 1 END) AS collection_enabled,
    MAX(d.last_collected_at) AS last_collection
FROM dcim_site s
LEFT JOIN dcim_device d ON s.id = d.site_id
GROUP BY s.id;

-- Platform summary with device counts
CREATE VIEW IF NOT EXISTS v_platform_summary AS
SELECT
    p.id,
    p.name,
    p.slug,
    p.platform_profile,
    m.name AS manufacturer_name,
    COUNT(d.id) AS device_count
FROM dcim_platform p
LEFT JOIN dcim_manufacturer m ON p.manufacturer_id = m.id
LEFT JOIN dcim_device d ON p.id = d.platform_id
GROUP BY p.id;

-- Job summary with last run info (field names match frontend expectations)
CREATE VIEW IF NOT EXISTS v_job_summary AS
SELECT
    j.id,
    j.name,
    j.slug,
    j.description,
    j.enabled,
    j.capture_type,
    j.commands,
    j.command_map,
    j.platform_filter,
    j.site_filter,
    j.role_filter,
    j.device_filter,
    j.status_filter,
    j.interval_seconds,
    j.last_run_at AS last_run,
    j.next_run_at,
    j.source,
    j.created_at,
    j.updated_at,
    h.status AS last_status,
    h.trigger AS last_trigger,
    h.total_devices AS last_total,
    h.success_count AS devices_collected,
    h.failed_count AS devices_failed,
    h.skipped_count AS devices_skipped,
    ROUND(
        (julianday(h.completed_at) - julianday(h.started_at)) * 86400, 1
    ) AS duration_seconds,
    h.completed_at AS last_completed,
    h.id AS last_history_id
FROM jobs j
LEFT JOIN job_history h ON h.id = (
    SELECT id FROM job_history
    WHERE job_slug = j.slug
    ORDER BY started_at DESC
    LIMIT 1
);
"""

# ---------------------------------------------------------------------------
# Default seed data
# ---------------------------------------------------------------------------

DEFAULT_MANUFACTURERS = [
    ("Cisco", "cisco", None),
    ("Arista", "arista", None),
    ("Juniper", "juniper", None),
    ("Palo Alto", "palo-alto", None),
    ("Fortinet", "fortinet", None),
    ("F5", "f5", None),
    ("HP", "hp", None),
    ("Dell", "dell", None),
]

DEFAULT_PLATFORMS = [
    # (name, slug, mfg_slug, platform_profile, paging_disable, enable_cmd, legacy_ssh)
    ("Cisco IOS", "cisco_ios", "cisco", "cisco_ios", "terminal length 0", "enable", 0),
    ("Cisco IOS-XE", "cisco_ios_xe", "cisco", "cisco_xe", "terminal length 0", "enable", 0),
    ("Cisco IOS-XR", "cisco_ios_xr", "cisco", "cisco_xr", "terminal length 0", None, 0),
    ("Cisco NX-OS", "cisco_nxos", "cisco", "cisco_nxos", "terminal length 0", None, 0),
    ("Cisco ASA", "cisco_asa", "cisco", "cisco_asa", "terminal pager 0", "enable", 0),
    ("Arista EOS", "arista_eos", "arista", "arista_eos", "terminal length 0", None, 0),
    ("Juniper Junos", "juniper_junos", "juniper", "juniper_junos", "set cli screen-length 0", None, 0),
    ("Palo Alto PAN-OS", "paloalto_panos", "palo-alto", "paloalto_panos", "set cli pager off", None, 0),
    ("Fortinet FortiOS", "fortinet_fortios", "fortinet", "fortinet", "config system console\nset output standard\nend", None, 0),
    ("F5 TMOS", "f5_tmos", "f5", "f5_tmsh", "modify cli preference pager disabled", None, 0),
    ("HP ProCurve", "hp_procurve", "hp", "hp_procurve", "no page", None, 1),
    ("HP Comware", "hp_comware", "hp", "hp_comware", "screen-length disable", None, 1),
    ("Dell OS10", "dell_os10", "dell", "dell_os10", "terminal length 0", None, 0),
]

DEFAULT_DEVICE_ROLES = [
    ("Router", "router", "3498db"),
    ("Switch", "switch", "2ecc71"),
    ("Firewall", "firewall", "e74c3c"),
    ("Load Balancer", "load-balancer", "9b59b6"),
    ("Wireless Controller", "wireless-controller", "f39c12"),
    ("Access Point", "access-point", "1abc9c"),
    ("Spine", "spine", "3498db"),
    ("Leaf", "leaf", "2ecc71"),
    ("Core", "core", "e67e22"),
    ("Distribution", "distribution", "f1c40f"),
    ("Access", "access", "27ae60"),
    ("border", "border", "9b59b6"),
]

DEFAULT_JOBS = [
    # (name, slug, capture_type, commands_json, command_map_json, interval)
    #
    # commands = default/fallback (Cisco IOS syntax)
    # command_map = per-platform overrides where syntax differs
    # Platforms not in the map fall through to the default commands.
    #
    ("Config backup", "config-backup", "config",
     '["show running-config"]',
     '{"juniper_junos": ["show configuration | display set"]}',
     3600),
    ("ARP table", "arp-table", "arp",
     '["show ip arp"]',
     '{"juniper_junos": ["show arp no-resolve"], "arista_eos": ["show arp"]}',
     1800),
    ("BGP summary", "bgp-summary", "bgp",
     '["show ip bgp summary"]',
     '{"juniper_junos": ["show bgp summary"]}',
     900),
    ("Interface status", "interface-status", "interfaces",
     '["show interfaces"]',
     None,
     1800),
    ("Route table", "route-table", "routes",
     '["show ip route"]',
     '{"juniper_junos": ["show route"]}',
     1800),
    ("OSPF neighbors", "ospf-neighbors", "ospf",
     '["show ip ospf neighbor"]',
     '{"juniper_junos": ["show ospf neighbor"], "nokia_sros": ["show router ospf neighbor"]}',
     1800),
    ("MAC address table", "mac-table", "mac",
     '["show mac address-table"]',
     '{"juniper_junos": ["show ethernet-switching table"]}',
     1800),
    ("LLDP neighbors detail", "lldp-neighbors", "lldp",
     '["show lldp neighbors detail"]',
     '{"juniper_junos": ["show lldp neighbors"]}',
     1800),
]


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------

class _ThreadSafeConnection:
    """
    Proxy that serializes all sqlite3.Connection access through an RLock.

    Every call to execute / executemany / executescript / commit / cursor
    acquires the lock first, so callers never see SQLITE_BUSY or corrupt
    state regardless of which thread they're on.

    The RLock is reentrant: a method that calls another method on the
    same db instance won't deadlock.
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    # --- Core sqlite3 operations, each serialized ---

    def execute(self, sql, parameters=()):
        with self._lock:
            return self._conn.execute(sql, parameters)

    def executemany(self, sql, seq_of_params):
        with self._lock:
            return self._conn.executemany(sql, seq_of_params)

    def executescript(self, sql_script):
        with self._lock:
            return self._conn.executescript(sql_script)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def cursor(self):
        with self._lock:
            return self._conn.cursor()

    def close(self):
        with self._lock:
            return self._conn.close()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value


class _Transaction:
    """Context manager: holds the RLock for an explicit BEGIN/COMMIT block."""

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self._lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
        return False


class NetlapseDB:
    """
    Unified database manager for Netlapse.

    Handles both DCIM tables (sites, platforms, devices) and
    collector tables (vault, credentials, jobs, history).
    Single SQLite file at ~/.netlapse/netlapse.db.

    Thread safety:
      One sqlite3.Connection shared across all threads, serialized
      through an RLock via the _ThreadSafeConnection proxy.
      isolation_level=None (autocommit) prevents implicit transactions
      from lingering and blocking other callers.

      For multi-step writes that need atomicity, use:
          with db.transaction() as conn:
              conn.execute("INSERT ...")
              conn.execute("INSERT ...")
              # auto-committed on clean exit, rolled back on exception
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.home() / ".netlapse" / "netlapse.db"

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._raw_conn = sqlite3.connect(
            str(self.db_path),
            timeout=10,
            check_same_thread=False,
            isolation_level=None,           # autocommit — no implicit txns
        )
        self._raw_conn.row_factory = sqlite3.Row
        self._raw_conn.execute("PRAGMA foreign_keys = ON")
        self._raw_conn.execute("PRAGMA journal_mode = WAL")
        self._raw_conn.execute("PRAGMA busy_timeout = 5000")
        self._proxy = _ThreadSafeConnection(self._raw_conn, self._lock)

    @property
    def conn(self) -> _ThreadSafeConnection:
        """Thread-safe connection proxy.

        Every execute / commit / cursor call acquires the RLock,
        so this is safe from any thread. No thread-local connections,
        no WAL contention, no implicit transactions.
        """
        return self._proxy

    def transaction(self):
        """Context manager for multi-step atomic writes.

        Usage:
            with db.transaction() as txn:
                txn.execute("INSERT ...")
                txn.execute("UPDATE ...")
            # COMMIT on clean exit, ROLLBACK on exception

        The RLock is held for the entire block, so no other thread
        can interleave writes.
        """
        return _Transaction(self._raw_conn, self._lock)

    def close(self):
        with self._lock:
            self._raw_conn.close()

    def init_schema(self, include_defaults: bool = True) -> bool:
        """
        Initialize database schema. Returns True if fresh, False if existing.
        """
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='dcim_site'"
        )
        if cursor.fetchone() is None:
            cursor.executescript(SCHEMA_SQL)
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            if include_defaults:
                self._seed_defaults(cursor)
            self.conn.commit()
            logger.info("Initialized fresh database at %s", self.db_path)
            return True

        # Check version for future migrations
        cursor.execute("SELECT MAX(version) FROM schema_version")
        current = cursor.fetchone()[0] or 0
        if current < SCHEMA_VERSION:
            logger.info("Database at v%d, current is v%d", current, SCHEMA_VERSION)

            if current < 4:
                # v3 → v4: Rebuild v_job_summary with correct field names
                # and full column set for frontend/scheduler
                logger.info("Migrating v3 → v4: rebuilding v_job_summary")
                cursor = self.conn.cursor()
                cursor.executescript("""
                    DROP VIEW IF EXISTS v_job_summary;
                    CREATE VIEW v_job_summary AS
                    SELECT
                        j.id,
                        j.name,
                        j.slug,
                        j.description,
                        j.enabled,
                        j.capture_type,
                        j.commands,
                        j.platform_filter,
                        j.site_filter,
                        j.role_filter,
                        j.device_filter,
                        j.status_filter,
                        j.interval_seconds,
                        j.last_run_at AS last_run,
                        j.next_run_at,
                        j.created_at,
                        j.updated_at,
                        h.status AS last_status,
                        h.trigger AS last_trigger,
                        h.total_devices AS last_total,
                        h.success_count AS devices_collected,
                        h.failed_count AS devices_failed,
                        h.skipped_count AS devices_skipped,
                        ROUND(
                            (julianday(h.completed_at) - julianday(h.started_at)) * 86400, 1
                        ) AS duration_seconds,
                        h.completed_at AS last_completed,
                        h.id AS last_history_id
                    FROM jobs j
                    LEFT JOIN job_history h ON h.id = (
                        SELECT id FROM job_history
                        WHERE job_slug = j.slug
                        ORDER BY started_at DESC
                        LIMIT 1
                    );
                """)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (4,)
                )
                self.conn.commit()
                logger.info("Migration v3 → v4 complete")

            if current < 5:
                # v4 → v5: Add command_map column to jobs,
                # rebuild v_job_summary to include it
                logger.info("Migrating v4 → v5: adding command_map to jobs")
                cursor = self.conn.cursor()
                # Add column (safe if it already exists — catch and ignore)
                try:
                    cursor.execute(
                        "ALTER TABLE jobs ADD COLUMN command_map TEXT"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
                cursor.executescript("""
                    DROP VIEW IF EXISTS v_job_summary;
                    CREATE VIEW v_job_summary AS
                    SELECT
                        j.id,
                        j.name,
                        j.slug,
                        j.description,
                        j.enabled,
                        j.capture_type,
                        j.commands,
                        j.command_map,
                        j.platform_filter,
                        j.site_filter,
                        j.role_filter,
                        j.device_filter,
                        j.status_filter,
                        j.interval_seconds,
                        j.last_run_at AS last_run,
                        j.next_run_at,
                        j.created_at,
                        j.updated_at,
                        h.status AS last_status,
                        h.trigger AS last_trigger,
                        h.total_devices AS last_total,
                        h.success_count AS devices_collected,
                        h.failed_count AS devices_failed,
                        h.skipped_count AS devices_skipped,
                        ROUND(
                            (julianday(h.completed_at) - julianday(h.started_at)) * 86400, 1
                        ) AS duration_seconds,
                        h.completed_at AS last_completed,
                        h.id AS last_history_id
                    FROM jobs j
                    LEFT JOIN job_history h ON h.id = (
                        SELECT id FROM job_history
                        WHERE job_slug = j.slug
                        ORDER BY started_at DESC
                        LIMIT 1
                    );
                """)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (5,)
                )
                self.conn.commit()
                logger.info("Migration v4 → v5 complete")

            if current < 6:
                # v5 → v6: Add device-level legacy_ssh override
                logger.info(
                    "Migrating v5 → v6: adding legacy_ssh to dcim_device"
                )
                cursor = self.conn.cursor()
                try:
                    cursor.execute(
                        "ALTER TABLE dcim_device "
                        "ADD COLUMN legacy_ssh INTEGER"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
                # Rebuild v_device_detail to expose device_legacy_ssh
                cursor.executescript("""
                    DROP VIEW IF EXISTS v_device_detail;
                    CREATE VIEW v_device_detail AS
                    SELECT
                        d.id,
                        d.name,
                        d.status,
                        d.primary_ip4,
                        d.primary_ip6,
                        d.oob_ip,
                        d.ssh_port,
                        d.serial_number,
                        d.asset_tag,
                        d.credential_id,
                        d.credential_tested_at,
                        d.credential_test_result,
                        d.description,
                        d.last_collected_at,
                        d.last_collection_status,
                        d.collection_enabled,
                        d.legacy_ssh AS device_legacy_ssh,
                        d.netbox_id,
                        d.created_at,
                        d.updated_at,
                        s.id AS site_id,
                        s.name AS site_name,
                        s.slug AS site_slug,
                        p.id AS platform_id,
                        p.name AS platform_name,
                        p.slug AS platform_slug,
                        p.platform_profile,
                        p.paging_disable_command,
                        p.prompt_regex,
                        p.enable_command,
                        p.legacy_ssh,
                        m.id AS manufacturer_id,
                        m.name AS manufacturer_name,
                        m.slug AS manufacturer_slug,
                        r.id AS role_id,
                        r.name AS role_name,
                        r.slug AS role_slug,
                        c.name AS credential_name,
                        c.username AS credential_username
                    FROM dcim_device d
                    LEFT JOIN dcim_site s ON d.site_id = s.id
                    LEFT JOIN dcim_platform p ON d.platform_id = p.id
                    LEFT JOIN dcim_manufacturer m ON p.manufacturer_id = m.id
                    LEFT JOIN dcim_device_role r ON d.role_id = r.id
                    LEFT JOIN credentials c ON d.credential_id = c.id;
                """)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (6,)
                )
                self.conn.commit()
                logger.info("Migration v5 → v6 complete")

            if current < 7:
                # v6 → v7: Drop the vestigial credential_id FK on dcim_device.
                #
                # credential_id holds a VAULT credential id (a separate DB
                # file), resolved in bridge.resolve_credentials. The original
                # schema declared FOREIGN KEY (credential_id) REFERENCES
                # credentials(id) — the empty DCIM mirror table — so any write
                # of a real credential id raised 'FOREIGN KEY constraint
                # failed'. The bulk-assign path worked around it by toggling
                # PRAGMA foreign_keys OFF; the device edit/create paths did not.
                #
                # SQLite can't drop a single FK in place, so rebuild the table
                # (same columns, same other FKs, uniques, indexes — just no
                # credential_id FK). Per the documented table-redefinition
                # procedure, any VIEW that references the table must be dropped
                # first and recreated afterward, or the schema change errors
                # with "no such table: dcim_device" mid-flight. We snapshot the
                # views and the table's own indexes from sqlite_master so the
                # rebuild restores them exactly, including any not defined here.
                logger.info(
                    "Migrating v6 → v7: removing vestigial credential_id FK "
                    "on dcim_device"
                )
                cursor = self.conn.cursor()

                # Snapshot dependent objects (DDL strings) before touching the
                # table. autoindexes from UNIQUE have sql=NULL and are excluded
                # (the new CREATE TABLE re-creates them via its constraints).
                view_ddls = cursor.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='view' AND sql IS NOT NULL"
                ).fetchall()
                index_ddls = cursor.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='dcim_device' "
                    "AND sql IS NOT NULL"
                ).fetchall()
                view_names = cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'"
                ).fetchall()

                # FK enforcement must be toggled outside a transaction; this
                # connection is autocommit (isolation_level=None).
                cursor.execute("PRAGMA foreign_keys = OFF")
                cursor.execute("BEGIN")
                try:
                    # 1. Drop every view (recreated verbatim at the end).
                    for (vname,) in view_names:
                        cursor.execute(f"DROP VIEW IF EXISTS {vname}")

                    # 2. New table — identical except no credential_id FK.
                    cursor.execute("""
                        CREATE TABLE dcim_device_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT NOT NULL,
                            site_id INTEGER NOT NULL,
                            platform_id INTEGER,
                            role_id INTEGER,
                            status TEXT NOT NULL DEFAULT 'active',
                            serial_number TEXT,
                            asset_tag TEXT UNIQUE,
                            primary_ip4 TEXT,
                            primary_ip6 TEXT,
                            oob_ip TEXT,
                            credential_id INTEGER,
                            ssh_port INTEGER DEFAULT 22,
                            legacy_ssh INTEGER,
                            credential_tested_at TEXT,
                            credential_test_result TEXT DEFAULT 'untested',
                            last_collected_at TEXT,
                            last_collection_status TEXT,
                            collection_enabled INTEGER DEFAULT 1,
                            description TEXT,
                            comments TEXT,
                            netbox_id INTEGER UNIQUE,
                            created_at TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                            FOREIGN KEY (site_id) REFERENCES dcim_site(id) ON DELETE CASCADE,
                            FOREIGN KEY (platform_id) REFERENCES dcim_platform(id) ON DELETE SET NULL,
                            FOREIGN KEY (role_id) REFERENCES dcim_device_role(id) ON DELETE SET NULL,
                            UNIQUE (name, site_id)
                        )
                    """)

                    # 3. Copy data (COALESCE guards any null NOT NULL timestamp).
                    cursor.execute("""
                        INSERT INTO dcim_device_new (
                            id, name, site_id, platform_id, role_id, status,
                            serial_number, asset_tag, primary_ip4, primary_ip6,
                            oob_ip, credential_id, ssh_port, legacy_ssh,
                            credential_tested_at, credential_test_result,
                            last_collected_at, last_collection_status,
                            collection_enabled, description, comments, netbox_id,
                            created_at, updated_at
                        )
                        SELECT
                            id, name, site_id, platform_id, role_id, status,
                            serial_number, asset_tag, primary_ip4, primary_ip6,
                            oob_ip, credential_id, ssh_port, legacy_ssh,
                            credential_tested_at, credential_test_result,
                            last_collected_at, last_collection_status,
                            collection_enabled, description, comments, netbox_id,
                            COALESCE(created_at, datetime('now')),
                            COALESCE(updated_at, datetime('now'))
                        FROM dcim_device
                    """)

                    # 4. Swap.
                    cursor.execute("DROP TABLE dcim_device")
                    cursor.execute(
                        "ALTER TABLE dcim_device_new RENAME TO dcim_device"
                    )

                    # 5. Restore the table's indexes, then every view, verbatim.
                    for (sql,) in index_ddls:
                        cursor.execute(sql)
                    for (sql,) in view_ddls:
                        cursor.execute(sql)

                    cursor.execute(
                        "INSERT INTO schema_version (version) VALUES (?)", (7,)
                    )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    cursor.execute("PRAGMA foreign_keys = ON")
                    raise

                cursor.execute("PRAGMA foreign_keys = ON")
                logger.info("Migration v6 → v7 complete")

            if current < 8:
                # v7 → v8: Add jobs.source for the definition registry.
                #
                # source distinguishes file-managed jobs (synced from
                # jobs.yaml by sync_definitions) from API/UI/seed-created jobs.
                # Existing rows stay NULL — sync only ever manages source='file'
                # rows, so this migration is inert until an operator adopts a
                # job into the registry (netlapse definitions adopt <slug>).
                logger.info("Migrating v7 → v8: adding source to jobs")
                cursor = self.conn.cursor()
                try:
                    cursor.execute("ALTER TABLE jobs ADD COLUMN source TEXT")
                except sqlite3.OperationalError:
                    pass  # column already exists
                cursor.executescript("""
                    DROP VIEW IF EXISTS v_job_summary;
                    CREATE VIEW v_job_summary AS
                    SELECT
                        j.id,
                        j.name,
                        j.slug,
                        j.description,
                        j.enabled,
                        j.capture_type,
                        j.commands,
                        j.command_map,
                        j.platform_filter,
                        j.site_filter,
                        j.role_filter,
                        j.device_filter,
                        j.status_filter,
                        j.interval_seconds,
                        j.last_run_at AS last_run,
                        j.next_run_at,
                        j.source,
                        j.created_at,
                        j.updated_at,
                        h.status AS last_status,
                        h.trigger AS last_trigger,
                        h.total_devices AS last_total,
                        h.success_count AS devices_collected,
                        h.failed_count AS devices_failed,
                        h.skipped_count AS devices_skipped,
                        ROUND(
                            (julianday(h.completed_at) - julianday(h.started_at)) * 86400, 1
                        ) AS duration_seconds,
                        h.completed_at AS last_completed,
                        h.id AS last_history_id
                    FROM jobs j
                    LEFT JOIN job_history h ON h.id = (
                        SELECT id FROM job_history
                        WHERE job_slug = j.slug
                        ORDER BY started_at DESC
                        LIMIT 1
                    );
                """)
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (8,)
                )
                self.conn.commit()
                logger.info("Migration v7 → v8 complete")

            if current < 9:
                # v8 → v9: Adopt the seeded default jobs into file management.
                #
                # v8 added jobs.source and left every existing row NULL, on the
                # assumption an operator would `definitions adopt` the defaults.
                # But the shipped default jobs share their slugs with jobs.yaml,
                # so while they sit at source=NULL sync treats them as hand-made
                # and skips all 8 as conflicts — an interval/command edit in the
                # YAML silently no-ops. The defaults ARE the file-managed jobs,
                # so stamp the known default slugs 'file' (only where still NULL;
                # an 'api' job that happens to share a slug is left untouched).
                #
                # Conservative by slug: a row a user genuinely hand-built via
                # the UI under a *different* slug is never touched. A user who
                # retuned a default job's interval in the UI will have that
                # value reclaimed by the file on the next sync — intended, since
                # the file is authoritative for these slugs.
                logger.info("Migrating v8 → v9: adopting seeded default jobs")
                cursor = self.conn.cursor()
                default_slugs = [row[1] for row in DEFAULT_JOBS]
                placeholders = ",".join("?" * len(default_slugs))
                cursor.execute(
                    f"UPDATE jobs SET source = 'file' "
                    f"WHERE source IS NULL AND slug IN ({placeholders})",
                    default_slugs,
                )
                adopted = cursor.rowcount
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (9,)
                )
                self.conn.commit()
                logger.info(
                    "Migration v8 → v9 complete (%d default job(s) now "
                    "file-managed)", adopted,
                )

            if current < 10:
                # v9 → v10: Correct the seeded-default provenance from 'file'
                # to 'seed'.
                #
                # v9 stamped the shipped default jobs 'file' so the catalog
                # could own them. That fixed catalog-present installs but broke
                # the catalog-ABSENT case: with no jobs.yaml on disk those rows
                # showed a 'file' badge, were read-only in the UI (pointing at a
                # file that doesn't exist), and — worse — would tombstone the
                # moment a *partial* jobs.yaml (one not listing them) was synced.
                #
                # 'seed' is the correct state: editable until a real binding
                # claims it, never tombstoned. Demote the default slugs back to
                # 'seed'; sync_definitions() runs immediately after this on
                # startup and re-promotes any slug the live catalog actually
                # defines (seed → file), so a genuine file-defined default ends
                # up 'file' again with no lost authority.
                logger.info("Migrating v9 → v10: default jobs 'file' → 'seed'")
                cursor = self.conn.cursor()
                default_slugs = [row[1] for row in DEFAULT_JOBS]
                placeholders = ",".join("?" * len(default_slugs))
                cursor.execute(
                    f"UPDATE jobs SET source = 'seed' "
                    f"WHERE source = 'file' AND slug IN ({placeholders})",
                    default_slugs,
                )
                demoted = cursor.rowcount
                cursor.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (10,)
                )
                self.conn.commit()
                logger.info(
                    "Migration v9 → v10 complete (%d default job(s) set to "
                    "'seed'; sync re-promotes any the catalog defines)", demoted,
                )

        return False

    # ------------------------------------------------------------------
    # Device lookups (used by API layer and storage layer)
    # ------------------------------------------------------------------

    def get_device_by_id(self, device_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a device by ID using the detail view."""
        row = self.conn.execute(
            "SELECT * FROM v_device_detail WHERE id = ?", (device_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_device_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a device by name. Used by Oxidized compat API
        (GET /node/fetch/:node) where we only have the node name.

        If multiple devices share a name across sites, returns the
        first active one found.
        """
        row = self.conn.execute(
            "SELECT * FROM v_device_detail WHERE name = ? "
            "ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END "
            "LIMIT 1",
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def get_device_by_name_and_site(
        self, name: str, site_slug: str
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a device by name + site slug. Used by Oxidized compat API
        (GET /node/fetch/:group/:node) where group = site_slug.
        """
        row = self.conn.execute(
            "SELECT * FROM v_device_detail "
            "WHERE name = ? AND site_slug = ?",
            (name, site_slug),
        ).fetchone()
        return dict(row) if row else None

    def get_device_by_ip(self, ip: str) -> Optional[Dict[str, Any]]:
        """Fetch a device by management IP. Useful for syslog triggers."""
        row = self.conn.execute(
            "SELECT * FROM v_device_detail "
            "WHERE primary_ip4 = ? OR primary_ip6 = ? OR oob_ip = ?",
            (ip, ip, ip),
        ).fetchone()
        return dict(row) if row else None

    def search_inventory(
        self, token: str, kind: str = "text"
    ) -> List[Dict[str, Any]]:
        """
        Identity lookup for One Search: which managed devices IS this token?

        This is the authoritative "what is this" half of a one-token search —
        distinct from search_all() over stored captures, which answers "where
        is it referenced". A hit here means the token names a device or is one
        of its addresses/identifiers in the inventory.

        Matching by token kind:
          ipv4 / ipv6  exact match (or CIDR-prefixed storage) on the three
                       address columns: primary_ip4, primary_ip6, oob_ip
          everything   case-insensitive substring on name, serial_number,
                       asset_tag, description

        Returns v_device_detail rows, each with an extra "matched_fields"
        list naming the columns that hit, so the UI can say why a device
        surfaced.
        """
        token = (token or "").strip()
        if not token:
            return []

        if kind in ("ipv4", "ipv6"):
            ip_cols = ("primary_ip4", "primary_ip6", "oob_ip")
            clauses, params = [], []
            for col in ip_cols:
                clauses.append(f"{col} = ? OR {col} LIKE ?")
                params.extend([token, f"{token}/%"])
            where = " OR ".join(f"({c})" for c in clauses)
            check_cols = ip_cols
        else:
            text_cols = ("name", "serial_number", "asset_tag", "description")
            like = f"%{token}%"
            clauses, params = [], []
            for col in text_cols:
                clauses.append(f"{col} LIKE ? COLLATE NOCASE")
                params.append(like)
            where = " OR ".join(clauses)
            check_cols = text_cols

        rows = self.conn.execute(
            f"SELECT * FROM v_device_detail WHERE {where} ORDER BY site_slug, name",
            params,
        ).fetchall()

        results: List[Dict[str, Any]] = []
        tok_lower = token.lower()
        for r in rows:
            d = dict(r)
            matched = []
            for col in check_cols:
                val = d.get(col)
                if not val:
                    continue
                sval = str(val)
                if kind in ("ipv4", "ipv6"):
                    if sval == token or sval.startswith(token + "/"):
                        matched.append(col)
                elif tok_lower in sval.lower():
                    matched.append(col)
            d["matched_fields"] = matched
            results.append(d)
        return results

    def list_devices(
        self,
        site: Optional[str] = None,
        platform: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        collection_enabled: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        List devices with optional filters.
        All filters match on slug fields.
        """
        query = "SELECT * FROM v_device_detail WHERE 1=1"
        params: list = []

        if site:
            query += " AND site_slug = ?"
            params.append(site)
        if platform:
            query += " AND platform_slug = ?"
            params.append(platform)
        if role:
            query += " AND role_slug = ?"
            params.append(role)
        if status:
            query += " AND status = ?"
            params.append(status)
        if collection_enabled is not None:
            query += " AND collection_enabled = ?"
            params.append(1 if collection_enabled else 0)

        query += " ORDER BY site_slug, name"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_platforms(self) -> List[Dict[str, Any]]:
        """List all platforms with device counts (from v_platform_summary)."""
        rows = self.conn.execute(
            "SELECT * FROM v_platform_summary ORDER BY slug"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_collection_targets(
        self,
        platform_filter: Optional[str] = None,
        site_filter: Optional[str] = None,
        role_filter: Optional[str] = None,
        device_filter: Optional[str] = None,
        status_filter: str = "active",
    ) -> List[Dict[str, Any]]:
        """
        List devices matching job filter criteria.
        Used by the scheduler to resolve which devices a job targets.

        Filters support comma-separated lists: "site1,site2" matches both.
        device_filter uses SQL LIKE: "border-%" matches border-rtr-01, border-rtr-02.
        """
        query = "SELECT * FROM v_device_detail WHERE collection_enabled = 1"
        params: list = []

        if status_filter:
            slugs = [s.strip() for s in status_filter.split(",")]
            placeholders = ",".join("?" * len(slugs))
            query += f" AND status IN ({placeholders})"
            params.extend(slugs)

        if site_filter:
            slugs = [s.strip() for s in site_filter.split(",")]
            placeholders = ",".join("?" * len(slugs))
            query += f" AND site_slug IN ({placeholders})"
            params.extend(slugs)

        if platform_filter:
            slugs = [s.strip() for s in platform_filter.split(",")]
            placeholders = ",".join("?" * len(slugs))
            query += f" AND platform_slug IN ({placeholders})"
            params.extend(slugs)

        if role_filter:
            slugs = [s.strip() for s in role_filter.split(",")]
            placeholders = ",".join("?" * len(slugs))
            query += f" AND role_slug IN ({placeholders})"
            params.extend(slugs)

        if device_filter:
            query += " AND name LIKE ?"
            params.append(device_filter)

        query += " ORDER BY site_slug, name"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Oxidized compat: node list
    # ------------------------------------------------------------------

    def list_nodes_oxidized(
        self, group: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Return device list in Oxidized node format.

        Maps:
          name       → device.name
          full_name  → device.name
          ip         → device.primary_ip4
          model      → platform.platform_profile (= Oxidized "model")
          group      → site.slug (= Oxidized "group")
          status     → "success" if last_collection_status = "success"
          time       → last_collected_at
        """
        devices = self.list_devices(
            site=group, collection_enabled=True
        )

        nodes = []
        for d in devices:
            status = "never"
            if d.get("last_collection_status") == "success":
                status = "success"
            elif d.get("last_collection_status"):
                status = "failure"

            nodes.append({
                "name": d["name"],
                "full_name": d["name"],
                "ip": d.get("primary_ip4") or "",
                "model": d.get("platform_profile") or "unknown",
                "group": d.get("site_slug") or "",
                "status": status,
                "time": d.get("last_collected_at"),
                "last": d.get("last_collection_status"),
            })

        return nodes

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def get_job(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE slug = ?", (slug,)
        ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM v_job_summary"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY slug"
        return [dict(r) for r in self.conn.execute(query).fetchall()]

    def list_job_capture_types(self) -> List[str]:
        """Distinct capture types across all configured jobs (the assignable
        set) — as opposed to storage.list_capture_types(), the collected set."""
        rows = self.conn.execute(
            "SELECT DISTINCT capture_type FROM jobs "
            "WHERE capture_type IS NOT NULL AND capture_type != '' "
            "ORDER BY capture_type"
        ).fetchall()
        return [r[0] for r in rows]

    def get_due_jobs(self, now_iso: str) -> List[Dict[str, Any]]:
        """
        Find jobs whose next_run_at has passed.
        Used by the scheduler to determine what to run.
        """
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE enabled = 1 "
            "AND (next_run_at IS NULL OR next_run_at <= ?)",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_device_collection_status(
        self,
        device_id: int,
        status: str,
        collected_at: str,
    ) -> None:
        """Update device after a collection attempt."""
        self.conn.execute(
            "UPDATE dcim_device SET last_collected_at = ?, "
            "last_collection_status = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (collected_at, status, device_id),
        )
        self.conn.commit()

    # Allowed fields for update_device — prevents SQL injection and
    # protects auto-managed fields (last_collected_at, etc.).
    _DEVICE_UPDATABLE_FIELDS = frozenset({
        "name", "site_id", "platform_id", "role_id", "status",
        "primary_ip4", "primary_ip6", "oob_ip",
        "credential_id", "ssh_port", "collection_enabled", "legacy_ssh",
        "serial_number", "asset_tag", "description", "comments",
    })

    def update_device(
        self, device_id: int, **fields
    ) -> Optional[Dict[str, Any]]:
        """
        Update a device's mutable fields.

        Only fields in _DEVICE_UPDATABLE_FIELDS are accepted.
        Collection state fields (last_collected_at, last_collection_status)
        are managed by the scheduler via update_device_collection_status().

        Args:
            device_id: Device ID to update.
            **fields: Column=value pairs to set.

        Returns:
            Updated device detail dict, or None if ID not found.

        Raises:
            ValueError: If an invalid field name is passed.
        """
        if not fields:
            return self.get_device_by_id(device_id)

        bad = set(fields.keys()) - self._DEVICE_UPDATABLE_FIELDS
        if bad:
            raise ValueError(
                f"Cannot update field(s): {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted(self._DEVICE_UPDATABLE_FIELDS))}"
            )

        # Normalize booleans/ints
        if "collection_enabled" in fields:
            fields["collection_enabled"] = (
                1 if fields["collection_enabled"] else 0
            )

        # Allow setting FK fields to null (e.g. credential_id = null)
        for fk in ("credential_id", "platform_id", "role_id"):
            if fk in fields and fields[fk] in (None, "", 0):
                fields[fk] = None

        # legacy_ssh: None = platform default, 0 = force off, 1 = force on
        if "legacy_ssh" in fields:
            val = fields["legacy_ssh"]
            if val is None or val == "":
                fields["legacy_ssh"] = None
            else:
                fields["legacy_ssh"] = 1 if val else 0

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.append(device_id)

        rows = self.conn.execute(
            f"UPDATE dcim_device SET {set_clause}, "
            "updated_at = datetime('now') WHERE id = ?",
            values,
        ).rowcount
        self.conn.commit()

        return self.get_device_by_id(device_id) if rows > 0 else None

    def create_device(self, name: str, site_id: int, **fields) -> Dict[str, Any]:
        """
        Create a single device by hand (the non-map path).

        Map import (map_importer.sync_map) remains the primary seeding
        mechanism. This exists for the handful of devices that need to be
        added manually, and — paired with delete_device — to correct what
        a discovery map produced.

        Required:
            name:    device hostname (unique within its site).
            site_id: FK to dcim_site (the table column is NOT NULL).

        Optional **fields: any column in _DEVICE_UPDATABLE_FIELDS
            (platform_id, role_id, status, primary_ip4, primary_ip6, oob_ip,
             credential_id, ssh_port, collection_enabled, legacy_ssh,
             serial_number, asset_tag, description, comments).

        Normalization mirrors update_device() so a device created here and a
        device edited there behave identically (FK blanks → NULL, booleans
        coerced, legacy_ssh tri-state preserved).

        Returns:
            The created device's v_device_detail row.

        Raises:
            ValueError: blank name, missing/unknown site, an unknown field,
                        or a uniqueness collision (name+site, asset_tag,
                        netbox_id).
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        if site_id in (None, "", 0):
            raise ValueError("site_id is required")
        site_id = int(site_id)

        bad = set(fields.keys()) - self._DEVICE_UPDATABLE_FIELDS
        if bad:
            raise ValueError(
                f"Cannot set field(s): {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted(self._DEVICE_UPDATABLE_FIELDS))}"
            )

        # Resolve site up front so the caller gets a clear message rather than
        # a raw FK failure.
        if self.conn.execute(
            "SELECT 1 FROM dcim_site WHERE id = ?", (site_id,)
        ).fetchone() is None:
            raise ValueError(f"site_id {site_id} does not exist")

        # --- normalize, identically to update_device ---
        if "collection_enabled" in fields:
            fields["collection_enabled"] = 1 if fields["collection_enabled"] else 0

        for fk in ("credential_id", "platform_id", "role_id"):
            if fk in fields and fields[fk] in (None, "", 0):
                fields[fk] = None

        if "legacy_ssh" in fields:
            val = fields["legacy_ssh"]
            fields["legacy_ssh"] = None if val in (None, "") else (1 if val else 0)

        # Blank optional text/IP → NULL. Critically, an empty-string asset_tag
        # would otherwise collide on the UNIQUE(asset_tag) constraint the
        # moment a second device is created without one.
        for txt in ("primary_ip4", "primary_ip6", "oob_ip",
                    "serial_number", "asset_tag", "description", "comments"):
            if txt in fields and (fields[txt] is None or str(fields[txt]).strip() == ""):
                fields[txt] = None

        cols = ["name", "site_id"] + list(fields.keys())
        vals = [name, site_id] + list(fields.values())
        placeholders = ", ".join("?" for _ in cols)

        try:
            cur = self.conn.execute(
                f"INSERT INTO dcim_device ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "name" in msg or "site_id" in msg:
                raise ValueError(
                    f"a device named '{name}' already exists at that site"
                )
            if "asset_tag" in msg:
                raise ValueError("asset_tag must be unique")
            if "netbox_id" in msg:
                raise ValueError("netbox_id must be unique")
            raise ValueError(f"could not create device: {e}")

        return self.get_device_by_id(cur.lastrowid)

    def delete_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        """
        Hard-delete a device's inventory row.

        Historical job results are preserved: job_device_results.device_id is
        ON DELETE SET NULL and the denormalized device_name column keeps those
        rows findable, exactly like delete_job() does for job history.

        This removes only the DCIM record. Stored captures on disk
        (<storage_base>/<site_slug>/<device>/) are NOT touched — deleting an
        inventory entry should not silently destroy collected history. Purge
        those separately if you want them gone.

        Note on re-seeding: if this device still appears in the discovery map,
        the next `sync-map` run will recreate it. See delete + map interaction
        notes in map_importer.

        Args:
            device_id: Device ID to delete.

        Returns:
            The deleted device's summary dict (id/name/site_slug) if a row was
            removed, else None.
        """
        existing = self.get_device_by_id(device_id)
        if existing is None:
            return None

        self.conn.execute(
            "DELETE FROM dcim_device WHERE id = ?", (device_id,)
        )
        self.conn.commit()

        return {
            "id": device_id,
            "name": existing.get("name"),
            "site_slug": existing.get("site_slug"),
        }

    # ══════════════════════════════════════════════════════════════════════
    # Reference data CRUD — sites, platforms, roles, manufacturers
    #
    # These four DCIM attribute tables share one shape (id / name / slug /
    # description / netbox_id / timestamps + a few table-specific columns) and
    # one set of operations. Rather than four bespoke CRUD implementations
    # that drift, a per-table descriptor drives generic create/update/delete.
    #
    # Until now these tables were populated ONLY as a side effect of the map
    # importer parsing slugs out of hostnames; there was no way to define one
    # by hand. That blocked manual device creation (create_device needs a
    # site_id, but no site could be minted without a map). This is that path.
    #
    # Delete is HARD-REFUSED while anything still references the row — even
    # where the FK is ON DELETE SET NULL (platform/role/manufacturer) — so a
    # delete can never silently strip a device's platform profile or orphan a
    # site's stored captures. Reassign first, then delete.
    # ══════════════════════════════════════════════════════════════════════

    # kind -> descriptor. `fields` are the settable columns beyond name/slug.
    # ref_table/ref_column identify who points at this row (for delete + slug
    # guards); ref_noun is for human-readable error messages.
    _REFERENCE_TABLES = {
        "site": {
            "table": "dcim_site",
            "fields": frozenset({
                "status", "description", "physical_address",
                "facility", "time_zone", "netbox_id",
            }),
            "ref_table": "dcim_device", "ref_column": "site_id",
            "ref_noun": "device",
            # slug is the git/storage top-level dir — immutable once captures
            # could exist under it (i.e. while any device references the site).
            "slug_locked_when_referenced": True,
        },
        "platform": {
            "table": "dcim_platform",
            "fields": frozenset({
                "manufacturer_id", "description", "platform_profile",
                "paging_disable_command", "prompt_regex", "enable_command",
                "legacy_ssh", "netbox_id",
            }),
            "ref_table": "dcim_device", "ref_column": "platform_id",
            "ref_noun": "device",
            "slug_locked_when_referenced": False,
        },
        "role": {
            "table": "dcim_device_role",
            "fields": frozenset({"color", "description", "netbox_id"}),
            "ref_table": "dcim_device", "ref_column": "role_id",
            "ref_noun": "device",
            "slug_locked_when_referenced": False,
        },
        "manufacturer": {
            "table": "dcim_manufacturer",
            "fields": frozenset({"description", "netbox_id"}),
            "ref_table": "dcim_platform", "ref_column": "manufacturer_id",
            "ref_noun": "platform",
            "slug_locked_when_referenced": False,
        },
    }

    @staticmethod
    def _slugify(value: str) -> str:
        """Normalize a name into a path/identifier-safe slug: [a-z0-9-]."""
        s = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
        return s.strip("-")

    def _ref_desc(self, kind: str) -> Dict[str, Any]:
        desc = self._REFERENCE_TABLES.get(kind)
        if desc is None:
            raise ValueError(
                f"unknown reference kind {kind!r}. "
                f"Valid: {', '.join(sorted(self._REFERENCE_TABLES))}"
            )
        return desc

    def _ref_count(self, kind: str, ref_id: int) -> int:
        """How many rows reference this attribute row."""
        d = self._ref_desc(kind)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {d['ref_table']} WHERE {d['ref_column']} = ?",
            (ref_id,),
        ).fetchone()[0]

    def _normalize_ref_fields(
        self, kind: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate field names and coerce blanks/booleans, per table."""
        d = self._ref_desc(kind)
        bad = set(fields) - d["fields"]
        if bad:
            raise ValueError(
                f"cannot set field(s) on {kind}: {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted(d['fields']))}"
            )

        out = dict(fields)
        # Nullable integer FKs / ids: blank/empty/0 -> NULL, else int.
        for ik in ("netbox_id", "manufacturer_id"):
            if ik in out:
                v = out[ik]
                out[ik] = None if v in (None, "", 0, "0") else int(v)
        # platform.legacy_ssh is a plain 0/1 flag (NOT the device tri-state).
        if "legacy_ssh" in out:
            v = out["legacy_ssh"]
            out["legacy_ssh"] = 1 if (v not in (None, "", 0, "0", False)) else 0
        # Blank text -> NULL so optional columns stay clean.
        for tk in ("description", "physical_address", "facility", "time_zone",
                   "platform_profile", "paging_disable_command",
                   "prompt_regex", "enable_command", "status", "color"):
            if tk in out and (out[tk] is None or str(out[tk]).strip() == ""):
                out[tk] = None
        return out

    @staticmethod
    def _ref_integrity_message(kind: str, name: str, err: Exception) -> str:
        msg = str(err).lower()
        if "name" in msg:
            return f"a {kind} named '{name}' already exists"
        if "slug" in msg:
            return f"that {kind} slug is already in use"
        if "netbox_id" in msg:
            return "netbox_id must be unique"
        return f"could not save {kind}: {err}"

    def list_reference(self, kind: str) -> List[Dict[str, Any]]:
        """List all rows of a reference table with a usage (reference) count."""
        d = self._ref_desc(kind)
        rows = self.conn.execute(
            f"SELECT t.*, ("
            f"  SELECT COUNT(*) FROM {d['ref_table']} r "
            f"  WHERE r.{d['ref_column']} = t.id"
            f") AS reference_count "
            f"FROM {d['table']} t ORDER BY t.name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_reference(self, kind: str, ref_id: int) -> Optional[Dict[str, Any]]:
        d = self._ref_desc(kind)
        row = self.conn.execute(
            f"SELECT * FROM {d['table']} WHERE id = ?", (ref_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_reference(
        self, kind: str, name: str,
        slug: Optional[str] = None, **fields,
    ) -> Dict[str, Any]:
        """
        Create a site / platform / role / manufacturer.

        slug is auto-derived from name when not supplied, normalized to
        [a-z0-9-] either way. Raises ValueError on a blank name, an unknown
        field, or a name/slug/netbox_id uniqueness collision.
        """
        d = self._ref_desc(kind)
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")

        slug = self._slugify(slug if slug else name)
        if not slug:
            raise ValueError(
                f"could not derive a valid slug from {name!r}; "
                f"provide an explicit slug ([a-z0-9-])"
            )

        fields = self._normalize_ref_fields(kind, fields)
        cols = ["name", "slug"] + list(fields.keys())
        vals = [name, slug] + list(fields.values())
        placeholders = ", ".join("?" for _ in cols)

        try:
            cur = self.conn.execute(
                f"INSERT INTO {d['table']} ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(self._ref_integrity_message(kind, name, e))

        return self.get_reference(kind, cur.lastrowid)

    def update_reference(
        self, kind: str, ref_id: int, **fields,
    ) -> Optional[Dict[str, Any]]:
        """
        Update a reference row. Returns the updated row, or None if not found.

        name and slug are handled specially: slug is re-normalized, and for a
        site it CANNOT change while devices reference it (the slug is the
        storage path — renaming it would orphan captured history).
        """
        d = self._ref_desc(kind)
        existing = self.get_reference(kind, ref_id)
        if existing is None:
            return None
        if not fields:
            return existing

        set_parts: Dict[str, Any] = {}

        if "name" in fields:
            nm = (fields.pop("name") or "").strip()
            if not nm:
                raise ValueError("name cannot be blank")
            set_parts["name"] = nm

        if "slug" in fields:
            new_slug = self._slugify(fields.pop("slug"))
            if not new_slug:
                raise ValueError("slug cannot be blank")
            if (
                new_slug != existing["slug"]
                and d["slug_locked_when_referenced"]
                and self._ref_count(kind, ref_id) > 0
            ):
                raise ValueError(
                    f"cannot change the {kind} slug while devices reference it "
                    f"— the slug is the storage path and renaming it would "
                    f"orphan captured history. Reassign or remove those "
                    f"devices first."
                )
            set_parts["slug"] = new_slug

        set_parts.update(self._normalize_ref_fields(kind, fields))
        if not set_parts:
            return existing

        clause = ", ".join(f"{k} = ?" for k in set_parts)
        values = list(set_parts.values()) + [ref_id]
        try:
            self.conn.execute(
                f"UPDATE {d['table']} SET {clause}, "
                f"updated_at = datetime('now') WHERE id = ?",
                values,
            )
            self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(
                self._ref_integrity_message(kind, set_parts.get("name", ""), e)
            )

        return self.get_reference(kind, ref_id)

    def delete_reference(
        self, kind: str, ref_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Delete a reference row — but HARD-REFUSE while anything references it.

        Returns the deleted row, or None if the id was not found. Raises
        ValueError (with the live count) when the row is still in use.
        """
        d = self._ref_desc(kind)
        existing = self.get_reference(kind, ref_id)
        if existing is None:
            return None

        count = self._ref_count(kind, ref_id)
        if count > 0:
            raise ValueError(
                f"cannot delete {kind} '{existing.get('name')}': "
                f"{count} {d['ref_noun']}(s) still reference it. "
                f"Reassign or remove them first."
            )

        self.conn.execute(f"DELETE FROM {d['table']} WHERE id = ?", (ref_id,))
        self.conn.commit()
        return existing

    # ── Bulk device reassignment ────────────────────────────────────────────
    # The companion to reference creation: once a real site/platform/role
    # exists, move the devices that landed in "unknown" (or the wrong bucket)
    # onto it in one call instead of one PATCH per device. Mirrors the vault's
    # bulk credential assignment.

    _BULK_ASSIGNABLE = frozenset({"site_id", "platform_id", "role_id"})

    def bulk_assign_device_attr(
        self, device_ids: List[int], field: str, value: Optional[int],
    ) -> Dict[str, Any]:
        """
        Set one FK attribute (site_id / platform_id / role_id) on many devices.

        site_id may not be set to NULL (the column is NOT NULL); platform_id
        and role_id may (NULL = unset). A non-NULL value is validated against
        its reference table so the caller gets a clear error, not a raw FK
        failure.

        Returns {"updated": <count>, "field": field, "value": value}.
        """
        if field not in self._BULK_ASSIGNABLE:
            raise ValueError(
                f"cannot bulk-assign {field!r}. "
                f"Allowed: {', '.join(sorted(self._BULK_ASSIGNABLE))}"
            )
        if not device_ids:
            return {"updated": 0, "field": field, "value": value}

        # Normalize + validate the target value.
        if value in (None, "", 0, "0"):
            if field == "site_id":
                raise ValueError("site_id cannot be null (every device needs a site)")
            value = None
        else:
            value = int(value)
            kind = {"site_id": "site", "platform_id": "platform",
                    "role_id": "role"}[field]
            if self.get_reference(kind, value) is None:
                raise ValueError(f"{kind} id {value} does not exist")

        ids = [int(i) for i in device_ids]
        placeholders = ", ".join("?" for _ in ids)
        cur = self.conn.execute(
            f"UPDATE dcim_device SET {field} = ?, updated_at = datetime('now') "
            f"WHERE id IN ({placeholders})",
            [value] + ids,
        )
        self.conn.commit()
        return {"updated": cur.rowcount, "field": field, "value": value}

    def list_credentials(self) -> List[Dict[str, Any]]:
        """
        List credentials (safe — no decrypted material).

        Returns id, name, username, and is_default for credential
        selection dropdowns.
        """
        rows = self.conn.execute(
            "SELECT id, name, username, is_default "
            "FROM credentials ORDER BY is_default DESC, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_roles(self) -> List[Dict[str, Any]]:
        """List device roles with device counts."""
        rows = self.conn.execute(
            "SELECT r.id, r.name, r.slug, r.color, "
            "COUNT(d.id) AS device_count "
            "FROM dcim_device_role r "
            "LEFT JOIN dcim_device d ON r.id = d.role_id "
            "GROUP BY r.id ORDER BY r.slug"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_job_schedule(
        self, slug: str, last_run: str, next_run: str
    ) -> None:
        """Update job timing after a run."""
        self.conn.execute(
            "UPDATE jobs SET last_run_at = ?, next_run_at = ?, "
            "updated_at = datetime('now') WHERE slug = ?",
            (last_run, next_run, slug),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Job history — written by the scheduler, read by API
    # ------------------------------------------------------------------

    def reap_running_jobs(self) -> int:
        """
        Mark orphaned 'running' job_history rows as 'interrupted'.

        A row can only be 'running' while the process that owns it is alive.
        If any 'running' rows exist at startup, they were left behind by a
        daemon that exited (restart, crash, kill) mid-collection — their
        completion was never recorded, so they would otherwise mask the last
        good run in v_job_summary forever (latest row by started_at wins).

        Call once at startup, before the scheduler begins polling.

        Returns:
            Number of stale rows reconciled.
        """
        from datetime import datetime as _dt
        now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = self.conn.execute(
            "UPDATE job_history SET status = 'interrupted', completed_at = ? "
            "WHERE status = 'running'",
            (now,),
        )
        self.conn.commit()
        count = cursor.rowcount or 0
        if count:
            logger.info("Reaped %d orphaned 'running' job run(s) → interrupted", count)
        return count

    def start_job_run(
        self,
        job_slug: str,
        trigger: str = "scheduled",
        total_devices: int = 0,
    ) -> int:
        """
        Record the start of a job run. Returns the history row ID.

        Args:
            job_slug: Job slug.
            trigger: What started it — scheduled, manual, api, syslog.
            total_devices: How many devices will be collected.

        Returns:
            job_history.id for subsequent updates.
        """
        from datetime import datetime as _dt

        job = self.get_job(job_slug)
        job_id = job["id"] if job else None
        now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        cursor = self.conn.execute(
            "INSERT INTO job_history "
            "(job_id, job_slug, trigger, started_at, total_devices, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (job_id, job_slug, trigger, now, total_devices),
        )
        self.conn.commit()
        return cursor.lastrowid

    def complete_job_run(
        self,
        history_id: int,
        status: str = "completed",
        success_count: int = 0,
        failed_count: int = 0,
        skipped_count: int = 0,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Finalize a job run with results.

        Args:
            history_id: Row ID from start_job_run().
            status: completed, failed, cancelled.
            success_count: Devices collected successfully.
            failed_count: Devices that errored.
            skipped_count: Devices skipped.
            error_message: Top-level error if the whole job failed.
        """
        from datetime import datetime as _dt

        now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "UPDATE job_history SET "
            "completed_at = ?, status = ?, "
            "success_count = ?, failed_count = ?, skipped_count = ?, "
            "error_message = ? "
            "WHERE id = ?",
            (now, status, success_count, failed_count,
             skipped_count, error_message, history_id),
        )
        self.conn.commit()

    def insert_device_result(
        self,
        history_id: int,
        device_id: Optional[int],
        device_name: str,
        status: str,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        version_id: Optional[str] = None,
    ) -> int:
        """
        Record a per-device result within a job run.

        Args:
            history_id: Parent job_history.id.
            device_id: DCIM device ID (nullable if device was deleted).
            device_name: Device hostname for display.
            status: success, failed, timeout, auth_error, skipped.
            error_category: From executor error categorization.
            error_message: Error detail string.
            duration_seconds: How long this device took.
            version_id: Git SHA or file timestamp from storage.

        Returns:
            job_device_results.id.
        """
        from datetime import datetime as _dt

        now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = self.conn.execute(
            "INSERT INTO job_device_results "
            "(job_history_id, device_id, device_name, status, "
            "error_category, error_message, duration_seconds, "
            "version_id, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (history_id, device_id, device_name, status,
             error_category, error_message, duration_seconds,
             version_id, now, now),
        )
        self.conn.commit()
        return cursor.lastrowid

    def list_job_history(
        self,
        job_slug: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """
        List job run history, newest first.

        Args:
            job_slug: Filter to a specific job (None = all).
            limit: Maximum rows to return.

        Returns:
            List of job_history rows as dicts.
        """
        query = "SELECT * FROM job_history"
        params: list = []

        if job_slug:
            query += " WHERE job_slug = ?"
            params.append(job_slug)

        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_job_run(self, history_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a single job run with its device-level results.

        Returns:
            Dict with job_history fields + 'device_results' list,
            or None if not found.
        """
        row = self.conn.execute(
            "SELECT * FROM job_history WHERE id = ?", (history_id,)
        ).fetchone()

        if not row:
            return None

        result = dict(row)

        # Attach per-device results
        devices = self.conn.execute(
            "SELECT * FROM job_device_results "
            "WHERE job_history_id = ? ORDER BY started_at",
            (history_id,),
        ).fetchall()
        result["device_results"] = [dict(d) for d in devices]

        return result

    # ------------------------------------------------------------------
    # Job CRUD — configurable jobs via API/UI
    # ------------------------------------------------------------------

    def create_job(
        self,
        name: str,
        slug: str,
        capture_type: str,
        commands: str,
        interval_seconds: int = 3600,
        description: Optional[str] = None,
        enabled: bool = True,
        platform_filter: Optional[str] = None,
        site_filter: Optional[str] = None,
        role_filter: Optional[str] = None,
        device_filter: Optional[str] = None,
        status_filter: str = "active",
        command_map: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Create a new collection job.

        Args:
            name: Human-readable name.
            slug: URL-safe identifier (unique).
            capture_type: config, arp, bgp, interfaces, routes, etc.
            commands: JSON array string of CLI commands (default/fallback).
            interval_seconds: How often to run (default 1 hour).
            description: Optional description.
            enabled: Start enabled (default True).
            platform_filter: Comma-separated platform slugs.
            site_filter: Comma-separated site slugs.
            role_filter: Comma-separated role slugs.
            device_filter: Device name pattern (SQL LIKE).
            status_filter: Device status filter (default 'active').
            command_map: JSON object mapping platform_slug → command arrays.

        Returns:
            The created job as a dict.

        Raises:
            sqlite3.IntegrityError: If slug already exists.
        """
        self.conn.execute(
            "INSERT INTO jobs "
            "(name, slug, capture_type, commands, command_map, interval_seconds, "
            "description, enabled, platform_filter, site_filter, "
            "role_filter, device_filter, status_filter, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, slug, capture_type, commands, command_map, interval_seconds,
             description, 1 if enabled else 0, platform_filter,
             site_filter, role_filter, device_filter, status_filter, source),
        )
        self.conn.commit()
        return self.get_job(slug)

    # Allowed fields for update_job — prevents SQL injection and
    # protects schedule fields (managed by scheduler).
    _JOB_UPDATABLE_FIELDS = frozenset({
        "name", "description", "enabled", "capture_type", "commands",
        "command_map", "platform_filter", "site_filter", "role_filter",
        "device_filter", "status_filter", "interval_seconds", "source",
    })

    def update_job(self, slug: str, **fields) -> Optional[Dict[str, Any]]:
        """
        Update a job's mutable fields.

        Only fields in _JOB_UPDATABLE_FIELDS are accepted.
        Schedule fields (last_run_at, next_run_at) are managed by
        the scheduler via update_job_schedule().

        Args:
            slug: Job slug to update.
            **fields: Column=value pairs to set.

        Returns:
            Updated job dict, or None if slug not found.

        Raises:
            ValueError: If an invalid field name is passed.
        """
        if not fields:
            return self.get_job(slug)

        bad = set(fields.keys()) - self._JOB_UPDATABLE_FIELDS
        if bad:
            raise ValueError(
                f"Cannot update field(s): {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted(self._JOB_UPDATABLE_FIELDS))}"
            )

        # Normalize enabled to int
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        values.append(slug)

        rows = self.conn.execute(
            f"UPDATE jobs SET {set_clause}, "
            "updated_at = datetime('now') WHERE slug = ?",
            values,
        ).rowcount
        self.conn.commit()

        return self.get_job(slug) if rows > 0 else None

    def delete_job(self, slug: str) -> bool:
        """
        Delete a job definition.

        History rows are preserved (job_id SET NULL via FK) so past
        runs remain queryable. The job_slug column on job_history
        keeps them findable.

        Args:
            slug: Job slug to delete.

        Returns:
            True if a job was deleted, False if not found.
        """
        rows = self.conn.execute(
            "DELETE FROM jobs WHERE slug = ?", (slug,)
        ).rowcount
        self.conn.commit()
        return rows > 0

    def set_job_enabled(self, slug: str, enabled: bool) -> bool:
        """
        Enable or disable a job. Convenience wrapper.

        Args:
            slug: Job slug.
            enabled: True to enable, False to disable.

        Returns:
            True if updated, False if slug not found.
        """
        rows = self.conn.execute(
            "UPDATE jobs SET enabled = ?, updated_at = datetime('now') "
            "WHERE slug = ?",
            (1 if enabled else 0, slug),
        ).rowcount
        self.conn.commit()
        return rows > 0

    # ------------------------------------------------------------------
    # Seed data
    # ------------------------------------------------------------------

    def _seed_defaults(self, cursor: sqlite3.Cursor):
        """Insert default manufacturers, platforms, roles, and jobs."""
        # Manufacturers
        cursor.executemany(
            "INSERT OR IGNORE INTO dcim_manufacturer (name, slug, description) "
            "VALUES (?, ?, ?)",
            DEFAULT_MANUFACTURERS,
        )

        # Map manufacturer slugs to IDs
        cursor.execute("SELECT slug, id FROM dcim_manufacturer")
        mfg_map = {row[0]: row[1] for row in cursor.fetchall()}

        # Platforms
        for name, slug, mfg_slug, profile, paging, enable, legacy in DEFAULT_PLATFORMS:
            mfg_id = mfg_map.get(mfg_slug)
            cursor.execute(
                "INSERT OR IGNORE INTO dcim_platform "
                "(name, slug, manufacturer_id, platform_profile, "
                "paging_disable_command, enable_command, legacy_ssh) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, slug, mfg_id, profile, paging, enable, legacy),
            )

        # Roles
        cursor.executemany(
            "INSERT OR IGNORE INTO dcim_device_role (name, slug, color) "
            "VALUES (?, ?, ?)",
            DEFAULT_DEVICE_ROLES,
        )

        # Default jobs.
        #
        # Seeded as source='seed' — a distinct provenance from both 'file'
        # (catalog-owned) and NULL/'api' (hand-made). A seed is a shipped
        # default that NO file owns yet:
        #   * UI/API treat it as a normal editable job (the read-only lock and
        #     'file' badge key on source=='file', so seeds are freely editable).
        #   * sync_definitions() ADOPTS it (promotes seed→file) the moment a
        #     jobs.yaml binding with the same slug appears, so an interval/
        #     command edit in the catalog takes effect.
        #   * the tombstone pass skips it (only 'file' jobs tombstone), so the
        #     baseline defaults can't be silently disabled by a partial catalog.
        # (Seeding 'file' would lock all 8 to a jobs.yaml that may not exist and
        # expose them to tombstoning; seeding NULL would make sync skip them as
        # conflicts so catalog edits no-op. 'seed' is the only correct state.)
        for name, slug, cap_type, commands, command_map, interval in DEFAULT_JOBS:
            cursor.execute(
                "INSERT OR IGNORE INTO jobs "
                "(name, slug, capture_type, commands, command_map, "
                "interval_seconds, source) "
                "VALUES (?, ?, ?, ?, ?, ?, 'seed')",
                (name, slug, cap_type, commands, command_map, interval),
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def init_database(db_path: Optional[Path] = None) -> NetlapseDB:
    """Convenience: initialize and return a database instance."""
    db = NetlapseDB(db_path)
    db.init_schema()
    return db