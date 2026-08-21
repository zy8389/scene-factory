from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def _ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a real YCB mesh to USD with Isaac Sim asset-converter."
    )
    parser.add_argument("source", type=Path, help="Real YCB geometry file, e.g. textured.obj")
    parser.add_argument("--output", type=Path, required=True, help="USD output path")
    parser.add_argument("--report", type=Path)
    return parser


async def _convert(source: Path, output: Path) -> tuple[bool, str | None]:
    import omni.kit.asset_converter

    context = omni.kit.asset_converter.AssetConverterContext()
    context.use_meter_as_world_unit = True
    context.create_world_as_default_root_prim = True
    context.ignore_materials = False
    context.single_mesh = False
    converter = omni.kit.asset_converter.get_instance()
    task = converter.create_converter_task(
        source.as_posix(), output.as_posix(), lambda _progress, _total: None, context
    )
    success = await task.wait_until_finished()
    if success:
        return True, None
    return False, f"{task.get_status()}: {task.get_error_message()}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve() if args.report else None
    report: dict[str, Any] = {
        "source": str(source),
        "output": str(output),
        "converter": "Isaac Sim omni.kit.asset_converter",
        "result": "blocked",
        "issues": [],
    }
    if not source.is_file():
        report["issues"].append({"code": "missing_source_geometry", "message": str(source)})
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if source.suffix.lower() not in {".obj", ".ply", ".stl", ".dae", ".glb", ".gltf"}:
        report["issues"].append(
            {"code": "unsupported_source_format", "message": source.suffix.lower()}
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if output.exists():
        report["issues"].append(
            {"code": "output_exists", "message": f"refusing to overwrite: {output}"}
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if sys.platform == "win32" and not _ascii_path(output):
        report["issues"].append(
            {"code": "non_ascii_output_path", "message": "Isaac USD output must use an ASCII path"}
        )
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")
    app = None
    try:
        from isaacsim import SimulationApp

        app = SimulationApp(
            {
                "headless": True,
                "hide_ui": True,
                "renderer": "Minimal",
                "minimal_shading_mode": 4,
                "anti_aliasing": 0,
                "multi_gpu": False,
                "max_gpu_count": 1,
                "fast_shutdown": True,
                "width": 320,
                "height": 240,
                "disable_viewport_updates": True,
                "extra_args": ["--/app/renderer/skipWhileMinimized=true"],
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.new_event_loop()
        try:
            success, error = loop.run_until_complete(_convert(source, output))
        finally:
            loop.close()
        if not success:
            report["issues"].append({"code": "conversion_failed", "message": error or "unknown error"})
        elif not output.is_file() or output.stat().st_size == 0:
            report["issues"].append(
                {"code": "empty_usd_output", "message": f"converter did not create a USD: {output}"}
            )
        else:
            report.update({"result": "passed", "usd_load": "not_run"})
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["issues"].append(
            {"code": "isaac_converter_unavailable", "message": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        if app is not None:
            app.close(exit_code=0 if report["result"] == "passed" else 1)

    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
