from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_URL = "https://ycb-benchmarks.s3.amazonaws.com/data/025_mug.tgz"
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "assets" / "source" / "ycb_025_mug"
DEFAULT_LICENSE = "YCB dataset license; verify source terms before redistribution"
_GEOMETRY_SUFFIXES = (".obj", ".ply", ".stl", ".dae", ".glb", ".gltf")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        if member.issym() or member.islnk():
            raise ValueError(f"archive contains unsupported link: {member.name}")
        member_path = Path(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"archive contains unsafe path: {member.name}")
        members.append(member)
    return members


def _find_geometry(root: Path) -> Path | None:
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _GEOMETRY_SUFFIXES
    ]
    candidates.sort(
        key=lambda path: (
            0 if path.name.lower() == "textured.obj" else 1,
            0 if path.name.lower() == "model.obj" else 1,
            len(path.parts),
            path.as_posix(),
        )
    )
    return candidates[0] if candidates else None


def _files_manifest(root: Path) -> list[dict[str, str | int]]:
    files: list[dict[str, str | int]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
        )
    return files


def _download(url: str, destination: Path, timeout: int) -> None:
    request = Request(url, headers={"User-Agent": "SceneFactory-YCB-import/1.0"})
    with urlopen(request, timeout=timeout) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_ycb_asset(
    *,
    archive_path: str | Path | None = None,
    source_url: str = DEFAULT_SOURCE_URL,
    source_dir: str | Path = DEFAULT_SOURCE_DIR,
    license_name: str = DEFAULT_LICENSE,
    report_path: str | Path | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Import a real YCB archive, or return a truthful blocked report.

    Extraction only happens after the archive passes ``tarfile`` validation. No
    placeholder geometry is ever written when download or validation fails.
    """
    destination = Path(source_dir).expanduser().resolve()
    report_file = Path(report_path).expanduser().resolve() if report_path else None
    report: dict[str, Any] = {
        "asset_id": "mug_001",
        "source_name": "YCB 025_mug",
        "source_url": source_url,
        "license": license_name,
        "status": "blocked",
        "result": "blocked",
        "source_dir": str(destination),
        "archive_sha256": None,
        "source_geometry": None,
        "source_files": [],
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "issues": [],
    }

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        if archive_path is None:
            temporary_directory = tempfile.TemporaryDirectory(prefix="scene_factory_ycb_")
            temporary_archive = Path(temporary_directory.name) / "025_mug.tgz"
            try:
                _download(source_url, temporary_archive, timeout_seconds)
            except (OSError, URLError, ValueError) as exc:
                report["issues"].append(
                    {
                        "code": "source_unavailable",
                        "message": f"could not download YCB 025_mug: {exc}",
                    }
                )
                _write_report(report_file, report)
                return report
            archive = temporary_archive
        else:
            archive = Path(archive_path).expanduser().resolve()
            if not archive.is_file():
                report["issues"].append(
                    {"code": "missing_archive", "message": f"archive does not exist: {archive}"}
                )
                _write_report(report_file, report)
                return report

        report["archive_sha256"] = _sha256(archive)
        with tarfile.open(archive, "r:*") as tar:
            members = _safe_members(tar)
            if not any(member.isfile() for member in members):
                raise ValueError("archive contains no regular files")
            with tempfile.TemporaryDirectory(prefix="scene_factory_ycb_extract_") as extraction:
                extraction_root = Path(extraction)
                tar.extractall(extraction_root, members=members, filter="data")
                geometry = _find_geometry(extraction_root)
                if geometry is None:
                    raise ValueError("archive contains no supported geometry file")
                if destination.exists():
                    raise FileExistsError(
                        f"refusing to overwrite existing source directory: {destination}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(extraction_root, destination)
                selected_geometry = destination / geometry.relative_to(extraction_root)
                files = _files_manifest(destination)
                source_manifest = {
                    "asset_id": "mug_001",
                    "source_name": "YCB 025_mug",
                    "source_url": source_url,
                    "license": license_name,
                    "archive_sha256": report["archive_sha256"],
                    "retrieved_at": report["retrieved_at"],
                    "source_geometry": str(selected_geometry.relative_to(destination).as_posix()),
                    "source_files": files,
                    "status": "imported",
                }
                (destination / "SOURCE.json").write_text(
                    json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                report.update(
                    {
                        "status": "imported",
                        "result": "passed",
                        "source_geometry": source_manifest["source_geometry"],
                        "source_files": files,
                    }
                )
    except (OSError, tarfile.TarError, ValueError, FileExistsError) as exc:
        report["issues"].append({"code": "invalid_ycb_archive", "message": str(exc)})
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()

    _write_report(report_file, report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import the real YCB 025_mug archive without creating substitute geometry."
    )
    parser.add_argument("--archive", type=Path, help="Existing local 025_mug.tgz archive")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--license", dest="license_name", default=DEFAULT_LICENSE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout < 1:
        raise ValueError("--timeout must be positive")
    report = import_ycb_asset(
        archive_path=args.archive,
        source_url=args.source_url,
        source_dir=args.source_dir,
        license_name=args.license_name,
        report_path=args.report,
        timeout_seconds=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
