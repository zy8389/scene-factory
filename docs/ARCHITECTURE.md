# Architecture

SceneFactory is a deterministic compilation layer for offline embodied-AI
workflow contracts. Its core does not require Isaac Sim, NumPy, a GPU, or a
network connection.

```text
recipe / external SceneIntent
            |
            v
       SceneFactory
            |
            v
       compiled SceneSpec
        /      |       \
       v       v        v
    Dataset   Task    Planner
                         |
                         v
                  InteractionPlan
                         |
                         v
                    Executor
                         |
                         v
                  ExecutionTrace
```

## Boundaries

`SceneFactory` resolves assets, compiles deterministic layouts, and writes
portable scene outputs. `Dataset` builds and validates collections of those
outputs. `TaskEvaluator` and task models describe goals without claiming that a
robot can physically execute them.

`Quality` derives a versioned report from recorded layout validation without
upgrading it to physics or task acceptance. `Dataset.audit_dataset` verifies
source integrity before computing quality selection and coverage statistics.
It preserves source files and reproducibility fingerprints; physical evidence
integration and trajectory collection remain separate stages.

`Sampling` composes existing v1 batch datasets into an ordered collection with
explicit candidate budgets, region filters and quality quotas. It releases no
partial selection if any quota is unmet and verifies stored selection references
against child dataset evidence. It adds collection bookkeeping without changing
the compiler, robot executor or trajectory protocols.

`SimulatorBackend` is the environment-level abstraction for reset, step,
render, and close. `InteractionExecutor` is the semantic interaction-plan
abstraction for reset, command execution, snapshots, and close. They are
separate contracts because an environment can host several task executors and
an executor can be tested offline.

The current `DryRunInteractionExecutor` applies symbolic state transitions and
reports `physical=false`. Its core conformance suite checks the executor
contract; it is not a physical manipulation acceptance test.

Isaac-specific modules keep simulator imports lazy. The Isaac backend and USD
export path are optional environment integrations and are not imported by the
pure-Python public API smoke.

## Data flow

1. A recipe or external `SceneIntent` is validated against the registered
   categories, room types, and events.
2. The intent compiler and layout solver choose assets and deterministic poses
   from a seed.
3. Validation produces a portable scene directory and optional USD output.
4. Batch generation records portable paths, content hashes, and semantic
   fingerprints.
5. Articulated metadata is compiled into an interaction plan.
6. A dry-run or future physical executor produces an execution trace that can
   be validated independently of execution.
