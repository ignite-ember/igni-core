"""Two calls igni's web client makes that igni did not answer.

* ``get_cloud_plan`` — the account menu asks who is signed in. The
  controller and the wire model were there; the RPC was never registered,
  so every call came back ``Unknown RPC method``.
* ``search_code`` — the client sent the snippet as ``snippet``; igni reads
  ``query``. The unknown key was ignored, so the error said only that
  ``query`` was missing. Now the stray key is named too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.auth import AuthRpcHandler
from igni_core.backend.rpc_handlers.panels import PanelRpcHandler
from igni_core.backend.schemas_rpc import CloudPlan
from igni_core.protocol.rpc import RpcMethod


async def test_get_cloud_plan_answers_who_is_signed_in() -> None:
    ctx = MagicMock()
    plan = CloudPlan(tier="team", org_name="Acme", email="a@acme.io", full_name="Ada")
    ctx.backend.controllers.auth.get_cloud_plan = AsyncMock(return_value=plan)

    answer = await AuthRpcHandler(ctx).methods()[RpcMethod.GET_CLOUD_PLAN]({})

    assert answer == plan


async def test_get_cloud_plan_answers_null_when_nobody_is_signed_in() -> None:
    ctx = MagicMock()
    ctx.backend.controllers.auth.get_cloud_plan = AsyncMock(return_value=None)

    assert await AuthRpcHandler(ctx).methods()[RpcMethod.GET_CLOUD_PLAN]({}) is None


def test_search_code_names_a_snippet_sent_under_another_name() -> None:
    call = PanelRpcHandler(MagicMock()).methods()[RpcMethod.SEARCH_CODE]

    with pytest.raises(RpcArgsError) as refused:
        call({"snippet": "def main"})

    assert "'query' is required" in str(refused.value)
    assert "'snippet' is not an argument of this call" in str(refused.value)
