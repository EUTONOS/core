"""Guard: every subprocess launch in src/eutonos must be able to hide its console window."""

import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "eutonos"
LAUNCHERS = {"run", "Popen", "check_output", "check_call", "call"}

# "file.py:function" -> reason a launch may stay bare. Only calls that must inherit the
# user's terminal belong here; none exist today.
ALLOWED = {}


def _bare_launches(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in LAUNCHERS and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "subprocess"
                    and not any(k.arg in (None, "creationflags") for k in child.keywords)):
                found.append((child.lineno, function))
            visit(child, name)

    visit(tree, "<module>")
    return found


class NoBareSubprocessTests(unittest.TestCase):
    def test_every_launch_sets_creationflags(self):
        bare = []
        for path in sorted(SOURCE.glob("*.py")):
            for line, function in _bare_launches(path):
                if f"{path.name}:{function}" not in ALLOWED:
                    bare.append(f"{path.name}:{line} in {function}")
        self.assertEqual([], bare, "Pass **no_window() (eutonos.hidden_process) or add an ALLOWED entry.")


if __name__ == "__main__":
    unittest.main()
