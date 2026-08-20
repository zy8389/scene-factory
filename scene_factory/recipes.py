from __future__ import annotations

import json
from pathlib import Path

from .models import SceneRecipe


class RecipeLibrary:
    def __init__(self, recipes: list[SceneRecipe]) -> None:
        if not recipes:
            raise ValueError("recipe library is empty")
        self._recipes = {recipe.name: recipe for recipe in recipes}
        if len(self._recipes) != len(recipes):
            raise ValueError("recipe names must be unique")

    @classmethod
    def load(cls, directory: str | Path) -> "RecipeLibrary":
        directory = Path(directory)
        recipes = []
        for path in sorted(directory.glob("*.json")):
            with path.open("r", encoding="utf-8") as handle:
                try:
                    recipes.append(SceneRecipe.from_dict(json.load(handle)))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid recipe {path}: {exc}") from exc
        return cls(recipes)

    def get(self, name: str) -> SceneRecipe:
        try:
            return self._recipes[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self._recipes))
            raise KeyError(f"unknown recipe {name!r}; available: {choices}") from exc

    def match_prompt(self, prompt: str) -> SceneRecipe:
        normalized = prompt.lower()
        scored = []
        for recipe in self._recipes.values():
            score = sum(2 if keyword in normalized else 0 for keyword in recipe.keywords)
            if recipe.room_type.lower() in normalized:
                score += 1
            scored.append((score, recipe.name, recipe))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored[0][0] == 0:
            return self._recipes[sorted(self._recipes)[0]]
        return scored[0][2]

    def names(self) -> list[str]:
        return sorted(self._recipes)

    def values(self) -> tuple[SceneRecipe, ...]:
        return tuple(self._recipes[name] for name in sorted(self._recipes))

    def room_types(self) -> list[str]:
        return sorted({recipe.room_type for recipe in self._recipes.values()})

    def events(self) -> list[str]:
        return sorted({recipe.event for recipe in self._recipes.values()})
