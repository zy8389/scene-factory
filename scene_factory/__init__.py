"""SceneFactory public API."""

from .factory import BuildResult, SceneFactory
from .asset_validator import AssetValidator, validate_asset, validate_usd
from .asset_pipeline import AssetNormalizer, CollisionProcessor
from .registry import AssetLoader, AssetMetadata, AssetRegistry

__all__ = [
    "AssetLoader",
    "AssetMetadata",
    "AssetRegistry",
    "AssetNormalizer",
    "AssetValidator",
    "BuildResult",
    "CollisionProcessor",
    "SceneFactory",
    "validate_asset",
    "validate_usd",
]
__version__ = "0.1.0"
