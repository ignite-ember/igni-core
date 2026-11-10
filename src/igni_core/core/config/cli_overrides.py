"""``CliOverrides`` — the ``Settings`` overrides one ``igni`` invocation's flags make.

The models mirror the ``Settings`` blocks they override (``models``,
``display``, ``permissions``, ``hooks``), so :meth:`CliOverrides.to_payload`
is a plain ``model_dump`` of the fields that were set: the shape
:class:`~igni_core.core.config.tiers.cli.CliTier` deep-merges over the tiers
below it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.config.schemas.orchestration import GroupHookMode
from igni_core.core.config.tool_permissions.schemas import PermissionLevel

if TYPE_CHECKING:
    from igni_core.cli.options import CliOptions


class _Overrides(BaseModel):
    """A block of overrides: only the fields a flag set reach the payload."""

    def to_payload(self) -> dict[str, Any]:
        """The fields that differ from their defaults, so the deep-merge
        never clobbers a lower tier's value with an unset one."""
        return self.model_dump(mode="json", exclude_defaults=True)


class ModelsOverrides(_Overrides):
    """``models`` from the command line: ``--model``."""

    default: str | None = None


class DisplayOverrides(_Overrides):
    """``display`` from the command line: ``--verbose`` and ``--quiet``."""

    show_routing: bool | None = None
    show_reasoning: bool | None = None
    show_tool_calls: bool | None = None


class HooksOverrides(_Overrides):
    """``hooks`` from the command line.

    ``group``: ``--strict`` asks before a group-policy hook runs. Combined
    with the user's own setting by ``DeveloperOnlyTier`` — strictest wins.
    """

    group: GroupHookMode | None = None


class PermissionOverrides(_Overrides):
    """CLI-supplied permission overrides.

    ``mode`` is typed via :class:`PermissionMode` (the same enum the
    :class:`PermissionEvaluator` reads) so the strictest-wins policy
    is enforced by the type layer rather than free-form strings.

    The per-category fields — ``file_write``, ``shell_execute``,
    ``git_push``, ``git_destructive``, ``web_search``, ``web_fetch``
    — are not legacy and are not inert. They travel through
    :meth:`to_payload` into ``Settings.permissions``, where
    :meth:`PermissionsConfig.category_rules
    <igni_core.core.config.schemas.permissions.PermissionsConfig.category_rules>`
    turns every level that differs from its schema default into
    ``deny``/``ask``/``allow`` rule strings, and
    ``ToolHookFactory.permission_evaluator``
    (:mod:`igni_core.core.session.tool_hook_factory`) appends those
    behind the explicit lists when it builds the live
    :class:`PermissionEvaluator`. ``backend/schemas_config.py`` reads
    ``file_write`` / ``shell_execute`` a second time, for the
    ``/config`` summary.

    So they carry the half of each flag that ``mode`` alone cannot
    express. ``--no-web`` is the starkest case: it sets no mode at all,
    so those two web fields are the entire flag. ``--read-only`` is the
    other way round: its ``PermissionMode.PLAN`` is what the person sees
    and what ``exit_plan_mode`` talks about, and ``read_only`` is the
    guarantee — held by the evaluator, so it survives a mode switch.
    """

    mode: PermissionMode | None = None
    file_write: PermissionLevel | None = None
    shell_execute: PermissionLevel | None = None
    git_push: PermissionLevel | None = None
    git_destructive: PermissionLevel | None = None
    web_search: PermissionLevel | None = None
    web_fetch: PermissionLevel | None = None
    #: Only ``True`` is an override: ``--read-only`` cannot switch it off.
    read_only: bool = False


class CliOverrides(_Overrides):
    """Typed bundle of CLI-driven ``Settings`` overrides.

    Constructed via :meth:`from_options` — that classmethod owns the
    permissive-to-strict precedence order of the permission flags.
    """

    models: ModelsOverrides = Field(default_factory=ModelsOverrides)
    display: DisplayOverrides = Field(default_factory=DisplayOverrides)
    permissions: PermissionOverrides = Field(default_factory=PermissionOverrides)
    hooks: HooksOverrides = Field(default_factory=HooksOverrides)

    @classmethod
    def from_options(cls, opts: CliOptions) -> CliOverrides:
        """Build overrides from a :class:`CliOptions` bundle.

        Order is "permissive → strict" so the strictest passed flag
        wins via standard field overwrite: ``--auto-approve --strict``
        ends up in ``dontAsk``. Matches the "safety beats convenience"
        principle used elsewhere in the permission stack.
        """
        overrides = cls()
        if opts.model:
            overrides.models.default = opts.model
        display = overrides.display
        if opts.verbose:
            display.show_routing = True
            display.show_reasoning = True
        if opts.quiet:
            display.show_tool_calls = False
            display.show_routing = False

        perms = overrides.permissions
        if opts.auto_approve:
            perms.mode = PermissionMode.BYPASS_PERMISSIONS
            perms.file_write = "allow"
            perms.shell_execute = "allow"
            perms.git_push = "allow"
            perms.git_destructive = "allow"
        if opts.accept_edits:
            perms.mode = PermissionMode.ACCEPT_EDITS
            perms.file_write = "allow"
        if opts.read_only:
            perms.mode = PermissionMode.PLAN
            perms.file_write = "deny"
            # Plan-mode reads, with no questions. The shell stays open for the
            # commands on the read-only allow-list (``cat``, ``ls``, ``rg``,
            # ``git log``, …) — igni reads files through the shell, so denying
            # it outright, as this flag used to, left a read-only agent unable
            # to read. Reading the web is reading too (search, GET fetches).
            # Every other shell command is refused, not asked, and so are an
            # extension's tools that change something: ``read_only`` is checked
            # by the evaluator before the mode, and survives a mode switch. See
            # tests/test_read_only_reads_and_refuses_the_rest.py.
            perms.read_only = True
        if opts.strict:
            perms.mode = PermissionMode.DONT_ASK
            perms.file_write = "deny"
            perms.shell_execute = "deny"
            perms.git_push = "deny"
            perms.git_destructive = "deny"
            # The agent's denies do not reach the hooks a group ships, which
            # run ``bash -c`` outside the permission system; ``--strict``
            # asks before one runs (and, headless, refuses it).
            overrides.hooks.group = "ask"
        if opts.no_web:
            perms.web_search = "deny"
            perms.web_fetch = "deny"

        return overrides

    def is_empty(self) -> bool:
        """True when no flag set anything. The loader takes ``None`` for
        "no overrides", which lets loader-side tests tell "user passed no
        flags" from "user passed flags that resolved to defaults"."""
        return not self.to_payload()
