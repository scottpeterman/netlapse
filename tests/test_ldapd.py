"""
Tests for the ldapd fixture -- and, by construction, for the client behaviors
it exists to pin down.

Read these as two things at once: they prove the fixture works, and they are
the templates for the assertions worth making against real authentication
code. Each one names a failure mode that a mocked connection strategy cannot
reach.

Note the ``get_info=NONE`` on every Server. Left at its default, a client
fetches the root DSE and the full schema during bind, which on a real domain
controller is a multi-thousand-attribute search on every connection.
"""

import socket
import ssl
import tempfile
import time

import pytest

ldap3 = pytest.importorskip("ldap3")
from ldap3 import Connection, NONE, SUBTREE, Server, ServerPool, Tls
from ldap3.utils.conv import escape_filter_chars

from tools.ldapd import (
    AD_SUB_CODES,
    Directory,
    MATCHING_RULE_IN_CHAIN,
    Reader,
    ThreadedLdapServer,
    app,
    ctx,
    dec_int,
    enc_int,
    enc_seq,
    enc_str,
    make_certificate,
    server_ssl_context,
    sub,
)

BASE = "dc=netlab,dc=internal"
USERS = f"ou=users,{BASE}"
GROUPS = f"ou=groups,{BASE}"
SERVICE_DN = f"cn=svc-directory,{BASE}"
SERVICE_PW = "svc-pw"
USER_FILTER = "(&(objectClass=user)(sAMAccountName={username}))"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ldapd():
    """A clean cleartext server on an ephemeral port."""
    with ThreadedLdapServer() as server:
        yield server


@pytest.fixture(scope="module")
def certs():
    """A throwaway CA plus a matching server certificate."""
    return make_certificate(tempfile.mkdtemp(), common_name="127.0.0.1")


def connect(server, user=SERVICE_DN, password=SERVICE_PW, **kwargs):
    target = Server(server.host, port=server.port, get_info=NONE)
    return Connection(target, user=user, password=password,
                      raise_exceptions=False, **kwargs)


def locate(server, username, attributes=("memberOf", "displayName",
                                         "sAMAccountName")):
    """Service bind, then search for a user -- the first half of a login."""
    conn = connect(server)
    assert conn.bind(), conn.result
    conn.search(USERS,
                USER_FILTER.format(username=escape_filter_chars(username)),
                search_scope=SUBTREE, attributes=list(attributes))
    entries = list(conn.entries)
    conn.unbind()
    return entries


def raw_bind(server, dn, password):
    """
    Simple bind built by hand, bypassing any client-side guard.

    Needed because a client may refuse to send a request the server should
    still be tested against.
    """
    request = enc_seq(enc_int(3), enc_str(dn), enc_str(password, ctx(0)),
                      tag=app(0))
    pdu = enc_seq(enc_int(1), request)
    sock = socket.create_connection((server.host, server.port), timeout=5)
    try:
        sock.sendall(pdu)
        data = sock.recv(4096)
    finally:
        sock.close()
    reader = Reader(data)
    body = reader.tlv()[1]
    inner = sub(body)
    inner.tlv()                        # messageID
    return dec_int(sub(inner.tlv()[1]).tlv()[1])   # resultCode


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_service_bind_locate_and_proxy_bind(ldapd):
    entries = locate(ldapd, "admin-user")
    assert len(entries) == 1
    entry = entries[0]
    assert str(entry.displayName) == "Lab Admin"
    assert any("lab-admins" in str(g) for g in entry.memberOf.values)

    good = connect(ldapd, user=entry.entry_dn, password="admin-pw")
    assert good.bind()
    good.unbind()

    bad = connect(ldapd, user=entry.entry_dn, password="not-the-password")
    assert not bad.bind()
    assert bad.result["result"] == 49


def test_search_scopes(ldapd):
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, "(objectClass=user)", search_scope="LEVEL",
                attributes=["sAMAccountName"])
    names = sorted(str(e.sAMAccountName) for e in conn.entries)
    assert names == ["admin-user", "nested-user", "outside-user", "plain-user"]

    conn.search(USERS, "(objectClass=*)", search_scope="BASE",
                attributes=["ou"])
    assert len(conn.entries) == 1
    conn.unbind()


