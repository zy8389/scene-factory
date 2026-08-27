# SceneFactory

SceneFactory is a deterministic scene, task, interaction-planning, and
executor-contract toolkit for embodied-AI simulation workflows.

The v0.1 release candidate focuses on an offline Python SDK that can be cloned,
installed, inspected, and used without Isaac Sim, a GPU, NumPy, an LLM API, or
a network connection.

## What it does

```text
recipe / external SceneIntent
          |
          v
deterministic SceneFactory compilation
          |
          +--> scene specification and SVG preview
          +--> reproducible batch dataset
          +--> articulated asset contract
          +--> symbolic InteractionPlan
          +--> DryRun ExecutionTrace
          +--> executor conformance report
```

The core pipeline provides:

- deterministic household scene generation from recipes or external JSON;
- asset registry, metadata, support surfaces, and articulation contracts;
- batch datasets with portable manifests, validation, and reproduction;
- symbolic articulation planning and offline dry-run execution;
- execution trace validation and a core executor conformance suite;
- optional Isaac Sim USD export and environment-specific robot integration.

## Installation

SceneFactory supports Python 3.12 or newer and has no required runtime
dependencies for the core SDK:

```bash
python -m pip install .
scene-factory list-recipes
```

For development checks:

```bash
python -m pip install ".[dev]"
```

The wheel includes recipes, schemas, web files, the asset registry, and the
committed asset metadata required by the offline workflows. It does not bundle
Isaac Sim or NVIDIA Local Assets.

## 5-minute quickstart

Build one deterministic scene without a simulator:

```bash
scene-factory build \
  --recipe living_room_recent_snacking \
  --seed 42 \
  --output outputs/basic-scene
```

The output contains `scene_spec.json`, `layout.json`, `validation.json`, and
an offline `preview.svg`. The same command works from any current directory
after installation.

The equivalent Python API is:

```python
from scene_factory import SceneFactory

result = SceneFactory().build_from_recipe(
    "living_room_recent_snacking",
    seed=42,
)
assert result.valid
print(result.scene.scene_id)
```

Runnable examples are in [`examples/`](examples/):

- [`basic_scene`](examples/basic_scene/README.md) for recipe compilation;
- [`external_intent`](examples/external_intent/README.md) for structured input;
- [`deterministic_dataset`](examples/deterministic_dataset/README.md) for batch
  validation and reproduction;
- [`articulated_drawer`](examples/articulated_drawer/README.md) for planning,
  dry-run execution, and trace validation.

## External SceneIntent

External programs can submit a versioned `SceneIntent` JSON document. Validate
and compile it through the same deterministic pipeline:

```bash
scene-factory intent validate examples/external_intent/scene.json
scene-factory build \
  --intent examples/external_intent/scene.json \
  --seed 42 \
  --output outputs/external-scene
```

The raw intent and `scene_factory.external_scene.v1` envelope are validated
before compilation. Producer metadata records provenance but does not affect
scene identity.

## Deterministic datasets

```bash
scene-factory batch \
  --recipe living_room_recent_snacking \
  --count 3 \
  --seed-start 100 \
  --output outputs/dataset
scene-factory dataset validate outputs/dataset
scene-factory dataset reproduce outputs/dataset
```

Dataset manifests use portable relative paths, content hashes, and semantic
fingerprints. Validation and reproduction are offline and do not call an LLM or
external service.

## Articulated planning and execution

The symbolic planner consumes articulated metadata and produces an
`InteractionPlan`. The dry-run executor applies semantic state transitions and
emits a validated `ExecutionTrace`:

```bash
scene-factory task plan \
  --scene examples/articulated_drawer/scene.json \
  --object drawer_1 \
  --state open \
  --output outputs/drawer-plan.json
scene-factory task execute \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer-plan.json \
  --executor dry-run \
  --output outputs/drawer-trace.json
scene-factory task execution-validate \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer-plan.json \
  --trace outputs/drawer-trace.json
```

`DryRunInteractionExecutor` reports `physical=false`. A passing symbolic plan,
trace, or conformance report does not claim collision-free motion or physical
manipulation.

## Executor conformance

Inspect the reference executor and run the simulator-neutral core suite:

```bash
scene-factory executor inspect --executor dry-run
scene-factory executor conformance \
  --executor dry-run \
  --output executor-conformance.json
scene-factory executor validate-report executor-conformance.json
```

The suite checks lifecycle, capability declarations, command/result correlation,
evidence, final goals, and trace semantics. It is a compatibility gate for
`InteractionExecutor`, not a physical acceptance gate.

## Architecture and API

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains the compilation and
  simulator boundaries.
- [`docs/PUBLIC_API.md`](docs/PUBLIC_API.md) documents the supported Python API.
- [`docs/CLI.md`](docs/CLI.md) lists CLI commands and exit-code behavior.
- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) records environment support.
- [`docs/SCHEMA_POLICY.md`](docs/SCHEMA_POLICY.md) defines schema compatibility.
- Existing deep dives remain available for the
  [asset pipeline](docs/ASSET_PIPELINE.md),
  [LLM integration](docs/LLM_INTEGRATION.md), and
  [web UI](docs/WEB_UI.md).

## Isaac Sim status

Isaac-specific modules use lazy imports so `import scene_factory` remains a
pure-Python operation. Isaac Sim 6.0.1 can be used for USD export and the
environment-specific Franka/RGB-D workflows described in
[`docs/ISAAC_VALIDATION.md`](docs/ISAAC_VALIDATION.md).

The current public status is intentionally conservative:

| Capability | Status |
| --- | --- |
| Pure-Python scene, dataset, planning, and dry-run workflows | available |
| Executor conformance | available |
| Franka real execution | environment-blocked |
| Real RGB-D acceptance | environment-blocked |
| Isaac Lab | not started |

Official Isaac Sim Local Assets are not bundled with SceneFactory. Real Franka
and RGB-D acceptance requires a separately validated Isaac environment and
official assets. No bundled URDF or offline result is presented as a substitute
for that acceptance.

## CLI reference

The public command groups are:

```text
list-recipes
build
batch
intent
dataset
task
executor
asset
llm-status
llm-test
```

Run `scene-factory --help` or see [`docs/CLI.md`](docs/CLI.md) for the complete
syntax. The CLI uses exit code `0` for success, `1` for command/configuration or
runtime input errors, and `2` for validation or acceptance failures.

## Development

The offline release checks are:

```bash
python tools/check_release.py
python tools/release_smoke.py
python -m ruff check scene_factory tools tests
python -B -m compileall -q scene_factory tools tests
python -B -m pytest -p no:cacheprovider -q
```

`tools/release_smoke.py` is intended to run after installing the wheel into a
fresh virtual environment. It runs from a temporary directory outside the
repository and does not import repository source files.

## License and asset attribution

The code is licensed under MIT. Packaged YCB source assets are attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md); their upstream license terms
continue to apply. See [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) and
[`CHANGELOG.md`](CHANGELOG.md) for release-readiness scope and status.
