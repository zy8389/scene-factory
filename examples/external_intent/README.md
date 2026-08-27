# External SceneIntent

Validate the checked-in external intent and compile it through the regular
SceneFactory pipeline:

```bash
scene-factory intent validate examples/external_intent/scene.json
scene-factory build \
  --intent examples/external_intent/scene.json \
  --seed 42 \
  --output outputs/external-scene
```

The input uses only categories and vocabulary shipped by the repository.
