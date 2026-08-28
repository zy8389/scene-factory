# P1-4 Technical Design: Real Articulated Interaction Execution

## Status

This document records the P1-4 scope discovery and the design that can be
derived from the current `main` branch.

The code architecture is sufficiently understood to define the integration
boundary. The physical acceptance contract is not frozen because the
configured Isaac Sim installation on this machine does not currently expose a
usable Isaac Python runtime or the required official Local Assets roots.

```text
Architecture Freeze: PARTIAL
Acceptance Freeze: BLOCKED
Implementation Started: NO
Isaac Lab Started: NO
Real Robot: NOT RUN
```

Evidence snapshot: 2026-08-28, release commit `7e0de6f419522f74cae4bb2314a7b3dc8a1eb906`.

The following items are intentionally not claimed by this document:

- no articulated USD was loaded or modified;
- no candidate asset was selected as the reference asset;
- no physical articulated execution was run;
- no runtime feature, planner action, executor implementation, or schema
  semantic was added.

The blocker is concrete rather than conceptual. The ordinary Python probe
reports no `isaacsim`, `pxr`, Isaac core, robot-motion, or PhysX extension
packages. The known Kit application directories contain application shells and
the compatibility-checker data, but none of the following paths exists:

```text
<asset-root>/Assets/Isaac/6.0/Isaac
<asset-root>/Assets/Isaac/6.0/NVIDIA
<asset-root>/Assets/Isaac/6.0/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd
```

Without a real asset stage, joint limits, articulation roots, link paths,
handle geometry, collision materials, mass properties, and Franka reachability
cannot be verified. Any exact asset name, USD path, joint range, stability
window, or controller offset written as final would be an invention.

## Motivation

SceneFactory v0.1.0 already represents articulated semantics and can generate a
symbolic interaction plan. It does not yet connect that plan to a real
articulated object in Isaac Sim through physical contact.

The gap is therefore between:

```text
semantic articulated metadata + symbolic plan
                    and
real Franka motion + handle contact + PhysX articulation motion
```

P1-4 is needed to close that gap without changing the meaning of the existing
semantic contracts. A passing P1-4 run must demonstrate that a robot caused an
observed articulated state transition in a real Isaac stage. A symbolic replay
or a direct joint assignment cannot establish that fact.

## Current Architecture

The current pipeline, reconstructed from `scene_factory/models.py`,
`scene_factory/factory.py`, `scene_factory/planning.py`,
`scene_factory/execution.py`, and `docs/ARCHITECTURE.md`, is:

```text
recipe / external SceneIntent
          |
          v
     SceneFactory
          |
          v
   compiled SceneSpec / scene output
       /       |        \
      v        v         v
  dataset     task     interaction planner
                              |
                              v
                       InteractionPlan
                              |
                              v
                    InteractionExecutor contract
                              |
                              v
                       ExecutionTrace
```

The boundaries are:

- **Canonical semantic boundary:** `SceneIntent`, asset metadata, compiled
  scene objects, task predicates, and interaction plans. These values are
  simulator-neutral and are already public or import-compatible v0.1 APIs.
- **Runtime boundary:** `InteractionExecutor` consumes a validated
  `InteractionPlan` as `ExecutionCommand` values and returns correlated
  `ExecutionStepResult` values plus a final snapshot.
- **Isaac-specific boundary:** `scene_factory/backends/isaac.py`,
  `scene_factory/exporters/isaac_usd.py`, and the standalone runtime tools.
  Isaac imports are lazy and must happen after `SimulationApp` starts in a
  clean child process.

`SimulatorBackend` remains a separate environment-level contract for reset,
step, render, and close. It must not be replaced by, or silently merged with,
the interaction executor contract.

### Articulation contract

`AssetRecord` currently carries the following semantic metadata:

- `ArticulationJoint`: joint id, type, parent and child link names, normalized
  axis, lower and upper limits, and default position;
- `InteractionRegion`: region id, semantic kind, link name, center, size,
  approach axis, allowed semantic actions, and optional controlled joint;
- `InteriorRegion`: semantic interior region and link;
- `SemanticState`: named joint range and optional target position;
- support surfaces with optional semantic articulation link references.

Construction validates unique joint and region ids, acyclicity, link and joint
references, finite limits, valid defaults, non-overlapping semantic ranges,
and support-surface links. The serialized interaction snapshot contains joint
positions, limits, regions, semantic states, and interior regions.

These fields describe the object and its intended interaction semantics. They
do not identify Isaac prim paths, joint prim paths, rigid-body handles,
collision prims, or simulator-local frames. They are therefore descriptive
metadata, not a physical USD binding.

### Planner and symbolic replay

