# CLI Reference

The installed command is `scene-factory`. Every command supports `--help`.

## Commands

```text
scene-factory list-recipes
scene-factory build --recipe NAME --seed INT --output PATH
scene-factory build --intent PATH --seed INT --output PATH
scene-factory batch --recipe NAME --count INT --seed-start INT --output PATH
scene-factory batch --intent PATH --count INT --seed-start INT --output PATH
scene-factory intent validate PATH
scene-factory intent inspect PATH
scene-factory intent schema
scene-factory dataset inspect PATH
scene-factory dataset validate PATH
scene-factory dataset reproduce PATH
scene-factory task plan --scene PATH --object ID --state NAME --output PATH
scene-factory task validate --scene PATH --plan PATH
scene-factory task replay --scene PATH --plan PATH
scene-factory task execute --scene PATH --plan PATH --executor dry-run --output PATH
scene-factory task execution-validate --scene PATH --plan PATH --trace PATH
scene-factory executor inspect --executor dry-run
scene-factory executor conformance --executor dry-run --output PATH
scene-factory executor validate-report PATH
scene-factory asset inspect --asset-id ID
scene-factory asset normalize SOURCE --output PATH --asset-id ID --category NAME
scene-factory asset collision --collision-path PATH --status STATUS --enabled
scene-factory llm-status
scene-factory llm-test
```

`build --usd`, `batch --usd`, and the Isaac asset commands require an Isaac/USD
environment. The command parser itself remains importable in ordinary Python.

## Exit codes

- `0`: command completed successfully;
- `1`: command, configuration, runtime, or file-system error;
- `2`: validation or acceptance failure.

Machine-readable commands print JSON on successful execution and validation
failures. Human-readable `list-recipes` prints one recipe name per line.

## Resource overrides

`--registry` and `--recipes` override the default resource locations. Installed
packages resolve their resources from the platform data directory. The optional
`SCENE_FACTORY_HOME` environment variable can override the complete resource
root for controlled deployments.
