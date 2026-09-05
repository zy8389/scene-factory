"""Audit two positive and one negative P1-4 runtime reports; does not run Isaac."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_factory.backends.isaac_acceptance import validate_acceptance


def read_report(path: Path) -> dict:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"report exceeds 64 MiB: {path}")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"non-finite JSON value: {value}")

    payload = json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=pairs,
        parse_constant=invalid_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError("report must be an object")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run1", required=True, type=Path)
    parser.add_argument("--run2", required=True, type=Path)
    parser.add_argument("--negative", required=True, type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    inputs = (args.run1, args.run2, args.negative)
    if args.report.resolve() in {path.resolve() for path in inputs}:
        parser.error("output must not overwrite a source report")
    try:
        result = validate_acceptance(
            *(read_report(path) for path in inputs), expected_head=args.expected_head
        )
    except (OSError, ValueError) as exc:
        result = {
            "result": "failed",
            "failure_reason": "invalid_evidence_input",
            "message": str(exc),
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0 if result["result"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