`InteractionPlan` is `scene_factory.interaction_plan.v1`. Its action vocabulary
is already sufficient for the proposed first slice:

```text
approach, grasp, pull, push, rotate, release
```

For a compatible prismatic drawer fixture, the existing planner emits:

```text
approach -> grasp -> pull -> release
```

For a compatible revolute door fixture, it emits:

```text
approach -> grasp -> rotate -> release
```

The plan stores semantic joint targets and a hash over semantic plan content.
`apply_symbolic_interaction_action()` and replay update a Python world-state
mapping. They validate ownership, action compatibility, direction, limits,
semantic ranges, and grasp prerequisites, but they do not load USD, step
PhysX, move an end effector, detect contacts, or prove reachability.

Consequently:

```text
symbolic replay is not physics replay
```

### Existing executor contract

`InteractionExecutor` exposes:

```text
capabilities()
reset(scene, initial_state)
execute(command)
snapshot()
close()
```

`ExecutorCapabilities` declares `physical`, `supported_actions`, and
`articulation_execution`. `ExecutionCommand` is
`scene_factory.execution_command.v1` and has a deterministic id derived from
the plan hash and step id. `ExecutionTrace` is
`scene_factory.execution_trace.v1` and records the command/result sequence,
final evidence, goal status, failure reason, and trace hash.

`DryRunInteractionExecutor` deliberately reports `physical=false` and applies
the symbolic transition helper. It is useful for contract, planner, and trace
tests. It is not a physical acceptance implementation:

```text
DryRun PASS != physical PASS
```

The core conformance suite already checks lifecycle, capability validity,
command correlation, evidence JSON validity, action execution, and trace
validation. It does not establish contact causality in Isaac Sim.

### TaskEvaluator

The existing `TaskEvaluator` supports the `articulation_state` predicate. It
resolves an object, joint, named state, and numeric range, then reads the
current position from either `evidence["articulation_positions"]` or the
supplied state mapping. It returns success when the observed value is inside
the requested range.

This is the correct terminal semantic predicate to reuse, but it is only one
part of a physical gate. By itself it does not require a physical executor,
contact, gripper state, end-effector motion, force, or physics stepping.

## Scope

### Recommended P1-4 definition

**P1-4 - Real Articulated Interaction Execution**

Connect the existing simulator-neutral articulated interaction plan and
executor contract to one official Isaac Sim articulated asset and one official
Franka, then pass one deterministic physical manipulation acceptance in which
the robot contacts the asset's interaction region and causes the target joint
to enter an observed semantic state.

The smallest implementation slice should be:

```text
one official articulated asset
one Franka robot
one joint and one interaction region
one manipulation family
one deterministic acceptance task
```

The preferred task family is a drawer opening task because the current
contracts already express a prismatic joint, a `pull` action, a handle region,
and an `articulation_state` goal. This is a provisional task-family
recommendation only. It is not a reference-asset decision until a real drawer
asset passes the required inspection and reachability checks.

### Included

- read-only asset discovery and inspection against Isaac Sim 6.0.1 Local
  Assets;
- an Isaac-specific binding description that maps canonical semantic ids to
  verified USD prims and frames;
- one physical Isaac executor implementing the existing
  `InteractionExecutor` protocol;
- one physical controller path for the selected asset and action family;
- fail-closed validation of asset binding, joint limits, action support, and
  physical evidence;
- real acceptance reports and correlated `ExecutionTrace` evidence;
- deterministic reset, repeatability, negative tests, and documentation;
- pure-Python regression coverage for all shared contracts.

### Explicitly excluded from P1-4

- multiple articulated assets or multiple manipulation families;
- general USD articulation discovery or automatic handle detection;
- a general-purpose grasp planner or learned grasping;
- new planner actions or LLM planner behavior;
- vision policies, language control, RL, training, domain randomization, or
  Isaac Lab environments;
- multiple robots, mobile bases, or real robot execution;
- mandatory RGB-D capture or a new dataset schema;
- changing `TaskEvaluator` semantics or weakening existing thresholds;
- adding official or third-party USD assets to the repository;
- replacing the existing executor contract with a second executor hierarchy.

## Reference Asset

### Asset inspection result

No official articulated asset could be inspected in this environment. The
required search roots were checked only at known Isaac installation locations:

```text
Assets/Isaac/6.0/Isaac
Assets/Isaac/6.0/NVIDIA
```

Neither root was present. The Isaac package probe also failed before any
`SimulationApp` startup, so no read-only `pxr` stage inspection was possible.
No asset was downloaded, generated, copied into the repository, or used as a
proxy.

### Candidate matrix

