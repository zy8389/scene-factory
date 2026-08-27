from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .factory import SceneFactory
from .intent import SceneIntent
from .paths import default_web_dir, project_root


class SceneWebApplication:
    def __init__(self, output_root: str | Path, factory: SceneFactory | None = None) -> None:
        self.factory = factory or SceneFactory()
        self.output_root = Path(output_root).expanduser().resolve()
        self.static_root = default_web_dir().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

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

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = str(payload.get("prompt", "")).strip()
        if len(prompt) < 2:
            raise ValueError("请至少输入 2 个字的场景需求")
        if len(prompt) > 1000:
            raise ValueError("场景需求不能超过 1000 个字")

        seed = int(payload.get("seed", 42))
        count = int(payload.get("count", 1))
        export_usd = bool(payload.get("export_usd", False))
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
            files = self.factory.write_result(result, scene_dir, export_usd=export_usd)
            items.append(self._result_payload(result, files))

        return {
            "prompt": prompt,
            "seed_start": seed,
            "count": count,
            "export_usd": export_usd,
            "valid_count": sum(bool(item["validation"]["valid"]) for item in items),
            "items": items,
        }

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
        files = self.factory.write_result(result, scene_dir, export_usd=export_usd)
        return {
            "source_scene_id": source_scene_id,
            "instruction": instruction,
            "item": self._result_payload(result, files),
        }

    @staticmethod
    def _result_payload(result: Any, files: dict[str, str]) -> dict[str, Any]:
        file_urls = {
            name: f"/outputs/{quote(result.scene.scene_id)}/{quote(Path(path).name)}"
            for name, path in files.items()
        }
        revision = None
        if result.revision_of is not None:
            revision = {
                "source_scene_id": result.revision_of,
                "instruction": result.revision_instruction,
            }
        return {
            "scene": result.scene.to_dict(),
            "validation": result.validation.to_dict(),
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

    def open_in_isaac(self, payload: dict[str, Any]) -> dict[str, Any]:
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

        launcher = project_root() / "tools" / "open_in_isaac.py"
        if not launcher.is_file():
            raise FileNotFoundError(launcher)

        log_dir = self.output_root / "_isaac_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_log_name = "".join(character if character.isalnum() else "_" for character in scene_id)
        stdout_path = log_dir / f"{safe_log_name}.stdout.log"
        stderr_path = log_dir / f"{safe_log_name}.stderr.log"
        environment = os.environ.copy()
        environment.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                [sys.executable, str(launcher), str(usd_path)],
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
                    },
                )
                return
            if path == "/api/llm/status":
                self._send_json(HTTPStatus.OK, self.app.factory.llm_status())
                return
            if path == "/api/recipes":
                self._send_json(HTTPStatus.OK, {"recipes": self.app.recipe_catalog()})
                return
            if path.startswith("/outputs/"):
                self._send_file(self.app.resolve_output(path.removeprefix("/outputs/")))
                return
            self._send_file(self.app.resolve_static(path))
        except FileNotFoundError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/api/generate", "/api/revise", "/api/open-isaac", "/api/llm/test"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求内容大小不合法")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象")
            if path == "/api/generate":
                result = self.app.generate(payload)
            elif path == "/api/revise":
                result = self.app.revise(payload)
            elif path == "/api/open-isaac":
                result = self.app.open_in_isaac(payload)
            else:
                result = self.app.factory.test_llm_connection()
            self._send_json(HTTPStatus.OK, result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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


def _default_output() -> Path:
    configured = os.environ.get("SCENE_FACTORY_WEB_OUTPUT")
    if configured:
        return Path(configured)
    return Path("outputs/web")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SceneFactory natural-language UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=_default_output())
    args = parser.parse_args(argv)

    app = SceneWebApplication(args.output)
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
