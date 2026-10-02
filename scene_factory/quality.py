from __future__ import annotations

import math
from typing import Any, Mapping


QUALITY_SCHEMA_VERSION = "scene_factory.quality.v1"
QUALITY_LEVELS = ("layout", "physics", "task")
QUALITY_STATUSES = ("passed", "failed", "not_verified")
_ISSUE_CATEGORIES = {
    "out_of_bounds_x": "bounds",
    "out_of_bounds_y": "bounds",
    "out_of_bounds_z": "bounds",
    "overlap": "overlap",
    "missing_support": "support",
    "missing_surface": "support",
    "outside_support_surface": "support",
    "floating_or_sunk": "support",
    "layout_validation_failed": "layout",
}


def assess_scene_quality(
    layout: Mapping[str, Any], validation: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe recorded layout evidence without claiming simulator acceptance."""
    if not isinstance(layout, Mapping) or not isinstance(validation, Mapping):
        raise ValueError("layout and validation must be objects")
    scene_id = layout.get("scene_id")
    seed = layout.get("seed")
    if not isinstance(scene_id, str) or not scene_id:
        raise ValueError("quality requires a non-empty scene_id")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("quality requires an integer seed")
    valid = validation.get("valid")
    issues = validation.get("issues")
    metrics = validation.get("metrics")
    if not isinstance(valid, bool) or not isinstance(issues, (list, tuple)):
        raise ValueError("validation requires boolean valid and an issues array")
    if not isinstance(metrics, Mapping) or any(
        not isinstance(key, str)
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        for key, value in metrics.items()
    ):
        raise ValueError("validation metrics must contain finite numbers")

    findings: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise ValueError("validation issue must be an object")
        code = issue.get("code")
        message = issue.get("message")
        severity = issue.get("severity", "error")
        object_ids = issue.get("object_ids", [])
        if (
            not isinstance(code, str) or not code
            or not isinstance(message, str) or not message
            or severity not in ("error", "warning", "info")
            or not isinstance(object_ids, (list, tuple))
            or any(not isinstance(object_id, str) for object_id in object_ids)
        ):
            raise ValueError("invalid validation issue fields")
        findings.append({
            "code": code,
            "category": _ISSUE_CATEGORIES.get(code, "other"),
            "severity": severity,
            "message": message,
            "object_ids": list(object_ids),
        })
    has_errors = any(issue["severity"] == "error" for issue in findings)
    if valid and has_errors:
        raise ValueError("validation.valid contradicts error issues")
    if not valid and not has_errors:
        findings.append({
            "code": "layout_validation_failed",
            "category": "layout",
            "severity": "error",
            "message": "Layout validation failed without a recorded error reason.",
            "object_ids": [],
        })
    return {
        "schema_version": QUALITY_SCHEMA_VERSION,
        "scene_id": scene_id,
        "seed": seed,
        "highest_verified_level": "layout" if valid else "none",
        "layers": {
            "layout": {
                "status": "passed" if valid else "failed",
                "basis": "recorded_validation",
                "scope": ["room_bounds", "bounding_box_overlap", "support_placement"],
                "issues": findings,
                "metrics": dict(metrics),
            },
            "physics": {
                "status": "not_verified",
                "reason": "physics_not_run",
                "scope": ["collision_geometry", "settling", "dynamic_stability"],
            },
            "task": {
                "status": "not_verified",
                "reason": "task_not_run",
                "scope": ["robot_reachability", "physical_interaction", "task_success"],
            },
        },
    }


def quality_rejection_codes(report: Mapping[str, Any], minimum_level: str) -> list[str]:
    if minimum_level not in QUALITY_LEVELS:
        raise ValueError(f"unknown minimum quality level: {minimum_level!r}")
    reasons: set[str] = set()
    for level in QUALITY_LEVELS[:QUALITY_LEVELS.index(minimum_level) + 1]:
        layer = report["layers"][level]
        if layer["status"] == "passed":
            continue
        if layer["status"] == "not_verified":
            reasons.add(f"{level}_not_verified")
        else:
            reasons.update(
                issue["code"] for issue in layer.get("issues", [])
                if issue["severity"] == "error"
            )
            if not layer.get("issues"):
                reasons.add(f"{level}_validation_failed")
    return sorted(reasons)
