"""Non-invasive diagnostics for the portable SDK and optional local runtimes."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .isaac_runtime import find_isaac_python
from .paths import project_root


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _isaac_candidate() -> Path | None:
    try:
        return find_isaac_python()
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return None


def _uv_command() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    launcher = shutil.which("py")
    if launcher:
        try:
            probe = subprocess.run([launcher, "-3.12", "-m", "uv", "--version"],
                                   capture_output=True, text=True, timeout=5, check=False)
            if probe.returncode == 0:
                return "py -3.12 -m uv (" + probe.stdout.strip() + ")"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def diagnose() -> dict[str, object]:
    root = project_root()
    isaac_python = _isaac_candidate()
    isaac: dict[str, object] = {
        "available": False, "python": None, "pxr": False, "isaac_sim": None,
        "torch": None, "cuda_runtime": None, "cuda_available": False,
        "usd_stage_usable": False,
    }
    if isaac_python:
        isaac["python"] = str(isaac_python)
        isaac["pxr"] = True
        probe_code = """
import importlib.metadata as metadata
import json

def version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

report = {"isaac_sim": version("isaacsim"), "torch": version("torch")}
if report["torch"]:
    try:
        import torch
        report.update(cuda_runtime=torch.version.cuda, cuda_available=torch.cuda.is_available())
    except Exception as error:
        report["warning"] = "PyTorch/CUDA probe failed: " + str(error)
print(json.dumps(report))
"""
        try:
            probe = subprocess.run(
                [str(isaac_python), "-c", probe_code],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=45, check=False,
            )
            if probe.returncode == 0:
                metadata = json.loads(probe.stdout.strip().splitlines()[-1])
                if not isinstance(metadata, dict):
                    raise ValueError("Isaac metadata probe must return a JSON object")
                isaac.update(metadata)
            else:
                isaac["warning"] = "pxr works, but Isaac/PyTorch metadata probe failed"
        except subprocess.TimeoutExpired:
            isaac["warning"] = "pxr works, but Isaac/PyTorch metadata probe timed out"
        except (OSError, ValueError, IndexError):
            isaac["warning"] = "pxr works, but Isaac/PyTorch metadata probe failed"
    if isaac_python:
        try:
            stage_probe = subprocess.run(
                [str(isaac_python), "-c",
                 "from pxr import Usd; stage=Usd.Stage.CreateInMemory(); "
                 "print('usd_stage_ok' if stage else 'usd_stage_failed')"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=25, check=False,
            )
            isaac["usd_stage_usable"] = (
                stage_probe.returncode == 0 and "usd_stage_ok" in stage_probe.stdout
            )
        except (OSError, subprocess.TimeoutExpired):
            isaac["usd_stage_usable"] = False
        if not isaac["usd_stage_usable"]:
            isaac["warning"] = (
                "pxr imports, but Usd.Stage.CreateInMemory fails; "
                "USD export is blocked in this Isaac environment"
            )
        elif os.name == "nt" and not str(isaac_python).isascii():
            isaac["warning"] = (
                "Isaac Python is still using a non-ASCII path; on Windows, "
                "use the ASCII scene_factory_isaac_py312 junction for USD/Kit"
            )
        isaac["available"] = bool(isaac.get("isaac_sim") and isaac["usd_stage_usable"])
    blender = os.environ.get("BLENDER_EXE") or shutil.which("blender")
    if blender and not Path(blender).is_file():
        blender = shutil.which(blender)
    return {
        "core_ready": (
            sys.version_info >= (3, 12)
            and (root / "data/assets/registry.jsonl").is_file()
            and (root / "recipes").is_dir()
        ),
        "python": {"executable": sys.executable, "version": sys.version.split()[0]},
        "scene_factory": _version("scene-factory"),
        "mujoco": _version("mujoco"),
        "numpy": _version("numpy"),
        "uv": _uv_command(),
        "isaac": isaac,
        "blender": {"available": bool(blender), "executable": blender},
        "non_ascii_project_path": not str(root).isascii(),
        "warnings": [
            "Isaac/USD on Windows may fail with non-ASCII project/output paths; use an ASCII-only checkout/output directory."
        ] if os.name == "nt" and not str(root).isascii() else [],
    }


def format_report(report: dict[str, object]) -> str:
    isaac = report["isaac"]
    blender = report["blender"]
    return "\n".join([
        f"Core: {'ready' if report['core_ready'] else 'not ready'} | Python {report['python']['version']}",
        f"MuJoCo: {report['mujoco'] or 'not installed (optional)'} | uv: {report['uv'] or 'not found'}",
        f"Isaac: {isaac['isaac_sim'] if isaac.get('available') else 'not fully detected (optional)'} | Python: {isaac['python'] or 'not found'}",
        f"Torch: {isaac.get('torch') or 'not detected'} | CUDA: {isaac.get('cuda_runtime') or 'not detected'} | GPU usable: {isaac.get('cuda_available', False)} | USD stage usable: {isaac.get('usd_stage_usable', False)}",
        f"Blender: {blender['executable'] or 'not installed (manifest can still be prepared)'}",
        *[f"Warning: {message}" for message in report['warnings']],
        *([f"Warning: {isaac['warning']}"] if isaac.get('warning') else []),
    ])
