"""``read_document`` reads only what the model's other file access may: the project, never a key or igni's own settings, under every mode's rules."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.permission_eval.schemas import PermissionDecision
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.config.schemas.safety import BypassGuardConfig
from igni_core.core.config.tool_permissions import ToolNameResolver, ToolPermissions
from igni_core.core.evals.tool_names import ToolNameCatalog
from igni_core.core.hooks.bypass_guard import READ_ONLY_TOOLS, BypassGuard
from igni_core.core.hooks.bypass_tool_kind import ToolKind
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.session.agent_builder.main_tools import MainToolSet
from igni_core.core.tools.orchestrate_sandbox import SpawnSandbox
from igni_core.core.tools.read_document import ReadDocumentTools
from igni_core.core.tools.registry import ToolRegistry
from igni_core.core.tools.tool_spec import ToolSpecCatalog
from igni_core.protocol.tool_friendly_names import ToolFriendlyNames
from tests.document_fixtures import DocumentFixtures

AGENTS = Path(__file__).resolve().parent.parent / "src/igni_core/bundled_agents"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path.resolve()


def _read(project: Path, path: str | Path, part: str = "") -> str:
    tool = ReadDocumentTools(base_dir=str(project))
    return asyncio.run(tool.read_document(str(path), part))


def _outside(path: Path) -> str:
    return f"[Could not read {path}: it is outside the project. Ask the user to attach it.]"


def _protected(path: Path) -> str:
    return f"[Could not read {path}: it holds credentials, keys or igni's own settings.]"


# ── the project is the boundary ─────────────────────────────────────


def test_a_document_outside_the_project_is_not_read(project, tmp_path):
    elsewhere = DocumentFixtures.pdf(tmp_path / "elsewhere" / "secret.pdf", [["do not read"]])

    result = _read(project, elsewhere)

    assert result == _outside(elsewhere.resolve())
    assert "do not read" not in result


def test_climbing_out_of_the_project_is_outside_it(project, tmp_path):
    escaped = DocumentFixtures.pdf(tmp_path / "secret.pdf", [["do not read"]])

    assert _read(project, "../secret.pdf") == _outside(escaped.resolve())


def test_a_link_inside_the_project_to_a_file_outside_it_is_outside_it(project, tmp_path):
    target = DocumentFixtures.pdf(tmp_path / "secret.pdf", [["do not read"]])
    (project / "notes.pdf").symlink_to(target)

    assert _read(project, "notes.pdf") == _outside(target.resolve())


def test_a_link_inside_the_project_to_a_file_inside_it_is_read(project):
    DocumentFixtures.pdf(project / "real.pdf", [["inside"]])
    (project / "alias.pdf").symlink_to(project / "real.pdf")

    assert "--- Page 1 ---\ninside" in _read(project, "alias.pdf")


def test_an_outside_path_is_refused_whether_or_not_it_exists(project, tmp_path):
    assert _read(project, tmp_path / "nothing.pdf") == _outside(tmp_path.resolve() / "nothing.pdf")


def test_a_home_relative_path_inside_the_project_is_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "work"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    DocumentFixtures.csv_commas(project / "people.csv")

    assert "| Anna | Kyiv |" in _read(project.resolve(), "~/work/people.csv")


def test_an_absolute_path_inside_the_project_is_read(project):
    (project / "sub").mkdir()
    path = DocumentFixtures.csv_commas(project / "sub" / "people.csv")

    assert "| Anna | Kyiv |" in _read(project, path)


def test_an_uploaded_attachment_in_the_project_s_igni_folder_is_read(project):
    upload = DocumentFixtures.pdf(project / ".igni" / "attachments" / "s1" / "cv.pdf", [["cv"]])

    assert f"[Text extracted from {upload} (1 page):]" in _read(project, upload)


def test_a_project_given_relative_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    DocumentFixtures.csv_commas(tmp_path / "rel" / "people.csv")
    tool = ReadDocumentTools(base_dir="rel")

    assert "| Anna | Kyiv |" in asyncio.run(tool.read_document("people.csv"))


# ── keys and igni's own settings ────────────────────────────────────


@pytest.mark.parametrize(
    "relative",
    [
        ".igni/config.yaml",
        ".igni/config.yml",
        ".igni/config.yaml.bak-2026",
        ".igni/settings.json",
        ".igni/settings.local.json",
        ".igni/credentials.json",
        ".ember/config.yaml",
        ".ember/credentials.json.old",
        "keys/id_rsa",
        "deploy/server.pem",
        ".netrc",
        ".ssh/known_hosts",
    ],
)
def test_a_key_or_igni_settings_file_in_the_project_is_not_read(project, relative):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("api_key: sk-secret\n", encoding="utf-8")

    result = _read(project, relative)

    assert result == _protected(path)
    assert "sk-secret" not in result


def test_igni_s_home_config_is_not_read_even_when_the_project_is_home(tmp_path):
    home = tmp_path.resolve()
    config = home / ".igni" / "config.yaml"
    config.parent.mkdir()
    config.write_text("api_key: sk-secret\n", encoding="utf-8")

    assert _read(home, "~/.igni/config.yaml".replace("~", str(home))) == _protected(config)


def test_a_secret_outside_the_project_is_named_a_secret(project, tmp_path):
    key = tmp_path / ".ssh" / "id_rsa"

    assert _read(project, key) == _protected(key.resolve())


def test_a_document_named_like_a_document_but_linked_to_igni_s_config_is_not_read(project):
    config = project / ".igni" / "config.yaml"
    config.parent.mkdir()
    config.write_text("api_key: sk-secret\n", encoding="utf-8")
    (project / "notes.pdf").symlink_to(config)

    assert _read(project, "notes.pdf") == _protected(config)


def test_a_link_named_like_igni_s_config_is_not_read_whatever_it_points_at(project):
    DocumentFixtures.pdf(project / "real.pdf", [["text"]])
    (project / ".igni").mkdir()
    (project / ".igni" / "config.yaml").symlink_to(project / "real.pdf")

    assert _read(project, ".igni/config.yaml") == _protected(project / "real.pdf")


@pytest.mark.parametrize(
    "relative",
    [".igni/notes.csv", "config.yaml.csv", ".igni/logs/backend.csv", "igni/config.yaml.csv"],
)
def test_other_files_near_igni_s_settings_are_ordinary(project, relative):
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a,b\n1,2\n", encoding="utf-8")

    assert "| a | b |\n|---|---|\n| 1 | 2 |" in _read(project, relative)


def test_a_config_file_outside_igni_s_folders_is_plain_text(project):
    (project / "settings").mkdir()
    (project / "settings" / "config.yaml").write_text("a: 1\n", encoding="utf-8")

    assert _read(project, "settings/config.yaml") == (
        f"[{project / 'settings' / 'config.yaml'} is plain text: read it with the shell.]"
    )


# ── the worktree sandbox moves the boundary ─────────────────────────


def test_a_worktree_sub_agent_reads_inside_its_worktree_and_not_the_main_checkout(tmp_path):
    main = (tmp_path / "main").resolve()
    worktree = (tmp_path / "worktree").resolve()
    main.mkdir()
    worktree.mkdir()
    DocumentFixtures.csv_commas(main / "main.csv")
    DocumentFixtures.csv_commas(worktree / "tree.csv")
    agent = SimpleNamespace(tools=[ReadDocumentTools(base_dir=str(main))])

    originals = SpawnSandbox.rebind_tool_base_dirs(agent, worktree)
    function = agent.tools[0].async_functions["read_document"]

    assert list(originals.values()) == [main]
    assert "| Anna | Kyiv |" in asyncio.run(function.entrypoint(path="tree.csv"))
    assert asyncio.run(function.entrypoint(path=str(main / "main.csv"))) == _outside(
        main / "main.csv"
    )


# ── how the tool is attached ────────────────────────────────────────


def _toolkit(registry: ToolRegistry) -> ReadDocumentTools:
    (toolkit,) = registry.resolve(["ReadDocument"])
    assert isinstance(toolkit, ReadDocumentTools)
    return toolkit


def _permissions(project: Path, **levels) -> ToolPermissions:
    return ToolPermissions(project_dir=project, settings_permissions=PermissionsConfig(**levels))


def test_the_registry_builds_it_on_the_project_asking_like_a_shell_read(project):
    toolkit = _toolkit(ToolRegistry(base_dir=str(project), permissions=_permissions(project)))

    assert toolkit.base_dir == project
    assert toolkit.async_functions["read_document"].requires_confirmation is True


def test_a_registry_built_without_settings_asks_too(project):
    toolkit = _toolkit(ToolRegistry(base_dir=str(project)))

    assert toolkit.async_functions["read_document"].requires_confirmation is True


@pytest.mark.parametrize("level", ["allow", "deny"])
def test_the_file_read_level_still_governs_nothing(project, level):
    toolkit = _toolkit(
        ToolRegistry(base_dir=str(project), permissions=_permissions(project, file_read=level))
    )

    assert toolkit.async_functions["read_document"].requires_confirmation is True
    assert "read_document" not in str(PermissionsConfig(file_read=level).category_rules())


def test_bypass_never_pauses_it(project):
    permissions = _permissions(project, mode="bypassPermissions")
    toolkit = _toolkit(ToolRegistry(base_dir=str(project), permissions=permissions))

    assert not toolkit.async_functions["read_document"].requires_confirmation


def test_a_bare_rule_in_the_settings_files_decides_its_level(project):
    settings = project / ".igni" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"permissions": {"allow": ["ReadDocument"]}}', encoding="utf-8")
    allowed = _toolkit(ToolRegistry(base_dir=str(project), permissions=_permissions(project)))
    settings.write_text('{"permissions": {"deny": ["ReadDocument"]}}', encoding="utf-8")
    denied = ToolRegistry(base_dir=str(project), permissions=_permissions(project))

    assert not allowed.async_functions["read_document"].requires_confirmation
    assert denied.resolve(["ReadDocument"]) == []


def test_the_catalog_knows_it_by_its_function_and_its_registry_name():
    catalog = ToolSpecCatalog.default()

    assert catalog.agno_to_registry["read_document"] == "ReadDocument"
    assert catalog.by_name["ReadDocument"].confirm_function_names == ("read_document",)
    assert "ReadDocument" in catalog.valid_ephemeral_names
    assert "ReadDocument" in catalog.registry_names_with_aliases
    assert catalog.specs[-1].name == "ReadDocument"


def test_its_names_resolve_for_permissions_display_and_evals():
    assert ToolNameResolver().resolve(func_name="read_document") == "ReadDocument"
    assert ToolFriendlyNames.friendly_name("read_document") == "ReadDocument"
    assert ToolNameCatalog.DEFAULT_EXPANSIONS["ReadDocument"] == ["read_document"]


@pytest.mark.parametrize("web", ["ask", "deny"])
def test_the_main_agent_always_gets_it(web):
    class Registry:
        def resolve(self, names):
            return []

    names = MainToolSet(PermissionsConfig(web_search=web, web_fetch=web)).names(Registry())

    assert "ReadDocument" in names
    assert names[: len(MainToolSet.CORE)] == list(MainToolSet.CORE)


def _bundled() -> list[tuple[str, list[str]]]:
    agents = []
    for path in sorted(AGENTS.glob("*.md")):
        front = yaml.safe_load(path.read_text().split("---", 2)[1]) or {}
        raw = front.get("tools", "")
        tools = raw if isinstance(raw, list) else [t.strip() for t in str(raw).split(",")]
        agents.append((path.stem, [t for t in tools if t]))
    return agents


def test_every_bundled_agent_with_the_shell_gets_it_and_only_those():
    for name, tools in _bundled():
        assert ("ReadDocument" in tools) == ("Bash" in tools), name


def test_a_bundled_agent_s_tools_build_it_on_the_project(project):
    registry = ToolRegistry(base_dir=str(project), permissions=_permissions(project))
    for name, tools in _bundled():
        if "ReadDocument" not in tools:
            continue
        built = [t for t in registry.resolve(tools) if isinstance(t, ReadDocumentTools)]
        assert len(built) == 1, name
        assert built[0].base_dir == project


# ── the permission modes ────────────────────────────────────────────


def _decide(mode: str = "default", call: str = "read_document", **lists) -> PermissionDecision:
    evaluator = PermissionEvaluator.from_strings(mode=mode, **lists)
    return evaluator.evaluate(call, {"path": "reports/q3.xlsx"})


@pytest.mark.parametrize("call", ["read_document", "ReadDocument"])
def test_plan_mode_runs_it(call):
    assert _decide("plan", call) is PermissionDecision.ALLOW


def test_plan_mode_still_asks_when_an_ask_rule_names_it():
    assert _decide("plan", ask=["ReadDocument"]) is PermissionDecision.ASK


def test_default_and_accept_edits_put_it_to_the_person():
    assert _decide("default") is PermissionDecision.DEFER
    assert _decide("acceptEdits") is PermissionDecision.DEFER


def test_an_allow_rule_lets_it_run_in_the_default_mode():
    assert _decide("default", allow=["ReadDocument"]) is PermissionDecision.ALLOW
    assert _decide("default", allow=["ReadDocument(reports/*)"]) is PermissionDecision.ALLOW


def test_dont_ask_refuses_it_unless_an_allow_rule_names_it():
    assert _decide("dontAsk") is PermissionDecision.DENY
    assert _decide("dontAsk", allow=["ReadDocument"]) is PermissionDecision.ALLOW


@pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "bypassPermissions", "dontAsk"])
def test_a_deny_rule_naming_it_wins_in_every_mode(mode):
    assert _decide(mode, deny=["ReadDocument"]) is PermissionDecision.DENY


def test_a_deny_rule_scoped_to_a_path_denies_only_that_path():
    evaluator = PermissionEvaluator.from_strings(mode="plan", deny=["ReadDocument(*.xlsx)"])

    assert evaluator.evaluate("read_document", {"path": "q3.xlsx"}) is PermissionDecision.DENY
    assert evaluator.evaluate("read_document", {"path": "q3.pdf"}) is PermissionDecision.ALLOW


def test_a_read_only_session_runs_it():
    evaluator = PermissionEvaluator.from_strings(mode="default")
    evaluator.read_only = True

    assert evaluator.evaluate("read_document", {"path": "a.pdf"}) is PermissionDecision.ALLOW


# ── Bypass's last check ─────────────────────────────────────────────


class _Judge:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def score(self, text):
        self.texts.append(text)
        raise AssertionError("a read-only call reached the judge")


class _Asker:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, tool_name, args, reason, agent_path, *, follow_up=None):
        self.asked.append(reason)
        return False


def _guard(project: Path, asker: _Asker, judge: _Judge) -> BypassGuard:
    return BypassGuard(
        config=BypassGuardConfig(),
        project_dir=project,
        in_bypass=lambda: True,
        ask=asker,
        tool_hints=ToolHintsRegistry(),
        judge=judge,  # type: ignore[arg-type]
    )


def test_bypass_runs_an_ordinary_read_without_asking_or_judging(project):
    asker, judge = _Asker(), _Judge()

    message = asyncio.run(_guard(project, asker, judge).check("read_document", {"path": "a.pdf"}))

    assert message is None
    assert asker.asked == []
    assert judge.texts == []


def test_bypass_asks_before_a_read_of_a_key(project):
    asker, judge = _Asker(), _Judge()

    message = asyncio.run(
        _guard(project, asker, judge).check("read_document", {"path": "~/.ssh/id_rsa"})
    )

    assert asker.asked == [
        "This file read reads a credential or secret file. Bypass mode skips approvals, "
        "but the safety check stops here so you decide."
    ]
    assert message == "Not run: the user rejected this call when asked to approve it."


def test_the_guard_sees_it_as_a_file_read_of_its_path():
    kind = ToolKind.of("read_document")

    assert kind is ToolKind.READ_FILE
    assert kind.target({"path": "a.pdf", "part": "2-"}) == "a.pdf"
    assert kind.describe("read_document", {"path": "a.pdf"}) == "Read: a.pdf"
    assert "read_document" in READ_ONLY_TOOLS
