# Public API

The documented Python API is the compatibility surface for the v0.1 developer
release. APIs may evolve before 1.0, but versioned schemas and documented
symbols should be preferred over implementation details.

## Core

```python
from scene_factory import BuildResult, SceneFactory

result: BuildResult = SceneFactory().build_from_recipe(
    "living_room_recent_snacking",
    seed=42,
)
assert result.valid
```

`SceneFactory` is the main compiler. `BuildResult` contains the compiled scene,
validation details, and optional output information.

## Assets

The registry and metadata types are:

```python
from scene_factory import (
    ArticulationJoint,
    AssetMetadata,
    AssetRegistry,
    AssetLoader,
    InteractionRegion,
    InteriorRegion,
    SemanticState,
)
```

`AssetRegistry` resolves records and metadata. `AssetLoader` resolves portable
asset paths. `ArticulationJoint`, `InteractionRegion`, `InteriorRegion`, and
`SemanticState` describe articulated and placement contracts.

## External input

```python
from scene_factory import ExternalSceneDocument, adapt_external_scene

document: ExternalSceneDocument = adapt_external_scene(
    payload,
    allowed_categories=categories,
    allowed_room_types=room_types,
    allowed_events=events,
)
```

Use `load_external_scene` for a JSON file or stdin. `external_scene_schema`
returns the schema for the currently registered vocabulary.

## Datasets

```python
from scene_factory import inspect_dataset, reproduce_dataset, validate_dataset

inspection = inspect_dataset("outputs/dataset")
validation = validate_dataset("outputs/dataset")
reproduction = reproduce_dataset("outputs/dataset")
```

Dataset reports are structured results with a boolean `valid` field and are
portable across operating systems.

## Planning

`InteractionAction`, `InteractionPlan`, `InteractionPlanningResult`, and
`InteractionWorldState` are the planning data types. Use
`plan_interaction`, `validate_interaction_plan`, and
`replay_interaction_plan` for articulated semantic tasks. The planner does not
claim collision-free motion or physical feasibility.

## Execution

```python
from scene_factory import (
    DryRunInteractionExecutor,
    InteractionExecutor,
    execute_interaction_plan,
    validate_execution_trace,
)
```

`InteractionExecutor` is the protocol for semantic plan execution.
`DryRunInteractionExecutor` is the reference non-physical implementation.
`ExecutionCommand`, `ExecutionResult`, `ExecutionStepResult`, `ExecutionTrace`,
and `ExecutionTraceStep` carry command and trace data. Use
`write_execution_trace_atomic` for portable output.

## Executor conformance

`ExecutorCapabilities`, `ConformanceCaseResult`,
`ExecutorConformanceReport`, and `ConformanceValidationResult` are the report
types. `run_executor_conformance` executes the core profile, while
`validate_conformance_report` validates a saved report without re-executing the
executor. `capability_sha256` provides a stable signature for normalized
capabilities.

The conformance suite is a semantic compatibility gate. A passing report does
not imply physical execution.

## Export policy

`scene_factory.__all__` preserves the existing v0.1 import surface. Lower-level
asset pipeline helpers, command-id helpers, symbolic effect helpers, and
normalization functions remain import-compatible but are considered advanced
API. Names beginning with `_` and modules not listed here are internal unless
their own documentation says otherwise.
