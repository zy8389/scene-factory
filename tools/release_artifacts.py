"""Audit and describe v0.1.0 release artifacts.

The command intentionally uses only the Python standard library.  It is safe
to run from a clean checkout and writes the manifest next to the supplied
output path, so generated provenance never becomes source-tree state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable


EXPECTED_VERSION = "0.1.0"
EXPECTED_WHEEL = "scene_factory-0.1.0-py3-none-any.whl"
EXPECTED_SDIST = "scene_factory-0.1.0.tar.gz"
MAX_WHEEL_BYTES = 20 * 1024 * 1024
MAX_SDIST_BYTES = 50 * 1024 * 1024

WHEEL_REQUIRED_FILES = (
    "recipes/kitchen_after_cooking.json",
    "recipes/kitchen_franka_mug_lift.json",
    "recipes/kitchen_franka_mug_pick_place.json",
    "recipes/living_room_recent_snacking.json",
    "recipes/living_room_returned_home.json",
    "schemas/asset_record.schema.json",
    "schemas/execution_trace.schema.json",
    "schemas/executor_capabilities.schema.json",
    "schemas/executor_conformance.schema.json",
    "schemas/interaction_plan.schema.json",
    "schemas/scene_intent.schema.json",
    "schemas/scene_spec.schema.json",
    "web/index.html",
    "web/styles.css",
    "web/app.js",
    "data/assets/registry.jsonl",
    "data/assets/metadata/mug_001.json",
    "data/assets/source/ycb_025_mug/SOURCE.json",
    "data/assets/source/ycb_025_mug/SOURCE.template.json",
    "data/assets/source/ycb_025_mug/textured.glb",
    "data/assets/source/ycb_025_mug/collision/025_cv_decomp.glb",
    "data/assets/collision/mug_001_collision.usd",
    "data/assets/usd/mug_001.usd",
)
SDIST_REQUIRED_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "scene_factory/__init__.py",
    *WHEEL_REQUIRED_FILES,
)
FORBIDDEN_COMMON_SEGMENTS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "outputs",
    "release-dist",
    "logs",
    "secrets",
    "secret",
    "credentials",
    "credential",
    "local_assets",
    "isaac_local_assets",
}
FORBIDDEN_WHEEL_SEGMENTS = FORBIDDEN_COMMON_SEGMENTS | {"tests", "test"}
FORBIDDEN_SUFFIXES = (".aria2", ".part", ".tmp", ".log")


class ArtifactError(RuntimeError):
    """Raised when a release artifact violates the packaging contract."""


def _read_project_version(root: Path) -> str:
    import tomllib

    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        version = project["version"]
    except (KeyError, OSError, tomllib.TOMLDecodeError) as exc:
        raise ArtifactError(f"could not read project version: {exc}") from exc
    if version != EXPECTED_VERSION:
        raise ArtifactError(f"project version is {version!r}, expected {EXPECTED_VERSION!r}")
    return version


def _git_status_porcelain(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ArtifactError(f"could not inspect git status: {completed.stderr.strip()}")
    return completed.stdout


def ensure_clean_tree(root: Path) -> None:
    status = _git_status_porcelain(root)
    if status:
        raise ArtifactError("canonical release manifest requires a clean git tree")


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ArtifactError(f"could not resolve git HEAD: {completed.stderr.strip()}")
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise ArtifactError("git HEAD is not a commit SHA")
    return commit


def _validate_commit(root: Path, commit: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise ArtifactError("git_commit must be a hexadecimal commit SHA")
    actual = _git_head(root)
    if commit != actual:
        raise ArtifactError(f"git_commit {commit} does not match HEAD {actual}")
    return commit


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(name: str, *, wheel: bool) -> str:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ArtifactError(f"absolute path in artifact: {name}")
    parts = [part for part in normalized.rstrip("/").split("/") if part]
    if ".." in parts or "" in parts:
        raise ArtifactError(f"unsafe path in artifact: {name}")
    forbidden = FORBIDDEN_WHEEL_SEGMENTS if wheel else FORBIDDEN_COMMON_SEGMENTS
    lowered = normalized.lower()
    if any(part.lower() in forbidden for part in parts):
        raise ArtifactError(f"forbidden path in artifact: {name}")
    if any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise ArtifactError(f"generated file in artifact: {name}")
    if any("secret" in part.lower() or "credential" in part.lower() for part in parts):
        raise ArtifactError(f"sensitive-looking file in artifact: {name}")
    return normalized


def _wheel_resource_name(name: str) -> str | None:
    marker = "/share/scene-factory/"
    if marker not in name:
        return None
    return "share/scene-factory/" + name.split(marker, 1)[1]


def _metadata_headers(metadata: str) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for line in metadata.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    return headers


def _require_members(members: Iterable[str], required: Iterable[str], *, label: str) -> None:
    available = set(members)
    missing = [path for path in required if path not in available]
    if missing:
        raise ArtifactError(f"{label} is missing required files: {', '.join(missing)}")


def _audit_wheel_content(path: Path) -> dict[str, object]:
    try:
        with zipfile.ZipFile(path) as archive:
            raw_names = archive.namelist()
            names = [_safe_member_name(name, wheel=True) for name in raw_names if not name.endswith("/")]
            if len(names) != len(set(names)):
                raise ArtifactError("wheel contains duplicate member names")
            dist_infos = sorted({name.split("/", 1)[0] for name in names if ".dist-info/METADATA" in name})
            if len(dist_infos) != 1 or not dist_infos[0].endswith(".dist-info"):
                raise ArtifactError("wheel must contain exactly one dist-info directory")
            dist_info = dist_infos[0]
            metadata_path = f"{dist_info}/METADATA"
            wheel_path = f"{dist_info}/WHEEL"
            record_path = f"{dist_info}/RECORD"
            entry_points_path = f"{dist_info}/entry_points.txt"
            _require_members(
                names,
                (metadata_path, wheel_path, record_path, entry_points_path),
                label="wheel",
            )
            metadata = _metadata_headers(archive.read(metadata_path).decode("utf-8"))
            if metadata.get("name") != ["scene-factory"]:
                raise ArtifactError("wheel metadata Name is not scene-factory")
            if metadata.get("version") != [EXPECTED_VERSION]:
                raise ArtifactError("wheel metadata Version is not 0.1.0")
            if metadata.get("requires-python") != [">=3.12"]:
                raise ArtifactError("wheel metadata Requires-Python is not >=3.12")
            license_values = metadata.get("license", []) + metadata.get("license-expression", [])
            if "MIT" not in license_values:
                raise ArtifactError("wheel metadata does not declare the MIT license")
            entry_points = archive.read(entry_points_path).decode("utf-8")
            for entry_point in ("scene-factory", "scene-factory-web"):
                if not re.search(rf"(?m)^{re.escape(entry_point)}\s*=", entry_points):
                    raise ArtifactError(f"wheel is missing console entry point: {entry_point}")
            resources = sorted(
                resource
                for name in names
                if (resource := _wheel_resource_name(name)) is not None
            )
            _require_members(resources, (f"share/scene-factory/{item}" for item in WHEEL_REQUIRED_FILES), label="wheel resources")
            if not any(name.rsplit("/", 1)[-1].upper() == "LICENSE" for name in names):
                raise ArtifactError("wheel does not contain a LICENSE file")
            if path.stat().st_size > MAX_WHEEL_BYTES:
                raise ArtifactError(f"wheel exceeds {MAX_WHEEL_BYTES} byte size gate")
            return {
                "metadata": "passed",
                "entry_points": "passed",
                "resources": "passed",
                "license": "passed",
                "forbidden_files": "passed",
                "size_gate": "passed",
            }
    except zipfile.BadZipFile as exc:
        raise ArtifactError(f"invalid wheel archive: {exc}") from exc


def _sdist_relative_name(name: str) -> str:
    normalized = _safe_member_name(name, wheel=False)
    parts = normalized.split("/")
    if len(parts) < 2:
        return parts[0]
    return "/".join(parts[1:])


def _audit_sdist_content(path: Path) -> dict[str, object]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            raw_names = [member.name for member in archive.getmembers() if member.isfile()]
            names = [_sdist_relative_name(name) for name in raw_names]
            if len(names) != len(set(names)):
                raise ArtifactError("sdist contains duplicate member names")
            _require_members(names, SDIST_REQUIRED_FILES, label="sdist")
            if path.stat().st_size > MAX_SDIST_BYTES:
                raise ArtifactError(f"sdist exceeds {MAX_SDIST_BYTES} byte size gate")
            return {
                "pyproject": "passed",
                "source": "passed",
                "resources": "passed",
                "license": "passed",
                "forbidden_files": "passed",
                "size_gate": "passed",
            }
    except (tarfile.ReadError, EOFError) as exc:
        raise ArtifactError(f"invalid sdist archive: {exc}") from exc


def audit_wheel(path: Path) -> dict[str, object]:
    if path.name != EXPECTED_WHEEL:
        raise ArtifactError(f"unexpected wheel filename: {path.name}")
    if not path.is_file():
        raise ArtifactError(f"wheel does not exist: {path}")
    return _audit_wheel_content(path)


def audit_sdist(path: Path) -> dict[str, object]:
    if path.name != EXPECTED_SDIST:
        raise ArtifactError(f"unexpected sdist filename: {path.name}")
    if not path.is_file():
        raise ArtifactError(f"sdist does not exist: {path}")
    return _audit_sdist_content(path)


def _find_artifacts(dist: Path) -> tuple[Path, Path]:
    if not dist.is_dir():
        raise ArtifactError(f"artifact directory does not exist: {dist}")
    wheel = dist / EXPECTED_WHEEL
    sdist = dist / EXPECTED_SDIST
    missing = [path.name for path in (wheel, sdist) if not path.is_file()]
    if missing:
        raise ArtifactError(f"artifact directory is missing: {', '.join(missing)}")
    unexpected = sorted(
        path.name
        for path in dist.iterdir()
        if path.is_file() and path.name not in {EXPECTED_WHEEL, EXPECTED_SDIST}
    )
    if unexpected:
        raise ArtifactError(f"unexpected files in artifact directory: {', '.join(unexpected)}")
    return wheel, sdist


def build_manifest(dist: Path, *, root: Path, git_commit: str | None = None) -> dict[str, object]:
    ensure_clean_tree(root)
    version = _read_project_version(root)
    commit = _validate_commit(root, git_commit or _git_head(root))
    wheel, sdist = _find_artifacts(dist)
    audit_wheel(wheel)
    audit_sdist(sdist)
    artifacts = []
    for path in sorted((wheel, sdist), key=lambda item: item.name):
        artifacts.append(
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {"release": version, "git_commit": commit, "artifacts": artifacts}


def write_manifest(manifest: dict[str, object], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums = output.parent / "SHA256SUMS.txt"
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    lines = [f"{item['sha256']}  {item['filename']}" for item in artifacts]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and describe SceneFactory v0.1.0 artifacts")
    parser.add_argument("--dist", type=Path, required=True, help="directory containing wheel and sdist")
    parser.add_argument("--git-commit", help="expected clean-checkout HEAD SHA")
    parser.add_argument("--output", type=Path, required=True, help="manifest JSON output path")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        manifest = build_manifest(args.dist, root=root, git_commit=args.git_commit)
        sums = write_manifest(manifest, args.output)
    except (ArtifactError, OSError, ValueError) as exc:
        print(json.dumps({"result": "failed", "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    print(json.dumps({"result": "passed", "manifest": manifest, "sha256sums": sums.name}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
