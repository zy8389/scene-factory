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
| Real robot | robot-specific hardware and integration | not run |

The package requires Python `>=3.12`. Its core dependency set is empty.
`mujoco` is an optional extra because MJCF files can be generated without the
simulator. `SceneFactoryEnv` selects `MujocoBackend` by default, so calling
`reset()` on that default environment requires the extra. Applications that
only need the portable contract can pass `DryRunBackend` explicitly.

LLM, Isaac Sim, USD and Gymnasium workflows remain optional integrations with
their own environment requirements. The bundled Three.js modules and local GLB
files are visual resources; current MuJoCo collision geometry remains derived
from registered primitive bounds.