"""Load the portable mesh contract used by the original kitchen/dining pack."""

from __future__ import annotations

import hashlib
import json
import math


def load_asset_parts(registry, asset):
    if "kitchen_dining_pack" not in asset.tags:
        return None
    folder = registry.base_dir / "source" / asset.asset_id
    manifest = json.loads((folder / "SOURCE.json").read_text(encoding="utf-8"))
    data = (folder / "geometry.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.get("geometry_sha256"):
        raise ValueError(f"geometry hash mismatch for {asset.asset_id}")
    parts = json.loads(data)["parts"]
    if not parts:
        raise ValueError(f"no geometry for {asset.asset_id}")
    for part in parts:
        vertices = part["vertices"]
        if not vertices or any(
            len(v) != 3 or not all(math.isfinite(x) for x in v) for v in vertices
        ):
            raise ValueError(f"invalid geometry vertices for {asset.asset_id}")
        if not part["faces"] or any(
            len(face) < 3
            or any(not isinstance(i, int) or i < 0 or i >= len(vertices) for i in face)
            for face in part["faces"]
        ):
            raise ValueError(f"invalid geometry faces for {asset.asset_id}")
    return parts
