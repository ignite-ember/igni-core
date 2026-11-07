"""igni's internal RPC wire, as the host's members speak it.

The arguments members hand igni's RPC handlers, and the readings of what
those handlers answer, one module per area. Field names are the wire
keys the handlers read and write; nothing here is published to an
extension. What an extension is answered with lives beside the members
(:mod:`igni_core.extensions.host.members.answers`) and in
:mod:`igni_core.extensions.host.values`.
"""
