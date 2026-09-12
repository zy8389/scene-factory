# Third-Party Notices

SceneFactory includes the following source assets. The information below is
copied from the committed `data/assets/source/*/SOURCE.json` manifests. No
additional license or copyright claims are made here.

## YCB assets

The four packaged YCB assets were imported from the `ai-habitat/ycb` dataset
at revision `29be64fdd95b4881f244152ad653058e0a48c28f`.

| Asset | Dataset object | License | Source | Packaged source hash |
| --- | --- | --- | --- | --- |
| `bowl_001` | YCB 024_bowl | CC BY 4.0 | [ai-habitat/ycb](https://huggingface.co/datasets/ai-habitat/ycb) | `eab02a7fbf0bcc9e3ad0f10797a10bbc3f1a4356e787982e3f41cbfaaf9baa88` |
| `knife_001` | YCB 032_knife | CC BY 4.0 | [ai-habitat/ycb](https://huggingface.co/datasets/ai-habitat/ycb) | `ef7df0085a07c6203ac9c4e39c3f323fe0b12053751861f6587ca2c4520d735e` |
| `plate_001` | YCB 029_plate | CC BY 4.0 | [ai-habitat/ycb](https://huggingface.co/datasets/ai-habitat/ycb) | `0de7960c542811ab9a03dc9dee65b7cfc5359a0b38496e38266bd85db9907d68` |
| `mug_001` | YCB 025_mug | CC BY 4.0 | [ai-habitat/ycb](https://huggingface.co/datasets/ai-habitat/ycb) | `01953e16a8039c14d9009084f7d17ec4660b97992735d357d4b46bb469717fe7` |

The manifests also record the visual and collision file hashes, original
dataset paths, retrieval metadata, and license source URL. See the individual
`SOURCE.json` files for those complete records.

The upstream license terms continue to apply to the asset files. Before any
redistribution outside this repository, review the current upstream terms and
preserve the CC BY 4.0 attribution requirements.

## Poly Haven high-detail visual pack

The `high_detail_cc0_v1` pack adds 1K PBR visual meshes for the large scene
anchors that previously rendered as boxes. The upstream glTF files were
converted to self-contained GLB files with glTF-Transform 4.5.0; physics and
collision continue to use SceneFactory's deterministic proxy definitions.

| Scene asset | Poly Haven model | License | Packaged GLB hash |
| --- | --- | --- | --- |
| `sofa_basic` | [Sofa 02](https://polyhaven.com/a/sofa_02) | CC0 | `7fe865939497e32bc2f6f0c3dc519954c7c83eb5b9251dbf8e1fc318f5c5ad1a` |
| `coffee_table_oak` | [Modern Coffee Table 01](https://polyhaven.com/a/modern_coffee_table_01) | CC0 | `3bdbf5235bc60553cf9b7174aa907a20d1284e7374a85d71521ff59f57053a8c` |
| `side_cabinet_basic` | [Modern Wooden Cabinet](https://polyhaven.com/a/modern_wooden_cabinet) | CC0 | `081b44f3b98d6903a82625d1941584f1e3bc4b6c366a3dc242649b01d3b3b0f9` |
| `entry_bench_basic` | [Painted Wooden Bench](https://polyhaven.com/a/painted_wooden_bench) | CC0 | `07c739a1e5361b878320ed62ea1bc4b68a6268498663198e78266af3b819c172` |
| `kitchen_counter_basic` | [Painted Wooden Cabinet](https://polyhaven.com/a/painted_wooden_cabinet) | CC0 | `1e6fc076b12caea37fa1f1b5302c7a4160bd5aeda5e20f72b4bccb3ea87b4391` |
| `kitchen_island_basic` | [Wooden Table 03](https://polyhaven.com/a/WoodenTable_03) | CC0 | `99e8494b67187126696aa9dc77596d187f5b8d5dc1f3420d1a5e6afe2747d5b1` |
| `cutting_board_wood` | [Wooden Cutting Board](https://polyhaven.com/a/wooden_cutting_board) | CC0 | `5bbcbcf22589a9da54cea73ab00983d7521c2cf9ef14673cc2f70f6cfbe53b06` |
| `pot_basic` | [Pot Enamel 01](https://polyhaven.com/a/pot_enamel_01) | CC0 | `ba7c8d3edad6c30b9e50dd436249fe980ab8aa860d1f609793d892cc1f7c6ce4` |

Poly Haven states that all downloadable assets are released under CC0 and may
be used commercially and redistributed without attribution. The individual
manifests preserve source URLs, packaged hashes, and conversion details.
