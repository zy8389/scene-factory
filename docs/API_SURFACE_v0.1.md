# SceneFactory v0.1 API Surface

This document freezes the documented compatibility surface for the 0.1
developer release. It is a documentation contract, not a promise that every
implementation detail will remain unchanged before 1.0.

## Recommended public imports

The recommended imports are the names exported by `scene_factory.__all__`:

```python
from scene_factory import (
    BuildResult,
    SceneFactory,
    AssetLoader,
    AssetMetadata,
    AssetRegistry,
    AssetValidator,
    DatasetResult,
    ExternalSceneDocument,
    InteractionAction,
    InteractionPlan,
    InteractionPlanningResult,
    InteractionWorldState,
    DryRunInteractionExecutor,
    ExecutionCommand,
    ExecutionResult,
    ExecutionTrace,
    ExecutorCapabilities,
    ExecutorConformanceReport,
    inspect_dataset,
    validate_dataset,
    reproduce_dataset,
    plan_interaction,
    validate_interaction_plan,
    execute_interaction_plan,
    validate_execution_trace,
    run_executor_conformance,
)
```

The complete exported list, including version constants and serialization
helpers, is authoritative in `scene_factory.__all__`. The existing
[`PUBLIC_API.md`](PUBLIC_API.md) gives usage examples for the stable groups.

## Advanced/import-compatible API

Modules such as `scene_factory.models`, `scene_factory.paths`,
`scene_factory.registry`, `scene_factory.planning`, `scene_factory.execution`,
and `scene_factory.conformance` expose lower-level constructors and helpers for
integrators. These symbols are import-compatible in v0.1 but are not all
documented as stable. Names beginning with `_` are internal.

Isaac-specific backends and exporters are optional runtime integrations. They
must remain lazily imported and are not part of the pure-Python dependency
contract.

## Versioned schemas

The schemas present in this release are the JSON files under `schemas/`:

- `asset_record.schema.json`
- `execution_trace.schema.json`
- `executor_capabilities.schema.json`
- `executor_conformance.schema.json`
- `interaction_plan.schema.json`
- `scene_intent.schema.json`
- `scene_spec.schema.json`

Serialized schema identifiers and compatibility rules are governed by
[`SCHEMA_POLICY.md`](SCHEMA_POLICY.md). Unknown schema versions must fail
closed; incompatible semantic changes require a new schema version.

## Stability boundary

Stable usage should prefer the documented imports and versioned schemas above.
The CLI is also a supported interface for the command groups listed in
[`CLI_REFERENCE.md`](CLI_REFERENCE.md). Physical simulator acceptance is a
separate gate and is never implied by a pure-Python API or executor result.
