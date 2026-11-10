"""The files a chat message carries, as the host model hands them over.

The host schema's ``attachments`` parameter is a ``dict[str, str]`` — path
to content — on the wire (:mod:`.definition`). On the model side it is
this model, so the seam between the host model and igni's backend
carries a model rather than a raw dict, and each side says what it
means: a message carries these files.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatAttachments(BaseModel):
    """The files a chat message carries: path → content.

    Empty when the message carries none — which is what the backend
    asks about when a message queued for a running session may not
    carry attachments (``backend/service_event_delivery.py``).
    """

    files: dict[str, str] = Field(default_factory=dict)

    def has_files(self) -> bool:
        """Whether there is a file to carry."""
        return bool(self.files)


__all__ = ["ChatAttachments"]
