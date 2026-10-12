from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .factory import SceneFactory
from .bundle import _visual_transform_for_asset
from .intent import SceneIntent
from .models import CompiledScene
from .isaac_runtime import find_isaac_python, isaac_process_environment
from .paths import default_web_dir, project_root


class SceneWebApplication:
    def __init__(
        self, output_root: str | Path, factory: SceneFactory | None = None, *,
        runtime_root: str | Path | None = None,
        isaac_python: str | Path | None = None,
        asset_root: str | Path | None = None,
        runtime_history: tuple[str | Path, ...] = (),
    ) -> None:
        self.factory = factory or SceneFactory()
        self.output_root = Path(output_root).expanduser().resolve()
        self.static_root = default_web_dir().resolve()
        self.asset_root = (project_root() / "data" / "assets" / "source").resolve()
        self.visual_assets = self._discover_visual_assets()
        self._blender_case_locks: dict[str, threading.Lock] = {}
        self._runtime_manager = None
        self._runtime_lock = threading.Lock()
        self._runtime_root = runtime_root or os.environ.get("SCENE_FACTORY_WEB_RUNTIME") or self.output_root / "_runtime"
        self._runtime_options = {
            "isaac_python": isaac_python,
            "asset_root": asset_root,
            "history_roots": runtime_history,
        }
        self.output_root.mkdir(parents=True, exist_ok=True)

    @property
    def runtime(self):
        from .web_runtime import WebRuntimeManager

        with self._runtime_lock:
            if self._runtime_manager is None:
                self._runtime_manager = WebRuntimeManager(
                    self._runtime_root, self.factory, **self._runtime_options,
                )
            return self._runtime_manager

    def runtime_replay(self, job_id: str, case_id: str) -> dict[str, Any]:
        from .runtime_replay import replay_payload

        job = self.runtime.get_job(job_id)
        if not job.get("validation", {}).get("valid"):
            raise RuntimeError("这批运行尚未通过证据校验，暂不能回放")
        data = self.runtime.case_data(job_id, case_id)
        assets = {}
        for item in data["scene"]["objects"]:
            try:
                assets[item["asset_id"]] = self._asset_payload(item["asset_id"])
            except KeyError:
                assets[item["asset_id"]] = {"visual_url": None}
        return replay_payload(data["case_root"], assets=assets)

    def close(self) -> None:
        if self._runtime_manager is not None:
            self._runtime_manager.close()

    def _runtime_scene_directory(self, payload: dict[str, Any]) -> Path:
        scene_id = payload.get("scene_id")
        if (not isinstance(scene_id, str) or not scene_id or scene_id != scene_id.strip()
                or scene_id in {".", ".."} or any(char in scene_id for char in "/\\:\x00")):
            raise ValueError("请选择网页中已生成的场景")
        directory = self.output_root / scene_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.output_root):
            raise ValueError("场景路径不安全")
        layout = directory / "layout.json"
        if (layout.is_symlink() or not layout.resolve().is_relative_to(directory.resolve())
                or not layout.is_file()):
            raise ValueError("当前场景没有可运行的布局记录，请先生成场景")
        if layout.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("当前场景布局记录过大")
        source = json.loads(layout.read_text(encoding="utf-8-sig"))
        if not isinstance(source, dict) or source.get("scene_id") != scene_id:
            raise ValueError("场景编号与保存的布局不一致")
        return directory

    def runtime_scene_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .scene_runtime import inspect_scene_runtime

        if set(payload) - {"scene_id", "profile"}:
            raise ValueError("场景执行检查只支持 scene_id 和 profile")
        profile = payload.get("profile", "tracking")
        if profile not in {"tracking", "baseline"}:
            raise ValueError("请选择 tracking 或 baseline 控制器")
        directory = self._runtime_scene_directory(payload)
        result = inspect_scene_runtime(self.factory, directory)
        plan = result.get("plan") or {}
        config = self.runtime.config()
        return {
            **result, "scene_id": payload["scene_id"], "profile": profile,
            "task_count": 2 if result.get("eligible") else 0,
            "robot_base_position_m": plan.get("robot_base_position_m"),
            "target_position_m": plan.get("target_position_m"),
            "runtime_available": config["available"], "runtime_message": config["message"],
        }

    def create_runtime_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "scene_id" in payload:
            return self.runtime.create_scene(self._runtime_scene_directory(payload), payload)
        return self.runtime.create(payload)

    def recipe_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": recipe.name,
                "room_type": recipe.room_type,
                "event": recipe.event,
                "description": recipe.description,
                "object_count": len(recipe.objects),
            }
            for recipe in (self.factory.recipes.get(name) for name in self.factory.recipes.names())
        ]

    def asset_catalog(self) -> list[dict[str, Any]]:
        return [
            self._asset_payload(record.asset_id)
            for record in self.factory.registry.list(statuses=("ready", "validated"))
        ]

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt", "")).strip()
        if len(prompt) < 2:
            raise ValueError("请至少输入 2 个字的场景需求")
        if len(prompt) > 1000:
            raise ValueError("场景需求不能超过 1000 个字")

        seed = int(payload.get("seed", 42))
        count = int(payload.get("count", 1))
        export_usd = bool(payload.get("export_usd", False))
        export_mjcf = bool(payload.get("export_mjcf", True))
        export_blender = bool(payload.get("export_blender", False))
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed 必须介于 0 和 2147483647 之间")
        if not 1 <= count <= 12:
            raise ValueError("单次生成数量必须介于 1 和 12 之间")
        if export_usd and os.name == "nt" and not str(self.output_root).isascii():
            raise ValueError("Windows 下导出 USD 时请使用纯英文输出路径")

        items = []
        for offset in range(count):
            result = self.factory.build_from_prompt(prompt, seed + offset)
            scene_dir = self.output_root / result.scene.scene_id
            files = self.factory.write_result(
                result,
                scene_dir,
                export_usd=export_usd,
                export_mjcf=export_mjcf,
                export_blender=export_blender,
            )
            items.append(self._result_payload(result, files))

        return {
            "prompt": prompt,
            "seed_start": seed,
            "count": count,
            "export_usd": export_usd,
            "export_mjcf": export_mjcf,
            "export_blender": export_blender,
            "valid_count": sum(bool(item["validation"]["valid"]) for item in items),
            "items": items,
        }

    def generate_priority(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .dataset import write_json_atomic
        from .priority import DEFAULT_TASK, build_priority_scenario

        result, scenario = build_priority_scenario(
            self.factory, seed=payload.get("seed", 42),
            clearance_m=payload.get("clearance_m", 0.005),
            motion_state=payload.get("motion_state", "toward_edge"),
            current_task=payload.get("current_task", DEFAULT_TASK),
            urgency=payload.get("urgency", "routine"),
            safe_to_pause=payload.get("safe_to_pause", True),
            pause_consequence=payload.get("pause_consequence", ""),
        )
        directory = self.output_root / result.scene.scene_id
        files = self.factory.write_result(result, directory, export_mjcf=True)
        path = directory / "priority_scenario.json"
        write_json_atomic(path, scenario)
        files["priority_scenario"] = str(path)
        item = self._result_payload(result, files)
        item["priority_scenario"] = scenario
        return {"items": [item], "count": 1, "valid_count": 1}

    def ask_priority(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .dataset import write_json_atomic
        from .priority import ask_priority, load_priority_scenario

        scene_id = payload.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id:
            raise ValueError("scene_id is required")
        directory = (self.output_root / scene_id).resolve()
        if not directory.is_relative_to(self.output_root):
            raise ValueError("invalid scene_id")
        scenario = load_priority_scenario(directory / "priority_scenario.json", factory=self.factory)
        decision = ask_priority(scenario)
        output = directory / "priority_decision.json"
        write_json_atomic(output, decision)
        return {"decision": decision, "files": self._file_urls(scene_id, {"priority_decision": str(output)})}

    def revise(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_scene_id = str(payload.get("scene_id", "")).strip()
        instruction = str(payload.get("instruction", "")).strip()
        if not source_scene_id:
            raise ValueError("scene_id is required")
        if len(instruction) < 2:
            raise ValueError("请至少输入 2 个字的修改要求")
        if len(instruction) > 1000:
            raise ValueError("修改要求不能超过 1000 个字")

        source_dir = (self.output_root / source_scene_id).resolve()
        if not source_dir.is_relative_to(self.output_root):
            raise ValueError("invalid scene_id")
        intent_path = source_dir / "scene_intent.json"
        layout_path = source_dir / "layout.json"
        if not intent_path.is_file():
            raise ValueError("当前场景没有 SceneIntent，只有 LLM 生成的场景可以继续修改")
        if not layout_path.is_file():
            raise FileNotFoundError("当前场景缺少 layout.json")

        raw_intent = json.loads(intent_path.read_text(encoding="utf-8"))
        current_intent = SceneIntent.from_dict(
            raw_intent,
            allowed_categories=self.factory.registry.categories(),
            allowed_room_types=self.factory.recipes.room_types(),
            allowed_events=self.factory.recipes.events(),
        )
        source_layout = json.loads(layout_path.read_text(encoding="utf-8"))
        seed = int(payload.get("seed", source_layout.get("seed", 42)))
        export_usd = bool(payload.get("export_usd", (source_dir / "scene.usd").is_file()))
        export_mjcf = bool(payload.get("export_mjcf", True))
        export_blender = bool(
            payload.get("export_blender", (source_dir / "blender_manifest.json").is_file())
        )
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed 必须介于 0 和 2147483647 之间")
        if export_usd and os.name == "nt" and not str(self.output_root).isascii():
            raise ValueError("Windows 下导出 USD 时请使用纯英文输出路径")

        result = self.factory.build_from_intent_revision(
            current_intent,
            instruction,
            seed,
            source_scene_id=source_scene_id,
        )
        scene_dir = self.output_root / result.scene.scene_id
        files = self.factory.write_result(
            result,
            scene_dir,
            export_usd=export_usd,
            export_mjcf=export_mjcf,
            export_blender=export_blender,
        )
        return {
            "source_scene_id": source_scene_id,
            "instruction": instruction,
            "item": self._result_payload(result, files),
        }

    def export_blender_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        scene_id = str(payload.get("scene_id", "")).strip()
        if not scene_id:
            raise ValueError("scene_id is required")
        scene_dir = (self.output_root / scene_id).resolve()
        if not scene_dir.is_relative_to(self.output_root):
            raise ValueError("invalid scene_id")
        layout_path = scene_dir / "layout.json"
        if not layout_path.is_file():
            raise FileNotFoundError("当前场景缺少 layout.json")
        raw_scene = json.loads(layout_path.read_text(encoding="utf-8"))
        scene = CompiledScene.from_dict(raw_scene)
        if scene.scene_id != scene_id:
            raise ValueError("layout.json 中的 scene_id 与请求不一致")
        from .exporters.blender import BlenderExporter

        render_blend = payload.get("render_blend", False)
        if not isinstance(render_blend, bool):
            raise ValueError("render_blend must be boolean")
        lock = self._blender_case_locks.setdefault(scene_id, threading.Lock())
        with lock:
            files = BlenderExporter(self.factory.registry).export(scene, scene_dir, render_blend=render_blend)
        return {"scene_id": scene_id, "files": self._file_urls(scene_id, files)}

    @staticmethod
    def _file_urls(scene_id: str, files: dict[str, str]) -> dict[str, str]:
        return {
            name: f"/outputs/{quote(scene_id, safe='')}/{quote(Path(path).name, safe='')}"
            for name, path in files.items()
        }

    def _result_payload(self, result: Any, files: dict[str, str]) -> dict[str, Any]:
        file_urls = self._file_urls(result.scene.scene_id, files)
        assets = {item.asset_id: self._asset_payload(item.asset_id) for item in result.scene.objects}
        proxies = [item for item in result.scene.objects if not assets[item.asset_id]["visual_url"]]
        revision = None
        if result.revision_of is not None:
            revision = {
                "source_scene_id": result.revision_of,
                "instruction": result.revision_instruction,
            }
        return {
            "scene": result.scene.to_dict(),
            "assets": assets,
            "appearance": {
                "modeled_count": len(result.scene.objects) - len(proxies),
                "proxy_count": len(proxies),
                "proxy_categories": sorted({item.category for item in proxies}),
            },
            "validation": result.validation.to_dict(),
            "quality": result.quality,
            "matched_recipe": {
                "name": result.recipe.name,
                "room_type": result.recipe.room_type,
                "event": result.recipe.event,
                "description": result.recipe.description,
            },
            "prompt_parser": result.prompt_parser,
            "parser_warning": result.parser_warning,
            "revision": revision,
            "files": file_urls,
        }

    def _asset_payload(self, asset_id: str) -> dict[str, Any]:
        record = self.factory.registry.get(asset_id)
        visual = self.visual_assets.get(asset_id)
        return {
            "asset_id": asset_id,
            "name": record.name or asset_id,
            "primitive": record.primitive,
            "color": list(record.color),
            "bbox_m": list(record.bbox_m),
            "source_type": record.source_type,
            "visual_url": visual["url"] if visual else None,
            "visual_transform": _visual_transform_for_asset(visual["transform"], record) if visual else None,
            "license": record.license,
            "support_surfaces": [asdict(surface) for surface in record.support_surfaces],
            "interior_regions": [asdict(region) for region in record.interior_regions],
            # Keep the manipulation contract alongside the visual and physical
            # metadata so the web planner can make the same graspability
            # decision as the offline task compiler.
            "grasp_region": record.grasp_region,
            "manipulation": {
                "graspable": bool(record.grasp_region),
                "validated": bool(record.grasp_region) and record.status == "ready",
                "allowed_actions": list(
                    record.grasp_region.get("allowed_actions", [])
                    if isinstance(record.grasp_region, dict)
                    else []
                ),
            },
            "physics": {"mass_kg": record.mass_kg,"static_friction": record.static_friction,
                        "dynamic_friction": record.dynamic_friction,"rigid_body": record.rigid_body,
                        "collision_status": record.collision_status},
        }

    def _discover_visual_assets(self) -> dict[str, dict[str, Any]]:
        assets: dict[str, dict[str, Any]] = {}
        if not self.asset_root.is_dir():
            return assets
        for metadata_path in self.asset_root.glob("*/SOURCE.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                asset_id = str(metadata["asset_id"]).strip()
                relative_geometry = Path(str(metadata["source_geometry"]))
                geometry_path = (metadata_path.parent / relative_geometry).resolve()
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not asset_id
                or not geometry_path.is_relative_to(self.asset_root)
                or not geometry_path.is_file()
            ):
                continue
            assets[asset_id] = {
                "path": geometry_path,
                "url": f"/assets/{quote(asset_id)}/visual",
                "transform": self._visual_transform(metadata),
            }
        return assets

    @staticmethod
    def _visual_transform(metadata: dict[str, Any]) -> dict[str, Any]:
        """Return the documented axis transform used to display one source GLB."""
        raw = metadata.get("visual_transform")
        if not isinstance(raw, dict):
            raw = {}
        rotation = raw.get("rotation_euler_deg", (90.0, 0.0, 0.0))
        if (
            not isinstance(rotation, (list, tuple))
            or len(rotation) != 3
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in rotation)
        ):
            rotation = (90.0, 0.0, 0.0)
        return {
            "up_axis": str(raw.get("up_axis", "Y")),
            "rotation_euler_deg": [float(value) for value in rotation],
            "scale_mode": "uniform_contain",
            "anchor": "bottom_center",
        }

    def resolve_output(self, relative_path: str) -> Path:
        requested = (self.output_root / unquote(relative_path)).resolve()
        if not requested.is_relative_to(self.output_root) or not requested.is_file():
            raise FileNotFoundError(relative_path)
        return requested

    def resolve_static(self, path: str) -> Path:
        relative = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        requested = (self.static_root / relative).resolve()
        if not requested.is_relative_to(self.static_root) or not requested.is_file():
            raise FileNotFoundError(path)
        return requested

    def resolve_asset(self, relative_path: str) -> Path:
        parts = relative_path.strip("/").split("/")
        if len(parts) != 2 or parts[1] != "visual":
            raise FileNotFoundError(relative_path)
        asset = self.visual_assets.get(unquote(parts[0]))
        if asset is None:
            raise FileNotFoundError(relative_path)
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to(self.asset_root) or not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def open_in_isaac(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._runtime_manager is not None and self._runtime_manager.has_active_jobs:
            raise RuntimeError("正在执行机器人验收，请等队列完成后再打开 Isaac 预览")
        scene_id = str(payload.get("scene_id", "")).strip()
        if not scene_id:
            raise ValueError("scene_id is required")

        scene_dir = (self.output_root / scene_id).resolve()
        if not scene_dir.is_relative_to(self.output_root):
            raise ValueError("invalid scene_id")
        usd_path = scene_dir / "scene.usd"
        if not usd_path.is_file():
            raise FileNotFoundError(
                "This scene has no USD file. Generate it again with USD export enabled."
            )

        log_dir = self.output_root / "_isaac_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_log_name = "".join(character if character.isalnum() else "_" for character in scene_id)
        stdout_path = log_dir / f"{safe_log_name}.stdout.log"
        stderr_path = log_dir / f"{safe_log_name}.stderr.log"
        environment = isaac_process_environment()
        environment.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        isaac_python = find_isaac_python()

        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                [str(isaac_python), "-m", "scene_factory.isaac_preview", str(usd_path)],
                cwd=project_root(),
                env=environment,
                stdout=stdout,
                stderr=stderr,
                creationflags=creation_flags,
            )
        return {
            "ok": True,
            "pid": process.pid,
            "scene_id": scene_id,
            "usd": str(usd_path),
            "message": "Isaac Sim is starting; the first launch can take about one minute.",
        }


class SceneFactoryHandler(BaseHTTPRequestHandler):
    server_version = "SceneFactoryWeb/0.1"

    @property
    def app(self) -> SceneWebApplication:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "output_root": str(self.app.output_root),
                        "prompt_parser": self.app.factory.prompt_parser_mode,
                        "default_backend": "mujoco",
                        "preview_backend": "threejs",
                        "validation_backend": "isaac_optional",
                    },
                )
                return
            if path == "/api/llm/status":
                self._send_json(HTTPStatus.OK, self.app.factory.llm_status())
                return
            if path == "/api/recipes":
                self._send_json(HTTPStatus.OK, {"recipes": self.app.recipe_catalog()})
                return
            if path == "/api/assets":
                self._send_json(HTTPStatus.OK, {"assets": self.app.asset_catalog()})
                return
            if path == "/api/runtime/config":
                self._send_json(HTTPStatus.OK, self.app.runtime.config())
                return
            if path == "/api/runtime/jobs":
                self._send_json(HTTPStatus.OK, {"jobs": self.app.runtime.list_jobs()})
                return
            if path.startswith("/api/runtime/jobs/"):
                parts = path.removeprefix("/api/runtime/jobs/").split("/")
                job_id = unquote(parts[0])
                if len(parts) == 1:
                    self._send_json(HTTPStatus.OK, self.app.runtime.get_job(job_id))
                elif len(parts) == 4 and parts[1] == "cases" and parts[3] == "replay":
                    self._send_json(HTTPStatus.OK, self.app.runtime_replay(job_id, unquote(parts[2])))
                elif len(parts) >= 3 and parts[1] == "files":
                    relative = unquote("/".join(parts[2:]))
                    self._send_file(self.app.runtime.resolve_file(job_id, relative))
                else:
                    raise FileNotFoundError(path)
                return
            if path.startswith("/outputs/"):
                self._send_file(self.app.resolve_output(path.removeprefix("/outputs/")))
                return
            if path.startswith("/assets/"):
                self._send_file(self.app.resolve_asset(path.removeprefix("/assets/")))
                return
            self._send_file(self.app.resolve_static(path))
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except (ValueError, TypeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {
            "/api/generate",
            "/api/revise",
            "/api/open-isaac",
            "/api/export-blender",
            "/api/llm/test",
            "/api/priority/generate",
            "/api/priority/ask",
            "/api/runtime/jobs",
            "/api/runtime/scene-check",
        }:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求内容大小不合法")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象")
            if path == "/api/runtime/jobs":
                result = self.app.create_runtime_job(payload)
            elif path == "/api/runtime/scene-check":
                result = self.app.runtime_scene_check(payload)
            elif path == "/api/generate":
                result = self.app.generate(payload)
            elif path == "/api/priority/generate":
                result = self.app.generate_priority(payload)
            elif path == "/api/priority/ask":
                result = self.app.ask_priority(payload)
            elif path == "/api/revise":
                result = self.app.revise(payload)
            elif path == "/api/open-isaac":
                result = self.app.open_in_isaac(payload)
            elif path == "/api/export-blender":
                result = self.app.export_blender_case(payload)
            else:
                result = self.app.factory.test_llm_connection()
            self._send_json(HTTPStatus.ACCEPTED if path == "/api/runtime/jobs" else HTTPStatus.OK, result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type, _ = mimetypes.guess_type(path.name)
        if path.suffix.lower() == ".glb":
            content_type = "model/gltf-binary"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if path.suffix.lower() == ".svg":
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        if path.suffix.lower() == ".zip":
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"',
            )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} {fmt % args}")


class SceneFactoryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        app: SceneWebApplication,
    ) -> None:
        super().__init__(server_address, SceneFactoryHandler)
        self.app = app

    def server_close(self) -> None:
        self.app.close()
        super().server_close()


def _default_output() -> Path:
    configured = os.environ.get("SCENE_FACTORY_WEB_OUTPUT")
    if configured:
        return Path(configured)
    output = Path("outputs/web")
    if os.name == "nt" and not str(output.absolute()).isascii():
        return Path(output.absolute().anchor) / "scene_factory_runtime" / "web"
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SceneFactory natural-language UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=_default_output())
    parser.add_argument("--runtime-output", type=Path, help="ASCII directory for queued robot acceptance runs")
    parser.add_argument("--isaac-python", type=Path, help="Python executable with Isaac Sim installed")
    parser.add_argument("--asset-root", type=Path, help="Official local Isaac assets root")
    parser.add_argument("--runtime-history", type=Path, action="append", default=[],
                        help="Existing runtime batch to show read-only; may be repeated")
    args = parser.parse_args(argv)

    app = SceneWebApplication(
        args.output, runtime_root=args.runtime_output, isaac_python=args.isaac_python,
        asset_root=args.asset_root, runtime_history=tuple(args.runtime_history),
    )
    server = SceneFactoryServer((args.host, args.port), app)
    print(f"SceneFactory UI: http://{args.host}:{args.port}", flush=True)
    print(f"Generated scenes: {app.output_root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
