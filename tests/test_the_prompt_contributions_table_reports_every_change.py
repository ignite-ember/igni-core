"""The table of what extensions do to prompts: who changed, who was told, who failed.

Each registering call answers whether anything moved; every watcher
hears of a change; one that raises is reported to the caller and does
not stop the others.
"""

from __future__ import annotations

import functools
import logging

from igni_core.core.prompts.contribution_models import EditOp, PromptEdit, PromptOverride
from igni_core.core.prompts.contributions import PromptContributions, WatchFailure


def _append(target: str, text: str, when: str | None = None) -> PromptEdit:
    return PromptEdit(target=target, op=EditOp.APPEND, name="n", text=text, when=when)


def _override(target: str, text: str, when: str | None = None) -> PromptOverride:
    return PromptOverride(target=target, text=text, when=when)


class _Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


class TestWhatChangedMeans:
    def test_registering_and_removing_an_owner_change_the_table(self):
        table = PromptContributions()
        assert table.set_owner("a", "A", edits=[_append("main", "x")]).changed is True
        assert table.remove_owner("a").changed is True

    def test_removing_an_owner_that_is_not_there_changes_nothing_and_tells_no_one(self):
        table = PromptContributions()
        watcher = _Counter()
        table.watch(watcher)
        change = table.remove_owner("ghost")
        assert change.changed is False and change.failures == []
        assert watcher.calls == 0

    def test_the_same_priority_again_changes_nothing(self):
        table = PromptContributions()
        watcher = _Counter()
        table.watch(watcher)
        assert table.set_priority(["a"]).changed is True
        assert table.set_priority(["a"]).changed is False
        assert table.set_priority([]).changed is True
        assert watcher.calls == 2

    def test_a_condition_reported_before_the_owner_registers_is_kept_without_telling_anyone(self):
        table = PromptContributions()
        watcher = _Counter()
        table.watch(watcher)
        assert table.hold("a", {"ready": True}).changed is False
        assert watcher.calls == 0
        table.set_owner("a", "A", edits=[_append("main", "late", when="ready")])
        assert [e.text for _, e in table.edits("main")] == ["late"]

    def test_a_condition_reported_again_unchanged_changes_nothing(self):
        table = PromptContributions()
        table.set_owner("a", "A", edits=[_append("main", "x", when="ready")])
        assert table.hold("a", {"ready": True}).changed is True
        assert table.hold("a", {"ready": True}).changed is False
        assert table.hold("a", {"other": False}).changed is False

    def test_a_condition_that_stops_holding_turns_the_contribution_off(self):
        table = PromptContributions()
        table.set_owner(
            "a",
            "A",
            overrides=[_override("agent:x", "o", when="ready")],
            edits=[_append("main", "x", when="ready")],
        )
        assert table.edits("main") == [] and table.override("agent:x") is None
        table.hold("a", {"ready": True})
        assert len(table.edits("main")) == 1 and table.override("agent:x") is not None
        assert table.hold("a", {"ready": False}).changed is True
        assert table.edits("main") == [] and table.override("agent:x") is None

    def test_registering_an_owner_again_keeps_the_conditions_already_reported(self):
        table = PromptContributions()
        table.set_owner("a", "A", edits=[_append("main", "v1", when="ready")])
        table.hold("a", {"ready": True})
        table.set_owner("a", "A", edits=[_append("main", "v2", when="ready")])
        assert [e.text for _, e in table.edits("main")] == ["v2"]


class TestWatchers:
    def test_every_change_is_told_to_every_watcher(self):
        table = PromptContributions()
        first, second = _Counter(), _Counter()
        table.watch(first)
        table.watch(second)
        table.set_owner("a", "A")
        table.set_priority(["a"])
        assert (first.calls, second.calls) == (2, 2)

    def test_a_watcher_that_raises_is_reported_by_name_and_does_not_stop_the_others(self, caplog):
        table = PromptContributions()
        after = _Counter()

        def broken() -> None:
            raise RuntimeError("cannot rebuild")

        table.watch(broken)
        table.watch(after)
        with caplog.at_level(logging.ERROR):
            change = table.set_owner("a", "A")
        assert change.changed is True
        [failure] = change.failures
        assert isinstance(failure, WatchFailure)
        assert failure.error == "cannot rebuild"
        assert failure.watcher.endswith("broken")
        assert after.calls == 1
        assert "a prompt-contributions watcher failed" in caplog.text

    def test_a_watcher_without_a_name_is_reported_by_its_repr(self):
        table = PromptContributions()

        def broken(_: int) -> None:
            raise ValueError("x")

        partial = functools.partial(broken, 1)
        table.watch(partial)
        [failure] = table.set_owner("a", "A").failures
        assert failure.watcher == repr(partial)

    def test_a_watcher_may_add_a_watcher_while_it_is_being_told(self):
        table = PromptContributions()
        late = _Counter()
        table.watch(lambda: table.watch(late))
        assert table.set_owner("a", "A").failures == []
        assert late.calls == 0


class TestReading:
    def test_owners_not_in_the_priority_list_come_after_those_in_it_alphabetically(self):
        table = PromptContributions()
        for owner in ("z", "b", "m", "a"):
            table.set_owner(owner, owner, edits=[_append("main", owner)])
        table.set_priority(["m", "z"])
        assert [owner for owner, _ in table.edits("main")] == ["m", "z", "a", "b"]

    def test_two_overrides_of_one_target_the_higher_one_keeps_it_and_a_warning_names_both(
        self, caplog
    ):
        table = PromptContributions()
        table.set_owner("a", "A", overrides=[_override("main", "from a")])
        table.set_owner("b", "B", overrides=[_override("main", "from b")])
        table.set_priority(["b", "a"])
        with caplog.at_level(logging.WARNING):
            owner, override = table.override("main") or ("", None)
        assert owner == "b" and override is not None and override.text == "from b"
        assert "b and a both override main; b keeps it" in caplog.text

    def test_agents_lists_sorted_live_agent_targets_only(self):
        table = PromptContributions()
        table.set_owner(
            "a",
            "A",
            overrides=[_override("agent:zed", "o"), _override("main", "o")],
            edits=[
                _append("agent:alpha", "e"),
                _append("plan_mode", "e"),
                _append("agent:waiting", "e", when="ready"),
            ],
        )
        assert table.agents() == ["alpha", "zed"]
        table.hold("a", {"ready": True})
        assert table.agents() == ["alpha", "waiting", "zed"]

    def test_an_empty_table_has_nothing(self):
        table = PromptContributions()
        assert table.agents() == [] and table.edits("main") == [] and table.override("main") is None


class TestComposeUsesTheTable:
    def test_compose_folds_the_live_override_and_edits_in_the_table_s_order(self):
        table = PromptContributions()
        table.set_owner("a", "A", overrides=[_override("main", "ours")])
        table.set_owner("b", "B", edits=[_append("main", "first")])
        table.set_owner("c", "C", edits=[_append("main", "second")])
        table.set_priority(["c", "b"])
        composition = table.compose("main", "igni's own")
        assert composition is not None
        rendered = composition.document.render()
        assert rendered.startswith("ours")
        assert rendered.index("second") < rendered.index("first")
        assert "igni's own" not in rendered
        assert composition.override_owner == "a"

    def test_a_table_is_its_own_there_is_no_shared_state_between_tables(self):
        first, second = PromptContributions(), PromptContributions()
        first.set_owner("a", "A", edits=[_append("main", "x")])
        assert second.edits("main") == []
