# P1-4B/C closeout: experimental implementation, physical acceptance blocked

## Scope and status rules

The frozen reference remains the official Sektion top drawer + official Franka.
No substitute door task, new assets, training, or parameter sweep is included.
P1-4A is the asset-binding milestone, **not** a physical grasp qualification.

Maintain four independent status axes, never a single "done" percentage:

| Deliverable | Code complete | Offline acceptance | Reference-machine acceptance | Released |
| --- | --- | --- | --- | --- |
| P1-4A binding | Merged at `4077a41376c1c254949347f2082658b53c307767`, PR #21 | Previously passed | Historical read-only binding PASS; not contact manipulation | Not part of the earlier v0.1.0 tag |
| P1-4B drawer executor | Experimental; submitted in Draft PR #22, not accepted as complete | 316 tests + 221 subtests pass; Ruff/CI pass (not physical acceptance) | FAILED twice on `24fe842`: approach/grasp pass; full-pull IK fails before motion; pull/release NOT RUN | No |
| P1-4C evidence gate | Implemented for review in Draft PR #22; not the physical task | Strict validation tests pass; original/review-copy audits agree | FAILED: two positive cases fail; independent pull-before-grasp negative passes | No |
| Isaac Lab training | Not started; frozen until P1-4C passes | Not run | Not run | No |

The v0.1.0 GitHub Release was published on **2026-08-28** with wheel, sdist,
manifest and checksums. That release must not be described as containing these
unreleased P1-4B/C changes. PyPI publication is not asserted.

## Correction to the earlier workspace-only audit

An earlier audit missed reports stored outside the repository. Their immutable
raw hashes and portable source locations are in
`evidence/p1_4_closeout/historical_manifest.json`. Originals remain under the
reference machine's runtime directory, not silently rewritten or uploaded.

- Earlier exact-default drawer approach failed `motion_convergence_failed`.
  The fresh `24fe842` batch below now passes approach/grasp; its first failure
  is full-pull IK continuation. Historical evidence is not the fresh result.
- Historical opposed contact and small traction do **not** qualify a full task.
  Candidate responses of 0.208/0.380 mm and best bounded recovery of
  6.916/7.237 mm remained below the historical 10 mm prerequisite.
- The drawer was explicitly rejected as the currently qualified physical pair.
  Its semantic/asset binding remains valid.
- A later door alternative passed corrected IK and world-fixed mounting, but
  collision preflight failed at all 241 samples. Downstream physical actions
  were NOT RUN. This is negative context, not a replacement acceptance task.
- A superseded support-contact-only rejection must not be used to conclude that
  a correctly world-fixed official Franka is invalid.

## What the implementation now enforces

1. Full acceptance executes the canonical validated `approach → grasp → pull →
   release` plan, nominal **0.35 m**, observed open range **[0.32, 0.38] m**.
   A 0.05 m pilot or 10 mm recovery cannot satisfy it.
2. Pull trajectory length comes from the requested measured displacement,
   rather than the historical fixed 50 mm pilot.
3. Release requires contact separation, followed by **30 consecutive steps at
   60 Hz**, measured |velocity| ≤0.01 m/s, drift ≤0.005 m and no recontact.
   Missing/invalid contact readings cannot stand in for separation.
4. Final state is checked with TaskEvaluator and an action-correlated
   ExecutionTrace. Physical trace validation uses the observed goal range;
   nonphysical dry-run traces retain exact symbolic-state equality.
5. Two positive processes and a separate pull-before-grasp negative process
   bind exact clean commit, tracked-file hash, binding, seed, controller config,
   loaded on-disk USD layer hashes, unique run/process identity, and OS exit code.
6. Isaac 6 fast shutdown terminates inside `close()`: child evidence is persisted
   before shutdown and the parent independently records the OS exit, timestamp
   and source-after hash. A returned `close()` is not invented; exit 0 alone is
   not task success. Close failure, timeout, changed source/assets, malformed JSON, or missing
   evidence fail closed. Child reports/logs are not overwritten by a rerun.
   Each process has a 1200-second limit; no retry sweep is launched.

The simulator-free P1-4C gate audits *recorded* observations, not their physical
origin. Hashes give reproducibility/integrity checks, not a signature or proof
against fabricated reports. Keep original simulator logs and review provenance.
Legacy diagnostic CLI modes remain explicitly non-acceptance experiments.

## Reproduction on the reference machine

Set `ISAACSIM_ASSET_ROOT` to the installed official 6.0 asset root, with
`OMNI_KIT_ACCEPT_EULA=YES` and `PYTHONNOUSERSITE=1`. Do not alter the frozen USDs.
Use a clean checkout, the Isaac Python 3.12 interpreter, and a NEW report basename
outside the repository:

```powershell
$head = git rev-parse HEAD
& $isaacPython tools/validate_p1_4b_executor.py --isaac-python $isaacPython `
  --head $head --report "$runtimeRoot/p1_4_closeout/full_task.json"

# Independent offline audit of the saved raw reports; no Isaac needed here.
& $devPython tools/validate_p1_4c_acceptance.py `
  --run1 "$runtimeRoot/p1_4_closeout/full_task.run1.json" `
  --run2 "$runtimeRoot/p1_4_closeout/full_task.run2.json" `
  --negative "$runtimeRoot/p1_4_closeout/full_task.negative.json" `
  --expected-head $head --report "$runtimeRoot/p1_4_closeout/evidence_gate.json"
