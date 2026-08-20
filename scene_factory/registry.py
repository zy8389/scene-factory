from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import AssetRecord, SupportSurface


_VALID_STATUSES = {
    "raw",
    "normalized",
    "quarantine",  # legacy alias for normalized/imported assets
    "validated",
    "ready",
    "rejected",
}
_ACTIVE_STATUSES = {"validated", "ready"}
_COLLISION_STATUSES = {
    "not_provided",
    "pending",
    "authored",
    "provided",
    "validated",
    "rejected",
}
_STATUS_TRANSITIONS = {
    "raw": {"normalized", "rejected"},
    "normalized": {"validated", "rejected"},
    "quarantine": {"validated", "rejected"},
    "validated": {"ready", "rejected"},
    "ready": {"ready", "rejected"},
    "rejected": {"raw", "normalized"},
}


class RegistryValidationReport(dict[str, Any]):
    """Mapping report that also behaves like a boolean validity result."""

    def __bool__(self) -> bool:
        return bool(self.get("valid", False))


@dataclass(frozen=True)
class AssetMetadata:
    """Normalized Registry v2 metadata for one asset."""

    asset_id: str
    name: str
    category: str
    source: str | None = None
    license: str | None = None
    asset_hash: str | None = None
    usd_path: str | None = None
    collision_path: str | None = None
    collision_status: str = "not_provided"
    mass: float | None = None
    friction: float | None = None
    static_friction: float | None = None
    dynamic_friction: float | None = None
    rigid_body: bool = True
    collision_enabled: bool = True
    support_surface: tuple[SupportSurface, ...] = ()
    grasp_region: Any = None
    status: str = "quarantine"
    bbox_m: tuple[float, float, float] | None = None
    primitive: str | None = None
    color: tuple[float, float, float] | None = None
    source_type: str = "primitive"
    collision_mode: str = "primitive"
    qa_report_path: str | None = None
    qa_report: str | None = None
    tags: tuple[str, ...] = ()
    _record: AssetRecord | None = None

    @property
    def hash(self) -> str | None:
        return self.asset_hash

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AssetMetadata":
        if not isinstance(raw, dict):
            raise TypeError("asset record must be a JSON object")
        record = AssetRecord.from_dict(raw)
        status = str(raw.get("status", "validated"))
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported asset status: {status}")
        mass_raw = raw.get("mass", raw.get("mass_kg"))
        mass = None if mass_raw is None else float(mass_raw)
        friction_raw = raw.get("friction")
        friction = None if friction_raw is None else float(friction_raw)
        static_friction_raw = raw.get("static_friction", friction_raw)
        dynamic_friction_raw = raw.get("dynamic_friction", friction_raw)
        static_friction = (
            None if static_friction_raw is None else float(static_friction_raw)
        )
        dynamic_friction = (
            None if dynamic_friction_raw is None else float(dynamic_friction_raw)
        )
        if mass is not None and not math.isfinite(mass):
            raise ValueError("asset mass must be finite")
        if friction is not None and not math.isfinite(friction):
            raise ValueError("asset friction must be finite")
        for value, label in (
            (static_friction, "static_friction"),
            (dynamic_friction, "dynamic_friction"),
        ):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"asset {label} must be finite")
        bbox = record.bbox_m if "bbox_m" in raw else None
        surfaces = record.metadata_support_surface or record.support_surfaces
        return cls(
            asset_id=record.asset_id,
            name=str(raw.get("name", record.asset_id)),
            category=record.category,
            source=raw.get("source"),
            license=record.license,
            asset_hash=raw.get("hash", raw.get("asset_hash")),
            usd_path=raw.get("usd_path") or raw.get("source_path"),
            collision_path=raw.get("collision_path"),
            collision_status=str(
                raw.get(
                    "collision_status",
                    "provided" if raw.get("collision_path") else "not_provided",
                )
            ),
            mass=mass,
            friction=friction,
            static_friction=static_friction,
            dynamic_friction=dynamic_friction,
            rigid_body=bool(raw.get("rigid_body", True)),
            collision_enabled=bool(raw.get("collision_enabled", True)),
            support_surface=tuple(surfaces),
            grasp_region=raw.get("grasp_region"),
            status=status,
            bbox_m=bbox,
            primitive=record.primitive,
            color=record.color,
            source_type=record.source_type,
            collision_mode=record.collision_mode,
            qa_report_path=raw.get("qa_report", raw.get("qa_report_path")),
            qa_report=raw.get("qa_report", raw.get("qa_report_path")),
            tags=record.tags,
            _record=record,
        )

    @classmethod
    def from_record(cls, record: AssetRecord) -> "AssetMetadata":
        return cls(
            asset_id=record.asset_id,
            name=record.name or record.asset_id,
            category=record.category,
            source=record.source,
            license=record.license,
            asset_hash=record.asset_hash,
            usd_path=record.usd_path or record.source_path,
            collision_path=record.collision_path,
            collision_status=record.collision_status,
            mass=record.metadata_mass if record.metadata_mass is not None else record.mass_kg,
            friction=(
                record.metadata_friction
                if record.metadata_friction is not None
                else record.friction
            ),
            static_friction=(
                record.static_friction
                if record.static_friction is not None
                else record.friction
            ),
            dynamic_friction=(
                record.dynamic_friction
                if record.dynamic_friction is not None
                else record.friction
            ),
            rigid_body=record.rigid_body,
            collision_enabled=record.collision_enabled,
            support_surface=record.metadata_support_surface or record.support_surfaces,
            grasp_region=record.grasp_region,
            status=record.status,
            bbox_m=record.bbox_m,
            primitive=record.primitive,
            color=record.color,
            source_type=record.source_type,
            collision_mode=record.collision_mode,
            qa_report_path=record.qa_report_path,
            qa_report=record.qa_report,
            tags=record.tags,
            _record=record,
        )

    def to_record(self) -> AssetRecord:
        if self._record is not None:
            return self._record
        return AssetRecord(
            asset_id=self.asset_id,
            category=self.category,
            bbox_m=self.bbox_m or (1.0, 1.0, 1.0),
            primitive=self.primitive or "cube",
            color=self.color or (0.6, 0.6, 0.6),
            mass_kg=self.mass if self.mass is not None else 1.0,
            friction=self.friction if self.friction is not None else 0.5,
            support_surfaces=self.support_surface,
            source_path=self.usd_path,
            source_type=self.source_type,
            collision_mode=self.collision_mode,
            qa_report_path=self.qa_report_path,
            license=self.license,
            status=self.status,
            tags=self.tags,
            name=self.name,
            asset_hash=self.asset_hash,
            usd_path=self.usd_path,
            collision_path=self.collision_path,
            collision_status=self.collision_status,
            grasp_region=self.grasp_region,
            source=self.source,
            metadata_mass=self.mass,
            metadata_friction=self.friction,
            static_friction=self.static_friction,
            dynamic_friction=self.dynamic_friction,
            rigid_body=self.rigid_body,
            collision_enabled=self.collision_enabled,
            qa_report=self.qa_report or self.qa_report_path,
            metadata_support_surface=self.support_surface,
            metadata_present=True,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "asset_id": self.asset_id,
            "name": self.name,
            "category": self.category,
            "source": self.source,
            "license": self.license,
            "hash": self.asset_hash,
            "usd_path": self.usd_path,
            "collision_path": self.collision_path,
            "collision_status": self.collision_status,
            "mass": self.mass,
            "friction": self.friction,
            "static_friction": self.static_friction,
            "dynamic_friction": self.dynamic_friction,
            "rigid_body": self.rigid_body,
            "collision_enabled": self.collision_enabled,
            "support_surface": [asdict(item) for item in self.support_surface],
            "grasp_region": self.grasp_region,
            "status": self.status,
            "qa_report": self.qa_report or self.qa_report_path,
            "primitive": self.primitive,
            "color": list(self.color) if self.color is not None else None,
            "source_type": self.source_type,
            "collision_mode": self.collision_mode,
            "tags": list(self.tags),
        }
        if self.bbox_m is not None:
            payload["bbox_m"] = list(self.bbox_m)
        return payload


