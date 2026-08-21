from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate one exported SceneFactory USD in Isaac Sim.")
    parser.add_argument("usd", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _write(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    usd = args.usd.expanduser().resolve()
    report = {"usd": str(usd), "stage_load": "not_run", "physics_initialization": "not_run", "result": "failed"}
    if not usd.is_file():
        report["error"] = f"missing USD: {usd}"
        _write(args.report, report)
        return 2
    sys.argv = [sys.argv[0]]
    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    app = None
    try:
        from isaacsim import SimulationApp

        app = SimulationApp(
            {
                "headless": True,
                "hide_ui": True,
                "renderer": "Minimal",
                "minimal_shading_mode": 4,
                "anti_aliasing": 0,
                "multi_gpu": False,
                "max_gpu_count": 1,
                "fast_shutdown": True,
                "open_usd": usd.as_posix(),
                "width": 320,
                "height": 240,
                "disable_viewport_updates": True,
            }
        )
        import omni.usd
        from isaacsim.core.api import SimulationContext

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            raise RuntimeError("Isaac Sim returned no stage")
        report["stage_load"] = "passed"
        physics_scene = stage.GetPrimAtPath("/World/PhysicsScene")
        if not physics_scene.IsValid():
            raise RuntimeError("scene has no /World/PhysicsScene")
        simulation = SimulationContext(
            physics_dt=1.0 / 60.0,
            rendering_dt=1.0 / 60.0,
            stage_units_in_meters=1.0,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        simulation.initialize_physics()
        simulation.step(render=False)
        report["physics_initialization"] = "passed"
        report["physics_scene"] = str(physics_scene.GetPath())
        report["result"] = "passed"
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _write(args.report, report)
        if app is not None:
            app.close(exit_code=0 if report["result"] == "passed" else 1)
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
