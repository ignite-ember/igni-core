"""Display utilities — Rich-based output for the terminal.

The public surface is exactly one class — :class:`DisplayManager`
— plus the two Pydantic DTOs it accepts
(:class:`RunStats` / :class:`ToolCallDisplay`), which are
re-exported here so external callers can pull everything from
this module.

There is deliberately no module-level singleton and no free-
function facade: every caller constructs (or is handed) a
:class:`DisplayManager` instance and calls methods on it. This
keeps Rule 1 (no raw dicts crossing module boundaries) and
Rule 3 (no emoji icons — text prefixes only) enforced at the
type-signature level rather than by convention.
"""

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from igni_core.core.utils.display_schemas import RunStats, ToolCallDisplay

__all__ = ["DisplayManager", "RunStats", "ToolCallDisplay"]


class DisplayManager:
    """Manages terminal output with configurable formatting.

    Every method takes either a plain string (info / warning /
    error / markdown / response / welcome banner) or one of the
    two Pydantic DTOs (:class:`RunStats`, :class:`ToolCallDisplay`).
    The DTOs own the domain formatting policy — this class is a
    thin Rich sink.
    """

    def __init__(self, console: Console | None = None, diagnostics: Console | None = None):
        self.console = console or Console()
        #: Where errors, warnings and the run's ``── 2.1s · model ──`` line
        #: go. By default stderr: stdout is the reply, and ``igni -m … |
        #: cat`` got the footer as part of it. A console passed alone takes
        #: everything, as it always did.
        self.diagnostics = diagnostics or (
            Console(stderr=True) if console is None else self.console
        )
        #: Stdout is not a terminal (a pipe, a file, CI): the reply is
        #: written as the model wrote it, not rendered into an 80-column box
        #: padded with spaces.
        self._plain = console is None and not self.console.is_terminal
        #: The same for the diagnostics stream, which is a stream of its
        #: own: stderr may be a terminal while stdout is a pipe, and the
        #: other way round.
        self._plain_diagnostics = (
            console is None and diagnostics is None and not self.diagnostics.is_terminal
        )

    def _out(self, console: Console, plain: bool, text: str, markup: str) -> None:
        """Write ``text``: as Rich markup in a terminal, as it is to a pipe.

        Rich wraps at the console's width, and a console whose file is
        not a terminal is 80 columns wide — so a path or a message came
        out split across two lines, and a reader had to join them again
        by hand. A stream that is not a terminal gets the text as it is:
        no wrapping, no cropping, no markup.
        """
        if plain:
            console.out(text, highlight=False)
            return
        console.print(markup)

    def print_markdown(self, text: str) -> None:
        """Render markdown text in the terminal; as it is, to a pipe."""
        if self._plain:
            self.console.out(text, highlight=False)
            return
        self.console.print(Markdown(text))

    def print_response(self, text: str, agent_name: str | None = None) -> None:
        """Print an agent response with optional agent label."""
        if agent_name:
            self._out(self.console, self._plain, agent_name, f"[dim]{escape(agent_name)}[/dim]")
        self.print_markdown(text)

    def print_tool_call(self, call: ToolCallDisplay) -> None:
        """Print a tool call notification.

        Delegates truncation to :meth:`ToolCallDisplay.format_args`
        so the "50-char clip + ellipsis" policy is a data
        concern, not a Rich concern. The prefix is the text
        marker ``>`` — no emoji icons (Rule 3).
        """
        args_str = call.format_args()
        self._out(
            self.console,
            self._plain,
            f"  > {call.tool_name}{args_str}",
            f"[dim]  > {escape(call.tool_name)}{escape(args_str)}[/dim]",
        )

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self._out(
            self.diagnostics,
            self._plain_diagnostics,
            f"Error: {message}",
            f"[red]Error:[/red] {escape(message)}",
        )

    def print_warning(self, message: str) -> None:
        """Print a warning message."""
        self._out(
            self.diagnostics,
            self._plain_diagnostics,
            f"Warning: {message}",
            f"[yellow]Warning:[/yellow] {escape(message)}",
        )

    def print_info(self, message: str) -> None:
        """Print an info message."""
        self._out(self.console, self._plain, message, f"[dim]{escape(message)}[/dim]")

    def print_run_stats(self, stats: RunStats) -> None:
        """Print run statistics after a completed run.

        The full summary string is composed by
        :meth:`RunStats.format_summary`; this method just wraps
        it in the ``── … ──`` separator and hands it to Rich.
        """
        summary = stats.format_summary()
        self._out(
            self.diagnostics,
            self._plain_diagnostics,
            f"  ── {summary} ──",
            f"[dim]  ── {escape(summary)} ──[/dim]",
        )

    def print_welcome(self, version: str) -> None:
        """Print the welcome banner."""
        self.console.print(
            Panel(
                f"[bold]igni[/bold] v{escape(version)}\n"
                f"[dim]Self-hosted, air-gapped agentic platform[/dim]\n\n"
                f"Type your message, or /help for commands.\n"
                f"Press Ctrl+C to exit.",
                border_style="blue",
            )
        )
