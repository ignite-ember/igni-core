"""What ``igni ext install SOURCE`` names: a directory, or a repository cloned for as long as it is needed.

Cloned with real git from real local repositories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.plugins.git import GitClient
from igni_core.extensions.bundle_source import BundleSource, BundleSourceError
from tests.plugin_git_repos import commit_again, git, plugin_repo

OPEN = NetworkPolicy()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return plugin_repo(tmp_path / "src", "bundle")


def _url(path: Path) -> str:
    return path.as_uri()


class NoGit(GitClient):
    def is_available(self) -> bool:
        return False


# ── Which is which ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        "http://h/x.git",
        "https://h/x.git",
        "git@h:o/x.git",
        "ssh://git@h/x.git",
        "git://h/x.git",
        "file:///srv/x.git",
    ],
)
def test_a_url_is_a_repository(source):
    assert BundleSource(source).is_repository


@pytest.mark.parametrize("source", ["", "./a:b", "relative/path", "/abs/path", "ftp://h/x", "C:/x"])
def test_anything_else_is_a_path(source):
    assert not BundleSource(source).is_repository


def test_a_url_that_exists_on_disk_is_still_a_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "https:").mkdir()
    (tmp_path / "https:" / "h").mkdir()
    assert BundleSource("https://h").is_repository


@pytest.mark.parametrize("source", ["@scope/name", "@"])
def test_a_marketplace_reference_is_refused(source):
    with pytest.raises(BundleSourceError, match="no marketplace"):
        BundleSource(source)


# ── A directory ────────────────────────────────────────────────────


def test_a_directory_is_the_bundle(tmp_path):
    assert BundleSource(str(tmp_path)).directory() == tmp_path


def test_something_that_is_not_a_directory_is_refused(tmp_path):
    (tmp_path / "file").write_text("x")
    for source in (str(tmp_path / "file"), str(tmp_path / "missing"), ""):
        with pytest.raises(BundleSourceError, match="is not a directory"):
            BundleSource(source).directory()


@pytest.mark.parametrize("extra", [{"ref": "main"}, {"subdir": "x"}])
def test_a_ref_or_subdir_is_for_a_repository(tmp_path, extra):
    with pytest.raises(BundleSourceError, match="--ref and --subdir are for installing"):
        BundleSource(str(tmp_path), **extra).directory()


# ── A repository ───────────────────────────────────────────────────


def test_a_repository_is_cloned_to_a_directory_that_goes_with_the_block(repo):
    with BundleSource(_url(repo)).clone(OPEN, GitClient()) as checkout:
        kept = checkout.bundle
        assert json.loads((kept / ".claude-plugin" / "plugin.json").read_text())["name"] == "bundle"
        assert checkout.commit == git(repo, "rev-parse", "HEAD")
    assert not kept.exists()


def test_the_checkout_goes_even_when_the_block_raises(repo):
    with pytest.raises(RuntimeError), BundleSource(_url(repo)).clone(OPEN, GitClient()) as checkout:
        kept = checkout.bundle
        raise RuntimeError("install failed")
    assert not kept.exists()


def test_on_start_runs_once_before_the_clone(repo):
    told: list[bool] = []
    source = BundleSource(_url(repo))
    with source.clone(OPEN, GitClient(), on_start=lambda: told.append(True)):
        pass
    assert told == [True]


def test_a_branch_and_a_tag_are_cloned_at_their_commit(repo):
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v1")
    second = commit_again(repo)
    with BundleSource(_url(repo), ref="v1").clone(OPEN, GitClient()) as checkout:
        assert checkout.commit == first
    with BundleSource(_url(repo), ref="main").clone(OPEN, GitClient()) as checkout:
        assert checkout.commit == second
    with BundleSource(_url(repo)).clone(OPEN, GitClient()) as checkout:
        assert checkout.commit == second


@pytest.mark.parametrize("length", [40, 8])
def test_an_older_commit_can_be_asked_for_by_sha(repo, length):
    first = git(repo, "rev-parse", "HEAD")
    commit_again(repo)
    with BundleSource(_url(repo), ref=first[:length]).clone(OPEN, GitClient()) as checkout:
        assert checkout.commit == first
        assert not (checkout.bundle / "more.txt").exists()


def test_a_ref_that_does_not_exist_is_a_refusal_naming_the_source(repo):
    with (
        pytest.raises(BundleSourceError, match="cloning .* failed"),
        BundleSource(_url(repo), ref="nope").clone(OPEN, GitClient()),
    ):
        pass


def test_a_sha_that_does_not_exist_is_a_refusal_naming_the_source(repo):
    with (
        pytest.raises(BundleSourceError, match="cloning .* failed"),
        BundleSource(_url(repo), ref="abcdef1234").clone(OPEN, GitClient()),
    ):
        pass


def test_a_repository_that_does_not_exist_is_a_refusal_naming_the_source(tmp_path):
    with (
        pytest.raises(BundleSourceError, match="cloning .* failed"),
        BundleSource(_url(tmp_path / "gone")).clone(OPEN, GitClient()),
    ):
        pass


def test_a_subdirectory_is_the_bundle(tmp_path):
    repo = tmp_path / "mono"
    (repo / "packages" / "one").mkdir(parents=True)
    (repo / "packages" / "one" / "manifest.json").write_text("{}")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    with BundleSource(_url(repo), subdir="packages/one").clone(OPEN, GitClient()) as checkout:
        assert checkout.bundle.name == "one"
        assert (checkout.bundle / "manifest.json").is_file()


@pytest.mark.parametrize("subdir", ["missing", "README.md"])
def test_a_subdirectory_that_is_not_one_is_refused(tmp_path, subdir):
    repo = tmp_path / "mono"
    repo.mkdir()
    (repo / "README.md").write_text("x")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "one")
    with (
        pytest.raises(BundleSourceError, match="is not a directory in"),
        BundleSource(_url(repo), subdir=subdir).clone(OPEN, GitClient()),
    ):
        pass


def test_a_subdirectory_that_climbs_out_of_the_clone_is_refused(tmp_path, repo):
    outside = tmp_path / "outside"
    outside.mkdir()
    for subdir in ("..", "../..", str(outside)):
        with (
            pytest.raises(BundleSourceError, match="is not a directory in"),
            BundleSource(_url(repo), subdir=subdir).clone(OPEN, GitClient()),
        ):
            pass


# ── Refusals before git starts ─────────────────────────────────────


def test_an_air_gapped_machine_refuses_an_outside_repository_before_git_starts():
    told: list[bool] = []
    air_gapped = NetworkPolicy(air_gapped=True)
    with (
        pytest.raises(BundleSourceError, match="Or clone it somewhere"),
        BundleSource("https://github.com/o/x.git").clone(
            air_gapped, GitClient(), on_start=lambda: told.append(True)
        ),
    ):
        pass
    assert told == []


def test_an_air_gapped_machine_clones_a_mirror_it_has(repo):
    with BundleSource(_url(repo)).clone(NetworkPolicy(air_gapped=True), GitClient()) as checkout:
        assert checkout.bundle.is_dir()


def test_no_git_is_a_refusal_saying_so_and_nothing_is_announced(repo):
    told: list[bool] = []
    with (
        pytest.raises(BundleSourceError, match="git is not on PATH"),
        BundleSource(_url(repo)).clone(OPEN, NoGit(), on_start=lambda: told.append(True)),
    ):
        pass
    assert told == []
