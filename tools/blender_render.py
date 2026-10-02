"""Run this script using Blender's Python: blender -b --python tools/blender_render.py -- ..."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_factory.exporters.blender_render import main  # noqa: E402

if __name__ == "__main__":
    main()
