"""Importing this package registers all built-in connectors.

The alpha wheel leaves out the connectors in HELD_BACK (see docs/SHIPPING.md):
their modules stay in the source tree and register there, and an installed
wheel simply has no such module to import.
"""

import importlib

from varyz.connectors import (  # noqa: F401
    ai_conversations,
    documents,
    git_repos,
    github,
    manual,
)
from varyz.connectors.base import all_connector_ids, get_connector

HELD_BACK = (
    "calendar_ics",
    "gmail_imap",
    "google_workspace",
    "personal_archive",
)


def is_shipped(module: str) -> bool:
    """True when `varyz.connectors.<module>` is importable in this install."""
    try:
        importlib.import_module(f"{__name__}.{module}")
    except ModuleNotFoundError as exc:
        # Only the module itself being absent means "held back"; a missing
        # dependency inside a present module is a real error and propagates.
        if exc.name != f"{__name__}.{module}":
            raise
        return False
    return True


for _module in HELD_BACK:
    is_shipped(_module)

__all__ = ["HELD_BACK", "all_connector_ids", "get_connector", "is_shipped"]