class AssetRegistry:
    def __init__(
        self,
        assets: list[AssetRecord],
        base_dir: str | Path | None = None,
        metadata: Iterable[AssetMetadata] | None = None,
    ) -> None:
        if not assets:
            raise ValueError("asset registry is empty")
        self._by_id: dict[str, AssetRecord] = {}
        self._metadata: dict[str, AssetMetadata] = {}
        self._by_category: dict[str, list[AssetRecord]] = defaultdict(list)
        self.base_dir = Path(base_dir or ".").expanduser().resolve()
        self.registry_path: Path | None = None
        metadata_by_id = {item.asset_id: item for item in (metadata or ())}
        for asset in assets:
            if asset.asset_id in self._by_id:
                raise ValueError(f"duplicate asset ID: {asset.asset_id}")
            self._by_id[asset.asset_id] = asset
            self._metadata[asset.asset_id] = metadata_by_id.get(
                asset.asset_id, AssetMetadata.from_record(asset)
            )
            if asset.status in _ACTIVE_STATUSES:
                self._by_category[asset.category].append(asset)

    @classmethod
    def load(cls, path: str | Path) -> "AssetRegistry":
        records: list[AssetRecord] = []
        metadata: list[AssetMetadata] = []
        registry_path = Path(path).expanduser().resolve()
        with registry_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    raw = json.loads(stripped)
                    item = AssetMetadata.from_dict(raw)
                    records.append(item.to_record())
                    metadata.append(item)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"invalid registry record at line {line_number}: {exc}"
                    ) from exc
        registry = cls(records, base_dir=registry_path.parent, metadata=metadata)
        registry.registry_path = registry_path
        return registry

    def get(self, asset_id: str) -> AssetRecord:
        try:
            return self._by_id[asset_id]
        except KeyError as exc:
            raise KeyError(f"unknown asset ID: {asset_id}") from exc

    def metadata(self, asset_id: str) -> AssetMetadata:
        try:
            return self._metadata[asset_id]
        except KeyError as exc:
            raise KeyError(f"unknown asset ID: {asset_id}") from exc

    def list(
        self,
        *,
        category: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[AssetRecord]:
        allowed = set(statuses) if statuses is not None else None
        return [
            asset
            for asset in self._by_id.values()
            if (category is None or asset.category == category)
            and (allowed is None or asset.status in allowed)
        ]

    def choose(self, category: str, rng: random.Random) -> AssetRecord:
        candidates = self._by_category.get(category, [])
        if not candidates:
            raise KeyError(f"no validated asset for category: {category}")
        return candidates[rng.randrange(len(candidates))]

    def resolve(self, category: str, asset_id: str | None, rng: random.Random) -> AssetRecord:
        asset = self.get(asset_id) if asset_id else self.choose(category, rng)
        if asset.category != category:
            raise ValueError(
                f"asset {asset.asset_id} has category {asset.category}, expected {category}"
            )
        if asset.status not in _ACTIVE_STATUSES:
            raise ValueError(
                f"asset {asset.asset_id} has status {asset.status}, not ready for scenes"
            )
        return asset

    def categories(self) -> list[str]:
        return sorted(self._by_category)

    def candidates(self, category: str) -> tuple[AssetRecord, ...]:
        return tuple(self._by_category.get(category, ()))

    def resolve_source_path(self, asset: AssetRecord) -> str | None:
        return self._resolve_path(asset.usd_path or asset.source_path)

    def resolve_collision_path(self, asset: AssetRecord) -> str | None:
        return self._resolve_path(asset.collision_path)

    def _resolve_path(self, value: str | None) -> str | None:
        if not value:
            return None
        if "://" in value:
            return value
        source = Path(value).expanduser()
        if not source.is_absolute():
            source = self.base_dir / source
        return str(source.resolve())

    def validate(self) -> dict[str, Any]:
        """Validate registry metadata without requiring Isaac Sim or opening USD."""
        issues: list[dict[str, str]] = []
        for asset in self._by_id.values():
            metadata = self._metadata[asset.asset_id]
            if asset.status not in _VALID_STATUSES:
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "invalid_status",
                        "message": asset.status,
                    }
                )
            if not metadata.category.strip():
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "missing_category",
                        "message": "category is required",
                    }
                )
            if metadata.mass is None or metadata.mass <= 0:
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "invalid_mass",
                        "message": "mass must be positive",
                    }
                )
            if metadata.friction is None or metadata.friction < 0:
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "invalid_friction",
                        "message": "friction must be non-negative",
                    }
                )
            for value, label in (
                (metadata.static_friction, "static_friction"),
                (metadata.dynamic_friction, "dynamic_friction"),
            ):
                if value is not None and (not math.isfinite(value) or value < 0):
                    issues.append(
                        {
                            "asset_id": asset.asset_id,
                            "code": f"invalid_{label}",
                            "message": f"{label} must be finite and non-negative",
                        }
                    )
            if metadata.collision_status not in _COLLISION_STATUSES:
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "invalid_collision_status",
                        "message": metadata.collision_status,
                    }
                )
            if metadata.bbox_m is None and not metadata.usd_path:
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "missing_bbox",
                        "message": "bbox_m or usd_path is required",
                    }
                )
            usd_path = self.resolve_source_path(asset)
            if usd_path and "://" not in usd_path and not Path(usd_path).is_file():
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "missing_usd",
                        "message": f"usd_path does not exist: {usd_path}",
                    }
                )
            collision_path = self.resolve_collision_path(asset)
            if collision_path and "://" not in collision_path and not Path(collision_path).is_file():
                issues.append(
                    {
                        "asset_id": asset.asset_id,
                        "code": "missing_collision",
                        "message": f"collision_path does not exist: {collision_path}",
                    }
                )
        return RegistryValidationReport({
            "valid": not issues,
            "asset_count": len(self),
            "validated_count": sum(
                asset.status == "validated" for asset in self._by_id.values()
            ),
            "ready_count": sum(
                asset.status in _ACTIVE_STATUSES for asset in self._by_id.values()
            ),
            "issues": issues,
        })

    def update(
        self,
        asset_id: str,
        updates: dict[str, Any] | None = None,
        *,
        persist_path: str | Path | None = None,
        **changes: Any,
    ) -> AssetMetadata:
        """Update one record and optionally persist the JSONL registry.

        This is intentionally metadata-only: it never copies or synthesizes a
        USD/collision file.  A caller must provide paths to existing assets.
        """
        raw = self.metadata(asset_id).to_dict()
        raw.update(updates or {})
        raw.update(changes)
        replacement = AssetMetadata.from_dict(raw)
        self._by_id[asset_id] = replacement.to_record()
        self._metadata[asset_id] = replacement
        self._rebuild_categories()
        if persist_path is not None:
            self.save(persist_path)
        return replacement

    def transition_status(
        self,
        asset_id: str,
        status: str,
        *,
        persist_path: str | Path | None = None,
    ) -> AssetMetadata:
        """Apply an explicit Registry lifecycle transition."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported asset status: {status}")
        current = self.metadata(asset_id).status
        if status not in _STATUS_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid asset status transition: {current} -> {status}")
        return self.update(asset_id, {"status": status}, persist_path=persist_path)

    @staticmethod
    def _read_report(report: str | Path | dict[str, Any]) -> dict[str, Any]:
        if isinstance(report, dict):
            return report
        path = Path(report).expanduser().resolve()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read QA report: {path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"QA report must be a JSON object: {path}")
        return payload

    def promote_to_validated(
        self,
        asset_id: str,
        normalize_report: str | Path | dict[str, Any],
        *,
        collision_report: str | Path | dict[str, Any] | None = None,
        persist_path: str | Path | None = None,
    ) -> AssetMetadata:
        """Promote a normalized asset after non-physics QA has passed."""
        current = self.metadata(asset_id).status
        if current not in {"normalized", "quarantine"}:
            raise ValueError(f"asset must be normalized before validated: {current}")
        normalized = self._read_report(normalize_report)
        if normalized.get("asset_id") not in {None, asset_id}:
            raise ValueError("normalize report asset_id does not match registry asset")
        if not normalized.get("valid"):
            raise ValueError("normalize report did not pass")
        updates: dict[str, Any] = {"status": "validated"}
        if isinstance(normalize_report, (str, Path)):
            updates["qa_report"] = str(Path(normalize_report).expanduser().resolve())
        if collision_report is not None:
            collision = self._read_report(collision_report)
            if not collision.get("valid") or collision.get("generated"):
                raise ValueError("collision report did not pass authored-collision checks")
            updates.update(
                {
                    "collision_path": collision.get("collision_path"),
                    "collision_status": collision.get("collision_status", "validated"),
                    "collision_enabled": bool(collision.get("collision_enabled", True)),
                }
            )
        return self.update(asset_id, updates, persist_path=persist_path)

    def promote_to_ready(
        self,
        asset_id: str,
        qa_report: str | Path | dict[str, Any],
        *,
        persist_path: str | Path | None = None,
    ) -> AssetMetadata:
        """Promote a validated asset only after the Isaac/PhysX QA passes."""
        report = self._read_report(qa_report)
        if report.get("asset_id") not in {None, asset_id}:
            raise ValueError("QA report asset_id does not match registry asset")
        if not report.get("valid"):
            raise ValueError("QA report did not pass")
        required_stages = ("usd_load", "collision", "physics")
        if any(report.get(stage) != "passed" for stage in required_stages):
            raise ValueError("QA report must pass usd_load, collision, and physics")
        current = self.metadata(asset_id)
        if current.status != "validated":
            raise ValueError(f"asset must be validated before ready: {current.status}")
        if current.collision_enabled and not current.collision_path:
            raise ValueError("ready asset with collision_enabled requires collision_path")
        updates: dict[str, Any] = {"status": "ready"}
        if isinstance(qa_report, (str, Path)):
            updates["qa_report"] = str(Path(qa_report).expanduser().resolve())
        return self.update(asset_id, updates, persist_path=persist_path)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path or self.registry_path or "registry.jsonl").expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for asset_id in self._by_id:
                handle.write(
                    json.dumps(
                        self._metadata[asset_id].to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
        self.registry_path = target
        return target

    def _rebuild_categories(self) -> None:
        self._by_category = defaultdict(list)
        for asset in self._by_id.values():
            if asset.status in _ACTIVE_STATUSES:
                self._by_category[asset.category].append(asset)

    def __len__(self) -> int:
        return len(self._by_id)


class AssetLoader:
    """Resolve and load one registry asset without importing ``pxr`` eagerly."""

    def __init__(self, registry: AssetRegistry) -> None:
        self.registry = registry

    def resolve_usd_path(self, asset: str | AssetRecord) -> str | None:
        return self.registry.resolve_source_path(self._record(asset))

    def resolve_collision_path(self, asset: str | AssetRecord) -> str | None:
        return self.registry.resolve_collision_path(self._record(asset))

    def load(
        self,
        asset: str | AssetRecord,
        *,
        require_collision: bool = False,
    ) -> AssetRecord:
        """Resolve an asset and verify local USD/collision files when declared."""
        record = self._record(asset)
        usd_path = self.resolve_usd_path(record)
        if usd_path and "://" not in usd_path and not Path(usd_path).is_file():
            raise FileNotFoundError(
                f"USD source for asset {record.asset_id} does not exist: {usd_path}"
            )
        collision_path = self.resolve_collision_path(record)
        if require_collision and not collision_path:
            raise FileNotFoundError(f"asset {record.asset_id} has no collision_path")
        if collision_path and "://" not in collision_path and not Path(collision_path).is_file():
            raise FileNotFoundError(
                f"collision mesh for asset {record.asset_id} does not exist: {collision_path}"
            )
        return record

    def _record(self, asset: str | AssetRecord) -> AssetRecord:
        return self.registry.get(asset) if isinstance(asset, str) else asset