The rows below are search targets, not inspected candidate assets. They are
included to make the unresolved decision explicit rather than to imply that
an asset exists locally.

| Search target | Official USD path | Articulation/joints | Handle/link | Collision/mass | Franka reachability | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| Drawer-family asset | unavailable | unavailable | unavailable | unavailable | unavailable | blocked |
| Hinged door-family asset | unavailable | unavailable | unavailable | unavailable | unavailable | blocked |
| Cabinet/storage-unit asset | unavailable | unavailable | unavailable | unavailable | unavailable | blocked |

For each real candidate, the next discovery pass must record the exact asset
path, name, source or license metadata, articulation root, joint count and
types, joint names and limits, default pose, handle/link structure, collision
and mass APIs, physics material, visual quality, Franka accessibility,
SceneFactory mapping complexity, and a repeatable reset pose.

### Reference-asset decision

```text
Selected reference asset: none
Reference-asset freeze: BLOCKED
Preferred family pending inspection: one prismatic drawer asset
```

The final reference asset must be an official Local Asset loaded from the
configured Isaac root. A proxy, hand-authored USD, or uninspected asset cannot
be promoted to the P1-4 acceptance reference. The exact object id, joint id,
joint path, handle path, semantic ranges, approach frame, controller offsets,
settle window, and physical tolerances remain open until the asset inspection
is completed.

## Architecture

The recommended integration reuses the existing plan and command boundaries:

```text
SceneFactory scene + interaction snapshot
                 |
                 v
          InteractionPlan v1
                 |
                 v
       ExecutionCommand v1
                 |
                 v
      IsaacInteractionExecutor
          |              |
          v              v
   Franka controller   USD/PhysX binding
          \              /
           v            v
       observed physical state
                 |
                 v
       ExecutionStepResult
                 |
                 v
          ExecutionTrace v1
                 |
                 v
        TaskEvaluator terminal check
```

The new implementation should be an Isaac-specific adapter named
`IsaacInteractionExecutor` unless implementation discovery shows that a more
precise local name is needed. The name is not an API commitment in this
documentation-only change. The important architectural commitment is that it
implements the existing `InteractionExecutor` protocol and does not create a
second command or trace contract.

The current `IsaacSimBackend` is optimized for the mug-lift environment flow
and uses a `MugLiftController`. It may provide reusable Isaac process,
Franka-loading, IK, gripper, contact-report, and evidence techniques, but it is
not itself the P1-4 executor. P1-4 should not force articulated plan commands
through the mug-lift state machine.

## Asset Binding

### Canonical semantics

Keep `ArticulationJoint`, `InteractionRegion`, `InteriorRegion`,
`SemanticState`, and `SupportSurface.link` simulator-neutral. Their meanings
are semantic ids, local geometry, axes, ranges, and links used by SceneFactory
and the planner. They should not be made dependent on Isaac prim naming.

### Isaac-specific binding

The physical adapter needs a separate binding object or adapter-owned
configuration that is resolved only for the selected asset. Its eventual
fields should be limited to verified runtime facts such as:

```text
asset source / resolved USD path
articulation root prim path
canonical joint id -> USD joint prim path
canonical link name -> USD link prim path
canonical interaction region -> handle or grasp prim path
local frame / pose transform for the interaction region
verified collision roots
```

This binding should be kept in an Isaac-specific module, runtime configuration,
or generated acceptance fixture. It should not be added to the canonical v0.1
asset semantics merely because one simulator has stable-looking prim paths.

The binding resolver must fail closed when a required prim is missing,
ambiguous, has the wrong type, has unexpected joint type or axis, lacks usable
limits, lacks required collision, or cannot be mapped to the canonical joint
and region. A name-based fallback that silently selects the first matching
prim is not acceptable.

### Resolution rules

1. Resolve the selected official asset from the Isaac Local Assets root and
   confirm the stage can load in the clean Isaac child process.
2. Locate exactly one articulation root and exactly one controlled joint for
   the selected semantic joint.
3. Read the runtime joint type, axis, limits, default pose, and current
   position. Compare them with the binding expectations and canonical semantic
   metadata.
4. Resolve the link and handle collision roots needed for approach and contact
   evidence. Record their exact prim paths in the run report.
5. Confirm required mass, collision, and physics material data before any
   manipulation command is accepted.
6. Refuse execution if any mapping is missing, ambiguous, out of limits, or
   inconsistent with the selected semantic state.

### Schema and compatibility impact

The documentation recommendation is no schema change for P1-4A. The existing
v1 interaction plan and v1 execution trace can carry the semantic plan and
open evidence objects. If implementation proves that binding data must be
serialized for portable replay, it must be proposed separately with an
explicit schema compatibility review. It must not be smuggled into v1 by
loosening `additionalProperties` or by changing the meaning of an existing
field.

