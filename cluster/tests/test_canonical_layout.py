from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cluster import coordinator


class CanonicalLayoutTests(unittest.TestCase):
    def modules(self, root: Path, name: str, marker: str, *, complete: bool = True):
        directory = root / name
        directory.mkdir()
        (directory / "structures.py").write_text("MARKER = " + repr(marker), encoding="utf-8")
        if complete:
            (directory / "progressive.py").write_text("MARKER = " + repr(marker), encoding="utf-8")

    def load(self, root: Path):
        with mock.patch.object(coordinator, "__file__", str(root / "cluster" / "coordinator.py")):
            return coordinator._canonical_modules()

    def test_source_tree_loads_workbench(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.modules(root, "workbench", "source")
            self.assertEqual([m.MARKER for m in self.load(root)], ["source", "source"])

    def test_deployment_loads_app_even_when_old_workbench_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.modules(root, "app", "deployed")
            self.modules(root, "workbench", "stale")
            self.assertEqual([m.MARKER for m in self.load(root)], ["deployed", "deployed"])

    def test_incomplete_app_does_not_fall_back_to_old_workbench(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.modules(root, "app", "incomplete", complete=False)
            self.modules(root, "workbench", "stale")
            with self.assertRaises(FileNotFoundError):
                self.load(root)


if __name__ == "__main__":
    unittest.main()
