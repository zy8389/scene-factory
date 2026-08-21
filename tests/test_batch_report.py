from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scene_factory.batch_ingestion import BatchAssetResult, BatchReport


class BatchReportTests(unittest.TestCase):
    def test_partial_report_has_required_summary_fields(self) -> None:
        ready = BatchAssetResult("ready")
        ready.transition("ready")
        blocked = BatchAssetResult("blocked")
        blocked.block("source_unresolved", "no source")
        report = BatchReport("batch", 2, {"ready": ready, "blocked": blocked})
        self.assertEqual(report.ready, 1)
        self.assertEqual(report.blocked, 1)
        self.assertEqual(report.result, "partial")
        with tempfile.TemporaryDirectory() as directory:
            path = report.write(Path(directory) / "batch.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(("batch_id", "requested", "ready", "blocked", "failed", "assets", "result")), set(payload))


if __name__ == "__main__":
    unittest.main()
