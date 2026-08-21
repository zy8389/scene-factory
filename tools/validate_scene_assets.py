from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.factory import SceneFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and QA one real-asset SceneFactory recipe.")
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--usd", action="store_true", help="Export a USD scene")
    parser.add_argument(
        "--run-isaac",
        action="store_true",
        help="Open the exported USD and initialize one Isaac Sim physics step",
    )
    return parser


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _portable_path(value: str, output: Path) -> str:
    """Represent generated scene files without embedding a local drive path."""
    candidate = Path(value)
    if not candidate.is_absolute():
        return value
    try:
        return f"runtime/{candidate.relative_to(output).as_posix()}"
    except ValueError:
        try:
            return candidate.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return value


def _portableize_paths(value: Any, output: Path) -> Any:
    if isinstance(value, dict):
        return {key: _portableize_paths(item, output) for key, item in value.items()}
    if isinstance(value, list):
        return [_portableize_paths(item, output) for item in value]
    if isinstance(value, str) and len(value) > 2 and value[1] == ":":
        return _portable_path(value, output)
    return value


def _run_isaac(usd_path: Path, report_path: Path) -> tuple[str, str, dict[str, Any]]:
    command = [sys.executable, str(PROJECT_ROOT / "tools" / "validate_scene_runtime.py"), str(usd_path), "--report", str(report_path)]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    if report_path.is_file():
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        return str(payload.get("stage_load", "failed")), str(payload.get("physics_initialization", "failed")), payload
    return "unavailable", "not_run", {
        "code": "isaac_scene_validator_failed",
        "returncode": completed.returncode,
        "stderr": completed.stderr[-2000:],
        "stdout": completed.stdout[-2000:],
    }


def _stage_registry(factory: SceneFactory, output: Path) -> Path:
    """Copy real asset packages beside an ASCII scene staging directory."""
    assets_dir = output / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    raw_records: list[dict[str, Any]] = []
    registry_path = factory.registry.registry_path
    assert registry_path is not None
    for line in registry_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raw_records.append(json.loads(line))
    for raw in raw_records:
        if raw.get("source_type") not in {"local_usd", "usd"} or raw.get("status") != "ready":
            continue
        record = factory.registry.get(str(raw["asset_id"]))
        source = factory.registry.resolve_source_path(record)
        collision = factory.registry.resolve_collision_path(record)
        if source:
            shutil.copy2(source, assets_dir / f"{record.asset_id}.usd")
            raw["usd_path"] = f"assets/{record.asset_id}.usd"
            raw.pop("source_path", None)
            source_dir = Path(source).parent
            for companion in source_dir.glob("source_*_clean.usd"):
                shutil.copy2(companion, assets_dir / companion.name)
        if collision:
            shutil.copy2(collision, assets_dir / f"{record.asset_id}_collision.usd")
            raw["collision_path"] = f"assets/{record.asset_id}_collision.usd"
    staged_registry = output / "registry.jsonl"
    staged_registry.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in raw_records), encoding="utf-8")
    return staged_registry


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report_path = args.report.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    base_factory = SceneFactory()
    staged_registry = _stage_registry(base_factory, output)
    factory = SceneFactory(registry_path=staged_registry)
    result = factory.build_from_recipe(args.recipe, args.seed)
    files = factory.write_result(result, output, export_usd=args.usd)
    real_assets: list[dict[str, str]] = []
    proxy_assets: list[dict[str, str]] = []
    fallback_reasons: dict[str, str] = {}
    missing_dependencies: list[str] = []
    for item in result.scene.objects:
        record = factory.registry.get(item.asset_id)
        entry = {"object_id": item.object_id, "asset_id": item.asset_id}
        if record.source_type in {"local_usd", "usd"} and record.status == "ready":
            real_assets.append(entry)
            usd_path = factory.registry.resolve_source_path(record)
            collision_path = factory.registry.resolve_collision_path(record)
            if usd_path and "://" not in usd_path and not Path(usd_path).is_file():
                missing_dependencies.append(usd_path)
            if collision_path and "://" not in collision_path and not Path(collision_path).is_file():
                missing_dependencies.append(collision_path)
        else:
            proxy_assets.append(entry)
        if item.fallback_reason:
            fallback_reasons[item.object_id] = item.fallback_reason

    usd_path = Path(files["usd"]).resolve() if "usd" in files else None
    usd_export = "passed" if usd_path and usd_path.is_file() else "not_run"
    stage_load = "not_run"
    physics_initialization = "not_run"
    isaac_details: dict[str, Any] = {}
    if args.run_isaac:
        if usd_path is None:
            stage_load = physics_initialization = "not_run"
            isaac_details = {"code": "usd_not_exported", "message": "--run-isaac requires --usd"}
        else:
            runtime_report_path = output / "isaac_scene_runtime.json"
            stage_load, physics_initialization, isaac_details = _run_isaac(usd_path, runtime_report_path)

    report = {
        "recipe": args.recipe,
        "seed": args.seed,
        "scene_id": result.scene.scene_id,
        "scene_valid": result.valid,
        "real_assets": real_assets,
        "proxy_assets": proxy_assets,
        "fallback_reasons": fallback_reasons,
        "missing_dependencies": missing_dependencies,
        "usd_export": usd_export,
        "isaac_stage_load": stage_load,
        "physics_initialization": physics_initialization,
        "isaac": isaac_details,
        "files": files,
        "result": "passed" if result.valid and not missing_dependencies and stage_load not in {"failed", "unavailable"} and physics_initialization not in {"failed", "unavailable"} else "failed",
    }
    _write_json(report_path, _portableize_paths(report, output))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
