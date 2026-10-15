import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "MotionScoreHRpQCT" / "MotionScoreHRpQCT.py"


class ExtensionReviewRequirementsTest(unittest.TestCase):
    def test_package_install_uses_slicer_pip_helper(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        pip_install = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_pip_install"
        )
        calls = [node.func for node in ast.walk(pip_install) if isinstance(node, ast.Call)]

        self.assertTrue(
            any(
                isinstance(func, ast.Attribute)
                and func.attr == "pip_install"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "util"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "slicer"
                for func in calls
            )
        )
        self.assertFalse(
            any(
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                for func in calls
            )
        )

    def test_readme_discloses_registration_payload(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()

        for phrase in (
            "free registration is required",
            "name, institution, and email address",
            "hostname and mac address",
            "device hash",
        ):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
