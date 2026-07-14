"""Compatibility launcher for commands formerly duplicated under 4090/."""

import os
import runpy
import sys
from pathlib import Path


def run_root(script_name: str) -> None:
    root = Path(__file__).resolve().parent.parent
    os.environ.setdefault("HOMETOWN_XR_PROFILE", "4090")
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / script_name), run_name="__main__")
