from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import AssetRecord, SupportSurface


_VALID_STATUSES = {"quarantine", "validated", "rejected"}


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
    mass: float | None = None
    friction: float | None = None
    support_surface: tuple[SupportSurface, ...] = ()
    grasp_region: Any = None
    status: str = "quarantine"
    bbox_m: tuple[float, float, float] | None = None
    primitive: str | None = None
    source_type: str = "primitive"
    collision_mode: str = "primitive"
    qa_report_path: str | None = None
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
        if mass is not None and not math.isfinite(mass):
            raise ValueError("asset mass must be finite")
        if friction is not None and not math.isfinite(friction):
            raise ValueError("asset friction must be finite")
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
            mass=mass,
            friction=friction,
            support_surface=tuple(surfaces),
            grasp_region=raw.get("grasp_region"),
            status=status,
            bbox_m=bbox,
            primitive=record.primitive,
            source_type=record.source_type,
            collision_mode=record.collision_mode,
            qa_report_path=record.qa_report_path,
            tags=record.tags,
            _record=record,
        )

    @classmethod
    def from_record(cls, record: AssetRecord) -> "AssetMetadata":
        return cls(
            asset_id=record.asset_id,
            name=record.name or record.asset_id,
            category=record.category,
            license=record.license,
            asset_hash=record.asset_hash,
            usd_path=record.usd_path or record.source_path,
            collision_path=record.collision_path,
            mass=record.metadata_mass if record.metadata_mass is not None else record.mass_kg,
            friction=(
                record.metadata_friction
                if record.metadata_friction is not None
                else record.friction
            ),
            support_surface=record.metadata_support_surface or record.support_surfaces,
            grasp_region=record.grasp_region,
            status=record.status,
            bbox_m=record.bbox_m,
            primitive=record.primitive,
            source_type=record.source_type,
            collision_mode=record.collision_mode,
            qa_report_path=record.qa_report_path,
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
            grasp_region=self.grasp_region,
            source=self.source,
            metadata_mass=self.mass,
            metadata_friction=self.friction,
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
            "mass": self.mass,
            "friction": self.friction,
            "support_surface": [asdict(item) for item in self.support_surface],
            "grasp_region": self.grasp_region,
            "status": self.status,
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
        metadata_by_id = {item.asset_id: item for item in (metadata or ())}
        for asset in assets:
            if asset.asset_id in self._by_id:
                raise ValueError(f"duplicate asset ID: {asset.asset_id}")
            self._by_id[asset.asset_id] = asset
            self._metadata[asset.asset_id] = metadata_by_id.get(
                asset.asset_id, AssetMetadata.from_record(asset)
            )
            if asset.status == "validated":
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
        return cls(records, base_dir=registry_path.parent, metadata=metadata)

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
        if asset.status != "validated":
            raise ValueError(f"asset {asset.asset_id} has status {asset.status}, not validated")
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
            "issues": issues,
        })

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
