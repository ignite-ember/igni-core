"""Why an extension was not installed.

:class:`InstallError` is raised for everything decided *before* the
extension is registered. After that point a failure is recorded as
:attr:`~igni_core.extensions.registry.ExtensionState.FAILED` instead,
because an extension that exists and is broken is a different thing
from one that was never accepted.

It has a module of its own because every collaborator of an install
raises it — the preflight, the packaging, the service — and none of
them may import another to agree on it.
"""

from __future__ import annotations


class InstallError(Exception):
    """The bundle will not be installed, and why."""
