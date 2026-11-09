"""The menu may not advertise a command the backend cannot run.

The composer carried a hand-written list of thirty-two commands beside
a ``get_slash_commands`` RPC that returns the authoritative forty. Both
directions had drifted:

* ``/workflows`` was offered and does not exist in the backend — that
  one is fine, it is intercepted client-side, and it is now declared as
  such rather than merely tolerated.
* Nine real commands were undiscoverable from the menu: ``/commit``,
  ``/pr``, ``/explain``, ``/resolve-issues``, ``/watcher``,
  ``/test-plan``, ``/migration``, ``/gpu-ai``, ``/exit``.

The frontend takes the backend's list at runtime now, so the drift
cannot recur in the direction that matters. What remains is the
fallback list — used while the RPC is in flight and against a backend
too old to answer — and a fallback that lies is still a lie. This
checks it, the same way ``wire-contract`` checks the message schema:
by reading the TypeScript and comparing it to the Python.

The list itself now lives in ``chat/slashCommands.ts`` rather than in
the composer component: the composer filters it, and the Help page
lists it.
"""

from __future__ import annotations

import re

from tests.siblings import IGNI_APP, needs_app

pytestmark = needs_app

COMMANDS = IGNI_APP / "clients" / "web" / "src" / "chat" / "slashCommands.ts"
#: Where the composer asks the backend for the list it runs on.
COMMANDS_HOOK = IGNI_APP / "clients" / "web" / "src" / "chat" / "useCommands.ts"


def _fallback_commands() -> set[str]:
    """Names from the composer's ``BUILTIN_COMMANDS`` array."""
    source = COMMANDS.read_text(encoding="utf-8")
    start = source.index("export const BUILTIN_COMMANDS")
    end = source.index("];", start)
    return set(re.findall(r'name:\s*"(/[a-z-]+)"', source[start:end]))


def _client_only() -> set[str]:
    """Names the frontend declares it handles itself."""
    source = COMMANDS.read_text(encoding="utf-8")
    start = source.index("export const CLIENT_ONLY_COMMANDS")
    end = source.index("];", start)
    return set(re.findall(r'"(/[a-z-]+)"', source[start:end]))


def _backend_commands() -> set[str]:
    """What the backend's builtin registry dispatches.

    The RPC also reports markdown commands and skills, which are
    per-project and per-user; the built-ins are the part a shipped
    frontend can reasonably know about.
    """
    # The registry imports nothing behind its entries, so it can be the
    # first import: the cycle through ``cmd_context`` that once made it
    # depend on ``core.session`` being imported first is gone.
    from igni_core.backend.builtin_command_registry import BUILTIN_REGISTRY

    return {f"/{name.lstrip('/')}" for name in BUILTIN_REGISTRY.names()}


def test_the_fallback_advertises_nothing_the_backend_lacks():
    """The rule. A command in the menu has to go somewhere."""
    phantom = _fallback_commands() - _backend_commands() - _client_only()

    assert not phantom, (
        f"the composer offers {sorted(phantom)}, which the backend does not "
        "define. Either implement them, remove them, or — if the client "
        "handles them itself, as it does /workflows — add them to "
        "CLIENT_ONLY_COMMANDS with the intercept that justifies it."
    )


def test_client_only_commands_really_are_absent_from_the_backend():
    """The other direction: an exemption that stops being needed is a
    lie of a quieter kind, and it would hide a real conflict if the
    backend later took that name."""
    overlap = _client_only() & _backend_commands()

    assert not overlap, (
        f"{sorted(overlap)} is declared client-only but the backend now "
        "defines it too — the intercept and the command will fight."
    )


def test_the_fallback_is_not_empty():
    """Guards the parser, not the product: a regex that silently
    matched nothing would make both tests above pass forever."""
    assert len(_fallback_commands()) > 20


def test_the_frontend_prefers_the_backends_list():
    """The structural half of the fix. Without the RPC wired, the
    fallback is not a fallback — it is the list again, and this file
    would be guarding a copy nobody reads."""
    hook = COMMANDS_HOOK.read_text(encoding="utf-8")
    catalog = COMMANDS.read_text(encoding="utf-8")

    assert "get_slash_commands" in hook, (
        "the frontend no longer asks the backend for its list, so the "
        "hand-written fallback is the list again"
    )
    merge = catalog[catalog.index("static merge(") :]
    assert re.search(r"if\s*\(\s*!backend\s*\|\|\s*backend\.length\s*===\s*0\s*\)", merge), (
        "the fallback is no longer conditional on the backend having said "
        "nothing, so the hand-written list would win over the backend's"
    )
