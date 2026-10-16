from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from installer.workflow_adapter import WorkflowInstaller


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkflowInstallerTests(unittest.TestCase):
    def test_install_is_idempotent_and_records_the_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "workflow.json"
            source.write_text('{"nodes": []}', encoding="utf-8")
            comfy = root / "ComfyUI"
            state: dict = {}
            installer = WorkflowInstaller(
                source_root=root,
                workflow_sets={"demo": {"workflows": ["workflow.json"]}},
                digest=digest,
            )

            installer.install("demo", comfy, state)
            first = (comfy / "user/default/workflows/EDx/demo/workflow.json").read_text(encoding="utf-8")
            installer.install("demo", comfy, state)

            self.assertEqual(first, source.read_text(encoding="utf-8"))
            record = next(iter(state["workflows"].values()))
            self.assertEqual(record["workflow_set"], "demo")
            self.assertEqual(record["hash"], digest(source))

    def test_edited_workflow_is_preserved_when_owned_record_no_longer_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "workflow.json"
            source.write_text("source", encoding="utf-8")
            comfy = root / "ComfyUI"
            target = comfy / "user/default/workflows/EDx/demo/workflow.json"
            target.parent.mkdir(parents=True)
            target.write_text("user edit", encoding="utf-8")
            state = {"workflows": {str(target): {"hash": digest(source)}}}
            installer = WorkflowInstaller(
                source_root=root,
                workflow_sets={"demo": {"workflows": ["workflow.json"]}},
                digest=digest,
            )

            installer.install("demo", comfy, state)

            self.assertEqual(target.read_text(encoding="utf-8"), "user edit")


if __name__ == "__main__":
    unittest.main()
