"""``OutputStyleSelection``: the styles a session knows and the one that is active.

Rediscovery (``adopt``) keeps the person's choice when it survives and
otherwise falls back, in order, to ``default``, the first style by name, and
nothing.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.output_styles import OutputStyle
from igni_core.core.session.output_style_selection import OutputStyleSelection


def _styles(*names: str) -> dict[str, OutputStyle]:
    return {
        n: OutputStyle(name=n, path=Path("/dev/null"), description=n, body=f"{n} body")
        for n in names
    }


class TestConstruction:
    def test_it_starts_empty_with_no_active_style(self):
        selection = OutputStyleSelection()

        assert selection.styles == {}
        assert selection.active == ""

    def test_none_styles_are_an_empty_set(self):
        assert OutputStyleSelection(None, active="x").styles == {}

    def test_the_given_active_name_is_kept(self):
        assert OutputStyleSelection(_styles("a"), active="a").active == "a"

    def test_it_copies_the_dict_so_the_callers_mutation_does_not_leak(self):
        given = _styles("a")
        selection = OutputStyleSelection(given, active="a")

        given["b"] = _styles("b")["b"]

        assert list(selection.styles) == ["a"]


class TestSelect:
    def test_it_sets_the_active_name(self):
        selection = OutputStyleSelection(_styles("a", "b"), active="a")

        selection.select("b")

        assert selection.active == "b"

    def test_a_name_that_is_not_a_known_style_is_accepted_as_is(self):
        selection = OutputStyleSelection(_styles("a"), active="a")

        selection.select("ghost")

        assert selection.active == "ghost"


class TestAdopt:
    def test_the_chosen_style_is_kept_when_the_new_set_still_has_it(self):
        selection = OutputStyleSelection(_styles("default", "terse"), active="terse")
        discovered = _styles("default", "terse", "extra")

        selection.adopt(discovered)

        assert selection.active == "terse"
        assert selection.styles is discovered

    def test_a_vanished_choice_falls_back_to_default(self):
        selection = OutputStyleSelection(_styles("a", "default"), active="a")

        selection.adopt(_styles("alpha", "default"))

        assert selection.active == "default"

    def test_with_no_default_the_first_by_name_is_taken(self):
        selection = OutputStyleSelection(_styles("gone"), active="gone")

        selection.adopt(_styles("zebra", "Éclair", "mango"))

        assert selection.active == "mango"

    def test_an_empty_discovery_clears_the_active_name(self):
        selection = OutputStyleSelection(_styles("a"), active="a")

        selection.adopt({})

        assert selection.styles == {}
        assert selection.active == ""

    def test_a_ghost_selection_is_replaced_on_the_next_adoption(self):
        selection = OutputStyleSelection(_styles("a"), active="a")
        selection.select("ghost")

        selection.adopt(_styles("a", "b"))

        assert selection.active == "a"
