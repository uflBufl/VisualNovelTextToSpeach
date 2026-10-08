import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scripts import moss_native_pause_probe as probe
from tests.native_moss_fixtures import FakeNativeBackend as _FakeBackend
from tests.native_moss_fixtures import OwnedServer as _OwnedServer
from tests.pregeneration_fixtures import clean_wav_bytes
from vntts.settings import AppSettings
from vntts.synthesis import SynthesisCompletion
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoice, CharacterVoiceRegistry, VoiceManifestError


class _LimitedThenInterruptedBackend(_FakeBackend):
    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        if len(self.requests) == 2:
            raise KeyboardInterrupt
        return self._result(request, SynthesisCompletion.LIMITED)


class _NoRawBackend(_FakeBackend):
    def render(self, request):
        self.requests.append(request)
        return self._result(request, SynthesisCompletion.COMPLETE)


class _LimitedThenReplacementBackend(_FakeBackend):
    def __init__(self, registry, *, replacement_stops, **options):
        super().__init__(registry, **options)
        self.replacement_stops = replacement_stops
        self.server = _OwnedServer(101)

    def render(self, request):
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        if len(self.requests) == 1:
            self.server.returncode = 0
            return self._result(request, SynthesisCompletion.LIMITED)
        if len(self.requests) == 2:
            self.server = _OwnedServer(202)
        return self._result(request, SynthesisCompletion.COMPLETE)

    def shutdown(self):
        super().shutdown()
        if self.replacement_stops:
            self.server.returncode = 0


def _options(root):
    reference = root / "reference.wav"
    reference.write_bytes(clean_wav_bytes())
    return SimpleNamespace(
        reference=reference, output=root / "probe", model=None, executable=None
    )


