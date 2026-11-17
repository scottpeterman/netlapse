"""
Netlapse default config template.

Single source of truth for the *documented* starter ``config.yaml``. This is
what the Settings editor pre-fills when no config file exists yet, so saving
from the UI writes a fully-commented file the operator can grow into.

Design notes:
  * The template MUST parse to a non-empty mapping (``update_config`` rejects
    anything that isn't a YAML dict), so the load-bearing core keys are left
    live while the optional/advanced sections ship commented out.
  * Defaults here mirror the programmatic fallbacks in ``app.load_config`` and
    the per-subsystem factories (``storage.create_backend``,
    ``Scheduler.__init__``, ``ParseEngine``). Keep them in sync.
"""

# The leading column of every line is intentional — this is emitted verbatim.
DEFAULT_CONFIG_TEMPLATE = """\
# ─────────────────────────────────────────────────────────────────────────────
# Netlapse configuration
#
# Location : ~/.netlapse/config.yaml  (override with $NETLAPSE_CONFIG)
# Reload   : changes are NOT hot-reloaded — restart the daemon to apply.
# Secrets  : credentials and bastion passwords live in the vault, never here.
#            Unlock headlessly with $NETLAPSE_VAULT_PASSWORD.
#
# Every key below is shown with its built-in default. Live keys are the
# minimum needed to run; optional sections are commented out — uncomment and
# edit the ones you need.
# ─────────────────────────────────────────────────────────────────────────────

# HTTP listener for the web UI + REST API (Oxidized-compatible and native).
listen:
  host: 0.0.0.0          # bind address; 127.0.0.1 to restrict to localhost
  port: 8888             # TCP port

# Where captured device state is versioned and stored.
storage:
  backend: file          # "file" (rotating JSON) or "git" (commit-per-change)
  path: ~/.netlapse/data # root directory for stored snapshots
  # max_versions: 50     # file backend only — snapshots retained per device/type
  # git:                 # git backend only
  #   commit_trailers: true   # append device/job metadata as commit trailers

# DCIM (device inventory) SQLite database — devices, sites, roles, schedules.
dcim_db: ~/.netlapse/netlapse.db

# Background collection scheduler. Polls due devices and runs captures.
scheduler:
  poll_interval: 15      # seconds between schedule sweeps
  max_workers: 2         # concurrent device collections
  adhoc_workers: 2       # separate pool for device-page "Collect Now"
  parse_timeout: 10      # seconds before a subprocess parse is killed

# Timeouts governing a single device collection. Optional — omitting the
# section uses every default shown below.
#
# expect_prompt_timeout is a cap on SILENCE, not on total read time: it
# resets on every chunk received, so a large config streams for as long as
# bytes keep flowing. It must exceed the longest mid-command quiet stretch
# the device produces (Cisco's pause after "Building configuration..." is
# the usual one). Set it too low and the read ends early, returning a
# capture that is short but structurally complete — which then compares
# clean against the stored copy and is recorded as no change.
collection:
  connect_timeout: 90           # s   TCP + SSH negotiation + auth
  probe_timeout: 15             # s   reachability check only
  expect_prompt_timeout: 3000   # ms  idle window between received chunks
  inter_command_time: 1.0       # s   pause after each command (0 to disable)
  shell_timeout: 5.0            # s   shell setup / no-prompt fallback read
  echo_grace: 1.5               # s   wait for the command echo before the
                                #     anchor releases and the prompt may match
                                #     anywhere. Lower is safer against a stale
                                #     prompt; too low breaks non-echoing gear.
  hard_deadline_multiplier: 20  #     absolute cap = idle window x this, min 60s

# Structured parsing engine (tfsm-fire). Optional — if the template DB is
# absent, Netlapse still stores raw captures, just without parsed JSON diffs.
parser:
  db_path: ~/.netlapse/tfsm_templates.db   # TextFSM template database
  min_score: 15.0        # minimum template match score to accept a parse

# ─────────────────────────────────────────────────────────────────────────────
# CONNECTIVITY — credential fallback + jump hosts.
#
# The three keys below are shipped live with empty defaults: as written they
# are inert (identical to omitting them), so a fresh config boots clean. Fill
# them in — or copy from the worked examples beneath each — to enable.
# ─────────────────────────────────────────────────────────────────────────────

# Credential fallback chain (vault credential names). Used ONLY on auth
# failure: the per-device default is tried first, then each of these in order.
# Lets a single fleet carry legacy gear with old passwords without per-device
# overrides. Empty list = no fallback.
credentials:
  fallback_order: []
  # Example:
  # fallback_order:
  #   - legacy-tacacs
  #   - break-glass-local

# Jump hosts (bastions / ProxyJump). A named registry of jump hosts; each
# "credential" is a VAULT credential name — bastion passwords are NEVER stored
# here in the clear. Empty mapping = no jump hosts defined.
jump_hosts: {}
  # Example:
  # jump_hosts:
  #   site-bastion:
  #     host: 10.0.1.10
  #     port: 22
  #     credential: bastion-site1    # vault credential name
  #   dmz-jump:
  #     host: 198.51.100.5
  #     credential: bastion-dmz

# Proxy rules associate devices with a jump host. Evaluated top-to-bottom,
# FIRST MATCH WINS. Within a rule every present match key must match (AND);
# a rule with no match keys is a catch-all. Use "jump: direct" to force a
# direct connection (e.g. for the bastion itself). No matching rule = direct.
# Empty list = every device connects directly.
#
# Match keys (each optional, scalar or list): site, role, platform,
# devices (name, exact or fnmatch glob), name_regex (re.search).
proxy_rules: []
  # Example:
  # proxy_rules:
  #   - match: {devices: [bastion-site1]}        # the bastion itself
  #     jump: direct
  #   - match: {devices: ["fw*-dmz", lb01-dmz]} # DMZ gear uses its own jump
  #     jump: dmz-jump
  #   - match: {site: [site1, site2]}             # everything else at these sites
  #     jump: site-bastion

# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL SECTIONS — uncomment to enable.
# ─────────────────────────────────────────────────────────────────────────────

# Backup archives (Settings → Backup & Restore, `netlapse backup`).
# Default directory is <config dir>/backups.
#
# backup:
#   dir: ~/.netlapse/backups

# Device emulation — replay recorded CLI output instead of touching real gear.
# Useful for lab/dev. Binds a local listener that answers as the device IPs
# found in the lookup file.
#
# emulation:
#   enabled: false
#   lookup_path: ~/.netlapse/emulation/lookup.json
#   bind_host: 127.0.0.1

# Authentication. provider: local (default) | ldap | ldap+local.
#   local      — scrypt-hashed accounts in app_users (the built-in behavior).
#   ldap       — directory only; users are provisioned as local shadow rows on
#                first successful login, role mapped from group membership.
#   ldap+local — try the directory first, fall back to local accounts (handles
#                break-glass logins when the directory is unreachable).
#
# Role model (ldap): role is DIRECTORY-AUTHORITATIVE. Group membership is
# re-evaluated on every login and the shadow row's role is overwritten to
# match — editing an LDAP user's role in the UI will not stick. Grant admin by
# adding the user to an admin_groups group. (Disabling a user in the UI IS a
# durable local override and is never auto-re-enabled.)
#
# BREAK-GLASS CAVEAT: the NETLAPSE_ADMIN_PASSWORD env var resets the LOCAL
# admin account, which only has a login path when a local provider is in the
# chain. In pure 'ldap' mode that reset cannot be used to log in. To recover a
# locked-out instance, set provider to 'local' (or 'ldap+local') AND set
# NETLAPSE_ADMIN_PASSWORD, restart, then log in as admin. (Startup logs a loud
# warning if the env var is set while LDAP-only.)
#
# auth:
#   provider: ldap+local
#   ldap:
#     servers:                       # one or more DCs; tried in order, failover
#       - ldaps://dc1.corp.example.com
#       - ldaps://dc2.corp.example.com
#     use_ssl: true                  # ldaps; set false only for plain 389
#     validate: true                 # verify the server cert (keep true)
#     ca_certs_file: /etc/ssl/certs/corp-ca.pem   # CA that signed the DC cert
#     # Service account used to locate the user before the per-user bind.
#     bind_dn: "CN=svc-netlapse,OU=Service,DC=corp,DC=example,DC=com"
#     bind_password_env: NETLAPSE_LDAP_BIND_PASSWORD   # read from env, not file
#     # Where/how to find the user. {username} is filter-escaped before use.
#     user_search_base: "OU=Users,DC=corp,DC=example,DC=com"
#     user_filter: "(&(objectClass=user)(sAMAccountName={username}))"
#     # Authorization. allowed_groups is an any-of gate; admin_groups elevates
#     # to the admin role. Entries may be a full group DN or a bare CN.
#     allowed_groups:
#       - "CN=netlapse-users,OU=Groups,DC=corp,DC=example,DC=com"
#     admin_groups:
#       - "CN=netlapse-admins,OU=Groups,DC=corp,DC=example,DC=com"
#     default_role: viewer           # role for an allowed user not in admin_groups
#     timeout: 8
#     # Mock mode — simulate the directory IN MEMORY for dev/testing, so you
#     # can boot Netlapse and log in via the real /login page with no DC/VM.
#     # NEVER enable in production (it logs a loud warning when active).
#     # With no `users:` block it seeds: admin/admin (admin), user/user
#     # (viewer), denied/denied (authenticates but in no allowed group).
#     # mock:
#     #   enabled: true
#     #   users:
#     #     - { username: alice, password: alice-pw, groups: [netlapse-users] }
#     #     - { username: bob,   password: bob-pw,   groups: [netlapse-users, netlapse-admins] }

# Read-only API tokens — for the MCP server and other scripts. Viewer role,
# limited to inventory, capture metadata, filtered parsed records, pattern-
# gated raw lines, compact diffs, search and change feeds. No whole-capture
# downloads, no admin surfaces. Collect Now only where allow_collect is true.
# Generate one with:  python -m netlapse token --name mcp --collect
#
# auth:
#   readonly_api_tokens:
#     - name: mcp
#       token: "<generated>"
#       allow_collect: true
"""


def default_config_yaml() -> str:
    """Return the documented starter config.yaml as a string."""
    return DEFAULT_CONFIG_TEMPLATE


__all__ = ["DEFAULT_CONFIG_TEMPLATE", "default_config_yaml"]