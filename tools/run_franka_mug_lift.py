from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.backends import IsaacSimBackend  # noqa: E402
from scene_factory.factory import SceneFactory  # noqa: E402
from scene_factory.robotics import (  # noqa: E402
    build_pick_place_acceptance_report,
    build_robot_acceptance_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the SceneFactory Franka real-mug lift acceptance in Isaac Sim."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipe", default="kitchen_franka_mug_lift")
    parser.add_argument("--seed", type=int, default=77)
    parser.add_argument("--max-steps", type=int, default=720)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--runtime-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--layout", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--usd", type=Path, help=argparse.SUPPRESS)
    return parser


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "robot_acceptance.json"
    if args.runtime_only:
        if args.layout is None or args.usd is None:
            raise ValueError("--runtime-only requires --layout and --usd")
        return _run_runtime(
            args.layout.expanduser().resolve(),
            args.usd.expanduser().resolve(),
            report_path,
            max_steps=args.max_steps,
            headless=not args.no_headless,
        )

    try:
        factory = SceneFactory()
        result = factory.build_from_recipe(args.recipe, args.seed)
        if not result.valid:
            raise RuntimeError(f"acceptance scene is invalid: {result.validation.to_dict()}")
        mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
        if mug.asset_id != "mug_001" or factory.registry.get(mug.asset_id).status != "ready":
            raise RuntimeError("acceptance requires real ready asset mug_001 without proxy fallback")
        files = factory.write_result(result, output, export_usd=True)
    except Exception as exc:
        report = build_robot_acceptance_report(
            scene_id="not_generated",
            initial_observation=None,
            final_observation=None,
            steps=0,
            ik="not_run",
            grasp="not_run",
            failure_reason=f"{type(exc).__name__}: {exc}",
            grasp_diagnostics={},
        )
        report.update({"error": report["failure_reason"], "traceback": traceback.format_exc()})
        _write(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--output",
        str(output),
        "--recipe",
        args.recipe,
        "--max-steps",
        str(args.max_steps),
        "--runtime-only",
        "--layout",
        files["layout"],
        "--usd",
        files["usd"],
    ]
    if args.no_headless:
        command.append("--no-headless")
    environment = os.environ.copy()
    environment.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    log_path = output / "isaac_runtime.log"
    report_path.unlink(missing_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        process = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    else:
        report = build_robot_acceptance_report(
            scene_id=result.scene.scene_id,
            initial_observation=None,
            final_observation=None,
            steps=0,
            ik="not_run",
            grasp="not_run",
            failure_reason=f"Isaac runtime process exited with code {process.returncode}",
            grasp_diagnostics={},
        )
        report["runtime_log"] = str(log_path)
        _write(report_path, report)
    _invalidate_report_on_process_failure(report, process.returncode)
    diagnostics_path = output / "grasp_diagnostics.json"
    if not diagnostics_path.is_file():
        _write(diagnostics_path, report.get("grasp_diagnostics", {}))
    report.setdefault("grasp_diagnostics_path", str(diagnostics_path))
    _write(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


def _invalidate_report_on_process_failure(report: dict[str, Any], returncode: int) -> None:
    """Never accept a passed artifact from a failed Isaac child process."""
    if returncode == 0:
        return
    report["runtime_process_returncode"] = int(returncode)
    if report.get("result") == "passed":
        report["result"] = "failed"
        report["failure_reason"] = (
            f"runtime_process_failed: Isaac runtime exited with code {returncode}"
        )


def _build_acceptance_report(
    *,
    scene: dict[str, Any],
    initial_observation: dict[str, Any] | None,
    final_observation: dict[str, Any] | None,
    steps: int,
    summary: dict[str, Any],
    failure_reason: str | None,
) -> dict[str, Any]:
    predicate = scene.get("task", {}).get("success", {}).get("predicate")
    if predicate == "pick_and_place":
        return build_pick_place_acceptance_report(
            scene_id=scene.get("scene_id", "not_loaded"),
            initial_observation=initial_observation,
            final_observation=final_observation,
            steps=steps,
            ik=str(summary.get("ik", "not_run")),
            pick=str(summary.get("pick_status", "not_run")),
            place=str(summary.get("place_status", "not_run")),
            released=bool(summary.get("released")),
            failure_reason=failure_reason,
            grasp_diagnostics=summary.get("grasp_diagnostics"),
        )
    return build_robot_acceptance_report(
        scene_id=scene.get("scene_id", "not_loaded"),
        initial_observation=initial_observation,
        final_observation=final_observation,
        steps=steps,
        ik=str(summary.get("ik", "not_run")),
        grasp=str(summary.get("grasp", "not_run")),
        failure_reason=failure_reason,
        grasp_diagnostics=summary.get("grasp_diagnostics"),
    )


def _run_runtime(
    layout_path: Path,
    usd_path: Path,
    report_path: Path,
    *,
    max_steps: int,
    headless: bool,
) -> int:
    backend = None
    initial_observation = None
    final_observation = None
    failure_reason = None
    summary = {
        "steps": 0,
        "ik": "not_run",
        "grasp": "not_run",
        "grasp_diagnostics": {},
    }
    trace_path = report_path.with_name("robot_trace.jsonl")
    trace_path.unlink(missing_ok=True)
    try:
        scene = json.loads(layout_path.read_text(encoding="utf-8"))
        backend = IsaacSimBackend(usd_path, headless=headless, max_steps=max_steps)
        initial_observation, _ = backend.reset(scene)
        final_observation = initial_observation
        with trace_path.open("w", encoding="utf-8") as trace:
            _write_trace(trace, initial_observation)
            while True:
                final_observation, _, terminated, truncated, summary = backend.step("scripted")
                _write_trace(trace, final_observation)
                trace.flush()
                if terminated or truncated:
                    break
        failure_reason = summary.get("failure_reason")
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"
        summary = backend.runtime_summary if backend is not None else summary
        error_payload = {
            "error": failure_reason,
            "traceback": traceback.format_exc(),
        }
    else:
        error_payload = {}
    if not trace_path.is_file():
        trace_path.write_text(
            json.dumps(
                {
                    "step": int(summary.get("steps", 0)),
                    "phase": summary.get("phase", "not_started"),
                    "failure_reason": failure_reason,
                    "grasp_diagnostics": summary.get("grasp_diagnostics", {}),
                    "runtime_error": error_payload.get("error"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    report = _build_acceptance_report(
        scene=scene if "scene" in locals() else {},
        initial_observation=initial_observation,
        final_observation=final_observation,
        steps=int(summary.get("steps", 0)),
        summary=summary,
        failure_reason=failure_reason,
    )
    report["robot_asset_source"] = summary.get("robot_asset_source")
    _attach_asset_root_diagnostics(report, summary)
    report.update(error_payload)
    report["trace"] = str(trace_path)
    diagnostics = report.get("grasp_diagnostics") or summary.get("grasp_diagnostics", {})
    diagnostics_path = report_path.with_name("grasp_diagnostics.json")
    _write(diagnostics_path, diagnostics)
    report["grasp_diagnostics_path"] = str(diagnostics_path)
    _write(report_path, report)
    if backend is not None:
        backend.close()
    return 0 if report["result"] == "passed" else 2


def _attach_asset_root_diagnostics(report: dict[str, Any], summary: dict[str, Any]) -> None:
    keys = (
        "asset_root_resolution_status",
        "asset_root",
        "asset_root_error",
        "franka_usd",
        "franka_usd_accessible",
        "asset_transport",
        "official_isaac_asset",
        "robot_asset_source",
    )
    diagnostics = {key: summary.get(key) for key in keys}
    report.update(diagnostics)
    report["asset_root_diagnostics"] = diagnostics


def _write_trace(handle, observation: dict[str, Any]) -> None:
    robot = observation.get("robot", {})
    handle.write(
        json.dumps(
            {
                "step": observation.get("simulation_step"),
                "phase": robot.get("phase"),
                "failure_reason": robot.get("failure_reason"),
                "target_position": observation.get("objects", {})
                .get("mug_1", {})
                .get("position"),
                "end_effector_position": robot.get("end_effector_pose", {}).get("position"),
                "end_effector_orientation_wxyz": robot.get("end_effector_pose", {}).get(
                    "orientation_wxyz"
                ),
                "orientation_error_rad": robot.get("orientation_error_rad"),
                "finger_positions": robot.get("finger_positions", {}),
                "finger_bounds": robot.get("finger_bounds", {}),
                "finger_joint_positions": robot.get("joint_positions", [])[-2:],
                "ik_target_joint_positions": robot.get("ik_target_joint_positions", []),
                "applied_joint_position_targets": robot.get(
                    "applied_joint_position_targets", []
                ),
                "grasp_diagnostics": robot.get("grasp_diagnostics", {}),
                "task_oracle": robot.get("task_oracle", {}),
                "pick_status": robot.get("pick_status"),
                "place_status": robot.get("place_status"),
                "released": robot.get("released"),
                "task_success": observation.get("task_success"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
