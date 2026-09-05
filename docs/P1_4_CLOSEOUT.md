# P1-4B/C closeout: experimental implementation, physical acceptance blocked

## Scope and status rules

The frozen reference remains the official Sektion top drawer + official Franka.
No substitute door task, new assets, training, or parameter sweep is included.
P1-4A is the asset-binding milestone, **not** a physical grasp qualification.

Maintain four independent status axes, never a single "done" percentage:

| Deliverable | Code complete | Offline acceptance | Reference-machine acceptance | Released |
| --- | --- | --- | --- | --- |
| P1-4A binding | Merged at `4077a41376c1c254949347f2082658b53c307767`, PR #21 | Previously passed | Historical read-only binding PASS; not contact manipulation | Not part of the earlier v0.1.0 tag |
| P1-4B drawer executor | Experimental; not accepted as complete | Unit/fake boundary tests only; see recorded run results | BLOCKED by approach/grasp/traction; full task not demonstrated | No |
| P1-4C evidence gate | Implemented for review; gate is not the physical task | Strict recorded-evidence validation; synthetic tests are not reference runs | Requires two independent full PASS runs plus negative test; not yet accepted | No |
| Isaac Lab training | Not started; frozen until P1-4C passes | Not run | Not run | No |

The v0.1.0 GitHub Release was published on **2026-08-28** with wheel, sdist,
manifest and checksums. That release must not be described as containing these
unreleased P1-4B/C changes. PyPI publication is not asserted.

## Correction to the earlier workspace-only audit

An earlier audit missed reports stored outside the repository. Their immutable
raw hashes and portable source locations are in
`evidence/p1_4_closeout/historical_manifest.json`. Originals remain under the
reference machine's runtime directory, not silently rewritten or uploaded.

- Exact-default drawer approach already failed `motion_convergence_failed`.
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
first failure and downstream NOT RUN stages. Add fresh-run results below only
after the processes exit and the offline evidence audit finishes.

Notion's project summary and Sprint board must share the same four axes.
Consolidate duplicate training/SAGE/planning work by choosing a canonical task,
marking aliases and preserving history (no task deletion). Keep asset-registry
and USD-standardization tasks separate unless their actual acceptance criteria
are identical. Broad Franka pick/place is not a synonym for drawer opening.
