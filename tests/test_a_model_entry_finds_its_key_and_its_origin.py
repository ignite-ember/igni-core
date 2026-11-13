"""What a registry entry does with the key it names and the url it points at.

``resolve_key`` is the one place a key is found: the literal, the
environment variable, the command, igni server's token for its gateway,
in that order, and it says whether the answer is igni server's token
(which a client must read when it sends, not now). The command is run
without a shell, bounded in time, and may fail in any way without taking
the session down: a key that cannot be found is ``None``, and
``why_no_api_key`` is what the log then says.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from igni_core.core.config import model_entry
from igni_core.core.config.model_entry import (
    CLOUD_TOKEN_SENTINEL,
    ModelRegistryEntry,
    ResolvedApiKey,
)
from igni_core.core.paths import DEFAULT_DATA_DIR


def _entry(**fields) -> ModelRegistryEntry:
    fields.setdefault("model_id", "m")
    return ModelRegistryEntry.model_validate(fields)


def _python_cmd(code: str) -> str:
    return f"{sys.executable} -c {code!r}"


class TestTheDefaults:
    def test_a_row_with_only_a_model_id_is_an_openai_like_entry(self):
        entry = _entry()

        assert entry.provider == "openai_like"
        assert entry.timeout == 60
        assert entry.vision is False
        assert entry.prompt_caching is True
        assert entry.url is None
        assert entry.context_window is None

    def test_unknown_keys_are_kept_not_refused(self):
        entry = _entry(source="cloud", something_new=3)

        assert entry.source == "cloud"
        assert entry.something_new == 3

    def test_thinking_is_a_json_object_sent_unchanged(self):
        entry = _entry(thinking={"type": "adaptive", "budget": {"tokens": 100}})

        assert entry.thinking == {"type": "adaptive", "budget": {"tokens": 100}}
        assert _entry().thinking is None

    def test_the_published_schema_is_an_open_object(self):
        schema = ModelRegistryEntry.model_json_schema()

        assert schema["type"] == "object"
        assert "model_id" in schema["required"]


class TestContextWindowHint:
    @pytest.mark.parametrize(("given", "hint"), [(None, None), (0, None), (204_800, 204_800)])
    def test_only_a_positive_override_is_a_hint(self, given, hint):
        assert _entry(context_window=given).context_window_hint() == hint

    def test_a_number_written_as_text_is_an_int(self):
        assert _entry(context_window="32000").context_window_hint() == 32000


class TestResolvingTheKey:
    def test_the_sentinel_is_replaced_by_the_token_and_flagged(self):
        resolved = _entry(api_key=CLOUD_TOKEN_SENTINEL).resolve_key("tok")

        assert resolved == ResolvedApiKey(value="tok", cloud=True)

    def test_the_sentinel_without_a_token_is_still_flagged_cloud(self):
        resolved = _entry(api_key=CLOUD_TOKEN_SENTINEL).resolve_key(None)

        assert resolved.value is None
        assert resolved.cloud is True

    def test_a_literal_key_is_not_cloud(self):
        resolved = _entry(api_key="sk-1").resolve_key("tok")

        assert resolved == ResolvedApiKey(value="sk-1", cloud=False)

    def test_an_empty_literal_is_returned_as_written_and_skips_the_rest(self, monkeypatch):
        monkeypatch.setenv("K_ENV", "from-env")

        assert _entry(api_key="", api_key_env="K_ENV").resolve_api_key() == ""

    def test_the_gateway_url_falls_back_to_the_token(self):
        resolved = _entry(url="https://api.ignite-ember.sh/v1").resolve_key("tok")

        assert resolved == ResolvedApiKey(value="tok", cloud=True)

    def test_an_explicit_env_key_beats_the_gateway_token_and_is_not_cloud(self, monkeypatch):
        monkeypatch.setenv("K_ENV", "from-env")
        entry = _entry(url="https://api.ignite-ember.sh/v1", api_key_env="K_ENV")

        assert entry.resolve_key("tok") == ResolvedApiKey(value="from-env", cloud=False)

    def test_another_url_with_nothing_configured_has_no_key(self):
        assert _entry(url="https://example.com/v1").resolve_key("tok").value is None

    def test_a_set_but_empty_variable_falls_through_to_the_command(self, monkeypatch):
        monkeypatch.setenv("K_ENV", "")
        entry = _entry(api_key_env="K_ENV", api_key_cmd="echo from-cmd")

        assert entry.resolve_api_key() == "from-cmd"

    def test_resolve_api_key_is_the_value_of_resolve_key(self):
        entry = _entry(api_key=CLOUD_TOKEN_SENTINEL)

        assert entry.resolve_api_key("tok") == entry.resolve_key("tok").value

    def test_the_resolved_key_is_frozen(self):
        resolved = ResolvedApiKey(value="a")

        with pytest.raises(ValueError):
            resolved.value = "b"  # type: ignore[misc]


class TestTheKeyCommand:
    def test_it_is_run_without_a_shell(self, tmp_path):
        """A shell metacharacter is an argument, not an operator."""
        marker = tmp_path / "ran"
        entry = _entry(api_key_cmd=f"echo sk-1 ; touch {marker}")

        assert entry.resolve_api_key() == "sk-1 ; touch " + str(marker)
        assert not marker.exists()

    def test_it_must_exit_zero(self):
        entry = _entry(api_key_cmd=_python_cmd("print('sk-1'); raise SystemExit(3)"))

        assert entry.resolve_api_key() is None

    def test_blank_output_is_no_key(self):
        entry = _entry(api_key_cmd=_python_cmd("print('   ')"))

        assert entry.resolve_api_key() is None

    def test_a_command_that_hangs_gives_no_key_after_the_bound(self, monkeypatch):
        """The bound is 15 seconds; the test runs the real command with a
        shorter clock, and asserts the production bound is what is asked for."""
        asked: list[float] = []
        real_run = subprocess.run

        def quick(*args, **kwargs):
            asked.append(kwargs["timeout"])
            return real_run(*args, **{**kwargs, "timeout": 0.3})

        monkeypatch.setattr(model_entry.subprocess, "run", quick)
        entry = _entry(api_key_cmd=_python_cmd("import time; time.sleep(30)"))

        assert entry.resolve_api_key() is None
        assert asked == [15.0]


class TestWhyThereIsNoKey:
    def test_it_names_the_variable_and_the_command(self):
        entry = _entry(model_id="gpt", api_key_env="OPENAI_KEY", api_key_cmd="pass show openai")

        assert entry.why_no_api_key() == (
            "No API key for 'gpt': tried the environment variable 'OPENAI_KEY' "
            "(unset or empty), and the command 'pass show openai' "
            "(failed or returned nothing)."
        )

    def test_it_names_the_cloud_token_for_the_gateway(self):
        entry = _entry(model_id="g", url="https://api.ignite-ember.sh/v1")

        assert "the igni cloud token (not signed in)" in entry.why_no_api_key()

    def test_with_nothing_configured_it_points_at_the_real_config_file(self):
        message = _entry(model_id="g").why_no_api_key()

        assert f"{DEFAULT_DATA_DIR}/config.yaml" in message
        assert "{DEFAULT_DATA_DIR}" not in message
        assert "nothing was configured to find one" in message


class TestWhichUrlsAreIgniServer:
    @pytest.mark.parametrize(
        ("url", "matches"),
        [
            ("https://api.ignite-ember.sh/v1", True),
            ("https://dev-api.ignite-ember.sh/v1", True),
            # By design a substring: this only routes the person's own token,
            # and the origin check decides whether it is sent.
            ("https://evil.test/ignite-ember.sh", True),
            ("https://example.com/v1", False),
            (None, False),
            ("", False),
        ],
    )
    def test_matches_cloud_gateway_is_a_substring_check(self, url, matches):
        assert _entry(url=url).matches_cloud_gateway() is matches

    @pytest.mark.parametrize(
        ("url", "production"),
        [
            ("https://api.ignite-ember.sh/v1", True),
            ("http://API.ignite-ember.sh", True),
            ("https://dev-api.ignite-ember.sh/v1", False),
            ("https://evil.test/api.ignite-ember.sh", False),
            ("https://api.ignite-ember.sh.evil.test/v1", False),
            ("", False),
            ("not a url", False),
            ("http://[::1", False),
        ],
    )
    def test_is_cloud_gateway_url_is_the_exact_production_host(self, url, production):
        assert ModelRegistryEntry.is_cloud_gateway_url(url) is production


class TestSameOrigin:
    @pytest.mark.parametrize(
        ("url", "server", "same"),
        [
            ("HTTPS://Igni.Example/v1", "https://igni.example", True),
            ("http://igni.example:80/x", "http://igni.example", True),
            ("https://user:pw@igni.example/v1", "https://igni.example", True),
            ("https://igni.example:8443/v1", "https://igni.example", False),
            ("https://igni.example:8443", "https://igni.example:8443/other", True),
            ("http://igni.example", "https://igni.example", False),
            ("https://other.example", "https://igni.example", False),
            ("igni.example/v1", "https://igni.example", False),
            ("https://igni.example", "", False),
            ("", "https://igni.example", False),
            ("https://igni.example:99999/v1", "https://igni.example", False),
            ("https://[::1/v1", "https://igni.example", False),
            ("file:///etc/passwd", "file:///etc/passwd", False),
        ],
    )
    def test_scheme_host_and_port_decide(self, url, server, same):
        assert ModelRegistryEntry.same_origin(url, server) is same


class TestWhoNeedsTheToken:
    @pytest.mark.parametrize(
        ("fields", "needs"),
        [
            ({"api_key": CLOUD_TOKEN_SENTINEL}, True),
            ({"api_key": CLOUD_TOKEN_SENTINEL, "url": "https://example.com"}, True),
            ({"url": "https://api.ignite-ember.sh/v1"}, True),
            ({"url": "https://api.ignite-ember.sh/v1", "api_key": "sk"}, False),
            ({"url": "https://api.ignite-ember.sh/v1", "api_key": ""}, False),
            ({"url": "https://api.ignite-ember.sh/v1", "api_key_env": "X"}, False),
            ({"url": "https://api.ignite-ember.sh/v1", "api_key_cmd": "x"}, False),
            ({"url": "https://example.com/v1"}, False),
            ({}, False),
        ],
    )
    def test_the_sentinel_or_a_bare_gateway_url(self, fields, needs):
        assert _entry(**fields).needs_cloud_token() is needs


def test_a_cloud_discovered_entry_goes_through_the_proxy_with_the_sentinel():
    entry = ModelRegistryEntry.from_cloud_discovery(
        model_id="MiniMax", proxy_url="https://igni.example/v1"
    )

    assert entry.provider == "openai_like"
    assert entry.model_id == "MiniMax"
    assert entry.url == "https://igni.example/v1"
    assert entry.api_key == CLOUD_TOKEN_SENTINEL
    assert entry.source == "cloud"
    assert entry.resolve_key("tok") == ResolvedApiKey(value="tok", cloud=True)
