from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import validate_mug_asset


class PhysxValidationTests(unittest.TestCase):
    def test_asset_report_maps_runtime_stages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usd = root / "mug_001.usda"
            collision = root / "mug_001_collision.usda"
            usd.write_text("#usda 1.0\n", encoding="utf-8")
            collision.write_text("#usda 1.0\n", encoding="utf-8")
            report_path = root / "qa" / "mug_001.json"
            work_dir = root / "work"

            def fake_run(command: list[str]) -> tuple[int, str, str]:
                if "validate_isaac_runtime.py" in command[1]:
                    runtime_path = Path(command[command.index("--report") + 1])
                    runtime_path.parent.mkdir(parents=True, exist_ok=True)
                    runtime_path.write_text(
                        json.dumps(
                            {
                                "valid": True,
                                "checks": {"stage_opened": True},
                                "collision": "passed",
                                "physics": "passed",
                            }
                        ),
                        encoding="utf-8",
                    )
                return 0, "", ""

            with patch("tools.validate_mug_asset._run", side_effect=fake_run):
                result = validate_mug_asset.main(
                    [
                        str(usd),
                        "--collision",
                        str(collision),
                        "--work-dir",
                        str(work_dir),
                        "--report",
                        str(report_path),
                    ]
                )
            self.assertEqual(result, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                (report["usd_load"], report["collision"], report["physics"]),
                ("passed", "passed", "passed"),
            )
            self.assertTrue(report["valid"])
            self.assertFalse(report["collision_generated"])

    def test_failed_runtime_cannot_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usd = root / "mug_001.usda"
            collision = root / "mug_001_collision.usda"
            usd.write_text("#usda 1.0\n", encoding="utf-8")
            collision.write_text("#usda 1.0\n", encoding="utf-8")
            report_path = root / "mug_001.json"

            def fake_run(command: list[str]) -> tuple[int, str, str]:
                if "validate_isaac_runtime.py" in command[1]:
                    runtime_path = Path(command[command.index("--report") + 1])
                    runtime_path.parent.mkdir(parents=True, exist_ok=True)
                    runtime_path.write_text(
                        json.dumps(
                            {
                                "valid": False,
                                "checks": {"stage_opened": True},
                                "collision": "failed",
                                "physics": "failed",
                            }
                        ),
                        encoding="utf-8",
                    )
                return 1 if "validate_isaac_runtime.py" in command[1] else 0, "", ""

            with patch("tools.validate_mug_asset._run", side_effect=fake_run):
                result = validate_mug_asset.main(
                    [str(usd), "--collision", str(collision), "--report", str(report_path)]
                )
            self.assertEqual(result, 2)
            self.assertFalse(json.loads(report_path.read_text(encoding="utf-8"))["valid"])


if __name__ == "__main__":
    unittest.main()
