"""Offline self-tests for the helper scripts (no AWS account, no network).

Run from the repository root:  python -m pytest scripts/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

# The scripts import each other as top-level modules (``from _common import ...``), exactly as
# they do when started with ``python scripts/<name>.py``; give the tests the same import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
