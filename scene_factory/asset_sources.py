from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AssetSourceError(ValueError):
    pass


@dataclass(frozen=True)
class SourceFile:
    role: str
    relative_path: str
    source_url: str
    original_filename: str
    dataset_path: str | None = None


@dataclass(frozen=True)
class SourceCandidate:
    asset_id: str
    source_name: str
    source_url: str
    license: str | None
    source_revision: str | None
    files: tuple[SourceFile, ...]
    license_source: str | None = None

    @classmethod
    def from_dict(cls, asset_id: str, raw: dict[str, Any]) -> "SourceCandidate":
        files: list[SourceFile] = []
        for item in raw.get("files", []):
            url = str(item.get("source_url") or item.get("url") or "").strip()
            relative = str(item.get("path") or item.get("relative_path") or "").strip()
            if not url or not relative:
                raise AssetSourceError(f"source file for {asset_id} needs url and relative_path")
            files.append(
                SourceFile(
                    role=str(item.get("role", "visual")),
                    relative_path=relative,
                    source_url=url,
                    original_filename=str(item.get("original_filename") or Path(relative).name),
                    dataset_path=item.get("dataset_path"),
                )
            )
        if not files:
            raise AssetSourceError(f"source candidate for {asset_id} has no files")
        return cls(
            asset_id=asset_id,
            source_name=str(raw.get("source_name") or asset_id),
            source_url=str(raw.get("source_url") or files[0].source_url),
            license=raw.get("license"),
            source_revision=raw.get("source_revision") or raw.get("revision"),
            files=tuple(files),
            license_source=raw.get("license_source"),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(url: str, destination: Path, timeout_seconds: int = 120) -> None:
    request = Request(url, headers={"User-Agent": "SceneFactory-AssetFetcher/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except (OSError, URLError) as exc:
        if os.name != "nt":
            raise
        command = (
            "& { param($url,$output,$timeout); "
            "Invoke-WebRequest -Uri $url -OutFile $output -UseBasicParsing "
            "-MaximumRedirection 5 -TimeoutSec ([int]$timeout) }"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command, url, str(destination), str(timeout_seconds)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.strip() or str(exc)) from exc
    if not destination.is_file() or destination.stat().st_size == 0:
        raise AssetSourceError(f"downloaded source is empty: {url}")
    prefix = destination.read_bytes()[:128]
    if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise AssetSourceError(f"download returned a Git LFS pointer: {url}")
    if b"<html" in prefix.lower() or b"<?xml" in prefix.lower():
        raise AssetSourceError(f"download returned HTML/XML: {url}")
    if destination.suffix.lower() == ".glb" and prefix[:4] != b"glTF":
        raise AssetSourceError(f"downloaded GLB has invalid magic: {url}")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _portable_output_dir(path: Path) -> str:
    """Avoid recording a developer-specific drive in committed manifests."""
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class AssetSourceResolver:
    """Resolve and fetch immutable source candidates without touching USD logic."""

    def __init__(self, candidates: dict[str, Iterable[SourceCandidate]]) -> None:
        self._candidates = {asset_id: tuple(items) for asset_id, items in candidates.items()}

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "AssetSourceResolver":
        candidates: dict[str, list[SourceCandidate]] = {}
        for item in config.get("assets", []):
            asset_id = str(item["asset_id"])
            candidates[asset_id] = [
                SourceCandidate.from_dict(asset_id, source)
                for source in item.get("preferred_sources", [])
            ]
        return cls(candidates)

    def candidates(self, asset_id: str) -> tuple[SourceCandidate, ...]:
        return self._candidates.get(asset_id, ())

    def resolve(self, asset_id: str) -> SourceCandidate:
        options = self.candidates(asset_id)
        if not options:
            raise AssetSourceError(f"no compliant source candidate configured for {asset_id}")
        for candidate in options:
            if candidate.license and candidate.license.lower() not in {"unknown", "unverified"}:
                return candidate
        raise AssetSourceError(f"all source candidates for {asset_id} have no verified license")

    def fetch(
        self,
        asset_id: str,
        output: str | Path,
        *,
        force: bool = False,
        dry_run: bool = False,
        downloader: Callable[[str, Path], None] | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        try:
            candidate = self.resolve(asset_id)
        except AssetSourceError as exc:
            return {"asset_id": asset_id, "status": "blocked", "result": "blocked", "issues": [{"code": "source_unresolved", "message": str(exc)}]}
        destination = Path(output).expanduser().resolve()
        manifest_path = destination / "SOURCE.json"
        files = [destination / item.relative_path for item in candidate.files]
        report: dict[str, Any] = {
            "asset_id": asset_id,
            "source_name": candidate.source_name,
            "source_url": candidate.source_url,
            "license": candidate.license,
            "license_source": candidate.license_source,
            "source_revision": candidate.source_revision,
            "original_filename": candidate.files[0].original_filename,
            "source_geometry": candidate.files[0].relative_path,
            "source_collision_geometry": next((item.relative_path for item in candidate.files if item.role == "collision"), None),
            "source_files": [
                {
                    "role": item.role,
                    "dataset_path": item.dataset_path,
                    "path": item.relative_path,
                    "original_filename": item.original_filename,
                    "source_url": item.source_url,
                }
                for item in candidate.files
            ],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "output_dir": _portable_output_dir(destination),
            "status": "planned" if dry_run else "blocked",
            "result": "dry_run" if dry_run else "blocked",
            "issues": [],
        }
        if dry_run:
            return report
        if not candidate.license:
            report["issues"].append({"code": "missing_license", "message": "source license is not verified"})
            return report
        if manifest_path.is_file() and not force and all(path.is_file() for path in files):
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = [entry.get("sha256") for entry in existing.get("source_files", [])]
            actual = [sha256_file(path) for path in files]
            if expected and expected == actual:
                existing["idempotent"] = True
                existing["output_dir"] = _portable_output_dir(destination)
                _write_json_atomic(manifest_path, existing)
                return existing
            report["issues"].append({"code": "hash_mismatch", "message": "existing source hash differs; use --force"})
            return report
        existing = [path for path in [manifest_path, *files] if path.exists()]
        if existing and not force:
            report["issues"].append({"code": "source_exists", "message": "existing source files require --force"})
            return report
        downloader = downloader or (lambda url, path: _download_file(url, path, timeout_seconds))
        temporary_paths: list[Path] = []
        try:
            for entry, target in zip(report["source_files"], files, strict=True):
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.part")
                if temporary.exists():
                    temporary.unlink()
                temporary_paths.append(temporary)
                downloader(entry["source_url"], temporary)
                temporary.replace(target)
                entry["sha256"] = sha256_file(target)
                entry["bytes"] = target.stat().st_size
            report["sha256"] = report["source_files"][0]["sha256"]
            report["status"] = "imported"
            report["result"] = "passed"
            _write_json_atomic(manifest_path, report)
        except (HTTPError, OSError, URLError, AssetSourceError, ValueError) as exc:
            report["issues"].append({"code": "source_unavailable", "message": str(exc)})
            for temporary in temporary_paths:
                if temporary.exists():
                    temporary.unlink()
        return report
