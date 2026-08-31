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

    def test_each_component_uses_its_own_private_port(self):
        self.assertEqual(
            RuntimeLifecycleComponent.__annotations__["controller"],
            "_RuntimeLifecyclePort",
        )
        self.assertEqual(
            LiveSessionComponent.__annotations__["controller"],
            "_LiveSessionPort",
        )
        self.assertEqual(
            VoiceAssignmentComponent.__annotations__["controller"],
            "_VoiceAssignmentPort",
        )
        self.assertEqual(
            DiagnosticsComponent.__annotations__["controller"],
            "_DiagnosticsPort",
        )

    def test_public_operations_delegate_to_their_components(self):
        controller = AppController(AppSettings())
        controller.runtime_lifecycle = Mock()
        controller.live_session = Mock()
        controller.voice_assignments = Mock()
        controller.diagnostics = Mock()

        controller.start()
        controller.apply_settings("settings", cancellation="token")
        controller.cancel_settings_apply("token")
        controller.shutdown()
        controller.toggle_live()
        controller.assign_voice("A", "voice")
        controller.inspect_current_dialog(notify=False)

        controller.runtime_lifecycle.start.assert_called_once_with()
        controller.runtime_lifecycle.apply_settings.assert_called_once_with(
            "settings",
            cancellation="token",
        )
        controller.runtime_lifecycle.cancel_settings_apply.assert_called_once_with(
            "token"
        )
        controller.runtime_lifecycle.shutdown.assert_called_once_with()
        controller.live_session.toggle.assert_called_once_with()
        controller.voice_assignments.assign.assert_called_once_with(
            "A",
            "voice",
            commit_settings=None,
        )
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
            "cancel_settings_apply": "runtime_lifecycle",
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

    def test_private_implementation_does_not_reenter_public_facade(self):
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
        public_operations = {
            "start",
            "apply_settings",
            "cancel_settings_apply",
            "shutdown",
            "read_once",
            "identify_live_scope",
            "toggle_live",
            "toggle_speech_pause",
            "skip_current_speech",
            "repeat_last_speech",
            "clear_speech_queue",
            "emergency_stop",
            "set_auto_advance_enabled",
            "available_voice_characters",
            "available_voice_choices",
            "voice_assignment_for",
            "preview_voice_choice",
            "stop_voice_preview",
            "assign_voice",
            "clear_voice_assignment",
            "set_force_live_narrator",
            "allow_narrator_fallback",
            "unresolved_live_speakers",
            "approve_live_narrator_fallbacks",
            "preview_voice",
            "replay_dialog",
            "get_capture_geometry",
            "get_latest_diagnostic",
            "get_live_pipeline_metrics",
            "inspect_current_dialog",
            "test_current_dialog",
        }
        violations = []
        for method in controller.body:
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith("_"):
                continue
            for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                target = call.func
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr in public_operations
                ):
                    violations.append(f"{method.name} -> {target.attr}")

        self.assertEqual(violations, [])

    def test_diagnostics_implementation_is_not_retained_on_controller(self):
        self.assertFalse(hasattr(AppController, "_inspect_current_dialog_impl"))
        self.assertFalse(hasattr(AppController, "_test_current_dialog_impl"))

    def test_basic_live_controls_are_not_retained_on_controller(self):
        migrated = (
            "_read_once_live",
            "_identify_live_scope_impl",
            "_toggle_live_impl",
            "_live_voice_preflight_allows_start",
            "_toggle_speech_pause_impl",
            "_skip_current_speech_impl",
            "_repeat_last_speech_impl",
            "_clear_speech_queue_impl",
            "_emergency_stop_impl",
            "_set_auto_advance_enabled_impl",
        )
        for name in migrated:
            self.assertFalse(hasattr(AppController, name), name)

    def test_basic_voice_actions_are_not_retained_on_controller(self):
        migrated = (
            "_available_voice_characters_impl",
            "_available_voice_choices_impl",
            "_voice_assignment_for_impl",
            "_preview_voice_choice_impl",
            "_unresolved_live_speakers_impl",
            "_stop_voice_preview_impl",
            "_allow_narrator_fallback_impl",
            "_approve_live_narrator_fallbacks_impl",
            "_preview_voice_impl",
            "_replay_dialog_impl",
        )
        for name in migrated:
            self.assertFalse(hasattr(AppController, name), name)


if __name__ == "__main__":
    unittest.main()
