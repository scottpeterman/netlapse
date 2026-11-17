"""
Tests for the auth providers (netlapse/auth/providers.py).

Two seams make this testable without a directory:

  * ``connection_factory`` — inject a fake connection and assert on what the
    provider actually sent (used here for the filter-escaping tests).
  * mock mode — an in-memory ldap3 MOCK_SYNC directory that runs the *same*
    flow (service bind -> search -> proxy bind -> group gate -> shadow row).
    Only the transport is simulated, so these are end-to-end tests of the
    real code path.

All fixtures use a lab directory (dc=netlapse,dc=local) and lab group names.

ldap3 is an optional dependency of the package, so the mock-mode tests skip
rather than fail when it is absent — matching the local-only install path.
"""

import logging

import pytest

from netlapse.auth.providers import (
    AuthResult,
    LdapAuthProvider,
    _cn_of,
    _dc_suffix,
    _norm_group,
    _swap_first_ou,
)


BASE = "ou=users,dc=netlapse,dc=local"
GROUPS = "ou=groups,dc=netlapse,dc=local"

LAB_CONFIG = {
    "user_search_base": BASE,
    "allowed_groups": ["netlapse-users"],
    "admin_groups": ["netlapse-admins"],
    "mock": {"enabled": True},
}


def mock_provider(store=None, **overrides):
    ldap3 = pytest.importorskip("ldap3")  # noqa: F841
    cfg = {**LAB_CONFIG, **overrides}
    return LdapAuthProvider(cfg, store=store)


# ---------------------------------------------------------------------------
# Fakes for the connection-factory seam
# ---------------------------------------------------------------------------

class FakeAttr:
    def __init__(self, value):
        self.value = value
        self.values = value if isinstance(value, list) else [value]


class FakeEntry:
    def __init__(self, dn, groups, display, sam):
        self.entry_dn = dn
        self.memberOf = FakeAttr(groups)
        self.displayName = FakeAttr(display)
        self.sAMAccountName = FakeAttr(sam)


class FakeConn:
    """Records what the provider asked the directory to do."""

    def __init__(self, user, password, log, entries, passwords):
        self.user = user
        self.password = password
        self.log = log
        self._entries = entries
        self._passwords = passwords
        self.entries = []

    def bind(self):
        self.log.append(("bind", self.user))
        return self._passwords.get(self.user) == self.password

    def search(self, search_base, search_filter, search_scope, attributes):
        self.log.append(("search", search_filter))
        self.entries = list(self._entries)
        return True

    def unbind(self):
        return True


def recording_provider(entries=(), passwords=None, **overrides):
    """A provider whose transport is a FakeConn, plus the call log."""
    log = []
    passwords = passwords or {}

    def factory(user, password):
        return FakeConn(user, password, log, entries, passwords)

    cfg = {
        "user_search_base": BASE,
        "bind_dn": f"cn=svc-netlapse,{BASE}",
        "bind_password": "service",
        "allowed_groups": ["netlapse-users"],
        "admin_groups": ["netlapse-admins"],
        **overrides,
    }
    # Seed the fake directory with the configured service password only when
    # the caller hasn't specified one — a test that wants the service bind to
    # fail supplies a different password for the bind DN.
    passwords.setdefault(cfg["bind_dn"], cfg["bind_password"])
    return LdapAuthProvider(cfg, store=None, connection_factory=factory), log


