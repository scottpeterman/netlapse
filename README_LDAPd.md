# ldapd — an in-process LDAP server for end-to-end auth tests

A real TCP listener speaking real LDAPv3, small enough to start inside a test
and rude enough to be useful. Python 3.10 or later, standard library only
(`cryptography` is needed only for the TLS tests).

## Why not a mock strategy

A mock connection strategy validates the *flow* — service bind, locate, proxy
bind, group gate — but never touches a socket. Everything below is therefore
unreachable by it, and every item is something that fails in production:

- TLS handshake, CA trust, expired certificates, hostname mismatch
- `ServerPool` failover across DCs
- `connect_timeout` / `receive_timeout`
- referrals, size limits, mid-operation disconnects
- server-side result codes and diagnostic strings
- what the server **parsed** from a filter, as opposed to what the client
  intended to send

## Why not Samba

Use Samba too. It is a better directory and it is the right check for "does
this work against a real AD." What it cannot do is misbehave on command. This
fixture exists for the failure surface, not the happy path.

## Quick start

```python
from ldapd import ThreadedLdapServer
from ldap3 import Server, Connection, NONE

with ThreadedLdapServer() as srv:
    conn = Connection(Server(srv.host, port=srv.port, get_info=NONE),
                      user="cn=svc-directory,dc=netlab,dc=internal",
                      password="svc-pw")
    conn.bind()
```

`Directory.lab()` seeds:

| account | password | purpose |
|---|---|---|
| `svc-directory` | `svc-pw` | service account for the initial bind |
| `admin-user` | `admin-pw` | member of `lab-admins` |
| `plain-user` | `user-pw` | member of `lab-users` only |
| `nested-user` | `nested-pw` | in `lab-contractors`, which is a member of `lab-admins` — direct `memberOf` does **not** show `lab-admins` |
| `outside-user` | `out-pw` | authenticates, in no lab group |

Build your own with `Directory()` / `.add()` / `.add_user()` / `.add_group()`
and pass it as `directory=`.

## Pointing the running app at it

Start the server in the foreground:

```
python -m netlapse.testing.ldapd --port 3389 -v
```

`-v` logs every bind and search at DEBUG, which is how you watch a login
happen. One successful login looks like this:

```
DEBUG  bind    dn='cn=svc-directory,dc=netlab,dc=internal' password=<supplied>
DEBUG  bind    -> 0 success
DEBUG  search  base='' scope=0 filter=(objectClass=*) ...      <- root DSE probe
DEBUG  search  -> done code=0 entries=1
DEBUG  search  base='ou=users,...' scope=2 filter=(&(objectClass=user)(sAMAccountName=admin-user))
DEBUG  search  -> cn=admin-user,ou=users,dc=netlab,dc=internal
DEBUG  search  -> done code=0 entries=1
DEBUG  bind    dn='cn=admin-user,ou=users,dc=netlab,dc=internal' password=<supplied>
DEBUG  bind    -> 0 success
DEBUG  search  base='' scope=0 filter=(objectClass=*) ...      <- and again
```

The two root-DSE probes bracketing the real work are the `get_info` cost,
visible per login. The filter shown is the one the server *parsed*, not the one
the client meant to send.

Add `--ldaps` or `--start-tls` to exercise the TLS paths; either one generates
a throwaway CA and prints its path.

Then add the `auth` block to `~/.netlapse/config.yaml`:

```yaml
auth:
  api_tokens:
    - "..."                       # keep whatever is already here
  provider: ldap+local
  ldap:
    servers:
      - ldap://127.0.0.1:3389
    use_ssl: false
    validate: false
    bind_dn: "cn=svc-directory,dc=netlab,dc=internal"
    bind_password_env: NETLAPSE_LDAP_BIND_PASSWORD
    user_search_base: "ou=users,dc=netlab,dc=internal"
    user_filter: "(&(objectClass=user)(sAMAccountName={username}))"
    allowed_groups:
      - "cn=lab-users,ou=groups,dc=netlab,dc=internal"
    admin_groups:
      - "cn=lab-admins,ou=groups,dc=netlab,dc=internal"
    default_role: viewer
    timeout: 5
```

```
export NETLAPSE_LDAP_BIND_PASSWORD=svc-pw
```

Restart the daemon and log in at `/login` as `admin-user` / `admin-pw` for an
admin session, or `plain-user` / `user-pw` for a viewer.

**One `auth:` key, not two.** The shipped config has a live `auth:` block
holding `api_tokens` and a second, commented-out `auth:` block documenting the
LDAP options. Uncommenting the second one gives the file two top-level `auth:`
keys, and YAML silently keeps only the last — the API tokens disappear with no
error and no warning. Merge the LDAP settings into the existing block, as
above.

