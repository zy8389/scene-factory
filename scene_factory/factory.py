from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    ) -> list[dict[str, Any]]:
        if count < 1:
            raise ValueError("count must be positive")
        if bool(recipe_name) == bool(prompt):
            raise ValueError("provide exactly one of recipe_name or prompt")

        output_root = Path(output_root)
        output_root.mkdir(parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []

        for offset in range(count):
            seed = seed_start + offset
            result = (
                self.build_from_recipe(recipe_name, seed)
                if recipe_name
                else self.build_from_prompt(prompt or "", seed)
            )
            scene_dir = output_root / result.scene.scene_id
            files = self.write_result(result, scene_dir, export_usd=export_usd)
            manifest.append(
                {
                    "scene_id": result.scene.scene_id,
                    "seed": seed,
                    "recipe": result.scene.recipe_name,
                    "valid": result.valid,
                    "prompt_parser": result.prompt_parser,
                    "parser_warning": result.parser_warning,
                    "files": files,
                }
            )

        manifest_path = output_root / "manifest.jsonl"
        with manifest_path.open("w", encoding="utf-8") as handle:
            for item in manifest:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        return manifest

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
