"""Group policy pack — fetched from igni server, cached on disk, applied as a settings tier.

A *pack* is everything an organisation's group gives its people: agents,
skills, commands, rules, workflows, tools, scripts, hooks, MCP servers,
plugins and ``settings`` entries. igni server signs each person's pack as a
compact JWS; igni stores it exactly as received and applies it only once
the signature verifies (``group_policy_signature.py``).

The fetch and the read happen in different places, and the split is
explained on :class:`~.settings_fetcher.CachedSettingsFetcher`: the backend
fetches (:class:`~.refresher.PackRefresher`), every settings load reads and
verifies what is on disk.

Layout, one responsibility per module:

* :mod:`.entry`            — one entry of a pack, and its name rules.
* :mod:`.pack`             — the pack, and the signed ``policy`` claim it is built from.
* :mod:`.answers`          — what a pack request can answer, and the fetcher protocol.
* :mod:`.jwks`             — igni server's signing keys.
* :mod:`.layout`           — the one derivation of the cache directory and its files.
* :mod:`.meta`             — ``pack_meta.json``: identity and freshness for the frontend.
* :mod:`.signed_store`     — ``pack.jws``, ``jwks.json`` and ``pack_state.json``.
* :mod:`.trust`            — what a pack is checked against on this install.
* :mod:`.verifier`         — checks a freshly fetched pack before anything is written.
* :mod:`.hooks`            — the merged ``hooks/settings.json``.
* :mod:`.kinds`            — one handler per entry kind.
* :mod:`.plugins`          — plugins a pack installs from a git source.
* :mod:`.materializer`     — writes a pack's file-shaped entries and prunes stale ones.
* :mod:`.cache`            — the facade that composes the collaborators above.
* :mod:`.refresher`        — fetch, verify, store, materialise.
* :mod:`.transport`        — the one synchronous fetch a settings load may make.
* :mod:`.settings_fetcher` — the ``group_policy_fetcher`` handed to ``load_settings``.
* :mod:`.policy_settings`  — the two-pass load every process uses to apply it.
* :mod:`.gate`             — whether this process may run agent sessions.
"""