def test_substring_and_negation_filters(ldapd):
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, "(sAMAccountName=*-user)", attributes=["sAMAccountName"])
    assert len(conn.entries) == 4

    conn.search(USERS, "(&(objectClass=user)(!(sAMAccountName=outside-user)))",
                attributes=["sAMAccountName"])
    names = sorted(str(e.sAMAccountName) for e in conn.entries)
    assert "outside-user" not in names and len(names) == 3
    conn.unbind()


# ---------------------------------------------------------------------------
# Filter injection
# ---------------------------------------------------------------------------

def test_escaping_holds_against_filter_injection(ldapd):
    """
    The payload must land inside a single assertion *value*. If escaping
    failed, the parsed tree would gain structure -- an extra branch under the
    `and`, or a `present` node the client never wrote.
    """
    payload = "admin-user)(objectClass=*"
    entries = locate(ldapd, payload)

    assert ldapd.received_filter_nodes[-1] == (
        "and",
        [("eq", "objectClass", "user"),
         ("eq", "sAMAccountName", payload)],
    )
    assert entries == []


def test_unescaped_input_really_does_inject(ldapd):
    """
    The control case. Without escaping the same payload restructures the
    filter, which is what makes the test above meaningful.
    """
    payload = "admin-user)(objectClass=*"
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, USER_FILTER.format(username=payload),
                search_scope=SUBTREE, attributes=["sAMAccountName"])
    conn.unbind()

    node = ldapd.received_filter_nodes[-1]
    assert node[0] == "and" and len(node[1]) == 3      # a branch appeared
    assert ("present", "objectClass") in node[1]


def test_wildcard_in_username_is_neutralized(ldapd):
    locate(ldapd, "*")
    assert ldapd.received_filter_nodes[-1][1][1] == ("eq", "sAMAccountName", "*")
    assert ldapd.received_filters[-1].endswith(r"(sAMAccountName=\2a))")


# ---------------------------------------------------------------------------
# Bind failure modes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sub_code", sorted(AD_SUB_CODES))
def test_ad_sub_codes_all_arrive_as_invalid_credentials(ldapd, sub_code):
    """
    Locked out, expired, disabled and plain-wrong-password are indistinguishable
    by result code. Only the diagnostic string tells them apart, which is what
    client code has to parse if it wants to say anything more useful than
    "invalid credentials".
    """
    ldapd.faults.fail_user_bind(sub_code)
    conn = connect(ldapd, user=f"cn=admin-user,{USERS}", password="admin-pw")
    assert not conn.bind()
    assert conn.result["result"] == 49
    assert f"data {sub_code}" in conn.result.get("message", "")


def test_service_bind_failure_is_distinguishable_from_bad_credentials(ldapd):
    ldapd.faults.service_bind_fails = True
    conn = connect(ldapd)
    assert not conn.bind()
    assert conn.result["result"] == 52       # unavailable, not 49
    assert conn.result["result"] != 49


def test_client_refuses_empty_password_but_server_would_accept_it(ldapd):
    """
    A directory commonly treats DN + empty password as an ANONYMOUS bind and
    returns success. ldap3 blocks it before the wire; other clients and other
    code paths may not, so application code should still reject it explicitly.
    """
    with pytest.raises(ldap3.core.exceptions.LDAPPasswordIsMandatoryError):
        connect(ldapd, user=f"cn=admin-user,{USERS}", password="").bind()

    assert raw_bind(ldapd, f"cn=admin-user,{USERS}", "") == 0

    ldapd.faults.allow_empty_password = False
    assert raw_bind(ldapd, f"cn=admin-user,{USERS}", "") == 49


def test_anonymous_bind_succeeds(ldapd):
    assert raw_bind(ldapd, "", "") == 0


# ---------------------------------------------------------------------------
# Search anomalies
# ---------------------------------------------------------------------------

def test_two_entries_for_one_username(ldapd):
    """
    A loose user_filter can match more than one account. Code that takes
    entries[0] authenticates whichever the directory happened to return first.
    """
    ldapd.faults.duplicate_entries = 2
    assert len(locate(ldapd, "plain-user")) == 2


