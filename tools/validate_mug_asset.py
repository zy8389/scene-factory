from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.asset_pipeline import CollisionProcessor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one real USD asset in Isaac Sim/PhysX with a 1 m drop test."
    )
    parser.add_argument("usd", type=Path, help="Normalized asset USD")
    parser.add_argument(
        "--collision",
        type=Path,
        help="Authored collision USD; it is required and never generated",
    )
    parser.add_argument("--asset-id", default="mug_001")
    parser.add_argument("--mass-kg", type=float, default=0.3)
    parser.add_argument("--drop-height-m", type=float, default=1.0)
    parser.add_argument("--steps", type=int, default=360)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("outputs/mug_001_physx"),
        help="ASCII-only Isaac Sim working directory on Windows",
    )
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _blocked_report(
    *,
    asset_id: str,
    usd_path: Path,
    collision_path: Path | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    unavailable = code == "isaac_unavailable"
    return {
        "validator": "SceneFactory/Isaac Sim PhysX asset validation",
        "asset_id": asset_id,
        "usd": str(usd_path),
        "collision_usd": str(collision_path) if collision_path else None,
        "usd_load": "unavailable" if unavailable else "failed",
        "mesh_check": "not_run",
        "mass_check": "not_run",
        "friction_check": "not_run",
        "collision": "unavailable" if unavailable else "failed",
        "physics": "unavailable",
        "valid": False,
        "issues": [{"code": code, "message": message}],
        "collision_generated": False,
    }


def _run(command: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    usd_path = args.usd.expanduser().resolve()
    collision_path = args.collision.expanduser().resolve() if args.collision else None
    report_path = args.report.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()

    if not usd_path.is_file():
        report = _blocked_report(
            asset_id=args.asset_id,
            usd_path=usd_path,
            collision_path=collision_path,
            code="missing_usd",
            message=f"normalized USD does not exist: {usd_path}",
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if collision_path is None:
        report = _blocked_report(
            asset_id=args.asset_id,
            usd_path=usd_path,
            collision_path=None,
            code="missing_collision",
            message="P0-3 requires an authored collision USD; no collision is generated",
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    collision_report = CollisionProcessor().process(
        collision_path,
        collision_status="authored",
        collision_enabled=True,
    )
    if not collision_report["valid"]:
        report = _blocked_report(
            asset_id=args.asset_id,
            usd_path=usd_path,
            collision_path=collision_path,
            code=collision_report["issues"][0]["code"],
            message=collision_report["issues"][0]["message"],
        )
        report["collision_report"] = collision_report
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if sys.platform == "win32" and not _ascii_path(work_dir):
        report = _blocked_report(
            asset_id=args.asset_id,
            usd_path=usd_path,
            collision_path=collision_path,
            code="non_ascii_work_dir",
            message="Isaac Sim/OpenUSD on Windows requires an ASCII-only work directory",
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.mass_kg <= 0 or args.drop_height_m < 0 or args.steps < 1:
        raise ValueError("mass, drop height, and steps must be valid positive values")

    work_dir.mkdir(parents=True, exist_ok=True)
    drop_scene = work_dir / f"{args.asset_id}_drop_test.usda"
    drop_scene_report = work_dir / f"{args.asset_id}_drop_scene.json"
    runtime_report_path = work_dir / f"{args.asset_id}_physx_runtime.json"
    prepare_command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "prepare_asset.py"),
        "drop-scene",
        str(usd_path),
        "--output",
        str(drop_scene),
        "--report",
        str(drop_scene_report),
        "--mass-kg",
        str(args.mass_kg),
        "--height",
        str(args.drop_height_m),
        "--collision",
        str(collision_path),
        "--require-mesh",
    ]
    prepare_code, prepare_stdout, prepare_stderr = _run(prepare_command)
    if prepare_code != 0:
        prepare_message = (prepare_stderr or prepare_stdout).strip() or "could not build drop scene"
        unavailable = "pxr is missing" in prepare_message or "Isaac Sim" in prepare_message
        report = _blocked_report(
            asset_id=args.asset_id,
            usd_path=usd_path,
            collision_path=collision_path,
            code="isaac_unavailable" if unavailable else "drop_scene_failed",
            message=prepare_message,
        )
        report["collision_report"] = collision_report
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    runtime_command = [
        sys.executable,
        str(PROJECT_ROOT / "tools" / "validate_isaac_runtime.py"),
        str(drop_scene),
        "--steps",
        str(args.steps),
        "--report",
        str(runtime_report_path),
        "--asset-id",
        args.asset_id,
        "--collision-required",
        "--mesh-required",
        "--mass-required",
        "--physics-material-required",
    ]
    runtime_code, runtime_stdout, runtime_stderr = _run(runtime_command)
    if runtime_report_path.is_file():
        runtime_report = json.loads(runtime_report_path.read_text(encoding="utf-8"))
    else:
        runtime_report = {
            "valid": False,
            "error": (runtime_stderr or runtime_stdout).strip() or "Isaac runtime produced no report",
        }

    checks = runtime_report.get("checks", {})
    usd_load = "passed" if checks.get("stage_opened") else "failed"
    mesh_check = (
        "passed"
        if runtime_report.get("mesh_check") == "passed" or checks.get("asset_mesh_found")
        else "failed"
    )
    collision = "passed" if runtime_report.get("collision") == "passed" else "failed"
    physics = "passed" if runtime_report.get("physics") == "passed" else "failed"
    mass_check = (
        "passed"
        if runtime_report.get("mass_check") == "passed" or checks.get("mass_found")
        else "failed"
    )
    friction_check = (
        "passed"
        if runtime_report.get("friction_check") == "passed"
        or checks.get("physics_material_found")
        else "failed"
    )
    source_valid = True
    report = {
        "validator": "SceneFactory/Isaac Sim PhysX asset validation",
        "asset_id": args.asset_id,
        "usd": str(usd_path),
        "collision_usd": str(collision_path),
        "drop_height_m": args.drop_height_m,
        "mass_kg": args.mass_kg,
        "usd_load": usd_load,
        "mesh_check": mesh_check,
        "mass_check": mass_check,
        "friction_check": friction_check,
        "collision": collision,
        "physics": physics,
        "valid": runtime_code == 0
        and usd_load == mesh_check == mass_check == friction_check == collision == physics == "passed",
        "collision_generated": False,
        "collision_report": collision_report,
        "runtime_report": runtime_report,
    }
    if args.source_manifest:
        source_manifest = args.source_manifest.expanduser().resolve()
        if source_manifest.is_file():
            try:
                report["source"] = json.loads(source_manifest.read_text(encoding="utf-8"))
                source_valid = bool(
                    isinstance(report["source"], dict)
                    and report["source"].get("status") in {"imported", "passed"}
                    and (
                        report["source"].get("archive_sha256")
                        or report["source"].get("sha256")
                    )
                    and report["source"].get("source_geometry")
                )
            except (OSError, json.JSONDecodeError) as exc:
                report["source"] = {"status": "invalid", "error": str(exc)}
                source_valid = False
        else:
            report["source"] = {"status": "missing", "path": str(source_manifest)}
            source_valid = False
    else:
        report["source"] = {"status": "not_provided"}
    report["valid"] = bool(report["valid"] and source_valid)
    if not source_valid:
        report.setdefault("issues", []).append(
            {
                "code": "invalid_source_manifest",
                "message": "real YCB source manifest is missing or incomplete",
            }
        )
    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
