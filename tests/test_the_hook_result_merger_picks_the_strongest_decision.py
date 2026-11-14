"""HookResultMerger: how several hooks' results become one."""

from __future__ import annotations

import itertools

import pytest

from igni_core.core.hooks.merger import HookResultMerger
from igni_core.core.hooks.schemas import HookResult, PermissionDecision

D = PermissionDecision


def _merge(*results: HookResult) -> HookResult:
    merger = HookResultMerger()
    for r in results:
        merger.absorb(r)
    return merger.finalize()


def test_nothing_absorbed_continues_with_no_message_and_no_decision():
    assert _merge() == HookResult(should_continue=True, message="", permission_decision=D.NONE)


def test_the_strongest_decision_wins_whatever_the_order():
    ranked = [D.DENY, D.ASK, D.ALLOW, D.DEFER, D.NONE]

    for order in itertools.permutations(ranked):
        merged = _merge(*(HookResult(permission_decision=d) for d in order))
        assert merged.permission_decision is D.DENY, order
    for i, strong in enumerate(ranked):
        weaker = ranked[i + 1 :]
        merged = _merge(*(HookResult(permission_decision=d) for d in [*weaker, strong]))
        assert merged.permission_decision is strong


@pytest.mark.parametrize("decision", list(PermissionDecision))
def test_a_single_decision_is_kept(decision):
    assert _merge(HookResult(permission_decision=decision)).permission_decision is decision


def test_one_hook_that_stops_stops_the_merge_wherever_it_comes():
    stop = HookResult(should_continue=False)
    go = HookResult(should_continue=True)

    assert _merge(stop, go, go).should_continue is False
    assert _merge(go, stop, go).should_continue is False
    assert _merge(go, go, stop).should_continue is False
    assert _merge(go, go).should_continue is True


def test_messages_join_with_a_newline_in_order_and_empty_ones_are_dropped():
    merged = _merge(
        HookResult(message="first"),
        HookResult(message=""),
        HookResult(message="second"),
        HookResult(message="third\nline"),
    )

    assert merged.message == "first\nsecond\nthird\nline"


def test_all_empty_messages_give_an_empty_message():
    assert _merge(HookResult(), HookResult(message="")).message == ""


def test_a_stop_and_a_message_and_a_decision_all_survive_together():
    merged = _merge(
        HookResult(message="why", permission_decision=D.ALLOW),
        HookResult(should_continue=False, permission_decision=D.ASK),
    )

    assert merged == HookResult(should_continue=False, message="why", permission_decision=D.ASK)


def test_finalize_twice_gives_equal_but_separate_results_and_absorbing_goes_on():
    merger = HookResultMerger()
    merger.absorb(HookResult(message="a"))

    first = merger.finalize()
    second = merger.finalize()
    merger.absorb(HookResult(message="b", permission_decision=D.DENY))

    assert first == second
    assert first is not second
    assert merger.finalize() == HookResult(message="a\nb", permission_decision=D.DENY)
    assert first.message == "a"
