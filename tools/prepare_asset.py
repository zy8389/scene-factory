from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.asset_pipeline import (
    AssetPipelineUnavailable,
    build_asset_record,
    build_drop_test_scene,
    inspect_usd,
    promote_asset_record,
    wrap_usd,
    write_json_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare local USD assets for SceneFactory without downloading anything."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Inspect one local USD")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("--report", type=Path)

    wrap = commands.add_parser("wrap", help="Create a normalized Z-up, meter-unit wrapper USD")
    wrap.add_argument("source", type=Path)
    wrap.add_argument("--output", type=Path, required=True)
    wrap.add_argument("--report", type=Path, required=True)
    wrap.add_argument("--asset-id", required=True)
    wrap.add_argument("--category", required=True)
    wrap.add_argument("--target-bbox", type=float, nargs=3, metavar=("X", "Y", "Z"))
    wrap.add_argument("--scale-mode", choices=("uniform", "exact"), default="uniform")
    wrap.add_argument(
        "--collision", choices=("proxy_box", "authored", "none"), default="proxy_box"
    )
    wrap.add_argument("--record", type=Path)
    wrap.add_argument("--mass-kg", type=float, default=1.0)
    wrap.add_argument("--friction", type=float, default=0.5)
    wrap.add_argument("--static-friction", type=float)
    wrap.add_argument("--dynamic-friction", type=float)
    wrap.add_argument("--rigid-body", action=argparse.BooleanOptionalAction, default=True)
    wrap.add_argument("--collision-enabled", action=argparse.BooleanOptionalAction)
    wrap.add_argument("--support-top", action="store_true")
    wrap.add_argument("--source-type", default="local_usd")
    wrap.add_argument("--license")

    drop = commands.add_parser("drop-scene", help="Build a PhysX drop-test scene")
    drop.add_argument("asset", type=Path)
    drop.add_argument("--output", type=Path, required=True)
    drop.add_argument("--report", type=Path, required=True)
    drop.add_argument("--mass-kg", type=float, required=True)
    drop.add_argument("--height", type=float, default=0.12)

    promote = commands.add_parser(
        "promote", help="Promote a quarantine record after a passing PhysX report"
    )
    promote.add_argument("record", type=Path)
    promote.add_argument("runtime_report", type=Path)
    promote.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_usd(args.source)
            if args.report:
                write_json_report(args.report, result)
        elif args.command == "wrap":
            result = wrap_usd(
                args.source,
                args.output,
                asset_id=args.asset_id,
                category=args.category,
                target_bbox_m=tuple(args.target_bbox) if args.target_bbox else None,
                scale_mode=args.scale_mode,
                collision_mode=args.collision,
            )
            write_json_report(args.report, result)
            if args.record:
                record = build_asset_record(
                    result,
                    source_path=args.output,
                    mass_kg=args.mass_kg,
                    friction=args.friction,
                    static_friction=args.static_friction,
                    dynamic_friction=args.dynamic_friction,
                    rigid_body=args.rigid_body,
                    collision_enabled=args.collision_enabled,
                    support_top=args.support_top,
                    source_type=args.source_type,
                    license_name=args.license,
                )
                write_json_report(args.record, record)
                result["record_path"] = str(args.record.resolve())
        elif args.command == "drop-scene":
            result = build_drop_test_scene(
                args.asset,
                args.output,
                mass_kg=args.mass_kg,
                drop_height_m=args.height,
            )
            write_json_report(args.report, result)
        else:
            result = promote_asset_record(
                args.record,
                args.runtime_report,
                output_path=args.output,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (AssetPipelineUnavailable, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"prepare-asset: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
