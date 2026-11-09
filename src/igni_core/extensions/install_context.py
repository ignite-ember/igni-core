"""The deployment an install happens in.

Four facts decide what an extension may do here: how many people this
backend serves, whether a bundle igni server did not distribute may be
installed, which project an extension may be told about, and where a
declared secret's value comes from. They are the same for every
extension in a deployment, so they travel as one typed model instead of
as four loose arguments that every collaborator has to accept and
pass on.

Nothing here is per-extension: a bundle cannot reach any of it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.profile import DeploymentProfile


class InstallContext(BaseModel):
    """The deployment this install happens in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: How many principals this backend serves.
    profile: DeploymentProfile = Field(default_factory=DeploymentProfile.current)
    #: Whether a bundle igni server did not distribute may be installed —
    #: ``IGNI_DEV_EXTENSIONS``. False everywhere but development, because
    #: the easiest way to ship must not be the one with no provenance.
    allow_development: bool = False
    #: The person's project. Told to a service only when
    #: ``permissions.workspace`` grants it.
    project_dir: Path | None = None
    #: The deployment's secret source. A name the manifest did not
    #: declare is never passed to it.
    secret_resolver: Callable[[str], str | None] | None = None
