"""A rule's ``paths:`` given as one value rather than a list scopes it to that glob.

Read as no paths, ``paths: docs/**`` applied the rule everywhere: the opposite of
what its author wrote."""

from __future__ import annotations

import pytest

from igni_core.core.utils.context_frontmatter import parse_frontmatter


@pytest.mark.parametrize(
    "line",
    ["paths: docs/**", 'paths: "docs/**"', "paths: 'docs/**'", "paths:   docs/**  "],
)
def test_one_value_is_that_one_glob(line):
    paths, body = parse_frontmatter(f"---\n{line}\n---\nUse the house style.\n")

    assert paths == ["docs/**"]
    assert body == "Use the house style.\n"


def test_a_glob_with_a_brace_list_stays_one_glob():
    paths, _ = parse_frontmatter("---\npaths: src/**/*.{ts,tsx}\n---\nbody")

    assert paths == ["src/**/*.{ts,tsx}"]


def test_one_value_then_another_key_is_still_one_glob():
    paths, _ = parse_frontmatter("---\npaths: docs/**\ndescription: docs\n  - not a path\n---\nb")

    assert paths == ["docs/**"]


def test_an_empty_quoted_value_is_no_paths():
    assert parse_frontmatter('---\npaths: ""\n---\nb') == ([], "b")
