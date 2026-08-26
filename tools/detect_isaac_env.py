from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def detect_environment() -> dict[str, Any]:
    version = _distribution_version("isaacsim")
    app_version = _distribution_version("isaacsim-app")
    core_version = _distribution_version("isaacsim-core")
    physics_version = _distribution_version("isaacsim-extscache-physics")
    motion_version = _distribution_version("isaacsim-robot-motion")
    result = {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "isaac_sim": _module_available("isaacsim"),
        "isaac_sim_version": version,
        "simulation_app": bool(app_version),
        "simulation_app_api": "isaacsim.SimulationApp" if app_version else None,
        "core_api_distribution": core_version,
        "robot_motion_distribution": motion_version,
        "pxr": _module_available("pxr"),
        "physx_distribution": physics_version,
        "runtime_import_probe": "deferred_until_SimulationApp",
    }
    result["available"] = all(
        result[key]
        for key in (
            "isaac_sim",
            "simulation_app",
            "core_api_distribution",
            "robot_motion_distribution",
            "pxr",
            "physx_distribution",
        )
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect Isaac Sim and PhysX Python capabilities.")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require", action="store_true", help="Return non-zero when unavailable")
    args = parser.parse_args(argv)
    report = detect_environment()
    if args.report:
        path = args.report.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["available"] or not args.require else 2


if __name__ == "__main__":
    raise SystemExit(main())
