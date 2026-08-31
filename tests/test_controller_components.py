import ast
import unittest
from pathlib import Path
from unittest.mock import Mock

from vntts.controller import AppController
from vntts.controller_components import (
    DiagnosticsComponent,
    LiveSessionComponent,
    RuntimeLifecycleComponent,
    VoiceAssignmentComponent,
)
from vntts.settings import AppSettings


class ControllerComponentsTest(unittest.TestCase):
    def test_controller_is_the_composition_root(self):
        controller = AppController(AppSettings())

        self.assertIsInstance(controller.runtime_lifecycle, RuntimeLifecycleComponent)
        self.assertIsInstance(controller.live_session, LiveSessionComponent)
        self.assertIsInstance(controller.voice_assignments, VoiceAssignmentComponent)
        self.assertIsInstance(controller.diagnostics, DiagnosticsComponent)
        self.assertIs(controller.runtime_lifecycle.controller, controller)
        self.assertIs(controller.live_session.controller, controller)
        self.assertIs(controller.voice_assignments.controller, controller)
        self.assertIs(controller.diagnostics.controller, controller)

    def test_public_operations_delegate_to_their_components(self):
        controller = AppController(AppSettings())
        controller.runtime_lifecycle = Mock()
        controller.live_session = Mock()
        controller.voice_assignments = Mock()
        controller.diagnostics = Mock()

        controller.start()
        controller.apply_settings("settings")
        controller.shutdown()
        controller.toggle_live()
        controller.assign_voice("A", "voice")
        controller.inspect_current_dialog(notify=False)

        controller.runtime_lifecycle.start.assert_called_once_with()
        controller.runtime_lifecycle.apply_settings.assert_called_once_with("settings")
        controller.runtime_lifecycle.shutdown.assert_called_once_with()
        controller.live_session.toggle.assert_called_once_with()
        controller.voice_assignments.assign.assert_called_once_with("A", "voice")
        controller.diagnostics.inspect_current_dialog.assert_called_once_with(
            notify=False
        )

    def test_facade_methods_do_not_reaccumulate_coordination_logic(self):
        tree = ast.parse(
            (Path(__file__).parents[1] / "vntts" / "controller.py").read_text(
                encoding="utf-8"
            )
        )
        controller = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AppController"
        )
        expected_components = {
            "start": "runtime_lifecycle",
            "apply_settings": "runtime_lifecycle",
            "shutdown": "runtime_lifecycle",
            "read_once": "live_session",
            "identify_live_scope": "live_session",
            "toggle_live": "live_session",
            "toggle_speech_pause": "live_session",
            "skip_current_speech": "live_session",
            "repeat_last_speech": "live_session",
            "clear_speech_queue": "live_session",
            "emergency_stop": "live_session",
            "set_auto_advance_enabled": "live_session",
            "available_voice_characters": "voice_assignments",
            "available_voice_choices": "voice_assignments",
            "voice_assignment_for": "voice_assignments",
            "preview_voice_choice": "voice_assignments",
            "stop_voice_preview": "voice_assignments",
            "assign_voice": "voice_assignments",
            "clear_voice_assignment": "voice_assignments",
            "set_force_live_narrator": "voice_assignments",
            "allow_narrator_fallback": "voice_assignments",
            "unresolved_live_speakers": "voice_assignments",
            "approve_live_narrator_fallbacks": "voice_assignments",
            "preview_voice": "voice_assignments",
            "replay_dialog": "voice_assignments",
            "get_capture_geometry": "diagnostics",
            "get_latest_diagnostic": "diagnostics",
            "get_live_pipeline_metrics": "diagnostics",
            "inspect_current_dialog": "diagnostics",
            "test_current_dialog": "diagnostics",
        }
        methods = {
            node.name: node
            for node in controller.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        for name, component_name in expected_components.items():
            method = methods[name]
            self.assertEqual(len(method.body), 1, name)
            self.assertIsInstance(method.body[0], ast.Return, name)
            call = method.body[0].value
            self.assertIsInstance(call, ast.Call, name)
            self.assertIsInstance(call.func, ast.Attribute, name)
            owner = call.func.value
            self.assertIsInstance(owner, ast.Attribute, name)
            self.assertEqual(owner.attr, component_name, name)


if __name__ == "__main__":
    unittest.main()
