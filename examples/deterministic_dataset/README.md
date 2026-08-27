# Deterministic dataset

Generate a small dataset, then inspect, validate, and reproduce it offline:

```bash
scene-factory batch \
  --recipe living_room_recent_snacking \
  --count 3 \
  --seed-start 100 \
  --output outputs/dataset
scene-factory dataset inspect outputs/dataset
scene-factory dataset validate outputs/dataset
scene-factory dataset reproduce outputs/dataset
```

The example intentionally does not include generated dataset files.
