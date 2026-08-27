# Compatibility Matrix

The pure-Python SDK is the v0.1 release-readiness scope. Physical simulator
acceptance is tracked separately and is not implied by the offline checks.

| Capability | Pure Python | Isaac Sim required | Status |
| --- | --- | --- | --- |
| Scene generation | yes | no | PASS |
| External `SceneIntent` | yes | no | PASS |
| Batch datasets | yes | no | PASS |
| Dataset validate/reproduce | yes | no | PASS |
| Articulation metadata | yes | no | PASS |
| Symbolic planner | yes | no | PASS |
| Dry-run executor | yes | no | PASS |
| Executor conformance | yes | no | PASS |
| Real Isaac Franka acceptance (P1-1/P1-2) | no | yes | validated on reference Isaac Sim 6.0.1 |
| Real Isaac RGB-D acceptance (P1-3) | no | yes | validated on reference Isaac Sim 6.0.1 |
| Isaac Lab | no | yes | not started |

The package declares Python `>=3.12`. The core install has no required runtime
dependencies. LLM integration, Isaac Sim, USD, and Gymnasium-related workflows
remain optional integrations with their own environment requirements.
