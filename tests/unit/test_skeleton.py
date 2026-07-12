from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


class RepositorySkeletonTest(unittest.TestCase):
    def test_responsibility_packages_are_importable(self) -> None:
        modules = (
            "hooklane.api",
            "hooklane.worker",
            "hooklane.mock_sink",
            "hooklane.domain",
            "hooklane.queue",
            "hooklane.delivery",
            "hooklane.observability",
        )
        for module in modules:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module(module))


if __name__ == "__main__":
    unittest.main()
