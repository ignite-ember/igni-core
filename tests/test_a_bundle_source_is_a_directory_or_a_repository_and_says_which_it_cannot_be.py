"""Where ``igni ext install SOURCE`` takes its bundle from, and what it refuses."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.plugins.git import GitClient
from igni_core.extensions.bundle_source import BundleSource, BundleSourceError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "origin"
    (root / "ext").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "ext" / "manifest.json").write_text("one")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


class _NoGit(GitClient):
    def is_available(self) -> bool:
        return False


@pytest.mark.parametrize(
    "source",
    [
        "http://h/x",
        "https://h/x",
        "git@h:x/y.git",
        "ssh://h/x",
        "git://h/x",
        "file:///srv/x",
    ],
)
def test_these_are_repositories(source: str):
    assert BundleSource(source).is_repository is True


@pytest.mark.parametrize(
    "source", ["./x", "/srv/x", "relative:with-colon", "ftp://h/x", "HTTPS://h/x"]
)
def test_anything_else_is_a_path(source: str):
    assert BundleSource(source).is_repository is False


def test_a_marketplace_reference_is_refused_and_names_where_extensions_come_from():
    with pytest.raises(
        BundleSourceError, match=r"@acme/x looks like a marketplace reference.*no marketplace"
    ):
        BundleSource("@acme/x")


def test_a_directory_is_the_bundle(tmp_path: Path):
    assert BundleSource(str(tmp_path)).directory() == tmp_path


def test_a_path_that_is_not_a_directory_is_refused_naming_it(tmp_path: Path):
    missing = tmp_path / "nope"
    with pytest.raises(BundleSourceError, match="is not a directory, and does not look like"):
        BundleSource(str(missing)).directory()


@pytest.mark.parametrize("kw", [{"ref": "v1"}, {"subdir": "ext"}])
def test_ref_and_subdir_are_for_repositories_only(tmp_path: Path, kw: dict[str, str]):
    with pytest.raises(
        BundleSourceError, match="--ref and --subdir are for installing from a repository"
    ):
        BundleSource(str(tmp_path), **kw).directory()


def test_an_air_gapped_network_refuses_an_outside_repository_before_anything_starts():
    started: list[str] = []
    source = BundleSource("https://github.example/x.git")
    network = NetworkPolicy(air_gapped=True)
    with (
        pytest.raises(BundleSourceError, match=r"air-gapped.*Or clone it somewhere"),
        source.clone(network, GitClient(), on_start=lambda: started.append("go")),
    ):
        pass
    assert started == []


def test_without_git_the_clone_is_refused_and_nothing_is_announced():
    started: list[str] = []
    with (
        pytest.raises(BundleSourceError, match="git is not on PATH"),
        BundleSource("https://h/x").clone(
            NetworkPolicy(), _NoGit(), on_start=lambda: started.append("go")
        ),
    ):
        pass
    assert started == []


@needs_git
def test_a_repository_is_cloned_for_the_block_and_deleted_after(repo: Path):
    started: list[str] = []
    with BundleSource(f"file://{repo}").clone(
        NetworkPolicy(), GitClient(), on_start=lambda: started.append("go")
    ) as checkout:
        assert started == ["go"]
        assert (checkout.bundle / "ext" / "manifest.json").read_text() == "one"
        assert checkout.commit == _git(repo, "rev-parse", "HEAD")
        kept = checkout.bundle
    assert not kept.exists()


@needs_git
def test_a_subdirectory_is_the_bundle_and_a_missing_one_is_refused(repo: Path):
    url = f"file://{repo}"
    with BundleSource(url, subdir="ext").clone(NetworkPolicy(), GitClient()) as checkout:
        assert (checkout.bundle / "manifest.json").is_file()
    with (
        pytest.raises(BundleSourceError, match="'gone' is not a directory in"),
        BundleSource(url, subdir="gone").clone(NetworkPolicy(), GitClient()),
    ):
        pass


@needs_git
def test_a_branch_is_cloned_and_a_commit_is_checked_out_with_full_history(repo: Path):
    first = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "ext" / "manifest.json").write_text("side")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "ext" / "manifest.json").write_text("two")
    _git(repo, "commit", "-q", "-am", "two")
    url = f"file://{repo}"

    with BundleSource(url, ref="side").clone(NetworkPolicy(), GitClient()) as checkout:
        assert (checkout.bundle / "ext" / "manifest.json").read_text() == "side"
    with BundleSource(url, ref=first).clone(NetworkPolicy(), GitClient()) as checkout:
        assert (checkout.bundle / "ext" / "manifest.json").read_text() == "one"
        assert checkout.commit == first


@needs_git
def test_a_clone_git_cannot_make_is_one_sentence_naming_the_source(tmp_path: Path):
    url = f"file://{tmp_path / 'absent'}"
    with (
        pytest.raises(BundleSourceError, match=r"cloning file://.*absent failed: "),
        BundleSource(url).clone(NetworkPolicy(), GitClient()),
    ):
        pass
