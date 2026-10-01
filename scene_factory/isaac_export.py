from __future__ import annotations

import argparse
import json
from pathlib import Path

from .exporters.isaac_usd import IsaacUsdExporter
from .models import CompiledScene
from .registry import AssetRegistry


def package_usd(source: Path, output: Path) -> None:
    from pxr import Sdf, Usd, UsdUtils

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in {".usd", ".usda", ".usdc"}:
        raise ValueError("USD package source must be a .usd, .usda, or .usdc layer")
    if output == source:
        raise ValueError("USD package output must differ from its source")
    if output.suffix.lower() != ".usdz":
        raise ValueError("USD package output must use the .usdz extension")
    if not Usd.Stage.Open(str(source)):
        raise RuntimeError(f"Cannot open USD scene: {source}")
    _, _, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(source)))
    if unresolved:
        raise RuntimeError(f"USD scene has unresolved dependencies: {sorted(unresolved)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    root_name = f"scene{source.suffix.lower()}"
    if not UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(source)), str(output), root_name):
        raise RuntimeError(f"Cannot create portable USD package: {output}")
    if not Usd.Stage.Open(str(output)):
        raise RuntimeError(f"Cannot open packaged USD scene: {output}")
    _, _, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(output)))
    if unresolved:
        raise RuntimeError(f"Packaged USD scene has unresolved dependencies: {sorted(unresolved)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export or package a scene with Isaac OpenUSD.")
    parser.add_argument("layout", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package", action="store_true")
    args = parser.parse_args(argv)
    if args.package:
        package_usd(args.layout, args.output)
    else:
        if args.registry is None:
            parser.error("--registry is required for layout export")
        scene = CompiledScene.from_dict(json.loads(args.layout.read_text(encoding="utf-8")))
        registry = AssetRegistry.load(args.registry)
        IsaacUsdExporter(registry).export(scene, args.output)
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
