"""The permission category and level vocabulary shared by the permission paths."""

from enum import Enum
from typing import Literal

#: One permission level. A ``Literal`` rather than an enum because the three
#: lowercase strings are the wire contract of ``backend/schemas_hitl.py``.
PermissionLevel = Literal["allow", "ask", "deny"]


class PermissionCategory(str, Enum):
    """The per-category permission levels on ``PermissionsConfig``.

    Each value names the matching ``PermissionsConfig`` field.
    ``tool_permissions.schemas.CategoryToToolMap`` keys the catalog tools
    for each category on these members.
    """

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_EXECUTE = "shell_execute"
