from __future__ import annotations

import hashlib
import math
import random

from .geometry import inverse_rotate_xy, objects_overlap, rotate_xy, rotated_half_extents_xy
from .models import (
    AssetRecord,
    CompiledScene,
    ObjectRequest,
    PlacedObject,
    Pose,
    SceneRecipe,
    SupportSurface,
)
from .registry import AssetRegistry


class LayoutError(RuntimeError):
    pass


class LayoutSolver:
    def __init__(self, registry: AssetRegistry, max_attempts: int = 192) -> None:
        self.registry = registry
        self.max_attempts = max_attempts

    def compile(
        self,
        recipe: SceneRecipe,
        seed: int,
        description_override: str | None = None,
    ) -> CompiledScene:
        rng = random.Random(seed)
        requests = self._dependency_order(recipe.objects)
        placed: list[PlacedObject] = []
        placed_by_id: dict[str, PlacedObject] = {}

        for request in requests:
            asset = self.registry.resolve(request.category, request.asset_id, rng)
            if request.fixed_pose is not None:
                candidate = self._make_placed(request, asset, request.fixed_pose)
                if self._collides(candidate, placed, request.support):
                    raise LayoutError(f"fixed object {request.object_id} collides with existing geometry")
            else:
                candidate = self._sample_object(
                    request=request,
                    asset=asset,
                    room_dimensions=recipe.room_dimensions_m,
                    placed=placed,
                    placed_by_id=placed_by_id,
                    rng=rng,
                )
            placed.append(candidate)
            placed_by_id[candidate.object_id] = candidate

        scene_identity = f"{recipe.name}:{seed}:{description_override or recipe.description}"
        scene_hash = hashlib.sha256(scene_identity.encode("utf-8")).hexdigest()[:12]
        return CompiledScene(
            scene_id=f"{recipe.name}-{scene_hash}",
            seed=seed,
            recipe_name=recipe.name,
            room_type=recipe.room_type,
            room_dimensions_m=recipe.room_dimensions_m,
            event=recipe.event,
            description=description_override or recipe.description,
            objects=tuple(placed),
            task=recipe.task,
        )

    def _dependency_order(self, requests: tuple[ObjectRequest, ...]) -> list[ObjectRequest]:
        by_id = {request.object_id: request for request in requests}
        fixed_ids = {request.object_id for request in requests if request.fixed_pose is not None}
        dependencies: dict[str, set[str]] = {}
        for request in requests:
            required: set[str] = set()
            # Fixtures define the usable geometry and must exist before any sampled clutter,
            # including floor objects that do not explicitly reference a fixture.
            if request.fixed_pose is None:
                required.update(fixed_ids)
            if request.support and request.support != "floor":
                required.add(request.support.split(":", 1)[0])
            required.update(
                relation.target for relation in request.relations if relation.target in by_id
            )
            dependencies[request.object_id] = required

        ordered: list[ObjectRequest] = []
        pending = dict(dependencies)
        while pending:
            ready = sorted(object_id for object_id, deps in pending.items() if not deps)
            if not ready:
                cycle = ", ".join(sorted(pending))
                raise LayoutError(f"cyclic or unknown placement dependency: {cycle}")
            for object_id in ready:
                ordered.append(by_id[object_id])
                pending.pop(object_id)
                for deps in pending.values():
                    deps.discard(object_id)
        return ordered

    def _sample_object(
        self,
        request: ObjectRequest,
        asset: AssetRecord,
        room_dimensions: tuple[float, float, float],
        placed: list[PlacedObject],
        placed_by_id: dict[str, PlacedObject],
        rng: random.Random,
    ) -> PlacedObject:
        support_object, surface, surface_center, support_yaw = self._resolve_support(
            request.support or "floor", room_dimensions, placed_by_id
        )
        support_size = surface.size

        for _ in range(self.max_attempts):
            yaw = rng.uniform(*request.yaw_range_deg)
            half_x, half_y = rotated_half_extents_xy(asset.bbox_m, yaw - support_yaw)
            margin_x = support_size[0] / 2.0 - half_x
            margin_y = support_size[1] / 2.0 - half_y
            if request.edge_bias:
                margin_x += min(asset.bbox_m[0] * 0.2, support_size[0] * 0.08)
                margin_y += min(asset.bbox_m[1] * 0.2, support_size[1] * 0.08)
            if margin_x <= 0 or margin_y <= 0:
                raise LayoutError(
                    f"asset {asset.asset_id} does not fit support {request.support}"
                )

            local_x, local_y = self._sample_local_xy(
                request, surface_center, support_yaw, margin_x, margin_y, placed_by_id, rng
            )
            if request.edge_bias:
                if rng.random() < 0.5:
                    local_x = math.copysign(margin_x * rng.uniform(0.82, 1.0), rng.choice([-1, 1]))
                else:
                    local_y = math.copysign(margin_y * rng.uniform(0.82, 1.0), rng.choice([-1, 1]))

            offset_x, offset_y = rotate_xy((local_x, local_y), support_yaw)
            x = surface_center[0] + offset_x
            y = surface_center[1] + offset_y
            z = surface_center[2] + asset.bbox_m[2] / 2.0

            if request.region_xy is not None:
                xmin, xmax, ymin, ymax = request.region_xy
                if not (xmin <= x <= xmax and ymin <= y <= ymax):
                    continue

            candidate = self._make_placed(request, asset, Pose((x, y, z), yaw))
            support_id = support_object.object_id if support_object else None
            if self._collides(candidate, placed, support_id):
                continue
            if not self._relations_satisfied(candidate, placed_by_id):
                continue
            return candidate

        raise LayoutError(
            f"could not place {request.object_id} after {self.max_attempts} attempts"
        )

    def _sample_local_xy(
        self,
        request: ObjectRequest,
        surface_center: tuple[float, float, float],
        support_yaw: float,
        margin_x: float,
        margin_y: float,
        placed_by_id: dict[str, PlacedObject],
        rng: random.Random,
    ) -> tuple[float, float]:
        near_relations = [relation for relation in request.relations if relation.kind == "near"]
        if not near_relations:
            return (rng.uniform(-margin_x, margin_x), rng.uniform(-margin_y, margin_y))

        relation = near_relations[0]
        target = placed_by_id.get(relation.target)
        if target is None:
            return (rng.uniform(-margin_x, margin_x), rng.uniform(-margin_y, margin_y))
        target_offset = (
            target.pose.position[0] - surface_center[0],
            target.pose.position[1] - surface_center[1],
        )
        target_local = inverse_rotate_xy(target_offset, support_yaw)
        radius = rng.uniform(relation.min_distance_m, relation.max_distance_m)
        angle = rng.uniform(-math.pi, math.pi)
        return (
            max(-margin_x, min(margin_x, target_local[0] + radius * math.cos(angle))),
            max(-margin_y, min(margin_y, target_local[1] + radius * math.sin(angle))),
        )

    def _resolve_support(
        self,
        support: str,
        room_dimensions: tuple[float, float, float],
        placed_by_id: dict[str, PlacedObject],
    ) -> tuple[PlacedObject | None, SupportSurface, tuple[float, float, float], float]:
        if support == "floor":
            surface = SupportSurface(
                name="floor",
                center=(0.0, 0.0, 0.0),
                size=(room_dimensions[0], room_dimensions[1]),
            )
            return None, surface, surface.center, 0.0

        support_id, _, surface_name = support.partition(":")
        try:
            support_object = placed_by_id[support_id]
        except KeyError as exc:
            raise LayoutError(f"support object {support_id!r} has not been placed") from exc
        support_asset = self.registry.get(support_object.asset_id)
        surfaces = support_asset.support_surfaces
        if surface_name:
            surfaces = tuple(item for item in surfaces if item.name == surface_name)
        if not surfaces:
            raise LayoutError(f"asset {support_asset.asset_id} has no surface for {support}")
        surface = surfaces[0]
        rotated_center = rotate_xy((surface.center[0], surface.center[1]), support_object.pose.yaw_deg)
        center = (
            support_object.pose.position[0] + rotated_center[0],
            support_object.pose.position[1] + rotated_center[1],
            support_object.pose.position[2] + surface.center[2],
        )
        return support_object, surface, center, support_object.pose.yaw_deg

    def _make_placed(
        self, request: ObjectRequest, asset: AssetRecord, pose: Pose
    ) -> PlacedObject:
        return PlacedObject(
            object_id=request.object_id,
            asset_id=asset.asset_id,
            category=asset.category,
            bbox_m=asset.bbox_m,
            pose=pose,
            dynamic=request.dynamic,
            support=request.support,
            relations=request.relations,
        )

    def _collides(
        self,
        candidate: PlacedObject,
        placed: list[PlacedObject],
        support_id: str | None,
    ) -> bool:
        return any(
            existing.object_id != support_id and objects_overlap(candidate, existing)
            for existing in placed
        )

    def _relations_satisfied(
        self, candidate: PlacedObject, placed_by_id: dict[str, PlacedObject]
    ) -> bool:
        for relation in candidate.relations:
            if relation.kind != "near":
                continue
            target = placed_by_id.get(relation.target)
            if target is None:
                return False
            distance = math.hypot(
                candidate.pose.position[0] - target.pose.position[0],
                candidate.pose.position[1] - target.pose.position[1],
            )
            tolerance = max(candidate.bbox_m[0], candidate.bbox_m[1]) / 2.0
            if distance > relation.max_distance_m + tolerance:
                return False
        return True
