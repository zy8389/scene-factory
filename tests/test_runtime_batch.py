"""Parent orchestration tests use fabricated workers, never a simulator.

Their trace evaluators are explicitly replaced: a passing assertion here is
evidence about process/provenance handling, not physical or robot performance.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scene_factory import SceneFactory
from scene_factory.dataset import DatasetError, sha256_file, write_json_atomic
from scene_factory.runtime_batch import normalize_runtime_settings, run_runtime_batch, validate_runtime_batch
from scene_factory.sampling import SAMPLING_PLAN_SCHEMA_VERSION, build_sampling_collection


class RuntimeBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(os.environ, {"SCENE_FACTORY_LLM_MODE": "off"}):
            cls.factory = SceneFactory()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-batch-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.collection = self.root / "collection"
        plan = {"schema_version": SAMPLING_PLAN_SCHEMA_VERSION, "strata": [
            {"id": "lift", "recipe": "kitchen_franka_mug_lift_data", "quota": 4,
             "candidate_count": 4, "seed_start": 1000},
            {"id": "place", "recipe": "kitchen_franka_mug_pick_place_data", "quota": 4,
             "candidate_count": 4, "seed_start": 1000},
        ]}
        result = build_sampling_collection(plan, self.collection, factory=self.factory)
        self.assertTrue(result.valid, result.to_dict())
        self.output = self.root / "output"
        self.robot = self.root / "robot" / "franka.usd"
        self.robot.parent.mkdir()
        self.robot.write_text("#usda 1.0\ndef Xform \"FakeTestRobot\" {}\n", encoding="utf-8")
        self.settings = {"robot_asset_path": str(self.robot)}
        self.behavior = "pass"
        self.jobs: list[dict] = []
        self.live_process_parents: dict[int, int] = {}
        self.source_hashes = self._hashes(self.collection)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("scene_factory.runtime_batch._code_fingerprint", return_value="test-double-code"))
        stack.enter_context(patch("scene_factory.runtime_batch.export_usd_with_isaac", side_effect=self._export))
        stack.enter_context(patch("scene_factory.runtime_batch.subprocess.Popen", side_effect=self._spawn))
        stack.enter_context(patch("scene_factory.runtime_batch._process_parent_snapshot", side_effect=lambda: dict(self.live_process_parents)))
        stack.enter_context(patch("scene_factory.runtime_evidence.assess_physics_trace", side_effect=self._physics))
        stack.enter_context(patch("scene_factory.runtime_evidence.assess_task_trace", side_effect=self._task))

    @staticmethod
    def _hashes(root: Path) -> dict[str, str]:
        return {path.relative_to(root).as_posix(): sha256_file(path) for path in root.rglob("*") if path.is_file()}

    @staticmethod
    def _export(layout, registry, output, *, isaac_python):
        # This is intentionally not a physically valid scene.
        Path(output).write_text("#usda 1.0\ndef Xform \"TestDoubleScene\" {}\n", encoding="utf-8")

    def _physics(self, scene, samples, settings):
        self.assertIsInstance(scene, dict)
        self.assertEqual(samples[0]["kind"], "test-double-physics")
        status = "failed" if self.behavior == "physics_failure" else "passed"
        return {"status": status, "checks": {"test_double_only": True},
                "metrics": {"evidence_complete": True},
                "failure_reasons": ["test_double_physics_failure"] if status == "failed" else []}

    def _task(self, scene, samples, settings):
        self.assertIsInstance(scene, dict)
        self.assertEqual(samples[0]["kind"], "test-double-task")
        status = "failed" if self.behavior == "task_failure" else "passed"
        return {"status": status, "checks": {"test_double_only": True},
                "metrics": {"evidence_complete": True, "observed_success": status == "passed"},
                "failure_reasons": ["test_double_task_failure"] if status == "failed" else []}

    def _spawn(self, command, **options):
        job = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        self.jobs.append(job)
        directory = Path(job["output_dir"])
        self.assertEqual(options["cwd"], str(directory))
        self.assertTrue(str(directory).isascii())
        self.assertIn("SCENE_FACTORY_HOME", options["env"])
        launcher_pid = 900000 + len(self.jobs)
        proxy = self.behavior in {"windows_proxy", "windows_proxy_wrong_parent"}
        pid = 800000 + len(self.jobs) if proxy else launcher_pid
        parent_pid = launcher_pid if proxy else os.getpid()
        self.live_process_parents[launcher_pid] = os.getpid()
        self.live_process_parents[pid] = 700000 if self.behavior == "windows_proxy_wrong_parent" else parent_pid
        trace = directory / f"{job['mode']}_trace.jsonl"
        trace.write_text("".join(json.dumps({"kind": f"test-double-{job['mode']}", "step": index}) + "\n"
                                 for index in range(2)), encoding="utf-8")
        summary = {"official_isaac_asset": True, "robot_asset_source": "nucleus_franka_usd"}
        if job["mode"] == "physics":
            settled = directory / "settled.usda"
            settled.write_text("#usda 1.0\ndef Xform \"SettledTestDouble\" {}\n", encoding="utf-8")
            summary.update({"settled_usd_path": str(settled), "settled_usd_sha256": sha256_file(settled),
                            "settled_velocity_cleared": True})
        report = {"schema_version": "scene_factory.runtime_worker.v1", "run_id": job["run_id"],
                  "case_id": job["case_id"], "mode": job["mode"], "bindings": job["bindings"],
                  "pid": pid, "parent_pid": parent_pid, "isaac_sim_version": "test-double-version", "completed_steps": 1,
                  "trace_path": str(trace), "trace_sha256": sha256_file(trace),
                  "result": "completed" if job["mode"] == "physics" else "task_success",
                  "failure_reason": None, "runtime_summary": summary, "worker_exit_code": 0,
                  "observed_code_sha256": job["bindings"]["code_sha256"],
                  "final_code_sha256": job["bindings"]["code_sha256"],
                  "observed_inputs": {"layout_sha256": job["bindings"]["input_sha256"]["layout"],
                                      "usd_sha256": job["bindings"]["usd_sha256"]},
                  "final_inputs": {"layout_sha256": job["bindings"]["input_sha256"]["layout"],
                                   "usd_sha256": job["bindings"]["usd_sha256"]},
                  "observed_robot_asset_sha256": sha256_file(self.robot),
                  "final_robot_asset_sha256": sha256_file(self.robot),
                  "settings": job["settings"], "runtime_assets": job["runtime_assets"],
                  "robot_asset_source": "nucleus_franka_usd", "loaded_asset_sha256": sha256_file(self.robot)}
        identity = {"schema_version": "scene_factory.runtime_identity.v1", "run_id": job["run_id"],
                    "case_id": job["case_id"], "mode": job["mode"], "pid": pid, "parent_pid": parent_pid,
                    "bindings": job["bindings"], "job_sha256": sha256_file(command[-1]),
                    "observed_code_sha256": job["bindings"]["code_sha256"]}
        identity_path = directory / "worker_identity.json"
        if self.behavior != "missing_identity":
            write_json_atomic(identity_path, identity)
        report["worker_identity_sha256"] = sha256_file(identity_path) if identity_path.is_file() else None
        if self.behavior == "wrong_bindings":
            report["bindings"] = {**job["bindings"], "code_sha256": "wrong"}
        if self.behavior == "wrong_observed_inputs":
            report["observed_inputs"]["layout_sha256"] = "wrong"
        if self.behavior == "wrong_final_robot":
            report["final_robot_asset_sha256"] = "wrong"
        if self.behavior == "logical_worker_error":
            report["result"] = "worker_error"
            report["worker_exit_code"] = 1
        if self.behavior == "task_failure" and job["mode"] == "task":
            report["result"] = "task_failure"
            report["failure_reason"] = "test_double_task_failure"
        if self.behavior != "missing_report":
            write_json_atomic(directory / "worker_report.json", report)
        options["stdout"].write(b"fabricated worker output for orchestration tests\n")
        options["stderr"].write(b"")
        behavior = self.behavior

        class FakeProcess:
            def __init__(self):
                self.pid = launcher_pid
                self.returncode = 1 if behavior == "nonzero_exit" else 0

            def wait(self, timeout=None):
                if behavior == "timeout" and self.returncode == 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                return self.returncode

        return FakeProcess()

    def _run(self, **kwargs):
        return run_runtime_batch(self.collection, self.output, isaac_python="unused-test-python",
                                 settings=kwargs.pop("settings", self.settings), limit_layouts=kwargs.pop("limit_layouts", 1),
                                 **kwargs)

    def test_paired_selection_frozen_sources_and_settled_stage_bindings(self):
        report = self._run(limit_layouts=2)
        self.assertTrue(report["valid"])
        self.assertTrue(report["validation"]["valid"], report["validation"])
        self.assertEqual(report["summary"]["case_count"], 4)
        self.assertEqual(report["summary"]["layout_count"], 2)
        self.assertEqual(report["summary"]["evidence_complete_count"], 4)
        self.assertEqual(len(self.jobs), 8)
        self.assertEqual(self.source_hashes, self._hashes(self.collection))
        self.assertEqual([job["mode"] for job in self.jobs], ["physics", "task"] * 4)
        for physics, task in zip(self.jobs[::2], self.jobs[1::2]):
            self.assertEqual(Path(task["usd_path"]), Path(physics["output_dir"]) / "settled.usda")
            self.assertEqual(task["bindings"]["usd_sha256"], sha256_file(task["usd_path"]))
            self.assertNotEqual(physics["bindings"]["usd_sha256"], task["bindings"]["usd_sha256"])
        self.assertTrue((self.output / "index.html").is_file())

    def test_resume_preserves_completed_evidence_and_extends_layout_pairs(self):
        initial = self._run(limit_layouts=1)
        case_hashes = self._hashes(self.output / "cases")
        resumed = self._run(limit_layouts=1, resume=True)
        self.assertEqual(len(self.jobs), 4)
        self.assertEqual(case_hashes, self._hashes(self.output / "cases"))
        self.assertEqual(initial["run_id"], resumed["run_id"])
        extended = self._run(limit_layouts=4, resume=True)
        self.assertEqual(extended["summary"]["case_count"], 8)
        self.assertEqual(len(self.jobs), 16)
        self.assertTrue(extended["validation"]["valid"], extended["validation"])

    def test_legitimate_failed_task_is_complete_evidence_and_valid_offline(self):
        self.behavior = "task_failure"
        report = self._run()
        self.assertTrue(report["validation"]["valid"], report["validation"])
        self.assertFalse(report["validation"]["all_task_pass"])
        self.assertEqual(report["summary"]["task_status_counts"], {"failed": 2})
        self.assertEqual(report["summary"]["evidence_complete_count"], 2)

    def test_physics_failure_skips_task_and_retains_reason(self):
        self.behavior = "physics_failure"
        report = self._run()
        self.assertTrue(report["validation"]["valid"], report["validation"])
        self.assertEqual(len(self.jobs), 2)
        self.assertEqual(report["summary"]["task_status_counts"], {"not_verified": 2})
        result = json.loads((self.output / "cases" / report["cases"][0]["case_id"] / "result.json").read_text("utf-8"))
        self.assertEqual(result["stages"]["task"]["reason"], "physics_not_passed")

    def test_missing_report_never_becomes_a_pass(self):
        self.behavior = "missing_report"
        report = self._run()
        self.assertFalse(report["validation"]["valid"])
        self.assertEqual(report["summary"]["physics_status_counts"], {"not_verified": 2})
        self.assertEqual(report["summary"]["evidence_complete_count"], 0)
        self.assertEqual(len(self.jobs), 2)

    def test_os_nonzero_exit_rejects_an_apparently_successful_report(self):
        self.behavior = "nonzero_exit"
        report = self._run()
        self.assertFalse(report["validation"]["valid"])
        self.assertEqual(report["summary"]["physics_status_counts"], {"not_verified": 2})
        self.assertIn("worker_exit_not_zero", " ".join(report["validation"]["errors"]))

    def test_worker_logical_error_rejects_fast_shutdown_os_zero(self):
        self.behavior = "logical_worker_error"
        report = self._run()
        self.assertFalse(report["validation"]["valid"])
        self.assertEqual(report["summary"]["evidence_complete_count"], 0)

    def test_report_binding_mismatch_is_not_evaluated_as_pass(self):
        self.behavior = "wrong_bindings"
        report = self._run()
        self.assertFalse(report["validation"]["valid"])
        self.assertIn("worker_report_bindings_mismatch", " ".join(report["validation"]["errors"]))

    def test_actual_worker_input_and_robot_hashes_must_match_frozen_bindings(self):
        for behavior in ("wrong_observed_inputs", "wrong_final_robot"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                self.output = self.root / behavior
                report = self._run()
                self.assertFalse(report["valid"])
                self.assertEqual(report["summary"]["evidence_complete_count"], 0)

    def test_windows_launcher_worker_pid_split_uses_independently_observed_lineage(self):
        self.behavior = "windows_proxy"
        with patch("scene_factory.runtime_batch._process_platform", return_value="nt"):
            report = self._run()
        self.assertTrue(report["valid"], report["validation"])
        for job in self.jobs:
            parent = json.loads((Path(job["output_dir"]) / "parent_process.json").read_text("utf-8"))
            self.assertNotEqual(parent["worker_pid"], parent["launcher_pid"])
            self.assertEqual(parent["pid"], parent["launcher_pid"])
            self.assertEqual(parent["worker_lineage"]["source"], "windows_toolhelp32_snapshot")
            self.assertEqual(parent["worker_lineage"]["lineage"], [
                {"pid": parent["worker_pid"], "parent_pid": parent["launcher_pid"]}])

    def test_worker_claimed_parent_is_rejected_when_os_reader_disagrees(self):
        self.behavior = "windows_proxy_wrong_parent"
        with patch("scene_factory.runtime_batch._process_platform", return_value="nt"):
            report = self._run()
        self.assertFalse(report["valid"])
        self.assertEqual(report["summary"]["evidence_complete_count"], 0)
        for job in self.jobs:
            parent = json.loads((Path(job["output_dir"]) / "parent_process.json").read_text("utf-8"))
            self.assertIsNone(parent["worker_pid"])
            self.assertIn("differs_from_live_process", parent["worker_identity_error"])

    def test_missing_identity_handshake_rejects_even_successful_matching_pid_report(self):
        self.behavior = "missing_identity"
        report = self._run()
        self.assertFalse(report["valid"])
        self.assertEqual(report["summary"]["physics_status_counts"], {"not_verified": 2})
        self.assertEqual(report["summary"]["evidence_complete_count"], 0)

    def test_timeout_terminates_only_owned_child_and_continues_cases(self):
        self.behavior = "timeout"

        def stop(process):
            process.returncode = -9

        with patch("scene_factory.runtime_batch._stop_process_tree", side_effect=stop) as killer:
            report = self._run(stage_timeout_s=0.01)
        self.assertEqual(killer.call_count, 2)
        self.assertEqual(report["summary"]["attempted_case_count"], 2)
        self.assertFalse(report["validation"]["valid"])
        self.assertIn("worker_timeout", " ".join(report["validation"]["errors"]))

    def test_changed_trace_is_detected_and_resume_refuses_it(self):
        self._run()
        trace = Path(self.jobs[0]["output_dir"]) / "physics_trace.jsonl"
        trace.write_text(trace.read_text("utf-8") + '{}\n', encoding="utf-8")
        self.assertFalse(validate_runtime_batch(self.output)["valid"])
        with self.assertRaisesRegex(DatasetError, "damaged"):
            self._run(resume=True)

    def test_changed_settled_snapshot_and_robot_binary_are_detected(self):
        self._run()
        settled = Path(self.jobs[0]["output_dir"]) / "settled.usda"
        settled.write_text("tampered", encoding="utf-8")
        self.assertFalse(validate_runtime_batch(self.output)["valid"])
        self.robot.write_text("changed robot", encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "changed"):
            self._run(resume=True)

    def test_resume_rejects_settings_and_source_changes(self):
        self._run()
        with self.assertRaisesRegex(DatasetError, "changed"):
            self._run(resume=True, settings={**self.settings, "max_steps": 900})
        source = self.collection / "collection.json"
        source.write_text(source.read_text("utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "changed"):
            self._run(resume=True)

    def test_invalid_settings_unknown_fields_and_incomplete_pairs_are_rejected(self):
        for settings in ({"physics_dt": float("nan")}, {"max_steps": True},
                         {"stable_window_steps": 500}, {"unknown": 1}):
            with self.subTest(settings=settings), self.assertRaises(DatasetError):
                normalize_runtime_settings(settings)
        report = json.loads((self.collection / "collection.json").read_text("utf-8"))
        report["selected"] = report["selected"][:-1]
        with patch("scene_factory.runtime_batch.validate_sampling_collection") as validator:
            validator.return_value.to_dict.return_value = report
            with self.assertRaisesRegex(DatasetError, "exactly one lift"):
                self._run()


if __name__ == "__main__":
    unittest.main()
