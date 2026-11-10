"""Typed schemas + taxonomy for media detection & attachment.

The taxonomy is the shape of the answer: every kind (image / audio /
video / document) is one :class:`MediaKindSpec` row inside a
:class:`MediaTaxonomy`, and polymorphic construction goes through
:meth:`MediaKindSpec.build`. The bundle the resolver produces is a
:class:`MediaAttachments` — one typed field per kind, holding the
live Agno containers.

Contents:

* :class:`MediaKind` — string enum naming the four kinds Agno's
  per-provider plugins accept as attachment containers.
* :class:`MediaBucket` — the four names the bundle's buckets go by.
  The bucket a kind belongs to is part of the taxonomy row, so
  adding a kind is one row rather than an edit in two places.
* :class:`MediaKindSpec` — frozen dataclass, one row per kind.
  Carries the extension set, the Agno container class, the bucket
  the attachment lands in, and a :meth:`build` method that
  constructs the Agno object from either a local filepath or a
  remote URL.
* :class:`MediaTaxonomy` — small object holding a tuple of specs
  with :meth:`classify` for ext lookup and :meth:`default`
  classmethod for the current 4-kind taxonomy.
* :class:`MediaAttachments` — Pydantic model, moved down from
  :mod:`igni_core.backend.schemas_run` so
  :class:`~igni_core.core.utils.media.MediaResolver` can produce
  it directly. One typed list per kind, a typed :meth:`merge`, and
  :meth:`total` (int summary) in place of the dict-shaped idioms.
* :class:`ResolvedText` — Pydantic model, return type of
  :meth:`MediaResolver.resolve_text_references`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from agno.media import Audio, File, Image, Video
from pydantic import BaseModel, Field

#: The names the attachment bundle's buckets go by. Agno's
#: ``arun`` takes one keyword per kind, so these are the keyword
#: names as well as the field names.
MediaBucket = Literal["images", "audio", "videos", "files"]

#: One attachment, whatever its kind.
MediaObject = Image | Audio | Video | File


class MediaKind(str, Enum):
    """The four media kinds Agno's per-provider plugins accept as
    attachment containers."""

    image = "image"
    audio = "audio"
    video = "video"
    document = "document"


@dataclass(frozen=True)
class MediaKindSpec:
    """One row of the media taxonomy — the extension → container
    mapping used to be a string return + two if/elif ladders. Now
    it's one row with :meth:`build` handling both local and remote
    attachment construction.

    ``bucket`` is the name of the :class:`MediaAttachments` field
    the attachment lands in. It differs from ``kind.value`` for
    images (bucket ``images``) and videos (bucket ``videos``) —
    hence the explicit field.
    """

    kind: MediaKind
    extensions: frozenset[str]
    container_cls: type[MediaObject]
    bucket: MediaBucket

    def build(
        self,
        *,
        url: str | None = None,
        filepath: Path | None = None,
    ) -> MediaObject:
        """Construct the Agno media container.

        Exactly one of ``url`` / ``filepath`` must be provided —
        Agno's constructors accept either, and a silent both-set
        would let the constructor pick one non-deterministically.
        """
        assert (url is None) ^ (filepath is None), (
            "MediaKindSpec.build requires exactly one of url / filepath"
        )
        if url is not None:
            return self.container_cls(url=url)
        return self.container_cls(filepath=filepath)


@dataclass(frozen=True)
class MediaTaxonomy:
    """Ordered tuple of :class:`MediaKindSpec` rows + an ext lookup.

    Adding a new kind (``.epub`` → document, or a new
    ``spreadsheet`` bucket) is one row in :meth:`default`. The
    old code required edits across three extension constants and
    two if/elif ladders.
    """

    specs: tuple[MediaKindSpec, ...]

    @cached_property
    def by_extension(self) -> Mapping[str, MediaKindSpec]:
        """Every known extension → its row.

        The lookup the resolver uses. The patterns that find a media
        path are built from these same extensions, so a path one of
        them matched always has a row here.
        """
        return {ext: spec for spec in self.specs for ext in spec.extensions}

    def classify(self, ext: str) -> MediaKindSpec | None:
        """Return the spec whose extension set contains ``ext``,
        or ``None`` if no spec matches. Case-insensitive."""
        return self.by_extension.get(ext.lower())

    @property
    def all_extensions(self) -> frozenset[str]:
        """All known extensions across every kind — used to build
        the URL / file regex alternation in
        :class:`~igni_core.core.utils.media.MediaResolver`."""
        return frozenset(self.by_extension)

    @classmethod
    def default(cls) -> MediaTaxonomy:
        """The current 4-kind taxonomy. Pinned as a classmethod so
        the default set is one call away, but callers can pass a
        custom taxonomy to :class:`MediaResolver` for tests."""
        return cls(
            specs=(
                MediaKindSpec(
                    kind=MediaKind.image,
                    extensions=frozenset(
                        {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".gif",
                            ".webp",
                            ".bmp",
                            ".tiff",
                            ".tif",
                            ".svg",
                            ".avif",
                            ".heic",
                            ".heif",
                        }
                    ),
                    container_cls=Image,
                    bucket="images",
                ),
                MediaKindSpec(
                    kind=MediaKind.audio,
                    extensions=frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma"}),
                    container_cls=Audio,
                    bucket="audio",
                ),
                MediaKindSpec(
                    kind=MediaKind.video,
                    extensions=frozenset({".mp4", ".mov", ".avi", ".webm", ".mkv", ".wmv"}),
                    container_cls=Video,
                    bucket="videos",
                ),
                MediaKindSpec(
                    kind=MediaKind.document,
                    extensions=frozenset({".pdf"}),
                    container_cls=File,
                    bucket="files",
                ),
            )
        )


class MediaAttachments(BaseModel):
    """The attachments one turn carries, one typed list per kind.

    Agno's per-provider model plugins accept different media kinds
    (images / audio / video / files), and ``team.arun`` takes one
    keyword per kind. This model is that set: four lists, each
    holding the live Agno container of its kind.

    The buckets are typed rather than ``list[Any]``, so a caller
    that reads ``attachments.images[0]`` gets an :class:`Image`
    back, and a bucket that holds the wrong kind of object is a
    validation error rather than a surprise at the provider.

    The one place we lower back to a dict is :meth:`to_kwargs` —
    the Agno interop seam.
    """

    images: list[Image] = Field(default_factory=list)
    audio: list[Audio] = Field(default_factory=list)
    videos: list[Video] = Field(default_factory=list)
    files: list[File] = Field(default_factory=list)

    def add(self, bucket: MediaBucket, item: MediaObject) -> None:
        """Put one attachment in the bucket its kind belongs to.

        The bucket is named by the taxonomy row that produced the
        attachment, so the kind and the bucket cannot drift apart.
        """
        buckets: dict[MediaBucket, list[Any]] = {
            "images": self.images,
            "audio": self.audio,
            "videos": self.videos,
            "files": self.files,
        }
        buckets[bucket].append(item)

    def merge(self, other: MediaAttachments) -> None:
        """Merge another attachments bundle into this one in place.

        Preserves existing entries and extends per-kind lists."""
        self.images.extend(other.images)
        self.audio.extend(other.audio)
        self.videos.extend(other.videos)
        self.files.extend(other.files)

    def to_kwargs(self) -> dict[str, Any]:
        """Lower to ``**kwargs`` for ``team.arun(**media.to_kwargs())``.

        Uses raw attribute access — the buckets hold live Agno
        ``Image``/``Audio``/``Video``/``File`` instances that must
        reach ``team.arun`` unchanged. ``model_dump`` would recurse
        into them (they're Pydantic too) and turn them into dicts
        that Agno rejects.

        Omits empty collections so the Agno call site is the same
        shape as the pre-refactor raw-dict path (empty ``images=[]``
        would cause some providers to send an empty images-array
        rather than skip the field)."""
        out: dict[str, Any] = {}
        if self.images:
            out["images"] = self.images
        if self.audio:
            out["audio"] = self.audio
        if self.videos:
            out["videos"] = self.videos
        if self.files:
            out["files"] = self.files
        return out

    def is_empty(self) -> bool:
        """True when no attachment payload is set — the caller can
        skip the ``**media.to_kwargs()`` splat entirely."""
        return not self.to_kwargs()

    def total(self) -> int:
        """Total attachment count across every bucket.

        Replaces the ``sum(len(v) for v in raw_dict.values())``
        idiom PromptBuilder used to run after ``extract_media_urls``
        returned a dict."""
        return len(self.images) + len(self.audio) + len(self.videos) + len(self.files)


class ResolvedText(BaseModel):
    """Return type of :meth:`MediaResolver.resolve_text_references`.

    :attr:`missed` surfaces bare-filename lookups that failed so a
    future :class:`PromptBuilder` Info message can warn the user
    ("photo.png not found in project / Downloads / Desktop / …")
    instead of silently dropping the reference. Not yet consumed
    by any caller — reserved for the follow-up UX pass.

    :attr:`paths` are the references the resolver rewrote in the text.
    :attr:`attachable` is every file the text names that a vision
    model may be sent as media: those, plus the absolute paths already
    written out (an upload's ``@<path>``) that lie in an allowed place —
    see :meth:`MediaResolver.resolve_text_references
    <igni_core.core.utils.media.MediaResolver.resolve_text_references>`.
    """

    text: str
    paths: list[str] = Field(default_factory=list)
    attachable: list[str] = Field(default_factory=list)
    missed: list[str] = Field(default_factory=list)
