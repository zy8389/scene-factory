# Schema Policy

The Python package version and serialized schema versions are independent:

```text
package: scene-factory 0.1.0
schemas: scene_factory.<domain>.v1
```

Current versioned contracts include scene intent, scene specification,
interaction plans, execution traces, executor capabilities, and executor
conformance reports.

## Compatibility rules

- Unknown schema versions fail closed.
- Additive optional fields may be added to a v1 schema when old readers can
  safely ignore them.
- Required-field changes, type changes, removed meanings, and incompatible
  semantics require a new schema version.
- Package patch releases do not automatically create new schema versions.
- Readers must validate strict fields and reject malformed or non-finite JSON
  where the domain contract requires it.

Schema validation is part of the offline release gate. Migration frameworks are
out of scope for v0.1; a future breaking schema should provide an explicit
converter or a new reader contract.
