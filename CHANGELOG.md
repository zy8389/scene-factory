# Changelog

## Unreleased

### Added

- eleven original kitchen/dining USD and GLB assets with shared mesh geometry,
  authored convex collision parts, real contact/drop QA and portable MuJoCo
  meshes; three recipes add shelf containment, chairs around tables and explicit
  reserved corridors with independent validation and web previews.

- web Franka acceptance queue with persistent job status, verified read-only
  history, per-case physics/task results, and recorded-state trajectory replay.

- generated kitchen single-mug layouts can be screened, frozen at submission,
  and executed as source-bound Franka lift/place tasks in the same web queue.

- structured table-edge cup scenarios with measured support clearances, current
  task and pause consequences; advisory LLM priority decisions through the CLI
  and web UI preserve the distinction between motion assumptions and physics evidence.

- opt-in Franka controller settings with strict finite-value validation, active
  drive-gain readback, and separate tracking/hold recipes for paired comparisons
  against unchanged baseline layouts and task success conditions.

- sequential Isaac acceptance for paired sampled mug-lift/place candidates,
  source and asset fingerprints, real settling/contact/state traces, independently
  recomputed quality, offline validation, resumable batches and a Chinese results
  preview; Windows launcher/worker identity is verified through the live process tree.

- nine authored household GLB visuals for living-room and entry clutter, with
  reproducible Blender generation, hashes and packaged source metadata.

- Blender cases include a Windows one-click generator and usage instructions;
  the web can generate and download a texture-packed .blend file and include it
  in the case ZIP, with executable discovery for running and common installations.

- configuration-grouped candidate splits that keep shared initial layouts together
  across tasks, plus verified selected-artifact export and an offline HTML preview.

- stratified sampling plans with bounded candidate pools, quality quotas,
  region filtering, cross-stratum deduplication, coverage reports and resumable
  collection generation; candidate datasets retain their existing v1 contract.

- separate mug-lift and pick-and-place data recipes with seeded start positions
  outside the placement target; fixed robot acceptance recipes remain available.

- offline layered scene quality reports, dataset quality selection, failure
  taxonomy, coverage statistics and opt-in exact-layout deduplication; physics
  and task checks remain explicitly unverified, with existing v1 datasets supported.

- MuJoCo MJCF export and a lazy MuJoCo environment backend;
- portable scene bundles with artifact hashes, asset metadata and local GLB
  visual files;
- a Three.js WebGL scene preview with bounding-box fallback;
- eight CC0 furniture and kitchen visual assets with source manifests and
  notices.
- a 17-object after-cooking kitchen slice with seven additional CC0 appliance,
  cookware, food and utensil meshes.

### Changed

- core CI now runs on Ubuntu and Windows with Python 3.12; Windows checks
  include artifact builds and a fresh wheel installation outside the checkout.
- release documentation records the published v0.1.0 tag and GitHub artifacts
  separately from current development changes and next-release readiness.

- detailed prompts use semantic parsing even when fast templates are enabled;
  parser context and cache keys include asset capabilities. Intent compilation
  preserves counts, colors and entry regions, and the sofa seat uses measured height.

- generated Blender files open with a framed material preview and hidden helper
  overlays; transform holders remain available for editing and lights for rendering.

- visual models now share the collision proxy's centered bottom anchor; top
  supports fit their registered dimensions in web, scene bundles and Blender
  cases, and the kitchen counter depth encloses its declared support surface.

- layout sampling exhaustion retries bounded full layouts while preserving
  selected assets and fixtures; retries constrain regional sampling to usable
  support bounds, and previously successful layouts keep their original output.

- scene builds and the Web UI export MJCF by default;
- `SceneFactoryEnv` selects the MuJoCo backend by default, while explicit
  `DryRunBackend` use remains dependency-free;
- documentation and release artifact checks now describe the visual-asset and
  MuJoCo workflow boundaries.
- Three.js visual assets now use documented axis transforms, uniform scaling and
  bottom alignment; the kitchen preview includes walls, backsplash and lighting.

## 0.1.0 - 2026-08-28

Published as an early developer release on
[GitHub](https://github.com/zy8389/scene-factory/releases/tag/v0.1.0).
Features listed under `Unreleased` belong to the current development branch and
are not included in the published v0.1.0 artifacts.

### Added

- deterministic household scene generation from recipes and external intents;
- reproducible batch datasets with validation and reproduction checks;
- articulated asset metadata and symbolic interaction planning;
- dry-run interaction execution and execution trace validation;
- executor capability inspection and core conformance reporting;
- a documented public Python API, runnable examples, and clean-install smoke coverage.

### Status

- Pure-Python SDK workflows are available without Isaac Sim, GPU, NumPy, or a
  network connection.
- Reference Isaac Sim 6.0.1 Franka and RGB-D acceptance passes are recorded for
  the official Local Assets environment; these checks remain environment-specific
  and are separate from the portable SDK release checks.
- Git tag `v0.1.0` and its GitHub Release were published on 2026-08-28, with
  `scene_factory-0.1.0-py3-none-any.whl`, `scene_factory-0.1.0.tar.gz`,
  `RELEASE_MANIFEST.json`, and `SHA256SUMS.txt` attached.
- As checked on 2026-10-07, PyPI has no `scene-factory` project listing; install
  the published wheel from the GitHub Release or build the current source.
