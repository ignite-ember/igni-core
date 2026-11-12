"""What the built-in command registry does with an entry, beyond listing it.

``tests/test_commands_load_on_first_use.py`` pins that an idle backend
imports no command. These pin the rest of an entry's life: its module is
imported on the first run and no sooner, the shim it yields is cached,
a class target is built once, a shim that takes no arguments is never
handed any, an unknown slash falls through to the next tier, and a module
that will not import fails loudly instead of being skipped.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from igni_core.backend.builtin_command_registry import (
    BUILTIN_REGISTRY,
    BuiltinCommand,
    BuiltinCommandRegistry,
)


@pytest.fixture
def shim_module(tmp_path, monkeypatch):
    """A real module on disk, so "imported" means what it means in production."""
    name = "audit_registry_shims"
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            """
            calls = []

            async def with_args(handler, args):
                calls.append(("with_args", handler, args))
                return "with:" + args

            async def without_args(handler):
                calls.append(("without_args", handler))
                return "bare"

            built = []

            class Klass:
                def __init__(self):
                    built.append(self)

                async def run(self, handler, args):
                    return "class:" + args
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    yield name
    sys.modules.pop(name, None)


def test_the_module_is_imported_on_first_use_and_not_before(shim_module):
    entry = BuiltinCommand("/x", "d", f"{shim_module}:with_args")

    assert (entry.bare, entry.module) == ("x", shim_module)
    assert shim_module not in sys.modules, "building the entry must not import it"

    entry.resolve()

    assert shim_module in sys.modules


async def test_a_resolved_shim_is_cached(shim_module):
    entry = BuiltinCommand("/x", "d", f"{shim_module}:with_args")

    first = entry.resolve()
    sys.modules.pop(shim_module)  # a second import would build a different function

    assert entry.resolve() is first
    assert await first("H", "a b") == "with:a b"


async def test_a_shim_that_takes_no_arguments_is_called_without_them(shim_module):
    entry = BuiltinCommand("/x", "d", f"{shim_module}:without_args", takes_args=False)

    assert await entry.resolve()("H", "ignored words") == "bare"
    assert sys.modules[shim_module].calls == [("without_args", "H")]


async def test_a_class_target_is_built_once_and_its_run_is_used(shim_module):
    entry = BuiltinCommand("/x", "d", f"{shim_module}:Klass")

    run = entry.resolve()
    assert await run("H", "1") == "class:1"
    assert await entry.resolve()("H", "2") == "class:2"

    assert len(sys.modules[shim_module].built) == 1


def test_a_target_that_will_not_import_fails_loudly():
    entry = BuiltinCommand("/x", "d", "audit_no_such_module_anywhere:cmd")

    with pytest.raises(ModuleNotFoundError):
        entry.resolve()


def test_a_target_missing_its_attribute_fails_loudly(shim_module):
    with pytest.raises(AttributeError):
        BuiltinCommand("/x", "d", f"{shim_module}:nope").resolve()


def test_the_leading_slash_is_stripped_from_the_bare_name():
    assert BuiltinCommand("/agents", "d", "m:a").bare == "agents"


async def test_an_unknown_slash_falls_through_to_the_next_tier():
    assert await BUILTIN_REGISTRY.dispatch(None, "/definitely-not-a-command", "x") is None


async def test_dispatch_needs_the_slash():
    """A bare name is not a slash command; markdown and skills get it."""
    assert await BUILTIN_REGISTRY.dispatch(None, "quit", "") is None


async def test_dispatch_runs_the_registered_shim_with_the_arguments(shim_module, monkeypatch):
    registry = BuiltinCommandRegistry()
    entry = BuiltinCommand("/mine", "d", f"{shim_module}:with_args")
    monkeypatch.setattr(registry, "_by_slash", {"/mine": entry})

    assert await registry.dispatch("H", "/mine", "a b") == "with:a b"


def test_get_takes_a_slash_or_a_bare_name_and_none_for_an_unknown_one():
    by_slash = BUILTIN_REGISTRY.get("/agents")
    assert by_slash is not None and by_slash is BUILTIN_REGISTRY.get("agents")
    assert BUILTIN_REGISTRY.get("/nope") is None
    assert BUILTIN_REGISTRY.get("nope") is None
    assert BUILTIN_REGISTRY.get("") is None


def test_describe_gives_the_description_or_nothing():
    assert BUILTIN_REGISTRY.describe("/agents") == "List and manage agents"
    assert BUILTIN_REGISTRY.describe("agents") == "List and manage agents"
    assert BUILTIN_REGISTRY.describe("nope") == ""


def test_names_are_bare_and_in_registration_order():
    names = BUILTIN_REGISTRY.names()

    assert names[:3] == ["quit", "exit", "help"]
    assert names == [e.bare for e in BUILTIN_REGISTRY.entries()]
    assert isinstance(BUILTIN_REGISTRY.entries(), tuple)


def test_quit_and_exit_share_one_shim():
    quit_, exit_ = BUILTIN_REGISTRY.get("quit"), BUILTIN_REGISTRY.get("exit")

    assert quit_.target == exit_.target and quit_.target.endswith(".cmd_misc:cmd_quit")
