"""Domain packs shared by natural-language scene compilation.

The core compiler remains domain agnostic.  A pack describes the vocabulary
and capabilities that a domain may contribute; assets and recipes still live
in the registry and recipe library so the existing deterministic pipeline is
reused unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainPack:
    domain_id: str
    room_types: tuple[str, ...]
    common_relations: tuple[str, ...]
    task_actions: tuple[str, ...]


DOMAIN_PACKS: tuple[DomainPack, ...] = (
    DomainPack("home", ("kitchen", "dining_room", "living_room"),
               ("on", "inside", "around", "near"),
               ("grasp", "place", "open", "close", "clean")),
    DomainPack("warehouse", ("warehouse", "storage"),
               ("on", "inside", "near", "blocking", "connected_to"),
               ("navigate", "grasp", "place", "retrieve", "load", "unload")),
    DomainPack("laboratory", ("laboratory", "lab"),
               ("on", "inside", "near", "attached_to", "connected_to"),
               ("navigate", "grasp", "place", "pour", "inspect", "assemble")),
    DomainPack("factory", ("factory", "workcell"),
               ("on", "inside", "near", "blocking", "connected_to"),
               ("navigate", "grasp", "place", "assemble", "inspect", "handoff")),
    DomainPack("outdoor", ("street", "campus", "outdoor"),
               ("near", "connected_to", "blocking"),
               ("navigate", "inspect", "follow", "avoid")),
)


def domain_pack_for_room(room_type: str) -> DomainPack:
    normalized = str(room_type).strip().lower()
    for pack in DOMAIN_PACKS:
        if normalized in pack.room_types:
            return pack
    return DomainPack("custom", (normalized,), ("near",), ("inspect",))


def domain_catalog() -> list[dict[str, object]]:
    return [
        {"domain_id": pack.domain_id, "room_types": list(pack.room_types),
         "common_relations": list(pack.common_relations),
         "task_actions": list(pack.task_actions)}
        for pack in DOMAIN_PACKS
    ]
