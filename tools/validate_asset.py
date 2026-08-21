from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.asset_profiles import collision_profile, validation_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the generic SceneFactory Isaac asset validator.")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--usd", type=Path)
    parser.add_argument("--collision", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("F:/scene_factory_runtime/asset_validation"))
    parser.add_argument("--profile", default="drop", choices=("drop", "drop_thin_object"))
    parser.add_argument("--collision-profile", default=None)
    parser.add_argument("--mass-kg", type=float, default=None)
    parser.add_argument("--drop-height-m", type=float, default=None)
    parser.add_argument("--steps", type=int, default=None)
    parser.add_argument(
        "--material-sanitized",
        choices=("passed", "failed", "not_run"),
        default="not_run",
        help="Material sanitization result recorded in the generic QA schema",
    )
    return parser


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path | None, Path | None]:
    usd = args.usd
    collision = args.collision
    manifest = args.source_manifest
    if args.registry:
        from scene_factory.registry import AssetLoader, AssetRegistry

        registry = AssetRegistry.load(args.registry)
        record = registry.get(args.asset_id)
        loader = AssetLoader(registry)
        usd = usd or (Path(loader.resolve_usd_path(record)) if loader.resolve_usd_path(record) else None)
        collision = collision or (
            Path(loader.resolve_collision_path(record))
            if loader.resolve_collision_path(record)
            else None
        )
        if manifest is None and record.source_path:
            candidate = Path(record.source_path).parent.parent / "SOURCE.json"
            if candidate.is_file():
                manifest = candidate
    if usd is None:
        raise ValueError("--usd or --registry is required")
    return usd, collision, manifest


def _portableize_report_paths(value: object, work_dir: Path) -> object:
    """Keep committed QA reports independent of the local Windows drive."""
    if isinstance(value, dict):
        return {key: _portableize_report_paths(item, work_dir) for key, item in value.items()}
    if isinstance(value, list):
        return [_portableize_report_paths(item, work_dir) for item in value]
    if not isinstance(value, str) or len(value) < 3 or value[1] != ":":
        return value
    path = Path(value).expanduser().resolve()
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        work_root = work_dir.expanduser().resolve()
        for candidate_root in (work_root, work_root.parent):
            try:
                return f"runtime/{path.relative_to(candidate_root).as_posix()}"
            except ValueError:
                continue
        return value


def _decorate(
    report_path: Path,
    asset_id: str,
    profile_name: str,
    collision_name: str | None,
    material_sanitized: str,
    work_dir: Path,
) -> None:
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    profile = validation_profile(profile_name)
    collision = collision_profile(collision_name) if collision_name else None
    report["validation_profile"] = profile.name
    report["collision_profile"] = collision.name if collision else None
    report["validated_use_cases"] = list(collision.validated_use_cases) if collision else ["drop"]
    report["unsupported_use_cases"] = list(collision.unsupported_use_cases) if collision else []
    geometry = report.setdefault("geometry", {})
    geometry.update(
        {
            "usd_load": report.get("usd_load", "not_run"),
            "mesh": report.get("mesh_check", "not_run"),
            "material_sanitized": material_sanitized,
        }
    )
    runtime = report.get("runtime_report", {})
    final_velocity = runtime.get("final_linear_velocity_mps")
    final_angular_velocity = runtime.get("final_angular_velocity_rps")
    report["physx"] = {
        "environment_available": runtime.get("isaac_sim_version") is not None,
        "drop_test": report.get("physics", "not_run"),
        "final_linear_velocity": final_velocity or "not_reported",
        "final_angular_velocity": final_angular_velocity or "not_reported",
    }
    report["result"] = "passed" if report.get("valid") else "failed"
    report["asset_id"] = asset_id
    portable = _portableize_report_paths(report, work_dir)
    report_path.write_text(json.dumps(portable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        usd, collision, manifest = _resolve_paths(args)
    except (KeyError, OSError, ValueError) as exc:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "asset_id": args.asset_id,
                    "valid": False,
                    "result": "blocked",
                    "issues": [{"code": "validation_inputs_unresolved", "message": str(exc)}],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _decorate(
            args.report.resolve(),
            args.asset_id,
            args.profile,
            args.collision_profile,
            args.material_sanitized,
            args.work_dir,
        )
        return 2
    profile = validation_profile(args.profile)
    from tools import validate_mug_asset as legacy

    translated = [
        str(usd),
        "--asset-id",
        args.asset_id,
        "--collision",
        str(collision) if collision else "",
        "--drop-height-m",
        str(args.drop_height_m if args.drop_height_m is not None else profile.drop_height_m),
        "--steps",
        str(args.steps if args.steps is not None else profile.steps),
        "--work-dir",
        str(args.work_dir),
        "--report",
        str(args.report),
    ]
    if collision is None:
        translated.remove("--collision")
        translated.remove("")
    if args.mass_kg is not None:
        translated.extend(["--mass-kg", str(args.mass_kg)])
    if manifest is not None:
        translated.extend(["--source-manifest", str(manifest)])
    result = legacy.main(translated)
    _decorate(
        args.report.resolve(),
        args.asset_id,
        args.profile,
        args.collision_profile,
        args.material_sanitized,
        args.work_dir,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
