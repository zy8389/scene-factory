# Articulated planning and dry-run execution

The fixture is a small articulated drawer contract. Run the complete symbolic
workflow without a simulator:

```bash
scene-factory task plan \
  --scene examples/articulated_drawer/scene.json \
  --object drawer_1 \
  --state open \
  --output outputs/drawer_plan.json
scene-factory task validate \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer_plan.json
scene-factory task execute \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer_plan.json \
  --executor dry-run \
  --output outputs/drawer_trace.json
scene-factory task execution-validate \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer_plan.json \
  --trace outputs/drawer_trace.json
scene-factory executor conformance --executor dry-run
```

This validates semantic contract execution only. It does not claim physical
robot motion or collision-free manipulation.
