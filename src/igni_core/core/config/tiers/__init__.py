"""The precedence tiers :class:`~igni_core.core.config.merge_plan.SettingsMergePlan` runs.

One module per kind of tier: the files a person writes (``files``), a
repository's files (``project``), the command line (``cli``), the blocks only
the organisation may set (``organisation``), the managed-settings file
(``managed``), the group policy (``group_policy``), the settings only the
developer may set (``developer``) and the cloud-model migration
(``cloud_migration``). Each folds itself into a
:class:`~igni_core.core.config.accumulator.SettingsAccumulator`.
"""
