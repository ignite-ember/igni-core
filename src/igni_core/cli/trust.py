"""Workspace trust on the command line: the first-run prompt and ``igni trust``.

Opening a folder never runs its code until the person trusts it
(``core/workspace_trust``). On a terminal, the first ``igni -m`` in a folder
nobody has decided about asks: Trust / Don't trust / This time only. With
nobody to ask — a pipe, CI, a script — the folder runs untrusted unless
``--trust`` is passed (and the organisation's policy allows it), or the
person trusted it earlier.

``igni trust`` records the decision outside a session:

* ``igni trust [PATH]`` — trust the folder (the current one by default);
* ``igni trust --revoke [PATH]`` — forget the decision, so it asks again;
* ``igni trust --list`` — every recorded decision.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import TracebackType

import click

from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader
from igni_core.core.workspace_trust import (
    GATED_SUMMARY,
    TrustDecision,
    TrustRefused,
    process_trust,
)


class HeldWarnings:
    """The settings loader's warnings, kept back until the folder's trust is settled.

    The first settings load of a run reads the organisation's policy, which
    decides whether ``--trust`` may apply, so it runs before the folder is
    trusted. An untrusted load says what it left out of the folder's files
    ("sets permissions … ignored"), and that is false once ``--trust`` makes
    the folder trusted and settings load again. So those warnings wait:
    :meth:`release` says them when the folder stayed untrusted, and
    :meth:`discard` drops them when the reload will have read the files.
    """

    LOGGER = "igni_core.core.config"

    def __init__(self) -> None:
        self._logger = logging.getLogger(self.LOGGER)
        self._records: list[logging.LogRecord] = []
        self._handler = logging.Handler(level=logging.WARNING)
        self._handler.emit = self._records.append  # type: ignore[method-assign]
        self._propagate = self._logger.propagate

    def __enter__(self) -> HeldWarnings:
        self._logger.addHandler(self._handler)
        self._logger.propagate = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.propagate = self._propagate
        if exc_type is not None:
            self.release()

    def release(self) -> None:
        """Say what was held, as it would have been said."""
        records, self._records = self._records, []
        for record in records:
            logging.getLogger(record.name).handle(record)

    def discard(self) -> None:
        self._records.clear()


def _can_prompt() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stderr and sys.stderr.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _describe(decision: TrustDecision) -> str:
    remote = decision.identity.remote or "none"
    return f"  Folder: {decision.identity.path}\n  Git remote: {remote}"


class SessionTrust:
    """Settle the trust decision for a ``-m`` / pipe run, before its session starts."""

    def __init__(self, project_dir: Path, *, pipe: bool, trust_flag: bool, quiet: bool) -> None:
        self._project_dir = project_dir
        self._pipe = pipe
        self._trust_flag = trust_flag
        self._quiet = quiet
        self._trust = process_trust()

    def settle(self) -> bool:
        """Ask or apply ``--trust`` as needed. True when the folder became
        trusted, so the caller reloads settings with its config."""
        before = self._trust.decide(self._project_dir)
        if before.trusted:
            return False
        if self._trust_flag:
            after = self._trust.grant_for_run(self._project_dir)
            if not after.trusted:
                self._notice(after)
            return after.trusted
        if before.needs_answer and not self._pipe and _can_prompt():
            return self._ask(before)
        self._notice(before)
        return False

    def _ask(self, decision: TrustDecision) -> bool:
        click.echo(
            "\nTrust this folder? It can run code: tools, hooks, workflows, "
            "language servers…\n" + _describe(decision) + "\n\n"
            f"Until you trust it, igni leaves off its {GATED_SUMMARY}.",
            err=True,
        )
        answer = click.prompt(
            "  [t] Trust  [d] Don't trust  [o] This time only",
            type=click.Choice(["t", "d", "o"], case_sensitive=False),
            default="d",
            show_choices=False,
            err=True,
        ).lower()
        if answer == "d":
            self._trust.distrust(self._project_dir)
            return False
        try:
            self._trust.trust(self._project_dir, remember=answer == "t")
        except TrustRefused as exc:  # policy changed underneath; say so
            click.echo(f"  Not trusted: {exc}", err=True)
            return False
        return True

    def _notice(self, decision: TrustDecision) -> None:
        if self._quiet:
            return
        hint = (
            " Run `igni trust` to trust it, or pass --trust for this run."
            if decision.can_trust
            else ""
        )
        click.echo(
            f"igni: {decision.identity.path} is not trusted — its {GATED_SUMMARY} "
            f"are off. {decision.reason}{hint}",
            err=True,
        )


@click.command("trust")
@click.argument(
    "path",
    required=False,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--revoke", is_flag=True, help="Forget the decision for the folder; igni asks again.")
@click.option("--list", "list_", is_flag=True, help="List every folder you have decided about.")
def trust_command(path: Path | None, revoke: bool, list_: bool) -> None:
    """Trust a folder to run its own code (tools, hooks, workflows, language servers…).

    PATH defaults to the current folder.
    """
    # Loads settings for the organisation's policy (``workspace_trust`` is
    # read from the managed file, the group policy and the environment).
    CliSettingsLoader(CliOptions()).load()
    service = process_trust()

    if list_:
        entries = service.entries()
        if not entries:
            click.echo("No folders trusted or distrusted yet.")
            return
        for entry in entries:
            click.echo(f"{entry.state:<10} {entry.path}  [{entry.remote or 'no remote'}]")
        return

    folder = path or Path.cwd()
    if revoke:
        decision = service.revoke(folder)
        click.echo(f"Trust revoked for {decision.identity.path}; igni will ask again.")
        return
    try:
        decision = service.trust(folder)
    except TrustRefused as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Trusted {decision.identity.path}\n" + _describe(decision))


__all__ = ["HeldWarnings", "SessionTrust", "trust_command"]