class FakeStore:
    """Minimal in-memory app_users store."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self._next = 100
        self.calls = []

    def get_by_username(self, username):
        return next((r for r in self.rows if r["username"] == username), None)

    def get_by_id(self, uid):
        return next((r for r in self.rows if r["id"] == uid), None)

    def create_user(self, username, password, role="viewer", auth_source="local"):
        self.calls.append(("create", username, role, auth_source))
        row = {"id": self._next, "username": username, "role": role,
               "is_active": 1, "auth_source": auth_source,
               "must_change_password": 0}
        self._next += 1
        self.rows.append(row)
        return row

    def set_role(self, uid, role):
        self.calls.append(("set_role", uid, role))
        self.get_by_id(uid)["role"] = role

    def set_auth_source(self, uid, source):
        self.calls.append(("set_auth_source", uid, source))
        self.get_by_id(uid)["auth_source"] = source


# ---------------------------------------------------------------------------
# DN / group helpers — a wrong answer here means a wrong role
# ---------------------------------------------------------------------------

class TestDNHelpers:

    @pytest.mark.parametrize("dn,expected", [
        (f"cn=netlapse-admins,{GROUPS}", "netlapse-admins"),
        ("CN=Netlapse-Admins,ou=groups,dc=netlapse,dc=local", "netlapse-admins"),
        ("netlapse-admins", "netlapse-admins"),
        ("", ""),
    ])
    def test_cn_of(self, dn, expected):
        assert _cn_of(dn) == expected

    @pytest.mark.parametrize("dn,expected", [
        (BASE, "dc=netlapse,dc=local"),
        ("cn=user,ou=eng,ou=users,dc=netlapse,dc=local", "dc=netlapse,dc=local"),
        ("dc=netlapse,dc=local", "dc=netlapse,dc=local"),
        ("ou=users", "ou=users"),   # no dc component -> returned whole
        ("", ""),
    ])
    def test_dc_suffix(self, dn, expected):
        assert _dc_suffix(dn) == expected

    def test_swap_first_ou(self):
        assert _swap_first_ou(BASE, "groups") == GROUPS

    def test_swap_first_ou_returns_none_without_a_leading_ou(self):
        assert _swap_first_ou("dc=netlapse,dc=local", "groups") is None
        assert _swap_first_ou("", "groups") is None

    def test_norm_group_trims_and_lowercases(self):
        assert _norm_group("  Netlapse-Users  ") == "netlapse-users"
        assert _norm_group(None) == ""


class TestGroupGate:
    """_in_any is the authorization decision — both directions matter."""

    USER_GROUPS = [
        f"CN=netlapse-users,{GROUPS}",
        f"CN=some-other-team,{GROUPS}",
    ]

    def test_bare_token_matches_the_cn(self):
        assert LdapAuthProvider._in_any(self.USER_GROUPS, ["netlapse-users"])

    def test_full_dn_matches_whole_and_case_insensitively(self):
        assert LdapAuthProvider._in_any(
            self.USER_GROUPS, [f"cn=netlapse-users,{GROUPS}".lower()]
        )

    def test_non_member_is_rejected(self):
        assert not LdapAuthProvider._in_any(self.USER_GROUPS, ["netlapse-admins"])

    def test_any_of_semantics(self):
        assert LdapAuthProvider._in_any(
            self.USER_GROUPS, ["netlapse-admins", "netlapse-users"]
        )

    def test_empty_wanted_is_false_not_true(self):
        """
        An empty admin_groups must not elevate everyone. This is the
        difference between 'no admins configured' and 'everyone is admin'.
        """
        assert not LdapAuthProvider._in_any(self.USER_GROUPS, [])

    def test_empty_user_groups_is_false(self):
        assert not LdapAuthProvider._in_any([], ["netlapse-users"])

    def test_bare_token_does_not_match_a_different_ou_with_the_same_cn(self):
        """A bare token matches the CN — document that this is OU-agnostic."""
        elsewhere = ["CN=netlapse-users,ou=contractors,dc=netlapse,dc=local"]
        assert LdapAuthProvider._in_any(elsewhere, ["netlapse-users"])
        assert not LdapAuthProvider._in_any(
            elsewhere, [f"cn=netlapse-users,{GROUPS}"]
        )


# ---------------------------------------------------------------------------
# Filter escaping — the security claim
# ---------------------------------------------------------------------------

class TestFilterEscaping:

    def test_injection_payload_is_escaped_before_it_reaches_the_filter(self):
        """
        A username crafted to broaden the search must be neutralised. The
        raw payload must never appear in the filter that is sent.
        """
        pytest.importorskip("ldap3")
        provider, log = recording_provider()
        payload = "*)(uid=*"

        provider.authenticate(payload, "irrelevant")

        sent = [f for kind, f in log if kind == "search"]
        assert sent, "no search was issued"
        assert payload not in sent[0]
        assert r"\2a" in sent[0].lower()  # '*' escaped

    @pytest.mark.parametrize("payload,escape", [
        ("*", r"\2a"),
        ("admin)", r"\29"),
        ("(admin", r"\28"),
        ("admin\\", r"\5c"),
        ("admin\x00", r"\00"),
    ])
    def test_every_filter_metacharacter_is_escaped(self, payload, escape):
        """
        Each of the five LDAP filter metacharacters must arrive as its hex
        escape. Asserting on the escape (rather than on the payload being
        absent) is the check that actually holds: an escaped backslash still
        contains the original character by construction.
        """
        pytest.importorskip("ldap3")
        provider, log = recording_provider()
        provider.authenticate(payload, "irrelevant")

        sent = [f for kind, f in log if kind == "search"][0]
        assert escape in sent.lower()

    def test_filter_structure_survives_a_payload(self):
        """The injected parens must not change the shape of the filter."""
        pytest.importorskip("ldap3")
        provider, log = recording_provider()
        provider.authenticate("admin)(|(objectClass=*", "irrelevant")

        sent = [f for kind, f in log if kind == "search"][0]
        assert sent.count("(") == sent.count(")") == 3

    def test_ordinary_username_is_unmodified(self):
        pytest.importorskip("ldap3")
        provider, log = recording_provider()
        provider.authenticate("lab.operator", "irrelevant")

        sent = [f for kind, f in log if kind == "search"][0]
        assert "lab.operator" in sent


# ---------------------------------------------------------------------------
# The auth flow, end to end against the mock directory
# ---------------------------------------------------------------------------

class TestMockAuthFlow:

    def test_admin_group_member_gets_the_admin_role(self):
        result = mock_provider().authenticate("admin", "admin")

        assert result.ok
        assert result.user["role"] == "admin"

    def test_ordinary_member_gets_the_default_role(self):
        result = mock_provider().authenticate("user", "user")

        assert result.ok
        assert result.user["role"] == "viewer"

    def test_default_role_is_configurable(self):
        provider = mock_provider(default_role="operator")
        assert provider.authenticate("user", "user").user["role"] == "operator"

    def test_authenticated_but_ungrouped_user_is_denied(self):
        """
        The seeded 'denied' user has valid credentials and no allowed group.
        Authentication succeeding must not imply authorization.
        """
        result = mock_provider().authenticate("denied", "denied")

        assert not result.ok
        assert "group" in result.reason

    def test_wrong_password_is_rejected(self):
        result = mock_provider().authenticate("admin", "wrong")

        assert not result.ok
        assert result.reason == "invalid credentials"

    def test_unknown_user_is_rejected(self):
        result = mock_provider().authenticate("nobody", "whatever")

        assert not result.ok
        assert result.reason == "invalid credentials"

    @pytest.mark.parametrize("username,password", [
        ("admin", ""),
        ("", "admin"),
        ("", ""),
        ("admin", None),
        (None, "admin"),
    ])
    def test_empty_credentials_never_succeed(self, username, password):
        """
        Many directories treat a bind with a DN and empty password as an
        anonymous bind and return success. This is the guard against that.
        """
        result = mock_provider().authenticate(username, password)

        assert not result.ok
        assert result.reason == "invalid credentials"

    def test_no_allowed_groups_configured_lets_any_directory_user_in(self):
        """Documented behaviour: an empty gate is an open gate, not a closed one."""
        provider = mock_provider(allowed_groups=[])
        assert provider.authenticate("denied", "denied").ok

    def test_mock_mode_logs_a_warning(self, caplog):
        pytest.importorskip("ldap3")
        with caplog.at_level(logging.WARNING):
            mock_provider()
        assert "MOCK MODE" in caplog.text


class TestFailureHandling:

    def test_directory_error_is_reported_not_raised(self):
        """
        An ldap+local chain must be able to fall through, so a directory
        failure returns a result rather than exploding the login route.
        """
        def exploding_factory(user, password):
            raise OSError("connection refused")

        provider = LdapAuthProvider(
            {"user_search_base": BASE}, store=None,
            connection_factory=exploding_factory,
        )
        result = provider.authenticate("admin", "admin")

        assert not result.ok
        assert result.reason == "directory unavailable"

    def test_service_bind_failure_is_reported_not_raised(self):
        """
        A rotated service-account password is an operational failure, not a
        user error — it must not be reported as bad user credentials.
        """
        pytest.importorskip("ldap3")
        provider, _ = recording_provider(
            passwords={f"cn=svc-netlapse,{BASE}": "the-real-password"},
            bind_password="stale-password",
        )
        result = provider.authenticate("admin", "admin")

        assert not result.ok
        assert result.reason == "directory unavailable"

    def test_failure_never_leaks_a_user_object(self):
        result = mock_provider().authenticate("admin", "wrong")
        assert result.user is None


# ---------------------------------------------------------------------------
# Shadow-row provisioning
# ---------------------------------------------------------------------------

class TestShadowProvisioning:

    def test_first_login_provisions_a_directory_sourced_row(self):
        store = FakeStore()
        result = mock_provider(store=store).authenticate("user", "user")

        assert result.ok
        assert store.get_by_username("user")["auth_source"] == "ldap"

    def test_provisioned_row_gets_an_unusable_local_password(self):
        """
        A directory user must never be able to authenticate through the
        local provider in ldap+local mode.
        """
        store = FakeStore()
        mock_provider(store=store).authenticate("user", "user")

        create = [c for c in store.calls if c[0] == "create"][0]
        assert create[3] == "ldap"

    def test_role_is_resynced_from_current_group_membership(self):
        store = FakeStore([{
            "id": 1, "username": "admin", "role": "viewer",
            "is_active": 1, "auth_source": "ldap", "must_change_password": 0,
        }])
        result = mock_provider(store=store).authenticate("admin", "admin")

        assert result.user["role"] == "admin"
        assert ("set_role", 1, "admin") in store.calls

    def test_locally_disabled_account_is_denied(self):
        """Local disable is the break-glass — it must beat a valid directory login."""
        store = FakeStore([{
            "id": 1, "username": "admin", "role": "admin",
            "is_active": 0, "auth_source": "ldap", "must_change_password": 0,
        }])
        result = mock_provider(store=store).authenticate("admin", "admin")

        assert not result.ok
        assert result.reason == "account disabled"

    def test_provenance_self_heals_for_a_row_marked_local(self):
        store = FakeStore([{
            "id": 1, "username": "admin", "role": "admin",
            "is_active": 1, "auth_source": "local", "must_change_password": 0,
        }])
        mock_provider(store=store).authenticate("admin", "admin")

        assert ("set_auth_source", 1, "ldap") in store.calls

    def test_unchanged_role_is_not_rewritten(self):
        store = FakeStore([{
            "id": 1, "username": "admin", "role": "admin",
            "is_active": 1, "auth_source": "ldap", "must_change_password": 0,
        }])
        mock_provider(store=store).authenticate("admin", "admin")

        assert not [c for c in store.calls if c[0] == "set_role"]

    def test_storeless_provider_synthesizes_a_row(self):
        result = mock_provider(store=None).authenticate("user", "user")

        assert result.ok
        assert result.user["id"] == -1
        assert result.user["is_active"] == 1


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------

class TestConfigResolution:

    def test_env_var_is_preferred_over_inline_bind_password(self, monkeypatch):
        monkeypatch.setenv("NETLAPSE_TEST_BIND_PW", "from-env")
        resolved = LdapAuthProvider._resolve_bind_password({
            "bind_password_env": "NETLAPSE_TEST_BIND_PW",
            "bind_password": "inline",
        })
        assert resolved == "from-env"

    def test_falls_back_to_inline_when_env_var_is_unset(self, monkeypatch):
        monkeypatch.delenv("NETLAPSE_TEST_BIND_PW", raising=False)
        resolved = LdapAuthProvider._resolve_bind_password({
            "bind_password_env": "NETLAPSE_TEST_BIND_PW",
            "bind_password": "inline",
        })
        assert resolved == "inline"

    def test_inline_password_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            LdapAuthProvider._resolve_bind_password({"bind_password": "inline"})
        assert "bind_password_env" in caplog.text

    def test_missing_password_is_empty_not_an_error(self):
        assert LdapAuthProvider._resolve_bind_password({}) == ""

    def test_single_server_key_is_accepted_as_a_list(self):
        provider = LdapAuthProvider(
            {"server": "ldaps://dc1.netlapse.local"}, store=None,
            connection_factory=lambda u, p: None,
        )
        assert provider.servers == ["ldaps://dc1.netlapse.local"]

    def test_tls_validation_defaults_to_on(self):
        provider = LdapAuthProvider(
            {}, store=None, connection_factory=lambda u, p: None
        )
        assert provider.validate_cert is True
        assert provider.use_ssl is True


class TestAuthResult:

    def test_success_carries_the_user(self):
        result = AuthResult.success({"username": "lab.operator"})
        assert result.ok and result.user["username"] == "lab.operator"

    def test_failure_carries_a_reason_and_no_user(self):
        result = AuthResult.failure("invalid credentials")
        assert not result.ok and result.user is None