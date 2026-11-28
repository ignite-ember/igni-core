"""A secret is never read without the person's yes, whatever the mode; an ordinary read in Bypass runs unasked.

Each call goes through what a session builds for it: the tool's approval
pause (:class:`ToolPermissions`), then the permission pipeline with Bypass's
guard on its rules. igni's own config, credentials and settings in the home
are secrets; the rest of ``~/.igni`` and a project's own ``.igni/`` are not.
"""

from __future__ import annotations

import asyncio
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.config.schemas.safety import BypassGuardConfig, SafetyConfig
from igni_core.core.config.tool_permissions import ToolPermissions
from igni_core.core.hooks.bypass_guard import NOT_RUN, BypassGuard
from igni_core.core.hooks.bypass_judge import HarmJudge, HarmScore
from igni_core.core.hooks.bypass_rules import flag_file_read, flag_shell
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.permission.context import ToolCallContext
from igni_core.core.hooks.permission.pipeline import PermissionPipeline
from igni_core.core.hooks.secret_files import SecretFiles
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.paths import CONFIG_DIR, LEGACY_CONFIG_DIR

REASON = "reads a credential or secret file"

SHELL = "run_shell_command"
SHELL_REGISTRY_NAME = "Bash"
"""The name the shell tool is built under, which decides its approval pause."""

MODES = [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.BYPASS_PERMISSIONS]

SECRET_READS = [
    "cat .env",
    "head -n 3 .env",
    "less ./.env",
    "grep KEY .env",
    "base64 .env",
    "cp .env /tmp/x",
    "cat ~/.igni/credentials.json",
    "cat ~/.igni/config.yaml",
    "tail $HOME/.igni/config.yml",
    'cat "${{HOME}}/.igni/settings.json"',
    "cat {home}/.igni/settings.local.json",
    "cat ~/.igni/config.yaml.bak-1788640457",
    "cat ~/.ember/credentials.json",
    "cat ~/.ember/config.yaml",
    "grep -r api_key ~/.igni",
    "rg sk- ~/.igni/",
    "tar czf /tmp/x.tgz ~/.igni",
    "cat ~/.igni/logs/../config.yaml",
    "cat ~/.ssh/id_ed25519",
    "cat ~/.aws/credentials",
]

ORDINARY_READS = [
    "cat src/main.py",
    "cat .env.example",
    "cat .igni/config.yaml",
    "cat .igni/settings.json",
    "cat ~/.igni/debug.log",
    "tail -n 50 ~/.igni/logs/backend.log",
    "ls ~/.igni",
    "cat ~/.igni/agents/reviewer.md",
    "cat ~/config.yaml",
    "cat ~/other/.igni/config.yaml",
    "grep -r TODO src",
]


class Decision(BaseModel):
    """What happened to one tool call before it could run."""

    paused: bool
    """Agno stopped the call for the person's approval before any hook saw it."""
    asked: list[str]
    """What Bypass's guard put to the person."""
    message: str | None
    """What the model reads instead of the result, when the call did not run."""

    @property
    def ran_unasked(self) -> bool:
        """True when the call ran with nobody asked."""
        return not self.paused and not self.asked and self.message is None


class Session:
    """The approval pause and the permission pipeline one session in ``mode`` builds."""

    def __init__(self, mode: PermissionMode, project: Path, judge: HarmJudge | None = None) -> None:
        self.asked: list[str] = []
        self.permissions = ToolPermissions(
            project_dir=project, settings_permissions=PermissionsConfig(mode=mode)
        )
        evaluator = PermissionEvaluator.from_strings(mode=mode)
        guard = BypassGuard(
            config=BypassGuardConfig(use_model=judge is not None, explain=False),
            project_dir=project,
            in_bypass=lambda: evaluator.mode is PermissionMode.BYPASS_PERMISSIONS,
            ask=self._ask,
            tool_hints=ToolHintsRegistry(),
            judge=judge,
        )
        safety = SafetyConfig()
        self.pipeline = PermissionPipeline.build(
            HookFirer(HookExecutor({}), "sess-1"),
            safety.protected_paths,
            safety.blocked_commands,
            evaluator,
            bypass_guard=guard,
        )

    async def _ask(self, name: str, args: dict[str, Any], prompt: str, *_: Any, **__: Any) -> bool:
        self.asked.append(prompt)
        return False

    def shell(self, command: str) -> Decision:
        """The command's fate: paused for approval, asked about by the guard, refused, or run."""
        self.asked = []
        if self.permissions.needs_confirmation(SHELL_REGISTRY_NAME):
            return Decision(paused=True, asked=[], message=None)
        args = {"command": command.format(home=Path.home())}
        message = asyncio.run(self.pipeline.evaluate(ToolCallContext.for_check(SHELL, args)))
        return Decision(paused=False, asked=self.asked, message=message)


class AlarmedJudge(HarmJudge):
    """A safety model that calls every call it is shown harmful."""

    async def score(self, text: str) -> HarmScore:
        return HarmScore(probability=1.0, question="destructive")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


# ── every mode: a secret never runs unasked ─────────────────────────


@pytest.mark.parametrize("mode", MODES, ids=lambda mode: mode.value)
@pytest.mark.parametrize("command", SECRET_READS)
def test_a_shell_read_of_a_secret_never_runs_unasked(mode, command, project) -> None:
    decision = Session(mode, project).shell(command)

    assert not decision.ran_unasked


