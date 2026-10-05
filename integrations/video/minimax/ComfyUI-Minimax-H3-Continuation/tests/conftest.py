"""Configure tests to use an external ComfyUI checkout."""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _resolve_comfyui_root() -> Path:
    """Return and validate the external ComfyUI checkout configured for tests."""
    configured_root = os.environ.get("COMFYUI_ROOT")
    if not configured_root:
        raise pytest.UsageError(
            "COMFYUI_ROOT is required. Set it to an external ComfyUI 0.30.0+ checkout, "
            "for example: export COMFYUI_ROOT=/absolute/path/to/ComfyUI"
        )
    comfyui_root = Path(configured_root).expanduser().resolve()
    missing = [name for name in ("comfy", "comfy_api") if not (comfyui_root / name).is_dir()]
    if not comfyui_root.is_dir() or missing:
        raise pytest.UsageError(f"COMFYUI_ROOT must contain comfy/ and comfy_api/: {comfyui_root}")
    return comfyui_root


COMFYUI_ROOT = _resolve_comfyui_root()
sys.path.insert(0, str(COMFYUI_ROOT))


@pytest.fixture(scope="session")
def package_module() -> ModuleType:
    """Load the repository root as a package, as ComfyUI's loader does."""
    repository_root = Path(__file__).resolve().parents[1]
    module_name = "comfyui_minimax_h3_continuation_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        repository_root / "__init__.py",
        submodule_search_locations=[str(repository_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not construct a module spec for the custom-node package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def nodes_module(package_module: ModuleType) -> ModuleType:
    """Return the loaded continuation_nodes module."""
    return sys.modules[f"{package_module.__name__}.continuation_nodes"]
