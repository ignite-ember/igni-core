"""Tests for protected path enforcement in ToolEventHook."""

import pytest

from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.permission.protected_path_stage import ProtectedPathStage
from igni_core.core.hooks.tool_hook import ToolEventHook


class TestIsProtectedPath:
    """Pins :meth:`ProtectedPathStage.matches_pattern` — the pure
    static predicate the pre-refactor ``_is_protected_path`` shim
    used to wrap."""

    def test_exact_match(self):
        assert ProtectedPathStage.matches_pattern(".env", [".env"])

    def test_glob(self):
        assert ProtectedPathStage.matches_pattern(".env.production", [".env.*"])

    def test_wildcard_ext(self):
        assert ProtectedPathStage.matches_pattern("server.pem", ["*.pem"])

    def test_no_match(self):
        assert not ProtectedPathStage.matches_pattern("app.py", [".env", "*.pem"])

    def test_full_path(self):
        assert ProtectedPathStage.matches_pattern("/project/.env", [".env"])

    def test_empty_patterns(self):
        assert not ProtectedPathStage.matches_pattern(".env", [])


class TestToolEventHookProtectedPaths:
    def _hook(self):
        return ToolEventHook(
            HookExecutor({}),
            session_id="test",
            protected_paths=[".env", ".env.*", "*.pem", "*.key", "credentials.*"],
        )

    @pytest.mark.asyncio
    async def test_blocks_write_to_env(self):
        result = await self._hook()(
            name="save_file", func=lambda **kw: "w", args={"file_path": "/project/.env"}
        )
        assert "Blocked" in result

    @pytest.mark.asyncio
    async def test_allows_normal_write(self):
        result = await self._hook()(
            name="save_file", func=lambda **kw: "ok", args={"file_path": "src/app.py"}
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_allows_read_of_protected(self):
        result = await self._hook()(
            name="read_file", func=lambda **kw: "contents", args={"file_path": ".env"}
        )
        # Read is allowed (not blocked) — the func ran and its output came back.
        # It is wrapped as untrusted external content; the contents survive.
        assert "contents" in result

    @pytest.mark.asyncio
    async def test_func_not_called_when_blocked(self):
        called = {}
        await self._hook()(
            name="save_file",
            func=lambda **kw: called.update(ran=True),
            args={"file_path": "credentials.json"},
        )
        assert "ran" not in called


class TestIgnisOwnSettingsAreAlwaysProtected:
    """The agent cannot raise its own permissions: with ordinary edit
    approval it wrote ``.igni/settings.local.json`` allowing Bash, and the
    next session in the trusted folder ran shell unasked (campaign R110).
    These files are protected whatever ``safety.protected_paths`` says."""

    def _hook(self, protected: list[str]):
        return ToolEventHook(HookExecutor({}), session_id="t", protected_paths=protected)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            ".igni/settings.local.json",
            ".igni/settings.json",
            "/abs/project/.igni/config.yaml",
            "./.igni/config.local.yaml",
            "sub/.igni/config.yml",
        ],
    )
    @pytest.mark.parametrize("protected", [[], [".env"]])
    async def test_a_write_to_igni_settings_is_blocked(self, path, protected):
        ran: list[str] = []
        result = await self._hook(protected)(
            name="save_file", func=lambda **kw: ran.append("x") or "w", args={"file_name": path}
        )
        assert result == f"Blocked: '{path}' is a protected path and cannot be written to."
        assert ran == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [".igni/agents/x.md", "config.yaml", "settings.json"])
    async def test_other_files_are_not(self, path):
        result = await self._hook([])(
            name="save_file", func=lambda **kw: "ok", args={"file_name": path}
        )
        assert result == "ok"


class TestTheShellWritingAProtectedPath:
    """A shell redirect to a protected path was only asked about, and once
    approved it wrote the file (campaign R109): protected paths cannot be
    written "regardless of permission level"."""

    @pytest.mark.parametrize(
        ("command", "targets"),
        [
            ("echo hi > shell.secret", ["shell.secret"]),
            ("echo hi>>a.pem; ls", ["a.pem"]),
            ("cat x | tee -a .env other.txt", [".env", "other.txt"]),
            ("printf x &> out.key", ["out.key"]),
            ("make 2>&1 >/dev/null", []),
            ("echo 'a > b'", []),
            ("echo 'unterminated", []),
        ],
    )
    def test_the_targets_a_command_writes(self, command, targets):
        assert ProtectedPathStage.shell_write_targets(command) == targets

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        ["echo hi > shell3.secret", "echo k | tee x.pem", "echo x >> .igni/settings.json"],
    )
    async def test_a_redirect_to_a_protected_path_is_blocked_before_it_runs(self, command):
        ran: list[str] = []
        hook = ToolEventHook(
            HookExecutor({}), session_id="t", protected_paths=["*.secret", "*.pem"]
        )
        result = await hook(
            name="run_shell_command",
            func=lambda **kw: ran.append("x") or "ran",
            args={"command": command},
        )
        assert "is a protected path and cannot be written to." in result
        assert ran == []

    @pytest.mark.asyncio
    async def test_reading_a_protected_path_is_not_blocked(self):
        hook = ToolEventHook(HookExecutor({}), session_id="t", protected_paths=["*.secret"])
        result = await hook(
            name="run_shell_command",
            func=lambda **kw: "ran",
            args={"command": "cat x.secret > out.txt"},
        )
        assert "ran" in result and "protected path" not in result
