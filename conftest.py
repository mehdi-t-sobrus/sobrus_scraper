"""
conftest.py
===========
Root pytest configuration. Runs before any test file is imported.
Sets up sys.path so all test modules can import from src/ without
needing PYTHONPATH env var or per-file sys.path manipulation.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent

# Make all source packages importable
sys.path.insert(0, str(ROOT / "src" / "backend"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "transformations"))
