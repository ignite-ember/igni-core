"""A catalog's plugin ``source`` is resolved to a directory of a clone.

The marketplace catalog is data from a repository somebody else
controls. A relative ``./plugins/x`` source names a folder of the
marketplace's own clone; ``../`` or an absolute path would name
something outside it, and the installer joins the subdirectory onto its
clone directory, so such a source is unresolvable, not followed.
"""

from __future__ import annotations

import pytest

from igni_core.core.plugins.models import MarketplacePluginEntry

_MKT = "https://example.com/mkt.git"


def _resolve(source: object, **extra: object):
    entry = MarketplacePluginEntry.model_validate({"name": "p", "source": source, **extra})
    return entry.resolved_source(_MKT)


def test_a_relative_source_descends_into_the_marketplace_clone() -> None:
    resolved = _resolve("./plugins/x", branch="stable")
    assert resolved is not None
    assert (resolved.kind, resolved.url, resolved.subdir, resolved.ref) == (
        "relative",
        _MKT,
        "plugins/x",
        "stable",
    )


def test_a_relative_source_keeps_the_leading_dot_of_a_hidden_directory() -> None:
    resolved = _resolve("./.hidden/x")
    assert resolved is not None
    assert resolved.subdir == ".hidden/x"


def test_a_relative_source_drops_only_whole_dot_slash_segments() -> None:
    resolved = _resolve("././plugins/x")
    assert resolved is not None
    assert resolved.subdir == "plugins/x"


@pytest.mark.parametrize(
    "source",
    ["../x", "./../x", "./plugins/../../x", "../../etc"],
)
def test_a_relative_source_that_climbs_out_of_the_repository_is_unresolvable(
    source: str,
) -> None:
    assert _resolve(source) is None


@pytest.mark.parametrize("path", ["../x", "/etc", "a/../../b", "\\windows", "a\\..\\b"])
def test_a_git_subdir_path_outside_the_clone_is_unresolvable(path: str) -> None:
    assert _resolve({"source": "git-subdir", "url": "https://h/r", "path": path}) is None


def test_a_git_subdir_path_inside_the_clone_resolves() -> None:
    resolved = _resolve({"source": "git-subdir", "url": "https://h/r", "path": "a/..b/c"})
    assert resolved is not None
    assert (resolved.kind, resolved.subdir) == ("git-subdir", "a/..b/c")
