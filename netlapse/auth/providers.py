"""
Authentication providers — the seam that lets the credential *source* change
without touching session handling, routes, or the UI.

``LocalAuthProvider`` validates against the hashed passwords in ``app_users``.
``LdapAuthProvider`` binds against a directory (Active Directory or any
RFC-4511 LDAP server) and provisions a *local shadow row* in ``app_users`` so
the rest of the system — which keys off ``app_users`` — keeps working
unchanged. Both honor the same ``authenticate`` contract, so the manager can
run them alone or chained (``ldap+local``).
"""

import logging
import secrets
from abc import ABC, abstractmethod
from typing import Callable, List, Optional

from netlapse.auth.hashing import needs_rehash, verify_password, hash_password

logger = logging.getLogger(__name__)


class AuthResult:
    """Outcome of an authentication attempt."""

    __slots__ = ("ok", "user", "reason")

    def __init__(self, ok: bool, user: Optional[dict] = None, reason: str = ""):
        self.ok = ok
        self.user = user
        self.reason = reason

    @classmethod
    def success(cls, user: dict) -> "AuthResult":
        return cls(True, user=user)

    @classmethod
    def failure(cls, reason: str) -> "AuthResult":
        return cls(False, reason=reason)


class AuthProvider(ABC):
    """A source of authentication decisions."""

    name = "base"

    @abstractmethod
    def authenticate(self, username: str, password: str) -> AuthResult:
        """Return an AuthResult. Must not raise on bad credentials."""
        raise NotImplementedError


class LocalAuthProvider(AuthProvider):
    """Validate against scrypt hashes in app_users."""

    name = "local"

    def __init__(self, store):
        self.store = store

    def authenticate(self, username: str, password: str) -> AuthResult:
        user = self.store.get_by_username(username)
        if user is None:
            # Spend a hash anyway to blunt username enumeration via timing.
            verify_password(password, hash_password("x"))
            return AuthResult.failure("invalid credentials")
        if not user.get("is_active"):
            return AuthResult.failure("account disabled")
        if not verify_password(password, user["password_hash"]):
            return AuthResult.failure("invalid credentials")
        # Transparent parameter upgrade on successful login.
        if needs_rehash(user["password_hash"]):
            try:
                self.store.update_password_hash(
                    user["id"], hash_password(password)
                )
            except Exception:
                logger.debug("rehash-on-login failed for %s", username)
        return AuthResult.success(user)


# ─────────────────────────────────────────────────────────────────────────────
# LDAP / Active Directory
# ─────────────────────────────────────────────────────────────────────────────

# ldap3 is an optional dependency: only required when provider includes 'ldap'.
# Import lazily so a local-only install needs no LDAP stack.
try:
    import ldap3
    from ldap3 import Server, ServerPool, Connection, Tls, ALL, SUBTREE
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.core.exceptions import LDAPException
    _HAVE_LDAP3 = True
except Exception:  # pragma: no cover - exercised only without ldap3 installed
    _HAVE_LDAP3 = False


def _norm_group(g: str) -> str:
    """
    Normalize a group token for comparison.

    Full DNs compare whole and case-insensitively. A bare token (no '=') is
    treated as a CN and matched against the CN component of each memberOf DN,
    so config can say ``netlapse-admins`` instead of the full DN.
    """
    return (g or "").strip().lower()


def _cn_of(dn: str) -> str:
    """Extract the first RDN value of a DN (the CN for a typical group DN)."""
    first = (dn or "").split(",", 1)[0]
    return first.split("=", 1)[1].strip().lower() if "=" in first else first.lower()


def _dc_suffix(dn: str) -> str:
    """Return just the dc=... tail of a DN (e.g. 'dc=corp,dc=example,dc=com')."""
    parts = [p.strip() for p in (dn or "").split(",")]
    dcs = [p for p in parts if p.lower().startswith("dc=")]
    return ",".join(dcs) if dcs else (dn or "")


