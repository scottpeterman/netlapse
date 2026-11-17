# Netlapse Configuration Reference

Every setting Netlapse reads, where it comes from, and what happens when it
is absent.

Configuration lives in one YAML file plus a small set of environment
variables. Secrets are never read from the YAML — credentials live in the
vault, and the few passwords the daemon needs at startup come from the
environment.

- **Config file:** `~/.netlapse/config.yaml`
- **Override location:** `$NETLAPSE_CONFIG`
- **Reload:** none. Changes require a daemon restart.
- **Starter file:** generated from `netlapse/config_template.py`, which is
  also what the Settings editor shows when no file exists yet.

Every key is optional. A missing section falls back to the defaults listed
below, and a fresh install runs with no edits at all.

> **A note on empty keys.** A key that is present but has no value
> (`max_workers:` with nothing after it) parses to `None` in YAML, not to
> the default. Most of the codebase coalesces with `or`, so `None` lands on
> the documented default — but a few settings take an explicit `None` test
> because zero is meaningful. Those are called out where they occur. When in
> doubt, delete the key rather than blanking it.

---

## Environment variables

| Variable | Purpose |
|---|---|
| `NETLAPSE_CONFIG` | Path to `config.yaml`. Default `~/.netlapse/config.yaml`. Also determines where definitions are looked for — see `definitions_dir`. |
| `NETLAPSE_VAULT_PASSWORD` | Unlocks the credential vault headlessly. **Without it the scheduler will not start**; the daemon logs a warning and serves the UI with collection disabled. |
| `NETLAPSE_ADMIN_PASSWORD` | Resets the local `admin` account password at startup. Only usable when a local provider is in the auth chain — see the break-glass note under `auth`. |
| `NETLAPSE_LDAP_BIND_PASSWORD` | Conventional name for the LDAP service-account password. The actual variable read is whatever `auth.ldap.bind_password_env` names. |
| `NETLAPSE_NONINTERACTIVE` | Suppresses interactive prompts. Also implied when stdin is not a TTY. Set it for containers, systemd units, and CI. |
| `NETLAPSE_SKIP_TEMPLATE_DOWNLOAD` | Skips fetching the TextFSM template database on first run. Useful for air-gapped installs where the DB is staged by hand. |

---

## `listen` — HTTP listener

```yaml
listen:
  host: 0.0.0.0
  port: 8888
```

| Key | Default | Notes |
|---|---|---|
| `host` | `0.0.0.0` | Bind address. `127.0.0.1` restricts to loopback. |
| `port` | `8888` | TCP port for the web UI and both REST APIs. |

`0.0.0.0` is a bind address, not a destination. Browse to `127.0.0.1:8888`
or the host's real address — Chrome 128+ blocks connections to `0.0.0.0`
outright, which surfaces as a working page whose WebSocket never connects.

---

## `storage` — where captured state is versioned

```yaml
storage:
  backend: file
  path: ~/.netlapse/data
  max_versions: 50
```

| Key | Default | Notes |
|---|---|---|
| `backend` | `file` | `file` (rotating snapshots) or `git` (commit per change). |
| `path` | `~/.netlapse/data` | Root of the `{site}/{device}/` tree. |
| `max_versions` | `50` | File backend only. Snapshots retained per device per capture type before the oldest are pruned. |

Layout under `path`:

```
{site}/{device}/{capture}.txt      current raw
{site}/{device}/{capture}.json     current parsed
{site}/{device}/_history/{capture}.{timestamp}.txt
```

A capture whose raw text is byte-identical to the stored copy is skipped —
no rotation, no new version. This is why a device with a static config shows
a single snapshot and cannot be diffed until something actually changes.

---

## `dcim_db` — device inventory

```yaml
dcim_db: ~/.netlapse/netlapse.db
```

SQLite database holding devices, sites, roles, platforms, and schedules.
Runtime state lives here; capture *definitions* do not — see
`definitions_dir`.

---

## `definitions_dir` — capture and job catalog

```yaml
# definitions_dir: ~/.netlapse
```

| Key | Default | Notes |
|---|---|---|
| `definitions_dir` | directory containing `config.yaml` | Where `captures.yaml`, `jobs.yaml`, `roles.yaml` and `volatility.yaml` are read from. |

Resolution order is `definitions_dir` if set, otherwise the parent directory
of `$NETLAPSE_CONFIG`. These files are authoritative for what Netlapse
collects; adding a vendor, a command, or a whole capture type is an edit here
plus a resync, not a code change.

