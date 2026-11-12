"""``/model <name>`` switches a known model and refuses an unknown one out loud.

The session's own ``set_default_model`` produces the result and ``/model``
renders it, so both run for real here against a session stand-in that owns
only the model registry and the team rebuild.
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.backend.cmd_model import ModelCommand
from igni_core.backend.schemas_model import ModelSwitchResult
from igni_core.core.session import Session
from igni_core.protocol.messages import CommandAction, CommandResultKind


class _Session:
    set_default_model = Session.set_default_model

    def __init__(self, *names: str) -> None:
        self.settings = SimpleNamespace(
            models=SimpleNamespace(registry={n: object() for n in names}, default=names[0])
        )
        self.rebuilds = 0

    def rebuild_main_team(self) -> None:
        self.rebuilds += 1


def test_switching_to_a_known_model_says_so_and_asks_the_status_bar_to_refresh() -> None:
    session = _Session("small", "large")

    result = ModelCommand(session).switch("large")  # type: ignore[arg-type]

    assert result.kind is CommandResultKind.INFO
    assert result.content == "Switched to model: large"
    assert result.action is CommandAction.MODEL_SWITCHED
    assert session.settings.models.default == "large"
    assert session.rebuilds == 1


def test_switching_to_an_unknown_model_is_refused_and_lists_what_there_is() -> None:
    session = _Session("zeta", "alpha")

    result = ModelCommand(session).switch("nope")  # type: ignore[arg-type]

    assert result.kind is CommandResultKind.ERROR
    assert result.content == "Unknown model: 'nope'. Available: alpha, zeta"
    assert result.action is CommandAction.NONE
    assert session.settings.models.default == "zeta"
    assert session.rebuilds == 0


def test_a_refusal_names_the_reason_it_was_given_instead_of_the_default() -> None:
    result = ModelSwitchResult(
        ok=False, model_name="x", available=["b", "a"], error="No key for 'x'."
    )

    assert result.failure_text == "No key for 'x'. Available: a, b"
    assert result.to_command_result().content == result.failure_text


def test_a_refusal_with_nothing_to_list_still_reads_as_a_sentence() -> None:
    result = ModelSwitchResult(ok=False, model_name='quo"te ünï')

    assert result.failure_text == "Unknown model: 'quo\"te ünï'. Available: "


def test_a_success_ignores_the_available_list() -> None:
    result = ModelSwitchResult(ok=True, model_name="m", available=["a"], error="stale")

    out = result.to_command_result()

    assert (out.kind, out.content, out.action) == (
        CommandResultKind.INFO,
        "Switched to model: m",
        CommandAction.MODEL_SWITCHED,
    )
