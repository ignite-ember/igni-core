"""``/config``: the auth line, the guardrail line and the enabled/disabled words.

``ConfigView.from_session`` reads a real credentials file; what the person is
told about their sign-in depends on that file alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_config import ConfigView
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.schemas.storage_and_paths import StorageConfig


def _session(
    *,
    pii: bool = False,
    injection: bool = False,
    moderation: bool = False,
    learning: bool = True,
    reasoning: bool = True,
    data_dir: str = "/data/igni",
) -> SimpleNamespace:
    storage = StorageConfig()
    storage.data_dir = data_dir
    settings = SimpleNamespace(
        models=SimpleNamespace(default="model-x"),
        permissions=SimpleNamespace(file_write="ask", shell_execute="deny"),
        storage=storage,
        learning=SimpleNamespace(enabled=learning),
        reasoning=SimpleNamespace(enabled=reasoning),
        guardrails=SimpleNamespace(
            pii_detection=pii, prompt_injection=injection, moderation=moderation
        ),
        orchestration=SimpleNamespace(max_total_agents=7, max_nesting_depth=4),
    )
    return SimpleNamespace(settings=settings, session_id="sess-9")


def _store(tmp_path, creds: Credentials | None) -> CredentialsStore:
    store = CredentialsStore(str(tmp_path / "credentials.json"))
    if creds is not None:
        store.save(creds)
    return store


def _line(view: ConfigView, label: str) -> str:
    for line in view.to_command_result().content.splitlines():
        if line.startswith(f"- **{label}:**"):
            return line.split(":** ", 1)[1]
    raise AssertionError(f"no {label} line")


class TestTheAuthLine:
    def test_a_valid_login_shows_its_email(self, tmp_path):
        store = _store(tmp_path, Credentials.new("tok", "ada@example.com"))

        view = ConfigView.from_session(_session(), store=store)

        assert view.auth_status == "ada@example.com"
        assert _line(view, "Auth") == "ada@example.com"

    def test_a_valid_login_without_an_email_says_logged_in(self, tmp_path):
        store = _store(tmp_path, Credentials.new("tok", ""))

        assert ConfigView.from_session(_session(), store=store).auth_status == "logged in"

    def test_an_expired_login_is_not_logged_in(self, tmp_path):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        creds = Credentials(access_token="tok", email="ada@example.com", expires_at=past)

        view = ConfigView.from_session(_session(), store=_store(tmp_path, creds))

        assert view.auth_status == "not logged in"

    def test_no_credentials_file_is_not_logged_in(self, tmp_path):
        view = ConfigView.from_session(_session(), store=_store(tmp_path, None))

        assert view.auth_status == "not logged in"

    def test_a_credentials_file_that_cannot_be_read_is_not_logged_in(self, tmp_path):
        store = _store(tmp_path, None)
        store.path.write_text("{not json")

        assert ConfigView.from_session(_session(), store=store).auth_status == "not logged in"


class TestTheGuardrailsLine:
    @pytest.mark.parametrize(
        ("pii", "injection", "moderation", "shown"),
        [
            (False, False, False, "(none)"),
            (True, False, False, "PII"),
            (False, True, False, "injection"),
            (False, False, True, "moderation"),
            (True, True, False, "PII injection"),
            (True, False, True, "PII moderation"),
            (False, True, True, "injection moderation"),
            (True, True, True, "PII injection moderation"),
        ],
    )
    def test_each_combination(self, tmp_path, pii, injection, moderation, shown):
        view = ConfigView.from_session(
            _session(pii=pii, injection=injection, moderation=moderation),
            store=_store(tmp_path, None),
        )

        assert _line(view, "Guardrails").strip() == shown


class TestTheRestOfTheBlock:
    def test_learning_and_reasoning_off_say_disabled(self, tmp_path):
        view = ConfigView.from_session(
            _session(learning=False, reasoning=False), store=_store(tmp_path, None)
        )

        assert _line(view, "Learning") == "disabled"
        assert _line(view, "Reasoning tools") == "disabled"

    def test_learning_and_reasoning_on_say_enabled(self, tmp_path):
        view = ConfigView.from_session(_session(), store=_store(tmp_path, None))

        assert _line(view, "Learning") == "enabled"
        assert _line(view, "Reasoning tools") == "enabled"

    def test_the_settings_reach_the_block(self, tmp_path):
        view = ConfigView.from_session(_session(), store=_store(tmp_path, None))

        assert _line(view, "Model") == "model-x"
        assert _line(view, "Permissions") == "file_write=ask, shell=deny"
        assert _line(view, "Max agents") == "7"
        assert _line(view, "Max depth") == "4"
        assert _line(view, "Session") == "sess-9"

    def test_the_storage_line_is_the_data_directory(self, tmp_path):
        view = ConfigView.from_session(
            _session(data_dir="/srv/igni-data"), store=_store(tmp_path, None)
        )

        assert _line(view, "Storage") == "/srv/igni-data"

    def test_it_is_a_markdown_block(self, tmp_path):
        view = ConfigView.from_session(_session(), store=_store(tmp_path, None))

        result = view.to_command_result()

        assert result.content.startswith("## Configuration\n")
        assert result.kind == "markdown"