## Executor Design

### Protocol reuse

`IsaacInteractionExecutor` should implement:

```text
capabilities()
reset(scene, initial_state)
execute(command)
snapshot()
close()
```

It should report `physical=true` only when it is connected to the live Isaac
stage and the physical evidence pipeline is available. It should report
`articulation_execution=true` only when the selected USD articulation was
resolved and its joint state can be observed from runtime APIs.

### P1-4 v1 capabilities

For the provisional drawer slice, the honest action set is:

```text
approach
grasp
pull
release
```

`push` and `rotate` should be absent from capabilities unless the selected
asset and controller genuinely support them. Returning `not_supported` for an
unsupported action is correct; advertising all six actions and failing at
runtime is not.

### Lifecycle

`reset` must validate the scene, binding, initial semantic state, USD stage,
physics scene, robot, controller, and diagnostic APIs before reporting a
ready state. It must leave the robot ungrasped and the controlled joint in the
verified initial range.

`execute` must process one command synchronously, preserve command id and
step id, advance physics, and return evidence from observations made after the
command. It must not write the target articulation joint to make the command
pass.

`snapshot` must return the observed joint positions and terminal diagnostics
needed by `TaskEvaluator`. `close` must stop and close the Isaac objects; a
close failure must prevent a passing overall result, matching the existing
orchestrator behavior.

### Evidence contract

The existing evidence fields are open JSON objects, so the first physical
adapter can add structured evidence without changing the trace envelope. The
following evidence groups are required for a physical pass:

| Action | Minimum observed evidence |
| --- | --- |
| `approach` | target region world pose, end-effector pose, distance/error, IK/controller status, physics step count |
| `grasp` | gripper joint positions and limits, handle-relative pose, contact API status, actual finger-to-handle contact pair, contact force/read status |
| `pull` | controlled joint position before and after, target position/range, joint limits, contact maintained during motion, physics step count |
| `release` | gripper state, contact separation observation, controlled joint position, post-release observation |

The final snapshot must include at least observed articulation positions,
terminal task status, relevant prim paths, controller state, contact summary,
step count, Isaac version, and asset-source diagnostics. Exact field names can
be finalized during implementation, but a result that contains only
`{"success": true}` is not physical evidence.

## Control Strategy

The controller should be the smallest deterministic path for the selected
asset, reusing the existing Franka runtime patterns where they apply.

### Approach

Use the verified interaction-region frame and approach axis to generate a
pre-grasp pose. The binding must define the frame conversion from the asset
link or handle to the robot end-effector target. Reuse the existing Lula IK
and Franka drive setup rather than introducing a new motion-planning stack.

Approach passes only when IK converges, the pose and orientation error are
inside implementation thresholds, and the end effector is observed near the
verified region. The exact thresholds require the selected asset inspection.

### Grasp

Open the gripper using runtime finger DOF limits, move to the pre-grasp and
grasp poses, close through the existing physical gripper interface, and verify
the actual handle contact pair. The adapter must record the two finger
colliders, handle collision root, resolved materials where available, and
contact force/read validity.

A gripper command or a declared holding flag is not sufficient. Grasp success
requires observed contact with the selected handle or region and a stable
relative configuration for the verification window defined after asset
inspection.

### Pull

For the preferred prismatic drawer slice, generate a Cartesian motion along
the verified joint axis while maintaining the handle contact. The controller
may use the joint axis and the desired semantic target to compute a bounded
trajectory, but the articulation joint itself must respond through contact and
PhysX.

The adapter must observe the controlled joint before and after each motion
segment. It must fail if the joint does not move, violates limits, loses
contact before the required phase, or reaches the target only after a direct
joint write.

If asset inspection selects a hinged door instead, the same boundary applies
with a bounded tangential/rotational handle trajectory and the existing
`rotate` action. The door path remains an alternative, not simultaneous P1-4
scope.

### Release and failure detection

Open the gripper, observe contact separation, and continue physics for a
post-release stability window. The task is not complete until the observed
joint remains in the selected semantic range and `TaskEvaluator` returns true.

Every phase needs a timeout and must fail closed on unavailable IK, missing
contacts, non-finite state, joint limit violation, controller divergence,
stage failure, or unsupported evidence APIs.

## Acceptance Contract

This section defines the invariant contract. Asset-dependent values are
explicitly pending the blocked inspection and must be filled from the selected
official USD, not guessed.

### Preconditions

