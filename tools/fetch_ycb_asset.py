from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DATASET_ID = "ai-habitat/ycb"
DATASET_REVISION = "29be64fdd95b4881f244152ad653058e0a48c28f"
DATASET_URL = f"https://huggingface.co/datasets/{DATASET_ID}"
LICENSE = "CC BY 4.0"
_BASE_URL = f"{DATASET_URL}/resolve/{DATASET_REVISION}"

ASSET_SPECS: dict[str, tuple[dict[str, str], ...]] = {
    "025_mug": (
        {
            "role": "visual",
            "dataset_path": "meshes/025_mug/google_16k/textured.glb",
            "relative_path": "textured.glb",
        },
        {
            "role": "collision",
            "dataset_path": "collision_meshes/025_cv_decomp.glb",
            "relative_path": "collision/025_cv_decomp.glb",
        },
    )
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _url_for(dataset_path: str, revision: str) -> str:
    return f"https://huggingface.co/datasets/{DATASET_ID}/resolve/{revision}/{dataset_path}?download=true"


def _download_file(url: str, destination: Path, *, timeout_seconds: int = 120) -> None:
    request = Request(url, headers={"User-Agent": "SceneFactory-YCB-fetch/1.0"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except (OSError, URLError) as exc:
        if os.name != "nt":
            raise
        # The Windows workstation has a system proxy that PowerShell can use,
        # while Python's urllib environment is intentionally proxy-free.
        command = "& { param($url, $output, $timeout) Invoke-WebRequest -Uri $url -OutFile $output -UseBasicParsing -MaximumRedirection 5 -TimeoutSec ([int]$timeout) }"
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command, url, str(destination), str(timeout_seconds)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.strip() or str(exc)) from exc
    if destination.stat().st_size == 0:
        raise ValueError(f"downloaded file is empty: {url}")
    prefix = destination.read_bytes()[:128]
    if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ValueError(f"download returned a Git LFS pointer instead of binary data: {url}")
    if b"<html" in prefix.lower() or b"<?xml" in prefix.lower():
        raise ValueError(f"download returned HTML/XML instead of binary data: {url}")
    if destination.suffix.lower() == ".glb" and prefix[:4] != b"glTF":
        raise ValueError(f"downloaded GLB has an invalid magic header: {url}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_ycb_asset(
    *,
    asset: str,
    output: str | Path,
    revision: str = DATASET_REVISION,
    force: bool = False,
    dry_run: bool = False,
    timeout_seconds: int = 120,
    download: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    if asset not in ASSET_SPECS:
        raise ValueError(f"unsupported YCB asset: {asset}; choices: {', '.join(ASSET_SPECS)}")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")

    destination = Path(output).expanduser().resolve()
    specs = ASSET_SPECS[asset]
    manifest_path = destination / "SOURCE.json"
    files = [destination / spec["relative_path"] for spec in specs]
    existing = [path for path in [manifest_path, *files] if path.exists()]
    if existing and not force and not dry_run:
        raise FileExistsError(
            "refusing to overwrite existing YCB files; pass --force: "
            + ", ".join(str(path) for path in existing)
        )

    retrieved_at = datetime.now(timezone.utc).isoformat()
    source_files: list[dict[str, Any]] = []
    for spec in specs:
        source_url = _url_for(spec["dataset_path"], revision)
        source_files.append(
            {
                "role": spec["role"],
                "dataset_path": spec["dataset_path"],
                "path": spec["relative_path"],
                "original_filename": Path(spec["dataset_path"]).name,
                "source_url": source_url,
                "sha256": None,
                "bytes": None,
            }
        )
    report: dict[str, Any] = {
        "asset_id": "mug_001",
        "source_name": "YCB 025_mug",
        "dataset": DATASET_ID,
        "source_url": source_files[0]["source_url"],
        "license": LICENSE,
        "license_source": DATASET_URL,
        "source_revision": revision,
        "original_filename": "textured.glb",
        "sha256": None,
        "source_geometry": "textured.glb",
        "source_collision_geometry": "collision/025_cv_decomp.glb",
        "source_files": source_files,
        "retrieved_at": retrieved_at,
        "status": "planned" if dry_run else "blocked",
        "result": "dry_run" if dry_run else "blocked",
        "output_dir": str(destination),
        "issues": [],
    }
    if dry_run:
        return report

    destination.mkdir(parents=True, exist_ok=True)
    downloader = download or (lambda url, path: _download_file(url, path, timeout_seconds=timeout_seconds))
    temporary_paths: list[Path] = []
    try:
        for entry, target in zip(source_files, files, strict=True):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.part")
            if temporary.exists():
                if not force:
                    raise FileExistsError(f"refusing to overwrite partial download: {temporary}")
                temporary.unlink()
            temporary_paths.append(temporary)
            downloader(entry["source_url"], temporary)
            temporary.replace(target)
            entry["sha256"] = _sha256(target)
            entry["bytes"] = target.stat().st_size

        report["sha256"] = source_files[0]["sha256"]
        report["status"] = "imported"
        report["result"] = "passed"
        _write_json(manifest_path, report)
    except (HTTPError, OSError, URLError, ValueError) as exc:
        report["issues"].append({"code": "source_unavailable", "message": str(exc)})
        for temporary in temporary_paths:
            if temporary.exists():
                temporary.unlink()
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch a real YCB asset from the pinned AI Habitat mirror."
    )
    parser.add_argument("--asset", default="025_mug", choices=sorted(ASSET_SPECS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", default=DATASET_REVISION)
    parser.add_argument("--force", action="store_true", help="Allow replacing existing files")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = fetch_ycb_asset(
            asset=args.asset,
            output=args.output,
            revision=args.revision,
            force=args.force,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
    except (FileExistsError, ValueError) as exc:
        print(json.dumps({"result": "blocked", "issues": [{"message": str(exc)}]}, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] in {"passed", "dry_run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
