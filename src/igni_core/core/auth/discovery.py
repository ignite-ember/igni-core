"""Find the igni server from the address somebody types.

Asking a person for a server URL asks them for something they usually
do not know. They know their email address, and the domain in it is
already the name of the organisation whose igni they should reach —
so the domain answers the question, the way it answers it for mail.

**The record lives in the customer's own zone.** ``acme.example``
publishes where its igni is; this reads it. Nothing vendor-run is
contacted, which is the whole point: a lookup service in the middle
would learn every user's address before they had chosen anything, on
a product whose claim is that nothing leaves the customer's
infrastructure. It would also put a third party in the path of every
login, including the air-gapped ones.

A customer publishes one of these, once::

    https://acme.example/.well-known/igni.json
    {"api_url": "https://igni.acme.example"}

    _igni.acme.example.  TXT  "api_url=https://igni.acme.example"

Both, because organisations rarely have both abilities to hand: one
needs a file at the email domain's web root, the other needs DNS
access, and most customers find one of the two easy and the other a
ticket. The file is tried first — it carries a content type, it is
easier to inspect, and a wrong answer there is easier to see.

Whoever controls the domain decides where its people sign in. That is
the same trust the address itself carries, so it is the right party —
but it also means the fetch has to be exactly as careful as any other
request to a host we were told about rather than chose:

* **https only.** Plaintext would let anybody between here and there
  name the server that receives sign-ins for the whole company.
* **Redirects are not followed.** A redirect is a different host
  answering a question asked of this one.
* **Bounded.** A short timeout and a size cap, because a discovery
  that hangs is a sign-in screen that hangs.
* **The answer is checked, not trusted.** An ``api_url`` that is not
  an https URL is refused rather than written into a config file
  (:meth:`~igni_core.core.auth.discovery_schemas.PublishedRecord.answer`).

Discovery failing is normal and is not an error to shout about: most
domains have no record, and the screen offers the address field
instead. It only *fails* in the sense that the next thing to try is
asking.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import dns.asyncresolver
import dns.exception
import httpx

from igni_core.core.activity.http import busy_call
from igni_core.core.auth.discovery_schemas import Discovered, PublishedRecord

logger = logging.getLogger(__name__)

#: Where a customer publishes it, relative to their email domain.
WELL_KNOWN = "/.well-known/igni.json"

#: Long enough for a real server, short enough that a domain which
#: simply does not answer does not hold the sign-in screen.
TIMEOUT_SECONDS = 4.0

#: A generous ceiling for a document with two keys in it. Stops a
#: discovery fetch being a way to stream a gigabyte into the client.
MAX_BYTES = 64 * 1024

#: The TXT record's name, prefixed to the email domain. Underscored
#: so it cannot collide with a host somebody wants to run.
DNS_LABEL = "_igni"

#: ``resolve(name, rdtype, lifetime=...)`` — dnspython's signature,
#: injectable so a test answers instead of somebody's nameserver.
Resolve = Callable[..., Awaitable[Iterable[Any]]]


class ServerDiscovery:
    """Reads where an address's organisation runs igni from its domain.

    ``transport`` and ``resolve`` are the two ways out to the network,
    injected so a caller (or a test) decides what answers; the
    defaults are the real HTTP stack and the system resolver.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: Resolve = dns.asyncresolver.resolve,
    ) -> None:
        self._transport = transport
        self._resolve = resolve

    @staticmethod
    def domain_of(email: str) -> str:
        """The domain part, lowercased, or empty if this is not an address.

        Deliberately strict about shape. The value is interpolated into a
        URL, so ``sam@acme.example/../../evil`` must not become a request
        to somewhere else — a domain with a slash, a space or no dot in it
        is not a domain and is refused here rather than sanitised into
        something plausible.
        """
        address = (email or "").strip().lower()
        local, sep, domain = address.rpartition("@")
        # ``local`` matters: "@acme.example" has a domain and is not an
        # address, and treating it as one would send a lookup on behalf of
        # a person who typed nothing.
        if not sep or not domain or not local:
            return ""
        if any(c in domain for c in "/\\?#@: \t") or ".." in domain:
            return ""
        if "." not in domain or domain.startswith(".") or domain.endswith("."):
            return ""
        return domain

    async def discover(self, email: str) -> Discovered:
        """Where this address's organisation runs igni.

        Never raises for anything the network or the record can do.
        Every outcome is a :class:`Discovered` — found, or carrying a
        reason the screen can show if somebody asks why the address
        field appeared.
        """
        domain = self.domain_of(email)
        if not domain:
            return Discovered.nothing("That does not look like an email address.")

        url = f"https://{domain}{WELL_KNOWN}"
        try:
            async with (
                busy_call("igni-server"),
                httpx.AsyncClient(
                    timeout=TIMEOUT_SECONDS,
                    # A redirect is a different host answering a question
                    # asked of this one.
                    follow_redirects=False,
                    transport=self._transport,
                ) as client,
            ):
                response = await client.get(url, headers={"Accept": "application/json"})
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # No web root, or none that answers. The zone may still say.
            logger.debug("discovery: %s did not answer: %r", url, exc)
            return await self._from_dns(domain)

        if response.status_code != 200:
            return await self._from_dns(domain)
        if len(response.content) > MAX_BYTES:
            return Discovered.nothing(f"The record at {domain} is too large to be one.")
        record = PublishedRecord.from_json(response.content)
        if isinstance(record, Discovered):
            return record
        # A file that is there but unusable is worth saying so about
        # rather than silently trying the other way: somebody published
        # it, and "no record" would send them looking in the wrong place.
        return record.answer("well-known")

    async def _from_dns(self, domain: str) -> Discovered:
        """The TXT record in the customer's zone, if there is one."""
        name = f"{DNS_LABEL}.{domain}"
        nothing = Discovered.nothing(f"{domain} has no igni record published.")
        try:
            answer = await self._resolve(name, "TXT", lifetime=TIMEOUT_SECONDS)
        except dns.exception.DNSException as exc:  # NXDOMAIN, timeout, no nameservers
            logger.debug("discovery: %s has no TXT record: %r", name, exc)
            return nothing

        for txt in answer:
            # A TXT record is a list of character-strings and a long value
            # is split across them; joined rather than taking the first,
            # because an URL longer than 255 bytes arrives in pieces.
            text = b"".join(getattr(txt, "strings", [])).decode("utf-8", "replace")
            found = PublishedRecord.from_txt(text).answer("dns")
            if found.found:
                return found
        return nothing
