import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MainEntrypointTests(unittest.TestCase):
    def test_project_root_main_exposes_gui_entrypoint(self):
        module = importlib.import_module("main")
        self.assertTrue(callable(module.main))


if __name__ == "__main__":
    unittest.main()
