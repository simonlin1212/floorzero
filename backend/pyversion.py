"""Minimum Python version gate.

⚠️ **This must run before every other import, and must itself use no modern syntax.**
Its whole reason for existing is to say one plain sentence on an old interpreter,
rather than let the user crash into a SyntaxError deep inside some module and guess.

The real floor comes from the features actually used (full scan, 2026-07-26):
- `zoneinfo` (3.9+): US/Eastern date arithmetic, a hard dependency
- `str.removesuffix`（3.9+）
- `X | Y` annotations: every file carries `from __future__ import annotations`,
  so they do **not** imply 3.10. Nothing 3.10-only is used anywhere.
"""
import sys

MIN = (3, 9)

if sys.version_info < MIN:
    raise SystemExit(
        "FloorZero needs Python %d.%d or newer; this is %d.%d.\n"
        "Why: US/Eastern date handling depends on the stdlib zoneinfo module (added in 3.9).\n"
        "Install a newer Python, or make a 3.9+ environment with pyenv or conda."
        % (MIN[0], MIN[1], sys.version_info[0], sys.version_info[1]))
