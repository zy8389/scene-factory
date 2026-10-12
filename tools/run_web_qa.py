"""Run the offline browser preview gate against a temporary local web service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scene_factory.webapp import SceneFactoryServer, SceneWebApplication


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/web-qa")
    args = parser.parse_args()
    node = shutil.which("node")
    if node is None:
        parser.error("Node.js is required. Install Node.js and run npm ci first.")
    # Never use personal LLM settings or a running service for the portable gate.
    os.environ["SCENE_FACTORY_LLM_MODE"] = "off"
    with tempfile.TemporaryDirectory(prefix="scene_factory_web_qa_") as directory:
        app = SceneWebApplication(Path(directory) / "scenes", runtime_root=Path(directory) / "runtime")
        server = SceneFactoryServer(("127.0.0.1", 0), app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        environment = os.environ.copy()
        environment["SCENE_FACTORY_QA_URL"] = f"http://127.0.0.1:{server.server_port}/"
        environment["SCENE_FACTORY_QA_OUTPUT"] = str(args.output.resolve())
        try:
            return subprocess.run(
                [node, str(ROOT / "tools/qa_web_preview.cjs")], cwd=ROOT,
                env=environment, timeout=180, check=False,
            ).returncode
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
