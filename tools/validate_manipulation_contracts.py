"""Check that ready kitchen task assets expose a usable grasp contract.

This is intentionally an offline metadata check.  It does not claim that a
robot can execute the grasp; it verifies that a planner has a bounded region,
an approach direction, and an explicit action set to work from.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scene_factory.registry import AssetRegistry


DEFAULT_ASSETS = ("kd_bowl_01", "kd_plate_01", "kd_bottle_01", "kd_box_01")
ALLOWED_ACTIONS = {"grasp", "lift", "place"}


def _validate(asset_id: str, contract: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(contract, dict):
        return [f"{asset_id}: grasp_region must be an object"]
    for key in ("type", "center", "size", "approach_axis", "allowed_actions"):
        if key not in contract:
            errors.append(f"{asset_id}: grasp_region missing {key}")
    for key in ("center", "size", "approach_axis"):
        value = contract.get(key)
        if not isinstance(value, list) or len(value) != 3 or not all(
            isinstance(item, (int, float)) for item in value
        ):
            errors.append(f"{asset_id}: {key} must be a numeric vec3")
    size = contract.get("size")
    if isinstance(size, list) and len(size) == 3 and any(item <= 0 for item in size):
        errors.append(f"{asset_id}: grasp_region.size must be positive")
    actions = contract.get("allowed_actions")
    if not isinstance(actions, list) or not actions or not set(actions) <= ALLOWED_ACTIONS:
        errors.append(f"{asset_id}: allowed_actions must use {sorted(ALLOWED_ACTIONS)}")
    axis = contract.get("approach_axis")
    if isinstance(axis, list) and len(axis) == 3 and all(float(item) == 0 for item in axis):
        errors.append(f"{asset_id}: approach_axis cannot be zero")
    return errors


def validate_contracts(registry_path: Path, asset_ids: tuple[str, ...]) -> dict[str, Any]:
    registry = AssetRegistry.load(registry_path)
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        asset = registry.get(asset_id)
        asset_errors = []
        if asset.status != "ready":
            asset_errors.append(f"{asset_id}: status is {asset.status}, expected ready")
        if not asset.collision_enabled or asset.collision_status not in {"validated", "authored", "provided"}:
            asset_errors.append(f"{asset_id}: collision is not validated")
        asset_errors.extend(_validate(asset_id, asset.grasp_region))
        errors.extend(asset_errors)
        results.append({"asset_id": asset_id, "status": "pass" if not asset_errors else "fail", "errors": asset_errors})
    return {"valid": not errors, "assets": results, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("data/assets/registry.jsonl"))
    parser.add_argument("--asset-id", action="append", dest="asset_ids")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate_contracts(args.registry, tuple(args.asset_ids or DEFAULT_ASSETS))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
