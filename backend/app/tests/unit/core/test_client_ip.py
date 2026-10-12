"""Who is on the other end of a request — attacks on ``app/core/client_ip.py``.

The anonymous rate limit, the authentication throttle and the address stored
against a session all count by what :func:`client_ip` returns. An attacker who
can choose that value chooses their own bucket: a fresh allowance per request,
or somebody else's address to exhaust. Every test here is a way of trying to
choose it.

The topology in the tests, left to right::

    attacker/client  →  [outer proxy]  →  inner proxy  →  application
       151.101.1.69                         10.0.0.5        (socket peer)

No database. Requests are real Starlette requests built from an ASGI scope.
"""

from __future__ import annotations

import ipaddress
import itertools
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from starlette.requests import Request

from app.core import client_ip as module
from app.core.client_ip import UNKNOWN_SOURCE, client_ip, parse_ip, source_of
from app.core.config import AppEnv, Settings, settings

if TYPE_CHECKING:
    from collections.abc import Sequence

PROXY = "10.0.0.5"  # a socket peer inside the default trusted networks
CLIENT = "151.101.1.69"  # what our proxy saw and appended
ATTACKER = "93.184.216.34"  # a public address connecting directly
VICTIM = "1.1.1.1"  # an address the attacker would like to be mistaken for

DEFAULT_CIDRS = list(Settings.model_fields["RATE_LIMIT_TRUSTED_PROXY_CIDRS"].default)


def _request(peer: str | None, *forwarded: str, port: int = 51234, **extra: str) -> Request:
    """Build a request from ``peer`` carrying one ``X-Forwarded-For`` header per value."""
    headers = [(b"x-forwarded-for", value.encode("utf-8")) for value in forwarded]
    headers += [(k.replace("_", "-").encode(), v.encode()) for k, v in extra.items()]
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "client": (peer, port) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every setting the resolver reads."""
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_CIDRS", list(DEFAULT_CIDRS))
    monkeypatch.setattr(settings, "APP_ENV", AppEnv.DEVELOPMENT)


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare that a proxy we operate stands in front of the application."""
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "APP_ENV", AppEnv.PRODUCTION)


def _hops(monkeypatch: pytest.MonkeyPatch, hops: int) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", hops)


def _cidrs(monkeypatch: pytest.MonkeyPatch, cidrs: Sequence[str]) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_CIDRS", list(cidrs))


# ── Trust off ────────────────────────────────────────────────────────────────


class TestHeaderIsIgnoredWhenTrustIsOff:
    """Attack: send ``X-Forwarded-For`` to an application that never asked for it."""

    @pytest.mark.parametrize("environment", [AppEnv.DEVELOPMENT, AppEnv.STAGING, AppEnv.PRODUCTION])
    @pytest.mark.parametrize(
        "forged",
        [VICTIM, f"{VICTIM}, {VICTIM}", "127.0.0.1", "10.0.0.1", "::1", "unknown", ""],
    )
    def test_a_direct_caller_cannot_name_its_own_address(
        self, monkeypatch: pytest.MonkeyPatch, environment: AppEnv, forged: str
    ) -> None:
        monkeypatch.setattr(settings, "APP_ENV", environment)

        assert client_ip(_request(ATTACKER, forged)) == ATTACKER

    def test_a_fresh_header_per_request_is_still_one_caller(self) -> None:
        """The attack on a rate limit: a new claimed address for every request."""
        seen = {client_ip(_request(ATTACKER, f"198.51.100.{i}")) for i in range(1, 200)}

        assert seen == {ATTACKER}

    def test_the_default_is_off(self) -> None:
        assert Settings.model_fields["RATE_LIMIT_TRUST_PROXY_HEADER"].default is False

    def test_other_forwarding_headers_are_never_read(self, trusted: None) -> None:
        """Only X-Forwarded-For is ours; Forwarded / X-Real-IP / X-Client-IP are the caller's."""
        request = _request(
            PROXY,
            CLIENT,
            forwarded=f"for={VICTIM}",
            x_real_ip=VICTIM,
            x_client_ip=VICTIM,
            true_client_ip=VICTIM,
            cf_connecting_ip=VICTIM,
        )

        assert client_ip(request) == CLIENT

    def test_no_peer_and_no_header_is_no_address(self) -> None:
        assert client_ip(_request(None)) is None

    def test_no_peer_with_a_header_is_still_no_address(self) -> None:
        assert client_ip(_request(None, VICTIM)) is None


