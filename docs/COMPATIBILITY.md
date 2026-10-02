# Compatibility Matrix

The pure-Python compiler is the portable baseline. Simulator execution and
robot acceptance are separate capabilities; a passing offline check does not
imply physical task success.

| Capability | Additional runtime | Status |
| --- | --- | --- |
| Scene generation, validation and SVG preview | none | PASS |
| External `SceneIntent` | none | PASS |
| Batch datasets, validation and reproduction | none | PASS |
| Articulation metadata, symbolic planner and dry-run executor | none | PASS |
| Executor conformance | none | PASS |
| MJCF export and portable scene bundles | none | PASS |
| Web UI and Three.js GLB preview | WebGL-capable browser | PASS with proxy fallback |
| MuJoCo environment stepping through `SceneFactoryEnv` | `mujoco>=3.3,<3.4` | PASS in the reference local environment |
| Real Isaac Franka acceptance (P1-1/P1-2) | Isaac Sim 6.0.1 and official Local Assets | validated on the reference environment |
| Real Isaac RGB-D acceptance (P1-3) | Isaac Sim 6.0.1 and official Local Assets | validated on the reference environment |
| Real articulated execution (P1-4) | Isaac Sim, official articulated asset and robot control | binding validation passed; physical acceptance not run |
| Isaac Lab | Isaac Lab | not started |
| Blender manifest + GLB/primitive scene preparation | none | PASS; render requires separate Blender installation |
| Blender .blend / PNG rendering | Blender executable | not run on reference machine (not installed) |
| Fresh Isaac USD export on the reference workstation | local Isaac pxr via ASCII Junction | PASS: USD Stage creation and fresh export verified on 2026-10-01 |
| Real robot | robot-specific hardware and integration | not run |

The package requires Python `>=3.12`. Its core dependency set is empty.
`mujoco` is an optional extra because MJCF files can be generated without the
simulator. `SceneFactoryEnv` selects `MujocoBackend` by default, so calling
`reset()` on that default environment requires the extra. Applications that
only need the portable contract can pass `DryRunBackend` explicitly.

Core CI skips simulator-only tests when the MuJoCo extra is absent; a separate
CI job installs and exercises the extra. Scene ZIPs with USD require OpenUSD
to localize dependencies into an embedded USDZ; MJCF/GLB-only bundles do not.
Windows Isaac bootstrap temporarily preserves the ASCII environment alias
only for paths within that environment. Its `realpath` hook is restored after
the import, including failed imports, and does not alter unrelated paths.

LLM, Isaac Sim, USD and Gymnasium workflows remain optional integrations with
their own environment requirements. The bundled Three.js modules and local GLB
files are visual resources; current MuJoCo collision geometry remains derived
from registered primitive bounds.
## Robot support matrix

| Robot body | SceneFactory runtime | Validation |
| --- | --- | --- |
| Franka Emika Panda | Isaac Sim arm + gripper, mug lift/pick-place/RGB-D | reference acceptance recorded; re-run per machine |
| UR5/UR10, xArm, mobile manipulator, dual-arm, humanoid/quadruped | no adapter implementation | not supported |
| Physical robot | no hardware integration | not supported |

`scene_factory/robot_specs.py` defines the extensibility contract, with exactly one registered spec. A dry-run or MuJoCo bounding-box proxy is not real robot acceptance. P1-4A articulated binding is read-only; P1-4B physical interaction remains outstanding. The baseline Isaac environment here reports Python 3.12, Isaac Sim 6.0.1.0, PyTorch `+cu128`, and CUDA 12.8. On Windows, use the automatically managed ASCII-only Isaac Python Junction; running Isaac through the original Chinese path can make USD/Kit fail even when `pxr` imports.
