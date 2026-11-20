"""Non-interactive session runner — the OOP peer of
:class:`InteractiveSessionLoop`.

Both coordinators subclass :class:`SessionRun`, so the SessionStart
/ SessionEnd hook emit sites and the ``@`` mention + MediaResolver
+ ``handle_message`` + :class:`RunStats` pipeline live in one place
(``session_run.py``). This module adds only what's unique to the
single-shot case:

* :meth:`SingleMessageRun._dispatch_slash` — what the turn is. Wraps
  :class:`CommandHandler`: a built-in command prints its result and is
  the whole run; a skill, a markdown command or ``/loop <prompt>`` hands
  back its prompt, which runs as the turn, as at the interactive prompt.
* :meth:`SingleMessageRun.run` — the top-level orchestration:
  SessionStart hook, slash-dispatch, :meth:`SessionRun._run_turn`,
  SessionEnd hook, and the exit code the turn earned.

The CLI (:class:`~igni_core.cli.invocation.CliInvocation`) builds one
of these directly.
"""

from __future__ import annotations

from igni_core.backend.command_handler import CommandHandler
from igni_core.backend.headless_extensions import HeadlessExtensions
from igni_core.core.session.commands import InteractiveCommandDispatcher
from igni_core.core.session.message_outcome import tools_as_prose
from igni_core.core.session.session_run import SessionRun
from igni_core.protocol.messages import CommandAction


class SingleMessageRun(SessionRun):
    """Runs one message end-to-end and exits.

    Subclasses :class:`SessionRun` so the base owns
    :class:`Session` construction, the SessionStart / SessionEnd
    hook emit sites, and the shared ``_run_turn`` pipeline. This
    class contributes only what's unique to the non-interactive
    path.
    """

    #: The exit code of a ``-m`` / ``-p`` run whose turn failed.
    FAILED_EXIT_CODE = 1

    # ── Slash-command dispatch ────────────────────────────────────

    async def _dispatch_slash(self, message: str) -> str | None:
        """What the model is sent for ``message``, or ``None`` if nothing.

        A plain message is sent as it is. A slash command runs through
        :class:`CommandHandler`: a skill, a markdown command and ``/loop
        <prompt>`` answer with the prompt to run (``RUN_PROMPT``), and that
        prompt is the turn, as at the interactive prompt. Any other command
        prints its result, and no turn runs.
        """
        if not message.startswith("/"):
            return message
        handler = CommandHandler(self._session)
        result = await handler.handle(message)
        if result.action == CommandAction.RUN_PROMPT:
            return result.content
        if result.content:
            self._session.display.print_info(result.content)
        elif result.action != CommandAction.QUIT:
            # An action with no content opens a panel or a picker in the
            # clients (``/help``, ``/login``, ``/sessions``, ``/model``,
            # ``/agents``...). Headless, the REPL's text form is printed in
            # its place; before, ``igni -m /sessions`` printed nothing and
            # exited 0, as if there were nothing to list.
            await InteractiveCommandDispatcher(self._session).render(result)
        return None

    # ── Public entry point ────────────────────────────────────────

    async def run(self, message: str) -> None:
        """Run one message: extensions up → SessionStart → dispatch → SessionEnd → extensions down.

        A built-in slash command runs without a model turn. A skill, a
        markdown command and ``/loop <prompt>`` run their prompt as the
        turn, and everything else is the turn itself, through the shared
        :meth:`SessionRun._run_turn` pipeline; the exit code follows that
        turn. SessionEnd always fires exactly once.
        """
        # The extensions installed here apply to a headless run as they
        # do in the app: their tools, hooks and prompt contributions.
        extensions = HeadlessExtensions(self._session)
        # Nobody answers a question here: a sub-agent's pause ends its run.
        self._session.sub_agent_hitl.mark_unattended()
        await extensions.start()
        try:
            await self._fire_session_start()

            turn = await self._dispatch_slash(message)
            if turn is None:
                # A command that was the whole run: no model call.
                await self._fire_session_end()
                return

            succeeded = await self._run_turn(turn)
            succeeded = self._report_unanswered_sub_agent_pauses() and succeeded
            self._emit_pending_plan()
            await self._fire_session_end()
        finally:
            self._cleanup_ephemeral_agents()
            await extensions.stop()
        if not succeeded:
            # The run stopped at a confirmation nobody here can answer,
            # found no model, or failed: said on stderr already, and the
            # exit code says it to whatever ran ``igni -m``.
            raise SystemExit(self.FAILED_EXIT_CODE)

    def _cleanup_ephemeral_agents(self) -> None:
        """Remove the agents this run created, as the app does when a session ends.

        ``orchestration.auto_cleanup`` (on by default) removes ephemeral
        agents when the session ends; the backend's shutdown and the
        interactive loop did, a headless run did not, so every later
        session's pool and ``/agents`` list carried them (campaign R131).
        """
        session = self._session
        # Typed checks: a loosely held session (a test double) answers
        # with truthy objects that are not booleans.
        if session.settings.orchestration.auto_cleanup is not True:
            return
        pool = session.pool
        if getattr(pool, "has_ephemeral", False) is True:
            pool.ephemeral.cleanup()

    def _report_unanswered_sub_agent_pauses(self) -> bool:
        """Say which sub-agent calls waited on a confirmation; ``False`` if any did.

        A sub-agent's pause ends that sub-agent's run here (nobody can
        answer it), and the main agent goes on without the call. The run
        still did not do what was asked, so it ends as a main-agent pause
        does: the tools named on stderr, and exit 1.
        """
        tools = self._session.sub_agent_hitl.take_unanswered()
        # Typed check, as in ``_run_turn``: a loosely held session (a test
        # double) answers with something truthy that is not a list.
        if not isinstance(tools, list) or not tools:
            return True
        self._session.display.print_error(
            f"A sub-agent's run paused for confirmation on {tools_as_prose(tools)} but "
            "this session cannot resolve a pause; the tool did not run. Decide up front "
            "what it may do: --read-only to only read, or a permissions.allow rule in "
            "~/.igni/config.yaml (with --accept-edits for edits); --auto-approve allows "
            "everything. Or use a client that supports confirmations."
        )
        return False

    def _emit_pending_plan(self) -> None:
        """Print a plan the agent submitted, because nothing else will.

        ``--read-only`` selects plan mode, where a denied edit tells the model
        to "call ``exit_plan_mode(plan)`` when ready". That writes the plan to
        :class:`PlanStore`, and the only consumer is the frontend's plan card —
        ``approve_plan`` is an FE operation and ``/plan`` is an interactive
        command. Headlessly there is no card and no one to approve, so the run
        ended having produced a plan nobody could read: the session looked like
        it had simply declined to do the work.

        Printing it is the whole fix. A headless run is one-shot by definition —
        there is no second turn for an approval to unblock — so the useful
        outcome is the plan itself, delivered rather than filed.
        """
        plan = self._session.plan_store.latest
        if not plan.strip():
            return
        self._session.display.print_info(
            "\n".join(
                [
                    "",
                    "Plan (read-only session — nothing was executed):",
                    "",
                    plan,
                    "",
                    "Re-run without --read-only to carry it out.",
                ]
            )
        )