class TestWithTrustOffThePeerIsAlwaysTheCaller:
    """Attack: from inside the network, add ``X-Forwarded-For`` to be counted as nobody.

    "No source" means the per-source password buckets are left out and only
    the account's shared budget applies; for the anonymous rate limit it is
    one budget shared by every address-less request. A caller who could choose
    that would shed its own limits, or spend everyone's.

    An earlier version guessed: trust off, a private peer and the header
    present meant "probably an undeclared proxy" and gave no address — which
    let exactly such a caller erase itself. Nothing is guessed now. Whether
    there is a proxy is the operator's statement, demanded at startup
    (``test_secret_key_config.py::TestProxyTopologyMustBeDeclared``); with
    trust off the socket peer — which cannot be forged — is the caller, in
    every environment, wherever it connects from, whatever it sends.
    """

    @pytest.mark.parametrize("environment", [AppEnv.DEVELOPMENT, AppEnv.STAGING, AppEnv.PRODUCTION])
    @pytest.mark.parametrize(
        "peer", [PROXY, "172.16.4.4", "192.168.1.1", "127.0.0.1", "::1", "fd00::1", ATTACKER]
    )
    @pytest.mark.parametrize(
        "header", [CLIENT, VICTIM, f"{VICTIM}, {CLIENT}", "127.0.0.1", "unknown", "garbage"]
    )
    def test_a_caller_on_a_private_address_cannot_make_itself_sourceless(
        self, monkeypatch: pytest.MonkeyPatch, environment: AppEnv, peer: str, header: str
    ) -> None:
        monkeypatch.setattr(settings, "APP_ENV", environment)

        resolved = client_ip(_request(peer, header))

        assert resolved == peer
        assert source_of(resolved) != UNKNOWN_SOURCE

    @pytest.mark.parametrize(
        ("declared", "development", "inside", "header"),
        list(itertools.product([False, True], repeat=4)),
    )
    def test_the_peer_is_the_answer_in_every_combination(
        self,
        monkeypatch: pytest.MonkeyPatch,
        declared: bool,
        development: bool,
        inside: bool,
        header: bool,
    ) -> None:
        """Flag declared or never set x development or not x peer in a proxy network or not x header or not.

        Sixteen cases, one answer. The undeclared, non-development cases
        cannot arise from a real startup (settings refuse to build); they are
        produced here by changing the environment on already-built settings,
        so that even then the resolver has no "no address" answer to give.
        """
        names = {name.upper() for name in Settings.model_fields}
        for variable in list(os.environ):
            if variable.upper() in names:
                monkeypatch.delenv(variable)
        values: dict[str, Any] = {"RATE_LIMIT_TRUST_PROXY_HEADER": False} if declared else {}
        built = Settings(_env_file=None, **values)  # type: ignore[call-arg]
        assert ("RATE_LIMIT_TRUST_PROXY_HEADER" in built.model_fields_set) is declared
        if not development:
            built.APP_ENV = AppEnv.PRODUCTION
        assert ("RATE_LIMIT_TRUST_PROXY_HEADER" in built.model_fields_set) is declared
        monkeypatch.setattr(module, "settings", built)
        peer = PROXY if inside else ATTACKER
        request = _request(peer, VICTIM) if header else _request(peer)

        resolved = client_ip(request)

        assert resolved == peer
        assert resolved != VICTIM
        assert source_of(resolved) == peer

    @pytest.mark.parametrize("cidrs", [DEFAULT_CIDRS, ["10.0.0.5/32"], ["203.0.113.0/24"], []])
    def test_the_proxy_networks_change_nothing_while_trust_is_off(
        self, monkeypatch: pytest.MonkeyPatch, production: None, cidrs: list[str]
    ) -> None:
        """Listing networks is not a statement that there is a proxy."""
        _cidrs(monkeypatch, cidrs)

        for peer in (PROXY, "10.0.0.6", "192.168.1.1", "127.0.0.1", "203.0.113.9", ATTACKER):
            assert client_ip(_request(peer, f"{VICTIM}, {CLIENT}")) == peer

    def test_a_new_header_per_request_from_inside_is_still_one_caller(
        self, production: None
    ) -> None:
        seen = {client_ip(_request(PROXY, f"198.51.100.{i}")) for i in range(1, 200)}

        assert seen == {PROXY}

    def test_an_ipv4_mapped_internal_peer_is_the_same_caller(self, production: None) -> None:
        """A dual-stack socket reports ``::ffff:10.0.0.5``; one machine, one source."""
        assert client_ip(_request(f"::ffff:{PROXY}", CLIENT)) == PROXY

    def test_only_a_missing_or_non_address_peer_is_no_address(self, production: None) -> None:
        """The one way to have no source: there genuinely is no address to be had."""
        assert client_ip(_request(None, CLIENT)) is None
        assert client_ip(_request("testclient", CLIENT)) is None
        assert source_of(client_ip(_request(None, VICTIM))) == UNKNOWN_SOURCE

    def test_the_resolver_has_no_heuristic_and_keeps_no_state(self) -> None:
        """Nothing in the module guesses at an undeclared proxy any more."""
        assert not hasattr(module, "_proxy_trust_is_declared")
        assert not hasattr(module, "_note_undeclared_proxy")
        assert not hasattr(module, "_undeclared_proxy_logged_at")
        assert "is_development" not in Path(module.__file__).read_text(encoding="utf-8")


