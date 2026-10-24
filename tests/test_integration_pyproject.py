import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# [project] keys defined by PEP 621. setuptools validates them and rejects anything else (pip -e then fails on the user's PC).
PEP_621_KEYS = {
    "name", "version", "description", "readme", "requires-python", "license", "license-files", "authors", "maintainers",
    "keywords", "classifiers", "urls", "scripts", "gui-scripts", "entry-points", "dependencies", "optional-dependencies", "dynamic",
}
SKIPPED_PARTS = {"node_modules", ".venv", "tests"}


class IntegrationPyprojectTests(unittest.TestCase):
    def test_every_bundled_node_declares_only_valid_project_keys(self):
        invalid = {}
        for path in sorted((ROOT / "integrations").rglob("pyproject.toml")):
            if SKIPPED_PARTS & set(path.relative_to(ROOT).parts):
                continue
            project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
            if project is not None and set(project) - PEP_621_KEYS:
                invalid[path.relative_to(ROOT).as_posix()] = sorted(set(project) - PEP_621_KEYS)
        self.assertEqual(invalid, {})


if __name__ == "__main__":
    unittest.main()
