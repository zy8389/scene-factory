from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import replace

from .bundle import _load_visual_assets
from .intent import IntentObject, SceneIntent
from .models import ObjectRequest, Relation, SceneRecipe
from .recipes import RecipeLibrary
from .registry import AssetRegistry


class IntentCompiler:
    """Compile semantic LLM output into the deterministic layout recipe format."""

    FLOOR_CATEGORIES = {"backpack", "shoe", "chair", "dining_table", "kitchen_cabinet", "storage_shelf"}
    SUPPORT_PREFERENCES = {
        "cushion": ("sofa",),
        "remote_control": ("sofa",),
        "keys": ("entry_bench", "side_cabinet", "coffee_table"),
        "mug": ("coffee_table", "side_cabinet", "kitchen_counter", "kitchen_island"),
        "snack_bag": ("coffee_table", "side_cabinet"),
        "tissue": ("coffee_table", "side_cabinet"),
        "cutting_board": ("kitchen_counter", "kitchen_island"),
        "kitchen_knife": ("cutting_board", "kitchen_counter", "kitchen_island"),
        "vegetable_bag": ("kitchen_counter", "kitchen_island"),
        "pot": ("kitchen_island", "kitchen_counter"),
        "pot_lid": ("kitchen_island", "kitchen_counter", "pot"),
        "bottle": ("dining_table", "kitchen_counter", "kitchen_island", "storage_shelf"),
        "box": ("storage_shelf", "kitchen_cabinet", "kitchen_counter"),
        "bowl": ("dining_table", "kitchen_counter", "kitchen_island"),
        "plate": ("dining_table", "kitchen_counter", "kitchen_island"),
    }
    YAW_LIMITS = {
        "cutting_board": 12.0,
        "kitchen_knife": 15.0,
        "pot": 20.0,
        "pot_lid": 45.0,
        "mug": 25.0,
        "remote_control": 40.0,
        "cushion": 20.0,
    }

    def __init__(self, registry: AssetRegistry, recipes: RecipeLibrary) -> None:
        self.registry = registry
        self.recipes = recipes
        self.visual_assets = _load_visual_assets(self.registry.base_dir / "source")

    def scene_context(self) -> dict:
        return {
            "rooms": [
                {"room_type": recipe.room_type, "event": recipe.event,
                 "fixtures": [request.category for request in recipe.objects if request.fixed_pose is not None]}
                for recipe in self.recipes.values() if not recipe.task.get("robot")
            ],
            "categories": [
                {"category": category,
                 "support_surfaces": sorted({surface.name for asset in self.registry.candidates(category)
                                             for surface in asset.support_surfaces}),
                 "preferred_supports": list(self.SUPPORT_PREFERENCES.get(category, ())),
                 "interior_regions": sorted({region.region_id for asset in self.registry.candidates(category)
                                             for region in asset.interior_regions}),
                 "has_visual": any(asset.asset_id in self.visual_assets for asset in self.registry.candidates(category))}
                for category in self.registry.categories()
            ],
        }

    def compile(self, intent: SceneIntent, prompt: str) -> SceneRecipe:
        base = self._select_base(intent, prompt)
        fixed = [request for request in base.objects if request.fixed_pose is not None]
        fixed_by_category: dict[str, list[ObjectRequest]] = defaultdict(list)
        for request in fixed:
            fixed_by_category[request.category].append(request)

        aliases: dict[str, str] = {}
        used_ids = {request.object_id for request in fixed}
        fixed_state_overrides: dict[str, tuple[str, ...]] = {}
        dynamic_intents: list[tuple[IntentObject, str]] = []
        fixture_index: dict[str, int] = defaultdict(int)
        for item in intent.objects:
            fixtures = fixed_by_category.get(item.category, [])
            if fixture_index[item.category] < len(fixtures):
                index = fixture_index[item.category]
                aliases[item.object_id] = fixtures[index].object_id
                if item.state:
                    fixed_state_overrides[fixtures[index].object_id] = item.state
                fixture_index[item.category] += 1
                continue
            object_id = self._unique_id(item.object_id, used_ids)
            aliases[item.object_id] = object_id
            used_ids.add(object_id)
            dynamic_intents.append((item, object_id))

        category_by_id = {request.object_id: request.category for request in fixed}
        category_by_id.update({object_id: item.category for item, object_id in dynamic_intents})
        relations_by_subject: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for relation in intent.relations:
            subject = aliases.get(relation.subject, relation.subject)
            target = aliases.get(relation.target, relation.target)
            if subject in category_by_id and target in category_by_id and subject != target:
                relations_by_subject[subject].append((relation.predicate, target))

        support_by_id: dict[str, str] = {}
        for item, object_id in dynamic_intents:
            support_by_id[object_id] = self._choose_support(
                item,
                object_id,
                aliases,
                category_by_id,
                relations_by_subject,
            )

        requests: list[ObjectRequest] = [
            replace(request, state=fixed_state_overrides.get(request.object_id, request.state))
            for request in fixed
        ]
        yaw_amplitude = min(90.0, 12.0 + 68.0 * intent.clutter_level)
        for item, object_id in dynamic_intents:
            object_yaw = min(yaw_amplitude, self.YAW_LIMITS.get(item.category, 90.0))
            compiled_relations = []
            for predicate, target in relations_by_subject.get(object_id, []):
                if predicate == "on" or target == self._support_target(support_by_id[object_id]):
                    if predicate == "inside":
                        compiled_relations.append(Relation(kind="inside", target=target))
                    continue
                if predicate == "near":
                    min_distance, max_distance = self._relation_distance(
                        item.category, category_by_id[target], intent.clutter_level
                    )
                    compiled_relations.append(
                        Relation(
                            kind="near",
                            target=target,
                            min_distance_m=min_distance,
                            max_distance_m=max_distance,
                        )
                    )
                elif predicate == "partly_occluded_by":
                    compiled_relations.append(
                        Relation(kind="partly_occluded_by", target=target)
                    )
                elif predicate == "around":
                    if item.category != "chair" or category_by_id[target] != "dining_table":
                        raise ValueError("around currently supports chairs around a dining table")
                    compiled_relations.append(Relation(kind="around",target=target,
                                                       min_distance_m=.08,max_distance_m=.18))
            state_text = " ".join(item.state).lower()
            base_request = next((request for request in base.objects if request.category == item.category
                                 and request.region_xy is not None), None)
            floor_targets = [category_by_id[target] for predicate, target in relations_by_subject.get(object_id, [])
                             if predicate == "near"]
            region = (base_request.region_xy if base_request and support_by_id[object_id] == "floor"
                      and all(category == "entry_bench" for category in floor_targets) else None)
            requests.append(
                ObjectRequest(
                    object_id=object_id,
                    category=item.category,
                    asset_id=self._visual_asset_id(item,base),
                    support=support_by_id[object_id],
                    dynamic=item.dynamic,
                    region_xy=region,
                    yaw_range_deg=(-object_yaw, object_yaw),
                    edge_bias=(
                        "edge" in state_text
                        or "边缘" in state_text
                        or intent.layout_style.lower() in {"edge_clutter", "边缘堆放"}
                    ),
                    relations=tuple(compiled_relations),
                    state=item.state,
                )
            )

        identity = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
        dimensions = base.room_dimensions_m
        if intent.room_dimensions_m is not None:
            dimensions = tuple(
                max(base.room_dimensions_m[index], intent.room_dimensions_m[index])
                for index in range(3)
            )
        return SceneRecipe(
            name=f"intent_{base.name}_{digest}",
            room_type=base.room_type,
            room_dimensions_m=dimensions,
            event=intent.event,
            description=prompt,
            keywords=(),
            objects=tuple(requests),
            task={},
            clearance_zones=base.clearance_zones,
        )

    def _visual_asset_id(self, item: IntentObject, base: SceneRecipe | None = None) -> str | None:
        candidates = [asset for asset in self.registry.candidates(item.category)
                      if asset.asset_id in self.visual_assets]
        if not candidates:
            return None
        ids={asset.asset_id for asset in candidates}
        preferred=next((request.asset_id for request in base.objects
                        if request.category==item.category and request.asset_id in ids),None) if base else None
        if preferred and "kitchen_dining_pack" in self.registry.get(preferred).tags:
            # Attribute labels such as "bowl" must not silently select an older
            # source with a different upright geometry/physics contract.
            candidates=[asset for asset in candidates if "kitchen_dining_pack" in asset.tags]
        attributes = " ".join(item.attributes).casefold()
        colors = {
            "blue": ((0.15, 0.4, 0.75), ("blue", "蓝")),
            "red": ((0.8, 0.15, 0.15), ("red", "红")),
            "green": ((0.2, 0.6, 0.2), ("green", "绿")),
            "white": ((0.95, 0.95, 0.95), ("white", "白")),
            "black": ((0.05, 0.05, 0.05), ("black", "黑")),
            "gray": ((0.4, 0.4, 0.4), ("gray", "grey", "灰")),
            "cream": ((0.83, 0.78, 0.62), ("cream", "米色")),
        }
        color = next((value for value, words in colors.values() if any(word in attributes for word in words)), None)
        if color is None and preferred:
            return preferred
        return min(candidates, key=lambda asset: (
            sum((asset.color[axis] - color[axis]) ** 2 for axis in range(3)) if color else 0,
            asset.asset_id,
        )).asset_id

    def _select_base(self, intent: SceneIntent, prompt: str) -> SceneRecipe:
        recipes = list(self.recipes.values())
        # Storage fixtures select the template that actually has registered cavities.
        requested = {item.category for item in intent.objects}
        if requested & {"kitchen_cabinet", "storage_shelf", "dining_table"}:
            matches = [r for r in recipes if r.room_type==intent.room_type and not r.task.get("robot")]
            if matches:
                return max(matches,key=lambda r:(
                    len(requested & {o.category for o in r.objects if o.fixed_pose is not None}),
                    r.event==intent.event,
                ))
        exact = [
            recipe
            for recipe in recipes
            if recipe.room_type == intent.room_type and recipe.event == intent.event
        ]
        if exact:
            return exact[0]
        room_matches = [recipe for recipe in recipes if recipe.room_type == intent.room_type]
        return room_matches[0] if room_matches else self.recipes.match_prompt(prompt)

    def _choose_support(
        self,
        item: IntentObject,
        object_id: str,
        aliases: dict[str, str],
        category_by_id: dict[str, str],
        relations_by_subject: dict[str, list[tuple[str, str]]],
    ) -> str:
        for predicate, target in relations_by_subject.get(object_id, []):
            if predicate == "inside":
                category=category_by_id[target]
                region=next((region for asset in self.registry.candidates(category)
                             for region in asset.interior_regions),None)
                if region is None:
                    raise ValueError(f"{target} has no registered storage interior")
                if item.support_hint:
                    hint_target,_,hint_surface=item.support_hint.partition(":")
                    if aliases.get(hint_target,hint_target)==target and hint_surface:
                        regions={r.region_id for asset in self.registry.candidates(category) for r in asset.interior_regions}
                        if hint_surface not in regions:
                            raise ValueError(f"{target} has no storage interior {hint_surface}")
                        return f"{target}:{hint_surface}"
                return f"{target}:{region.region_id}"
            if predicate == "on":
                support = self._surface_reference(target, category_by_id)
                if support:
                    return support

        if item.support_hint:
            hint = item.support_hint.strip()
            if hint == "floor":
                return "floor"
            target_name, separator, surface_name = hint.partition(":")
            target = aliases.get(target_name, target_name)
            if target in category_by_id:
                if separator and surface_name:
                    return f"{target}:{surface_name}"
                support = self._surface_reference(target, category_by_id)
                if support:
                    return support

        if item.category in self.FLOOR_CATEGORIES or (
            not item.dynamic and any(asset.support_surfaces for asset in self.registry.candidates(item.category))
        ):
            return "floor"
        preferences = self.SUPPORT_PREFERENCES.get(item.category, ())
        for category in preferences:
            for target, target_category in category_by_id.items():
                if target != object_id and target_category == category:
                    support = self._surface_reference(target, category_by_id)
                    if support:
                        return support
        for target, target_category in category_by_id.items():
            if target == object_id:
                continue
            if self.registry.candidates(target_category):
                support = self._surface_reference(target, category_by_id)
                if support:
                    return support
        return "floor"

    def _surface_reference(
        self,
        target: str,
        category_by_id: dict[str, str],
    ) -> str | None:
        category = category_by_id.get(target)
        if category is None:
            return None
        for asset in self.registry.candidates(category):
            if asset.support_surfaces:
                surface=next((s for s in asset.support_surfaces if s.name=="top"),asset.support_surfaces[0])
                return f"{target}:{surface.name}"
        return None

    def _relation_distance(
        self,
        subject_category: str,
        target_category: str,
        clutter_level: float,
    ) -> tuple[float, float]:
        subject = self.registry.candidates(subject_category)[0]
        target = self.registry.candidates(target_category)[0]
        clearance = (
            max(subject.bbox_m[0], subject.bbox_m[1])
            + max(target.bbox_m[0], target.bbox_m[1])
        ) / 2.0
        minimum = clearance + 0.02
        return minimum, minimum + 0.18 + 0.18 * (1.0 - clutter_level)

    @staticmethod
    def _support_target(support: str) -> str:
        return support.split(":", 1)[0]

    @staticmethod
    def _unique_id(value: str, used: set[str]) -> str:
        base = re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_") or "object"
        if base[0].isdigit():
            base = f"obj_{base}"
        candidate = base
        index = 2
        while candidate in used:
            candidate = f"{base}_{index}"
            index += 1
        return candidate