@pytest.mark.parametrize("command", SECRET_READS)
def test_bypass_puts_a_secret_read_to_the_person_with_the_secret_reason(command, project) -> None:
    decision = Session(PermissionMode.BYPASS_PERMISSIONS, project).shell(command)

    assert not decision.paused
    assert len(decision.asked) == 1 and REASON in decision.asked[0]
    assert decision.message == f"{NOT_RUN} the user rejected this call when asked to approve it."


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS])
def test_outside_bypass_the_shell_pauses_for_the_person_before_a_secret_read(mode, project) -> None:
    decision = Session(mode, project).shell("cat .env")

    assert decision.paused


# ── Bypass: an ordinary read runs, and nobody is asked ──────────────


@pytest.mark.parametrize("command", ORDINARY_READS)
def test_bypass_runs_an_ordinary_read_without_asking(command, project) -> None:
    decision = Session(PermissionMode.BYPASS_PERMISSIONS, project).shell(command)

    assert decision.ran_unasked


# ── igni's own stores, as the read check sees a path ────────────────


@pytest.mark.parametrize("folder", [CONFIG_DIR, LEGACY_CONFIG_DIR])
@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.yml",
        "credentials.json",
        "settings.json",
        "settings.local.json",
        "config.yaml.bak-before-key-swap-1791733621",
        "credentials.json.bak-real-1795429960",
        "Config.YAML",
    ],
)
def test_igni_s_own_store_in_the_home_is_a_secret(folder, name) -> None:
    for spelt in (f"~/{folder}/{name}", f"$HOME/{folder}/{name}", f"{Path.home()}/{folder}/{name}"):
        assert flag_file_read(spelt) == REASON, spelt


@pytest.mark.parametrize(
    "path",
    [
        "~/.igni",
        "~/.igni/",
        "$HOME/.ember",
        "${{HOME}}/.igni",
        "{home}/.igni",
        "{home}\\.igni\\config.yaml",
    ],
)
def test_igni_s_own_folder_in_the_home_is_read_as_its_stores(path) -> None:
    assert flag_file_read(path.format(home=Path.home())) == REASON


@pytest.mark.parametrize(
    "path",
    [
        "~/.igni/debug.log",
        "~/.igni/client_state.db",
        "~/.igni/agents",
        "~/.igni/agents/config.yaml",
        "~/.igni/hooks/settings.json",
        "~/config.yaml",
        "~/settings.json",
        "~/projects/app/.igni/config.yaml",
        "~/projects/app/.igni/settings.json",
        ".igni/config.yaml",
        ".igni/settings.local.json",
        "/elsewhere/.igni/config.yaml",
        "~/.igniter/config.yaml",
        "~/.igni/configuration.md",
        "$HOMEWORK/.igni/config.yaml",
    ],
)
def test_the_rest_of_igni_s_folder_and_a_project_s_own_config_are_ordinary(path) -> None:
    assert flag_file_read(path) is None


def test_igni_s_own_stores_are_one_table_the_shell_and_the_file_check_share() -> None:
    assert SecretFiles.OWN_FOLDERS == (CONFIG_DIR, LEGACY_CONFIG_DIR)
    assert flag_shell("cat ~/.igni/config.yaml", None) == REASON
    assert flag_shell("cat ~/.igni/debug.log", None) is None


OWN_STORE_EXAMPLES = [
    ("config.yaml*", "config.yaml.bak"),
    ("config.yml*", "config.yml"),
    ("credentials.json*", "credentials.json.old"),
    ("settings*.json*", "settings.local.json"),
]


def test_every_own_store_entry_has_an_example_here() -> None:
    assert [glob for glob, _ in OWN_STORE_EXAMPLES] == list(SecretFiles.OWN_STORES)


@pytest.mark.parametrize(("glob", "name"), OWN_STORE_EXAMPLES)
def test_an_own_store_is_caught_and_its_example_is_its_own(glob, name) -> None:
    assert [g for g in SecretFiles.OWN_STORES if fnmatchcase(name, g)] == [glob]
    assert flag_file_read(f"~/.igni/{name}") == REASON


# ── the notebook reader ─────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["notebook_read", "notebook_read_cell"])
def test_bypass_asks_before_a_notebook_read_of_a_secret_path(tool, project) -> None:
    session = Session(PermissionMode.BYPASS_PERMISSIONS, project)
    args = {"file_path": f"{Path.home()}/.ssh/keys.ipynb", "cell_index": 0}

    message = asyncio.run(session.pipeline.evaluate(ToolCallContext.for_check(tool, args)))

    assert len(session.asked) == 1 and REASON in session.asked[0]
    assert message is not None and message.startswith(NOT_RUN)


@pytest.mark.parametrize("tool", ["notebook_read", "notebook_read_cell"])
def test_bypass_runs_an_ordinary_notebook_read_without_asking(tool, project) -> None:
    session = Session(PermissionMode.BYPASS_PERMISSIONS, project, judge=AlarmedJudge())
    args = {"file_path": "analysis.ipynb", "cell_index": 0}

    assert asyncio.run(session.pipeline.evaluate(ToolCallContext.for_check(tool, args))) is None
    assert session.asked == []


def test_a_home_spelt_with_capitals_still_holds_igni_s_stores(tmp_path, monkeypatch) -> None:
    home = tmp_path / "Users" / "Ada"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    assert flag_file_read(f"{home}/.igni/config.yaml") == REASON
    assert flag_shell("cat ~/.igni/credentials.json", None) == REASON
