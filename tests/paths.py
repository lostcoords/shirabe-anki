"""Paths for standalone tests and explicit Rails integration checks."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIRABE = Path(os.environ.get("SHIRABE_PATH", ROOT.parent / "shirabe")).resolve()
SCRATCH = (SHIRABE if "SHIRABE_PATH" in os.environ else ROOT) / "tmp"
SCRATCH.mkdir(parents=True, exist_ok=True)
