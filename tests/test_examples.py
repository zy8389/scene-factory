from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ExampleWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.environment = os.environ.copy()
        self.environment["PYTHONNOUSERSITE"] = "1"
        self.environment["PYTHONPATH"] = str(self.root)

    def run_cli(self, cwd: Path, *arguments: str) -> dict[str, object] | None:
        completed = subprocess.run(
            [sys.executable, "-m", "scene_factory", *arguments],
            cwd=cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        if not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)

    def test_examples_run_from_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scene_factory_examples_") as directory:
            cwd = Path(directory)
            basic = self.run_cli(
                cwd,
                "build",
                "--recipe",
                "living_room_recent_snacking",
                "--seed",
                "42",
                "--output",
                str(cwd / "basic"),
            )
            self.assertTrue(basic and basic["valid"])

            intent = self.root / "examples" / "external_intent" / "scene.json"
            intent_report = self.run_cli(cwd, "intent", "validate", str(intent))
            self.assertTrue(intent_report and intent_report["valid"])
            external = self.run_cli(
                cwd,
                "build",
                "--intent",
                str(intent),
                "--seed",
                "42",
                "--output",
                str(cwd / "external"),
            )
            self.assertTrue(external and external["valid"])

            dataset = cwd / "dataset"
            batch = self.run_cli(
                cwd,
                "batch",
                "--recipe",
                "living_room_recent_snacking",
                "--count",
                "3",
                "--seed-start",
                "100",
                "--output",
                str(dataset),
            )
            self.assertEqual(batch, {"generated": 3, "valid": 3, "output": str(dataset)})
            self.assertTrue(self.run_cli(cwd, "dataset", "validate", str(dataset))["valid"])
            self.assertTrue(self.run_cli(cwd, "dataset", "reproduce", str(dataset))["valid"])

            scene = self.root / "examples" / "articulated_drawer" / "scene.json"
            plan = cwd / "plan.json"
            trace = cwd / "trace.json"
            planned = self.run_cli(
                cwd,
                "task",
                "plan",
                "--scene",
                str(scene),
                "--object",
                "drawer_1",
                "--state",
                "open",
                "--output",
                str(plan),
            )
            self.assertTrue(planned and planned["valid"])
            self.assertTrue(self.run_cli(cwd, "task", "validate", "--scene", str(scene), "--plan", str(plan))["valid"])
            executed = self.run_cli(
                cwd,
                "task",
                "execute",
                "--scene",
                str(scene),
                "--plan",
                str(plan),
                "--executor",
                "dry-run",
                "--output",
                str(trace),
            )
            self.assertTrue(executed and executed["valid"])
            self.assertTrue(
                self.run_cli(
                    cwd,
                    "task",
                    "execution-validate",
                    "--scene",
                    str(scene),
                    "--plan",
                    str(plan),
                    "--trace",
                    str(trace),
                )["valid"]
            )


if __name__ == "__main__":
    unittest.main()
