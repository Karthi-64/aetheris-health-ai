"""Unit tests for the AI settings.

Every ``Settings`` here passes ``_env_file=None``: ``backend/.env`` holds the
developer's real key and pytest runs from ``backend/``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.tests.ai_fakes import FAKE_GROQ_KEY, contains_any


def _settings(**values: Any) -> Settings:
    # `_env_file` is a pydantic-settings init argument, not a declared field.
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class TestDefaults:
    def test_ai_is_off_by_default(self) -> None:
        settings = _settings()

        assert settings.GROQ_API_KEY is None
        assert settings.AI_ENABLED is None
        assert settings.AI_FAST_MODEL is None
        assert settings.GROQ_BASE_URL == "https://api.groq.com/openai/v1"
        assert settings.AI_REQUEST_TIMEOUT_SECONDS == 8.0
        assert settings.AI_MAX_CONCURRENT_CALLS == 4

    def test_strict_json_schema_is_on_by_default_and_can_be_turned_off(self) -> None:
        assert _settings().GROQ_STRICT_JSON_SCHEMA is True
        assert _settings(GROQ_STRICT_JSON_SCHEMA="false").GROQ_STRICT_JSON_SCHEMA is False


class TestApiKey:
    def test_key_is_a_secret_that_does_not_print(self) -> None:
        settings = _settings(GROQ_API_KEY=FAKE_GROQ_KEY)

        assert isinstance(settings.GROQ_API_KEY, SecretStr)
        value_ok = settings.GROQ_API_KEY.get_secret_value() == FAKE_GROQ_KEY
        assert value_ok
        printed = contains_any(
            [
                repr(settings),
                str(settings),
                repr(settings.GROQ_API_KEY),
                str(settings.GROQ_API_KEY),
            ],
            [FAKE_GROQ_KEY],
        )
        assert printed is False
        dumped = contains_any(
            [repr(settings.model_dump()), settings.model_dump_json()], [FAKE_GROQ_KEY]
        )
        assert dumped is False

    @pytest.mark.parametrize("blank", ["", " ", "   \t", "\n"])
    def test_blank_key_is_unset(self, blank: str) -> None:
        assert _settings(GROQ_API_KEY=blank).GROQ_API_KEY is None

    def test_key_is_stripped(self) -> None:
        settings = _settings(GROQ_API_KEY=f"  {FAKE_GROQ_KEY}\n")

        assert settings.GROQ_API_KEY is not None
        stripped_ok = settings.GROQ_API_KEY.get_secret_value() == FAKE_GROQ_KEY
        assert stripped_ok

    def test_an_odd_key_does_not_raise_at_settings_time(self) -> None:
        """A validation error would print the value; the runtime rejects it instead."""
        settings = _settings(GROQ_API_KEY="has a space")

        assert settings.GROQ_API_KEY is not None

    def test_key_comes_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)

        settings = _settings()

        assert settings.GROQ_API_KEY is not None
        env_ok = settings.GROQ_API_KEY.get_secret_value() == FAKE_GROQ_KEY
        assert env_ok


class TestKillSwitch:
    @pytest.mark.parametrize("blank", ["", "  "])
    def test_empty_value_is_unset_not_a_parse_error(self, blank: str) -> None:
        assert _settings(AI_ENABLED=blank).AI_ENABLED is None

    @pytest.mark.parametrize(("raw", "expected"), [("false", False), ("true", True), ("0", False)])
    def test_boolean_values(self, raw: str, expected: bool) -> None:
        assert _settings(AI_ENABLED=raw).AI_ENABLED is expected


class TestFastModel:
    @pytest.mark.parametrize(
        "model",
        ["openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen2.5:14b", "a", "meta/llama_4.1"],
    )
    def test_accepts_model_ids(self, model: str) -> None:
        actual = _settings(AI_FAST_MODEL=f" {model} ").AI_FAST_MODEL
        assert actual == model

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_is_unset(self, blank: str) -> None:
        assert _settings(AI_FAST_MODEL=blank).AI_FAST_MODEL is None

    @pytest.mark.parametrize(
        "model", ["has space", "-leading-dash", "semi;colon", "new\nline", "x" * 101, 'quo"te']
    )
    def test_rejects_odd_shapes(self, model: str) -> None:
        with pytest.raises(ValidationError):
            _settings(AI_FAST_MODEL=model)


class TestBounds:
    @pytest.mark.parametrize("value", [0, -1, 30.5, 600])
    def test_timeout_bounds(self, value: float) -> None:
        with pytest.raises(ValidationError):
            _settings(AI_REQUEST_TIMEOUT_SECONDS=value)

    @pytest.mark.parametrize("value", [0.01, 8, 30])
    def test_timeout_accepts_the_range(self, value: float) -> None:
        actual = _settings(AI_REQUEST_TIMEOUT_SECONDS=value).AI_REQUEST_TIMEOUT_SECONDS
        assert actual == value

    @pytest.mark.parametrize("value", [0, -3, 65])
    def test_concurrency_bounds(self, value: int) -> None:
        with pytest.raises(ValidationError):
            _settings(AI_MAX_CONCURRENT_CALLS=value)

    @pytest.mark.parametrize("value", [1, 4, 64])
    def test_concurrency_accepts_the_range(self, value: int) -> None:
        actual = _settings(AI_MAX_CONCURRENT_CALLS=value).AI_MAX_CONCURRENT_CALLS
        assert actual == value


class TestBaseUrl:
    """The bearer key must never go over plain HTTP to a remote host."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1"),
            ("https://api.groq.com/openai/v1/", "https://api.groq.com/openai/v1"),
            ("  https://api.groq.com/openai/v1//  ", "https://api.groq.com/openai/v1"),
            ("http://localhost:8080/v1", "http://localhost:8080/v1"),
            ("http://127.0.0.1:9/v1", "http://127.0.0.1:9/v1"),
            ("http://[::1]:9/v1", "http://[::1]:9/v1"),
        ],
    )
    def test_accepts(self, raw: str, expected: str) -> None:
        actual = _settings(GROQ_BASE_URL=raw).GROQ_BASE_URL
        assert actual == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "http://example.com",
            "http://localhost.example.com",
            "http://127.0.0.1.example.com",
            "http://localhost@example.com",
            "https://user:pw@api.groq.com/openai/v1",
            "https://user@api.groq.com/openai/v1",
            "https://api.groq.com/openai/v1?x=1",
            "https://api.groq.com/openai/v1?",
            "https://api.groq.com/openai/v1#f",
            "ftp://api.groq.com",
            "https://",
            "api.groq.com/openai/v1",
            "",
            "http://[::1",
        ],
    )
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ValidationError) as caught:
            _settings(GROQ_BASE_URL=raw, GROQ_API_KEY=FAKE_GROQ_KEY)

        assert "GROQ_BASE_URL" in str(caught.value)
        key_printed = contains_any([str(caught.value), repr(caught.value)], [FAKE_GROQ_KEY])
        assert key_printed is False

    @pytest.mark.parametrize(
        "raw",
        [
            "https://user:PW-MARKER-3e9d@api.groq.com/openai/v1",
            "http://PW-MARKER-3e9d@example.com/v1",
            "https://api.groq.com/openai/v1?token=PW-MARKER-3e9d",
        ],
    )
    def test_a_rejected_url_is_not_printed(self, raw: str) -> None:
        """A credential mistakenly put in the URL must not reach the startup traceback."""
        with pytest.raises(ValidationError) as caught:
            _settings(GROQ_BASE_URL=raw)

        assert "GROQ_BASE_URL" in str(caught.value)
        assert "must be an https URL" in str(caught.value)
        printed = contains_any([str(caught.value), repr(caught.value)], ["PW-MARKER-3e9d"])
        assert printed is False

    def test_other_rejected_settings_are_not_printed_either(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _settings(AI_FAST_MODEL="bad model PW-MARKER-3e9d")

        assert "AI_FAST_MODEL" in str(caught.value)
        printed = contains_any([str(caught.value), repr(caught.value)], ["PW-MARKER-3e9d"])
        assert printed is False
