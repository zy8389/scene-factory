# Stratified dataset collection

This example balances mug-lift and mug-placement candidates across two start
regions. It generates 40 candidates for each stratum and selects exactly 10
unique, layout-qualified scenes from each: 160 candidates and a total quota of 40.

```bash
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified
scene-factory dataset sampling-validate outputs/stratified
scene-factory dataset sampling-reproduce outputs/stratified
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified --resume
```

`collection.json` holds the selection index, source fingerprints, coverage and
per-stratum counts. Each selected entry has a `dataset_path` relative to the
collection and a `record` whose file paths remain relative to that child dataset.
The `datasets/` children keep the existing dataset v1 contract.

The ordered plan prioritizes earlier strata, then lower seeds. Region bounds
are half-open: `[xmin, xmax)` and `[ymin, ymax)`. The example separates cup starts
at X=0.60 m into two non-overlapping bins; it does not assert robot reachability
or assign a physical difficulty rating.

If any quota is short, the collection is incomplete and releases no selection.
Increase the candidate budget in a new output directory to try a revised plan.
Resume requires the same plan, recipe and registry fingerprints and preserves
completed candidates. Physics and task evidence remain unverified.

See [the data workflow](../../docs/DATA_WORKFLOW.md) for field and validation semantics.
