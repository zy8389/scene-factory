from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dataset import (
    clean_known_staging,
    load_dataset_snapshot,
    make_dataset_metadata,
    make_manifest_record,
    update_dataset_metadata,
    write_json_atomic,
    write_manifest_atomic,
    write_staging_marker,
)
from .intent import SceneIntent
from .intent_compiler import IntentCompiler
from .layout import LayoutSolver
from .llm import IntentParser, create_intent_parser_from_env, load_llm_settings
from .models import CompiledScene, SceneRecipe, ValidationReport
from .paths import default_recipes_dir, default_registry_path
from .recipes import RecipeLibrary
from .registry import AssetRegistry
from .validation import SceneValidator


@dataclass(frozen=True)
class BuildResult:
    recipe: SceneRecipe
    scene: CompiledScene
    validation: ValidationReport
    intent: SceneIntent | None = None
    prompt_parser: str = "recipe"
    parser_warning: str | None = None
    revision_of: str | None = None
    revision_instruction: str | None = None

    @property
    def valid(self) -> bool:
        return self.validation.valid


class SceneFactory:
    def __init__(
        self,
        registry_path: str | Path | None = None,
        recipes_dir: str | Path | None = None,
        intent_parser: IntentParser | None = None,
    ) -> None:
        self.registry_path = Path(registry_path or default_registry_path())
        self.recipes_dir = Path(recipes_dir or default_recipes_dir())
        self.registry = AssetRegistry.load(self.registry_path)
        self.recipes = RecipeLibrary.load(self.recipes_dir)
        self.layout_solver = LayoutSolver(self.registry)
        self.validator = SceneValidator(self.registry)
        self.intent_compiler = IntentCompiler(self.registry, self.recipes)
        self.llm_settings = load_llm_settings()
        self.llm_required = self.llm_settings["mode"] == "required"
        self.intent_parser = intent_parser or create_intent_parser_from_env(
            categories=self.registry.categories(),
            room_types=self.recipes.room_types(),
            events=self.recipes.events(),
            settings=self.llm_settings,
        )

    @property
    def prompt_parser_mode(self) -> str:
        return self.intent_parser.name if self.intent_parser else "keyword"

    def llm_status(self) -> dict[str, Any]:
        base_url = str(self.llm_settings["base_url"])
        endpoint = (
            base_url.rstrip("/")
            if base_url.rstrip("/").endswith("/chat/completions")
            else f"{base_url.rstrip('/')}/chat/completions"
        ) if base_url else ""
        return {
            "mode": self.llm_settings["mode"],
            "active": self.intent_parser is not None,
            "configured": bool(base_url and self.llm_settings["model"]),
            "parser": self.prompt_parser_mode,
            "endpoint": endpoint,
            "model": self.llm_settings["model"],
            "api_key_env": self.llm_settings["api_key_env"],
            "api_key_configured": bool(self.llm_settings["api_key"]),
            "timeout_seconds": self.llm_settings["timeout_seconds"],
            "cache_dir": str(self.llm_settings["cache_dir"]),
            "ca_bundle": str(self.llm_settings["ca_bundle"]),
            "transport": self.llm_settings["transport"],
            "proxy_url": self.llm_settings["proxy_url"],
            "config_path": str(self.llm_settings["config_path"]),
            "config_file_exists": self.llm_settings["config_file_exists"],
            "fallback_policy": "error" if self.llm_required else "keyword",
        }

    def test_llm_connection(self) -> dict[str, Any]:
        if self.intent_parser is None:
            raise ValueError(
                "LLM 尚未启用：请填写 config/llm.json 的 base_url 和 model，"
                "密钥放入 SCENE_FACTORY_LLM_API_KEY 环境变量，然后重启服务"
            )
        tester = getattr(self.intent_parser, "test_connection", None)
        if not callable(tester):
            raise RuntimeError("current intent parser does not support connection testing")
        return tester()

    def build_from_recipe(self, recipe_name: str, seed: int) -> BuildResult:
        recipe = self.recipes.get(recipe_name)
        scene = self.layout_solver.compile(recipe, seed)
        return BuildResult(recipe, scene, self.validator.validate(scene))

    def build_from_prompt(self, prompt: str, seed: int) -> BuildResult:
        parser_warning = None
        if self.intent_parser is not None:
            try:
                intent = self.intent_parser.parse(prompt)
                recipe = self.intent_compiler.compile(intent, prompt)
                scene = self.layout_solver.compile(recipe, seed, description_override=prompt)
                return BuildResult(
                    recipe,
                    scene,
                    self.validator.validate(scene),
                    intent=intent,
                    prompt_parser=self.intent_parser.name,
                )
            except (KeyError, OSError, RuntimeError, ValueError) as exc:
                if self.llm_required:
                    raise RuntimeError(f"required LLM scene parsing failed: {exc}") from exc
                parser_warning = f"{type(exc).__name__}: {exc}"[:500]
        recipe = self.recipes.match_prompt(prompt)
        scene = self.layout_solver.compile(recipe, seed, description_override=prompt)
        parser = "keyword_fallback" if self.intent_parser else "keyword"
        return BuildResult(
            recipe,
            scene,
            self.validator.validate(scene),
            prompt_parser=parser,
            parser_warning=parser_warning,
        )

    def build_from_intent_revision(
        self,
        current_intent: SceneIntent,
        instruction: str,
        seed: int,
        *,
        source_scene_id: str,
    ) -> BuildResult:
        """Create a new scene version by revising an existing semantic intent."""
        normalized = instruction.strip()
        if len(normalized) < 2:
            raise ValueError("请至少输入 2 个字的修改要求")
        if len(normalized) > 1000:
            raise ValueError("修改要求不能超过 1000 个字")
        if self.intent_parser is None:
            raise RuntimeError("继续修改场景需要启用 LLM")
        reviser = getattr(self.intent_parser, "revise", None)
        if not callable(reviser):
            raise RuntimeError("当前 LLM 解析器不支持场景增量修改")

        try:
            revised_intent = reviser(current_intent, normalized)
            recipe = self.intent_compiler.compile(
                revised_intent, revised_intent.description
            )
            scene = self.layout_solver.compile(
                recipe,
                seed,
                description_override=revised_intent.description,
            )
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"LLM 场景修改失败: {exc}") from exc

        return BuildResult(
            recipe,
            scene,
            self.validator.validate(scene),
            intent=revised_intent,
            prompt_parser=self.intent_parser.name,
            revision_of=source_scene_id,
            revision_instruction=normalized,
        )

    def write_result(
        self,
        result: BuildResult,
        output_dir: str | Path,
        export_usd: bool = False,
    ) -> dict[str, str]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}

        spec_path = output / "scene_spec.json"
        layout_path = output / "layout.json"
        validation_path = output / "validation.json"
        self._write_json(
            spec_path,
            {
                **result.recipe.to_dict(),
                "resolved_description": result.scene.description,
                "seed": result.scene.seed,
                "scene_id": result.scene.scene_id,
                "prompt_parser": result.prompt_parser,
                "parser_warning": result.parser_warning,
                "revision_of": result.revision_of,
                "revision_instruction": result.revision_instruction,
            },
        )
        self._write_json(layout_path, result.scene.to_dict())
        self._write_json(validation_path, result.validation.to_dict())
        from .exporters.topdown_svg import TopDownSvgExporter

        preview_path = output / "preview.svg"
        TopDownSvgExporter(self.registry).export(result.scene, preview_path)
        files.update(
            scene_spec=str(spec_path.resolve()),
            layout=str(layout_path.resolve()),
            validation=str(validation_path.resolve()),
            preview=str(preview_path.resolve()),
        )

        if result.intent is not None:
            intent_path = output / "scene_intent.json"
            self._write_json(intent_path, result.intent.to_dict())
            files["intent"] = str(intent_path.resolve())

        if result.revision_of is not None:
            revision_path = output / "revision.json"
            self._write_json(
                revision_path,
                {
                    "source_scene_id": result.revision_of,
                    "scene_id": result.scene.scene_id,
                    "instruction": result.revision_instruction,
                    "prompt_parser": result.prompt_parser,
                },
            )
            files["revision"] = str(revision_path.resolve())

        if export_usd:
            from .exporters.isaac_usd import IsaacUsdExporter

            usd_path = output / "scene.usd"
            IsaacUsdExporter(self.registry).export(result.scene, usd_path)
            files["usd"] = str(usd_path.resolve())

        return files

    def build_batch(
        self,
        output_root: str | Path,
        count: int,
        seed_start: int,
        recipe_name: str | None = None,
        prompt: str | None = None,
        export_usd: bool = False,
        resume: bool = False,
    ) -> list[dict[str, Any]]:
        if count < 1:
            raise ValueError("count must be positive")
        if bool(recipe_name) == bool(prompt):
            raise ValueError("provide exactly one of recipe_name or prompt")

        output_root = Path(output_root).expanduser().resolve()
        metadata = make_dataset_metadata(
            recipe_name=recipe_name,
            prompt=prompt,
            count=count,
            seed_start=seed_start,
            export_usd=export_usd,
        )
        if resume:
            if not output_root.is_dir() or output_root.is_symlink():
                raise FileNotFoundError(f"cannot resume missing dataset root: {output_root}")
            snapshot = load_dataset_snapshot(output_root, allow_incomplete=True)
            self._assert_resume_invocation(snapshot.metadata, metadata)
            clean_known_staging(
                output_root,
                metadata["dataset_id"],
                seed_start=metadata["seed_start"],
                count=metadata["count"],
            )
            manifest = [dict(record) for record in snapshot.records]
            metadata = dict(snapshot.metadata)
        else:
            if output_root.exists():
                if not output_root.is_dir():
                    raise FileExistsError(f"batch output is not a directory: {output_root}")
                if any(output_root.iterdir()):
                    raise FileExistsError(
                        f"batch output already exists and is not empty: {output_root}; use --resume"
                    )
            output_root.mkdir(parents=True, exist_ok=True)
            manifest = []
            write_json_atomic(output_root / "dataset.json", metadata)
            write_manifest_atomic(output_root / "manifest.jsonl", manifest)

        existing_seeds = {record["seed"] for record in manifest}
        staging_root = output_root / ".staging"
        for offset in range(count):
            seed = seed_start + offset
            if seed in existing_seeds:
                continue
            try:
                result = (
                    self.build_from_recipe(recipe_name, seed)
                    if recipe_name
                    else self.build_from_prompt(prompt or "", seed)
                )
                if result.scene.seed != seed:
                    raise RuntimeError(
                        f"build result seed mismatch: expected={seed} actual={result.scene.seed}"
                    )
                scene_id = result.scene.scene_id
                if (
                    not isinstance(scene_id, str)
                    or not scene_id
                    or scene_id in {".", ".."}
                    or "/" in scene_id
                    or "\\" in scene_id
                ):
                    raise ValueError(f"invalid generated scene_id: {scene_id!r}")
                scene_dir = output_root / scene_id
                if scene_dir.exists() or scene_dir.is_symlink():
                    raise FileExistsError(f"scene directory already exists: {scene_dir}")
                staging_root.mkdir(parents=True, exist_ok=True)
                stage_dir = staging_root / scene_id
                if stage_dir.exists() or stage_dir.is_symlink():
                    raise FileExistsError(f"staging directory already exists: {stage_dir}")
                stage_dir.mkdir()
                write_staging_marker(
                    stage_dir,
                    dataset_key=metadata["dataset_id"],
                    scene_id=scene_id,
                    seed=seed,
                )
                files = self.write_result(result, stage_dir, export_usd=export_usd)
                record = make_manifest_record(result, files, output_root, scene_id)
                marker = stage_dir / ".scene_factory_staging.json"
                if not marker.is_file():
                    raise RuntimeError("staging marker disappeared before scene commit")
                marker.unlink()
                os.rename(stage_dir, scene_dir)
                manifest.append(record)
                manifest.sort(key=lambda item: item["seed"])
                write_manifest_atomic(output_root / "manifest.jsonl", manifest)
                metadata = update_dataset_metadata(
                    metadata, manifest, status="in_progress", generation_error=None
                )
                write_json_atomic(output_root / "dataset.json", metadata)
                existing_seeds.add(seed)
            except Exception as exc:
                metadata = update_dataset_metadata(
                    metadata,
                    manifest,
                    status="incomplete",
                    generation_error=type(exc).__name__,
                )
                try:
                    write_json_atomic(output_root / "dataset.json", metadata)
                except OSError:
                    pass
                raise

        try:
            staging_root.rmdir()
        except OSError:
            pass
        metadata = update_dataset_metadata(metadata, manifest, status="complete")
        write_manifest_atomic(output_root / "manifest.jsonl", manifest)
        write_json_atomic(output_root / "dataset.json", metadata)
        return manifest

    @staticmethod
    def _assert_resume_invocation(
        existing: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        keys = (
            "schema_version",
            "dataset_id",
            "source",
            "seed_start",
            "count",
            "expected_seed_end",
            "manifest",
            "export_usd",
        )
        mismatches = [key for key in keys if existing.get(key) != expected.get(key)]
        if mismatches:
            raise ValueError(f"resume invocation does not match dataset metadata: {mismatches}")

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