- Isaac Sim must be 6.0.1.0 with the supported OpenUSD/PhysX runtime.
- The run must use the official Local Assets root and an official Franka USD.
- The controlled articulated asset must be an official Local Asset loaded from
  the same reference environment and must not be a proxy or generated USD.
- The runtime must start `SimulationApp` in a clean child process before
  importing `omni.*`, Isaac core, motion-generation, or `pxr` runtime modules.
- The run must use one documented scene seed and controller configuration.
- Output and USD paths must satisfy the existing Windows ASCII-path rule.
- The reference machine must record Isaac version, asset root diagnostics,
  resolved USD paths, GPU/driver context, and configuration hash.

### Initial state

Before the first command:

- the selected object is loaded and its exact articulation binding is valid;
- the controlled joint is in the verified closed semantic range and within
  runtime limits;
- the Franka is initialized at the documented base pose and is not holding the
  handle;
- the gripper is open and no target contact is active;
- the stage has a valid physics scene and has completed deterministic reset
  settling;
- the initial observed snapshot is finite and includes the controlled joint
  position and binding diagnostics.

The exact closed range and reset pose are blocked until a real asset is
inspected.

### Goal

The plan goal must be the existing `articulation_state` task for the selected
object and semantic state. For the provisional drawer family this is:

```text
Franka physically opens the selected drawer.
```

The target is entry into a verified semantic `open` range, not merely an
arbitrary positive joint displacement. The range and target position must be
derived from the selected asset's verified limits and reset pose.

### Physical pass conditions

All of the following are required:

1. The plan validates symbolically before the executor is reset.
2. The executor declares `physical=true` and
   `articulation_execution=true` with only honestly supported actions.
3. The official asset, Franka, articulation root, controlled joint, region
   link, handle collision root, and required materials/colliders resolve
   uniquely.
4. Approach reaches the verified interaction region through controller motion
   and physics stepping.
5. Grasp produces the required observed finger-to-handle contact evidence.
6. The controlled joint changes through physical robot interaction while the
   contact evidence is present; no direct target-joint assignment, object
   teleport, or USD transform edit is used as a success mechanism.
7. The observed joint remains within its runtime limits and enters the
   selected semantic target range.
8. Release opens the gripper, observes contact separation, and preserves the
   target state through the post-release stability window.
9. The terminal `TaskEvaluator` result is true from the observed articulation
   position.
10. Every plan command has a correlated result, and the execution trace
    records enough evidence to audit the physical conditions above.
11. The run exits cleanly. A close failure converts a nominal success to
    failure.

### Measurements to record

Each accepted run must record:

- initial and final controlled-joint position;
- runtime lower and upper limits;
- semantic target range and target position;
- joint displacement and normalized progress, if normalization is defined by
  the selected asset;
- end-effector pose and controller error for approach/grasp/pull;
- gripper joint positions, limits, and state;
- contact API availability, contact force-read validity, contact event count,
  active finger-to-handle pairs, and contact duration;
- resolved asset, articulation, joint, link, and handle prim paths;
- physics step count, simulated time, Isaac version, seed, and config hash;
- terminal `TaskEvaluator` status and complete failure reason when failed.

### Repeatability

The first physical acceptance must include two independent runs from the same
exact release commit, seed, asset, and controller configuration. Both runs
must satisfy the complete physical pass conditions.

The acceptance must compare metric ranges rather than require exact floating
point identity. The allowed joint, pose, contact-duration, and post-release
variation must be selected after inspecting the real asset and measuring the
reference machine. Those values are currently unfreezable because the asset
and runtime are unavailable.

No claim of acceptance may be made from one symbolic run, one failed physical
probe, or a bundled URDF fallback.

## Failure Modes

The adapter and acceptance harness must return a useful failed trace and must
not silently substitute a different execution mode.

| Failure | Required behavior |
| --- | --- |
| Isaac runtime missing | fail before physical execution with an unavailable-runtime reason |
| Official Local Assets root missing | fail closed; do not use a guessed path or proxy |
| Wrong Isaac version | fail the reference gate |
| Missing or ambiguous articulation root | fail binding resolution |
| Missing or wrong controlled joint | fail binding resolution |
| Joint type, axis, or limits disagree | fail binding validation |
| Missing interaction region or handle collision | fail before `grasp` |
| Unsupported action | do not advertise it; return `not_supported` if commanded |
| IK cannot reach the region | fail approach with controller evidence |
| Contact API unavailable | fail grasp/physical acceptance, not pass symbolically |
| No finger-to-handle contact | fail grasp |
| Joint does not move under contact | fail pull/rotate |
| Joint leaves limits | fail immediately and preserve diagnostics |
| Final semantic range not reached | fail terminal goal evaluation |
| Release does not separate or state is unstable | fail release/stability gate |
| Executor claims success without evidence | reject during physical acceptance and trace validation |
| Direct joint/object/USD manipulation detected | invalidate the run |
| Close or report finalization fails | overall result is failed |

