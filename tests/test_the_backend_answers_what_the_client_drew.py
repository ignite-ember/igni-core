"""What the client drew of a card, answered by the real backend.

The client tells the backend what it drew of a card the agent drew, and
the backend answers with the card, the draw and how much of it was
drawn. The visualizer's recap quotes that verdict, so the agent is told
what is on the screen.

Pinned:

* the answer says the verdict was recorded, and names the card, the
  draw and how much of the card was drawn;
* an element the client could not draw is not counted as drawn;
* the call answers at all — the result type is resolved when the answer
  is built, not only when it is annotated.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for


@pytest.fixture
def backend(tmp_path) -> BackendServer:
    return BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)


@pytest.fixture
def table(backend):
    return rpc_table_for(backend, MagicMock())


async def _ask(table: dict, method: RpcMethod, wire: dict[str, Any]) -> Any:
    result = table[method](wire)
    if hasattr(result, "__await__"):
        return await result
    return result


async def test_the_answer_says_the_verdict_was_recorded(table):
    answer = await _ask(
        table,
        RpcMethod.REPORT_VISUALIZATION_VERDICT,
        {"spec_id": "card-1", "revision": 1, "total": 3, "failed": [], "issues": []},
    )

    assert answer.ok is True
    assert answer.spec_id == "card-1"
    assert answer.revision == 1
    assert answer.total == 3
    assert answer.drawn == 3


async def test_an_element_the_client_could_not_draw_is_not_drawn(table):
    answer = await _ask(
        table,
        RpcMethod.REPORT_VISUALIZATION_VERDICT,
        {
            "spec_id": "card-2",
            "revision": 2,
            "total": 3,
            "failed": [{"type": "BarGraph", "message": "the data was empty"}],
            "issues": ["a child is not an element of the card"],
        },
    )

    assert answer.ok is True
    assert answer.spec_id == "card-2"
    assert answer.revision == 2
    assert answer.total == 3
    assert answer.drawn == 2


def test_the_result_type_is_there_when_the_answer_is_built():
    """The annotation alone is not the type: it is resolved at runtime.

    A result type imported only for typing is resolved by the frame that
    builds the answer, and the call fails there — so the name has to be
    there when the method runs, not only when it is read.
    """
    from igni_core.backend import server as server_module

    assert hasattr(server_module, "VisualizationVerdictResult")
