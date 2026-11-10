"""Settings for a CLI run, loaded from its flags."""

from __future__ import annotations

import click

from igni_core.cli.options import CliOptions
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.group_policy.settings_fetcher import CachedSettingsFetcher
from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader


class CliSettingsLoader:
    """Loads the :class:`Settings` one ``igni`` invocation runs with.

    Loaded twice, and the second is the one returned. ``-m`` and the pipe
    mode run a real session in this process, so an admin's group policy has
    to apply here too — otherwise ``{"permissions": {"web_fetch": "deny"}}``
    would hold in the app and not on the command line, which is the kind of
    half-enforcement a policy cannot have. The cache it is read from lives
    under ``storage.data_dir``, which is itself a setting, so the first pass
    answers "which directory" from local configuration alone. Both passes
    read a handful of small files; neither touches the network.

    The fetcher reads what the backend's ``GroupPolicySync`` last cached — a
    CLI invocation has no token of its own at settings-load time and must
    not stall on a portal call. See ``CachedSettingsFetcher``. The pack is
    verified before it is merged; with ``group_policy.required`` set and no
    verified pack, the fetcher raises ``GroupPolicyUnavailable`` and this run
    starts no session (``main`` reports it).
    """

    def __init__(self, options: CliOptions) -> None:
        self._options = options

    def load(self) -> Settings:
        local_only = self._load(self._options, None)
        settings = self._load(self._options, CachedSettingsFetcher.for_settings(local_only))
        policy = settings.permissions.mode_policy
        if self._options.auto_approve and policy is not None and not policy.allow_bypass:
            # ``--auto-approve`` is bypass mode plus four categories set to
            # ``allow``. The loader already refuses the mode; the categories
            # would still run every shell command and push unasked, which is
            # bypass under another name. Ignored with a warning — the way the
            # group tier treats a loosening it refuses — and loaded again
            # without the flag, so any other flag passed still applies.
            click.echo(
                "Warning: --auto-approve is turned off by your organisation's "
                f"{policy.bypass_source or 'policy'} "
                "(permissions.allow_bypass: false); running without it.",
                err=True,
            )
            settings = self._load(
                self._options.model_copy(update={"auto_approve": False}),
                CachedSettingsFetcher.for_settings(local_only),
            )
        return settings

    @staticmethod
    def _load(options: CliOptions, fetcher: CachedSettingsFetcher | None) -> Settings:
        overrides = CliOverrides.from_options(options)
        return SettingsLoader.load(
            # ``None`` tells the loader no flag was passed at all.
            cli_overrides=None if overrides.is_empty() else overrides,
            group_policy_fetcher=fetcher,
        )
