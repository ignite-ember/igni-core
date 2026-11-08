"""The permission category vocabulary shared by the tool-permission path."""

from enum import Enum


class PermissionCategory(str, Enum):
    """The per-category permission levels on ``PermissionsConfig``.

    Each value names the matching ``PermissionsConfig`` field.
    ``tool_permissions.schemas.CategoryToToolMap`` keys the catalog tools
    for each category on these members.
    """

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    SHELL_EXECUTE = "shell_execute"
