from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import replace

from .intent import IntentObject, SceneIntent
from .models import ObjectRequest, Relation, SceneRecipe
from .recipes import RecipeLibrary
from .registry import AssetRegistry


class IntentCompiler:
    """Compile semantic LLM output into the deterministic layout recipe format."""

    FLOOR_CATEGORIES = {"backpack", "shoe"}
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
            if fixtures:
                index = min(fixture_index[item.category], len(fixtures) - 1)
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
            state_text = " ".join(item.state).lower()
            requests.append(
                ObjectRequest(
                    object_id=object_id,
                    category=item.category,
                    support=support_by_id[object_id],
                    dynamic=item.dynamic,
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
        )

    def _select_base(self, intent: SceneIntent, prompt: str) -> SceneRecipe:
        recipes = list(self.recipes.values())
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

        if item.category in self.FLOOR_CATEGORIES:
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
                return f"{target}:{asset.support_surfaces[0].name}"
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
