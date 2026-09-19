"""Test-only environment setup before production modules are imported."""

import os
from pathlib import Path

TEST_DATA_DIR = Path(__file__).parent / ".runtime"
TEST_DATA_DIR.mkdir(exist_ok=True)
os.environ.setdefault("LOCAL_DB_PATH", str(TEST_DATA_DIR / "watchlist.db"))
