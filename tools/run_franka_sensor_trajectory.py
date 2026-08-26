from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.factory import SceneFactory  # noqa: E402
from scene_factory.trajectory import DatasetError, EpisodeRecorder, Episode, load_episode  # noqa: E402


_BASE_RUNNER = runpy.run_path(str(PROJECT_ROOT / "tools" / "run_franka_mug_lift.py"))
_build_acceptance_report = _BASE_RUNNER["_build_acceptance_report"]
_invalidate_report_on_process_failure = _BASE_RUNNER["_invalidate_report_on_process_failure"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run real Isaac Sim Franka pick-and-place and export an RGB-D episode."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=77)
    parser.add_argument("--max-steps", type=int, default=720)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--runtime-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--layout", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--usd", type=Path, help=argparse.SUPPRESS)
    return parser


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    episode_path = output / "episode_000000"
    result_path = episode_path / "result.json"
    if args.runtime_only:
        if args.layout is None or args.usd is None:
            raise ValueError("--runtime-only requires --layout and --usd")
        return _run_runtime(
            args.layout.expanduser().resolve(),
            args.usd.expanduser().resolve(),
            episode_path,
            max_steps=args.max_steps,
            headless=not args.no_headless,
        )

    try:
        factory = SceneFactory()
        result = factory.build_from_recipe("kitchen_franka_mug_pick_place", args.seed)
        if not result.valid:
            raise RuntimeError(f"acceptance scene is invalid: {result.validation.to_dict()}")
        mug = next(item for item in result.scene.objects if item.object_id == "mug_1")
        if mug.asset_id != "mug_001" or factory.registry.get(mug.asset_id).status != "ready":
            raise RuntimeError("acceptance requires real ready asset mug_001 without proxy fallback")
        if not result.scene.task.get("camera"):
            raise RuntimeError("P1-3 acceptance recipe has no camera configuration")
        files = factory.write_result(result, output, export_usd=True)
    except Exception as exc:
        _write(
            result_path,
            {
                "result": "failed",
                "failure_reason": f"{type(exc).__name__}: {exc}",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )
        print(result_path.read_text(encoding="utf-8"))
        return 2

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--output",
        str(output),
        "--seed",
        str(args.seed),
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
    result_path.unlink(missing_ok=True)
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
    if result_path.is_file():
        report = json.loads(result_path.read_text(encoding="utf-8"))
    else:
        report = {
            "result": "failed",
            "failure_reason": f"Isaac runtime process exited with code {process.returncode}",
            "runtime_log": str(log_path),
        }
        _write(result_path, report)
    _invalidate_report_on_process_failure(report, process.returncode)
    if process.returncode != 0 and report.get("result") == "passed":
        report["result"] = "failed"
    report["runtime_log"] = str(log_path)
    _write(result_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("result") == "passed" else 2


def _run_runtime(
    layout_path: Path,
    usd_path: Path,
    episode_path: Path,
    *,
    max_steps: int,
    headless: bool,
) -> int:
    from scene_factory.backends import IsaacSimBackend

    backend = None
    recorder: EpisodeRecorder | None = None
    initial_observation: dict[str, Any] | None = None
    final_observation: dict[str, Any] | None = None
    summary: dict[str, Any] = {"steps": 0, "ik": "not_run", "grasp": "not_run"}
    failure_reason: str | None = None
    scene: dict[str, Any] = {}
    try:
        scene = json.loads(layout_path.read_text(encoding="utf-8"))
        backend = IsaacSimBackend(
            usd_path,
            headless=headless,
            max_steps=max_steps,
            enable_rgbd=True,
        )
        initial_observation, _ = backend.reset(scene)
        if backend.runtime_summary.get("robot_asset_source") != "nucleus_franka_usd":
            raise RuntimeError(
                "P1-3 real acceptance requires nucleus_franka_usd; "
                f"got {backend.runtime_summary.get('robot_asset_source')!r}"
            )
        camera = backend.camera_config
        if not camera:
            raise RuntimeError("backend did not initialize the recipe RGB-D camera")
        episode_id = f"{scene['scene_id']}-seed-{scene['seed']}"
        recorder = EpisodeRecorder(
            episode_path,
            {
                "schema_version": "scene_factory.trajectory.v1",
                "scene_id": scene["scene_id"],
                "recipe": scene["recipe_name"],
                "seed": int(scene["seed"]),
                "episode_id": episode_id,
                "robot_asset_source": backend.runtime_summary.get("robot_asset_source"),
                "isaac_sim_version": backend.isaac_sim_version,
                "camera": camera,
                "control_frequency_hz": 1.0 / backend.physics_dt,
                "sensor_frequency_hz": camera["frequency_hz"],
                "intrinsics": None,
                "extrinsics": None,
            },
        )
        final_observation = initial_observation
        _record_frame(
            recorder,
            backend,
            episode_id,
            0,
            initial_observation,
            0.0,
            False,
            False,
        )
        frame_id = 1
        while True:
            final_observation, reward, terminated, truncated, summary = backend.step("scripted")
            _record_frame(
                recorder,
                backend,
                episode_id,
                frame_id,
                final_observation,
                reward,
                terminated,
                truncated,
            )
            frame_id += 1
            if terminated or truncated:
                break
        failure_reason = summary.get("failure_reason")
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"
        if recorder is not None:
            recorder.fail(failure_reason)
        summary = backend.runtime_summary if backend is not None else summary
    report = _build_acceptance_report(
        scene=scene,
        initial_observation=initial_observation,
        final_observation=final_observation,
        steps=int(summary.get("steps", 0)),
        summary=summary,
        failure_reason=failure_reason,
    )
    report["robot_asset_source"] = summary.get("robot_asset_source")
    if recorder is None:
        _write(
            episode_path / "result.json",
            {
                **report,
                "result": "failed",
                "failure_reason": failure_reason or "runtime_initialization_failed",
            },
        )
        if backend is not None:
            backend.close()
        return 2

    finalized = recorder.finalize(report)
    if finalized.get("result") == "passed":
        try:
            episode = load_episode(episode_path)
            _finalize_trajectory_acceptance(finalized, episode)
        except (DatasetError, OSError, ValueError) as exc:
            finalized["result"] = "failed"
            finalized["failure_reason"] = f"dataset_reload_failure: {type(exc).__name__}: {exc}"
            finalized["trajectory_export"]["result"] = "failed"
            finalized["trajectory_export"]["failure_reason"] = finalized["failure_reason"]
    else:
        finalized["dataset_reload"] = {
            "result": "failed",
            "failure_reason": finalized.get("failure_reason"),
        }
    if finalized.get("result") != "passed":
        finalized["result"] = "failed"
    _write(episode_path / "result.json", finalized)
    if backend is not None:
        backend.close()
    return 0 if finalized.get("result") == "passed" else 2


_EXPECTED_PHASES = {
    "PRE_GRASP",
    "APPROACH",
    "GRASP",
    "VERIFY_GRASP",
    "LIFT",
    "TRANSFER",
    "LOWER",
    "RELEASE",
    "VERIFY_PLACE",
    "DONE",
}


def _finalize_trajectory_acceptance(report: dict[str, Any], episode: Episode) -> None:
    """Turn reader validation into explicit acceptance fields before passing."""
    frames = list(episode)
    if not frames:
        raise DatasetError("dataset reload produced no frames")
    frame_ids = [frame.frame_id for frame in frames]
    sim_steps = [frame.sim_step for frame in frames]
    phases = {str(frame.get("phase")) for frame in frames}
    first = frames[0]
    last = frames[-1]
    first_rgb = first.rgb_data
    first_depth = first.depth_data
    last_rgb = last.rgb_data
    last_depth = last.depth_data
    if not first_rgb or not last_rgb or not first_depth or not last_depth:
        raise DatasetError("first or last sensor frame is unreadable")
    if not all(frame.get("sensor", {}).get("finite_depth_values", 0) > 0 for frame in frames):
        raise DatasetError("one or more depth frames have no finite values")
    export = report.setdefault("trajectory_export", {})
    export.update(
        {
            "trajectory_record_count": len(frames),
            "frame_count": len(frames),
            "rgb_frame_count": len(list((episode.path / "rgb").glob("*.png"))),
            "depth_frame_count": len(list((episode.path / "depth").glob("*.npy"))),
            "sim_step_monotonic": all(
                current > previous for previous, current in zip(sim_steps, sim_steps[1:])
            ),
            "frame_id_contiguous": frame_ids == list(range(len(frames))),
            "phase_coverage": sorted(phases),
            "phase_coverage_complete": _EXPECTED_PHASES <= phases,
            "sensor_state_synchronized": True,
            "rgb_shape": first.get("sensor", {}).get("rgb_shape"),
            "depth_shape": first.get("sensor", {}).get("depth_shape"),
            "finite_depth": True,
            "intrinsics_valid": episode.metadata.get("intrinsics") is not None,
            "extrinsics_valid": episode.metadata.get("extrinsics") is not None,
        }
    )
    required = (
        export["trajectory_record_count"] == export["frame_count"] == export["rgb_frame_count"]
        == export["depth_frame_count"] > 0
        and export["sim_step_monotonic"]
        and export["frame_id_contiguous"]
        and export["phase_coverage_complete"]
        and export["sensor_state_synchronized"]
        and export["intrinsics_valid"]
        and export["extrinsics_valid"]
    )
    if not required:
        raise DatasetError("trajectory acceptance fields are incomplete")
    report["dataset_reload"] = {
        "result": "passed",
        "frame_count": len(frames),
        "first_frame_readable": True,
        "last_frame_readable": True,
        "first_rgb_bytes": len(first_rgb),
        "last_rgb_bytes": len(last_rgb),
        "first_depth_shape": [len(first_depth), len(first_depth[0])],
        "last_depth_shape": [len(last_depth), len(last_depth[0])],
    }


def _record_frame(
    recorder: EpisodeRecorder,
    backend: Any,
    episode_id: str,
    frame_id: int,
    observation: dict[str, Any],
    reward: float,
    terminated: bool,
    truncated: bool,
) -> None:
    robot = observation.get("robot", {})
    recorder.append(
        episode_id=episode_id,
        frame_id=frame_id,
        sim_step=int(observation["simulation_step"]),
        timestamp=float(observation["timestamp"]),
        sensor=backend.capture_rgbd_observation(),
        robot_state=robot,
        object_state=observation.get("objects", {}),
        action=observation.get("action") or robot.get("action"),
        oracle=robot.get("task_oracle", {}),
        phase=str(robot.get("phase", "unknown")),
        contact_diagnostics=robot.get("grasp_diagnostics", {}),
        reward=reward,
        terminated=terminated,
        truncated=truncated,
    )


if __name__ == "__main__":
    raise SystemExit(main())