## Negative Tests

At minimum, the implementation PRs must cover these negative cases:

1. The asset root is absent or resolves to a non-official path.
2. The selected USD has no articulation root.
3. The expected joint is missing, ambiguous, the wrong type, or outside the
   declared limits.
4. The interaction region is missing, maps to the wrong object, or does not
   control the selected joint.
5. The handle/link collision prim cannot be resolved.
6. A plan requests an action not present in executor capabilities.
7. The target position is outside runtime limits or semantic range.
8. Approach cannot reach the interaction region within the phase timeout.
9. Grasp closes without a real finger-to-handle contact pair.
10. The joint fails to move while the robot is in contact.
11. The controller loses contact before the required actuation completes.
12. The joint reaches a value only because a test writes it directly.
13. The final observed semantic state is outside the target range.
14. Release does not separate or the state is not stable after release.
15. A fake executor returns success with empty or non-physical evidence.
16. Command id, step id, plan hash, or scene id correlation is incorrect.
17. A failed step is followed by additional steps.
18. The executor closes with an exception after an otherwise nominal pass.

The existing pure-Python tests already cover many symbolic versions of these
conditions. P1-4 must add runtime-specific tests for binding, contacts,
observed articulation motion, and fail-closed physical evidence.

## Trace / Schema Impact

### Existing trace reuse

The existing `scene_factory.execution_trace.v1` envelope is sufficient for the
first physical slice if its open evidence objects are populated consistently.
The existing orchestrator already validates plan hash, scene id, command
correlation, contiguous steps, terminal result, final evidence, and
`TaskEvaluator` agreement.

The physical adapter must not bypass the orchestrator or mark a trace passed
solely from an executor-declared success. The final snapshot must contain
observed `articulation_positions` so the existing evaluator checks the real
joint value.

### Additive evidence

The following evidence is expected to be added inside existing step and final
evidence objects:

```text
asset_binding
articulation_root_path
joint_path
link_path
handle_collision_path
joint_limits
joint_position_before
joint_position_after
end_effector_pose
gripper_state
contact_diagnostics
physics_steps
controller_status
```

Exact names and nesting should be finalized in the implementation PR after
the asset inspection. They must remain finite JSON and preserve the existing
trace hash behavior.

### Version impact

No v1 semantic change is authorized by this design-only PR. A new trace or
binding schema is required only if the physical evidence cannot be represented
without changing the meaning of existing fields. Any such change must be an
explicit follow-up with schema fixtures, compatibility policy, and migration
notes. Do not weaken strict top-level validation to avoid versioning work.

## Sensor Scope

RGB-D is not required for the P1-4 primary gate.

P1-3 already demonstrates synchronized RGB/depth capture, camera setup, and
trajectory recording in the existing `SimulatorBackend` flow. P1-4's question
is physical articulated execution and causality, which can be evaluated from
physics state, robot state, contact state, and task evidence. Requiring RGB-D
would expand the first articulated acceptance into a sensor and dataset
integration project without improving the core physical proof.

RGB-D may be an optional diagnostic or a later dataset integration, reusing
the existing P1-3 path. It must not be a required P1-4 merge gate unless a
separate roadmap decision changes the scope.

## P1-3B `task_spec` Gap

The P1-3B episode path currently allows an episode to validate while reporting
`task_spec` absent and `task_replay_available=false`. This is a dataset episode
enrichment gap, not a blocker for the minimal physical articulated execution
loop.

```text
Part of P1-4: NO
Recommendation: separate follow-up
```

P1-4 should still record its task goal and terminal evaluator evidence in its
execution trace. It should not change the trajectory schema or retrofit
`task_spec` into existing P1-3B episodes as unrelated scope.

## PR Breakdown

The documentation branch describes the following implementation sequence. The
PRs are proposed work items, not work performed by this task.

### P1-4A - Official Asset Binding and Runtime Inspection

**Scope:** inspect the official Local Assets roots, select one real asset,
define an Isaac-specific binding, resolve the articulation/joint/link/handle
paths, and validate limits, collision, mass, materials, reset pose, and
mapping.

**Non-scope:** no robot manipulation, no planner changes, no new actions, no
Isaac Lab, no repository asset copy, and no physical task pass claim.

**Likely files:** a new Isaac adapter/binding module, focused binding tests,
an external or local acceptance inspection tool, and documentation. Exact
paths must follow existing module ownership after implementation begins.

