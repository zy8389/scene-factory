from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .paths import project_root


def find_isaac_python() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("SCENE_FACTORY_ISAAC_PYTHON", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        (
            project_root().parent / "scene_factory_isaac_py312" / "Scripts" / "python.exe",
            Path(sys.executable),
        )
    )
    checked: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in checked or not resolved.is_file():
            continue
        checked.add(resolved)
        probe = subprocess.run(
            [str(resolved), "-c", "import pxr"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if probe.returncode == 0:
            return resolved
    raise RuntimeError(
        "Isaac validation is optional but no Python with 'pxr' was found. "
        "Set SCENE_FACTORY_ISAAC_PYTHON to Isaac Sim's python executable."
    )


def export_usd_with_isaac(
    layout_path: str | Path,
    registry_path: str | Path,
    output_path: str | Path,
) -> Path:
    runtime = find_isaac_python()
    exporter = project_root() / "tools" / "export_isaac_usd.py"
    if not exporter.is_file():
        raise FileNotFoundError(exporter)
    environment = os.environ.copy()
    python_path = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(project_root()), python_path) if value
    )
    command = [
        str(runtime),
        str(exporter),
        str(Path(layout_path).resolve()),
        "--registry",
        str(Path(registry_path).resolve()),
        "--output",
        str(Path(output_path).resolve()),
    ]
    result = subprocess.run(
        command,
        cwd=project_root(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Isaac USD export failed: {detail[-1000:]}")
    return Path(output_path).resolve()
