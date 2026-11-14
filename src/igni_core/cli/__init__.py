"""igni CLI — the ``igni`` command-line entry point.

Non-interactive command surface. The Textual TUI was removed in
v0.9.7 — interactive chat now lives in the React clients
(``clients/web``, ``clients/tauri``), which speak to the backend
server started via ``python -m igni_core.backend --socket <path>``.

Supported modes:

* ``igni -m "<message>"`` — single-message non-interactive run.
* ``igni -p`` — pipe mode: read stdin, run one message, write
  stdout.
* ``igni`` (no args) — prints help and pointer to the React
  clients; use ``python -m igni_core.backend --socket <path>`` to
  start the backend for a client to connect to.

Global flags (permissions, worktree, additional dirs) apply to
the non-interactive modes.

This module is a *thin* Click-decorator surface: every option
lands on :func:`cli`, which validates the params into a typed
:class:`CliOptions`, loads settings via
:class:`CliSettingsLoader`, and hands off to
:class:`CliInvocation` for the real work, including running the
session (:class:`HeadlessRun`).
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from igni_core import __version__
from igni_core.cli.ext import ext
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.login import login
from igni_core.cli.migrate import migrate
from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader
from igni_core.cli.trust import HeldWarnings, SessionTrust, trust_command
from igni_core.core import air_gap
from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.observability import OtelRuntime
from igni_core.core.paths import DEFAULT_DATA_DIR


# A group that runs without one.
#
# This was ``@click.group`` with zero commands registered, which put
# ``[COMMAND] [ARGS]...`` in the usage line advertising something that
# did not exist — every natural first move after ``--help`` produced a
# usage error — so it became a plain command. It is a group again now
# that there is a command to put in it, and ``invoke_without_command``
# keeps the old behaviour exactly: ``igni`` and ``igni -m "…"`` still
# start a session, because the subcommands are words and this command
# takes no positional argument for one to be mistaken for.
@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="igni")
@click.option("--model", default=None, help="Model to use")
@click.option("--verbose", is_flag=True, help="Show routing and reasoning")
@click.option("--quiet", is_flag=True, help="Minimal output")
@click.option("-m", "--message", default=None, help="Single message (non-interactive)")
@click.option(
    "--continue", "-c", "continue_session", is_flag=True, help="Resume the most recent session"
)
@click.option("--session-id", default=None, help="Resume a specific session by ID")
@click.option(
    "--read-only",
    is_flag=True,
    help=(
        "Read files and the web, never change anything: read-only shell "
        "commands (cat, ls, rg, git log, …), web search and URL fetches run "
        "without asking; edits, any other shell command, and extension tools "
        "that change something are refused, not asked"
    ),
)
@click.option("--accept-edits", is_flag=True, help="Auto-approve file edits")
@click.option("--auto-approve", is_flag=True, help="Auto-approve everything")
@click.option(
    "-p", "--pipe", is_flag=True, help="Pipe mode: read stdin, write stdout, no interactive UI"
)
@click.option("--no-web", is_flag=True, help="Disable web search/fetch tools")
@click.option("--no-color", is_flag=True, help="Disable color output")
@click.option("--debug", is_flag=True, help=f"Enable debug logging to {DEFAULT_DATA_DIR}/debug.log")
@click.option(
    "--strict",
    is_flag=True,
    # Two things this is NOT, both of which the help text used to imply.
    #
    # It is not "deny all dangerous operations" on its own: hooks run shell
    # commands on a separate path the agent's permissions do not gate. What
    # ``--strict`` does for hooks is ``hooks.group: ask`` — a hook the
    # organisation's group policy ships runs only once the person says yes,
    # and in a headless run, where nobody can, it does not run. The
    # developer's own hooks run as they always do. See
    # tests/test_group_hooks_are_the_developers_call.py.
    #
    # And it is not "deny the dangerous ones, keep the safe ones". ``--strict``
    # selects ``dontAsk``, whose whole point is never to prompt, so anything
    # without an explicit allow rule must fail closed — reads included. Naming
    # only writes/shell/git promised a working read-only agent and delivered one
    # that can do nothing at all.
    #
    # A default read-only allowlist would be the obvious repair and does not
    # work here: igni reads files through ``run_shell_command``, which strict
    # denies, so "allow the safe reads" cannot include reading a file without
    # re-allowing the shell. That is a policy decision, not a patch, so the help
    # states the real behaviour instead of implying a gentler one.
    help=(
        "Headless deny-by-default for the agent's own tool use: anything without "
        "an explicit allow rule is denied, including reads. Pair with "
        "permissions.allow to opt tools back in. Hooks your organisation's group "
        "policy ships run only once you approve them (hooks.group: ask), and not "
        "at all in a headless run; your own hooks are unaffected"
    ),
)
@click.option(
    "--trust",
    is_flag=True,
    help=(
        "Trust this folder for this run: its tools, hooks, workflows, language "
        "servers and settings load (if your organisation's policy allows it)"
    ),
)
@click.option("--worktree", is_flag=True, help="Run in an isolated git worktree")
@click.option(
    "--add-dir",
    multiple=True,
    type=click.Path(exists=True, file_okay=False),
    help="Additional directory to include (can be repeated)",
)
@click.pass_context
def cli(ctx: click.Context, **_params: object) -> None:
    """igni — an AI coding assistant that works on your code where it lives.

    Run one message with -m, or pipe work in with -p. The chat itself is in
    the desktop app or the web client; running igni with no arguments says
    where to get them.
    """
    # Click prints the docstring above verbatim as ``--help`` output, so
    # implementation notes do not belong in it. This one used to explain
    # the delegation to ``CliInvocation`` and every user saw it, complete
    # with a ``:class:`` reStructuredText marker Click does not render.
    #
    # The note itself is worth keeping: Click forces a
    # decorator-per-option surface here, so all the logic lives on the
    # invocation object and this callback stays small.
    # A subcommand handles itself. Falling through would run the CLI's own
    # setup as well — and, with no message, print the help pointer on top
    # of it.
    if ctx.invoked_subcommand is not None:
        return

    options = CliOptions.model_validate(ctx.params)
    # Workspace trust: the folder's config, tools, hooks and the rest wait for
    # it (``core/workspace_trust``). Settled after the first load, which read
    # the organisation's policy, and settings reload with the folder's config
    # once it is trusted. Only for a run that starts a session. What the first
    # load says about the folder's files being left out is held until then:
    # it is untrue once the folder is trusted.
    folder = Path.cwd()
    starts_session = bool(options.message or options.pipe)
    held = HeldWarnings()
    try:
        with held:
            settings = CliSettingsLoader(options).load()
    except GroupPolicyUnavailable as exc:
        # ``group_policy.required`` and no verified pack: no session, and
        # the message says what to do. ``igni login`` still works — it is a
        # subcommand, handled above.
        raise click.ClickException(str(exc)) from exc
    if starts_session:
        became_trusted = SessionTrust(
            folder, pipe=options.pipe, trust_flag=options.trust, quiet=options.quiet
        ).settle()
        if became_trusted:
            held.discard()
            settings = CliSettingsLoader(options).load()
    held.release()

    # A ``-m`` or pipe run starts igni's decisions service itself, and that
    # process reads no settings: it inherits the air-gap decision instead.
    air_gap.export_to(os.environ, settings.air_gapped)
    # Agno's own run telemetry stays off when air-gapped; traces go to the
    # organisation's collector, if it named one (``core/observability``).
    AgnoTelemetry.enforce(os.environ, air_gapped=settings.air_gapped)
    OtelRuntime.start(settings)

    invocation = CliInvocation(options, settings, HeadlessRun())
    invocation.enable_debug_logging()

    invocation.resolve_resume_id()
    invocation.setup_worktree()
    invocation.carry_trust_into_worktree(folder)
    invocation.store_context(ctx)

    if options.pipe:
        invocation.run_pipe()
        return
    if options.message:
        invocation.run_message(options.message)
        return

    # No message + no subcommand → point users at the React clients.
    invocation.echo_help_pointer()
    invocation.cleanup_worktree()


# Registered after the callback so the module reads top to bottom.
cli.add_command(ext)
cli.add_command(login)
cli.add_command(migrate)
cli.add_command(trust_command)

__all__ = ["cli"]
