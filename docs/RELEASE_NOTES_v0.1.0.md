# SceneFactory v0.1.0

SceneFactory v0.1.0 is an early developer release for deterministic embodied-AI
scene generation, dataset production, symbolic interaction planning, and
simulator-neutral executor contracts. It is intended for developers building
repeatable simulation and data-generation workflows.

## Overview

The release provides a pure-Python core for compiling scene descriptions into
validated scene specifications, checking asset metadata, generating deterministic
datasets, planning articulated interactions, and executing those plans through a
portable dry-run executor contract.

## Core capabilities

- deterministic household scenes from built-in recipes or external `SceneIntent`
  documents;
- asset registry, metadata, collision, and USD validation contracts;
- deterministic batch datasets with validation, resume, and reproduction checks;
- articulated asset interaction metadata and symbolic task planning;
- execution commands, traces, validation, and executor conformance reports;
- optional web UI and simulator-facing Isaac/USD adapters kept outside the core
  Python dependency set.

## Installation

Build or download the release artifact, then install it in a Python 3.12 or
newer environment:

```bash
python -m pip install scene_factory-0.1.0-py3-none-any.whl
scene-factory list-recipes
```

The core runtime has no mandatory third-party dependencies. The optional
`gym` extra provides Gymnasium integration. Isaac Sim and USD workflows require
their own compatible simulator environment and are not installed by the core
package.

## 5-minute example

```python
from scene_factory import SceneFactory

result = SceneFactory().build_from_recipe("living_room_recent_snacking", seed=42)
if not result.valid:
    raise RuntimeError(result.to_dict())
print(result.scene.scene_id)
```

The CLI provides the same path:

```bash
scene-factory build --recipe living_room_recent_snacking --seed 42 --output ./scene
```

## Public SDK

The documented stable import surface is recorded in
[`API_SURFACE_v0.1.md`](API_SURFACE_v0.1.md). The package exposes scene
construction, asset and dataset inspection, external intent adaptation,
interaction planning, dry-run execution, trace validation, and executor
conformance. Lower-level modules remain available for advanced integrations but
are not promised as stable by this early release.

## Dataset pipeline

Use `SceneFactory.build_batch` or the `batch` CLI command to create deterministic
datasets. `inspect_dataset`, `validate_dataset`, and `reproduce_dataset` provide
offline integrity and reproducibility checks. Dataset outputs are intentionally
kept outside the installed package and should be treated as generated data.

## Articulated interaction

Articulated assets can describe joints, handles, interaction regions, allowed
actions, and semantic states. The interaction model is symbolic and validates
contracts such as joint limits, region references, and requested target states.

## Planning and execution

The planner produces versioned interaction plans. The portable dry-run executor
produces versioned commands and execution traces, and the conformance suite
checks executor behavior against the core semantic profile. A passing dry-run
or conformance report does not claim physical feasibility.

## Isaac status

Isaac integration remains an independent, environment-specific acceptance gate.
Reference Isaac Sim 6.0.1 real P1-1, P1-2, and P1-3 acceptance passes locally
with official Local Assets. P1-1 Franka mug-lift, P1-2 Franka pick-and-place,
and P1-3 RGB-D trajectory export are PASS results. P1-3B real episode
`inspect`, `validate`, and `replay` are also PASS for episode integrity and
contract consistency. The real episode metadata has no `task_spec`, so
`task_replay_available=false`: pure-Python task-oracle recomputation is
unavailable, while integrity replay remains PASS. This evidence is
environment-specific and does not claim universal hardware compatibility.

The following physical scopes remain explicitly unvalidated:

Real articulated asset execution has not been validated. Real robot execution
has not been run. Isaac Lab integration has not started.

- Real articulated asset execution: NOT RUN.
- Real robot execution: NOT RUN.
- Isaac Lab: NOT STARTED.

These limitations do not prevent use of the pure-Python v0.1 SDK for offline
scene, dataset, planning, and executor-contract workflows.

## Known limitations

- this is an early developer release, not a production-ready distribution;
- simulator availability, asset transport, and USD runtime behavior depend on
  the user's Isaac Sim installation and environment;
- the dry-run executor does not replace a physical robot or simulator executor;
- schema migration tooling and long-term API stability guarantees are out of
  scope before 1.0;
- no real robot execution has been run or certified.

## Compatibility

The supported baseline is Python 3.12 or newer. The pure-Python core is
designed to run without Isaac Sim, GPU libraries, NumPy, or network access.
See [`COMPATIBILITY.md`](COMPATIBILITY.md) and
[`SCHEMA_POLICY.md`](SCHEMA_POLICY.md) for platform and versioned-contract
guidance.
