"""CLI invocation orchestrator.

Owns every ounce of behaviour the pre-refactor procedural
callback used to run inline: debug logging, resume-id lookup,
worktree setup, additional-dirs resolution, and mode dispatch
(pipe / single-message / help-pointer). The Click decorator
surface in :mod:`igni_core.cli.__init__` collapses to a few
lines that build a :class:`CliOptions`, load settings
(:class:`~igni_core.cli.run_settings.CliSettingsLoader`), and
delegate here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from igni_core.cli.context import CliContext
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.logging import DebugLogging
from igni_core.cli.options import CliOptions
from igni_core.cli.resume import ResumeResolver
from igni_core.core.config.settings import Settings
from igni_core.core.session.single_message_run import SingleMessageRun
from igni_core.core.workspace_trust import WorkspaceTrust
from igni_core.core.worktree import WorktreeManager

#: The console script pyproject installs. `ignite-ember` is the other
#: one and is kept only for backwards compatibility, so it is not what
#: we tell anybody to type.
#:
#: Spelled here rather than inline because this text used to say
#: `ember`, which has never been an entry point — the first thing a new
#: installation printed was three commands that do not exist.
COMMAND = "igni"

_HELP_TEXT = (
    f"{COMMAND} no longer ships a terminal chat UI. Use one of:\n"
    f'  • {COMMAND} -m "<message>"         one-shot message\n'
    f"  • {COMMAND} -p                      pipe mode (stdin → stdout)\n"
    f'  • {COMMAND} -c -m "<message>"      resume last session, one-shot\n'
    "  • python -m igni_core.backend --ws-port 0\n"
    "        start the backend for a React client (web / Tauri)\n"
    "        to connect to over WebSocket.\n"
)


class CliInvocation:
    """The single owner of everything the CLI callback does after
    Click has parsed argv and merged settings.

    Composition surface:

    * :class:`CliOptions` — typed view of the Click params.
    * :class:`Settings` — the merged, validated settings snapshot.
    * :class:`HeadlessRun` — runs a ``-m`` / ``-p`` session and exits.

    All methods are I/O-side-effecting by design (the callback IS
    the CLI's imperative shell) — the class exists to give that
    imperative shell a single named owner rather than a 200-line
    procedural cascade.
    """

    def __init__(
        self,
        options: CliOptions,
        settings: Settings,
        headless_run: HeadlessRun,
    ) -> None:
        self._options = options
        self._settings = settings
        self._headless_run = headless_run
        self._resume_session_id: str | None = None
        self._worktree_manager: WorktreeManager | None = None

    # ── Startup phases ──────────────────────────────────────────

    def enable_debug_logging(self) -> None:
        """Attach the ``--debug`` file handler when the flag is set."""
        if not self._options.debug:
            return
        log_path = DebugLogging.enable()
        click.echo(f"Debug logging enabled → {log_path}")

    def resolve_resume_id(self) -> None:
        """Fill :attr:`_resume_session_id` from ``--session-id`` or
        ``--continue`` (in that order — an explicit id wins)."""
        opts = self._options
        if opts.session_id:
            self._resume_session_id = opts.session_id
            return
        if not opts.continue_session:
            return

        resolver = ResumeResolver(self._settings)
        lookup = resolver.latest_id()
        if lookup.error is not None:
            click.echo("Could not look up last session.")
            return
        if lookup.session_id is None:
            click.echo("No previous sessions found.")
            return
        self._resume_session_id = lookup.session_id
        click.echo(f"Resuming last session: {lookup.session_id}")

    def setup_worktree(self) -> None:
        """Create the isolated git worktree when ``--worktree`` is
        set. Aborts the CLI with exit code 1 on failure so callers
        see a clear stderr instead of a stack trace."""
        if not self._options.worktree:
            return
        wm = WorktreeManager(Path.cwd())
        result = wm.create_result(session_id=self._resume_session_id)
        if not result.ok or result.info is None:
            click.echo(f"worktree failed: {result.message}", err=True)
            raise SystemExit(1)
        info = result.info
        self._worktree_manager = wm
        click.echo(f"Worktree: {info.worktree_path} (branch: {info.branch_name})")

    def carry_trust_into_worktree(self, folder: Path) -> None:
        """``--worktree`` runs in a new folder of the same repository:
        trusted for this run when ``folder``, the one it was made from,
        is (and policy allows it)."""
        worktree = self.project_dir
        if worktree is None or worktree == folder:
            return
        service = WorkspaceTrust.shared()
        if service.is_trusted(folder):
            service.grant_for_run(worktree)

    def store_context(self, ctx: click.Context) -> None:
        """Stash the typed :class:`CliContext` on ``ctx.obj`` so
        subcommands can read the same settings / worktree state."""
        ctx.obj = CliContext(
            settings=self._settings,
            worktree_manager=self._worktree_manager,
            project_dir=self.project_dir,
            additional_dirs=self.additional_dirs,
        )

    @property
    def project_dir(self) -> Path | None:
        """The worktree the run happens in, when ``--worktree`` made one."""
        if self._worktree_manager is None or self._worktree_manager.info is None:
            return None
        return self._worktree_manager.info.worktree_path

    @property
    def additional_dirs(self) -> list[Path] | None:
        """Each ``--add-dir`` value as an absolute path."""
        raw = self._options.add_dir
        if not raw:
            return None
        return [Path(d).resolve() for d in raw]

    # ── Mode dispatch tail ──────────────────────────────────────

    def read_pipe_message(self) -> str:
        """Read stdin and combine with ``-m`` for pipe mode.

        Exits with code 1 when neither stdin nor ``-m`` supplied a
        message — matches the pre-refactor error path.
        """
        message = self._options.message
        if message and sys.stdin.isatty():
            # Nothing is ever piped in from a terminal, and ``read()`` on a tty
            # blocks until the user sends EOF by hand — so ``-p -m "..."``
            # invoked interactively hung before the session even started, with
            # no output on either stream.
            return message
        text = sys.stdin.read().strip()
        if message:
            text = f"{message}\n\n{text}" if text else message
        if not text:
            click.echo("Error: no input provided via stdin or -m", err=True)
            raise SystemExit(1)
        return text

    def run_pipe(self) -> None:
        """Pipe mode: run one session on stdin (plus ``-m``), then exit."""
        self.run_message(self.read_pipe_message())

    def run_message(self, message: str) -> None:
        """Run one session on ``message``, then exit (see :class:`HeadlessRun`)."""
        self._headless_run.run(self._session(message), cleanup=self.cleanup_worktree)

    async def _session(self, message: str) -> None:
        # Built inside the coroutine so the session is constructed on the
        # event loop :class:`HeadlessRun` starts, not before it.
        await SingleMessageRun(
            self._settings,
            resume_session_id=self._resume_session_id,
            project_dir=self.project_dir,
            additional_dirs=self.additional_dirs,
        ).run(message)

    def echo_help_pointer(self) -> None:
        """Print the "use a React client instead" pointer when
        ``igni`` is called with no message and no subcommand."""
        click.echo(_HELP_TEXT)

    def cleanup_worktree(self) -> None:
        """Delegate to :meth:`WorktreeManager.report_cleanup`. Safe
        no-op when no worktree was created."""
        if self._worktree_manager is None:
            return
        self._worktree_manager.report_cleanup(click.echo)
