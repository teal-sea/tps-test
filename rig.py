#!/usr/bin/env python3
"""Deprecated entry point kept for earlier links and scripts.

Use the packaged CLI instead:

    pipx install .        # gives you `tps`
    tps run -c targets.toml

or, without installing:

    python -m tps run -c targets.toml
"""

from __future__ import annotations

import sys
import warnings

from tps.cli import main

warnings.warn(
    "rig.py is deprecated; use `tps` or `python -m tps`",
    DeprecationWarning,
    stacklevel=1,
)

if __name__ == "__main__":
    print("note: rig.py is deprecated — use `tps` or `python -m tps`", file=sys.stderr)
    raise SystemExit(main())