```

A failed approach ends that run: grasp/pull/release are **NOT RUN**, not failed
observations and not implicit passes. Reproducing a failure twice is not two
successful acceptance runs. Never merge/publish this branch as a completed
physical drawer task while this gate is red.

## Evidence and ledger updates

Record tested **code commit** separately from any later **documentation commit**.
Each evidence entry must contain command, timestamps, raw path, SHA-256, result,
first failure and downstream NOT RUN stages. The final batch below is recorded
only after all three processes exited and the independent offline evidence
audit finished.

Notion's project summary and Sprint board must share the same four axes.
Consolidate duplicate training/SAGE/planning work by choosing a canonical task,
marking aliases and preserving history (no task deletion). Keep asset-registry
and USD-standardization tasks separate unless their actual acceptance criteria
are identical. Broad Franka pick/place is not a synonym for drawer opening.

## Final reference-machine batch — 2026-09-05 UTC

**Verdict: physical acceptance FAILED; Draft PR #22 is not merge-ready.**
The same times are early **2026-09-06 in Asia/Shanghai**, which explains the
runtime directory date; all timestamps below use UTC.

- Tested **code commit**: `24fe842b9b452aac7bd8ea9542ad316379adc6d3`.
  Later evidence/documentation commits do not mean the simulator was rerun.
- Tracked-source fingerprint before, before shutdown and after exit:
  `98bb75e6a00d563dead509af8afe38b87278051324e2b2b92cd60418d75bd811`;
  all three processes attest the same clean code and unchanged source.
- Configuration SHA-256:
  `470fa0e5f1486bbd071f879ebc9d536dd4726da79b20082decfecaa5fedb5ff5`;
  same seed 0, frozen binding, controller and loaded USD layer hashes.
- Reference runtime: Windows, Python 3.12.7, Isaac Sim 6.0.1.0.
- Offline suite: **316 passed, 221 subtests passed in 18.95 s**. Ruff passed.
  Both CI checks on the tested code passed; URLs are in the evidence manifest.

| Case | PID | Start UTC | Finish UTC | Actual result |
| --- | ---: | --- | --- | --- |
| run1 | 60920 | 21:21:25.644677 | 21:21:47.034671 | FAIL `pull_ik_continuation_failed` |
| run2 | 62528 | 21:21:47.976824 | 21:22:09.153550 | Same FAIL, separate process/run UUID |
| negative | 62944 | 21:22:10.111902 | 21:22:29.013121 | PASS: `pull_before_grasp` correctly rejected |

Both positive runs successfully approach and form opposed grasp. The pull
**action** fails during pre-motion IK continuation with
`ik_failed_at_minimum_spacing`; actual physical pull and release are **NOT RUN**.
The 13 accepted IK waypoints cover only **0.012464389668151443 m of planned path**,
not measured drawer traction. The planned `panda_joint4` reaches its lower limit
`-3.0717999935150146 rad`. Pull physics steps start and end at 257; observed
execution drawer-joint writes are zero. The drawer stays effectively closed
(initial `2.0248551724222352e-09 m`, final `-6.188169265897159e-08 m`).
This diagnoses the current path/controller, not global Franka infeasibility.

All three children exit with OS return code 0 and parent-observed exit evidence.
`closed_cleanly=false` remains truthful: fast shutdown terminates inside close;
the child report was durably written first. **Normal process exit is not task
success.** The failed ExecutionTrace is structurally valid, but TaskEvaluator is
false and the gate's successful-full-task trace criterion is false. The gate's
`no_execution_writes=false` on positive cases is missing full release coverage,
not evidence that execution writes occurred.

The independent P1-4C CLI returns **2 / FAILED**. Its result is identical for
original JSON and decompressed portable review copies. Two reproduced failures
are **not two accepted runs**.

### Reviewable evidence

- [Readable evidence index and audit commands](evidence/p1_4_closeout/README.md)
- [Original/copy SHA-256 and size manifest](evidence/p1_4_closeout/manifest.json)
- [Per-run provenance and failure summary](evidence/p1_4_closeout/summary.json)
- [Compressed portable full-report/log review copies](evidence/p1_4_closeout/review)

The originals remain unchanged under the reference machine runtime root,
`p1_4_closeout_20260906/full_task_r3.*`, plus the independent gate and offline logs.
Public review copies are **explicitly not byte-identical originals**: local
paths and GPU identifiers are redacted, with separate hashes. No physics,
configuration, action, source-attestation or trace payload is changed. The
parent console copy had an encoding-loss repository path; the authoritative
JSON files did not. Full JSON, not stdout, is authoritative.

Earlier `full_task.*` and `full_task_r2.*` originals are retained and hashed in
the manifest. The former lacks shutdown-tail attestation; the latter crashes
in full extension teardown (Windows `0xC0000005`). Neither is acceptance evidence
that can be upgraded into a pass.

### Remaining work and ledger consolidation

Keep only one canonical B executor task and one canonical C acceptance task.
Notion records code/offline/reference/released independently and links the
Draft PR, tested code commit, this report and the evidence manifest.
The project homepage now separates the current four-axis ledger from the
retained August snapshot; the new homepage ledger was verified after reload.
Franka-grasp-Demo, Isaac-Lab-adapter and SAGE/NuRec/Marble-adapter duplicate rows
are marked as merged aliases with history preserved, excluded from delivery
counts. Their canonical expansion tasks remain frozen; broad pick/place is not
a completed drawer task. Distinct asset/physics acceptance criteria were not
blanket-merged or marked complete. No owner or deadline was invented.

The next bounded engineering gate is to establish a complete, collision-safe
35 cm continuation for this frozen drawer/Franka setup (including a usable
initial IK branch), then validate actual contact-driven traction and release.
If that cannot be achieved within the frozen configuration, record the failed
qualification and request an explicit scope/configuration decision; do not
silently substitute a door, lower the distance or sweep fixtures. Any code fix
needs a **new clean tested commit and fresh two-positive/one-negative runs**.
No Isaac Lab work, merge or release is authorized by this closeout.