---

## `scheduler` — background collection

```yaml
scheduler:
  poll_interval: 15
  max_workers: 2
  parse_timeout: 10
```

| Key | Default | Notes |
|---|---|---|
| `poll_interval` | `15` | Seconds between sweeps for due devices. |
| `max_workers` | `2` | Concurrent device collections. |
| `parse_timeout` | `10` | Seconds before a parse subprocess is killed. |

Parsing runs in a subprocess specifically so a pathological TextFSM template
cannot hang the collector. A kill is logged as `PARSE KILLED after timeout —
regex backtracking`; the raw capture is still stored, just without parsed
JSON. Repeated kills on the same platform indicate a template problem, not a
timeout that needs raising.

---

## `collection` — per-device timeouts

```yaml
collection:
  connect_timeout: 90
  probe_timeout: 15
  expect_prompt_timeout: 3000
  inter_command_time: 1.0
  shell_timeout: 5.0
  echo_grace: 1.5
  hard_deadline_multiplier: 20
```

| Key | Default | Unit | Notes |
|---|---|---|---|
| `connect_timeout` | `90` | s | TCP connect, SSH negotiation, and auth. |
| `probe_timeout` | `15` | s | Reachability/auth check only; runs no commands. |
| `expect_prompt_timeout` | `3000` | ms | Idle window between received chunks. |
| `inter_command_time` | `1.0` | s | Pause after each command. |
| `shell_timeout` | `5.0` | s | Shell setup, and the fallback read when no prompt was detected. |
| `echo_grace` | `1.5` | s | How long to wait for the command echo before releasing the echo anchor. |
| `hard_deadline_multiplier` | `20` | — | Absolute read cap = idle window x this, floored at 60s. |

**`expect_prompt_timeout` is a cap on silence, not on total read time.** It
resets on every chunk received, so a large config streams for as long as
bytes keep flowing. It has to exceed the longest quiet stretch the device
produces mid-command — a router pausing after `Building configuration...` is
the usual one.

Set it too low and the read terminates early on a prompt left in the channel
from a previous command. The resulting capture is short but *structurally
complete*, so it stores cleanly, compares as unchanged against the previous
copy, and reports success at every layer. Nothing surfaces the problem except
a diff that never appears. If a capture shrinks by more than a quarter in one
cycle the store logs `possible truncated read` — treat that as the tell.

`inter_command_time` accepts `0` to disable the pause entirely. It is the one
key here where a literal zero is honoured rather than treated as unset.

`echo_grace` is the safety valve on the anchor that keeps a stale prompt from
terminating a read early. Lowering it tightens that protection; raising it
accommodates devices that echo slowly. Devices that do not echo at all need
it non-zero or the prompt is never matched.

---

## `parser` — structured parsing (tfsm-fire)

```yaml
parser:
  db_path: ~/.netlapse/tfsm_templates.db
  min_score: 15.0
```

| Key | Default | Notes |
|---|---|---|
| `db_path` | `~/.netlapse/tfsm_templates.db` | TextFSM template database. |
| `min_score` | `15.0` | Minimum match score for a parse to be accepted. |

Optional. With no template DB, Netlapse stores raw captures and skips parsed
JSON — text diffs still work, semantic diffs do not.

Scoring picks the best-fitting template for a given output, which means an
unstructured capture (a running config) can still score against some show-
command template and produce records that look plausible but mean nothing.
Semantic diff is therefore restricted to capture types with a defined record
identity; configs use the line-aligned config-aware diff instead. Raising
`min_score` reduces spurious matches at the cost of unparsed captures.

---

## `credentials` — fallback chain

```yaml
credentials:
  fallback_order: []
```

| Key | Default | Notes |
|---|---|---|
| `fallback_order` | `[]` | Ordered list of **vault credential names**. |

Consulted only on authentication failure. The device's own credential is
tried first, then each entry in order. This lets one fleet carry gear with
old passwords without per-device overrides.

```yaml
credentials:
  fallback_order:
    - legacy-tacacs
    - break-glass-local
```

---

## `jump_hosts` and `proxy_rules` — bastion routing

```yaml
jump_hosts: {}
proxy_rules: []
```

Both are top-level, since proxy routing is a global topology concern rather
than a per-job one.

### `jump_hosts`

A named registry. Each `credential` is a **vault credential name** —
bastion passwords are never stored here in the clear.