class MossNativePauseProbeTest(unittest.TestCase):
    def test_unrelated_log_events_do_not_disrupt_native_evidence(self):
        class LoggedBackend(_FakeBackend):
            def render(self, request):
                probe.support.record_native_speech(
                    operation="fresh-generation", outcome="complete"
                )
                return super().render(request)

        log = probe.support.NativeSpeechLog()
        log.add("info", "An unrelated log event has no native details")
        with TemporaryDirectory() as temporary:
            options = _options(Path(temporary))
            with patch.object(probe.support, "native_speech_log", log):
                self.assertEqual(
                    probe.run(
                        options,
                        backend_factory=LoggedBackend,
                        path_check=lambda _model: (
                            Path("server"),
                            Path("model.gguf"),
                            Path("codec.gguf"),
                        ),
                        settings_loader=AppSettings,
                    ),
                    0,
                )
            report = json.loads((options.output / "report.json").read_text())
            self.assertTrue(report["all_requests_complete"])
            self.assertEqual(len(report["attempts"]), 6)
            self.assertTrue(
                all(
                    attempt["native"]["outcome"] == "complete"
                    for attempt in report["attempts"]
                )
            )
            original = LoggedBackend.instances[-1]
            self.assertEqual(original._http.__func__, _FakeBackend._http)

    def test_missing_raw_responses_never_count_as_complete_evidence(self):
        with TemporaryDirectory() as temporary:
            options = _options(Path(temporary))

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_NoRawBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                0,
            )

            report = json.loads((options.output / "report.json").read_text())
            self.assertFalse(report["all_requests_complete"])
            self.assertFalse(report["all_requests_terminal"])

    def test_required_alternate_uses_another_usable_saved_game_voice(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            narrator = root / "narrator.wav"
            alternate = root / "alternate.wav"
            narrator.write_bytes(clean_wav_bytes())
            alternate.write_bytes(clean_wav_bytes(amplitude=0.11))
            registry = CharacterVoiceRegistry(
                (
                    CharacterVoice("Narrator", "narrator", narrator),
                    CharacterVoice("Centurion", "centurion", alternate),
                )
            )

            qualification, selected_registry = probe._qualification_alternate(
                SimpleNamespace(
                    alternate_reference=None,
                    require_changing_voice=True,
                ),
                registry,
                narrator,
            )

            self.assertEqual(qualification, ("Centurion", alternate.resolve()))
            self.assertIs(selected_registry, registry)

    def test_replacement_servers_all_receive_shutdown_receipts(self):
        for replacement_stops, expected in ((False, False), (True, True)):
            with self.subTest(replacement_stops=replacement_stops):
                with TemporaryDirectory() as temporary:
                    options = _options(Path(temporary))
                    self.assertEqual(
                        probe.run(
                            options,
                            backend_factory=lambda registry, **options: (
                                _LimitedThenReplacementBackend(
                                    registry,
                                    replacement_stops=replacement_stops,
                                    **options,
                                )
                            ),
                            path_check=lambda _: (
                                Path("server"),
                                Path("model.gguf"),
                                Path("codec.gguf"),
                            ),
                            settings_loader=lambda: SimpleNamespace(tts_model=None),
                        ),
                        0 if replacement_stops else 1,
                    )
                    report = json.loads((options.output / "report.json").read_text())
                    self.assertFalse(report["all_requests_complete"])
                    self.assertTrue(report["all_requests_terminal"])
                    self.assertEqual(report["exit_code"], 0 if replacement_stops else 1)
                    receipt = report["server_shutdown"]
                    self.assertEqual(receipt["confirmed_exited"], expected)
                    self.assertEqual(
                        [item["pid"] for item in receipt["servers"]], [101, 202]
                    )

    def test_surviving_owned_server_fails_probe_and_retains_report(self):
        def factory(registry, **options):
            backend = _FakeBackend(registry, **options)
            backend.server = SimpleNamespace(pid=123, poll=lambda: None)
            return backend

        with TemporaryDirectory() as temporary:
            options = _options(Path(temporary))
            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=factory,
                    path_check=lambda _: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                1,
            )
            report = json.loads((options.output / "report.json").read_text())
            self.assertFalse(report["server_shutdown"]["confirmed_exited"])
            self.assertIn("still running", report["shutdown_error"])
            self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_missing_explicit_narrator_never_falls_back_to_base_voice(self):
        with TemporaryDirectory() as temporary:
            reference = Path(temporary) / "old.wav"
            reference.write_bytes(clean_wav_bytes(seconds=0.06075, sample_rate=24000))
            settings = AppSettings()
            library = VoiceLibrary(Path(temporary) / "library")
            library.select("Narrator", route="voice", source_id="character:missing")

            def missing_voice(_settings, *, voice_library):
                raise VoiceManifestError("selected voice is no longer available")

            with self.assertRaisesRegex(
                VoiceManifestError, "selected voice is no longer available"
            ):
                probe._saved_narrator_reference(
                    settings,
                    missing_voice,
                    library,
                )

    def test_probe_writes_cacheless_stereo_comparison_and_stops_backend(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                0,
            )

            report = json.loads((options.output / "report.json").read_text())
            self.assertEqual(report["http_capture_method"], "_http")
            self.assertTrue(report["all_requests_complete"])
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(report["reference"], "reference.wav")
            self.assertEqual(report["reference_preflight"]["path"], "reference.wav")
            self.assertTrue(report["contains_generated_voice_audio"])
            self.assertNotIn(str(root.resolve()), json.dumps(report))
            self.assertIsNone(report["server_shutdown"]["confirmed_exited"])
            self.assertEqual(len(report["attempts"]), 6)
            joined = next(
                item
                for item in report["attempts"]
                if item["id"].endswith("stable-joined")
            )
            sentences = next(
                item
                for item in report["attempts"]
                if item["id"].endswith("stable-sentences")
            )
            self.assertEqual(
                joined["production_limits"], sentences["production_limits"]
            )
            self.assertEqual(joined["raw_quality"]["mono"]["analysis_version"], 2)
            for attempt in report["attempts"]:
                self.assertGreaterEqual(attempt["output_wav_validation_s"], 0)
                self.assertGreaterEqual(attempt["raw_wav_validation_s"], 0)
                self.assertTrue(
                    (options.output / attempt["files"]["raw_wav"]["path"]).is_file()
                )
                self.assertTrue(
                    (
                        options.output / attempt["files"]["output_mono_wav"]["path"]
                    ).is_file()
                )
                self.assertTrue((options.output / f"{attempt['id']}.json").is_file())
            backend = _FakeBackend.instances[0]
            self.assertTrue(backend.shutdown_called)
            self.assertFalse(Path(backend.options["prompt_cache_directory"]).exists())
            self.assertFalse((options.output / ".cache-disabled").exists())
            self.assertTrue(all(request.seed == 1 for request in backend.requests))
            self.assertTrue(
                all(
                    request.cache_policy is probe.SynthesisCachePolicy.BYPASS
                    for request in backend.requests
                )
            )
            archive = options.output.with_suffix(".zip")
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn("report.json", bundle.namelist())
                self.assertIn(joined["files"]["raw_wav"]["path"], bundle.namelist())
                self.assertIn(
                    joined["files"]["output_mono_wav"]["path"], bundle.namelist()
                )

    def test_probe_records_cold_warm_and_changed_voice_phases(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            options.alternate_reference = root / "alternate.wav"
            options.alternate_reference.write_bytes(clean_wav_bytes(amplitude=0.11))
            options.require_changing_voice = True
            options.timing_sequence = True

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                0,
            )

            report = json.loads((options.output / "report.json").read_text())
            self.assertEqual(report["expected_attempt_count"], 9)
            self.assertTrue(report["all_requests_complete"])
            self.assertTrue(report["all_requests_terminal"])
            self.assertEqual(
                [
                    attempt["phase"]
                    for attempt in report["attempts"]
                    if attempt["phase"]
                    in {
                        "same-voice-warm",
                        "changed-voice-cold",
                        "changed-voice-warm",
                    }
                ],
                ["same-voice-warm", "changed-voice-cold", "changed-voice-warm"],
            )
            self.assertEqual(report["alternate_reference"]["name"], "alternate.wav")

    def test_probe_never_overwrites_an_existing_output_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            options.output.mkdir()
            marker = options.output / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "already exists"):
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_omitted_reference_uses_only_the_saved_narrator_reference(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "saved.wav"
            reference.write_bytes(clean_wav_bytes())
            options = SimpleNamespace(
                reference=None,
                output=root / "probe",
                model=Path("model.gguf"),
                executable=None,
            )
            registry = CharacterVoiceRegistry(
                (CharacterVoice("Narrator", "narrator", reference),)
            )
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: AppSettings(tts_model="saved-model"),
                    registry_initializer=lambda _settings, **_kwargs: registry,
                    voice_library=VoiceLibrary(root / "library"),
                ),
                0,
            )
            self.assertIs(_FakeBackend.instances[0].registry, registry)

    def test_omitted_reference_rejects_an_ambiguous_saved_narrator(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "one.wav", root / "two.wav"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            options = SimpleNamespace(
                reference=None,
                output=root / "probe",
                model=Path("model.gguf"),
                executable=None,
            )
            registry = SimpleNamespace(
                resolve=lambda _name: SimpleNamespace(references=(first, second))
            )

            with self.assertRaisesRegex(ValueError, "pass --reference PATH"):
                probe.run(
                    options,
                    settings_loader=lambda: AppSettings(tts_model="saved-model"),
                    registry_initializer=lambda _settings, **_kwargs: registry,
                )

    def test_limited_and_interrupted_attempts_keep_reports_raw_audio_and_archive(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _LimitedThenInterruptedBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_LimitedThenInterruptedBackend,
                    path_check=lambda _model: (
                        Path("server"),
                        Path("model.gguf"),
                        Path("codec.gguf"),
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                130,
            )
            report = json.loads((options.output / "report.json").read_text())
            self.assertTrue(report["interrupted"])
            self.assertEqual(
                [item["completion"] for item in report["attempts"]],
                ["limited", "cancelled"],
            )
            self.assertEqual(
                report["attempts"][0]["result"]["limits"],
                report["attempts"][0]["production_limits"],
            )
            self.assertTrue(report["attempts"][1]["interrupted"])
            self.assertTrue(
                (
                    options.output / report["attempts"][1]["files"]["raw_wav"]["path"]
                ).is_file()
            )
            self.assertEqual(
                len(_LimitedThenInterruptedBackend.instances[0].requests), 2
            )
            self.assertTrue(_LimitedThenInterruptedBackend.instances[0].shutdown_called)
            self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_missing_native_assets_never_construct_the_backend(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            _FakeBackend.instances.clear()

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=lambda _model: (_ for _ in ()).throw(
                        RuntimeError("assets missing")
                    ),
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                1,
            )
            report = json.loads((options.output / "report.json").read_text())
            self.assertIn("assets missing", report["error"])
            self.assertFalse(_FakeBackend.instances)
            self.assertTrue(options.output.with_suffix(".zip").is_file())

    def test_unusable_reference_is_reported_without_loading_native_runtime(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = _options(root)
            options.reference.write_bytes(
                clean_wav_bytes(seconds=0.06075, sample_rate=24000)
            )
            _FakeBackend.instances.clear()

            def no_native_probe(_model):
                self.fail("invalid reference must fail before native runtime checks")

            self.assertEqual(
                probe.run(
                    options,
                    backend_factory=_FakeBackend,
                    path_check=no_native_probe,
                    settings_loader=lambda: SimpleNamespace(tts_model=None),
                ),
                1,
            )
            self.assertFalse(_FakeBackend.instances)
            with zipfile.ZipFile(options.output.with_suffix(".zip")) as archive:
                report = json.loads(archive.read("report.json"))
            self.assertEqual(report["attempts"], [])
            self.assertEqual(report["reference_preflight"]["duration_seconds"], 0.061)
            self.assertIn("duration-under-1-second", report["error"])
            self.assertIn("--reference PATH", report["error"])


if __name__ == "__main__":
    unittest.main()
