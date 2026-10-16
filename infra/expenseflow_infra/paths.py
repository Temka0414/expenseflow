"""Absolute paths to the repository folders the CDK app packages into assets.

Paths are resolved from this file, never from the current working directory, so `cdk synth`,
pytest and IDE runs all see the same locations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

INFRA_DIR: Final = Path(__file__).resolve().parent.parent
REPO_ROOT: Final = INFRA_DIR.parent

BACKEND_DIR: Final = REPO_ROOT / "backend"
# Lambda code asset: the `expenseflow` library + `handlers` package, deployed as-is (no dependencies).
BACKEND_SRC: Final = BACKEND_DIR / "src"
# Third-party runtime dependencies, installed into the shared Lambda layer (see DependenciesLayer).
BACKEND_REQUIREMENTS: Final = BACKEND_DIR / "requirements.txt"

FRONTEND_DIR: Final = REPO_ROOT / "frontend"
