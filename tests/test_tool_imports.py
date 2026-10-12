import json
import subprocess
import sys
from pathlib import Path


def test_repository_tools_win_over_installed_package(tmp_path):
    # Regular packages elsewhere on sys.path shadow a namespace-only tools folder.
    foreign = tmp_path / "other_project"
    (foreign / "tools").mkdir(parents=True)
    (foreign / "tools/__init__.py").write_text("", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run([
        sys.executable, "-I", "-c",
        "import sys,json; sys.path[:0]=sys.argv[1:]; "
        "import tools,tools.ingest_assets; print(json.dumps(tools.__file__))",
        str(root), str(foreign),
    ], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30)
    assert Path(json.loads(probe.stdout)) == root / "tools/__init__.py"