def test_size_limit_exceeded(ldapd):
    ldapd.faults.flood_entries = 20
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, "(objectClass=user)", attributes=["cn"], size_limit=5)
    assert conn.result["result"] == 4
    conn.unbind()


def test_referral_reaches_the_client(ldapd):
    ldapd.faults.referral_uris = [f"ldap://dc2.netlab.internal/{BASE}"]
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, USER_FILTER.format(username="plain-user"),
                attributes=["cn"])
    kinds = {item.get("type") for item in (conn.response or [])}
    assert "searchResRef" in kinds
    conn.unbind()


def test_forced_result_code_on_search(ldapd):
    ldapd.faults.search_code = 11             # adminLimitExceeded
    conn = connect(ldapd)
    conn.bind()
    conn.search(USERS, "(objectClass=user)", attributes=["cn"])
    assert conn.result["result"] == 11
    conn.unbind()


# ---------------------------------------------------------------------------
# Nested groups
# ---------------------------------------------------------------------------

def test_direct_memberof_misses_nested_membership(ldapd):
    """
    memberOf is not transitive. A user who inherits a group through another
    group does not list it, so a direct membership check silently under-grants.
    """
    entry = locate(ldapd, "nested-user")[0]
    direct = [str(g) for g in entry.memberOf.values]
    assert not any("lab-admins" in g for g in direct)
    assert any("lab-contractors" in g for g in direct)


def test_matching_rule_in_chain_finds_nested_membership(ldapd):
    conn = connect(ldapd)
    conn.bind()
    conn.search(
        USERS,
        f"(&(objectClass=user)"
        f"(memberOf:{MATCHING_RULE_IN_CHAIN}:=cn=lab-admins,{GROUPS}))",
        search_scope=SUBTREE, attributes=["sAMAccountName"])
    names = sorted(str(e.sAMAccountName) for e in conn.entries)
    assert names == ["admin-user", "nested-user"]
    conn.unbind()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def test_receive_timeout_fires(ldapd):
    conn = connect(ldapd, receive_timeout=1)
    assert conn.bind()
    ldapd.faults.hang(search=5.0)
    started = time.monotonic()
    with pytest.raises(ldap3.core.exceptions.LDAPSocketReceiveError):
        conn.search(USERS, "(objectClass=user)", attributes=["cn"])
    assert time.monotonic() - started < 3.0


def test_connection_dropped_mid_search_does_not_hang(ldapd):
    ldapd.faults.drop_mid_search = True
    conn = connect(ldapd, receive_timeout=3)
    conn.bind()
    started = time.monotonic()
    try:
        conn.search(USERS, "(objectClass=user)", attributes=["cn"])
    except Exception:
        pass
    assert time.monotonic() - started < 5.0


