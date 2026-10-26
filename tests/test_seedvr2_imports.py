"""The vendored SeedVR2 node must import inside ComfyUI: a relative import that climbs above the node folder
("attempted relative import beyond top-level package") makes ComfyUI skip the whole node, and every upscale then fails."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

NODE = Path(__file__).resolve().parents[1] / "integrations" / "upscale" / "seedvr2_videoupscaler"
RELATIVE = re.compile(r"^\s*from\s+(\.+)([\w.]*)\s+import\s+(.+)$", re.M)


def unresolved(root: Path) -> list[str]:
    problems = []
    for source in sorted(root.joinpath("src").rglob("*.py")):
        relative = source.relative_to(root)
        package = relative.parts[:-1]
        for match in RELATIVE.finditer(source.read_text(encoding="utf-8", errors="ignore")):
            dots, module, names = len(match.group(1)), match.group(2), match.group(3)
            if dots - 1 > len(package):
                problems.append(f"{relative}: sobe acima da raiz do nó: {match.group(0).strip()}")
                continue
            base = root.joinpath(*package[: len(package) - (dots - 1)])
            target = base.joinpath(*[part for part in module.split(".") if part])
            if module:
                found = target.with_suffix(".py").is_file() or target.is_dir()
            else:
                first = names.split(",")[0].strip().split(" ")[0].strip("()")
                found = (target / f"{first}.py").is_file() or (target / first).is_dir() or (target / "__init__.py").is_file()
            if not found:
                problems.append(f"{relative}: não resolve: {match.group(0).strip()}")
    return problems


class SeedVR2ImportTests(unittest.TestCase):
    @unittest.skipUnless(NODE.joinpath("src").is_dir(), "submódulo/fonte do SeedVR2 ausente")
    def test_every_relative_import_resolves_inside_the_node(self) -> None:
        self.assertEqual(unresolved(NODE), [])

    def test_the_checker_catches_an_import_that_climbs_out_of_the_node(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "models").mkdir(parents=True)
            (root / "src" / "models" / "a.py").write_text("from .......upscale.node.src.models import na\nfrom . import b\n", encoding="utf-8")
            (root / "src" / "models" / "b.py").write_text("", encoding="utf-8")
            problems = unresolved(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("sobe acima", problems[0])


if __name__ == "__main__":
    unittest.main()
