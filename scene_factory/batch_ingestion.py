from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .asset_profiles import collision_profile, validation_profile


BATCH_STATES = (
    "declared",
    "source_resolved",
    "downloaded",
    "raw",
    "converted",
    "normalized",
    "collision_ready",
    "validated",
    "ready",
    "blocked",
    "failed",
)


@dataclass
class BatchAssetResult:
    asset_id: str
    state: str = "declared"
    status: str = "declared"
    issues: list[dict[str, str]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    resumed: bool = False

    def transition(self, state: str, **details: Any) -> None:
        if state not in BATCH_STATES:
            raise ValueError(f"unknown batch state: {state}")
        if self.state not in {"blocked", "failed", "ready"} and state not in {"blocked", "failed"}:
            current_index = BATCH_STATES.index(self.state)
            next_index = BATCH_STATES.index(state)
            if next_index < current_index:
                raise ValueError(f"batch state cannot move backwards: {self.state} -> {state}")
        self.state = state
        self.status = state
        self.details.update(details)

    def block(self, code: str, message: str, **details: Any) -> None:
        self.issues.append({"code": code, "message": message})
        self.transition("blocked", **details)

    def fail(self, code: str, message: str, **details: Any) -> None:
        self.issues.append({"code": code, "message": message})
        self.transition("failed", **details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "state": self.state,
            "status": self.status,
            "resumed": self.resumed,
            "issues": list(self.issues),
            **self.details,
        }


@dataclass
class BatchReport:
    batch_id: str
    requested: int
    assets: dict[str, BatchAssetResult]

    @property
    def ready(self) -> int:
        return sum(result.state == "ready" for result in self.assets.values())

    @property
    def blocked(self) -> int:
        return sum(result.state == "blocked" for result in self.assets.values())

    @property
    def failed(self) -> int:
        return sum(result.state == "failed" for result in self.assets.values())

    @property
    def result(self) -> str:
        return "success" if self.ready == self.requested else "partial"

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "requested": self.requested,
            "ready": self.ready,
            "blocked": self.blocked,
            "failed": self.failed,
            "assets": {asset_id: result.to_dict() for asset_id, result in self.assets.items()},
            "result": self.result,
        }

    def write(self, path: str | Path) -> Path:
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)
        return output


def validate_batch_config(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    assets = config.get("assets")
    if not isinstance(assets, list) or not assets:
        return ["config.assets must be a non-empty list"]
    seen: set[str] = set()
    for item in assets:
        asset_id = str(item.get("asset_id", "")).strip() if isinstance(item, dict) else ""
        if not asset_id:
            issues.append("each asset requires asset_id")
            continue
        if asset_id in seen:
            issues.append(f"duplicate asset_id: {asset_id}")
        seen.add(asset_id)
        category = str(item.get("category", "")).strip()
        if not category:
            issues.append(f"{asset_id} requires category")
        try:
            collision_profile(str(item.get("collision_profile", "")))
            validation_profile(str(item.get("validation_profile", "")))
        except ValueError as exc:
            issues.append(f"{asset_id}: {exc}")
        physics = item.get("physics", {})
        if not isinstance(physics, dict) or float(physics.get("mass_kg", 0)) <= 0:
            issues.append(f"{asset_id} requires positive physics.mass_kg")
    return issues


def run_independent_batch(
    config: dict[str, Any],
    processor: Callable[[dict[str, Any], BatchAssetResult], BatchAssetResult],
    *,
    existing: dict[str, dict[str, Any]] | None = None,
    force: bool = False,
) -> BatchReport:
    issues = validate_batch_config(config)
    if issues:
        raise ValueError("invalid batch config: " + "; ".join(issues))
    results: dict[str, BatchAssetResult] = {}
    existing = existing or {}
    for item in config["assets"]:
        asset_id = str(item["asset_id"])
        prior = existing.get(asset_id, {})
        result = BatchAssetResult(asset_id)
        if not force and prior.get("state") == "ready":
            result.resumed = True
            result.transition(
                "ready",
                **{key: value for key, value in prior.items() if key not in {"state", "status"}},
            )
            results[asset_id] = result
            continue
        try:
            processed = processor(item, result)
            if processed is not result:
                result = processed
        except Exception as exc:  # one asset must not stop the remaining batch
            result.fail("asset_processing_failed", f"{type(exc).__name__}: {exc}")
        results[asset_id] = result
    return BatchReport(str(config.get("batch_id", "batch")), len(config["assets"]), results)
