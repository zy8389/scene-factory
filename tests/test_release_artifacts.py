from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import release_artifacts


def _wheel_members() -> dict[str, bytes]:
    dist_info = "scene_factory-0.1.0.dist-info"
    members = {
        "scene_factory/__init__.py": b"__version__ = '0.1.0'\n",
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.4\nName: scene-factory\nVersion: 0.1.0\n"
            b"Requires-Python: >=3.12\nLicense: MIT\n"
        ),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n",
        f"{dist_info}/RECORD": b"",
        f"{dist_info}/entry_points.txt": (
            b"[console_scripts]\nscene-factory = scene_factory.cli:main\n"
            b"scene-factory-web = scene_factory.webapp:main\n"
        ),
        f"{dist_info}/licenses/LICENSE": b"MIT\n",
    }
    for relative in release_artifacts.WHEEL_REQUIRED_FILES:
        members[f"scene_factory-0.1.0.data/data/share/scene-factory/{relative}"] = b"resource\n"
    return members


def _sdist_members() -> dict[str, bytes]:
    root = "scene_factory-0.1.0"
    members = {
        f"{root}/MANIFEST.in": b"prune tests\n",
        f"{root}/pyproject.toml": b"[project]\nversion = '0.1.0'\n",
        f"{root}/README.md": b"# SceneFactory\n",
        f"{root}/LICENSE": b"MIT\n",
        f"{root}/scene_factory/__init__.py": b"__version__ = '0.1.0'\n",
    }
    for relative in release_artifacts.WHEEL_REQUIRED_FILES:
        members[f"{root}/{relative}"] = b"resource\n"
    return members


def _write_wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _write_sdist(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


class ReleaseArtifactTests(unittest.TestCase):
    def test_release_version_and_artifact_audits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_artifact_test_") as directory:
            root = Path(directory)
            dist = root / "dist"
            dist.mkdir()
            wheel = dist / release_artifacts.EXPECTED_WHEEL
            sdist = dist / release_artifacts.EXPECTED_SDIST
            _write_wheel(wheel, _wheel_members())
            _write_sdist(sdist, _sdist_members())
            with patch.object(release_artifacts, "_git_status_porcelain", return_value=""), patch.object(
                release_artifacts, "_git_head", return_value="1234567"
            ), patch.object(release_artifacts, "_read_project_version", return_value="0.1.0"):
                manifest = release_artifacts.build_manifest(dist, root=root, git_commit="1234567")
            self.assertEqual(manifest["release"], "0.1.0")
            self.assertEqual(manifest["git_commit"], "1234567")
            artifacts = manifest["artifacts"]
            self.assertEqual(
                [item["filename"] for item in artifacts],
                sorted((release_artifacts.EXPECTED_SDIST, release_artifacts.EXPECTED_WHEEL)),
            )
            self.assertTrue(all(len(item["sha256"]) == 64 for item in artifacts))

            output = root / "RELEASE_MANIFEST.json"
            sums = release_artifacts.write_manifest(manifest, output)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded, manifest)
            self.assertEqual(sums.name, "SHA256SUMS.txt")
            self.assertIn(release_artifacts.EXPECTED_WHEEL, sums.read_text(encoding="utf-8"))

    def test_dirty_tree_refuses_canonical_manifest(self) -> None:
        with patch.object(release_artifacts, "_git_status_porcelain", return_value=" M README.md"):
            with self.assertRaises(release_artifacts.ArtifactError):
                release_artifacts.ensure_clean_tree(Path("."))

    def test_wrong_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_artifact_test_") as directory:
            path = Path(directory) / "scene_factory-0.2.0-py3-none-any.whl"
            path.write_bytes(b"not a wheel")
            with self.assertRaises(release_artifacts.ArtifactError):
                release_artifacts.audit_wheel(path)

    def test_forbidden_wheel_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_artifact_test_") as directory:
            path = Path(directory) / release_artifacts.EXPECTED_WHEEL
            members = _wheel_members()
            members["tests/leaked.py"] = b"temporary\n"
            _write_wheel(path, members)
            with self.assertRaises(release_artifacts.ArtifactError):
                release_artifacts.audit_wheel(path)

    def test_forbidden_sdist_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_artifact_test_") as directory:
            path = Path(directory) / release_artifacts.EXPECTED_SDIST
            members = _sdist_members()
            members["scene_factory-0.1.0/tests/leaked.py"] = b"temporary\n"
            _write_sdist(path, members)
            with self.assertRaises(release_artifacts.ArtifactError):
                release_artifacts.audit_sdist(path)

    def test_unsafe_archive_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_artifact_test_") as directory:
            path = Path(directory) / release_artifacts.EXPECTED_SDIST
            members = _sdist_members()
            members["scene_factory-0.1.0/../secret.txt"] = b"secret\n"
            _write_sdist(path, members)
            with self.assertRaises(release_artifacts.ArtifactError):
                release_artifacts.audit_sdist(path)


if __name__ == "__main__":
    unittest.main()
