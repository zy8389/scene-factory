# SceneFactory v0.1 CLI Reference

The installed entry point is `scene-factory`. The command parser and every
public command support `--help`. Exit code `0` means the command completed;
`1` means an operational error; `2` means validation or acceptance failed.

## Public command groups

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

`build --usd`, `batch --usd`, and asset/USD validation commands require a
compatible Isaac/USD environment. Parsing `--help` and using the pure-Python
commands do not import that optional runtime.

## Resource selection

`--registry` and `--recipes` override resource locations. The optional
`SCENE_FACTORY_HOME` environment variable overrides the complete resource root
for controlled deployments. Installed packages resolve their default recipes,
schemas, web files, registry, and asset resources from the platform data
directory.

## Machine-readable output

Validation and build commands print JSON. `list-recipes` prints one recipe name
per line for shell-friendly use. Generated datasets, traces, and reports should
be written to caller-owned output directories and are not part of the package.
