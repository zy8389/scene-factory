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

from scene_factory.asset_profiles import physics_defaults
from scene_factory.asset_sources import AssetSourceResolver
from scene_factory.batch_ingestion import BatchAssetResult, BatchReport, validate_batch_config
from scene_factory.registry import AssetRegistry


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the independent batch real-asset ingestion pipeline.")
    parser.add_argument("config", type=Path, nargs="?", default=Path("configs/assets_batch.json"))
    parser.add_argument("--report", type=Path, default=Path("data/assets/qa_reports/batch_p0_4.json"))
    parser.add_argument("--runtime-root", type=Path, default=Path("F:/scene_factory_runtime/p0_4_batch"))
    parser.add_argument("--isaac-python", type=Path, default=Path("F:/scene_factory_isaac_py312/Scripts/python.exe"))
    parser.add_argument("--run-isaac", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*", default=None)
    return parser


def _run(command: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _repo_path(path: Path) -> str:
    """Keep committed batch reports independent of the checkout location."""
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _copy_source_to_stage(source_dir: Path, stage: Path, *, force: bool = False) -> None:
    """Stage immutable source files without destroying resumable outputs."""
    if stage.exists() and force:
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)
    for source in source_dir.rglob("*"):
        if source.is_file() and source.name != "SOURCE.json":
            target = stage / source.relative_to(source_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _report_passed(path: Path, *, valid_key: str = "result") -> bool:
    if not path.is_file():
        return False
    try:
        payload = _load_manifest(path)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("valid") if valid_key == "valid" else payload.get(valid_key) == "passed")


def _run_step(
    command: list[str],
    label: str,
    *,
    output: Path,
    report: Path,
    force: bool,
    valid_key: str = "result",
) -> dict[str, Any]:
    """Reuse a complete stage on resume; only --force overwrites stale output."""
    if output.exists() and not force:
        if _report_passed(report, valid_key=valid_key):
            return {"reused": True, "report": str(report)}
        raise RuntimeError(
            f"{label} output already exists without a passing report: {output}; use --force"
        )
    if force and output.exists():
        output.unlink()
    return _command_or_fail(command, label)


def _blocked_registry_record(item: dict[str, Any], batch_id: str, reason: str) -> dict[str, Any]:
    defaults = physics_defaults(str(item["category"]), item.get("physics"))
    return {
        "asset_id": item["asset_id"],
        "name": item["asset_id"],
        "category": item["category"],
        "bbox_m": [1.0, 1.0, 1.0],
        "source_type": "real_asset_pending",
        "collision_mode": "none",
        "collision_status": "not_provided",
        "collision_enabled": False,
        "rigid_body": True,
        "mass": defaults.mass_kg,
        "friction": defaults.dynamic_friction,
        "static_friction": defaults.static_friction,
        "dynamic_friction": defaults.dynamic_friction,
        "physics_parameters_source": defaults.source,
        "status": "rejected",
        "tags": list(item.get("scene_tags", [])),
        "batch_id": batch_id,
        "failure_reason": reason,
    }


def _command_or_fail(command: list[str], label: str) -> dict[str, Any]:
    code, stdout, stderr = _run(command)
    if code != 0:
        detail = (stderr or stdout).strip()[-2000:]
        raise RuntimeError(f"{label} failed ({code}): {detail}")
    return {"command": command, "stdout": stdout[-2000:]}


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _asset_record(
    item: dict[str, Any],
    manifest: dict[str, Any],
    repo_usd: Path,
    repo_collision: Path,
    qa_path: Path,
    batch_id: str,
    bbox: list[float],
) -> dict[str, Any]:
    defaults = physics_defaults(str(item["category"]), item.get("physics"))
    source_hash = manifest.get("sha256") or manifest.get("source_files", [{}])[0].get("sha256")
    return {
        "asset_id": item["asset_id"],
        "name": manifest.get("source_name", item["asset_id"]),
        "category": item["category"],
        "bbox_m": bbox,
        "usd_path": str(repo_usd.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
        "collision_path": str(repo_collision.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
        "source_type": "local_usd",
        "collision_mode": "authored",
        "collision_status": "validated",
        "collision_enabled": True,
        "rigid_body": True,
        "mass": defaults.mass_kg,
        "friction": defaults.dynamic_friction,
        "static_friction": defaults.static_friction,
        "dynamic_friction": defaults.dynamic_friction,
        "physics_parameters_source": defaults.source,
        "source": f"{manifest.get('dataset', manifest.get('source_name', 'source'))}@{manifest.get('source_revision', '')}".rstrip("@"),
        "source_url": manifest.get("source_url"),
        "license": manifest.get("license"),
        "hash": f"sha256:{source_hash}" if source_hash and not str(source_hash).startswith("sha256:") else source_hash,
        "qa_report": str(qa_path.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
        "status": "normalized",
        "tags": list(item.get("scene_tags", [])),
        "batch_id": batch_id,
    }


def _process_asset(
    item: dict[str, Any],
    result: BatchAssetResult,
    *,
    config: dict[str, Any],
    args: argparse.Namespace,
    resolver: AssetSourceResolver,
    registry: AssetRegistry,
) -> BatchAssetResult:
    asset_id = str(item["asset_id"])
    try:
        current = registry.get(asset_id)
    except KeyError:
        current = None
    # A forced run may rebuild generated stages, but it cannot downgrade a
    # ready asset when the batch has no source candidate to rebuild from.
    if current is not None and current.status == "ready" and (
        not args.force or not item.get("preferred_sources")
    ):
        registry.update(
            asset_id,
            {"batch_id": str(config["batch_id"]), "failure_reason": None},
            persist_path=registry.registry_path,
        )
        result.transition("ready", registry_status="ready", reused=True)
        return result
    if not item.get("preferred_sources"):
        reason = str(item.get("blocked_reason", "no source candidate configured"))
        result.block("source_unresolved", reason)
        registry.upsert_batch(
            [_blocked_registry_record(item, str(config["batch_id"]), reason)],
            persist_path=registry.registry_path,
            batch_id=str(config["batch_id"]),
        )
        return result
    result.transition("source_resolved", source_candidates=len(resolver.candidates(asset_id)))
    source_dir = PROJECT_ROOT / "data/assets/source" / asset_id
    # --force rebuilds generated stages; an immutable source with a matching
    # manifest remains idempotent unless the fetch CLI is explicitly forced.
    source_force = args.force and not (source_dir / "SOURCE.json").is_file()
    fetched = resolver.fetch(asset_id, source_dir, force=source_force, dry_run=args.dry_run)
    result.details["source_manifest"] = _repo_path(source_dir / "SOURCE.json")
    if args.dry_run:
        result.transition("raw", dry_run=True, next_step="download and run with --run-isaac")
        return result
    if fetched.get("idempotent"):
        result.transition("downloaded", idempotent=True)
    elif fetched.get("result") == "passed":
        result.transition("downloaded")
    else:
        issue = fetched.get("issues", [{"code": "source_fetch_failed", "message": "source fetch failed"}])[0]
        result.block(issue.get("code", "source_fetch_failed"), issue.get("message", "source fetch failed"))
        registry.upsert_batch(
            [_blocked_registry_record(item, str(config["batch_id"]), issue.get("message", "source fetch failed"))],
            persist_path=registry.registry_path,
            batch_id=str(config["batch_id"]),
        )
        return result
    result.transition("raw")
    if args.dry_run or not args.run_isaac:
        result.details["next_step"] = "run with --run-isaac for conversion and PhysX validation"
        return result
    if not args.isaac_python.is_file():
        result.block("isaac_unavailable", f"Isaac Python does not exist: {args.isaac_python}")
        return result

    runtime_root = args.runtime_root.resolve()
    stage_root = runtime_root / asset_id
    _copy_source_to_stage(source_dir, stage_root / "source", force=args.force)
    visual_source = next((path for path in (stage_root / "source").glob("*") if path.suffix.lower() in {".glb", ".obj", ".stl", ".dae"}), None)
    collision_source = next((path for path in (stage_root / "source" / "collision").glob("*") if path.suffix.lower() in {".glb", ".obj", ".stl", ".dae"}), None)
    if visual_source is None or collision_source is None:
        result.fail("source_geometry_missing", "visual and collision source files are required")
        return result
    imported = stage_root / "visual_imported.usd"
    collision_imported = stage_root / "collision_source.usd"
    clean = stage_root / "source_clean.usd"
    normalized = stage_root / f"{asset_id}.usd"
    relocated = stage_root / f"{asset_id}_portable.usd"
    collision = stage_root / f"{asset_id}_collision.usd"
    _run_step(
        [str(args.isaac_python), "tools/convert_asset.py", str(visual_source), "--output", str(imported), "--asset-id", asset_id, "--report", str(stage_root / "convert_visual.json")],
        "visual conversion",
        output=imported,
        report=stage_root / "convert_visual.json",
        force=args.force,
    )
    _run_step(
        [str(args.isaac_python), "tools/convert_asset.py", str(collision_source), "--output", str(collision_imported), "--asset-id", asset_id, "--report", str(stage_root / "convert_collision.json")],
        "collision conversion",
        output=collision_imported,
        report=stage_root / "convert_collision.json",
        force=args.force,
    )
    _run_step(
        [str(args.isaac_python), "tools/sanitize_usd_materials.py", str(imported), "--output", str(clean), "--asset-id", asset_id, "--report", str(stage_root / "sanitize.json")],
        "material sanitization",
        output=clean,
        report=stage_root / "sanitize.json",
        force=args.force,
    )
    result.transition("converted")
    _run_step(
        [str(args.isaac_python), "tools/prepare_asset.py", "wrap", str(clean), "--output", str(normalized), "--report", str(stage_root / "normalize.json"), "--asset-id", asset_id, "--category", str(item["category"]), "--collision", "none", "--mass-kg", str(item.get("physics", {}).get("mass_kg", 0.5)), "--static-friction", str(item.get("physics", {}).get("static_friction", 0.5)), "--dynamic-friction", str(item.get("physics", {}).get("dynamic_friction", 0.4)), "--source-type", "local_usd", "--license", str((resolver.resolve(asset_id)).license)],
        "normalization",
        output=normalized,
        report=stage_root / "normalize.json",
        force=args.force,
        valid_key="valid",
    )
    result.transition("normalized")
    _run_step(
        [str(args.isaac_python), "tools/relocate_usd_references.py", str(normalized), "--output", str(relocated), "--asset-id", asset_id, "--reference", f"source_{asset_id}_clean.usd", "--report", str(stage_root / "relocate.json")],
        "USD reference relocation",
        output=relocated,
        report=stage_root / "relocate.json",
        force=args.force,
    )
    _run_step(
        [str(args.isaac_python), "tools/author_collision_usd.py", str(collision_imported), "--output", str(collision), "--asset-id", asset_id, "--static-friction", str(item.get("physics", {}).get("static_friction", 0.5)), "--dynamic-friction", str(item.get("physics", {}).get("dynamic_friction", 0.4)), "--report", str(stage_root / "collision.json")],
        "collision authoring",
        output=collision,
        report=stage_root / "collision.json",
        force=args.force,
    )
    result.transition("collision_ready")

    repo_usd = PROJECT_ROOT / "data/assets/usd" / f"{asset_id}.usd"
    repo_source_usd = PROJECT_ROOT / "data/assets/usd" / f"source_{asset_id}_clean.usd"
    repo_collision = PROJECT_ROOT / "data/assets/collision" / f"{asset_id}_collision.usd"
    repo_metadata = PROJECT_ROOT / "data/assets/metadata" / f"{asset_id}.json"
    repo_qa = PROJECT_ROOT / "data/assets/qa_reports" / f"{asset_id}.json"
    repo_usd.parent.mkdir(parents=True, exist_ok=True)
    repo_collision.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(relocated, repo_usd)
    shutil.copy2(clean, repo_source_usd)
    shutil.copy2(collision, repo_collision)
    normalize_report = _load_manifest(stage_root / "normalize.json")
    collision_report = _load_manifest(stage_root / "collision.json")
    collision_report["valid"] = collision_report.get("result") == "passed"
    collision_report.setdefault("generated", False)
    manifest = _load_manifest(source_dir / "SOURCE.json")
    source_hash = manifest.get("sha256") or manifest.get("source_files", [{}])[0].get("sha256")
    bbox = list(normalize_report.get("wrapped_bbox_m") or normalize_report.get("bbox_m") or [0.1, 0.1, 0.1])
    result.details["bbox_m"] = bbox
    defaults = physics_defaults(str(item["category"]), item.get("physics"))
    repo_metadata.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        repo_metadata,
        {
            "asset_id": asset_id,
            "name": manifest.get("source_name", asset_id),
            "category": item["category"],
            "source": f"{manifest.get('dataset', manifest.get('source_name', 'source'))}@{manifest.get('source_revision', '')}".rstrip("@"),
            "source_url": manifest.get("source_url"),
            "license": manifest.get("license"),
            "source_manifest": str((source_dir / "SOURCE.json").relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
            "hash": f"sha256:{source_hash}" if source_hash and not str(source_hash).startswith("sha256:") else source_hash,
            "usd_path": str(repo_usd.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
            "collision_path": str(repo_collision.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
            "bbox_m": bbox,
            "normalized": bool(normalize_report.get("valid")),
            "collision_level": "L1",
            "collision_profile": item["collision_profile"],
            "collision_mode": "authored",
            "mass": defaults.mass_kg,
            "static_friction": defaults.static_friction,
            "dynamic_friction": defaults.dynamic_friction,
            "physics_parameters_source": defaults.source,
            "validation_profile": item["validation_profile"],
            "status": "normalized",
        },
    )
    record = _asset_record(item, manifest, repo_usd, repo_collision, repo_qa, str(config["batch_id"]), bbox)
    registry.upsert_batch(
        [record],
        persist_path=registry.registry_path,
        batch_id=str(config["batch_id"]),
        allow_ready_downgrade=args.force,
    )
    collision_report["collision_path"] = str(
        repo_collision.relative_to(PROJECT_ROOT / "data/assets")
    ).replace("\\", "/")
    registry.promote_to_validated(asset_id, normalize_report, collision_report=collision_report, persist_path=registry.registry_path)
    profile_name = str(item["validation_profile"])
    collision_name = str(item["collision_profile"])
    _run_step(
        [str(args.isaac_python), "tools/validate_asset.py", "--asset-id", asset_id, "--usd", str(normalized), "--collision", str(collision), "--profile", profile_name, "--collision-profile", collision_name, "--mass-kg", str(item.get("physics", {}).get("mass_kg", 0.5)), "--source-manifest", str(source_dir / "SOURCE.json"), "--work-dir", str(stage_root / "qa"), "--report", str(repo_qa), "--material-sanitized", "passed"],
        "Isaac validation",
        output=repo_qa,
        report=repo_qa,
        force=args.force,
        valid_key="valid",
    )
    qa = _load_manifest(repo_qa)
    if not qa.get("valid"):
        result.fail("isaac_validation_failed", "generic validator returned invalid QA")
        return result
    registry.promote_to_ready(asset_id, qa, persist_path=registry.registry_path)
    registry.update(asset_id, {"qa_report": str(repo_qa.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"), "last_validation": profile_name}, persist_path=registry.registry_path)
    metadata_payload = _load_manifest(repo_metadata)
    metadata_payload.update(
        {
            "status": "ready",
            "qa_report": str(repo_qa.relative_to(PROJECT_ROOT / "data/assets")).replace("\\", "/"),
            "validated_use_cases": qa.get("validated_use_cases", []),
            "unsupported_use_cases": qa.get("unsupported_use_cases", []),
            "final_linear_velocity": qa.get("physx", {}).get("final_linear_velocity", "not_reported"),
            "final_angular_velocity": qa.get("physx", {}).get("final_angular_velocity", "not_reported"),
        }
    )
    _write_json(repo_metadata, metadata_payload)
    result.transition("validated")
    result.transition("ready", usd_path=_repo_path(repo_usd), qa_report=_repo_path(repo_qa))
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    issues = validate_batch_config(config)
    if issues:
        raise SystemExit("invalid batch config: " + "; ".join(issues))
    if args.only:
        config["assets"] = [item for item in config["assets"] if item["asset_id"] in set(args.only)]
    resolver = AssetSourceResolver.from_config(config)
    registry_path = PROJECT_ROOT / "data/assets/registry.jsonl"
    registry = AssetRegistry.load(registry_path)
    existing: dict[str, dict[str, Any]] = {}
    if args.report.is_file():
        existing = json.loads(args.report.read_text(encoding="utf-8")).get("assets", {})
    results: dict[str, BatchAssetResult] = {}
    for item in config["assets"]:
        asset_id = str(item["asset_id"])
        prior = existing.get(asset_id, {})
        result = BatchAssetResult(asset_id)
        if not args.force and prior.get("state") == "ready":
            result.resumed = True
            try:
                current = registry.get(asset_id)
                if current.status == "ready":
                    registry.update(
                        asset_id,
                        {"batch_id": str(config["batch_id"]), "failure_reason": None},
                        persist_path=registry.registry_path,
                    )
            except KeyError:
                pass
            result.transition("ready", **{key: value for key, value in prior.items() if key not in {"state", "status"}})
        else:
            try:
                result = _process_asset(
                    item, result, config=config, args=args, resolver=resolver, registry=registry
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                result.fail("asset_processing_failed", message)
                try:
                    registry.upsert_batch(
                        [_blocked_registry_record(item, str(config["batch_id"]), message)],
                        persist_path=registry.registry_path,
                        batch_id=str(config["batch_id"]),
                    )
                except (KeyError, ValueError):
                    # A failed update must not mask the per-asset batch result.
                    pass
        results[asset_id] = result
        BatchReport(str(config["batch_id"]), len(config["assets"]), results).write(args.report)
    report = BatchReport(str(config["batch_id"]), len(config["assets"]), results)
    report.write(args.report)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.result == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
