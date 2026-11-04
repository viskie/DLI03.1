"""Vercel entrypoint for the deployable application stored in dist/."""
from pathlib import Path
import sys

DIST = Path(__file__).resolve().parents[1] / "dist"
sys.path.insert(0, str(DIST))

from app import app  # noqa: E402,F401
