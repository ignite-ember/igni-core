"""The ``/plugin`` and ``/plugins`` slash commands.

:class:`BuiltinCommandRegistry` targets the command classes in
``commands.py`` directly, so this package exports nothing of its own.

* ``context.py`` — :class:`SlashCommandContext` value bag.
* ``gateway.py`` — :class:`PluginBackendGateway` (the single seam
  for installer/marketplace calls).
* ``verbs.py`` — the :class:`PluginVerb` hierarchy.
* ``routers.py`` — three router classes (install / marketplace /
  toggle).
* ``bulk_refresh.py`` — :class:`BulkRefreshRunner` for
  ``/plugin marketplace refresh`` (all).
* ``commands.py`` — the two top-level :class:`SlashCommand` classes.
"""
