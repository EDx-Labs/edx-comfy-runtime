from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from installer.catalog import ManifestCatalog
from installer.provisioning import ProvisioningError


class ManifestCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "installer"
        self.source = Path(self.tmp.name) / "runtime"
        for name in ("components", "models", "profiles", "workflow_sets"):
            (self.root / name).mkdir(parents=True)
        (self.source / "nodes").mkdir(parents=True)
        (self.source / "workflows").mkdir(parents=True)
        (self.source / "workflows" / "example.json").write_text("{}", encoding="utf-8")
        self._write("components/local.json", {"components": {
            "node": {"source": "nodes", "name": "ExampleNode", "hardware": {"min_vram": 2}},
        }})
        self._write("models/registry.json", {"models": {
            "weights": {"url": "https://example.invalid/weights", "size_bytes": 12},
        }})
        self._write("profiles/capabilities.json", {"profiles": {
            "base": {"components": ["node"], "models": ["weights"], "hardware": {
                "supported_backends": ["CUDA", "CPU"], "execution_strategies": ["local"],
            }, "workflow_sets": ["example"]},
            "child": {"requires": ["base"], "hardware": {"supported_backends": ["cuda"]}},
        }})
        self._write("workflow_sets/sets.json", {"workflow_sets": {
            "example": {"workflows": ["workflows/example.json"]},
        }})
        self._write("capabilities.json", {"capabilities": {
            "example-capability": {"aliases": ["example"], "providers": ["child"]},
        }})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, relative: str, payload: dict) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_resolution_and_requirements_are_pure_manifest_operations(self) -> None:
        catalog = ManifestCatalog(self.root, source_root=self.source)

        resolved = catalog.resolve("child")

        self.assertEqual(resolved.profiles, ("child", "base"))
        self.assertEqual(resolved.components, ("node",))
        self.assertEqual(resolved.models, ("weights",))
        self.assertEqual(catalog.profile_requirements("child")["download_bytes"], 12)
        self.assertEqual(catalog.profile_requirements("child")["supported_backends"], ["cuda"])
        self.assertTrue(catalog.profile_provisionability("child")["installable"])

    def test_capability_aliases_resolve_only_to_declared_profiles(self) -> None:
        catalog = ManifestCatalog(self.root, source_root=self.source)

        self.assertEqual(catalog.profiles_for_capability("example"), ["child"])
        self.assertEqual(catalog.resolve_profile("example-capability"), "child")
        with self.assertRaisesRegex(ProvisioningError, "capability inexistente"):
            catalog.resolve_profile("not-declared")

    def test_cycle_is_reported_without_accessing_a_comfyui_host(self) -> None:
        self._write("profiles/cycle.json", {"profiles": {
            "cycle-a": {"requires": ["cycle-b"]},
            "cycle-b": {"requires": ["cycle-a"]},
        }})
        catalog = ManifestCatalog(self.root, source_root=self.source)

        with self.assertRaisesRegex(ProvisioningError, "ciclo"):
            catalog.resolve("cycle-a")


if __name__ == "__main__":
    unittest.main()
