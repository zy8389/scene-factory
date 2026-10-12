# Kitchen and dining asset pack

This pack expands the scene generator with eleven original assets. Each has
real USD visual geometry, a separate USD collision layer, a web GLB, and a
portable mesh description used by MuJoCo. All are generated from the same
vertices and faces, with MIT-licensed project geometry and material colours.
They are procedural models, not scanned or photorealistic assets.

| Asset | Category | Dimensions (m, X × Y × Z) | Default role |
| --- | --- | --- | --- |
| `kd_table_01` | dining_table | 1.60 × 0.90 × 0.76 | Static, tabletop |
| `kd_counter_01` | kitchen_counter | 2.20 × 0.86 × 0.90 | Static, worktop |
| `kd_island_01` | kitchen_island | 1.40 × 0.78 × 0.92 | Static, worktop |
| `kd_cabinet_01` | kitchen_cabinet | 0.90 × 0.50 × 1.80 | Static, three open compartments |
| `kd_shelf_01` | storage_shelf | 0.90 × 0.40 × 1.60 | Static, three open compartments |
| `kd_chair_01` | chair | 0.46 × 0.50 × 0.86 | Static, seat |
| `kd_mug_01` | mug | 0.12 × 0.09 × 0.11 | Dynamic, open body and handle |
| `kd_bowl_01` | bowl | 0.18 × 0.18 × 0.07 | Dynamic, open bowl |
| `kd_plate_01` | plate | 0.24 × 0.24 × 0.024 | Dynamic, raised rim |
| `kd_bottle_01` | bottle | 0.075 × 0.075 × 0.24 | Dynamic, capped bottle |
| `kd_box_01` | box | 0.18 × 0.14 × 0.12 | Dynamic, closed box |

Coordinates use metres and Z up. The origin is the bounding-box centre.
The GLB uses the standard glTF Y-up basis; its documented import transform
restores the canonical Z-up geometry without changing scale. Collision parts
preserve furniture openings and cup/bowl cavities instead of using one solid
bounding box. Mass and friction are labelled project defaults, not measured
properties of a particular real product. MuJoCo uses an explicit bounding-box
inertia approximation; Isaac derives inertia from collision geometry.

## Generate and inspect

The webpage provides **完整厨房**, **厨房收纳**, and **餐厅布置** examples.
The corresponding deterministic recipes are `kitchen_core_assets`,
`kitchen_storage_assets`, and `dining_core_assets`.

```python
from scene_factory import SceneFactory

factory = SceneFactory()
result = factory.build_from_recipe("dining_core_assets", seed=42)
assert result.valid
factory.write_result(result, "outputs/dining", export_mjcf=True)
```

Enable USD export in the webpage or pass `export_usd=True` when an Isaac/OpenUSD
Python is configured. On Windows use an ASCII output directory. Generated
pack USD layers are copied byte-for-byte beside `scene.usd` with relative
references; moving that directory retains its asset dependencies.

## Spatial contracts

- `on` uses the tabletop or cabinet top; `inside` uses a registered compartment.
- `support_hint="cabinet_id:shelf_2"` selects a particular compartment.
- Both the footprint and full object height must fit the compartment. Invalid
  containment fails instead of placing the object above the cabinet.
- `around` currently supports chairs around a dining table. Chairs remain on
  the floor, face inward, and maintain the specified edge-to-edge clearance.
- `clearance_zones` reserve explicit world-space floor rectangles. Sampling
  excludes them; independent validation rejects blocked or out-of-room zones.
  Green markers show these zones in the web and top-down previews. This is a
  corridor constraint, not a general navigation or human-accessibility proof.
- Room bounds, object overlap, support footprint and floating/sunk checks run
  alongside storage and corridor checks.

## Reproduce and validate

```text
python tools/build_kitchen_dining_assets.py
python tools/build_kitchen_dining_recipes.py
python -m pytest tests/test_kitchen_dining_pack.py
<isaac-python> tools/validate_kitchen_dining_assets.py --output <ascii-directory> --promote
```

Asset generation needs only standard Python. Regeneration preserves readiness
only when the USD, collision and GLB hashes still match the saved real QA.
Changed geometry returns to `normalized` and cannot be selected until accepted.
The validator stages exact source bytes, checks OpenUSD geometry, materials,
units, bounds and dependencies, then runs real PhysX contact/drop cases.
Registry promotion requires passing evidence whose hashes still match.

The committed [asset QA reports](../data/assets/qa_reports/) record furniture
support contacts (including all cabinet/shelf levels), and floor-drop/table
contact cases for each movable asset. Structural generation tests cover 100
seeds per new recipe and intentional invalid storage/corridor cases. Full
scene settling is also checked locally in MuJoCo and Isaac.

Display, asset contact, whole-scene physics and robot task acceptance are
distinct. Generating a new layout does not automatically verify its physics.
Robot grasping of these new objects has **not** been validated; the existing
Franka task runner retains its YCB single-mug restrictions. Cabinets are open
storage, with no door/drawer articulation. Fluids and deformable materials are
outside this pack's scope.