# ── Trust on, but the connection is not from our proxy ───────────────────────


class TestHeaderIsIgnoredFromAnUntrustedPeer:
    """Attack: reach the application directly, around the proxy, and bring a header."""

    @pytest.mark.parametrize(
        "forged",
        [VICTIM, f"{CLIENT}, {VICTIM}", "127.0.0.1", "10.0.0.1", "::1", "garbage"],
    )
    def test_a_client_connecting_directly_cannot_name_its_own_address(
        self, trusted: None, forged: str
    ) -> None:
        assert client_ip(_request(ATTACKER, forged)) == ATTACKER

    def test_every_forged_address_from_one_direct_caller_is_one_source(self, trusted: None) -> None:
        sources = {
            source_of(client_ip(_request(ATTACKER, f"198.51.{i}.{j}")))
            for i in range(20)
            for j in range(20)
        }

        assert sources == {ATTACKER}

    def test_an_ipv6_caller_outside_our_networks_is_not_believed(self, trusted: None) -> None:
        assert client_ip(_request("2001:db8::66", VICTIM)) == "2001:db8::66"

    def test_only_the_configured_networks_are_proxies(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        """Narrow the list to the real proxy: every other private host is just a client."""
        _cidrs(monkeypatch, ["10.0.0.5/32"])

        assert client_ip(_request("10.0.0.5", CLIENT)) == CLIENT
        assert client_ip(_request("10.0.0.6", VICTIM)) == "10.0.0.6"
        assert client_ip(_request("127.0.0.1", VICTIM)) == "127.0.0.1"
        assert client_ip(_request("192.168.1.9", VICTIM)) == "192.168.1.9"

    def test_an_empty_network_list_trusts_nobody(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        _cidrs(monkeypatch, [])

        assert client_ip(_request(PROXY, VICTIM)) == PROXY
        assert client_ip(_request("127.0.0.1", VICTIM)) == "127.0.0.1"

    def test_a_public_proxy_range_is_believed_only_when_listed(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        _cidrs(monkeypatch, ["203.0.113.0/24"])

        assert client_ip(_request("203.0.113.9", CLIENT)) == CLIENT
        assert client_ip(_request("203.0.114.9", VICTIM)) == "203.0.114.9"
        assert client_ip(_request(PROXY, VICTIM)) == PROXY

    def test_an_ipv4_network_never_matches_an_ipv6_peer_or_the_reverse(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        _cidrs(monkeypatch, ["0.0.0.0/0"])
        assert client_ip(_request("2001:db8::1", VICTIM)) == "2001:db8::1"

        _cidrs(monkeypatch, ["::/0"])
        assert client_ip(_request(ATTACKER, VICTIM)) == ATTACKER

    def test_an_ipv4_mapped_peer_is_matched_as_the_ipv4_address_it_is(self, trusted: None) -> None:
        """Dual-stack sockets: the proxy at 10.0.0.5 arrives as ``::ffff:10.0.0.5``."""
        assert client_ip(_request(f"::ffff:{PROXY}", CLIENT)) == CLIENT

    def test_an_ipv4_mapped_public_peer_gains_no_trust(self, trusted: None) -> None:
        """Attack: hope the mapped form of a public address matches ``fc00::/7`` or ``::1``."""
        assert client_ip(_request(f"::ffff:{ATTACKER}", VICTIM)) == ATTACKER

    def test_the_default_networks_are_private_and_loopback_only(self) -> None:
        """No public range is trusted unless an operator lists one."""
        for network in DEFAULT_CIDRS:
            parsed = ipaddress.ip_network(network)
            assert parsed.is_private or parsed.is_loopback, network

    @pytest.mark.parametrize(
        "peer", ["127.0.0.1", "::1", "10.1.2.3", "172.31.0.1", "192.168.0.1", "fd00::1"]
    )
    def test_a_proxy_on_any_default_network_is_believed(self, trusted: None, peer: str) -> None:
        assert client_ip(_request(peer, CLIENT)) == CLIENT

    @pytest.mark.parametrize(
        "peer", ["172.32.0.1", "11.0.0.1", "192.169.0.1", "fe80::1", "169.254.1.1"]
    )
    def test_a_peer_just_outside_them_is_not(self, trusted: None, peer: str) -> None:
        assert client_ip(_request(peer, VICTIM)) == peer


# ── Trust on, from our proxy: which entry ────────────────────────────────────


class TestRightmostSelection:
    """Attack: send your own ``X-Forwarded-For`` and let the proxy append to it."""

    def test_one_proxy_takes_the_last_entry(self, trusted: None) -> None:
        assert client_ip(_request(PROXY, CLIENT)) == CLIENT

    @pytest.mark.parametrize(
        "forged_prefix",
        [
            VICTIM,
            f"{VICTIM}, {VICTIM}, {VICTIM}",
            "127.0.0.1",
            "10.0.0.1, 10.0.0.2",
            "::1",
            "unknown",
            "garbage, more garbage",
            ", ,",
            "0.0.0.0",  # noqa: S104 — an address an attacker might claim, not a bind
            ",".join(f"198.51.100.{i}" for i in range(1, 250)),
        ],
    )
    def test_forged_left_entries_change_nothing(self, trusted: None, forged_prefix: str) -> None:
        honest = client_ip(_request(PROXY, CLIENT))
        forged = client_ip(_request(PROXY, f"{forged_prefix}, {CLIENT}"))

        assert forged == honest == CLIENT

    def test_the_leftmost_entry_is_never_the_answer(self, trusted: None) -> None:
        """The classic mistake: "the original client is the first entry"."""
        assert client_ip(_request(PROXY, f"{VICTIM}, 10.0.0.1, {CLIENT}")) == CLIENT

    def test_two_proxies_take_the_second_from_the_right(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        """client → outer proxy (appends CLIENT) → inner proxy (appends the outer's address)."""
        _hops(monkeypatch, 2)

        assert client_ip(_request(PROXY, f"{CLIENT}, 10.0.0.9")) == CLIENT
        assert client_ip(_request(PROXY, f"{VICTIM}, {CLIENT}, 10.0.0.9")) == CLIENT

    @pytest.mark.parametrize("hops", [1, 2, 3, 4, 8])
    def test_the_nth_from_the_right_for_every_chain_length(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None, hops: int
    ) -> None:
        _hops(monkeypatch, hops)
        ours = [CLIENT, *[f"10.0.1.{i}" for i in range(1, hops)]]
        forged = [VICTIM, "127.0.0.1", ATTACKER]

        assert client_ip(_request(PROXY, ", ".join(forged + ours))) == CLIENT

    def test_too_many_hops_configured_falls_back_to_the_peer_not_to_a_forged_entry(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        """Hops set to 3 with one real proxy: the header is shorter than the chain.

        Reading "as far left as there is" would hand the choice to the client.
        """
        _hops(monkeypatch, 3)

        assert client_ip(_request(PROXY, CLIENT)) == PROXY
        assert client_ip(_request(PROXY, f"{VICTIM}, {CLIENT}")) == PROXY

    def test_hops_cannot_be_configured_to_zero_or_absurdly_high(self) -> None:
        """Zero would index the list from the left (``entries[-0]``): the forged end."""
        for bad in (0, -1, 9):
            with pytest.raises(ValueError, match="RATE_LIMIT_TRUSTED_PROXY_HOPS"):
                Settings(_env_file=None, RATE_LIMIT_TRUSTED_PROXY_HOPS=bad)  # type: ignore[call-arg]

    def test_no_header_from_our_proxy_is_the_proxy_itself(self, trusted: None) -> None:
        """A health check from the proxy host; nothing to forge and nothing forged."""
        assert client_ip(_request(PROXY)) == PROXY


class TestDuplicateHeaders:
    """Attack: send a second ``X-Forwarded-For`` header and hope only one is read."""

    def test_headers_are_read_as_one_list_and_the_proxys_entry_is_last(self, trusted: None) -> None:
        assert client_ip(_request(PROXY, VICTIM, CLIENT)) == CLIENT
        assert client_ip(_request(PROXY, f"{VICTIM}, {VICTIM}", "127.0.0.1", CLIENT)) == CLIENT

    def test_a_forged_first_header_is_not_preferred_over_the_real_last_one(
        self, trusted: None
    ) -> None:
        one = client_ip(_request(PROXY, f"{VICTIM}, {CLIENT}"))
        two = client_ip(_request(PROXY, VICTIM, CLIENT))

        assert one == two == CLIENT

    def test_two_hops_across_two_header_lines(
        self, monkeypatch: pytest.MonkeyPatch, trusted: None
    ) -> None:
        _hops(monkeypatch, 2)

        assert client_ip(_request(PROXY, f"{VICTIM}, {CLIENT}", "10.0.0.9")) == CLIENT

    def test_header_name_case_does_not_matter(self, trusted: None) -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "headers": [
                (b"x-forwarded-for", VICTIM.encode()),
                (b"x-forwarded-for", CLIENT.encode()),
            ],
            "client": (PROXY, 1),
        }
        assert client_ip(Request(scope)) == CLIENT

    def test_duplicate_headers_with_trust_off_change_nothing(self) -> None:
        assert client_ip(_request(ATTACKER, VICTIM, VICTIM, VICTIM)) == ATTACKER


class TestGarbageFallsBackToThePeer:
    """Attack: put something that is not an address where the address should be."""

    @pytest.mark.parametrize(
        "entry",
        [
            "unknown",
            "_hidden",
            "localhost",
            "evil.example",
            "999.1.1.1",
            "1.2.3",
            "1.2.3.4.5",
            "1.2.3.4/8",
            "0x7f.0.0.1",
            "127.1",
            "2130706433",
            "١٢٧.٠.٠.١",
            "1.2.3.4 OR 1=1",
            "'; DROP TABLE users;--",
            "<script>alert(1)</script>",
            "203.0.113.7\t198.51.100.1",
            "203.0.113.7 198.51.100.1",
            "::ffff:999.1.1.1",
            "[::1",
            "]",
            "[]",
            "[]:80",
            "gggg::1",
            ":",
            ":80",
            "%eth0",
            "a" * 5000,
        ],
    )
    def test_an_entry_that_is_not_an_address_is_never_returned(
        self, trusted: None, entry: str
    ) -> None:
        resolved = client_ip(_request(PROXY, f"{VICTIM}, {entry}"))

        # Never the garbage, never the forged entry to its left: the socket
        # peer, which cannot be forged — unless the text does parse as an
        # address, in which case that address and nothing else.
        parsed = parse_ip(entry)
        assert resolved == (str(parsed) if parsed is not None else PROXY)
        assert resolved != VICTIM
        assert resolved is not None
        ipaddress.ip_address(resolved)

    def test_garbage_does_not_shift_the_selection_one_entry_left(self, trusted: None) -> None:
        """Skipping unparseable entries would let a client behind a sloppy proxy pick its address."""
        assert client_ip(_request(PROXY, f"{VICTIM}, not-an-address")) == PROXY

    def test_what_is_returned_is_always_an_address_or_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value goes into an INET column and a bucket key: text must never get through."""
        nasty = ["", " ", ",", "x", "1.2.3.4:5:6", "[::1]:x", "::1%", "1.2.3.4%eth0", "\x00", "é"]
        peers = [None, PROXY, ATTACKER, "::1", "not-a-peer", "testclient", ""]
        for trust in (False, True):
            monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", trust)
            for environment in (AppEnv.DEVELOPMENT, AppEnv.PRODUCTION):
                monkeypatch.setattr(settings, "APP_ENV", environment)
                for peer in peers:
                    for entry in nasty:
                        resolved = client_ip(_request(peer, entry))
                        if resolved is not None:
                            assert str(ipaddress.ip_address(resolved)) == resolved

    def test_a_peer_that_is_not_an_address_is_no_address(self, trusted: None) -> None:
        """Test clients and unix sockets report names like ``testclient``; never trusted."""
        assert client_ip(_request("testclient", VICTIM)) is None
        assert client_ip(_request("", VICTIM)) is None
        assert client_ip(_request("/var/run/app.sock", VICTIM)) is None


# ── Address forms ────────────────────────────────────────────────────────────


class TestAddressForms:
    """One machine must be one source however its address is written."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("203.0.113.7", "203.0.113.7"),
            ("  203.0.113.7  ", "203.0.113.7"),
            ("203.0.113.7:4711", "203.0.113.7"),
            ("203.0.113.7:", "203.0.113.7"),
            ("2001:db8::1", "2001:db8::1"),
            ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
            ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
            ("[2001:db8::1]", "2001:db8::1"),
            ("[2001:db8::1]:443", "2001:db8::1"),
            ("fe80::1%eth0", "fe80::1"),
            ("[fe80::1%25eth0]:443", "fe80::1"),
            ("::ffff:203.0.113.7", "203.0.113.7"),
            ("::FFFF:203.0.113.7", "203.0.113.7"),
            ("::ffff:cb00:7107", "203.0.113.7"),
            ("[::ffff:203.0.113.7]:8443", "203.0.113.7"),
            ("0:0:0:0:0:ffff:203.0.113.7", "203.0.113.7"),
        ],
    )
    def test_ports_brackets_zones_and_mapped_addresses_normalise(
        self, text: str, expected: str
    ) -> None:
        assert str(parse_ip(text)) == expected

    @pytest.mark.parametrize("text", [None, "", "   ", "nope", "1.2.3.4:5:6", "::g", "1.2.3.256"])
    def test_what_is_not_an_address_is_none(self, text: str | None) -> None:
        assert parse_ip(text) is None

    def test_an_ipv4_mapped_entry_from_the_proxy_is_the_ipv4_client(self, trusted: None) -> None:
        """Otherwise one machine is two sources depending on the socket family."""
        plain = client_ip(_request(PROXY, "203.0.113.7"))
        mapped = client_ip(_request(PROXY, "::ffff:203.0.113.7"))
        hexed = client_ip(_request(PROXY, "::ffff:cb00:7107"))

        assert plain == mapped == hexed == "203.0.113.7"

    def test_an_entry_with_a_port_is_the_address(self, trusted: None) -> None:
        assert client_ip(_request(PROXY, f"{CLIENT}:55012")) == CLIENT
        assert client_ip(_request(PROXY, "[2001:db8::7]:55012")) == "2001:db8::7"
        assert client_ip(_request(PROXY, "[2001:db8::7]")) == "2001:db8::7"

    def test_an_ipv6_client_is_returned_compressed(self, trusted: None) -> None:
        assert (
            client_ip(_request(PROXY, "2001:0DB8:0000:0000:0000:0000:0000:0007")) == "2001:db8::7"
        )

    def test_a_peer_with_a_zone_is_parsed(self) -> None:
        assert client_ip(_request("fe80::1%en0")) == "fe80::1"

    def test_every_spelling_of_one_peer_is_one_caller(self) -> None:
        spellings = [ATTACKER, f"::ffff:{ATTACKER}", "::ffff:5db8:d822", f" {ATTACKER} "]

        assert {client_ip(_request(s)) for s in spellings} == {ATTACKER}


# ── The source a caller is counted under ─────────────────────────────────────


class TestSourceOf:
    def test_an_ipv4_address_is_its_own_source(self) -> None:
        assert source_of("203.0.113.7") == "203.0.113.7"
        assert source_of("203.0.113.7") != source_of("203.0.113.8")

    def test_an_ipv6_address_is_counted_as_its_slash_64(self) -> None:
        assert source_of("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
        assert source_of("2001:db8:1:2::") == "2001:db8:1:2::/64"

    def test_rotating_through_a_slash_64_is_one_source(self) -> None:
        """Attack: one machine with 2^64 addresses, a new one per guess."""
        sources = {source_of(f"2001:db8:aa:bb:{i:x}:{i * 7:x}:ffff:{i:x}") for i in range(1, 2000)}

        assert sources == {"2001:db8:aa:bb::/64"}

    def test_a_different_slash_64_is_a_different_source(self) -> None:
        assert source_of("2001:db8:aa:bb::1") != source_of("2001:db8:aa:bc::1")

    @pytest.mark.parametrize("nothing", [None, "", "   ", "garbage", "unknown", "1.2.3"])
    def test_no_address_is_the_one_shared_unknown_source(self, nothing: str | None) -> None:
        """An attempt that hides where it came from gets no bucket of its own."""
        assert source_of(nothing) == UNKNOWN_SOURCE == "unknown"

    def test_an_ipv4_mapped_address_is_the_ipv4_source(self) -> None:
        assert source_of("::ffff:203.0.113.7") == source_of("203.0.113.7") == "203.0.113.7"

    def test_every_spelling_of_an_address_is_one_source(self) -> None:
        assert (
            len({source_of(s) for s in ("2001:db8::1", "2001:DB8:0:0::1", "[2001:db8::1]:443")})
            == 1
        )
        assert len({source_of(s) for s in ("203.0.113.7", "203.0.113.7:80", " 203.0.113.7 ")}) == 1

    def test_the_unknown_marker_can_never_be_an_address(self) -> None:
        assert parse_ip(UNKNOWN_SOURCE) is None

    def test_loopback_v6_is_a_source_like_any_other(self) -> None:
        assert source_of("::1") == "::/64"

    def test_the_resolver_and_the_reducer_compose(self, trusted: None) -> None:
        """End to end: forged prefix, IPv6 client with a port, behind our proxy."""
        request = _request(PROXY, f"{VICTIM}, [2001:db8:1:2:dead:beef:0:1]:4444")

        assert source_of(client_ip(request)) == "2001:db8:1:2::/64"
