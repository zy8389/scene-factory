from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .exporters.isaac_usd import IsaacBackendUnavailable
from .factory import SceneFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scene-factory",
        description="Generate reproducible household simulation scenes.",
    )
    parser.add_argument("--registry", type=Path, help="Override asset registry JSONL")
    parser.add_argument("--recipes", type=Path, help="Override recipe directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-recipes", help="List available event recipes")
    subparsers.add_parser("llm-status", help="Show natural-language parser configuration")
    subparsers.add_parser("llm-test", help="Test the configured LLM with one structured request")

    build = subparsers.add_parser("build", help="Build one scene")
    source = build.add_mutually_exclusive_group(required=True)
    source.add_argument("--recipe")
    source.add_argument("--prompt")
    build.add_argument("--seed", type=int, default=42)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--usd", action="store_true", help="Export USD using Isaac Sim pxr")

    batch = subparsers.add_parser("batch", help="Build multiple deterministic scenes")
    source = batch.add_mutually_exclusive_group(required=True)
    source.add_argument("--recipe")
    source.add_argument("--prompt")
    batch.add_argument("--count", type=int, required=True)
    batch.add_argument("--seed-start", type=int, default=0)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--usd", action="store_true", help="Export USD using Isaac Sim pxr")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    factory = SceneFactory(args.registry, args.recipes)
    try:
        if args.command == "list-recipes":
            for name in factory.recipes.names():
                print(name)
            return 0

        if args.command == "llm-status":
            print(json.dumps(factory.llm_status(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "llm-test":
            print(
                json.dumps(
                    factory.test_llm_connection(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "build":
            result = (
                factory.build_from_recipe(args.recipe, args.seed)
                if args.recipe
                else factory.build_from_prompt(args.prompt, args.seed)
            )
            files = factory.write_result(result, args.output, export_usd=args.usd)
            print(
                json.dumps(
                    {
                        "scene_id": result.scene.scene_id,
                        "recipe": result.scene.recipe_name,
                        "valid": result.valid,
                        "files": files,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if result.valid else 2

        manifest = factory.build_batch(
            output_root=args.output,
            count=args.count,
            seed_start=args.seed_start,
            recipe_name=args.recipe,
            prompt=args.prompt,
            export_usd=args.usd,
        )
        valid_count = sum(item["valid"] for item in manifest)
        print(
            json.dumps(
                {"generated": len(manifest), "valid": valid_count, "output": str(args.output)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if valid_count == len(manifest) else 2
    except (KeyError, ValueError, OSError, RuntimeError, IsaacBackendUnavailable) as exc:
        print(f"scene-factory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
