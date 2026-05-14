"""Shared pytest fixtures. No DB required for unit tests."""
import sys
from pathlib import Path

# Make `src/` importable for unit tests without requiring `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
