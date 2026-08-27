# SceneFactory v0.1 Release Readiness Checklist

This checklist is intentionally left unchecked until a release owner reviews
the evidence. It describes readiness work; it is not a publication record.

## Package

- [ ] package version is consistent at `0.1.0`
- [ ] public API exports and documentation agree
- [ ] wheel builds in a clean environment
- [ ] fresh installation reports the expected distribution metadata
- [ ] `pip check` passes
- [ ] runtime recipes, schemas, web files, registry, and asset resources are present

## Offline SDK

- [ ] scene generation passes
- [ ] external intent validation and build pass
- [ ] deterministic batch dataset validation and reproduction pass
- [ ] articulated planning, dry-run execution, and trace validation pass
- [ ] executor conformance report validates
- [ ] no-Isaac, no-NumPy, and no-network checks pass

## Documentation and provenance

- [ ] README is accurate for the current scope
- [ ] architecture, API, compatibility, and schema policy docs are current
- [ ] runnable examples pass from an installed package
- [ ] third-party asset notices cover all packaged source manifests
- [ ] no secrets, personal paths, or generated outputs are tracked

## Physical simulator status

- [ ] official Isaac assets and environment are available for the target machine
- [ ] real Franka acceptance is run separately
- [ ] real RGB-D acceptance is run separately

## Publication actions

- [ ] release owner approves publication
- [ ] version tag created
- [ ] GitHub Release created
- [ ] PyPI artifact published

The final R0 report must distinguish pure-Python release readiness from the
environment-specific physical simulator gate and must not mark publication
items complete automatically.
