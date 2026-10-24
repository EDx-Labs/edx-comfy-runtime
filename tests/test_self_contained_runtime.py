from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from installer import runtime as runtime_module
from installer.provisioning import ProvisioningCancelled, ProvisioningError
from installer.runtime import BaseRuntimeInstaller, bundled_python, remove_tree, runtime_python

COMMIT = "a" * 40


def write_manifest(root: Path, *, archive: bool = True) -> Path:
    comfy = {
        "repository": "https://example.invalid/ComfyUI.git", "revision": "aaaaaaa",
        "entrypoint": "main.py", "requirements": "requirements.txt",
        "torch_backends": {"nvidia": {"packages": ["torch"], "index_url": "https://download.example/cu130"}},
    }
    if archive:
        comfy["archive"] = {"url": "https://example.invalid/ComfyUI.zip", "commit": COMMIT}
    path = root / "runtime.json"
    path.write_text(json.dumps({"comfyui": comfy}), encoding="utf-8")
    return path


def make_source_zip(path: Path, top: str = f"ComfyUI-{COMMIT}", extra: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"{top}/main.py", "")
        bundle.writestr(f"{top}/requirements.txt", "")
        for name, data in (extra or {}).items():
            bundle.writestr(name, data)


class SelfContainedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "ComfyUI"
        self.commands: list[list[str]] = []

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        if "venv" in command:
            python = runtime_python(Path(command[-1]).parent)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
        return CompletedProcess(command, 0, "", "")

    def downloader(self, zip_builder=make_source_zip):
        calls = []

        def download(url, target, cancel_event=None):
            calls.append(url)
            zip_builder(target)

        download.calls = calls
        return download

    def test_installs_from_the_pinned_archive_without_git_and_with_the_given_python(self):
        download = self.downloader()
        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "py" / "python.exe", downloader=download)
        result = installer.ensure(self.target)
        self.assertTrue(result["installed"])
        self.assertEqual(download.calls, ["https://example.invalid/ComfyUI.zip"])
        self.assertFalse(any(command[0] == "git" for command in self.commands), self.commands)
        venv = next(command for command in self.commands if "venv" in command)
        self.assertEqual(Path(venv[0]).name, "python.exe")
        self.assertEqual(Path(venv[0]).parent.name, "py")
        self.assertTrue((self.target / "main.py").is_file())
        self.assertFalse((self.target / ".git").exists())
        marker = json.loads((self.target / ".edx-source.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["commit"], COMMIT)

    def test_second_ensure_is_idempotent_and_does_not_download_again(self):
        download = self.downloader()
        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "python.exe", downloader=download)
        installer.ensure(self.target)
        count = len(self.commands)
        installer.ensure(self.target)
        self.assertEqual(len(download.calls), 1)
        self.assertEqual(len(self.commands), count)

    def test_rejects_an_archive_that_is_not_the_pinned_commit(self):
        download = self.downloader(lambda target: make_source_zip(target, top="ComfyUI-" + "b" * 40))
        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "python.exe", downloader=download)
        with self.assertRaises(ProvisioningError):
            installer.ensure(self.target)
        self.assertFalse(self.target.exists())

    def test_rejects_entries_that_escape_the_unpack_directory(self):
        download = self.downloader(lambda target: make_source_zip(target, extra={f"ComfyUI-{COMMIT}/../../escape.txt": b"x"}))
        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "python.exe", downloader=download)
        with self.assertRaises(ProvisioningError):
            installer.ensure(self.target)
        self.assertFalse((self.root / "escape.txt").exists())

    def test_cancelled_download_leaves_no_install_behind(self):
        def cancelled(url, target, cancel_event=None):
            raise ProvisioningCancelled("instalação do Runtime cancelada")

        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "python.exe", downloader=cancelled)
        with self.assertRaises(ProvisioningCancelled):
            installer.ensure(self.target)
        self.assertFalse(self.target.exists())

    def test_manifest_without_archive_keeps_the_git_path(self):
        def runner(command, **kwargs):
            self.commands.append([str(part) for part in command])
            if command[:2] == ["git", "clone"]:
                partial = Path(command[-1])
                (partial / ".git").mkdir(parents=True)
                (partial / "main.py").write_text("", encoding="utf-8")
                (partial / "requirements.txt").write_text("", encoding="utf-8")
            return self.runner(command, **kwargs)

        installer = BaseRuntimeInstaller(write_manifest(self.root, archive=False), runner=runner, python_executable=self.root / "python.exe")
        installer.ensure(self.target)
        self.assertTrue(any(command[:2] == ["git", "clone"] for command in self.commands))

    def test_a_leftover_installing_folder_with_read_only_files_does_not_block_a_new_install(self):
        leftover = self.root / "ComfyUI.installing"
        pack = leftover / ".git" / "objects" / "pack" / "pack-1.pack"
        pack.parent.mkdir(parents=True)
        pack.write_bytes(b"git")
        os.chmod(pack, stat.S_IREAD)
        installer = BaseRuntimeInstaller(write_manifest(self.root), runner=self.runner, python_executable=self.root / "python.exe", downloader=self.downloader())
        self.assertTrue(installer.ensure(self.target)["installed"])
        self.assertFalse(leftover.exists())
        self.assertFalse((self.target / ".git").exists())

    def test_remove_tree_deletes_read_only_files(self):
        folder = self.root / "tree"
        locked = folder / "a" / "b.bin"
        locked.parent.mkdir(parents=True)
        locked.write_bytes(b"x")
        os.chmod(locked, stat.S_IREAD)
        remove_tree(folder)
        self.assertFalse(folder.exists())
        remove_tree(folder)  # idempotent

    def test_shipped_manifest_pins_a_full_commit_archive_over_https(self):
        shipped = json.loads((runtime_module.ROOT / "installer" / "runtime.json").read_text(encoding="utf-8"))["comfyui"]
        archive = shipped["archive"]
        self.assertRegex(archive["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(archive["url"].startswith("https://"))
        self.assertTrue(archive["commit"].startswith(shipped["revision"]))
        self.assertIn(archive["commit"], archive["url"])


class BundledPythonTests(unittest.TestCase):
    def test_prefers_the_interpreter_shipped_next_to_the_runtime_package(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder)
            package = agent / "edx-comfy-runtime"
            (package / "installer").mkdir(parents=True)
            name = "python.exe" if runtime_module.os.name == "nt" else "bin/python3"
            exe = agent / "python-runtime" / name
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"python")
            with mock.patch.object(runtime_module, "ROOT", package):
                self.assertEqual(bundled_python(), exe)
                installer = BaseRuntimeInstaller(package / "runtime.json") if (package / "runtime.json").is_file() else None
                self.assertIsNone(installer)

    def test_returns_none_when_no_interpreter_is_bundled(self):
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "edx-comfy-runtime"
            package.mkdir()
            with mock.patch.object(runtime_module, "ROOT", package), mock.patch.object(runtime_module.sys, "executable", str(Path(folder) / "x" / "python.exe")):
                self.assertIsNone(bundled_python())


if __name__ == "__main__":
    unittest.main()
