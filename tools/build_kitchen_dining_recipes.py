"""Build deterministic kitchen/dining examples using registered USD assets."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fixture(id_, category, asset, position):
    return {
        "object_id": id_,
        "category": category,
        "asset_id": asset,
        "dynamic": False,
        "support": "floor",
        "fixed_pose": {"position": position, "yaw_deg": 0},
    }


def clutter(id_, category, kind, support, inside=False):
    item = {
        "object_id": id_,
        "category": category,
        "asset_id": f"kd_{kind}_01",
        "dynamic": True,
        "support": support,
        "yaw_range_deg": [-15, 15],
    }
    if inside:
        item["relations"] = [{"kind": "inside", "target": support.split(":")[0]}]
    return item


def build(root=ROOT):
    counter = fixture("counter_1", "kitchen_counter", "kd_counter_01", [-0.7, 1.6, 0.45])
    cabinet = fixture("cabinet_1", "kitchen_cabinet", "kd_cabinet_01", [2.15, 1.5, 0.9])
    shelf = fixture("shelf_1", "storage_shelf", "kd_shelf_01", [-2.5, -0.5, 0.8])
    island = fixture("island_1", "kitchen_island", "kd_island_01", [0.1, -0.65, 0.46])
    kitchen = {
        "name": "kitchen_core_assets",
        "room_type": "kitchen",
        "event": "kitchen_stocked",
        "room_dimensions_m": [6.8, 5.2, 2.8],
        "description": "厨房料理台与岛台摆着杯碗盘和瓶子，储物柜、置物架内分层存放盒子，中央通道保持畅通。",
        "keywords": ["完整厨房", "厨房资产", "厨房布置", "料理台与岛台", "kitchen stocked"],
        "clearance_zones": [{"name": "kitchen_aisle", "center": [0, 0.55], "size": [3.2, 0.9]}],
        "objects": [
            counter,
            cabinet,
            shelf,
            island,
            clutter("mug_1", "mug", "mug", "counter_1:top"),
            clutter("bowl_1", "bowl", "bowl", "counter_1:top"),
            clutter("plate_1", "plate", "plate", "counter_1:top"),
            clutter("bottle_1", "bottle", "bottle", "counter_1:top"),
            clutter("mug_2", "mug", "mug", "island_1:top"),
            clutter("plate_2", "plate", "plate", "island_1:top"),
            clutter("box_1", "box", "box", "cabinet_1:shelf_0", True),
            clutter("bottle_2", "bottle", "bottle", "cabinet_1:shelf_1", True),
            clutter("box_2", "box", "box", "shelf_1:shelf_1", True),
        ],
        "task": {},
    }
    storage = {
        **kitchen,
        "name": "kitchen_storage_assets",
        "event": "kitchen_storage",
        "description": "厨房储物区的开放式橱柜和置物架内分层摆着盒子、杯子和瓶子，料理台放着碗盘，中央通道保持畅通。",
        "keywords": ["厨房储物区", "分层收纳", "开放式橱柜", "储物柜", "置物架", "pantry"],
        "objects": [
            counter,
            cabinet,
            shelf,
            clutter("bowl_1", "bowl", "bowl", "counter_1:top"),
            clutter("plate_1", "plate", "plate", "counter_1:top"),
            clutter("box_1", "box", "box", "cabinet_1:shelf_0", True),
            clutter("mug_1", "mug", "mug", "cabinet_1:shelf_1", True),
            clutter("bottle_1", "bottle", "bottle", "shelf_1:shelf_1", True),
            clutter("box_2", "box", "box", "shelf_1:shelf_2", True),
        ],
    }
    table = fixture("table_1", "dining_table", "kd_table_01", [0, 0, 0.38])
    dining = {
        "name": "dining_core_assets",
        "room_type": "dining_room",
        "event": "dining_setup",
        "room_dimensions_m": [6.4, 5.2, 2.8],
        "description": "餐厅里四把椅子围着餐桌，桌上放着两个杯子、两个盘子、一个碗和一个瓶子，置物架内放着盒子，右侧通道保持畅通。",
        "keywords": ["餐厅", "餐桌", "椅子", "用餐", "dining", "dinner", "table setting"],
        "clearance_zones": [{"name": "dining_aisle", "center": [2.35, 0], "size": [0.9, 4.8]}],
        "objects": [table, fixture("shelf_1", "storage_shelf", "kd_shelf_01", [-2.4, 1.5, 0.8])],
        "task": {},
    }
    for i in range(4):
        dining["objects"].append(
            {
                "object_id": f"chair_{i + 1}",
                "category": "chair",
                "asset_id": "kd_chair_01",
                "dynamic": False,
                "support": "floor",
                "relations": [
                    {
                        "kind": "around",
                        "target": "table_1",
                        "min_distance_m": 0.08,
                        "max_distance_m": 0.18,
                    }
                ],
            }
        )
    dining["objects"] += [
        clutter("mug_1", "mug", "mug", "table_1:top"),
        clutter("mug_2", "mug", "mug", "table_1:top"),
        clutter("plate_1", "plate", "plate", "table_1:top"),
        clutter("plate_2", "plate", "plate", "table_1:top"),
        clutter("bowl_1", "bowl", "bowl", "table_1:top"),
        clutter("bottle_1", "bottle", "bottle", "table_1:top"),
        clutter("box_1", "box", "box", "shelf_1:shelf_0", True),
    ]
    for recipe in (kitchen, storage, dining):
        (root / "recipes" / f"{recipe['name']}.json").write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    build()
