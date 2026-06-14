"""``python -m docmcp`` entry point → the data CLI (see ``docmcp.cli``)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
