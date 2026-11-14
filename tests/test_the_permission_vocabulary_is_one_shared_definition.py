"""The permission vocabulary: three categories, three levels, one definition.

``PermissionCategory`` names the per-category fields on ``PermissionsConfig``
and keys ``CategoryToToolMap``; ``PermissionLevel`` is the wire contract of
the approval path. Both are reachable from the package and from its
``schemas`` module, and the dead ``PermissionGuard`` era names stay gone.
"""

from __future__ import annotations

import subprocess
import sys
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

import igni_core.core.config.permissions as permissions_pkg
from igni_core.core.config.permissions import schemas as permissions_schemas
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.config.tool_permissions.schemas import CategoryToToolMap


def test_the_package_exports_exactly_the_category_and_the_level() -> None:
    assert sorted(permissions_pkg.__all__) == ["PermissionCategory", "PermissionLevel"]
    assert permissions_pkg.PermissionCategory is permissions_schemas.PermissionCategory
    assert permissions_pkg.PermissionLevel is permissions_schemas.PermissionLevel


@pytest.mark.parametrize(
    "name",
    [
        "PermissionGuard",
        "PermissionPolicy",
        "AllowlistStore",
        "ApprovalPrompt",
        "SessionApprovalCache",
    ],
)
def test_the_removed_guard_machinery_is_not_importable_from_the_package(name: str) -> None:
    assert not hasattr(permissions_pkg, name)
    assert not hasattr(permissions_schemas, name)


def test_the_schemas_module_imports_first_in_a_fresh_process_without_a_cycle() -> None:
    code = (
        "from igni_core.core.config.permissions.schemas import PermissionCategory;"
        "print(PermissionCategory.FILE_READ.value)"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "file_read"


def test_the_levels_are_exactly_allow_ask_deny() -> None:
    assert get_args(permissions_pkg.PermissionLevel) == ("allow", "ask", "deny")


class _Typed(BaseModel):
    level: permissions_pkg.PermissionLevel


@pytest.mark.parametrize("good", ["allow", "ask", "deny"])
def test_a_field_typed_with_the_level_accepts_the_three_lowercase_strings(good: str) -> None:
    assert _Typed(level=good).level == good


@pytest.mark.parametrize("bad", ["Allow", "ALLOW", "", "deny_all", None, "allow "])
def test_a_field_typed_with_the_level_refuses_everything_else(bad: object) -> None:
    with pytest.raises(ValidationError):
        _Typed(level=bad)  # type: ignore[arg-type]


def test_the_categories_are_exactly_three_named_strings() -> None:
    category = permissions_pkg.PermissionCategory
    assert {member.name: member.value for member in category} == {
        "FILE_READ": "file_read",
        "FILE_WRITE": "file_write",
        "SHELL_EXECUTE": "shell_execute",
    }


def test_a_category_is_a_string_and_a_json_key() -> None:
    category = permissions_pkg.PermissionCategory
    assert category.FILE_READ == "file_read"
    assert {category.SHELL_EXECUTE: 1}["shell_execute"] == 1
    assert category("file_write") is category.FILE_WRITE
    with pytest.raises(ValueError):
        category("bogus")


def test_every_category_names_a_permissions_config_field_and_has_tools() -> None:
    fields = PermissionsConfig.model_fields
    canonical = CategoryToToolMap().canonical
    for member in permissions_pkg.PermissionCategory:
        assert member.value in fields
        assert canonical[member], member
    assert set(canonical) == set(permissions_pkg.PermissionCategory)
