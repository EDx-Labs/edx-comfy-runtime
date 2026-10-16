from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from installer.provisioning_state import ProvisioningStateStore, empty_state


class ProvisioningStateStoreTests(unittest.TestCase):
    def test_missing_state_has_schema_defaults_without_creating_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProvisioningStateStore(Path(directory) / "EDx" / "provisioning-state.json")

            state = store.load()

            self.assertEqual(state, empty_state())
            self.assertFalse(store.exists())
            self.assertFalse(store.path.parent.exists())

    def test_save_is_atomic_and_preserves_unknown_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProvisioningStateStore(Path(directory) / "EDx" / "provisioning-state.json")
            state = {**empty_state(), "future_field": {"kept": True}}

            store.save(state)

            self.assertTrue(store.exists())
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8")), state)
            self.assertFalse(store.path.with_suffix(".json.tmp").exists())

    def test_explicit_empty_fallback_distinguishes_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ProvisioningStateStore(Path(directory) / "state.json")

            self.assertEqual(store.load({}), {})


if __name__ == "__main__":
    unittest.main()
