from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rewrite one normalized USD's local source reference.")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def _write(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sys.argv = [sys.argv[0]]
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    report = {
        "asset_id": args.asset_id,
        "source": str(source),
        "output": str(output),
        "reference": args.reference,
        "result": "blocked",
        "issues": [],
    }
    try:
        if not source.is_file():
            raise FileNotFoundError(source)
        if output.exists():
            raise FileExistsError(output)
        from pxr import Usd

        stage = Usd.Stage.Open(source.as_posix())
        if stage is None:
            raise RuntimeError(f"could not open USD: {source}")
        source_prim = stage.GetPrimAtPath("/Asset/Visual/Source")
        if not source_prim.IsValid():
            raise ValueError("normalized USD has no /Asset/Visual/Source prim")
        references = source_prim.GetReferences()
        references.ClearReferences()
        references.AddReference(args.reference)
        asset_prim = stage.GetPrimAtPath("/Asset")
        source_attr = asset_prim.GetAttribute("sceneFactory:source")
        if source_attr.IsValid():
            source_attr.Set(args.reference)
        output.parent.mkdir(parents=True, exist_ok=True)
        stage.GetRootLayer().Export(output.as_posix())
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("relocated USD output is empty")
        report["result"] = "passed"
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["issues"].append({"code": "usd_reference_relocation_failed", "message": str(exc)})
    _write(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
