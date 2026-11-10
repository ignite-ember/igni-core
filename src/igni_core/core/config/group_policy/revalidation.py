"""What one group-policy revalidation attempt did.

The portal being unreachable is an *expected* failure: the session
carries on with the pack it already has, and the background poller
catches up. So the failure comes back as a value — the caller reads
it and decides — rather than as an exception every caller has to
catch and swallow.

One class, one module: the outcome of asking the server whether the
group changed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PolicyRevalidation(BaseModel):
    """The outcome of one revalidation attempt.

    * ``changed`` — a new pack landed.
    * ``error`` — why the attempt failed, when it did. ``None`` when it
      did not fail; "nothing changed" is not a failure.
    """

    model_config = ConfigDict(frozen=True)

    changed: bool = False
    error: str | None = None