```yaml
jump_hosts:
  lab-bastion:
    host: 10.42.0.10
    port: 22
    credential: bastion-lab
  dmz-jump:
    host: 198.51.100.5
    credential: bastion-dmz
```

| Key | Default | Notes |
|---|---|---|
| `host` | — | Required. |
| `port` | `22` | |
| `credential` | — | Vault credential name, not a password. |

### `proxy_rules`

Evaluated top to bottom, **first match wins**. Within a rule every present
match key must match (AND). A rule with no match keys is a catch-all. No
matching rule means a direct connection.

```yaml
proxy_rules:
  - match: {devices: [lab-bastion-1]}
    jump: direct
  - match: {devices: ["fw*-dmz", lb01-dmz]}
    jump: dmz-jump
  - match: {site: [lab-east, lab-west]}
    jump: lab-bastion
```

Match keys, each optional and each accepting a scalar or a list:

| Key | Matches on |
|---|---|
| `site` | Site slug |
| `role` | Device role |
| `platform` | Platform slug |
| `devices` | Device name, exact or fnmatch glob |
| `name_regex` | `re.search` against the device name |

`jump: direct` forces a direct connection — needed for the bastion itself,
which would otherwise match a later catch-all and try to proxy through
itself.

---

## `emulation` — replay recorded output

```yaml
# emulation:
#   enabled: false
#   lookup_path: ~/.netlapse/emulation/lookup.json
#   bind_host: 127.0.0.1
```

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `lookup_path` | searched in several locations | Maps device IPs to mock listener ports. |
| `bind_host` | `127.0.0.1` | Where the mock listeners are bound. |

Redirects SSH connections to local mock devices serving recorded CLI output,
so collection logic can be exercised without touching real gear. When
enabled, a device with no lookup entry logs `[EMULATION] UNRESOLVED` and
fails to connect rather than silently reaching the real address.

Collection logs record whether each connection was emulated. If captures look
stale, confirm this section is off before suspecting the collector.

---

## `auth` — authentication and authorization

```yaml
auth:
  provider: local
  session_ttl: 604800
  cookie_secure: false
  basic_auth: false
  api_tokens: []
```

| Key | Default | Notes |
|---|---|---|
| `provider` | `local` | `local`, `ldap`, or `ldap+local`. |
| `session_ttl` | `604800` (7 days) | Session cookie lifetime in seconds. |
| `cookie_secure` | `false` | Set `true` when serving over HTTPS. |
| `basic_auth` | `false` | Enables HTTP Basic as a fallback, for consumers that can only store URL-embedded credentials. |
| `api_tokens` | `[]` | Bearer tokens for machine access. A valid token grants **admin**. |

### Providers

- **`local`** — scrypt-hashed accounts stored in the database.
- **`ldap`** — directory only. Users are provisioned as local shadow rows on
  first successful login.
- **`ldap+local`** — the directory is tried first; if it does not return a
  successful result, the local provider is tried next.

> **What "falls back" means.** The chain advances on **any** unsuccessful
> directory result — not only when the directory is unreachable. A wrong
> password, an unknown user, and a rejection by the `allowed_groups` gate all
> fall through to the local provider.
>
> For directory users that Netlapse provisioned this is harmless: shadow rows
> are created with an unusable random password, so they can never satisfy the
> local provider. The case to be aware of is a **pre-existing local account
> whose username matches a directory user**. If that user is rejected by the
> group gate, the local account is still tried, and a known local password will
> log them in at the local row's role. Group-gate denial is therefore not a hard
> deny under `ldap+local` unless the matching local account is disabled or absent.
>
> Under pure `ldap` there is no second provider and the denial is final.

### Role model under LDAP

Role is **directory-authoritative**. Group membership is re-evaluated on every
login and the shadow row's role is overwritten to match, so editing an LDAP
user's role in the UI will not stick. Grant admin by adding the user to an
`admin_groups` group.

Disabling a user in the UI *is* a durable local override and is never
automatically re-enabled.

> **Break-glass caveat.** `NETLAPSE_ADMIN_PASSWORD` resets the *local* admin
> account, which only has a login path when a local provider is in the chain.
> Under pure `ldap` it cannot be used to get back in. To recover a locked-out
> instance: set `provider` to `local` or `ldap+local`, set the environment
> variable, restart, log in as `admin`. Startup logs a loud warning if the
> variable is set while LDAP-only.

### `auth.ldap`

