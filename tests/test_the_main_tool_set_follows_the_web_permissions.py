"""The main agent's built-in tool names: a fixed shell-first core, plus the web tools.

Each web tool is its own decision. It is left out when its permission
is ``deny`` or when the registry cannot build it (a missing extra raises
``ImportError``, an unknown name ``ValueError``); any other failure is a
real error and reaches the caller.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.session.agent_builder.main_tools import MainToolSet


class _Registry:
    """A registry that cannot resolve the names in ``broken``, raising ``error``."""

    def __init__(self, broken: set[str] | None = None, error: Exception | None = None) -> None:
        self._broken = broken or set()
        self._error = error
        self.asked: list[list[str]] = []

    def resolve(self, names):
        self.asked.append(list(names))
        if self._broken & set(names):
            raise self._error or ImportError("missing extra")
        return []


def _names(web="ask", fetch="ask", registry=None) -> list[str]:
    tool_set = MainToolSet(PermissionsConfig(web_search=web, web_fetch=fetch))
    return tool_set.names(registry if registry is not None else _Registry())


CORE = ["Write", "Edit", "Bash", "Schedule", "NotebookEdit"]


@pytest.mark.parametrize("setting", ["ask", "allow"])
def test_a_web_tool_that_is_not_denied_is_included_after_the_core(setting):
    assert _names(web=setting, fetch=setting) == [*CORE, "WebSearch", "WebFetch"]


def test_denying_search_leaves_fetch_in():
    assert _names(web="deny", fetch="ask") == [*CORE, "WebFetch"]


def test_denying_fetch_leaves_search_in():
    assert _names(web="ask", fetch="deny") == [*CORE, "WebSearch"]


def test_a_denied_web_tool_is_not_even_probed():
    registry = _Registry()
    _names(web="deny", fetch="deny", registry=registry)
    assert registry.asked == []


@pytest.mark.parametrize("error", [ImportError("no ddgs"), ValueError("unknown tool")])
def test_a_tool_the_registry_cannot_build_is_left_out_quietly(error):
    registry = _Registry(broken={"WebSearch"}, error=error)
    assert _names(registry=registry) == [*CORE, "WebFetch"]


def test_any_other_failure_of_the_registry_is_not_swallowed():
    registry = _Registry(broken={"WebFetch"}, error=RuntimeError("disk on fire"))
    with pytest.raises(RuntimeError, match="disk on fire"):
        _names(registry=registry)


def test_every_call_returns_a_list_of_its_own():
    tool_set = MainToolSet(PermissionsConfig())
    first = tool_set.names(_Registry())
    first.append("Mutated")
    assert tool_set.names(_Registry()) == [*CORE, "WebSearch", "WebFetch"]
    assert tuple(CORE) == MainToolSet.CORE
