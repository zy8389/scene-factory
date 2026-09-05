# P1-4B/C final evidence index

**Physical acceptance: FAILED.** This directory makes the actual failed batch
reviewable; it does not turn two failures into two accepted runs.

- Tested code: `24fe842b9b452aac7bd8ea9542ad316379adc6d3`.
- Batch: 2026-09-05 21:21:25–21:22:29 UTC (2026-09-06 Asia/Shanghai).
- [Four-axis ledger and analysis](../../P1_4_CLOSEOUT.md)
- [Machine-readable summary](summary.json)
- [Original/copy hashes and commands](manifest.json)
- [Earlier external evidence hashes](historical_manifest.json)

## Originals versus portable review copies

Original files remain on the reference machine at
`<RUNTIME_ROOT>/p1_4_closeout_20260906` (`<CLOSEOUT_ROOT>`). They were not rewritten.
Each manifest entry records original path, byte count and SHA-256 separately
from the compressed copy and its uncompressed SHA-256.

`review/*.gz` contains all three full child JSON reports and raw-runtime-log
review copies, the full aggregate, parent stdout, independent gate JSON/log,
offline pytest/Ruff output and environment probe. These are **portable/redacted
review copies, NOT byte-identical originals**. Redactions replace local runtime,
repository, interpreter and user-home roots and hardware-specific GPU LUID/UUID
cells. JSON copies are reserialized. Parent stdout's already encoding-damaged
repository root is marked `<REPO_ROOT_ENCODING_LOSS>`; use full JSON as the
authoritative source. No asset binaries or credentials are included.

Action evidence, measured observations, configuration, loaded USD hashes, source
attestations and ExecutionTrace payloads are unchanged. Their equivalence and
identical gate output were verified before publication. Hashes are integrity
checks, not signatures or proof of physical origin. Original simulator logs
remain available for local provenance review.

## Verify and unpack without touching the originals

From a checkout containing this evidence, run the following Python snippet.
It validates both compressed and decompressed hashes and writes to a **new
system temporary directory**. Save/execute the snippet using the dev Python.

```python
import gzip
import hashlib
import json
from pathlib import Path
import tempfile

root = Path("docs/evidence/p1_4_closeout")
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
out = Path(tempfile.mkdtemp(prefix="p1-4-review-"))
for entry in manifest["entries"]:
    copy = entry["review_copy"]
    packed = (root / copy["path"]).read_bytes()
    assert len(packed) == copy["bytes"]
    assert hashlib.sha256(packed).hexdigest() == copy["sha256"]
    data = gzip.decompress(packed)
    assert len(data) == copy["uncompressed_bytes"]
    assert hashlib.sha256(data).hexdigest() == copy["uncompressed_sha256"]
    assert Path(entry["name"]).name == entry["name"]
    (out / entry["name"]).write_bytes(data)
print(out)
```

Use the printed directory as `$reviewRoot` in PowerShell:

```powershell
& $devPython tools/validate_p1_4c_acceptance.py `
  --run1 "$reviewRoot/full_task_r3.run1.json" `
  --run2 "$reviewRoot/full_task_r3.run2.json" `
  --negative "$reviewRoot/full_task_r3.negative.json" `
  --expected-head 24fe842b9b452aac7bd8ea9542ad316379adc6d3 `
  --report "$reviewRoot/replayed_gate.json"
# Expected exit code: 2. Expected result: failed, not passed.
```

`replayed_gate.json` must match `evidence_gate_r3.json` as parsed JSON.
The exact unpack snippet and standalone CLI above were executed successfully:
all 13 copies passed hash checks, exit code was 2, and parsed JSON was identical.
This is an offline audit; it does NOT launch Isaac or reproduce physical runs.
Use the exact tested code commit for a simulator rerun, official installed
assets and the documented frozen environment, with a NEW report basename.
Never count an evidence-only documentation commit as a new physics-tested commit.

## First failure versus downstream evidence

Both positives pass approach/opposed grasp, then fail full-pull IK continuation
before any pull simulation step. `panda_joint4` reaches its lower limit in the
planned path. The 12.464 mm solved path is NOT measured drawer traction. Pull
and release physics are NOT RUN; the drawer remains closed. The negative case
passes by rejecting pull before grasp. Source/configuration/asset provenance
and process exits are valid, but cannot substitute for task success.

The gate's positive `no_execution_writes` failure means required full-task
release coverage is absent; measured write counts are zero. The failed trace
is structurally valid, not a successful full-task trace. See the closeout report
for the Isaac fast-shutdown/parent-attestation semantics and earlier failed
teardown attempts. Historical originals are retained and indexed, never recycled
as successful acceptance.
