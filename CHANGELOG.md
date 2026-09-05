# Changelog

## 0.1.0 - 2026-08-28

Published as GitHub Release `v0.1.0` on 2026-08-28 (wheel, sdist, manifest and SHA-256 checksums).

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
- Git tag and GitHub Release artifacts are published. PyPI publication is not asserted.
- P1-4B/C drawer changes are experimental and unreleased; see `docs/P1_4_CLOSEOUT.md`.
