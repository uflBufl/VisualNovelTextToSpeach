import ast
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vntts.controller import AppController
from vntts.controller_components import (
    DiagnosticsComponent,
    LiveSessionComponent,
    RuntimeLifecycleComponent,
    VoiceAssignmentComponent,
)
from vntts.settings import AppSettings
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


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

    def test_each_component_is_typed_to_the_composition_root(self):
        self.assertEqual(
            RuntimeLifecycleComponent.__annotations__["controller"],
            "AppController",
        )
        self.assertEqual(
            LiveSessionComponent.__annotations__["controller"],
            "AppController",
        )
        self.assertEqual(
            VoiceAssignmentComponent.__annotations__["controller"],
            "AppController",
        )
        self.assertEqual(
            DiagnosticsComponent.__annotations__["controller"],
            "AppController",
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
            notify=False, publish=True
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
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith(
                "_"
            ):
                continue
            for call in (
                node for node in ast.walk(method) if isinstance(node, ast.Call)
            ):
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

    def test_read_once_does_not_resume_emergency_during_live_shutdown(self):
        controller = Mock()
        controller.live_reader.is_running = True

        self.assertFalse(LiveSessionComponent(controller).read_once())

        controller.live_reader.resume_after_emergency.assert_not_called()
        controller.schedule_dialog_read.assert_not_called()

    def test_emergency_stop_cancels_pending_one_time_read(self):
        controller = Mock()
        controller.schedule_dialog_read.cancel.return_value = True
        controller.live_reader.emergency_stop.return_value = False

        self.assertTrue(LiveSessionComponent(controller).emergency_stop())

        controller.schedule_dialog_read.cancel.assert_called_once_with()
        controller.live_reader.emergency_stop.assert_called_once_with()

    def test_settings_change_cancels_pending_one_time_read(self):
        controller = Mock(is_live_running=False)

        self.assertFalse(
            RuntimeLifecycleComponent(controller)._stop_live_for_settings()
        )

        controller.schedule_dialog_read.cancel.assert_called_once_with()

    def test_auto_advance_enable_fails_closed_without_capture_authority(self):
        for capture_mode, sequence_mode, expected_reason in (
            ("screen", "off", "selected game window"),
            ("window", "audio-manual", "never sends advance keys"),
        ):
            with self.subTest(capture_mode=capture_mode, sequence_mode=sequence_mode):
                controller = Mock()
                controller.settings = AppSettings(
                    capture_mode=capture_mode,
                    live_sequence_mode=sequence_mode,
                    auto_advance_enabled=False,
                )
                controller.speech_backend = None
                controller.live_reader = Mock()
                component = LiveSessionComponent(controller)

                self.assertFalse(component.set_auto_advance_enabled(True))

                self.assertFalse(controller.settings.auto_advance_enabled)
                controller.live_reader.set_auto_advance.assert_called_once_with(
                    controller._live_auto_advance_callback.return_value
                )
                self.assertIn(
                    expected_reason,
                    controller.status_handler.call_args.args[0],
                )

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

    def test_shutdown_implementation_is_not_retained_on_controller(self):
        self.assertFalse(hasattr(AppController, "_shutdown_runtime"))

    def test_settings_apply_implementation_is_not_retained_on_controller(self):
        self.assertFalse(hasattr(AppController, "_apply_runtime_settings"))

    def test_startup_implementation_is_not_retained_on_controller(self):
        self.assertFalse(hasattr(AppController, "_start_runtime"))


if __name__ == "__main__":
    unittest.main()


class RuntimeLifecycleTest(unittest.TestCase):
    def test_startup_and_settings_refresh_wire_current_dialog_read_inputs(self):
        backend = Mock()
        reader = Mock(is_running=False)
        scheduler_factory = Mock()
        with (
            patch(
                "vntts.controller.initialize_voice_registry",
                return_value=CharacterVoiceRegistry(),
            ),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=reader),
            patch("vntts.controller.create_dialog_read_scheduler", scheduler_factory),
        ):
            controller = AppController(
                AppSettings(speech_backend="pocket-tts", warm_up_voices=False),
                pocket_backend_factory=Mock(return_value=backend),
                model_asset_manager_factory=Mock(),
            )
            self.addCleanup(controller.shutdown)
            self.assertTrue(controller.start())
            self.assertEqual(scheduler_factory.call_count, 1)
            initial_corrections = controller.correction_dictionary
            self.assertTrue(
                controller.apply_settings(
                    controller.settings.updated(
                        screenshot_directory="new-screenshots",
                        ocr_language="rus",
                        ocr_minimum_confidence=80,
                    )
                )
            )

        self.assertEqual(scheduler_factory.call_count, 2)
        initial, refreshed = scheduler_factory.call_args_list
        self.assertEqual(initial.args[:2], refreshed.args[:2])
        self.assertEqual(refreshed.args[2], Path("new-screenshots"))
        expected = {
            "live_reader": reader,
            "error_handler": controller.error_handler,
            "capture_target": controller.capture_target,
            "speech_handler": controller._enqueue_dialog,
            "minimum_confidence": 80,
            "uncertain_frame_recorder": controller.uncertain_frame_recorder,
            "diagnostic_handler": controller._publish_diagnostic,
            "voice_resolver": controller._resolve_voice_label,
            "ocr_language": "rus",
            "correction_dictionary": controller.correction_dictionary,
            "region_provider": controller._capture_region,
        }
        self.assertEqual(refreshed.kwargs, expected)
        self.assertEqual(
            initial.kwargs,
            {
                **expected,
                "minimum_confidence": 60,
                "ocr_language": "eng",
                "correction_dictionary": initial_corrections,
            },
        )

    def test_backend_initialization_resolves_narrator_once(self):
        for voice in (
            None,
            CharacterVoice("Narrator", "Ada"),
            CharacterVoice("Narrator", "Ada", references=(Path("ada.wav"),)),
        ):
            with self.subTest(voice=voice):
                registry = CharacterVoiceRegistry(() if voice is None else (voice,))
                backend_factory = Mock(return_value=Mock())
                controller = AppController(
                    AppSettings(speech_backend="moss-tts"),
                    moss_backend_factory=backend_factory,
                    model_asset_manager_factory=Mock(),
                )
                controller.voice_registry_initializer = Mock(return_value=registry)
                with patch.object(
                    registry, "resolve", wraps=registry.resolve
                ) as resolve:
                    self.assertTrue(
                        controller.runtime_lifecycle._initialize_backend(False)
                    )
                resolve.assert_called_once_with("Narrator")
                self.assertEqual(
                    backend_factory.call_args.kwargs["narrator_reference"],
                    None if voice is None or not voice.references else Path("ada.wav"),
                )
                controller.shutdown()
