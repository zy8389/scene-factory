from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .asset_pipeline import AssetPipelineUnavailable, inspect_usd
from .models import AssetRecord
from .registry import AssetLoader, AssetMetadata, AssetRegistry


def _issue(
    issues: list[dict[str, str]],
    code: str,
    message: str,
    *,
    severity: str = "error",
) -> None:
    issues.append({"code": code, "message": message, "severity": severity})


def _write_report(path: str | Path, report: dict[str, Any]) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def _metadata_checks(
    metadata: AssetMetadata,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "mass": metadata.mass,
        "friction": metadata.friction,
        "support_surface_count": len(metadata.support_surface),
    }
    if (
        metadata.mass is None
        or not math.isfinite(metadata.mass)
        or metadata.mass <= 0
    ):
        _issue(issues, "missing_or_invalid_mass", "mass must be a finite positive number")
    if (
        metadata.friction is None
        or not math.isfinite(metadata.friction)
        or metadata.friction < 0
    ):
        _issue(issues, "invalid_friction", "friction must be a finite non-negative number")
    for surface in metadata.support_surface:
        if any(value <= 0 for value in surface.size):
            _issue(
                issues,
                "invalid_support_surface",
                f"support surface {surface.name!r} has non-positive size",
            )
        if metadata.bbox_m is not None:
            if abs(surface.center[2]) > metadata.bbox_m[2] / 2.0 + 1.0e-6:
                _issue(
                    issues,
                    "support_surface_outside_bbox",
                    f"support surface {surface.name!r} is outside the asset bbox",
                    severity="warning",
                )
    return checks


def _path_check(
    value: str | None,
    *,
    label: str,
    issues: list[dict[str, str]],
) -> str | None:
    if not value:
        return None
    if "://" in value:
        _issue(
            issues,
            "remote_path",
            f"{label} uses a URI and was not checked locally",
            severity="warning",
        )
        return value
    path = Path(value)
    if not path.is_file():
        _issue(issues, f"missing_{label}", f"{label} does not exist: {value}")
    return value


def _usd_checks(
    usd_path: str,
    *,
    collision_mode: str | None,
    collision_path: str | None,
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        inspection = inspect_usd(usd_path)
    except AssetPipelineUnavailable as exc:
        _issue(issues, "usd_inspection_unavailable", str(exc))
        return {"available": False, "source_path": usd_path, "error": str(exc)}
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        _issue(issues, "usd_inspection_failed", str(exc))
        return {"available": False, "source_path": usd_path, "error": str(exc)}

    if inspection.get("up_axis") != "Z":
        _issue(
            issues,
            "non_z_up",
            f"USD stage up axis is {inspection.get('up_axis')}, expected Z",
        )
    if abs(float(inspection.get("meters_per_unit", 0.0)) - 1.0) > 1.0e-9:
        _issue(issues, "non_meter_units", "USD stage metersPerUnit must be 1.0")
    if not inspection.get("has_default_prim"):
        _issue(issues, "missing_default_prim", "USD stage has no default Prim")
    counts = inspection.get("counts", {})
    if int(counts.get("mesh_prims", 0)) <= 0:
        _issue(issues, "missing_mesh", "USD stage contains no mesh prim")

    collision_checked = bool(collision_path)
    if collision_mode not in {None, "none", "primitive"} and not collision_path:
        collision_checked = True
        if int(counts.get("collision_prims", 0)) <= 0:
            _issue(
                issues,
                "missing_collision",
                "asset declares collision support but no collision prim was found",
            )
    if collision_path:
        # A collision mesh may be a standalone USD, so path existence is the
        # required check here; Isaac Sim can inspect its authored APIs later.
        collision_checked = True

    return {
        "available": True,
        "inspection": inspection,
        "collision_checked": collision_checked,
    }


def validate_asset(
    asset: AssetRecord | AssetMetadata | str,
    registry: AssetRegistry | None = None,
    *,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate one registry asset and optionally write a deterministic QA report."""
    if isinstance(asset, str):
        if registry is None:
            raise ValueError("registry is required when validating an asset ID")
        record = registry.get(asset)
        metadata = registry.metadata(asset)
    elif isinstance(asset, AssetMetadata):
        metadata = asset
        record = asset.to_record()
    else:
        record = asset
        metadata = AssetMetadata.from_record(asset)

    issues: list[dict[str, str]] = []
    loader = AssetLoader(registry) if registry is not None else None
    usd_path = (
        loader.resolve_usd_path(record)
        if loader is not None
        else (record.usd_path or record.source_path)
    )
    collision_path = (
        loader.resolve_collision_path(record)
        if loader is not None
        else record.collision_path
    )

    checks = {"metadata": _metadata_checks(metadata, issues), "usd": None}
    if usd_path:
        if "://" not in usd_path:
            _path_check(usd_path, label="usd", issues=issues)
        if "://" in usd_path:
            checks["usd"] = {
                "available": False,
                "source_path": usd_path,
                "error": "remote USD URI is outside offline validation scope",
            }
        elif not any(item["code"] == "missing_usd" for item in issues):
            checks["usd"] = _usd_checks(
                usd_path,
                collision_mode=record.collision_mode,
                collision_path=collision_path,
                issues=issues,
            )
    elif record.source_type not in {"primitive", "proxy"}:
        _issue(issues, "missing_usd", "non-primitive asset has no usd_path")

    if collision_path:
        _path_check(collision_path, label="collision", issues=issues)
    elif record.collision_mode == "authored" and not usd_path:
        _issue(issues, "missing_collision", "authored collision mode requires collision_path or usd_path")

    report = {
        "asset_id": record.asset_id,
        "name": metadata.name,
        "category": record.category,
        "status": record.status,
        "valid": not any(item["severity"] == "error" for item in issues),
        "checks": checks,
        "issues": issues,
        "paths": {"usd": usd_path, "collision": collision_path},
    }
    if report_path is not None:
        report["report_path"] = str(_write_report(report_path, report))
    return report


def validate_usd(
    source: str | Path,
    *,
    report_path: str | Path | None = None,
    asset_id: str | None = None,
    category: str = "unknown",
) -> dict[str, Any]:
    """Validate a standalone USD when no registry metadata is available."""
    path = str(Path(source).expanduser().resolve())
    issues: list[dict[str, str]] = []
    if not Path(path).is_file():
        _issue(issues, "missing_usd", f"usd does not exist: {path}")
        inspection: dict[str, Any] = {"available": False, "source_path": path}
    else:
        inspection = _usd_checks(
            path,
            collision_mode=None,
            collision_path=None,
            issues=issues,
        )
    report: dict[str, Any] = {
        "asset_id": asset_id or Path(path).stem,
        "category": category,
        "status": "inspection",
        "valid": not any(item["severity"] == "error" for item in issues),
        "checks": {"metadata": {}, "usd": inspection},
        "issues": issues,
        "paths": {"usd": path, "collision": None},
    }
    if report_path is not None:
        report["report_path"] = str(_write_report(report_path, report))
    return report


class AssetValidator:
    """Small object facade for applications that want a reusable validator."""

    def __init__(self, registry: AssetRegistry | None = None) -> None:
        self.registry = registry

    def validate(self, asset_id: str, *, report_path: str | Path | None = None) -> dict[str, Any]:
        if self.registry is None:
            raise ValueError("registry is required")
        return validate_asset(asset_id, self.registry, report_path=report_path)
