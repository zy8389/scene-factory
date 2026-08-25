from __future__ import annotations

import os
import sysconfig
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def project_root() -> Path:
    configured = os.environ.get("SCENE_FACTORY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if _has_runtime_resources(PROJECT_ROOT):
        return PROJECT_ROOT
    return (Path(sysconfig.get_path("data")) / "share" / "scene-factory").resolve()


def default_registry_path() -> Path:
    return project_root() / "data" / "assets" / "registry.jsonl"


def default_recipes_dir() -> Path:
    return project_root() / "recipes"


def default_web_dir() -> Path:
    return project_root() / "web"


def _has_runtime_resources(root: Path) -> bool:
    return (
        (root / "recipes").is_dir()
        and (root / "data" / "assets" / "registry.jsonl").is_file()
        and (root / "web").is_dir()
    )