Leave `mock:` out entirely. The provider resolves its connection factory as
explicit > mock > real, so a lingering `mock: {enabled: true}` quietly wins and
you will be testing the in-memory directory again without noticing.

## Pointing the tests at it

`test_auth_e2e.py` is a working example: it builds a config dict from the
fixture's ephemeral port and hands it to the real `LdapAuthProvider`. No
`connection_factory` seam, no stubbing below the config line.

```python
@pytest.fixture
def provider(ldapd, store):
    return LdapAuthProvider({
        "servers": [f"ldap://{ldapd.host}:{ldapd.port}"],
        "use_ssl": False,
        "bind_dn": "cn=svc-directory,dc=netlab,dc=internal",
        "bind_password": "svc-pw",
        "user_search_base": "ou=users,dc=netlab,dc=internal",
        "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
        "allowed_groups": ["cn=lab-users,ou=groups,dc=netlab,dc=internal"],
        "admin_groups": ["cn=lab-admins,ou=groups,dc=netlab,dc=internal"],
        "default_role": "viewer",
    }, store=store)
```

Suggested layout:

```
netlapse/testing/__init__.py
netlapse/testing/ldapd.py
tests/test_ldapd.py        # the fixture's own tests
tests/test_auth_e2e.py     # the provider, over a socket
```


## Set `get_info=NONE`

Left at its default, a client fetches the root DSE **and the full schema**
during `bind()`. Measured against this fixture, one successful login costs
three searches with `get_info=ALL` and one with `get_info=NONE` — the two extra
are root-DSE probes, one per connection, since a service bind and a proxy bind
are two connections. Against a real DC it is worse: the schema fetch lands on
top, on every connection, and a schema-fetch failure fails the bind.

## Faults

All runtime-mutable, all inert by default, so a test declares only the failure
it is asserting against.

```python
srv.faults.fail_user_bind("533")   # AD: account disabled
srv.faults.hang(search=30)         # stall before SearchResultDone
srv.faults.duplicate_entries = 2   # two entries for one username
srv.faults.flood_entries = 5000    # blow past a size limit
srv.faults.referral_uris = ["ldap://dc2.netlab.internal/dc=netlab,dc=internal"]
srv.faults.drop_mid_search = True  # close after the first entry
srv.faults.service_bind_fails = True
srv.faults.allow_empty_password = False
srv.faults.reject_start_tls = True
srv.reset()                        # clear faults and recorded traffic
```

`fail_user_bind` takes any documented AD sub-code — `525`, `52e`, `530`, `531`,
`532`, `533`, `701`, `773`, `775`. All of them arrive as plain
`invalidCredentials(49)`; only the diagnostic string distinguishes a locked
account from a typo.

## The injection oracle

The server records every filter it parsed off the wire:

```python
assert srv.received_filter_nodes[-1] == (
    "and", [("eq", "objectClass", "user"),
            ("eq", "sAMAccountName", "admin-user)(objectClass=*")])
```

The payload landing inside a single assertion *value* — rather than adding a
branch to the tree — is the proof that escaping held. `srv.received_filters`
has the same thing rendered back to RFC 4515 text for assertion messages.

## TLS

```python
from ldapd import make_certificate, server_ssl_context, ThreadedLdapServer

ca, cert, key = make_certificate(tmpdir, common_name="127.0.0.1")
srv = ThreadedLdapServer(ssl_context=server_ssl_context(cert, key))       # LDAPS
srv = ThreadedLdapServer(start_tls_context=server_ssl_context(cert, key)) # StartTLS
```

`days=-1` produces an already-expired certificate; a `common_name` that does
not match the listener exercises hostname verification. Verification stays a
client-side choice — the fixture never forces it, because a fixture that
required TLS could not test what happens without it.

## Wiring it into provider tests

Point the config at the fixture and let the real code path run:

```python
@pytest.fixture
def ldap_config(ldapd):
    return {
        "servers": [f"ldap://{ldapd.host}:{ldapd.port}"],
        "use_ssl": False,
        "bind_dn": "cn=svc-directory,dc=netlab,dc=internal",
        "bind_password": "svc-pw",
        "user_search_base": "ou=users,dc=netlab,dc=internal",
        "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
        "allowed_groups": ["lab-users"],
        "admin_groups": ["lab-admins"],
    }
```

No `connection_factory` seam required — the provider builds its own real
connections, which is the whole point.

## Scope

Implemented: simple bind, unbind, abandon, search (base/one/sub with
and/or/not/equality/presence/substring/ordering/extensible filters), StartTLS,
LDAPS.

Not implemented: SASL, add/modify/delete, paged-results controls, access
control. This is a test fixture, not a directory server.

## Tests

```
python -m pytest test_ldapd.py -q      # 36 tests, ~2s
```

The StartTLS upgrade uses `StreamWriter.start_tls` on 3.11+ and drops to
`loop.start_tls` on 3.10; both paths are covered.