**New APIs:** only an Isaac-specific binding API if needed. Keep canonical
`AssetRecord` and v1 schemas unchanged unless a compatibility review proves
otherwise.

**Tests:** pure-Python malformed binding tests; fixture-stage resolution tests;
real Isaac read-only inspection; fail-closed missing/ambiguous prim tests.

**Real gate:** the official asset loads, its exact joint and handle mapping is
resolved, runtime limits and default pose are recorded, and all required
collision/physics facts are available.

**Merge gate:** full pure-Python suite, static checks, and a reference-machine
read-only inspection report. No P1-4 physical pass is claimed by this PR.

**Complexity:** Medium code complexity, Large Isaac/environment risk, Medium
test complexity, Medium architecture risk.

### P1-4B - Physical Isaac Interaction Executor

**Scope:** implement the existing `InteractionExecutor` protocol for the
selected asset and Franka, with honest capabilities and physical evidence for
`approach`, `grasp`, `pull`, and `release`.

**Non-scope:** multiple assets, generalized manipulation, door support in the
same slice, vision policy, learning, new plan actions, or trace schema
replacement.

**Likely files:** the Isaac-specific executor module, controller helpers,
runtime launch/acceptance tooling, and executor/binding tests. It should reuse
the existing Franka loading, Lula IK, gripper, contact reporting, and
parent/child process patterns where applicable.

**New APIs:** an Isaac executor implementing the current protocol and an
adapter-owned evidence/binding surface. No second generic executor protocol.

**Tests:** protocol and capability tests; lifecycle tests; command/result
correlation tests; binding mismatch tests; fake-stage/controller tests; real
contact and observed-joint tests on the reference machine.

**Real gate:** the executor reports physical capability only after binding and
diagnostic initialization; each command produces physical evidence and the
joint moves through contact rather than direct assignment.

**Merge gate:** pure-Python tests plus a reference-machine smoke that fails
closed when Isaac is unavailable. The generic conformance suite remains
mandatory.

**Complexity:** Large code complexity, Large Isaac risk, Large test
complexity, Medium architecture risk.

### P1-4C - Reference Asset Physical Acceptance

**Scope:** run the exact selected plan against the exact selected official
asset, prove approach, grasp, physical actuation, release, semantic success,
trace evidence, and two-run repeatability.

**Non-scope:** new controller families, asset generalization, RGB-D gate,
Isaac Lab, CI simulation, or real robot execution.

**Likely files:** the reference recipe/configuration, acceptance launcher and
report validation, focused integration tests, and acceptance documentation.

**New APIs:** none expected; prefer configuration and acceptance reports over
new public API.

**Tests:** two independent reference-machine runs; failure injection for
missing contact, no joint motion, out-of-range target, and final semantic
failure; trace reload and validation.

**Real gate:** both runs satisfy every physical pass condition in this
document, with asset-dependent thresholds filled from inspected runtime facts.

**Merge gate:** all pure-Python checks and attached reference-machine reports
are passing; no symbolic or bundled-URDF substitute is accepted.

**Complexity:** Medium code complexity, Large Isaac risk, Large test
complexity, Medium architecture risk.

## Dependency Graph

```text
official Isaac runtime + Local Assets inspection
                    |
                    v
             P1-4A binding
                    |
                    v
             P1-4B executor
                    |
                    v
        P1-4C physical acceptance
                    |
                    v
        later sensor/dataset integration
```

P1-4A depends on a usable Isaac 6.0.1 Python environment and official local
asset roots. P1-4B depends on one accepted binding, existing Franka/Lula/
gripper runtime patterns, and the unchanged executor contract. P1-4C depends
on P1-4B, a deterministic reset, a fixed seed/configuration, and a reference
machine with enough GPU/RAM for repeated runs. Isaac Lab depends on P1-4C,
not merely on a symbolic plan or binding fixture.

## Risks

| Risk | Impact | Mitigation / gate |
| --- | --- | --- |
| Handle geometry is difficult to resolve | grasp cannot be repeatable | inspect real collision geometry and bind an explicit handle frame |
| USD prim paths change | binding silently targets the wrong prim | exact path/type checks, ambiguity rejection, recorded binding report |
| Contact is unstable | false grasp or lost drawer | actual contact pairs, force/read validity, contact-duration gate |
| Franka cannot reach the handle | no physical task | verify reachability before choosing the reference asset |
| Friction/damping is unsuitable | pull stalls or oscillates | inspect material and drive data; tune adapter-owned config on reference machine |
| Semantic and physical ranges differ | false terminal result | compare runtime limits and semantic ranges before execution |
| Isaac details leak into core metadata | simulator lock-in | keep binding in Isaac-specific adapter/configuration layer |
| Physics is nondeterministic | one-off acceptance | two exact-head runs and measured metric ranges |
| Real tests are expensive | slow feedback | keep unit/contract tests pure Python; run one focused reference gate |
| GitHub CI lacks Isaac | false confidence | separate local/reference-machine gate from CI and report it explicitly |
| Hardware limits are tight | runtime crash or timeout | headless low-resolution single-GPU reference configuration |
| Trace evidence is too weak | lying executor can pass | require observed articulation positions, contacts, commands, and evaluator agreement |

