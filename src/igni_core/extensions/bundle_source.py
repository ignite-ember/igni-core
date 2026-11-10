"""Where a development install's bundle comes from: a directory or a repository."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from igni_core.core.network_policy import NetworkPolicy
from igni_core.core.plugins.git import GitClient, GitError


class BundleSourceError(Exception):
    """The source cannot give a bundle. The message is for the person."""


@dataclass(frozen=True)
class Checkout:
    """A cloned bundle, and the commit it was taken at."""

    bundle: Path
    commit: str


class BundleSource:
    """What ``igni ext install SOURCE`` names.

    A repository is cloned to a temporary directory that is deleted on the
    way out: the store keeps its own copy, so a second one under ``~/.igni``
    would be a stale checkout nobody updates and everybody finds.
    """

    #: What counts as a repository rather than a directory. Checked before
    #: the filesystem, so a relative path that happens to contain a colon is
    #: still a path and a URL that happens to exist on disk is still a URL.
    #:
    #: ``file://`` is here on purpose: it is how an air-gapped deployment
    #: installs from a mirror it already has, and it is what makes the clone
    #: path testable without a network.
    REPOSITORY = ("http://", "https://", "git@", "ssh://", "git://", "file://")

    def __init__(self, source: str, *, ref: str | None = None, subdir: str | None = None) -> None:
        if source.startswith("@"):
            raise BundleSourceError(
                f"{source} looks like a marketplace reference, and extensions have "
                "no marketplace. They arrive from their services through igni "
                "server when you are signed in; a development install takes a "
                "directory or a repository URL."
            )
        self.source = source
        self.ref = ref
        self.subdir = subdir

    @property
    def is_repository(self) -> bool:
        return self.source.startswith(self.REPOSITORY)

    def directory(self) -> Path:
        """The bundle, when the source is a directory."""
        bundle = Path(self.source)
        if not bundle.is_dir():
            raise BundleSourceError(
                f"{self.source} is not a directory, and does not look like a repository URL either."
            )
        if self.ref or self.subdir:
            raise BundleSourceError(
                f"--ref and --subdir are for installing from a repository; "
                f"{self.source} is a directory."
            )
        return bundle

    @contextmanager
    def clone(
        self,
        network: NetworkPolicy,
        git: GitClient,
        *,
        on_start: Callable[[], None] | None = None,
    ) -> Iterator[Checkout]:
        """The bundle, cloned from the repository for as long as the block runs.

        Under ``air_gapped`` a repository outside the network is refused
        before git starts, so no connection is attempted. ``on_start`` runs
        once both checks have passed, just before git does, so "cloning …"
        is never said about a clone that was refused.
        """
        if not network.allows(self.source):
            raise BundleSourceError(
                f"{network.refusal(self.source, what='the repository')} Or clone it "
                "somewhere this machine can read and install from that directory."
            )
        if not git.is_available():
            raise BundleSourceError(
                "git is not on PATH, so a repository cannot be cloned. Clone it "
                "yourself and pass the directory."
            )
        if on_start is not None:
            on_start()
        with tempfile.TemporaryDirectory(prefix="igni-ext-") as tmp:
            checkout = Path(tmp) / "bundle"
            commit = self._clone_into(checkout, git)
            bundle = checkout / self.subdir if self.subdir else checkout
            if not bundle.is_dir():
                raise BundleSourceError(f"{self.subdir!r} is not a directory in {self.source}.")
            yield Checkout(bundle=bundle, commit=commit)

    def _clone_into(self, checkout: Path, git: GitClient) -> str:
        ref = self.ref
        # A commit cannot be given to ``git clone --branch`` on older gits, so
        # a SHA is cloned-then-checked-out — the same split ``PluginInstaller``
        # makes, because ``--ref`` takes whatever a person would think to type.
        sha = GitClient.looks_like_sha(ref) if ref else False
        try:
            # A shallow clone holds only the tip, so checking out an older
            # commit fails with "unable to read tree" — which reads like a
            # corrupt repository rather than like the depth it is. Full
            # history only when a commit was asked for; a branch, a tag and
            # "latest" stay shallow.
            git.clone(self.source, checkout, ref=None if sha else ref, shallow=not sha)
            if ref and sha:
                git.checkout(checkout, ref)
            return git.current_sha(checkout)
        except GitError as exc:
            raise BundleSourceError(f"cloning {self.source} failed: {exc}") from exc


__all__ = ["BundleSource", "BundleSourceError", "Checkout"]
