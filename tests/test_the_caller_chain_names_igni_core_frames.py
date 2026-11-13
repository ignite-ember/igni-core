"""``CallerContextInspector`` formats the stack for the call log.

Frames of the SDKs that forwarded a request are skipped; a path inside
``igni_core/`` is shortened to start there, any other is its basename;
``unknown`` is what is left when nothing qualifies.
"""

from __future__ import annotations

import os

from igni_core.core.config.caller_inspector import CallerContextInspector


def test_the_chain_is_file_line_function_joined_by_arrows() -> None:
    chain = CallerContextInspector().format_caller_chain(depth=1, max_frames=2)
    first, second = chain.split(" <- ")
    here = os.path.basename(__file__)
    assert first.startswith(f"{here}:")
    assert first.endswith("(test_the_chain_is_file_line_function_joined_by_arrows)")
    assert second  # the caller's frame follows


def test_a_path_inside_igni_core_is_shortened_to_start_there() -> None:
    # Compile a function under a fake igni_core path so its frame carries it.
    ns: dict = {"Inspector": CallerContextInspector}
    code = compile(
        "def f():\n    return Inspector().format_caller_chain(depth=1, max_frames=1)\n"
        "def g():\n    return f()\n",
        "/somewhere/src/igni_core/core/thing.py",
        "exec",
    )
    exec(code, ns)  # noqa: S102
    assert ns["g"]().startswith("core/thing.py:2(f)")


def test_sdk_frames_are_skipped() -> None:
    ns: dict = {"Inspector": CallerContextInspector}
    exec(  # noqa: S102
        compile(
            "def inner():\n    return Inspector().format_caller_chain(depth=1, max_frames=4)\n"
            "def outer():\n    return inner()\n",
            "/venv/lib/openai/resources/chat.py",
            "exec",
        ),
        ns,
    )
    chain = ns["outer"]()
    assert "chat.py" not in chain
    assert chain.startswith(os.path.basename(__file__))


def test_custom_markers_choose_what_is_skipped() -> None:
    chain = CallerContextInspector(sdk_markers=(os.path.basename(__file__),)).format_caller_chain(
        depth=1, max_frames=1
    )
    assert os.path.basename(__file__) not in chain


def test_max_frames_caps_the_chain() -> None:
    inspector = CallerContextInspector()
    assert len(inspector.format_caller_chain(depth=0, max_frames=1).split(" <- ")) == 1
    assert len(inspector.format_caller_chain(depth=0, max_frames=3).split(" <- ")) == 3


def test_nothing_qualifying_is_unknown() -> None:
    assert CallerContextInspector(sdk_markers=("",)).format_caller_chain() == "unknown"
    assert CallerContextInspector().format_caller_chain(depth=10_000) == "unknown"
