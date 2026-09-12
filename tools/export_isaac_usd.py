from __future__ import annotations

import argparse
import json
from pathlib import Path

from scene_factory.exporters.isaac_usd import IsaacUsdExporter
from scene_factory.models import CompiledScene
from scene_factory.registry import AssetRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Export one compiled layout with Isaac OpenUSD.")
    parser.add_argument("layout", type=Path)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    scene = CompiledScene.from_dict(json.loads(args.layout.read_text(encoding="utf-8")))
    registry = AssetRegistry.load(args.registry)
    IsaacUsdExporter(registry).export(scene, args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
