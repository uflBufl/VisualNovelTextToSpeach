import ast
import unittest
from pathlib import Path


class SpeechBackendAPIBoundaryTest(unittest.TestCase):
    def _tree(self, relative: str) -> ast.Module:
        return ast.parse(
            (Path(__file__).parents[1] / relative).read_text(encoding="utf-8")
        )

    def test_runtime_speech_helpers_use_typed_playback_boundary(self):
        tree = self._tree("vntts/live_speech.py")
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        called_by_helper = {
            node.func.attr
            for node in ast.walk(functions["play_typed_text"])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn("prepare_playback", called_by_helper)
        self.assertIn("play_prepared", called_by_helper)

        controller_tree = self._tree("vntts/controller.py")
        controller_functions = {
            node.name: node
            for node in ast.walk(controller_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("speak_live_chunk", "_speak_with_live_backend"):
            called_names = {
                node.func.id
                for node in ast.walk(controller_functions[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            called = {
                node.func.attr
                for node in ast.walk(controller_functions[name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            self.assertIn("play_typed_text", called_names, name)
            self.assertNotIn("prepare", called, name)
            self.assertNotIn("play", called, name)
            self.assertNotIn("speak", called, name)

    def test_runtime_protocol_exposes_only_typed_playback_operations(self):
        tree = self._tree("vntts/speech_backend.py")
        protocol = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SpeechBackend"
        )
        methods = {
            node.name
            for node in protocol.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertEqual(methods, {"prepare_playback", "play_prepared", "stop"})


if __name__ == "__main__":
    unittest.main()
