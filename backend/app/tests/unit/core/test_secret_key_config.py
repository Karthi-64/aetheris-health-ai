"""The token-signing key: no published key ever signs a token (P3, finding F2).

``APP_SECRET_KEY`` signs every access token and MFA ticket. These tests attack
the configuration: every way of ending up with a key an outsider knows must
either stop the application from starting or be replaced by a key nobody knows.

No database.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.core.config import (
    MIN_AUTH_FAILURE_SECONDS,
    MIN_SECRET_KEY_LENGTH,
    PUBLISHED_SECRET_KEYS,
    Settings,
    settings,
)
from app.core.security import create_access_token, verify_access_token

PRIVATE_KEY = "a-private-signing-key-for-these-tests-0123456789"
#: Staging and production also require the Patient App's OTP key
#: (``PATIENT_OTP_SECRET``); its own rules are tested in ``test_patient_config.py``.
OTP_SECRET = "a-private-otp-key-for-these-tests-0123456789abcdef"
REPO_ROOT = Path(__file__).resolve().parents[5]
BACKEND_ENV_EXAMPLE = REPO_ROOT / "backend" / ".env.example"

#: The only places a published key may appear: the denylist that refuses it,
#: and the tests that attack with it.
PUBLISHED_KEY_ALLOWED_FILE = "backend/app/core/config.py"
PUBLISHED_KEY_ALLOWED_PREFIX = "backend/app/tests/"


@pytest.fixture(autouse=True)
def _no_settings_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``_settings()`` mean what it says: explicit values only.

    ``Settings(_env_file=None, ...)`` skips ``.env`` but still reads the
    process environment. A developer shell or CI job that exports, say,
    ``AUTH_DEVICE_COOKIE_SECURE=false`` or ``APP_SECRET_KEY`` then decides the
    outcome of these tests — the staging and production cases used to fail on
    whichever validator the stray variable tripped first. Every variable that
    names a setting is removed for the duration of each test.
    """
    names = {name.upper() for name in Settings.model_fields}
    for variable in list(os.environ):
        if variable.upper() in names:
            monkeypatch.delenv(variable)


#: Pass as ``RATE_LIMIT_TRUST_PROXY_HEADER`` to leave the flag unset, as a
#: deployment that never mentions it would.
UNDECLARED: Any = object()


