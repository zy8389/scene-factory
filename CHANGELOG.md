# Changelog

## Unreleased

### Added

- MuJoCo MJCF export and a lazy MuJoCo environment backend;
- portable scene bundles with artifact hashes, asset metadata and local GLB
  visual files;
- a Three.js WebGL scene preview with bounding-box fallback;
- eight CC0 furniture and kitchen visual assets with source manifests and
  notices.

### Changed

- scene builds and the Web UI export MJCF by default;
- `SceneFactoryEnv` selects the MuJoCo backend by default, while explicit
  `DryRunBackend` use remains dependency-free;
- documentation and release artifact checks now describe the visual-asset and
  MuJoCo workflow boundaries.

## 0.1.0 - 2026-08-28

This is a release-readiness milestone, not a published package release.

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
  and are not part of this release candidate's pure-Python gate.
- No PyPI package, Git tag, GitHub Release, or release artifact has been published.
