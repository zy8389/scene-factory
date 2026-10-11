"""Web scheduling/provenance tests use simulator doubles, never physical claims."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scene_factory.dataset import DatasetResult, sha256_file, write_json_atomic
from scene_factory.web_runtime import WebRuntimeManager, _sampling_plan, _tail_frame


class WebRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="scene-web-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.python = self.root / "python.exe"
        self.python.write_bytes(b"simulator double, not a Python interpreter")
        self.assets = self.root / "assets"
        robot = self.assets / "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
        robot.parent.mkdir(parents=True)
        robot.write_text("#usda 1.0", encoding="utf-8")
        self.factory = object()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("scene_factory.web_runtime.build_sampling_collection",
                                      return_value=DatasetResult("passed", True, {})))
        self.managers: list[WebRuntimeManager] = []
        self.addCleanup(self._close)

    def _close(self) -> None:
        for manager in self.managers:
            manager.close()
            manager._thread.join(timeout=3)

    def manager(self, **kwargs) -> WebRuntimeManager:
        manager = WebRuntimeManager(kwargs.pop("root", self.root / "web"), self.factory,
                                    isaac_python=self.python, asset_root=self.assets, **kwargs)
        self.managers.append(manager)
        return manager

    @staticmethod
    def _write_batch(output: Path, *, failed: bool = False, state: str = "complete") -> dict:
        output.mkdir(parents=True, exist_ok=True)
        cases = []
        for event in ("lift", "place"):
            identifier = "a" * 20 + "-" + event
            case_root = output / "cases" / identifier
            (case_root / "task").mkdir(parents=True, exist_ok=True)
            write_json_atomic(case_root / "layout.json", {"scene_id": "test-scene", "objects": []})
            (case_root / "task/task_trace.jsonl").write_text('{"step":0}\n', encoding="utf-8")
            cases.append({"case_id": identifier, "state": state,
                          "configuration_fingerprint": "a" * 64, "scene_id": "test-scene",
                          "event": "robot_mug_lift" if event == "lift" else "robot_mug_pick_place",
                          "source": {"record": {"seed": 1004}},
                          "physics_status": "passed", "task_status": "failed" if failed else "passed",
                          "evidence_complete": True,
                          "failure_reasons": ["grasp_failure"] if failed else []})
        manifest = {"state": state, "limit_layouts": 1, "cases": cases,
                    "settings": {"settle_steps": 240, "max_steps": 720},
                    "summary": {"task_status_counts": {"passed": 2}}}
        write_json_atomic(output / "manifest.json", manifest)
        (output / "index.html").write_text("<h1>Test double report</h1>", encoding="utf-8")
        return {**manifest, "valid": True}

    def _wait(self, manager: WebRuntimeManager, identifier: str, statuses: set[str] | None = None) -> dict:
        statuses = statuses or {"completed", "failed", "interrupted"}
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = manager.get_job(identifier)
            if record["status"] in statuses:
                return record
            time.sleep(0.01)
        self.fail("web worker did not reach the expected state")

    def _history(self, manager: WebRuntimeManager) -> dict:
        return self._wait(manager, manager.list_jobs()[0]["job_id"])

    def test_strict_payload_and_source_plan(self) -> None:
        manager = self.manager()
        for limit in (True, 0, 21, 1.0, "1", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                manager.create({"limit_layouts": limit})
        for payload in ({"profile": []}, {"profile": "unknown"}, {"runtime_root": "C:/"},
                        {"label": ""}, {"label": 123}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                manager.create(payload)
        for profile in ("baseline", "tracking"):
            plan = _sampling_plan(profile)
            self.assertEqual(len(plan["strata"]), 4)
            self.assertEqual([entry["seed_start"] for entry in plan["strata"]], [1000, 2000, 1000, 2000])
            self.assertEqual(sum(entry["quota"] for entry in plan["strata"]), 40)
            self.assertTrue(all(entry["candidate_count"] == 40 for entry in plan["strata"]))
            self.assertTrue(all(("tracking" in entry["recipe"]) == (profile == "tracking")
                                for entry in plan["strata"]))

    def test_queue_is_serial_and_close_preserves_active_evidence(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls: list[Path] = []

        def run(collection, output, **kwargs):
            calls.append(Path(output))
            if len(calls) == 1:
                started.set()
                release.wait(timeout=3)
            return self._write_batch(Path(output))

        self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch", side_effect=run))
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager()
        first = manager.create({})
        self.assertTrue(started.wait(timeout=2))
        second = manager.create({})
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertTrue(manager.has_active_jobs)
        release.set()
        self._wait(manager, first["job_id"])
        self._wait(manager, second["job_id"])
        self.assertEqual([path.parent.name for path in calls], [first["job_id"], second["job_id"]])
        self.assertFalse(manager.has_active_jobs)

    def test_task_failure_is_a_completed_valid_run(self) -> None:
        self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch",
                                      side_effect=lambda collection, output, **kw: self._write_batch(Path(output), failed=True)))
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager()
        record = self._wait(manager, manager.create({})["job_id"])
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["validation"]["valid"])
        self.assertEqual(record["summary"]["task_status_counts"], {"failed": 2})
        self.assertEqual(record["summary"]["evidence_complete_count"], 2)
        self.assertEqual(record["cases"][0]["failure_reasons"], ["grasp_failure"])

    def test_exception_does_not_block_the_next_queued_job(self) -> None:
        count = 0

        def run(collection, output, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("worker bootstrap failed")
            return self._write_batch(Path(output))

        self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch", side_effect=run))
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager()
        first = manager.create({})
        second = manager.create({})
        record = self._wait(manager, first["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("bootstrap", record["error"])
        self.assertEqual(self._wait(manager, second["job_id"])["status"], "completed")

    def test_real_validator_rejects_fabricated_pass_claims(self) -> None:
        self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch",
                                      side_effect=lambda collection, output, **kw: self._write_batch(Path(output))))
        manager = self.manager()
        record = self._wait(manager, manager.create({})["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["validation"]["valid"])
        self.assertTrue(record["validation"]["errors"])
        self.assertNotIn("passed", record["summary"]["task_status_counts"])
        self.assertEqual(record["summary"]["evidence_complete_count"], 0)
        self.assertFalse(any(case["replay_available"] for case in record["cases"]))
        self.assertIsNone(record["report_url"])

    def test_running_claims_wait_for_independent_validation(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def run(collection, output, **kwargs):
            result = self._write_batch(Path(output))
            started.set()
            release.wait(timeout=3)
            return result

        self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch", side_effect=run))
        validator = self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                                  return_value={"valid": True, "errors": []}))
        manager = self.manager()
        job = manager.create({})
        self.assertTrue(started.wait(timeout=2))
        record = manager.get_job(job["job_id"])
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["summary"]["task_status_counts"], {"pending_validation": 2})
        self.assertIsNone(record["validation"]["valid"])
        self.assertEqual(record["summary"]["evidence_complete_count"], 0)
        self.assertIsNone(record["report_url"])
        validator.assert_not_called()
        release.set()
        self.assertEqual(self._wait(manager, job["job_id"])["status"], "completed")

    def test_history_is_read_only_and_changed_trace_is_revalidated(self) -> None:
        history = self.root / "historic"
        self._write_batch(history)
        hashes = {path.relative_to(history).as_posix(): sha256_file(path)
                  for path in history.rglob("*") if path.is_file()}
        validator = self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                                  return_value={"valid": True, "errors": []}))
        manager = self.manager(history_roots=(history,))
        record = self._history(manager)
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["history"])
        self.assertEqual(validator.call_count, 1)
        data = manager.case_data(record["job_id"], record["cases"][0]["case_id"])
        self.assertEqual(data["scene"]["scene_id"], "test-scene")
        self.assertTrue(data["trace_path"].is_file())
        self.assertEqual(hashes, {path.relative_to(history).as_posix(): sha256_file(path)
                                for path in history.rglob("*") if path.is_file()})
        validator.return_value = {"valid": False, "errors": ["trace hash mismatch"]}
        data["trace_path"].write_text('{"step":0,"modified":true}\n', encoding="utf-8")
        changed = manager.get_job(record["job_id"])
        self.assertEqual(validator.call_count, 2)
        self.assertEqual(changed["status"], "failed")
        self.assertNotIn("passed", changed["summary"]["task_status_counts"])

    def test_paths_and_unregistered_jobs_are_rejected(self) -> None:
        history = self.root / "historic"
        self._write_batch(history)
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager(history_roots=(history,))
        record = self._history(manager)
        self.assertEqual(manager.resolve_file(record["job_id"], "index.html"), history / "index.html")
        for relative in ("../python.exe", "cases/../../python.exe", "C:\\private.txt", "/etc/passwd",
                         "..\\python.exe", "index.html:stream", "", "./index.html"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                manager.resolve_file(record["job_id"], relative)
        with self.assertRaises(ValueError):
            manager.resolve_file("job-" + "f" * 32, "index.html")
        with self.assertRaises(ValueError):
            manager.case_data(record["job_id"], "../elsewhere")
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = history / "external.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Windows without developer-mode symlink privilege.
        with self.assertRaises(ValueError):
            manager.resolve_file(record["job_id"], "external.txt")

    def test_restart_marks_unfinished_jobs_interrupted_without_launching(self) -> None:
        manager = self.manager()
        identifier = "job-" + "a" * 32
        directory = manager.root / "jobs" / identifier
        directory.mkdir()
        record = manager._new_record(identifier, "unfinished", "tracking", 1, "2026-10-11T00:00:00Z")
        record["status"] = "running"
        write_json_atomic(directory / "job.json", {"schema_version": "scene_factory.web_runtime_job.v1", **record})
        manager.close()
        manager._thread.join(timeout=1)
        runner = self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch"))
        restored = self.manager()
        result = restored.get_job(identifier)
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(restored.has_active_jobs)
        runner.assert_not_called()
        self.assertEqual(json.loads((directory / "job.json").read_text(encoding="utf-8"))["status"], "interrupted")

    def test_close_allows_current_run_to_finish_but_cancels_queued_start(self) -> None:
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def run(collection, output, **kwargs):
            started.set()
            release.wait(timeout=3)
            return self._write_batch(Path(output))

        runner = self.stack.enter_context(patch("scene_factory.web_runtime.run_runtime_batch", side_effect=run))
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager()
        first = manager.create({})
        self.assertTrue(started.wait(timeout=2))
        second = manager.create({})
        manager.close()
        self.assertEqual(manager.get_job(second["job_id"])["status"], "interrupted")
        self.assertTrue(manager.has_active_jobs)
        with self.assertRaises(ValueError):
            manager.create({})
        release.set()
        self.assertEqual(self._wait(manager, first["job_id"])["status"], "completed")
        manager._thread.join(timeout=2)
        self.assertEqual(runner.call_count, 1)

    def test_bounded_progress_ignores_partial_last_row(self) -> None:
        trace = self.root / "trace.jsonl"
        trace.write_text('{"step":5}\n{"step":6}\n{"step":', encoding="utf-8")
        self.assertEqual(_tail_frame(trace)["step"], 6)
        trace.write_bytes(b'x' * 100000 + b'\n{"step":100}\n{"step":')
        self.assertEqual(_tail_frame(trace)["step"], 100)

    def test_same_root_cannot_have_two_queue_owners(self) -> None:
        first = self.manager()
        with self.assertRaisesRegex(ValueError, "占用"):
            self.manager()
        first.close()
        first._thread.join(timeout=2)
        second = self.manager()
        self.assertFalse(second.has_active_jobs)

    def test_deleted_final_manifest_removes_success_claims(self) -> None:
        history = self.root / "historic"
        self._write_batch(history)
        validator = self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                                  return_value={"valid": True, "errors": []}))
        manager = self.manager(history_roots=(history,))
        record = self._history(manager)
        self.assertEqual(record["status"], "completed")
        (history / "manifest.json").unlink()
        validator.return_value = {"valid": False, "errors": ["manifest missing"]}
        record = manager.get_job(record["job_id"])
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["validation"]["valid"])
        self.assertEqual(record["summary"]["evidence_complete_count"], 0)

    def test_unavailable_isaac_still_allows_history(self) -> None:
        history = self.root / "historic"
        self._write_batch(history)
        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch",
                                      return_value={"valid": True, "errors": []}))
        manager = self.manager(history_roots=(history,))
        manager.isaac_python = self.root / "missing.exe"
        self.assertFalse(manager.config()["available"])
        self.assertEqual(len(manager.list_jobs()), 1)
        self.assertEqual(self._history(manager)["summary"]["task_status_counts"], {"passed": 2})

    def test_history_validation_is_async_and_api_stays_responsive(self) -> None:
        history = self.root / "historic"
        self._write_batch(history)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def validate(output):
            started.set()
            release.wait(timeout=3)
            return {"valid": True, "errors": []}

        self.stack.enter_context(patch("scene_factory.web_runtime.validate_runtime_batch", side_effect=validate))
        manager = self.manager(history_roots=(history,))
        self.assertTrue(started.wait(timeout=2))
        self.assertTrue(manager.config()["available"])
        record = manager.list_jobs()[0]
        self.assertEqual(record["status"], "validating")
        self.assertIsNone(record["validation"]["valid"])
        self.assertNotIn("passed", record["summary"]["task_status_counts"])
        release.set()
        self.assertEqual(self._wait(manager, record["job_id"])["status"], "completed")
