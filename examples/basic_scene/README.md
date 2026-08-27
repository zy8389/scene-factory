# Basic scene

Build a deterministic scene from a packaged recipe:

```bash
scene-factory build \
  --recipe living_room_recent_snacking \
  --seed 42 \
  --output outputs/basic-scene
```

The output directory contains the scene specification, layout, validation
report, and an offline SVG preview. The command does not require Isaac Sim.
