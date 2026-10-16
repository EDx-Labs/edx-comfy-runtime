from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from installer import storage
from installer.provisioning_state import ProvisioningStateStore, empty_state


class StateResilienceTests(unittest.TestCase):
    def test_failed_replace_keeps_previous_state_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = ProvisioningStateStore(path)
            previous = {**empty_state(), "profiles": {"previous": {"version": "1"}}}
            store.save(previous)

            replacement = {**empty_state(), "profiles": {"new": {"version": "2"}}}
            with mock.patch.object(storage.Path, "replace", side_effect=OSError("synthetic rename denial")):
                with self.assertRaises(OSError):
                    store.save(replacement)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), previous)
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_concurrent_saves_do_not_share_a_temporary_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            stores = [ProvisioningStateStore(path), ProvisioningStateStore(path)]
            states = [
                {**empty_state(), "profiles": {"first": {"version": "1"}}},
                {**empty_state(), "profiles": {"second": {"version": "2"}}},
            ]
            real_replace = Path.replace
            first_replace_started = threading.Event()
            release_first_replace = threading.Event()
            active_replace = False
            active_guard = threading.Lock()

            def synchronized_replace(source: Path, destination: Path):
                nonlocal active_replace
                with active_guard:
                    if active_replace:
                        raise AssertionError("state replacements overlapped in one process")
                    active_replace = True
                    first_replace_started.set()
                try:
                    if not release_first_replace.wait(5):
                        raise TimeoutError("synthetic test did not release state replacement")
                    return real_replace(source, destination)
                finally:
                    with active_guard:
                        active_replace = False

            errors: list[BaseException] = []
            with mock.patch.object(storage.Path, "replace", new=synchronized_replace):
                threads = [
                    threading.Thread(target=lambda store=store, state=state: self._save(store, state, errors))
                    for store, state in zip(stores, states)
                ]
                threads[0].start()
                self.assertTrue(first_replace_started.wait(5))
                threads[1].start()
                release_first_replace.set()
                for thread in threads:
                    thread.join(5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(loaded["profiles"], [state["profiles"] for state in states])
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    @staticmethod
    def _save(store: ProvisioningStateStore, state: dict, errors: list[BaseException]) -> None:
        try:
            store.save(state)
        except BaseException as exc:  # surfaced by the assertion in the caller
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
