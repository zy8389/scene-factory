from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replace converter-specific external GLTF material shaders with USD Preview Surface."
    )
    parser.add_argument("source", type=Path, help="Converted USD source")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-color", type=float, nargs=3, default=(0.72, 0.58, 0.40))
    parser.add_argument("--roughness", type=float, default=0.65)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _write(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sys.argv = [sys.argv[0]]
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    report: dict[str, Any] = {
        "source": str(source),
        "output": str(output),
        "operation": "sanitize_external_materials",
        "replacement": "UsdPreviewSurface",
        "result": "blocked",
        "issues": [],
    }
    if not source.is_file():
        report["issues"].append({"code": "missing_source_usd", "message": str(source)})
        _write(report_path, report)
        return 2
    if output.exists():
        report["issues"].append({"code": "output_exists", "message": str(output)})
        _write(report_path, report)
        return 2
    try:
        from pxr import Sdf, Usd, UsdShade

        stage = Usd.Stage.Open(source.as_posix())
        if stage is None:
            raise RuntimeError(f"could not open USD: {source}")
        changed = 0
        for prim in stage.Traverse():
            if not prim.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(prim)
            implementation = shader.GetImplementationSourceAttr().Get()
            source_asset = shader.GetSourceAsset("mdl")
            if implementation != "sourceAsset" and source_asset is None:
                continue
            for attribute in list(prim.GetAuthoredAttributes()):
                name = attribute.GetName()
                if name.startswith("info:mdl") or name.startswith("inputs:") or name.startswith("outputs:"):
                    prim.RemoveProperty(name)
            shader.CreateIdAttr().Set("UsdPreviewSurface")
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
                tuple(float(value) for value in args.base_color)
            )
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(args.roughness))
            shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
            shader_output = shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
            for material_prim in stage.Traverse():
                if not material_prim.IsA(UsdShade.Material):
                    continue
                material = UsdShade.Material(material_prim)
                for material_output in material.GetOutputs():
                    material_output.GetAttr().ClearConnections()
                material.CreateSurfaceOutput().ConnectToSource(shader_output)
            changed += 1
        if changed == 0:
            raise ValueError("no external material shader found")
        output.parent.mkdir(parents=True, exist_ok=True)
        flattened = stage.Flatten()
        # Converter documentation can contain the author's machine-local path.
        # Keep the committed USD portable and reproducible.
        flattened.documentation = "SceneFactory sanitized USD"
        flattened.Export(output.as_posix())
        report.update({"shader_count": changed, "result": "passed"})
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["issues"].append({"code": "material_sanitization_failed", "message": str(exc)})
    finally:
        _write(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
