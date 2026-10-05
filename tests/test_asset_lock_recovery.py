from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner

DATA = b"abcdef"
MODEL = {"url": "https://example.invalid/m", "filename": "model.bin", "directory": "x", "size_bytes": 6, "sha256": hashlib.sha256(DATA).hexdigest()}


class Resp:
    def __init__(self, data, status=200): self.data, self.status = data, status
    def read(self, n=-1): d, self.data = self.data, b""; return d
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class AssetLockRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); self.target = Path(self.tmp.name) / "model.bin"
        self.lock = self.target.with_suffix(".bin.lock"); self.partial = self.target.with_suffix(".bin.partial")
        self.sleeps = 0
        self.p = Provisioner(sleeper=self._sleep)
    def tearDown(self) -> None: self.tmp.cleanup()
    def _sleep(self, _delay) -> None:
        self.sleeps += 1
        if self.sleeps > 5: raise AssertionError("ficou esperando um lock que deveria ter sido recuperado/preservado")
    def write_lock(self, pid=424242, age=0.0) -> None:
        self.lock.write_text(json.dumps({"pid": pid, "created_at": time.time() - age}))
    def acquire(self):
        with self.p._asset_lock(self.target): return json.loads(self.lock.read_text())["pid"]
    def windows(self, state):
        return mock.patch.multiple(provisioning, IS_WINDOWS=True, _windows_process_state=mock.Mock(return_value=state))

    def test_windows_dead_pid_recovers_lock_without_os_kill(self) -> None:
        self.write_lock()
        with self.windows("dead"), mock.patch.object(os, "kill", side_effect=AssertionError("os.kill proibido")) as kill:
            self.assertEqual(self.acquire(), os.getpid()); kill.assert_not_called()
        self.assertFalse(self.lock.exists())

    def test_windows_live_pid_keeps_lock(self) -> None:
        self.write_lock()
        with self.windows("alive"), self.assertRaises(AssertionError): self.acquire()
        self.assertEqual(json.loads(self.lock.read_text())["pid"], 424242)

    def test_windows_ambiguous_keeps_lock_until_24h(self) -> None:
        self.write_lock()
        with self.windows("unknown"), self.assertRaises(AssertionError): self.acquire()
        self.assertTrue(self.lock.exists())
        self.sleeps = 0; self.write_lock(age=25 * 3600)
        with self.windows("unknown"): self.assertEqual(self.acquire(), os.getpid())

    def test_regression_system_error_from_os_kill_never_reaches_caller(self) -> None:
        self.write_lock()
        with self.windows("dead"), mock.patch.object(os, "kill", side_effect=SystemError("<class 'OSError'> returned a result with an exception set")):
            self.assertEqual(self.acquire(), os.getpid())

    def test_real_win32_state_for_self_and_missing_pid(self) -> None:
        if os.name != "nt": self.skipTest("Win32")
        self.assertEqual(provisioning.process_state(os.getpid()), "alive")
        self.assertEqual(provisioning.process_state(999999), "dead")

    def test_win32_access_denied_consults_process_snapshot(self) -> None:
        """Regressão: PID com 'acesso negado' que nem consta no snapshot (processo encerrado) é órfão, não ambíguo."""
        if os.name != "nt": self.skipTest("Win32")
        for listed, expected in ((None, "unknown"), (True, "alive"), (False, "dead")):
            with mock.patch("ctypes.get_last_error", return_value=5), mock.patch("ctypes.WinDLL") as dll,                     mock.patch.object(provisioning, "_windows_pid_listed", return_value=listed):
                dll.return_value.OpenProcess.return_value = 0
                self.assertEqual(provisioning._windows_process_state(1), expected)

    def test_win32_snapshot_lists_self_and_not_missing_pid(self) -> None:
        if os.name != "nt": self.skipTest("Win32")
        self.assertTrue(provisioning._windows_pid_listed(os.getpid()))
        self.assertFalse(provisioning._windows_pid_listed(999999))

    def test_posix_states(self) -> None:
        with mock.patch.object(provisioning, "IS_WINDOWS", False):
            with mock.patch.object(os, "kill", side_effect=ProcessLookupError): self.assertEqual(provisioning.process_state(5), "dead")
            with mock.patch.object(os, "kill", side_effect=PermissionError): self.assertEqual(provisioning.process_state(5), "alive")
            with mock.patch.object(os, "kill", side_effect=SystemError): self.assertEqual(provisioning.process_state(5), "unknown")
            with mock.patch.object(os, "kill", return_value=None): self.assertEqual(provisioning.process_state(5), "alive")

    def test_orphan_lock_keeps_partial_and_download_resumes(self) -> None:
        self.partial.write_bytes(b"abc"); self.write_lock(); requests = []
        def opener(request, timeout): requests.append(dict(request.headers)); return Resp(b"def", 206)
        p = Provisioner(opener=opener, sleeper=self._sleep)
        with self.windows("dead"): self.assertTrue(p._download("m", self.target, MODEL))
        self.assertEqual(self.target.read_bytes(), DATA); self.assertEqual(requests[0].get("Range"), "bytes=3-")
        self.assertFalse(self.lock.exists())

    def test_lock_vanishing_during_recovery_is_a_normal_race(self) -> None:
        self.write_lock()
        real = Provisioner._reclaim_orphan_lock
        def vanish(lock_path, pid): lock_path.unlink(); real(lock_path, pid)
        with self.windows("dead"), mock.patch.object(Provisioner, "_reclaim_orphan_lock", staticmethod(vanish)):
            self.assertEqual(self.acquire(), os.getpid())

    def test_fresh_lock_taken_by_another_process_is_not_removed(self) -> None:
        self.write_lock(pid=111)  # dono morto
        orig = Provisioner._reclaim_orphan_lock
        def swap_then_reclaim(lock_path, pid):
            # outro processo substitui o lock órfão por um novo, vivo, antes da remoção
            lock_path.write_text(json.dumps({"pid": 222, "created_at": time.time()}))
            orig(lock_path, pid)
        with mock.patch.object(Provisioner, "_reclaim_orphan_lock", staticmethod(swap_then_reclaim)), \
                mock.patch.object(provisioning, "process_state", side_effect=lambda pid: "dead" if pid == 111 else "alive"):
            with self.assertRaises(AssertionError): self.acquire()
        self.assertEqual(json.loads(self.lock.read_text())["pid"], 222)

    def test_concurrent_acquirers_stay_mutually_exclusive(self) -> None:
        self.write_lock(); inside = []; overlap = []
        def worker():
            with Provisioner()._asset_lock(self.target):
                inside.append(1)
                if len(inside) > 1: overlap.append(1)
                time.sleep(0.05); inside.pop()
        with mock.patch.object(provisioning, "process_state", side_effect=lambda pid: "dead" if pid == 424242 else "alive"):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(overlap, [])


if __name__ == "__main__":
    unittest.main()
