from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from installer import provisioning
from installer.provisioning import Provisioner, link_or_copy, same_physical_tree


class LocalComponentIdempotenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.comfy = base / "ComfyUI"; (self.comfy / "custom_nodes").mkdir(parents=True); (self.comfy / "models").mkdir()
        self.src = base / "src"; (self.src / "sub").mkdir(parents=True)
        (self.src / "__init__.py").write_text("a"); (self.src / "sub" / "x.txt").write_text("x")
        self.p = Provisioner(); self.p.components = {"c": {"name": "Node", "source": "src", "version": "1"}}
        self.p.profiles = {"p": {"components": ["c"]}}
        patcher = mock.patch.object(provisioning, "ROOT", base); patcher.start(); self.addCleanup(patcher.stop)
        self.target = self.comfy / "custom_nodes" / "Node"
    def tearDown(self) -> None: self.tmp.cleanup()

    def install(self) -> dict:
        state: dict = {}
        with mock.patch.object(Provisioner, "_run_cancellable"):
            self.p._component("c", self.comfy, False, state)
        return state

    def touch_source(self) -> None:
        stat = (self.src / "__init__.py").stat(); os.utime(self.src / "__init__.py", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))

    def test_fresh_install_then_second_install_is_idempotent(self) -> None:
        self.install(); self.assertTrue((self.target / ".edx-component.json").is_file())
        self.install()
        self.assertEqual((self.target / "sub" / "x.txt").read_text(), "x")

    def test_regression_source_changed_over_hardlinked_install_does_not_raise_same_file(self) -> None:
        self.install()
        if not os.path.samefile(self.src / "__init__.py", self.target / "__init__.py"): self.skipTest("filesystem sem hardlink")
        self.touch_source()  # fingerprint (mtime) muda, arquivos continuam sendo o mesmo arquivo físico
        self.assertEqual(self.p.plan("p", self.comfy).to_install, [])  # status: instalado
        self.install()  # não pode lançar shutil.SameFileError
        self.assertTrue(os.path.samefile(self.src / "__init__.py", self.target / "__init__.py"))

    def test_changed_source_is_resynced_when_destination_is_a_real_copy(self) -> None:
        self.install()
        (self.target / "__init__.py").unlink(); shutil.copy2(self.src / "__init__.py", self.target / "__init__.py")  # cópia independente
        (self.src / "__init__.py").write_text("changed-content"); self.touch_source()
        self.assertEqual(self.p.plan("p", self.comfy).to_install, ["component:c"])
        self.install()
        self.assertEqual((self.target / "__init__.py").read_text(), "changed-content")
        self.assertEqual(self.p.plan("p", self.comfy).to_install, [])

    def test_incomplete_install_is_still_detected_and_completed(self) -> None:
        self.install(); (self.target / "sub" / "x.txt").unlink(); self.touch_source()
        self.assertFalse(same_physical_tree(self.src, self.target))
        self.assertEqual(self.p.plan("p", self.comfy).to_install, ["component:c"])
        self.install(); self.assertTrue((self.target / "sub" / "x.txt").is_file())

    def test_link_or_copy_same_file_is_noop_and_different_file_is_replaced_without_write_through(self) -> None:
        a = self.src / "a.txt"; a.write_text("A"); dst = self.comfy / "a.txt"
        link_or_copy(str(a), str(dst)); link_or_copy(str(a), str(dst))  # 2ª vez: mesmo arquivo físico
        self.assertEqual(dst.read_text(), "A")
        other = self.comfy / "other.txt"; other.write_text("old"); alias = self.comfy / "alias.txt"
        try: os.link(other, alias)
        except OSError: self.skipTest("sem hardlink")
        link_or_copy(str(a), str(other))
        self.assertEqual(other.read_text(), "A"); self.assertEqual(alias.read_text(), "old")  # alias não foi sobrescrito

    def test_development_link_is_recognised_and_source_is_untouched(self) -> None:
        try: os.symlink(self.src, self.target, target_is_directory=True)
        except (OSError, NotImplementedError): self.skipTest("symlink indisponível sem privilégio")
        self.assertEqual(self.p.plan("p", self.comfy).to_install, [])
        before = sorted(p.name for p in self.src.rglob("*"))
        self.install()
        self.assertEqual(sorted(p.name for p in self.src.rglob("*")), before)  # nenhum marcador escrito na fonte
        self.assertTrue(self.target.is_symlink())

    def test_foreign_node_is_still_preserved(self) -> None:
        self.target.mkdir(); (self.target / "mine.py").write_text("m")
        self.install()
        self.assertEqual([p.name for p in self.target.iterdir()], ["mine.py"])


if __name__ == "__main__":
    unittest.main()
