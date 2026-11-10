"""Extensions arrive from their services, through igni server.

igni server's ``docs/service-identity.md`` §8. A service uploads its extension to
igni, signed with the key it registered; once a super admin approves
the service, igni lists the extension to every signed-in machine.
This is the machine's half: keep what is installed in step with that
list.

For every listed extension whose version or digest differs from what
is installed:

1. download ``GET /v1/extensions/{id}/bundle``;
2. check the archive's SHA-256 against the listing and the
   ``X-Igni-Bundle-Sha256`` header;
3. verify ``X-Igni-Bundle-Signature`` — a compact JWS — against the
   ``publisher_jwks`` the listing carries, and that its claim
   ``{extension, version, sha256}`` is this archive;
4. unpack it (no path outside itself, no links, no devices, bounded
   size) and check the manifest names the same id and version;
5. download each asset its manifest declares for this machine's
   platform that is not on this machine already —
   ``GET /v1/extensions/{id}/assets/{sha256}`` — and keep it only if it
   is the bytes the signed manifest pins (§8.2);
6. store it with a :class:`~igni_core.extensions.provenance.DistributionRecord`
   and hand it to the ordinary boot path — trust, profile, capability
   resolution — through
   :class:`~igni_core.extensions.distribution.sync.ExtensionLifecycle`.

A person belongs to exactly one group. Only an extension switched on
for the signed-in person's group is installed (igni server's §8.3): the
listing names nothing else, and the machine checks that the person's
``group`` is in each entry's ``groups``. An extension igni distributed
that is no longer listed — its service gone, or switched off for the
person's group — is stopped and removed. A **development install** of the same id is
left alone in both directions: a developer running their own build of
an extension is not overwritten by the released one, and its absence
from the listing is not a reason to delete it.

**Never in the way.** A sync runs in the background, at start-up and
on sign-in. Not signed in, no server configured, igni unreachable, a
bundle that does not verify — each is logged, and whatever is
installed keeps running. A failure here costs an update, never the
extension already working.

## The parts

* :mod:`.constants` — the wire's header names and the size limits.
* :mod:`.errors` — :class:`~igni_core.extensions.distribution.errors.DistributionError`,
  the one failure this reports.
* :mod:`.schemas` — the wire models: the listing, the downloaded
  bundle, the publisher's keys, and what one sync did.
* :mod:`.client` —
  :class:`~igni_core.extensions.distribution.client.DistributionClient`,
  the three calls, with the person's igni session.
* :mod:`.bundle` — the archive's digest, its safe unpacking, and
  every check between a download and the store.
* :mod:`.sync` —
  :class:`~igni_core.extensions.distribution.sync.ExtensionDistribution`,
  which keeps this machine's extensions in step with igni's listing.
* :mod:`.once` —
  :class:`~igni_core.extensions.distribution.once.OneShotSync`, one
  bounded sync for a process that is not the app's backend.
"""
