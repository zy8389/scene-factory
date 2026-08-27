from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .asset_pipeline import AssetNormalizer, CollisionProcessor
from .asset_validator import validate_asset, validate_usd
from .dataset import inspect_dataset, reproduce_dataset, validate_dataset
from .external import ExternalSceneError, external_scene_schema, load_external_scene
from .exporters.isaac_usd import IsaacBackendUnavailable
from .factory import SceneFactory
from .registry import AssetRegistry


def _intent_report(document) -> dict[str, object]:
    intent = document.intent
    return {
        **document.to_dict(),
        "room_type": intent.room_type,
        "event": intent.event,
        "description": intent.description,
        "object_count": len(intent.objects),
        "relation_count": len(intent.relations),
        "categories": sorted({item.category for item in intent.objects}),
        "relation_predicates": sorted({item.predicate for item in intent.relations}),
        "room_dimensions_m": intent.room_dimensions_m,
        "clutter_level": intent.clutter_level,
        "layout_style": intent.layout_style,
    }


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

    intent = subparsers.add_parser("intent", help="Validate and inspect external SceneIntent JSON")
    intent_commands = intent.add_subparsers(dest="intent_command", required=True)
    intent_validate = intent_commands.add_parser("validate", help="Validate an external SceneIntent")
    intent_validate.add_argument("path", type=str)
    intent_inspect = intent_commands.add_parser("inspect", help="Inspect a normalized SceneIntent")
    intent_inspect.add_argument("path", type=str)
    intent_commands.add_parser("schema", help="Print the canonical SceneIntent JSON Schema")

    build = subparsers.add_parser("build", help="Build one scene")
    source = build.add_mutually_exclusive_group(required=True)
    source.add_argument("--recipe")
    source.add_argument("--prompt")
    source.add_argument("--intent", help="External SceneIntent JSON path, or - for stdin")
    build.add_argument("--seed", type=int, default=42)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--usd", action="store_true", help="Export USD using Isaac Sim pxr")

    batch = subparsers.add_parser("batch", help="Build multiple deterministic scenes")
    source = batch.add_mutually_exclusive_group(required=True)
    source.add_argument("--recipe")
    source.add_argument("--prompt")
    source.add_argument("--intent", help="External SceneIntent JSON path, or - for stdin")
    batch.add_argument("--count", type=int, required=True)
    batch.add_argument("--seed-start", type=int, default=0)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--usd", action="store_true", help="Export USD using Isaac Sim pxr")
    batch.add_argument("--resume", action="store_true", help="Resume an incomplete dataset")

    dataset = subparsers.add_parser("dataset", help="Inspect and validate generated datasets")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    for name, help_text in (
        ("inspect", "Inspect dataset metadata and manifest"),
        ("validate", "Validate dataset integrity and completeness"),
        ("reproduce", "Rebuild recipe scenes and compare fingerprints"),
    ):
        command = dataset_commands.add_parser(name, help=help_text)
        command.add_argument("path", type=Path)

    asset = subparsers.add_parser("asset", help="Inspect and validate registered assets")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    inspect = asset_commands.add_parser("inspect", help="Run asset metadata and USD QA")
    inspect.add_argument(
        "--registry",
        dest="asset_registry",
        type=Path,
        help="Asset registry JSONL (defaults to the project registry)",
    )
    inspect_source = inspect.add_mutually_exclusive_group(required=True)
    inspect_source.add_argument("--asset-id", help="Registry asset ID to inspect")
    inspect_source.add_argument("--usd", type=Path, help="Inspect a standalone local USD")
    inspect.add_argument("--report", type=Path, help="Write the JSON QA report")

    normalize = asset_commands.add_parser(
        "normalize", help="Normalize one real USD without creating collision geometry"
    )
    normalize.add_argument("source", type=Path)
    normalize.add_argument("--output", type=Path, required=True)
    normalize.add_argument("--asset-id", required=True)
    normalize.add_argument("--category", required=True)
    normalize.add_argument("--target-bbox", type=float, nargs=3, metavar=("X", "Y", "Z"))
    normalize.add_argument("--scale-mode", choices=("uniform", "exact"), default="uniform")
    normalize.add_argument("--report", type=Path)

    collision = asset_commands.add_parser(
        "collision", help="Validate an authored collision file without generating one"
    )
    collision.add_argument("--collision-path", type=Path)
    collision.add_argument("--status")
    collision.add_argument("--enabled", action="store_true")
    collision.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "asset":
            if args.asset_command == "normalize":
                report = AssetNormalizer().normalize(
                    args.source,
                    args.output,
                    asset_id=args.asset_id,
                    category=args.category,
                    target_bbox_m=tuple(args.target_bbox) if args.target_bbox else None,
                    scale_mode=args.scale_mode,
                    report_path=args.report,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["valid"] else 2

            if args.asset_command == "collision":
                report = CollisionProcessor().process(
                    args.collision_path,
                    collision_status=args.status,
                    collision_enabled=args.enabled,
                    report_path=args.report,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report["valid"] else 2

            report_path = args.report
            if args.asset_id:
                registry_path = args.asset_registry or args.registry
                default_registry = (
                    Path(__file__).resolve().parents[1]
                    / "data"
                    / "assets"
                    / "registry.jsonl"
                )
                registry = AssetRegistry.load(registry_path or default_registry)
                report = validate_asset(args.asset_id, registry, report_path=report_path)
            else:
                report = validate_usd(args.usd, report_path=report_path)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["valid"] else 2

        if args.command == "dataset":
            handler = {
                "inspect": inspect_dataset,
                "validate": validate_dataset,
                "reproduce": reproduce_dataset,
            }[args.dataset_command]
            report = handler(args.path)
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            if args.dataset_command == "inspect":
                return 0
            return 0 if report["valid"] else 2

        factory = SceneFactory(args.registry, args.recipes)
        if args.command == "intent":
            if args.intent_command == "schema":
                print(
                    json.dumps(
                        external_scene_schema(
                            factory.registry.categories(),
                            factory.recipes.room_types(),
                            factory.recipes.events(),
                        ),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            try:
                document = load_external_scene(
                    args.path,
                    allowed_categories=factory.registry.categories(),
                    allowed_room_types=factory.recipes.room_types(),
                    allowed_events=factory.recipes.events(),
                )
            except ExternalSceneError as exc:
                print(f"scene-factory: {exc}", file=sys.stderr)
                return 2
            if args.intent_command == "validate":
                print(
                    json.dumps(
                        {"result": "passed", "valid": True, **_intent_report(document)},
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            print(json.dumps(_intent_report(document), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
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
            if args.recipe:
                result = factory.build_from_recipe(args.recipe, args.seed)
            elif args.prompt is not None:
                result = factory.build_from_prompt(args.prompt, args.seed)
            else:
                document = load_external_scene(
                    args.intent,
                    allowed_categories=factory.registry.categories(),
                    allowed_room_types=factory.recipes.room_types(),
                    allowed_events=factory.recipes.events(),
                )
                result = factory.build_from_intent(
                    document.intent,
                    args.seed,
                    input_source=document.input_source,
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

        external_document = None
        if args.intent is not None:
            external_document = load_external_scene(
                args.intent,
                allowed_categories=factory.registry.categories(),
                allowed_room_types=factory.recipes.room_types(),
                allowed_events=factory.recipes.events(),
            )
        manifest = factory.build_batch(
            output_root=args.output,
            count=args.count,
            seed_start=args.seed_start,
            recipe_name=args.recipe,
            prompt=args.prompt,
            intent=external_document.intent if external_document else None,
            input_source=external_document.input_source if external_document else None,
            export_usd=args.usd,
            resume=args.resume,
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
    except ExternalSceneError as exc:
        print(f"scene-factory: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError, OSError, RuntimeError, IsaacBackendUnavailable) as exc:
        print(f"scene-factory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