```yaml
auth:
  provider: ldap+local
  ldap:
    servers:
      - ldaps://dc1.lab.example.com
      - ldaps://dc2.lab.example.com
    use_ssl: true
    # start_tls: true          # instead of use_ssl, for in-band upgrade on 389
    validate: true
    ca_certs_file: /etc/ssl/certs/lab-ca.pem
    bind_dn: "CN=svc-netlapse,OU=Service,DC=lab,DC=example,DC=com"
    bind_password_env: NETLAPSE_LDAP_BIND_PASSWORD
    user_search_base: "OU=Users,DC=lab,DC=example,DC=com"
    user_filter: "(&(objectClass=user)(sAMAccountName={username}))"
    allowed_groups:
      - "CN=netlapse-users,OU=Groups,DC=lab,DC=example,DC=com"
    admin_groups:
      - "CN=netlapse-admins,OU=Groups,DC=lab,DC=example,DC=com"
    default_role: viewer
    timeout: 8
```

| Key | Default | Notes |
|---|---|---|
| `servers` | — | One or more URIs, tried in order for failover. `server` (singular) is accepted as an alias for a single entry. |
| `port` | — | Overrides the port implied by the URI scheme. Left unset, 389 or 636 is chosen by scheme. |
| `use_ssl` | `true` | LDAPS — the socket is TLS from the first byte. |
| `start_tls` | `false` | Upgrade a cleartext connection in-band, before the bind. **Mutually exclusive with `use_ssl`** — setting both raises a config error at startup. Setting neither logs a cleartext warning. |
| `validate` | `true` | Verify the server certificate. Keep enabled outside a lab. |
| `ca_certs_file` | — | CA that signed the directory certificate. |
| `bind_dn` | `""` | Service account used to locate the user before the per-user bind. |
| `bind_password_env` | — | **Name of an environment variable**, not the password. |
| `bind_password` | — | Inline password. Works, but logs a warning recommending `bind_password_env`. |
| `user_search_base` | `""` | |
| `user_filter` | `(&(objectClass=user)(sAMAccountName={username}))` | `{username}` is filter-escaped before use. |
| `username_attr` | `sAMAccountName` | Attribute read back as the canonical username for the shadow row. |
| `display_name_attr` | `displayName` | Attribute used for the display name. |
| `group_attr` | `memberOf` | Attribute read for group membership. |
| `allowed_groups` | `[]` | Any-of gate. Full DN or bare CN. **Empty means every directory user is admitted** — see below. |
| `admin_groups` | `[]` | Elevates to admin. Full DN or bare CN. |
| `default_role` | `viewer` | For an allowed user not in an admin group. |
| `timeout` | `8` | Seconds. |

### Transport

`use_ssl` and `start_tls` describe two different things and only one may be set.
LDAPS (`use_ssl: true`, `ldaps://`, port 636) negotiates TLS before any LDAP
message is exchanged. StartTLS (`start_tls: true`, `ldap://`, port 389) opens in
cleartext and upgrades in-band, before the bind. Setting both is rejected at
startup rather than silently resolved in favour of one.

A StartTLS negotiation that fails — refused by the directory, not offered at
all, or a certificate that does not validate — **fails the login**. It does not
fall back to a cleartext bind. The failure surfaces as `directory unavailable`,
the same as any other transport-level problem, and the specific cause is logged.

Setting neither is legitimate on a trusted segment but means the service-account
password and every user password cross the network in the clear; a warning is
logged at startup so this is never the setting nobody noticed.

`validate: true` is the default and is the right setting anywhere the
directory certificate chains to a real CA. In a lab with a self-signed DC
certificate, point `ca_certs_file` at the signing CA rather than disabling
validation — but `validate: false` exists for the case where that is not
practical.

### Authorization gotchas

Three behaviors that are deliberate, tested, and easy to get wrong:

**An empty `allowed_groups` is an open gate, not a closed one.** With no entries
the group check is skipped entirely and every user who binds successfully is
admitted at `default_role`. This makes the directory usable before groups exist,
but it means an unset `allowed_groups` grants access to the whole directory. Set
it explicitly for any instance that is not a lab.

**A bare group token matches the CN in any OU.** `allowed_groups: [netlapse-admins]`
matches a group with that CN anywhere in the tree, not only the one you meant.
Use a full DN when two OUs can carry the same CN.

