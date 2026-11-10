"""igni's prompts: the files it ships, and the anchors extensions fill into them.

The table of anchors is :class:`~igni_core.core.prompts.fragments.PromptFragments`;
the files, read through it, are
:class:`~igni_core.core.prompts.loader.PromptLoader`. Both are injected:
whoever wires the backend creates them and hands the same instances to
every session.
"""

from igni_core.core.prompts.fragments import (
    ANCHOR,
    AnchorRegistration,
    FragmentSupplier,
    PromptFragments,
)
from igni_core.core.prompts.loader import PROMPTS_DIR, PromptLoader

__all__ = [
    "ANCHOR",
    "PROMPTS_DIR",
    "AnchorRegistration",
    "FragmentSupplier",
    "PromptFragments",
    "PromptLoader",
]
