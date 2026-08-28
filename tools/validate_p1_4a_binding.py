from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.backends.isaac_binding import (
    P1_4A_REPORT_VERSION,
    POSITION_TOLERANCE,
    SEKTION_TOP_DRAWER_BINDING,
    SEKTION_TOP_DRAWER_RUNTIME_ROOT,
    IsaacArticulationBinding,
    IsaacArticulationBindingResolution,
    IsaacBindingError,
    IsaacUsdBindingInspector,
    resolve_isaac_articulation_binding,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only P1-4A validation of the official Isaac Sektion drawer binding."
    )
    parser.add_argument(
        "--report", type=Path, required=True, help="JSON report path outside the repository"
    )
    parser.add_argument(
        "--settle-steps",
        type=int,
        default=120,
        help="Physics steps used only to settle the authored reset state",
    )
    return parser


def _write_report(path: Path, report: dict[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _flat_numbers(raw: Any) -> list[float]:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if isinstance(raw, (list, tuple)):
        result: list[float] = []
        for value in raw:
            result.extend(_flat_numbers(value))
        return result
    return [float(raw)]


def _runtime_vector(articulation: Any, method_name: str) -> list[float]:
    method = getattr(articulation, method_name, None)
    if not callable(method):
        raise RuntimeError(f"runtime articulation has no {method_name} API")
    return _flat_numbers(method())


def _joint_index(articulation: Any, binding: IsaacArticulationBinding) -> int:
    names = [str(name) for name in getattr(articulation, "dof_names", ())]
    if binding.joint_name not in names:
        raise IsaacBindingError(
            "joint_missing",
            "joint_name",
            binding.joint_name,
            names,
            "runtime articulation does not expose the frozen joint",
        )
    return names.index(binding.joint_name)


def _joint_value(values: list[float], index: int, field_name: str) -> float:
    if index >= len(values):
        raise RuntimeError(f"runtime {field_name} array is shorter than the DOF name array")
    return values[index]


def _resolution_or_report(
    binding: IsaacArticulationBinding,
    resolution: IsaacArticulationBindingResolution,
    report: dict[str, Any],
    key: str,
) -> IsaacArticulationBindingResolution:
    report.setdefault("binding_resolutions", {})[key] = resolution.to_dict()
    if not resolution.valid:
        codes = [issue.code for issue in resolution.errors]
        raise IsaacBindingError(
            "binding_invalid",
            f"binding_resolutions.{key}",
            "valid binding",
            codes,
            "; ".join(codes),
        )
    return resolution


def _check_closed(binding: IsaacArticulationBinding, position: float, field: str) -> None:
    lower, upper = binding.closed_range
    if not lower - POSITION_TOLERANCE <= position <= upper + POSITION_TOLERANCE:
        raise IsaacBindingError(
            "joint_reset_not_closed",
            field,
            f"{lower} .. {upper} m",
            position,
            "authored/reset joint position is outside the frozen closed semantic range",
        )


def _runtime_version() -> str | None:
    try:
        return importlib.metadata.version("isaacsim")
    except importlib.metadata.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.settle_steps < 1:
        raise ValueError("--settle-steps must be positive")

    report: dict[str, Any] = {
        "report_version": P1_4A_REPORT_VERSION,
        "result": "failed",
        "p1_4_physical_acceptance": "not_run",
        "physical_manipulation": {
            "franka_loaded": False,
            "gripper_closed": False,
            "drawer_commanded": False,
            "grasp": "not_run",
            "pull": "not_run",
            "release": "not_run",
        },
        "settle": {
            "requested_steps": args.settle_steps,
            "completed_steps": 0,
            "joint_position_before": None,
            "joint_position_after": None,
            "joint_velocity_before": None,
            "joint_velocity_after": None,
            "position_drift_m": None,
        },
        "checks": {},
        "errors": [],
    }
    app: Any | None = None
    exit_code = 1

    try:
        asset_root_value = os.environ.get("ISAACSIM_ASSET_ROOT")
        asset_source = SEKTION_TOP_DRAWER_BINDING.resolve_asset_path(asset_root_value)
        asset_root = Path(str(asset_root_value)).expanduser().resolve()
        report["asset"] = {
            "root_source": "ISAACSIM_ASSET_ROOT",
            "root": str(asset_root),
            "relative_path": SEKTION_TOP_DRAWER_BINDING.asset_relative_path,
            "resolved_path": str(asset_source),
            "is_below_root": True,
            "is_usd": asset_source.suffix.lower() in {".usd", ".usda", ".usdc"},
        }

        # SimulationApp forwards process arguments to Kit. Keep validator flags
        # out of Kit and construct SimulationApp before any Isaac/USD imports.
        sys.argv = [sys.argv[0]]
        os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
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
                "width": 320,
                "height": 240,
                "disable_viewport_updates": True,
                "fast_shutdown": True,
                "extra_args": [
                    "--/app/renderer/skipWhileMinimized=true",
                    "--/rtx-transient/resourcemanager/texturestreaming/enabled=false",
                    "--/isaac/startup/create_new_stage=false",
                ],
            }
        )

        import carb
        import omni.usd
        from isaacsim.core.api import SimulationContext
        from isaacsim.core.prims import SingleArticulation
        from pxr import Gf, UsdGeom, UsdPhysics

        version = _runtime_version()
        kit_build = carb.settings.get_settings().get_as_string("/app/buildVersion")
        report["runtime"] = {
            "isaac_sim_version": version,
            "kit_build": kit_build,
            "python": sys.executable,
            "runtime_version_supported": bool(version and version.startswith("6.0.1")),
        }
        if not report["runtime"]["runtime_version_supported"]:
            raise IsaacBindingError(
                "isaac_version_mismatch",
                "isaac_sim_version",
                "6.0.1.x",
                version or kit_build,
                "P1-4A is frozen against Isaac Sim 6.0.1",
            )

        context = omni.usd.get_context()
        context.new_stage()
        for _ in range(5):
            app.update()
        stage = context.get_stage()
        if stage is None:
            raise RuntimeError("Isaac Sim returned no stage after creating a clean stage")
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        physics_scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
        physics_scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(0.0, 0.0, -1.0))
        physics_scene.CreateGravityMagnitudeAttr().Set(9.81)

        cabinet_prim = stage.DefinePrim(SEKTION_TOP_DRAWER_RUNTIME_ROOT, "Xform")
        cabinet_prim.GetReferences().AddReference(asset_source.as_posix())
        stage.Load()
        for _ in range(10):
            app.update()
        if not cabinet_prim.IsValid():
            raise RuntimeError("failed to define /World/Cabinet reference prim")

        articulation = SingleArticulation(SEKTION_TOP_DRAWER_RUNTIME_ROOT)
        simulation = SimulationContext(
            physics_dt=1.0 / 60.0,
            rendering_dt=0.0,
            stage_units_in_meters=1.0,
            physics_prim_path="/World/PhysicsScene",
            stage=stage,
        )
        physics_context = simulation.get_physics_context()
        physics_context.set_physx_update_transformations_settings(
            update_to_usd=True,
            update_velocities_to_usd=True,
        )
        simulation.initialize_physics()
        articulation.initialize()

        joint_index = _joint_index(articulation, SEKTION_TOP_DRAWER_BINDING)
        positions_before = _runtime_vector(articulation, "get_joint_positions")
        velocities_before = _runtime_vector(articulation, "get_joint_velocities")
        position_before = _joint_value(positions_before, joint_index, "position")
        velocity_before = _joint_value(velocities_before, joint_index, "velocity")
        report["settle"].update(
            {
                "joint_position_before": position_before,
                "joint_velocity_before": velocity_before,
            }
        )
        _check_closed(SEKTION_TOP_DRAWER_BINDING, position_before, "settle.joint_position_before")

        inspector = IsaacUsdBindingInspector(
            stage=stage,
            runtime_asset_root_prim=SEKTION_TOP_DRAWER_RUNTIME_ROOT,
            runtime_articulation=articulation,
        )
        before = _resolution_or_report(
            SEKTION_TOP_DRAWER_BINDING,
            resolve_isaac_articulation_binding(
                SEKTION_TOP_DRAWER_BINDING,
                asset_root=asset_root,
                runtime_asset_root_prim=SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                inspector=inspector,
            ),
            report,
            "before_settle",
        )

        simulation.play()
        for _ in range(args.settle_steps):
            simulation.step(render=False)
        completed_steps = int(simulation.current_time_step_index)
        positions_after = _runtime_vector(articulation, "get_joint_positions")
        velocities_after = _runtime_vector(articulation, "get_joint_velocities")
        position_after = _joint_value(positions_after, joint_index, "position")
        velocity_after = _joint_value(velocities_after, joint_index, "velocity")
        report["settle"].update(
            {
                "completed_steps": completed_steps,
                "joint_position_after": position_after,
                "joint_velocity_after": velocity_after,
                "position_drift_m": position_after - position_before,
            }
        )
        _check_closed(SEKTION_TOP_DRAWER_BINDING, position_after, "settle.joint_position_after")
        after = _resolution_or_report(
            SEKTION_TOP_DRAWER_BINDING,
            resolve_isaac_articulation_binding(
                SEKTION_TOP_DRAWER_BINDING,
                asset_root=asset_root,
                runtime_asset_root_prim=SEKTION_TOP_DRAWER_RUNTIME_ROOT,
                inspector=inspector,
            ),
            report,
            "after_settle",
        )
        simulation.stop()

        report["binding"] = after.to_dict()
        report["joint"] = {
            "prim": after.joint_prim,
            "name": after.joint_name,
            "type": after.runtime_joint_type,
            "axis": after.runtime_axis,
            "lower": after.runtime_lower_limit,
            "upper": after.runtime_upper_limit,
            "expected_default": SEKTION_TOP_DRAWER_BINDING.expected_default_position,
            "runtime_reset_before_settle": before.runtime_default_position,
            "current_before_settle": before.runtime_current_position,
            "current_after_settle": after.runtime_current_position,
        }
        report["joint_position_before"] = position_before
        report["joint_position_after"] = position_after
        report["joint_velocity_before"] = velocity_before
        report["joint_velocity_after"] = velocity_after
        report["checks"] = {
            "asset_exists": asset_source.is_file(),
            "asset_is_usd": asset_source.suffix.lower() in {".usd", ".usda", ".usdc"},
            "stage_is_meter_z_up": (
                float(UsdGeom.GetStageMetersPerUnit(stage)) == 1.0
                and UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
            ),
            "physics_scene_exists": stage.GetPrimAtPath("/World/PhysicsScene").IsValid(),
            "runtime_version_supported": report["runtime"]["runtime_version_supported"],
            "binding_before_settle_valid": before.valid,
            "binding_after_settle_valid": after.valid,
            "reset_position_closed": (
                SEKTION_TOP_DRAWER_BINDING.closed_range[0] - POSITION_TOLERANCE
                <= position_before
                <= SEKTION_TOP_DRAWER_BINDING.closed_range[1] + POSITION_TOLERANCE
            ),
            "settled_position_closed": (
                SEKTION_TOP_DRAWER_BINDING.closed_range[0] - POSITION_TOLERANCE
                <= position_after
                <= SEKTION_TOP_DRAWER_BINDING.closed_range[1] + POSITION_TOLERANCE
            ),
            "settled_position_finite": all(
                value == value and abs(value) < 1.0e6
                for value in (position_before, position_after)
            ),
            "settled_velocity_finite": all(
                value == value and abs(value) < 1.0e6
                for value in (velocity_before, velocity_after)
            ),
            "settle_steps_completed": completed_steps >= args.settle_steps,
            "drawer_commanded": False,
            "franka_loaded": False,
            "collision_available": after.collision_available,
        }
        report["result"] = "passed" if all(report["checks"].values()) else "failed"
        exit_code = 0 if report["result"] == "passed" else 1
    except IsaacBindingError as exc:
        report["errors"].append(exc.issue.to_dict())
    except (ImportError, ModuleNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
        report["errors"].append(
            {
                "code": "runtime_validation_failed",
                "field": "runtime",
                "expected": "successful P1-4A read-only validation",
                "observed": type(exc).__name__,
                "reason": str(exc),
            }
        )
        report["traceback"] = traceback.format_exc()
    finally:
        report["shutdown"] = "not_started" if app is None else "attempted"
        # Persist before Kit teardown. Some Isaac Sim builds terminate the
        # process from app.close(), which would otherwise erase the report.
        _write_report(args.report, report)
        print("SCENE_FACTORY_P1_4A_REPORT=" + json.dumps(report, ensure_ascii=False), flush=True)
        if app is not None:
            try:
                app.close(exit_code=exit_code)
                report["shutdown"] = "completed"
            except Exception as exc:  # pragma: no cover - depends on Kit teardown
                report["shutdown"] = "failed"
                report["errors"].append(
                    {
                        "code": "shutdown_failed",
                        "field": "simulation_app",
                        "expected": "clean Isaac shutdown",
                        "observed": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
                exit_code = 1
        if report["shutdown"] == "completed":
            _write_report(args.report, report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