def _swap_first_ou(dn: str, new_ou: str) -> Optional[str]:
    """Replace a leading ``ou=<x>`` RDN with ``ou=<new_ou>``; None if absent."""
    parts = [p.strip() for p in (dn or "").split(",")]
    if parts and parts[0].lower().startswith("ou="):
        parts[0] = f"ou={new_ou}"
        return ",".join(parts)
    return None


# Default seed used when mock mode is enabled with no explicit users. Lets you
# boot straight into a working login: admin/admin (admin role), user/user
# (viewer role), denied/denied (authenticates but in no allowed group).
_DEMO_USERS = [
    {"username": "admin", "password": "admin", "display": "Mock Admin",
     "groups": ["netlapse-users", "netlapse-admins"]},
    {"username": "user", "password": "user", "display": "Mock Viewer",
     "groups": ["netlapse-users"]},
    {"username": "denied", "password": "denied", "display": "Mock Outsider",
     "groups": ["some-other-team"]},
]


class LdapAuthProvider(AuthProvider):
    """
    Directory-backed auth using the service-bind -> locate -> proxy-bind pattern.

    Flow per login:
      1. Bind as the service account and search for the user (sAMAccountName /
         UPN by default) to resolve their DN and read group membership.
      2. Re-bind ("proxy bind") as the user's DN with the supplied password.
         A successful bind *is* the authentication.
      3. Require membership in at least one ``allowed_groups`` entry (an
         any-of gate). ``admin_groups`` membership elevates the mapped role to
         admin; everyone else gets ``default_role``.
      4. Provision or refresh a local shadow row in ``app_users`` (random
         unusable password hash, so a directory user can never authenticate via
         the local provider even in ``ldap+local`` mode) and return that row.

    Never raises on bad credentials *or* on directory failure — a failure
    returns ``AuthResult.failure`` so an ``ldap+local`` chain can fall through
    to the local provider instead of 500-ing the login route.

    ``connection_factory`` is the test seam: it defaults to real ldap3
    connections built from config, but a test can inject a factory that returns
    ``MOCK_SYNC`` connections backed by an in-memory directory. Nothing else in
    the class changes between production and test.
    """

    name = "ldap"

    def __init__(
        self,
        config: dict,
        store=None,
        connection_factory: Optional[Callable[[str, str], "Connection"]] = None,
    ):
        self.config = config or {}
        self.store = store
        self._explicit_factory = connection_factory

        c = self.config
        self.servers: List[str] = c.get("servers") or (
            [c["server"]] if c.get("server") else []
        )
        self.use_ssl = bool(c.get("use_ssl", True))
        self.start_tls = bool(c.get("start_tls", False))
        self.ca_certs_file = c.get("ca_certs_file")
        self.validate_cert = bool(c.get("validate", True))
        self.port = c.get("port")  # None -> ldap3 picks 389/636 by scheme
        self.timeout = int(c.get("timeout", 8))

        self.bind_dn = c.get("bind_dn", "")
        self.bind_password = self._resolve_bind_password(c)

        self.user_search_base = c.get("user_search_base", "")
        self.user_filter = c.get(
            "user_filter", "(&(objectClass=user)(sAMAccountName={username}))"
        )
        self.group_attr = c.get("group_attr", "memberOf")
        self.username_attr = c.get("username_attr", "sAMAccountName")
        self.display_name_attr = c.get("display_name_attr", "displayName")

        self.allowed_groups = [_norm_group(g) for g in (c.get("allowed_groups") or [])]
        self.admin_groups = [_norm_group(g) for g in (c.get("admin_groups") or [])]
        self.default_role = c.get("default_role", "viewer")

        # Mock mode — a simulated in-memory directory so the whole app can be
        # exercised (boot Netlapse, log in via the real /login page) with no DC.
        self.mock_cfg = c.get("mock") or {}
        self.mock_enabled = bool(self.mock_cfg.get("enabled"))

        self._validate_transport()

        # Resolve the connection factory exactly once, now that config is read:
        #   explicit (tests) > mock (config) > real ldap3 (production).
        if self._explicit_factory is not None:
            self._factory = self._explicit_factory
        elif self.mock_enabled:
            self._factory = self._build_mock_factory()
        else:
            self._factory = self._default_factory

        if not _HAVE_LDAP3 and self._explicit_factory is None:
            logger.warning(
                "auth.provider includes 'ldap' but the ldap3 package is not "
                "installed — LDAP logins will fail. `pip install ldap3`."
            )

    # -- config helpers ---------------------------------------------------
    @staticmethod
    def _resolve_bind_password(c: dict) -> str:
        """Prefer an env var (bind_password_env); fall back to inline."""
        import os
        env_key = c.get("bind_password_env")
        if env_key:
            val = os.environ.get(env_key)
            if val:
                return val
            logger.warning("bind_password_env %s is unset/empty", env_key)
        if c.get("bind_password"):
            logger.warning(
                "ldap.bind_password is set inline in config — prefer "
                "bind_password_env to keep the secret out of config.yaml."
            )
            return c["bind_password"]
        return ""

    def _validate_transport(self) -> None:
        """
        Check the use_ssl / start_tls combination once, at construction.

        The two are mutually exclusive: LDAPS wraps the socket in TLS from the
        first byte, StartTLS upgrades a cleartext connection in-band. Asking for
        both is a config error, not something to silently resolve in favour of
        one of them. Neither is cleartext, which is legitimate on a trusted
        segment but must not be the setting nobody noticed they were running.
        """
        if self.mock_enabled or self._explicit_factory is not None:
            return  # no real socket; the transport settings are inert
        if self.use_ssl and self.start_tls:
            raise ValueError(
                "auth.ldap: use_ssl and start_tls are mutually exclusive — "
                "use_ssl wraps the socket in TLS (ldaps://, 636), start_tls "
                "upgrades a cleartext connection in-band (ldap://, 389). "
                "Set exactly one."
            )
        if not self.use_ssl and not self.start_tls:
            logger.warning(
                "auth.ldap: neither use_ssl nor start_tls is set — the service "
                "bind password and every user password will cross the network "
                "in CLEARTEXT. Set use_ssl: true (ldaps) or start_tls: true."
            )

    def _build_tls(self):
        if not self.use_ssl and not self.start_tls:
            return None
        import ssl
        validate = ssl.CERT_REQUIRED if self.validate_cert else ssl.CERT_NONE
        return Tls(ca_certs_file=self.ca_certs_file, validate=validate)

    def _build_server_pool(self):
        tls = self._build_tls()
        servers = []
        for host in self.servers:
            servers.append(
                Server(
                    host,
                    port=self.port,
                    use_ssl=self.use_ssl,
                    tls=tls,
                    get_info=ALL,
                    connect_timeout=self.timeout,
                )
            )
        if not servers:
            raise RuntimeError("no LDAP servers configured")
        # First-available with failover across DCs.
        return ServerPool(servers, pool_strategy="FIRST", active=True, exhaust=True)

    def _default_factory(self, user: str, password: str) -> "Connection":
        """Production connection factory: real ldap3 Connection (not yet bound)."""
        pool = self._build_server_pool()
        conn = Connection(
            pool,
            user=user or None,
            password=password or None,
            auto_bind=False,
            receive_timeout=self.timeout,
            raise_exceptions=False,
        )
        # StartTLS must be negotiated BEFORE the bind, or the credentials this
        # connection carries go out in cleartext. Building the Tls object in
        # _build_tls() only makes the upgrade *possible* — ldap3 does not
        # initiate it on its own. Failure is fatal by design: a directory that
        # won't upgrade must not be silently fallen back to plaintext, so this
        # raises and authenticate() reports "directory unavailable".
        if self.start_tls:
            if not conn.start_tls():
                raise RuntimeError(
                    f"StartTLS negotiation failed, refusing to bind in "
                    f"cleartext: {conn.result}"
                )
        return conn

    # -- mock directory (config-driven, no server) ------------------------
    def _build_mock_factory(self):
        """
        Build a connection factory backed by an in-memory ldap3 MOCK_SYNC
        directory, seeded from ``auth.ldap.mock`` config. The *same* auth flow
        (service bind -> search -> proxy bind -> group gate -> shadow row) runs
        against it; only the transport is simulated. For dev/CI only.
        """
        from ldap3 import (Server as _S, Connection as _C, MOCK_SYNC as _M,
                           OFFLINE_AD_2012_R2 as _SCHEMA)

        person_oc = ["top", "person", "organizationalPerson", "user"]
        base = self.user_search_base or "ou=users,dc=netlapse,dc=local"
        self.user_search_base = base  # ensure the search uses a non-empty base
        dc = _dc_suffix(base)
        groups_base = self.mock_cfg.get("groups_base") or _swap_first_ou(
            base, "groups") or f"ou=groups,{dc}"

        # Service account: reuse configured bind_dn/password, else synthesize.
        svc_dn = self.bind_dn or f"cn=svc-netlapse,{dc}"
        svc_pw = self.bind_password or "mock-service"
        self.bind_dn, self.bind_password = svc_dn, svc_pw

        users = self.mock_cfg.get("users") or _DEMO_USERS
        entries = [(svc_dn, {"objectClass": person_oc,
                             "sAMAccountName": _cn_of(svc_dn),
                             "userPassword": svc_pw})]
        group_dns = {}
        for u in users:
            uname = u["username"]
            gdns = []
            for g in (u.get("groups") or []):
                gdn = g if "=" in g else f"cn={g},{groups_base}"
                gdns.append(gdn)
                group_dns[gdn] = _cn_of(gdn)
            entries.append((f"cn={uname},{base}", {
                "objectClass": person_oc,
                "sAMAccountName": uname,
                "displayName": u.get("display", uname),
                "userPassword": u.get("password", ""),
                "memberOf": gdns}))
        for gdn, gcn in group_dns.items():
            entries.append((gdn, {"objectClass": ["top", "group"], "cn": gcn}))

        logger.warning(
            "\n%s\n  Netlapse LDAP is in MOCK MODE — the directory is SIMULATED "
            "in memory.\n  Do NOT use in production. Seeded users: %s\n%s",
            "=" * 64,
            ", ".join(f"{u['username']}/{u.get('password', '')}" for u in users),
            "=" * 64,
        )

        server = _S("netlapse-mock", get_info=_SCHEMA)

        def factory(user, password):
            conn = _C(server, user=user or None, password=password or None,
                      client_strategy=_M, raise_exceptions=False)
            for dn, attrs in entries:
                try:
                    conn.strategy.add_entry(dn, attrs)
                except Exception:
                    pass  # idempotent across connections
            return conn

        return factory

    # -- the contract -----------------------------------------------------
    def authenticate(self, username: str, password: str) -> AuthResult:
        # An empty password must never succeed: many LDAP servers treat a bind
        # with a DN and empty password as an *anonymous* bind (returns True).
        if not username or not password:
            return AuthResult.failure("invalid credentials")
        try:
            return self._authenticate(username, password)
        except Exception as e:  # directory down, TLS error, bad base DN, ...
            logger.warning("LDAP auth error for %r: %s", username, e)
            return AuthResult.failure("directory unavailable")

    def _authenticate(self, username: str, password: str) -> AuthResult:
        # 1. service bind + search for the user's DN and groups
        svc = self._factory(self.bind_dn, self.bind_password)
        if not svc.bind():
            logger.error("LDAP service-account bind failed: %s", svc.result)
            return AuthResult.failure("directory unavailable")

        safe = escape_filter_chars(username)
        flt = self.user_filter.format(username=safe)
        svc.search(
            search_base=self.user_search_base,
            search_filter=flt,
            search_scope=SUBTREE,
            attributes=[self.group_attr, self.display_name_attr,
                        self.username_attr],
        )
        entries = list(svc.entries)
        if not entries:
            svc.unbind()
            return AuthResult.failure("invalid credentials")  # no such user
        entry = entries[0]
        user_dn = entry.entry_dn
        groups = self._read_groups(entry)
        display = self._attr(entry, self.display_name_attr) or username
        sam = self._attr(entry, self.username_attr) or username
        svc.unbind()

        # 2. proxy bind as the user — this is the actual credential check
        user_conn = self._factory(user_dn, password)
        if not user_conn.bind():
            return AuthResult.failure("invalid credentials")  # bad password
        user_conn.unbind()

        # 3. authorization: any-of allowed_groups; admin_groups -> admin
        if self.allowed_groups and not self._in_any(groups, self.allowed_groups):
            logger.info("LDAP user %r authenticated but in no allowed group", sam)
            return AuthResult.failure("not authorized (group membership)")
        role = "admin" if self._in_any(groups, self.admin_groups) else self.default_role

        # 4. provision / refresh the local shadow row and return it
        user = self._sync_shadow(sam, role, display)
        if user is None:
            return AuthResult.failure("account disabled")
        return AuthResult.success(user)

    # -- group handling ---------------------------------------------------
    def _read_groups(self, entry) -> List[str]:
        raw = getattr(entry, self.group_attr, None)
        if raw is None:
            return []
        try:
            vals = list(raw.values)  # ldap3 Attribute -> list
        except AttributeError:
            vals = raw if isinstance(raw, (list, tuple)) else [raw]
        return [str(v) for v in vals]

    @staticmethod
    def _in_any(user_groups: List[str], wanted: List[str]) -> bool:
        """
        True if any wanted token matches a user group. A wanted token with '='
        is a full DN (whole, case-insensitive); a bare token matches the CN.
        """
        if not wanted:
            return False
        dns = {(g or "").strip().lower() for g in user_groups}
        cns = {_cn_of(g) for g in user_groups}
        for w in wanted:
            if "=" in w:
                if w in dns:
                    return True
            elif w in cns:
                return True
        return False

    @staticmethod
    def _attr(entry, name: str):
        a = getattr(entry, name, None)
        if a is None:
            return None
        try:
            return a.value
        except AttributeError:
            return a

    # -- shadow-row provisioning ------------------------------------------
    def _sync_shadow(self, username: str, role: str, display: str) -> Optional[dict]:
        """
        Ensure a local app_users row exists for this directory user, with role
        synced from current group membership. Returns the row, or None if an
        admin has locally disabled the account (local disable wins — it's the
        break-glass for revoking a directory user faster than the directory).
        """
        if self.store is None:
            # No store (e.g. a pure-bind unit test) — synthesize a minimal row.
            return {
                "id": -1, "username": username, "role": role,
                "is_active": 1, "must_change_password": 0,
            }
        existing = self.store.get_by_username(username)
        if existing is None:
            # Unusable local password: a directory user can never log in via
            # the local provider, even in ldap+local mode.
            unusable = secrets.token_urlsafe(32)
            user = self.store.create_user(username, unusable, role=role,
                                          auth_source="ldap")
            logger.info("provisioned LDAP shadow user %r (role=%s)", username, role)
            return user
        if not existing.get("is_active"):
            return None  # locally disabled -> deny
        # Self-heal provenance: reaching here means a successful directory auth,
        # so the row is directory-sourced even if it predates the column (was
        # backfilled as 'local') or was first created locally.
        if existing.get("auth_source") != "ldap":
            self.store.set_auth_source(existing["id"], "ldap")
            existing = self.store.get_by_id(existing["id"])
        if existing.get("role") != role:
            self.store.set_role(existing["id"], role)
            existing = self.store.get_by_id(existing["id"])
            logger.info("synced LDAP user %r role -> %s", username, role)
        return existing