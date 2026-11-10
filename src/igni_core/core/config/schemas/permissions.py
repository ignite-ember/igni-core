"""``PermissionsConfig`` — the ``permissions`` block of ``Settings``.

Besides the fields it holds what each per-category level governs, and turns
the levels into evaluator rules (:meth:`PermissionsConfig.category_rules`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.permissions.schemas import PermissionLevel

if TYPE_CHECKING:
    from igni_core.core.config.permission_eval.mode_policy import ModePolicy

#: The shell tool every git invocation arrives as. igni has no git tool and no
#: read tool — a commit, a push and a ``cat`` are all ``run_shell_command`` — so
#: a git category can only be carried by the command string, not by a function
#: name the way ``file_write`` is.
_SHELL_FUNCTION = "run_shell_command"


def _shell_rules(*commands: str) -> tuple[str, ...]:
    """``"git push"`` → the rule strings that match it as a real command.

    Two patterns per command because the evaluator fnmatches the whole
    ``command`` string and fnmatch has no word boundaries: ``git push*``
    catches the command run on its own, ``* git push*`` catches it after a
    separator (``cd repo && git push``, ``sudo git push``). Neither is exact —
    ``echo git push`` matches the second — and that asymmetry is deliberate:
    over-matching sends a call to the ask/deny path, under-matching lets a
    force-push run unseen, and only one of those is recoverable.
    """
    rules: list[str] = []
    for command in commands:
        rules.append(f"{_SHELL_FUNCTION}({command}*)")
        rules.append(f"{_SHELL_FUNCTION}(* {command}*)")
    return tuple(rules)


class CategoryRules(BaseModel):
    """The per-category levels as rule strings, one list per level."""

    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)

    def add(self, level: PermissionLevel, rules: Iterable[str]) -> None:
        """Append ``rules`` to the list for ``level``."""
        match level:
            case "deny":
                self.deny.extend(rules)
            case "ask":
                self.ask.extend(rules)
            case "allow":
                self.allow.extend(rules)


class PermissionsConfig(BaseModel):
    """The ``permissions`` block: per-category levels, mode and rule lists."""

    model_config = ConfigDict(use_enum_values=True, validate_assignment=True)

    # Per-category levels, honoured through :meth:`category_rules`.
    # ``file_read`` and ``shell_restricted`` are not read: see the note on
    # ``CATEGORY_FUNCTIONS``.
    file_read: PermissionLevel = "allow"
    file_write: PermissionLevel = "ask"
    shell_execute: PermissionLevel = "ask"
    shell_restricted: PermissionLevel = "allow"
    # The web asks by default: a search or fetch the model decides on is
    # a request out of the customer's network, and none leaves without a
    # person's yes unless the config says ``allow``. ``air_gapped`` denies
    # both and builds neither toolkit.
    web_search: PermissionLevel = "ask"
    web_fetch: PermissionLevel = "ask"
    git_push: PermissionLevel = "ask"
    git_destructive: PermissionLevel = "ask"
    # Claude Code-style permission system (mirrors ``settings.json``'s
    # ``permissions`` block). ``deny`` / ``ask`` / ``allow`` are lists of
    # ``Tool`` or ``Tool(pattern)`` strings (e.g. ``"Bash(rm *)"``,
    # ``"Read(./.env)"``). ``mode`` is stored as its plain string value.
    mode: PermissionMode = Field(default=PermissionMode.DEFAULT, validate_default=True)
    #: ``false`` forbids ``bypassPermissions`` outright — by flag, by
    #: ``/bypass``, by a tool. Honoured only from the managed-settings file
    #: or the group policy: the loader reads those two tiers on their own
    #: (:class:`~igni_core.core.config.permission_eval.mode_policy.ModePolicy`)
    #: and never consults the merged value, so a user's or a project's
    #: ``allow_bypass`` changes nothing. Defaults to ``true``: with no
    #: organisation policy every mode, fully automatic included, is open.
    allow_bypass: bool = True
    #: ``--read-only``: plan-mode reads for the whole session, with no
    #: questions. A shell command built only from the read-only allow-list
    #: runs, as do the web tools (a search or a GET) and an extension's tools
    #: its manifest declares read-only; any other shell command, the edit
    #: tools and an extension's tools that change something are refused.
    #: Held by the evaluator itself, so switching mode does not reopen it
    #: (:meth:`PermissionEvaluator._read_only_step`). Only ever tightens, so
    #: any tier may set it.
    read_only: bool = False
    deny: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=list)

    #: ``safety.require_confirmation``, copied here by ``Settings`` so
    #: :meth:`category_rules` can emit it: the evaluator built from this
    #: block is the only reader of permission rules.
    #:
    #: ``exclude=True`` matters: ``Settings.default_dict()`` seeds the merge
    #: accumulator, so a field carrying the eight shipped defaults would bake
    #: them into the merge base and a user who set ``require_confirmation: []``
    #: could never take them back out.
    confirm_commands: list[str] = Field(default_factory=list, exclude=True)

    #: What the managed settings and group policy say about modes, recorded by
    #: ``SettingsLoader.load`` from those two tiers alone. Private so no config
    #: file can write it; ``None`` means no policy was read (tests, tooling).
    _mode_policy: ModePolicy | None = PrivateAttr(default=None)

    #: Permission fields the managed settings or the group policy set,
    #: recorded by ``SettingsLoader.load``. These are final: a lower tier —
    #: a user's file, a trusted project's — cannot loosen them, and a
    #: category level here is emitted even when it equals its default
    #: (see :meth:`category_rules`). Private so no config file can write it.
    _final_fields: frozenset[str] = PrivateAttr(default=frozenset())

    @property
    def final_fields(self) -> frozenset[str]:
        return self._final_fields

    def set_final_fields(self, fields: frozenset[str]) -> None:
        self._final_fields = frozenset(fields)

    @property
    def mode_policy(self) -> ModePolicy | None:
        return self._mode_policy

    def set_mode_policy(self, policy: ModePolicy | None) -> None:
        self._mode_policy = policy

    #: What each per-category level governs, as permission-rule strings.
    #: Function-name categories use the registry's own names, so a renamed
    #: function does not quietly drop out of its category.
    #:
    #: The two git rows are argument patterns rather than function names,
    #: because there is no git *tool* to name (see ``_shell_rules``).
    #: ``--auto-approve`` sets both to ``allow`` and ``--strict`` to ``deny``.
    #:
    #: ``file_read`` and ``shell_restricted`` are deliberately absent. igni has
    #: no read tool (a file is read through ``run_shell_command``), so a
    #: ``file_read`` rule would either do nothing or, if pointed at the shell,
    #: silently govern writes too.
    CATEGORY_FUNCTIONS: ClassVar[dict[str, tuple[str, ...]]] = {
        "file_write": (
            "save_file",
            "create_file",
            "edit_file",
            "edit_file_replace_all",
            "notebook_add_cell",
            "notebook_edit_cell",
            "notebook_remove_cell",
        ),
        "shell_execute": ("run_shell_command",),
        "web_search": ("web_search", "search_news"),
        "web_fetch": ("fetch_url", "fetch_json"),
        "git_push": _shell_rules("git push"),
        # Destructive means "loses work that is not recoverable from this
        # clone": a discarded working tree, a deleted branch, a dropped stash,
        # a rewritten remote history. Ordinary history rewriting that keeps the
        # objects (``git rebase``, ``git commit --amend``) is left out — it is
        # recoverable through the reflog, and a category that fires on every
        # rebase gets turned off.
        "git_destructive": _shell_rules(
            "git push --force",
            "git push -f",
            "git reset --hard",
            "git clean",
            "git checkout --",
            "git branch -D",
            "git stash drop",
            "git stash clear",
            "git filter-branch",
        ),
    }

    def category_rules(self) -> CategoryRules:
        """The per-category levels as ``deny`` / ``ask`` / ``allow`` rule strings.

        Emitted as ordinary rules, appended after the explicit lists so nothing
        explicit is displaced. The evaluator runs deny first, so a category set
        to ``deny`` is not reopened by a matching allow rule.

        Only a level that differs from its schema default is emitted, or one
        the organisation set (:attr:`final_fields`), which is emitted even at
        its default so a lower tier's ``allow: [Bash]`` meets its ask rule.
        Compared against the default rather than ``model_fields_set`` because
        the settings loader populates every field. Every default is ``ask`` or
        unread, which is what an unmatched tool does already, so emitting
        defaults would only add ``PERMISSION_REQUEST`` hook traffic.

        ``safety.require_confirmation`` rides along in the ask bucket — see
        :meth:`_confirmation_ask_rules`.
        """
        rules = CategoryRules()
        for category, functions in self.CATEGORY_FUNCTIONS.items():
            level: PermissionLevel = getattr(self, category)
            is_default = level == type(self).model_fields[category].default
            if is_default and category not in self._final_fields:
                continue
            rules.add(level, functions)
        rules.ask.extend(self._confirmation_ask_rules())
        return rules

    def _confirmation_ask_rules(self) -> list[str]:
        """``safety.require_confirmation`` as ask rules, or nothing in ``dontAsk``.

        These are exactly the ask rules the user would otherwise have
        hand-written: an ask rule loses to the mode step, so ``/bypass`` still
        bypasses them.

        Nothing is emitted in ``dontAsk``. An ask rule there is a pass (the
        evaluator fires ``PERMISSION_REQUEST`` and continues) while an
        unmatched tool is denied, so emitting the list would turn
        ``terraform apply`` from denied into allowed on every ``--strict`` and
        headless run. With nobody there to confirm, "requires confirmation"
        reads as "no".
        """
        if self.mode == PermissionMode.DONT_ASK:
            return []
        return list(_shell_rules(*self.confirm_commands))