## CI Strategy

### Pure-Python CI

GitHub CI should continue to run:

- ruff/static checks;
- compile checks;
- model, planner, evaluator, execution, conformance, and trace tests;
- fixture-stage or mock-adapter tests that do not import Isaac before the
  runtime boundary;
- packaging and release checks;
- schema fixture validation.

These tests must not pretend that a mock or dry-run pass is a physical Isaac
pass.

### Real Isaac acceptance

The P1-4 physical gate remains a local or reference-machine acceptance because
the current hardware and GitHub-hosted runners do not provide a supported
Isaac Sim environment. The gate must publish its version, asset root, resolved
paths, seed, metrics, evidence, and full failure reason. A skipped or
unavailable Isaac run is `not run` or `blocked`, never `passed`.

## Isaac Lab Boundary

P1-4 should provide Isaac Lab with evidence-backed building blocks:

- one validated official articulated asset and binding;
- deterministic reset and initial state;
- a stable semantic joint observation;
- an action/command mapping for one manipulation family;
- physical contact and controller diagnostics;
- a stable task success predicate and trace format;
- a known reference-machine acceptance procedure.

P1-4 does not define an RL observation space, action space, reward, rollout
collector, policy, vectorized environment, domain randomization scheme, or
training pipeline.

Isaac Lab entry criteria are:

1. P1-4C physical acceptance passes twice from the exact release commit.
2. Reset reproduces a valid initial semantic state and verified binding.
3. Observed joint and contact signals are stable and finite.
4. The physical command interface and success predicate are documented and
   unchanged.
5. The pure-Python contract and trace tests remain green.

## Definition of Done

P1-4 is complete only when all of the following are true:

1. One official Isaac Sim 6.0.1 articulated asset is selected from the local
   asset roots and its source/path is recorded.
2. The selected articulation root, controlled joint, interaction link, handle
   collision, limits, default pose, collision, mass, and material facts are
   verified from the real USD/runtime.
3. Canonical semantic metadata binds to those verified runtime prims without
   simulator-specific fields being forced into core semantics.
4. A physical Isaac executor implements the existing `InteractionExecutor`
   protocol and advertises only real capabilities.
5. Franka approaches the verified interaction region and establishes observed
   contact.
6. The controlled joint moves due to robot interaction and PhysX stepping; no
   direct joint/object/USD pose edit is used as a success mechanism.
7. The target semantic state is reached within verified runtime limits.
8. Release and post-release stability pass.
9. `TaskEvaluator` confirms success from observed articulation state.
10. The `ExecutionTrace` contains correlated physical evidence and reloads
    successfully.
11. Two exact-head canonical reference runs pass using the same selected
    asset, seed, and configuration.
12. Negative tests cover binding, capability, contact, motion, final-state,
    evidence, correlation, and lifecycle failures.
13. Pure-Python regression, static, compile, release, and diff checks remain
    green.
14. No schema semantics, evaluator semantics, or existing thresholds were
    weakened.

## Documentation PR

This task is documentation-only. After this document is validated, the change
should be published from:

```text
branch: codex/p1-4-scope-design
commit: Define P1-4 real articulated execution scope
title: [codex] Define P1-4 real articulated execution scope
draft: yes
```

The Draft PR must contain only this technical design document. The missing
Isaac runtime and asset roots must be visible in the PR as the reason the
architecture status is partial and the acceptance status is blocked. The PR
must not add a generated USD, an asset package, an acceptance report that
pretends to be physical, or implementation code.

## Final Decision

```text
Recommended P1-4: Real Articulated Interaction Execution
Reference articulated asset: none selected; official Local Asset inspection is blocked
Preferred task family: one prismatic drawer opening task, pending real inspection
Architecture Freeze: PARTIAL
Acceptance Freeze: BLOCKED
Implementation Started: NO
Isaac Lab Started: NO
Real Robot: NOT RUN
```

The next unblocker is environmental, not a change in scope: restore or expose
the Isaac Sim 6.0.1 Python executable and the official Local Assets roots,
rerun the read-only candidate inspection, select one real asset, and fill the
asset-dependent binding and metric values before starting P1-4A.
