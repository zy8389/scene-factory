from scene_factory.domains import domain_pack_for_room, domain_catalog
from scene_factory.factory import SceneFactory


def test_domain_catalog_exposes_non_home_packs():
    ids = {item["domain_id"] for item in domain_catalog()}
    assert {"home", "warehouse", "laboratory", "factory", "outdoor"} <= ids
    assert domain_pack_for_room("warehouse").task_actions == (
        "navigate", "grasp", "place", "retrieve", "load", "unload"
    )


def test_warehouse_recipe_compiles_through_existing_pipeline():
    result = SceneFactory().build_from_recipe("warehouse_retrieval", 42)
    assert result.valid
    assert result.scene.room_type == "warehouse"
    assert result.scene.task["actions"] == ["navigate", "grasp", "retrieve", "place"]
