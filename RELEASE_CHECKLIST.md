# SceneFactory Release Readiness Checklist

The checklist below applies to the next release from the current development
branch. Its readiness items remain unchecked until a release owner reviews the
evidence for that release. Historical publication facts are recorded separately.

## Verified v0.1.0 publication

Checked on 2026-10-07 against the public GitHub Release and PyPI project endpoint:

- [x] tag `v0.1.0` exists at commit `7e0de6f419522f74cae4bb2314a7b3dc8a1eb906`
- [x] [GitHub Release](https://github.com/zy8389/scene-factory/releases/tag/v0.1.0)
  was published on 2026-08-28 and is neither a draft nor a prerelease
- [x] release attachments include `scene_factory-0.1.0-py3-none-any.whl`,
  `scene_factory-0.1.0.tar.gz`, `RELEASE_MANIFEST.json`, and `SHA256SUMS.txt`
- [ ] PyPI publication: the `scene-factory` project endpoint returned HTTP 404

These facts establish publication, not validation of subsequent development
changes. Artifact contents and physical acceptance require their own evidence.

## Package

- [ ] package version is consistent with the target release
- [ ] public API exports and documentation agree
- [ ] wheel builds in a clean environment
- [ ] fresh installation reports the expected distribution metadata
- [ ] `pip check` passes
- [ ] runtime recipes, schemas, web files, registry, and asset resources are present
- [ ] bundled Three.js modules and all referenced GLB source assets are present
- [ ] optional MuJoCo environment smoke passes in an environment with `.[mujoco]`

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
- [ ] target release notes and API/CLI surface snapshots are reviewed
- [ ] runnable examples pass from an installed package
- [ ] third-party asset notices cover all packaged source manifests
- [ ] no secrets, personal paths, or generated outputs are tracked
- [ ] wheel and sdist contents pass the release artifact audit
- [ ] artifact SHA-256 and git provenance manifest is generated from a clean tag commit
- [ ] wheel and sdist-derived fresh installs pass outside the source checkout
- [ ] `tools/release_smoke.py` is run from a non-editable wheel installation

## Physical simulator status

- [ ] official Isaac assets and environment are available for the target machine
- [ ] real Franka acceptance is run separately
- [ ] real RGB-D acceptance is run separately

## Next release publication actions

- [ ] release owner approves publication
- [ ] version tag created
- [ ] GitHub Release created
- [ ] PyPI artifact published

The final release report must distinguish pure-Python release readiness from
the environment-specific physical simulator gate. Mark next-release publication
items complete only when the corresponding action has been performed and verified.
