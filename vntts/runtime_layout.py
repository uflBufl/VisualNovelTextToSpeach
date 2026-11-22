"""Directories for isolated speech runtime generations."""

import re
import sys
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4


def runtime_storage_location(recipe: Path) -> Path:
    # Keep the full digest in verified.json. CUDA appends long DLL names here.
    return recipe.with_name(recipe.name[:24]) if sys.platform == "win32" else recipe


def valid_recipe_directory(name: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{24}|[0-9a-f]{64})", name) is not None


def create_generation(location: Path) -> Path:
    generations = location / "generations"
    if sys.platform == "win32":
        # Native imports can hit MAX_PATH even after uv successfully installs.
        generations.mkdir(parents=True, exist_ok=True)
        return Path(mkdtemp(prefix="w-", dir=generations))
    generation = generations / uuid4().hex
    generation.mkdir(parents=True)
    return generation


def generation_environment(generation: Path) -> Path:
    return generation / ("e" if generation.name.startswith("w-") else "environment")


def valid_generation_name(name: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{32}|w-[a-z0-9_]{1,32})", name) is not None
