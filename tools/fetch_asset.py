from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scene_factory.asset_sources import AssetSourceResolver


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch one configured real asset source.")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/assets_batch.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    output = args.output or Path("data/assets/source") / args.asset_id
    report = AssetSourceResolver.from_config(config).fetch(
        args.asset_id,
        output,
        force=args.force,
        dry_run=args.dry_run,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("result") in {"passed", "dry_run"} or report.get("idempotent") else 2


if __name__ == "__main__":
    raise SystemExit(main())
