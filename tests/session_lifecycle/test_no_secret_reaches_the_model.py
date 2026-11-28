"""No secret file's contents reach the model: the bypass guard, cross-checked end to end."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.session_lifecycle.constants import RIG_CONFIG, SECRET_CANARY
from tests.session_lifecycle.invariant_checker import InvariantChecker
from tests.session_lifecycle.lifecycle_rig import LifecycleRig
from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.scripted_tool_call import ScriptedToolCall
from tests.session_lifecycle.verdict import Verdict

pytestmark = [pytest.mark.session_lifecycle]

# The secret reads the model attempts, by a name for the test id.
READS: dict[str, str] = {
    "dotenv": "cat .env",
    "ssh-key": "cat ~/.ssh/id_ed25519",
    "aws": "cat ~/.aws/credentials",
    "igni-credentials": "cat ~/.igni/credentials.json",
}
MODES: dict[str, str] = {
    "default": "",
    "bypass": "  mode: bypassPermissions\n",
}


@pytest.fixture(scope="module", params=list(MODES))
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def rig(mode: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[LifecycleRig]:
    config = RIG_CONFIG.replace("permissions:\n", f"permissions:\n{MODES[mode]}")
    rig = LifecycleRig(tmp_path_factory.mktemp(f"secrets-{mode}"), extra_config=config)
    credentials = rig.home / ".igni" / "credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text(f'{{"note": "{SECRET_CANARY}"}}\n')
    for name, command in READS.items():
        rig.script.add(
            f"show me the {name} file",
            ModelTurn(
                text="Reading it.",
                tool_calls=[
                    ScriptedToolCall(name="run_shell_command", arguments={"command": command})
                ],
            ),
            ModelTurn(text="Done."),
        )
    rig.start()
    try:
        yield rig
    finally:
        rig.stop()


@pytest.mark.parametrize("secret", list(READS))
async def test_a_secret_file_is_never_shown_to_the_model(
    rig: LifecycleRig, mode: str, secret: str, request: pytest.FixtureRequest
) -> None:
    cell = f"secret.{mode}.{secret}"
    Verdict.expect_known(request, cell)
    mark = f"[{cell}]"
    checker = InvariantChecker(rig, mark)
    client = await rig.new_session(cell)
    try:
        text = f"show me the {secret} file {mark}"
        sent = await client.send(text)
        frame = await client.wait_for(
            lambda f: (f.type == "run_paused") or (f.type == "stream_end" and f.id == sent),
            since=0,
            timeout=30,
        )
        if frame.type == "run_paused":
            await client.quiet()
            reply = await client.answer("reject", "once")
            await client.wait_for(
                lambda f: f.type == "stream_end" and f.id == reply, since=0, timeout=30
            )
        await client.quiet()
        checker.check_requests("the read")
    finally:
        await rig.close_windows()
    Verdict.settle(cell, checker.violations)
