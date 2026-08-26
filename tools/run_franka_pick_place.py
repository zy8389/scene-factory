from __future__ import annotations

import runpy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MUG_LIFT_RUNNER = runpy.run_path(
    str(PROJECT_ROOT / "tools" / "run_franka_mug_lift.py")
)
main = _MUG_LIFT_RUNNER["main"]


if __name__ == "__main__":
    raise SystemExit(
        main([*sys.argv[1:], "--recipe", "kitchen_franka_mug_pick_place"])
    )