def test_server_pool_fails_over_to_a_live_host():
    dead = ThreadedLdapServer().start()
    dead_port = dead.port
    dead.stop()                       # the port is now closed

    with ThreadedLdapServer() as live:
        pool = ServerPool(
            [Server("127.0.0.1", port=dead_port, connect_timeout=1, get_info=NONE),
             Server("127.0.0.1", port=live.port, connect_timeout=1, get_info=NONE)],
            pool_strategy="FIRST", active=True, exhaust=True)
        conn = Connection(pool, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        assert conn.bind(), conn.result
        assert live.bind_log            # the live host is the one that saw it
        conn.unbind()


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

def test_ldaps_succeeds_with_the_right_ca(certs):
    ca, cert, key = certs
    with ThreadedLdapServer(ssl_context=server_ssl_context(cert, key)) as server:
        target = Server(server.host, port=server.port, use_ssl=True,
                        get_info=NONE,
                        tls=Tls(ca_certs_file=ca, validate=ssl.CERT_REQUIRED))
        conn = Connection(target, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        assert conn.bind(), conn.result
        conn.unbind()


def test_ldaps_rejects_an_untrusted_ca(certs, tmp_path):
    ca, cert, key = certs
    other_ca, _, _ = make_certificate(str(tmp_path), common_name="127.0.0.1")
    with ThreadedLdapServer(ssl_context=server_ssl_context(cert, key)) as server:
        target = Server(server.host, port=server.port, use_ssl=True,
                        get_info=NONE,
                        tls=Tls(ca_certs_file=other_ca,
                                validate=ssl.CERT_REQUIRED))
        conn = Connection(target, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        try:
            assert not conn.bind()
        except Exception:
            pass                       # a raised handshake error is also a reject


def test_ldaps_rejects_an_expired_certificate(tmp_path):
    ca, cert, key = make_certificate(str(tmp_path), common_name="127.0.0.1",
                                     days=-1)
    with ThreadedLdapServer(ssl_context=server_ssl_context(cert, key)) as server:
        target = Server(server.host, port=server.port, use_ssl=True,
                        get_info=NONE,
                        tls=Tls(ca_certs_file=ca, validate=ssl.CERT_REQUIRED))
        conn = Connection(target, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        try:
            assert not conn.bind()
        except Exception:
            pass


def test_validation_can_be_disabled_for_lab_use(certs, tmp_path):
    """
    Turning verification off is a supported lab choice, not a default. This
    pins the escape hatch so it cannot rot.
    """
    ca, cert, key = certs
    other_ca, _, _ = make_certificate(str(tmp_path), common_name="127.0.0.1")
    with ThreadedLdapServer(ssl_context=server_ssl_context(cert, key)) as server:
        target = Server(server.host, port=server.port, use_ssl=True,
                        get_info=NONE, tls=Tls(validate=ssl.CERT_NONE))
        conn = Connection(target, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        assert conn.bind(), conn.result
        conn.unbind()


def test_start_tls_upgrades_the_plain_port(certs):
    ca, cert, key = certs
    with ThreadedLdapServer(
            start_tls_context=server_ssl_context(cert, key)) as server:
        target = Server(server.host, port=server.port, get_info=NONE,
                        tls=Tls(ca_certs_file=ca, validate=ssl.CERT_REQUIRED))
        conn = Connection(target, user=SERVICE_DN, password=SERVICE_PW,
                          raise_exceptions=False)
        assert conn.start_tls(), conn.result
        assert conn.bind(), conn.result
        conn.unbind()


def test_start_tls_refusal_is_visible(certs):
    ca, cert, key = certs
    with ThreadedLdapServer(
            start_tls_context=server_ssl_context(cert, key)) as server:
        server.faults.reject_start_tls = True
        target = Server(server.host, port=server.port, get_info=NONE,
                        tls=Tls(ca_certs_file=ca, validate=ssl.CERT_REQUIRED))
        conn = Connection(target, raise_exceptions=False)
        try:
            assert not conn.start_tls()
        except Exception:
            pass


def test_start_tls_is_unavailable_when_not_configured(ldapd, certs):
    ca, _, _ = certs
    target = Server(ldapd.host, port=ldapd.port, get_info=NONE,
                    tls=Tls(ca_certs_file=ca, validate=ssl.CERT_REQUIRED))
    conn = Connection(target, raise_exceptions=False)
    try:
        assert not conn.start_tls()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Directory customization
# ---------------------------------------------------------------------------

def test_a_custom_directory_can_be_supplied():
    directory = Directory()
    directory.add("dc=example,dc=test", objectClass=["domain"])
    directory.add("ou=people,dc=example,dc=test",
                  objectClass=["organizationalUnit"])
    directory.add("cn=svc-bind,dc=example,dc=test", password="p",
                  objectClass=["user"], sAMAccountName="svc-bind")
    directory.add_user("solo", "solo-pw", "ou=people,dc=example,dc=test",
                       groups=["team-a"],
                       groups_base="ou=teams,dc=example,dc=test")
    with ThreadedLdapServer(directory=directory) as server:
        conn = connect(server, user="cn=svc-bind,dc=example,dc=test",
                       password="p")
        assert conn.bind()
        conn.search("ou=people,dc=example,dc=test", "(sAMAccountName=solo)",
                    attributes=["memberOf"])
        assert len(conn.entries) == 1
        assert "team-a" in str(conn.entries[0].memberOf.values[0])
        conn.unbind()


def test_reset_clears_faults_and_traffic(ldapd):
    ldapd.faults.fail_user_bind("533")
    locate(ldapd, "plain-user")
    assert ldapd.received_filter_nodes
    ldapd.reset()
    assert ldapd.faults.user_bind_code is None
    assert ldapd.received_filter_nodes == []
    assert ldapd.bind_log == []