from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from .models import AssetRecord


class AssetRegistry:
    def __init__(self, assets: list[AssetRecord], base_dir: str | Path | None = None) -> None:
        if not assets:
            raise ValueError("asset registry is empty")
        self._by_id: dict[str, AssetRecord] = {}
        self._by_category: dict[str, list[AssetRecord]] = defaultdict(list)
        self.base_dir = Path(base_dir or ".").expanduser().resolve()
        for asset in assets:
            if asset.asset_id in self._by_id:
                raise ValueError(f"duplicate asset ID: {asset.asset_id}")
            self._by_id[asset.asset_id] = asset
            if asset.status == "validated":
                self._by_category[asset.category].append(asset)

    @classmethod
    def load(cls, path: str | Path) -> "AssetRegistry":
        records: list[AssetRecord] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                try:
                    records.append(AssetRecord.from_dict(json.loads(stripped)))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid registry record at line {line_number}: {exc}") from exc
        return cls(records, base_dir=Path(path).resolve().parent)

    def get(self, asset_id: str) -> AssetRecord:
        try:
            return self._by_id[asset_id]
        except KeyError as exc:
            raise KeyError(f"unknown asset ID: {asset_id}") from exc

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
        if not asset.source_path:
            return None
        if "://" in asset.source_path:
            return asset.source_path
        source = Path(asset.source_path).expanduser()
        if not source.is_absolute():
            source = self.base_dir / source
        return str(source.resolve())

    def __len__(self) -> int:
        return len(self._by_id)