def _settings(**values: Any) -> Settings:
    """Build settings from explicit values only — no environment, no ``.env``.

    Staging and production also require an MFA encryption key, the Patient
    App's OTP key and a statement of whether a proxy stands in front
    (``RATE_LIMIT_TRUST_PROXY_HEADER``). These tests are mostly about
    something else, so all three are supplied unless a test passes its own —
    or passes :data:`UNDECLARED` to say nothing.
    """
    values.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    values.setdefault("PATIENT_OTP_SECRET", OTP_SECRET)
    values.setdefault("RATE_LIMIT_TRUST_PROXY_HEADER", False)
    if values["RATE_LIMIT_TRUST_PROXY_HEADER"] is UNDECLARED:
        del values["RATE_LIMIT_TRUST_PROXY_HEADER"]
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _repository_files() -> list[str]:
    """Every file in the repository: tracked, or new and not ignored.

    Paths are relative to the repository root, with forward slashes. Ignored
    files (a developer's own ``.env``, virtualenvs, build output) are not part
    of the repository and are not read.
    """
    listed = subprocess.run(  # noqa: S603 — fixed arguments, no shell
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    return [path for path in listed.stdout.decode("utf-8").split("\0") if path]


def _env_assignments(path: Path, name: str) -> list[tuple[bool, str]]:
    """Every ``NAME=value`` line of an env file: ``(commented_out, value)``."""
    found: list[tuple[bool, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"\s*(#?)\s*(?:export\s+)?{re.escape(name)}\s*=(.*)$", line)
        if match:
            found.append((bool(match.group(1)), match.group(2).strip().strip("\"'")))
    return found


def _forged_token(key: str, *, user_id: str = "00000000-0000-0000-0000-000000000001") -> str:
    """Mint what an attacker who knows ``key`` would mint: a valid-looking access token."""
    now = datetime.now(UTC)
    return pyjwt.encode(
        {
            "sub": user_id,
            "iss": settings.JWT_ISSUER,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "type": "access",
            "hospital_id": None,
        },
        key,
        algorithm="HS256",
    )


class TestOutsideDevelopment:
    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_startup_fails_when_the_key_is_missing(self, environment: str) -> None:
        with pytest.raises(ValidationError, match="APP_SECRET_KEY is required"):
            _settings(APP_ENV=environment)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_startup_fails_when_the_key_is_blank(self, environment: str, blank: str) -> None:
        with pytest.raises(ValidationError, match="APP_SECRET_KEY"):
            _settings(APP_ENV=environment, APP_SECRET_KEY=blank)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    def test_startup_fails_with_a_key_published_in_the_repository(
        self, environment: str, published: str
    ) -> None:
        with pytest.raises(ValidationError, match="published in the repository"):
            _settings(APP_ENV=environment, APP_SECRET_KEY=published)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_startup_fails_with_a_key_that_is_too_short(self, environment: str) -> None:
        with pytest.raises(ValidationError, match="at least"):
            _settings(APP_ENV=environment, APP_SECRET_KEY="x" * (MIN_SECRET_KEY_LENGTH - 1))

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_a_private_key_starts(self, environment: str) -> None:
        configured = _settings(APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY)

        assert configured.APP_SECRET_KEY.get_secret_value() == PRIVATE_KEY
        assert configured.secret_key_is_ephemeral is False

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_there_is_no_fallback_key_outside_development(self, environment: str) -> None:
        """Nothing is generated or substituted: the only outcome is refusal."""
        for bad in (None, "", *sorted(PUBLISHED_SECRET_KEYS)):
            values: dict[str, Any] = {"APP_ENV": environment}
            if bad is not None:
                values["APP_SECRET_KEY"] = bad
            with pytest.raises(ValidationError):
                _settings(**values)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    @pytest.mark.parametrize(
        "wrapping",
        ['"{}"', "'{}'", "\"'{}'\"", "'{}", '{}"', ' "{}" ', '"{}"\n'],
        ids=["double", "single", "nested", "opening-only", "closing-only", "padded", "newline"],
    )
    def test_startup_fails_with_a_published_key_still_wrapped_in_its_quotes(
        self, environment: str, published: str, wrapping: str
    ) -> None:
        """Attack: forge tokens for a deployment whose key is the published one, quotes and all.

        The example file shipped the key quoted, and not every way of loading
        an env file strips quotes (``docker --env-file``, a systemd
        ``EnvironmentFile``, a hand-written ``export``). The quoted value is
        two characters longer than the published one, so it is not on the
        denylist as written and is long enough to pass for a real key — while
        being exactly as public.
        """
        quoted = wrapping.format(published)
        assert quoted not in PUBLISHED_SECRET_KEYS
        assert len(quoted.strip()) >= MIN_SECRET_KEY_LENGTH

        with pytest.raises(ValidationError, match="published in the repository") as refused:
            _settings(APP_ENV=environment, APP_SECRET_KEY=quoted)

        assert published not in str(refused.value)
        assert published not in repr(refused.value)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    def test_startup_fails_with_a_quoted_published_key_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, environment: str, published: str
    ) -> None:
        """The same, arriving the way it would in a container: as a process variable."""
        monkeypatch.setenv("APP_SECRET_KEY", f'"{published}"')

        with pytest.raises(ValidationError, match="published in the repository"):
            _settings(APP_ENV=environment)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_a_private_key_that_merely_contains_quotes_still_starts(self, environment: str) -> None:
        """The quote handling refuses the published key; it does not rewrite anybody's own."""
        private = 'it\'s-a-"private"-signing-key-0123456789-abcdef'

        configured = _settings(APP_ENV=environment, APP_SECRET_KEY=private)

        assert configured.APP_SECRET_KEY.get_secret_value() == private
        assert configured.secret_key_is_ephemeral is False


class TestTheEnvironmentCannotDecideTheseTests:
    """The cause of the old staging/production failures, held down."""

    @pytest.mark.parametrize(
        ("variable", "value"),
        [
            ("AUTH_DEVICE_COOKIE_SECURE", "false"),
            ("auth_device_cookie_secure", "false"),
            ("APP_SECRET_KEY", "change-me-to-a-long-random-string-in-production"),
            ("APP_ENV", "development"),
            ("MFA_ENCRYPTION_KEY", "not-a-fernet-key"),
            ("RATE_LIMIT_TRUSTED_PROXY_CIDRS", "garbage"),
        ],
    )
    def test_a_stray_variable_does_not_reach_settings_built_here(
        self, variable: str, value: str
    ) -> None:
        # Simulate the variable having been exported before the test session:
        # set it, then let the fixture's own cleaning run again.
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(variable, value)
            names = {name.upper() for name in Settings.model_fields}
            for present in list(os.environ):
                if present.upper() in names:
                    patch.delenv(present)

            configured = _settings(APP_ENV="production", APP_SECRET_KEY=PRIVATE_KEY)

        assert configured.APP_ENV.value == "production"
        assert configured.AUTH_DEVICE_COOKIE_SECURE is True
        assert configured.APP_SECRET_KEY.get_secret_value() == PRIVATE_KEY

    def test_no_variable_naming_a_setting_is_left_in_the_environment(self) -> None:
        names = {name.upper() for name in Settings.model_fields}

        assert [variable for variable in os.environ if variable.upper() in names] == []


class TestDeviceCookieOutsideDevelopment:
    """Attack: get the trusted-device cookie sent over plain HTTP in a real deployment.

    Without ``Secure`` and the ``__Host-`` prefix the cookie travels in the
    clear and can be overwritten from a sibling domain. That is allowed for
    local development over ``http://localhost`` and nowhere else.
    """

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("off", [False, "false", "False", "0", "no", "off"])
    def test_startup_fails_with_the_cookie_not_secure(self, environment: str, off: Any) -> None:
        with pytest.raises(ValidationError, match="AUTH_DEVICE_COOKIE_SECURE must be true"):
            _settings(
                APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_DEVICE_COOKIE_SECURE=off
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("off", ["false", "0", "off"])
    def test_startup_fails_when_the_environment_switches_it_off(
        self, monkeypatch: pytest.MonkeyPatch, environment: str, off: str
    ) -> None:
        """The way it would really happen: a variable copied from a laptop's ``.env``."""
        monkeypatch.setenv("APP_ENV", environment)
        monkeypatch.setenv("APP_SECRET_KEY", PRIVATE_KEY)
        monkeypatch.setenv("AUTH_DEVICE_COOKIE_SECURE", off)

        with pytest.raises(ValidationError, match="AUTH_DEVICE_COOKIE_SECURE must be true"):
            _settings()

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_the_refusal_names_the_environment(self, environment: str) -> None:
        with pytest.raises(ValidationError, match=f"when APP_ENV is {environment}"):
            _settings(
                APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_DEVICE_COOKIE_SECURE=False
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_it_starts_with_the_cookie_secure(self, environment: str) -> None:
        configured = _settings(
            APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_DEVICE_COOKIE_SECURE=True
        )

        assert configured.AUTH_DEVICE_COOKIE_SECURE is True

    @pytest.mark.parametrize("environment", ["development", "staging", "production"])
    def test_secure_is_the_default_everywhere(self, environment: str) -> None:
        """Nobody has to remember to switch it on."""
        assert Settings.model_fields["AUTH_DEVICE_COOKIE_SECURE"].default is True
        assert (
            _settings(APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY).AUTH_DEVICE_COOKIE_SECURE
            is True
        )

    def test_development_may_switch_it_off(self) -> None:
        configured = _settings(APP_ENV="development", AUTH_DEVICE_COOKIE_SECURE=False)

        assert configured.AUTH_DEVICE_COOKIE_SECURE is False

    def test_the_cookie_takes_the_host_prefix_exactly_when_it_is_secure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``__Host-`` without ``Secure`` is rejected by browsers; the two go together."""
        from app.api.v1 import auth as auth_routes

        monkeypatch.setattr(settings, "AUTH_DEVICE_COOKIE_SECURE", True)
        assert auth_routes._device_cookie_name() == "__Host-aetheris-device"  # noqa: SLF001

        monkeypatch.setattr(settings, "AUTH_DEVICE_COOKIE_SECURE", False)
        assert auth_routes._device_cookie_name() == "aetheris-device"  # noqa: SLF001


class TestTrustedProxyNetworks:
    """Attack (on the operator): a typo that silently trusts everyone, or no one.

    ``RATE_LIMIT_TRUSTED_PROXY_CIDRS`` decides whose ``X-Forwarded-For`` is
    believed. A value that is not a list of networks must stop the
    application, not be skipped at request time.
    """

    @pytest.mark.parametrize(
        "garbage",
        [
            "garbage",
            "10.0.0.0/8",  # a bare network, not a JSON array
            "10.0.0.0/8,192.168.0.0/16",
            '"10.0.0.0/8"',
            '{"net": "10.0.0.0/8"}',
            "10",
            "true",
            '["10.0.0.0/8"',
            '["not-a-network"]',
            '["10.0.0.0/33"]',
            '["10.0.0.0/8", "nope"]',
            '["999.0.0.0/8"]',
            '["proxy.internal"]',
            '["10.0.0.0/8/8"]',
            '["::1/129"]',
            '["10.0.0.0/-1"]',
            '[""]',
            '["*"]',
        ],
    )
    def test_garbage_from_the_environment_stops_startup(
        self, monkeypatch: pytest.MonkeyPatch, garbage: str
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", garbage)

        with pytest.raises(ValueError, match="RATE_LIMIT_TRUSTED_PROXY_CIDRS"):
            _settings()

    @pytest.mark.parametrize(
        "garbage",
        [
            ["not-a-network"],
            ["10.0.0.0/8", "nope"],
            ["10.0.0.0/33"],
            [""],
            "10.0.0.0/8",
            {"10.0.0.0/8": True},
            ("10.0.0.0/8",),
            42,
            None,
            [None],
            [["10.0.0.0/8"]],
        ],
    )
    def test_garbage_passed_directly_is_rejected(self, garbage: Any) -> None:
        with pytest.raises(ValueError, match="RATE_LIMIT_TRUSTED_PROXY_CIDRS"):
            _settings(RATE_LIMIT_TRUSTED_PROXY_CIDRS=garbage)

    def test_a_json_null_from_the_environment_means_the_default_not_everyone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``null`` is read as "unset". Unset must mean the private default, never "trust all"."""
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", "null")

        configured = _settings().RATE_LIMIT_TRUSTED_PROXY_CIDRS

        assert configured == Settings.model_fields["RATE_LIMIT_TRUSTED_PROXY_CIDRS"].default
        assert "0.0.0.0/0" not in configured
        assert "::/0" not in configured

    def test_real_networks_are_accepted_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", '["10.1.2.0/24", "fd00::/8"]')

        assert _settings().RATE_LIMIT_TRUSTED_PROXY_CIDRS == ["10.1.2.0/24", "fd00::/8"]

    def test_an_empty_list_is_allowed_and_trusts_no_proxy(self) -> None:
        assert _settings(RATE_LIMIT_TRUSTED_PROXY_CIDRS=[]).RATE_LIMIT_TRUSTED_PROXY_CIDRS == []

    def test_the_default_trusts_only_private_and_loopback_networks(self) -> None:
        import ipaddress

        for network in _settings().RATE_LIMIT_TRUSTED_PROXY_CIDRS:
            parsed = ipaddress.ip_network(network)
            assert parsed.is_private or parsed.is_loopback, network

    def test_the_header_is_not_trusted_by_default(self) -> None:
        assert Settings.model_fields["RATE_LIMIT_TRUST_PROXY_HEADER"].default is False
        undeclared = _settings(RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED)

        assert undeclared.RATE_LIMIT_TRUST_PROXY_HEADER is False
        assert "RATE_LIMIT_TRUST_PROXY_HEADER" not in undeclared.model_fields_set

    @pytest.mark.parametrize(
        "everyone",
        [
            ["0.0.0.0/0"],
            ["::/0"],
            ["8.8.8.8/0"],  # host bits set: still every IPv4 address
            ["2001:db8::1/0"],
            ["10.0.0.0/8", "0.0.0.0/0"],
            ["::/0", "10.0.0.0/8"],
            ["0.0.0.0/0.0.0.0"],  # the same network, written with a netmask
            ["0.0.0.0/1", "128.0.0.0/1"],  # two halves
            ["128.0.0.0/1", "10.0.0.0/8", "0.0.0.0/1"],
            ["::/1", "8000::/1"],
            ["0.0.0.0/2", "64.0.0.0/2", "128.0.0.0/2", "192.0.0.0/2"],  # four quarters
            ["0.0.0.0/1", "128.0.0.0/2", "192.0.0.0/3", "224.0.0.0/3"],
            ["10.0.0.0/8", "::/1", "8000::/2", "c000::/2"],
            ["1.2.3.4/1", "200.1.1.1/1"],  # host bits set on both halves
        ],
    )
    @pytest.mark.parametrize("environment", ["development", "staging", "production"])
    def test_a_network_that_covers_every_address_is_refused(
        self, environment: str, everyone: list[str]
    ) -> None:
        """Attack (on the operator): "trust the proxy, wherever it is" — ``0.0.0.0/0``.

        If everyone is our proxy, every caller's ``X-Forwarded-For`` is
        believed, and each request chooses its own rate-limit and throttle
        bucket. Refused in every environment, wherever in the list it hides —
        and however it is spelled: as one ``/0`` or as several networks that
        add up to one.
        """
        with pytest.raises(
            ValidationError, match="RATE_LIMIT_TRUSTED_PROXY_CIDRS must not cover every address"
        ):
            _settings(
                APP_ENV=environment,
                APP_SECRET_KEY=PRIVATE_KEY,
                RATE_LIMIT_TRUSTED_PROXY_CIDRS=everyone,
            )

    @pytest.mark.parametrize(
        "everyone", ['["0.0.0.0/0"]', '["::/0"]', '["10.0.0.0/8","0.0.0.0/0"]']
    )
    def test_a_network_that_covers_every_address_is_refused_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, everyone: str
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", everyone)

        with pytest.raises(ValidationError, match="RATE_LIMIT_TRUSTED_PROXY_CIDRS"):
            _settings()

    @pytest.mark.parametrize(
        "networks",
        [
            ["0.0.0.0/1"],
            ["128.0.0.0/1"],
            ["::/1"],
            ["0.0.0.0/1", "128.0.0.0/2"],  # three quarters is not everything
            ["0.0.0.0/1", "8000::/1"],  # half of each family is not all of either
            ["0.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12"],
        ],
    )
    def test_a_merely_large_set_of_networks_is_not_mistaken_for_everyone(
        self, networks: list[str]
    ) -> None:
        """Only "every address" is refused outright; the check must not reject real ranges."""
        accepted = _settings(RATE_LIMIT_TRUSTED_PROXY_CIDRS=networks).RATE_LIMIT_TRUSTED_PROXY_CIDRS

        assert accepted == networks

    @pytest.mark.parametrize(
        ("rejected", "telltales"),
        [
            (["203.0.113.77/0"], ["203.0.113.77"]),
            (["77.88.99.11/1", "198.51.100.200/1"], ["77.88.99.11", "198.51.100.200"]),
            (["10.0.0.0/8", "198.51.100.200/0"], ["198.51.100.200", "10.0.0.0"]),
            (["2001:db8:feed::1/0"], ["2001:db8:feed", "feed"]),
            (["edge-proxy-7.corp.hospital.example"], ["edge-proxy-7", "hospital.example"]),
            (["10.0.0.0/8", "https://ops:hunter2@proxy.internal/"], ["hunter2", "proxy.internal"]),
            ("ops:hunter2@proxy.internal", ["hunter2", "proxy.internal"]),
        ],
    )
    def test_the_refusal_does_not_echo_the_rejected_value(
        self, rejected: Any, telltales: list[str]
    ) -> None:
        """The error lands in the startup log: it names the setting, never what was in it.

        A network list describes the deployment's internal topology, and a
        mistyped value can be anything — a hostname, a URL with credentials.
        """
        with pytest.raises(ValidationError) as refused:
            _settings(RATE_LIMIT_TRUSTED_PROXY_CIDRS=rejected)

        shown = f"{refused.value}\n{refused.value!r}\n{refused.value.errors(include_input=False)}"
        assert "RATE_LIMIT_TRUSTED_PROXY_CIDRS" in shown
        for telltale in telltales:
            assert telltale not in shown

    def test_the_refusal_from_the_environment_does_not_echo_the_value_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_TRUSTED_PROXY_CIDRS", '["203.0.113.77/0"]')

        with pytest.raises(ValidationError) as refused:
            _settings()

        assert "203.0.113.77" not in str(refused.value)
        assert "203.0.113.77" not in repr(refused.value)

    def test_the_env_example_never_switches_header_trust_on(self) -> None:
        """Attack (on the operator): copy the example and believe every caller's header.

        Trusting ``X-Forwarded-For`` is safe only behind a proxy the operator
        runs. An example that shipped ``true`` — even commented out, ready to
        be uncommented — would hand every direct caller its choice of address.
        """
        for _commented, value in _env_assignments(
            BACKEND_ENV_EXAMPLE, "RATE_LIMIT_TRUST_PROXY_HEADER"
        ):
            assert value.lower() not in {"true", "1", "yes", "on"}
        for _commented, value in _env_assignments(
            BACKEND_ENV_EXAMPLE, "RATE_LIMIT_TRUSTED_PROXY_CIDRS"
        ):
            assert "/0" not in value


class TestProxyTopologyMustBeDeclared:
    """Attack (on the operator): deploy behind a proxy nobody told the application about.

    Left unsaid, every caller behind that proxy has the proxy's address: the
    whole platform is one source, and one attacker exhausts the per-source
    limits for everyone. No heuristic can tell that from a client that merely
    sends ``X-Forwarded-For`` — so nothing is guessed. Outside development the
    application refuses to start until ``RATE_LIMIT_TRUST_PROXY_HEADER`` has
    been set, to true or to false.
    """

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_startup_fails_when_nobody_has_said(self, environment: str) -> None:
        with pytest.raises(
            ValidationError, match="RATE_LIMIT_TRUST_PROXY_HEADER must be set explicitly"
        ):
            _settings(
                APP_ENV=environment,
                APP_SECRET_KEY=PRIVATE_KEY,
                RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED,
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_the_refusal_names_the_environment_and_both_answers(self, environment: str) -> None:
        with pytest.raises(
            ValidationError, match=rf"\(true or false\) when APP_ENV is {environment}"
        ):
            _settings(
                APP_ENV=environment,
                APP_SECRET_KEY=PRIVATE_KEY,
                RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED,
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_other_proxy_settings_are_not_an_answer(self, environment: str) -> None:
        """Listing networks or a hop count does not say whether the header is to be believed."""
        with pytest.raises(
            ValidationError, match="RATE_LIMIT_TRUST_PROXY_HEADER must be set explicitly"
        ):
            _settings(
                APP_ENV=environment,
                APP_SECRET_KEY=PRIVATE_KEY,
                RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED,
                RATE_LIMIT_TRUSTED_PROXY_HOPS=2,
                RATE_LIMIT_TRUSTED_PROXY_CIDRS=["10.0.0.0/8"],
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("answer", [False, True])
    def test_either_answer_starts(self, environment: str, answer: bool) -> None:
        configured = _settings(
            APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, RATE_LIMIT_TRUST_PROXY_HEADER=answer
        )

        assert configured.RATE_LIMIT_TRUST_PROXY_HEADER is answer

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize(
        ("name", "value", "answer"),
        [
            ("RATE_LIMIT_TRUST_PROXY_HEADER", "false", False),
            ("RATE_LIMIT_TRUST_PROXY_HEADER", "0", False),
            ("RATE_LIMIT_TRUST_PROXY_HEADER", "true", True),
            ("rate_limit_trust_proxy_header", "false", False),
        ],
    )
    def test_an_answer_from_the_environment_starts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        environment: str,
        name: str,
        value: str,
        answer: bool,
    ) -> None:
        monkeypatch.setenv(name, value)

        configured = _settings(
            APP_ENV=environment,
            APP_SECRET_KEY=PRIVATE_KEY,
            RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED,
        )

        assert configured.RATE_LIMIT_TRUST_PROXY_HEADER is answer

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("blank", ["", "   ", "maybe", "null"])
    def test_a_blank_or_meaningless_answer_is_not_an_answer(
        self, monkeypatch: pytest.MonkeyPatch, environment: str, blank: str
    ) -> None:
        """``RATE_LIMIT_TRUST_PROXY_HEADER=`` in a copied file must not pass for "false"."""
        monkeypatch.setenv("RATE_LIMIT_TRUST_PROXY_HEADER", blank)

        with pytest.raises(ValidationError, match="RATE_LIMIT_TRUST_PROXY_HEADER"):
            _settings(
                APP_ENV=environment,
                APP_SECRET_KEY=PRIVATE_KEY,
                RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED,
            )

    def test_development_starts_without_an_answer_and_does_not_trust_the_header(self) -> None:
        configured = _settings(APP_ENV="development", RATE_LIMIT_TRUST_PROXY_HEADER=UNDECLARED)

        assert configured.RATE_LIMIT_TRUST_PROXY_HEADER is False

    def test_the_running_settings_cannot_be_an_undeclared_real_deployment(self) -> None:
        """The process under test is development; anything else would have had to declare."""
        assert (
            settings.is_development or "RATE_LIMIT_TRUST_PROXY_HEADER" in settings.model_fields_set
        )

    def test_the_env_example_leaves_the_question_unanswered(self) -> None:
        """Attack (on the operator): deploy by copying ``.env.example`` and inherit an answer.

        Either answer is wrong for somebody: ``false`` behind a proxy makes
        every user one caller, ``true`` without one lets callers name their
        own address. So the example may mention the setting only as a comment,
        and with no value an operator could uncomment and keep.
        """
        assignments = _env_assignments(BACKEND_ENV_EXAMPLE, "RATE_LIMIT_TRUST_PROXY_HEADER")

        assert assignments == [(True, "")]

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_the_env_example_with_only_the_secrets_filled_in_cannot_start_a_real_deployment(
        self, environment: str
    ) -> None:
        """Copy the example, supply the secrets, change only APP_ENV: it must still refuse."""
        values: dict[str, Any] = {
            "_env_file": BACKEND_ENV_EXAMPLE,
            "APP_ENV": environment,
            "APP_SECRET_KEY": PRIVATE_KEY,
            "MFA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "PATIENT_OTP_SECRET": OTP_SECRET,
        }

        with pytest.raises(
            ValidationError, match="RATE_LIMIT_TRUST_PROXY_HEADER must be set explicitly"
        ):
            Settings(**values)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize(
        ("line", "starts_trusting"),
        [
            ("RATE_LIMIT_TRUST_PROXY_HEADER=", None),
            ('RATE_LIMIT_TRUST_PROXY_HEADER=""', None),
            ("RATE_LIMIT_TRUST_PROXY_HEADER=false", False),
            ("RATE_LIMIT_TRUST_PROXY_HEADER=true", True),
        ],
        ids=["uncommented-blank", "uncommented-empty-quotes", "false", "true"],
    )
    def test_uncommenting_the_line_in_a_copied_example_is_an_answer_only_with_a_value(
        self, tmp_path: Path, environment: str, line: str, starts_trusting: bool | None
    ) -> None:
        """The operator's next move: remove the ``#``. Blank must not pass for "false"."""
        shipped = BACKEND_ENV_EXAMPLE.read_text(encoding="utf-8")
        assert shipped.count("# RATE_LIMIT_TRUST_PROXY_HEADER=\n") == 1
        copied = tmp_path / ".env"
        copied.write_text(
            shipped.replace("# RATE_LIMIT_TRUST_PROXY_HEADER=\n", f"{line}\n"), encoding="utf-8"
        )
        values: dict[str, Any] = {
            "_env_file": copied,
            "APP_ENV": environment,
            "APP_SECRET_KEY": PRIVATE_KEY,
            "MFA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "PATIENT_OTP_SECRET": OTP_SECRET,
        }

        if starts_trusting is None:
            with pytest.raises(ValidationError, match="RATE_LIMIT_TRUST_PROXY_HEADER"):
                Settings(**values)
        else:
            assert Settings(**values).RATE_LIMIT_TRUST_PROXY_HEADER is starts_trusting


class TestFailureDurationFloor:
    """Attack: tell a throttled attempt from an evaluated one by how fast it comes back.

    A throttled sign-in does no password hashing; an evaluated one does. Only
    ``AUTH_FAILURE_MIN_SECONDS`` makes the two take the same time, and it
    also hides whether an address belongs to an account. Switching it off —
    or setting it too low to cover anything — is for development and test
    suites only.
    """

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("floor", [0, 0.0, 0.001, 0.1, 0.2, 0.2499, "0", "0.2"])
    def test_startup_fails_with_the_floor_below_the_minimum(
        self, environment: str, floor: Any
    ) -> None:
        with pytest.raises(ValidationError, match="AUTH_FAILURE_MIN_SECONDS must be at least"):
            _settings(
                APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_FAILURE_MIN_SECONDS=floor
            )

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("floor", ["0", "0.0", "0.1", "0.249"])
    def test_startup_fails_when_the_environment_switches_it_off(
        self, monkeypatch: pytest.MonkeyPatch, environment: str, floor: str
    ) -> None:
        """The way it would really happen: ``AUTH_FAILURE_MIN_SECONDS=0`` copied from a CI file."""
        monkeypatch.setenv("APP_ENV", environment)
        monkeypatch.setenv("APP_SECRET_KEY", PRIVATE_KEY)
        monkeypatch.setenv("AUTH_FAILURE_MIN_SECONDS", floor)

        with pytest.raises(ValidationError, match="AUTH_FAILURE_MIN_SECONDS must be at least"):
            _settings()

    @pytest.mark.parametrize("environment", ["staging", "production"])
    def test_the_refusal_names_the_minimum_and_the_environment(self, environment: str) -> None:
        with pytest.raises(ValidationError, match=f"at least 0.25 when APP_ENV is {environment}"):
            _settings(APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_FAILURE_MIN_SECONDS=0)

    @pytest.mark.parametrize("environment", ["staging", "production"])
    @pytest.mark.parametrize("floor", [0.25, 0.5, 1.0, 5.0])
    def test_it_starts_at_or_above_the_minimum(self, environment: str, floor: float) -> None:
        chosen = _settings(
            APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_FAILURE_MIN_SECONDS=floor
        ).AUTH_FAILURE_MIN_SECONDS

        assert chosen == floor

    @pytest.mark.parametrize("floor", [0, 0.0, 0.1, 0.2499, 0.25, 0.5])
    def test_development_may_lower_it_or_switch_it_off(self, floor: float) -> None:
        chosen = _settings(
            APP_ENV="development", AUTH_FAILURE_MIN_SECONDS=floor
        ).AUTH_FAILURE_MIN_SECONDS

        assert chosen == floor

    @pytest.mark.parametrize("environment", ["development", "staging", "production"])
    def test_the_default_is_safe_everywhere(self, environment: str) -> None:
        """Nobody has to remember to switch it on."""
        default = Settings.model_fields["AUTH_FAILURE_MIN_SECONDS"].default

        chosen = _settings(APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY).AUTH_FAILURE_MIN_SECONDS

        assert default >= MIN_AUTH_FAILURE_SECONDS
        assert chosen == default

    def test_the_minimum_is_the_reviewed_quarter_second(self) -> None:
        assert MIN_AUTH_FAILURE_SECONDS == 0.25

    @pytest.mark.parametrize("floor", [-0.001, -1, 5.001, 60])
    def test_a_negative_or_absurd_floor_is_refused_everywhere(self, floor: float) -> None:
        """Negative would make the padding arithmetic meaningless; minutes would be a self-DoS."""
        for environment in ("development", "production"):
            with pytest.raises(ValidationError):
                _settings(
                    APP_ENV=environment, APP_SECRET_KEY=PRIVATE_KEY, AUTH_FAILURE_MIN_SECONDS=floor
                )


class TestDevelopment:
    def test_an_unset_key_becomes_a_random_one(self) -> None:
        configured = _settings(APP_ENV="development")
        key = configured.APP_SECRET_KEY.get_secret_value()

        assert configured.secret_key_is_ephemeral is True
        assert len(key) >= MIN_SECRET_KEY_LENGTH
        assert key not in PUBLISHED_SECRET_KEYS

    def test_every_process_gets_a_different_random_key(self) -> None:
        first = _settings(APP_ENV="development").APP_SECRET_KEY.get_secret_value()
        second = _settings(APP_ENV="development").APP_SECRET_KEY.get_secret_value()

        assert first != second

    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    def test_a_published_key_is_never_used_even_in_development(self, published: str) -> None:
        """A deployment started in development mode by mistake is still not forgeable."""
        configured = _settings(APP_ENV="development", APP_SECRET_KEY=published)

        assert configured.APP_SECRET_KEY.get_secret_value() != published
        assert configured.APP_SECRET_KEY.get_secret_value() not in PUBLISHED_SECRET_KEYS
        assert configured.secret_key_is_ephemeral is True

    def test_a_private_key_is_kept(self) -> None:
        configured = _settings(APP_ENV="development", APP_SECRET_KEY=PRIVATE_KEY)

        assert configured.APP_SECRET_KEY.get_secret_value() == PRIVATE_KEY
        assert configured.secret_key_is_ephemeral is False

    def test_a_short_key_is_rejected_rather_than_used(self) -> None:
        with pytest.raises(ValidationError, match="at least"):
            _settings(APP_ENV="development", APP_SECRET_KEY="too-short")

    def test_the_default_environment_is_covered_too(self) -> None:
        """With neither APP_ENV nor a key set, the result is still a private key."""
        configured = _settings()

        assert configured.APP_SECRET_KEY.get_secret_value() not in PUBLISHED_SECRET_KEYS
        assert configured.APP_SECRET_KEY.get_secret_value() != ""


class TestTheKeyIsNeverPrinted:
    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    def test_a_rejected_published_key_is_not_in_the_error(self, published: str) -> None:
        with pytest.raises(ValidationError) as raised:
            _settings(APP_ENV="production", APP_SECRET_KEY=published)

        assert published not in str(raised.value)
        assert published not in repr(raised.value)

    def test_a_rejected_short_key_is_not_in_the_error(self) -> None:
        short = "short-but-secret-looking"
        with pytest.raises(ValidationError) as raised:
            _settings(APP_ENV="production", APP_SECRET_KEY=short)

        assert short not in str(raised.value)
        assert short not in repr(raised.value)

    def test_the_key_is_masked_when_settings_are_printed(self) -> None:
        configured = _settings(APP_ENV="production", APP_SECRET_KEY=PRIVATE_KEY)

        assert isinstance(configured.APP_SECRET_KEY, SecretStr)
        assert PRIVATE_KEY not in repr(configured)
        assert PRIVATE_KEY not in str(configured)
        assert PRIVATE_KEY not in str(configured.model_dump())

    def test_an_ephemeral_key_is_masked_too(self) -> None:
        configured = _settings(APP_ENV="development")

        assert configured.APP_SECRET_KEY.get_secret_value() not in repr(configured)


class TestNoKeyInTheRepositoryWorks:
    def test_there_is_no_default_key_in_code(self) -> None:
        default = Settings.model_fields["APP_SECRET_KEY"].default

        assert isinstance(default, SecretStr)
        assert default.get_secret_value() == ""

    def test_the_backend_env_example_ships_no_signing_key(self) -> None:
        """Attack: deploy by copying ``.env.example`` — and sign tokens with a public key.

        The example must offer nothing to copy: exactly one ``APP_SECRET_KEY=``
        line, with no value. A blank value stops staging and production from
        starting and gives development a random key (both proven above).
        """
        assignments = _env_assignments(BACKEND_ENV_EXAMPLE, "APP_SECRET_KEY")

        assert assignments == [(False, "")]

    def test_the_env_example_as_shipped_cannot_start_a_real_deployment(self) -> None:
        """Copy the example, change only APP_ENV: the application must refuse."""
        [(_, shipped)] = _env_assignments(BACKEND_ENV_EXAMPLE, "APP_SECRET_KEY")

        for environment in ("staging", "production"):
            with pytest.raises(ValidationError, match="APP_SECRET_KEY is required"):
                _settings(APP_ENV=environment, APP_SECRET_KEY=shipped)

    def test_no_env_example_ships_any_signing_key_even_commented_out(self) -> None:
        examples = [path for path in _repository_files() if Path(path).name.startswith(".env")]
        assert "backend/.env.example" in examples

        for example in examples:
            for commented, value in _env_assignments(REPO_ROOT / example, "APP_SECRET_KEY"):
                assert value == "", f"{example} ships an APP_SECRET_KEY (commented={commented})"

    def test_the_env_example_does_not_switch_the_device_cookie_off(self) -> None:
        """A copied example must not carry the development-only relaxation into production."""
        for _commented, value in _env_assignments(BACKEND_ENV_EXAMPLE, "AUTH_DEVICE_COOKIE_SECURE"):
            assert value.lower() not in {"false", "0", "no", "off"}

    def test_no_repository_file_contains_a_published_key(self) -> None:
        """Attack: find a working signing key by reading the repository.

        The published keys may appear in exactly two places — the denylist
        that refuses them and the tests that attack with them. Anywhere else
        (an example env file, a compose file, a seed script, documentation, a
        CI workflow) it is a key somebody will copy into a deployment.
        """
        files = _repository_files()
        assert len(files) > 100, "the repository listing is implausibly small"
        assert PUBLISHED_KEY_ALLOWED_FILE in files

        offenders: list[str] = []
        for relative in files:
            if relative == PUBLISHED_KEY_ALLOWED_FILE or relative.startswith(
                PUBLISHED_KEY_ALLOWED_PREFIX
            ):
                continue
            path = REPO_ROOT / relative
            if not path.is_file():
                continue  # deleted in the working tree, or a submodule
            content = path.read_bytes()
            if any(key.encode() in content for key in PUBLISHED_SECRET_KEYS):
                offenders.append(relative)

        assert offenders == []

    def test_the_denylist_itself_is_where_the_scan_expects_it(self) -> None:
        """If the allowed file stopped containing the key, the scan would prove nothing."""
        content = (REPO_ROOT / PUBLISHED_KEY_ALLOWED_FILE).read_text(encoding="utf-8")

        assert PUBLISHED_SECRET_KEYS
        assert all(key in content for key in PUBLISHED_SECRET_KEYS)

    def test_every_key_shipped_in_an_env_example_is_on_the_denylist(self) -> None:
        """Whatever ``.env.example`` offers as a signing key must be refused as one."""
        shipped: list[str] = []
        for example in (REPO_ROOT / ".env.example", REPO_ROOT / "backend" / ".env.example"):
            if not example.exists():
                continue
            for line in example.read_text().splitlines():
                match = re.match(r"\s*#?\s*APP_SECRET_KEY\s*=\s*\"?([^\"#\s]*)", line)
                if match and match.group(1):
                    shipped.append(match.group(1))

        assert all(value in PUBLISHED_SECRET_KEYS for value in shipped), (
            "an .env.example ships an APP_SECRET_KEY that is not in PUBLISHED_SECRET_KEYS"
        )

    @pytest.mark.parametrize("published", sorted(PUBLISHED_SECRET_KEYS))
    def test_a_token_forged_with_a_published_key_is_rejected(self, published: str) -> None:
        """The attack itself: sign a token with the key from the repository."""
        forged = _forged_token(published)

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_access_token(forged)

    def test_a_token_forged_with_a_guessed_key_is_rejected(self) -> None:
        with pytest.raises(pyjwt.InvalidTokenError):
            verify_access_token(_forged_token("x" * 48))

    def test_a_token_signed_by_this_process_is_still_accepted(self) -> None:
        """Existing token behaviour and claims are unchanged."""
        import uuid

        user_id, hospital_id = uuid.uuid4(), uuid.uuid4()
        token = create_access_token(
            user_id=user_id, hospital_id=hospital_id, roles=["Doctor"], permissions=["patient.read"]
        )

        claims = verify_access_token(token)

        assert claims["sub"] == str(user_id)
        assert claims["hospital_id"] == str(hospital_id)
        assert claims["type"] == "access"
        assert claims["roles"] == ["Doctor"]
        assert claims["permissions"] == ["patient.read"]
        assert pyjwt.get_unverified_header(token)["alg"] == "HS256"

    def test_the_running_settings_do_not_hold_a_published_key(self) -> None:
        assert settings.APP_SECRET_KEY.get_secret_value() not in PUBLISHED_SECRET_KEYS
