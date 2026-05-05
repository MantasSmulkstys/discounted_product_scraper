"""Adds the scraper root to sys.path so tests can import top-level modules
(`db`, `engine`, `transforms`, `normalization.*`) without a package install."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
