"""Identity declared to upstream data sources (User-Agent).

⚠️ **Never hardcode the author's contact details.**
SEC, the House and the Senate all want a User-Agent that identifies the caller.
Baking one person's address into an open-source repository has two consequences:

1. that address ships publicly with the code, and
2. **every self-hosting user's traffic goes out under that person's name** —
   so whoever gets SEC throttled or blocked, it lands on him.

Hence: **the deployer configures it, and an unset value is a hard error** — rather
than a placeholder quietly standing in, which would leave users rate-limited upstream
with no idea why and nothing to grep for.

How to set it:
    export FZ_CONTACT="Your Name your@email.com"

(global-stock-data v2.0.1 already paid for the lesson where a fail-fast message was
 swallowed by the module's own `except Exception`. So this raises a dedicated
 exception type, and callers must let it propagate rather than folding it into
 DataNotAvailable.)
"""
from __future__ import annotations

import os


class ContactNotConfigured(RuntimeError):
    """Contact details unset — **a configuration error that must propagate**, not "no data"."""


_HINT = (
    "FZ_CONTACT is not set. SEC and the congressional disclosure sites require a "
    "User-Agent that identifies the caller; without one you will be throttled or blocked.\n"
    "  Set it and restart:\n"
    '    export FZ_CONTACT="Your Name your@email.com"\n'
    "  (This identifies you to those sites. It is sent nowhere else.)"
)


def user_agent(product: str = "FloorZero/0.1") -> str:
    """Build a compliant UA. **Raises** when contact details are unset."""
    contact = (os.environ.get("FZ_CONTACT") or "").strip()
    if not contact:
        raise ContactNotConfigured(_HINT)
    if "@" not in contact:
        raise ContactNotConfigured(
            f"FZ_CONTACT must contain a reachable email address; got {contact!r}\n{_HINT}")
    return f"{product} ({contact})"