**Group membership is direct only.** `memberOf` is not transitive in Active
Directory, so a user who belongs to a group that is *itself* a member of an
`admin_groups` group reads as not a member and receives `default_role`. Nest
your groups one level deep for Netlapse's purposes, or name the groups your
users are directly in. (Resolving the chain would require an
`LDAP_MATCHING_RULE_IN_CHAIN` search; it is not currently performed.)

**A `user_filter` that matches more than one object** uses the first entry
returned, with no warning. Write filters that can only match one user.

### `auth.ldap.mock`

```yaml
auth:
  ldap:
    mock:
      enabled: true
      users:
        - { username: alice, password: alice-pw, groups: [netlapse-users] }
        - { username: bob,   password: bob-pw,   groups: [netlapse-users, netlapse-admins] }
```

Simulates the directory in memory so the real login page can be exercised
with no domain controller. With no `users` block it seeds `admin/admin`
(admin), `user/user` (viewer), and `denied/denied` (authenticates but is in
no allowed group). `groups_base` overrides where the simulated groups live;
it defaults to a `groups` OU alongside `user_search_base`.

Mock mode simulates the *transport* only — the service bind, the search, the
per-user bind, the group gate, and shadow-row provisioning all run exactly as
they do against a real directory. Transport settings (`use_ssl`, `start_tls`,
`validate`) are inert here and are not validated.

Never enable outside development. It logs a loud warning while active.

### How a login is evaluated

1. **Reject empty input.** A username or password that is empty fails before
   any network call — a bind with a DN and an empty password is treated as an
   anonymous bind by many directories and would otherwise succeed.
2. **Service bind** using `bind_dn` and the resolved bind password.
3. **Search** under `user_search_base` with `user_filter`, with the supplied
   username filter-escaped. No match is an invalid-credentials failure.
4. **Proxy bind** as the located DN with the supplied password. This is the
   actual credential check; the service account never validates the password.
5. **Authorize** — any-of `allowed_groups`, then `admin_groups` for the admin
   role, otherwise `default_role`.
6. **Sync the shadow row** — create it if absent (with an unusable local
   password), update its role to match current membership, and deny if an
   administrator has locally disabled it.

A directory that cannot be reached, or a transport that cannot be established,
returns `directory unavailable` rather than an invalid-credentials error, so an
outage is distinguishable from a bad password in the logs.

### Oxidized-compatible public access

```yaml
auth:
  oxidized_public: false
  oxidized_public_allow_ips: []
  oxidized_public_trigger_ips: []
```

| Key | Default | Notes |
|---|---|---|
| `oxidized_public` | `false` | Master switch for anonymous read access to the Oxidized-compatible routes. |
| `oxidized_public_allow_ips` | `[]` | Source-IP fence for reads. Empty means no restriction beyond the route set. |
| `oxidized_public_trigger_ips` | `[]` | Source IPs additionally permitted to trigger a collection. Empty means nobody. |

Exists for consumers that can only store a bare URL with no credentials —
LibreNMS being the case it was built for. Reads only, restricted to the
Oxidized route set.

The collection trigger is gated separately and more narrowly: reading configs
and poking production devices are different grants, so an IP listed in
`oxidized_public_allow_ips` still cannot trigger collection unless it also
appears in `oxidized_public_trigger_ips`.

Authentication is evaluated in tiers — public Oxidized read, then Oxidized
trigger, then API token, then session cookie or HTTP Basic. A request
declining one tier simply moves to the next, so a normal browser session
passes through the Oxidized gates on its way to the cookie check. This is
expected and is not logged.

---

## Worked example: minimum viable lab install

```yaml
listen:
  host: 127.0.0.1
  port: 8888

storage:
  backend: file
  path: ~/.netlapse/data

dcim_db: ~/.netlapse/netlapse.db

scheduler:
  poll_interval: 15
  max_workers: 2
```

With `NETLAPSE_VAULT_PASSWORD` exported, that is enough to collect. Every
other section adds capability rather than being required.

---

## Verifying what is actually in effect

Since nothing hot-reloads, the values in the file and the values in the
running daemon can diverge after an edit. Startup logs the resolved
collection timeouts:

```
Collection timeouts: connect=90s idle=3000ms inter_cmd=1.0s echo_grace=1.5s
```

Storage backend, listen address, and auth provider are logged at startup as
well. When a setting appears to have no effect, confirm the daemon restarted
before assuming the key is wrong — and confirm the file being read is the one
being edited, since `$NETLAPSE_CONFIG` moves the whole resolution chain,
definitions directory included.