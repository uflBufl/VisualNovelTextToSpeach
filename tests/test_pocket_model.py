import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2
from huggingface_hub.errors import GatedRepoError

from vntts.pocket_model import (
    asset_path,
    model_assets,
    pocket_model_cache,
    pocket_model_ready,
    prepare_pocket_model,
)
from vntts.pocket_model_worker import (
    _authorize,
    _download_originals,
    _DownloadProgress,
    main,
)


class PocketModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        pocket_model_cache.cache_clear()
        self.addCleanup(pocket_model_cache.cache_clear)

    def config(self):
        return {
            "roots": [str(self.root / "managed"), str(self.root / "old")],
            "assets": [
                ["kyutai/pocket-tts", "pinned", "languages/english/model.safetensors"]
            ],
            "model_url": "https://huggingface.co/kyutai/pocket-tts",
        }

    def access_error(self, status):
        response = httpx2.Response(
            status,
            request=httpx2.Request("GET", "https://huggingface.co/kyutai/pocket-tts"),
        )
        return GatedRepoError("private detail", response=response)

    def test_readiness_admits_local_original_assets_once_without_network(self):
        for asset in model_assets():
            path = asset_path(self.root, asset)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"admitted asset")
        with patch("vntts.pocket_model.cache_roots", return_value=(self.root,)):
            self.assertTrue(pocket_model_ready())
            with patch.object(
                Path, "is_file", side_effect=AssertionError("revalidated")
            ):
                self.assertTrue(pocket_model_ready())
        self.assertTrue(
            all(
                repo in {"kyutai/pocket-tts", "kyutai/pocket-tts-without-voice-cloning"}
                for repo, _rev, _file in model_assets()
            )
        )

    def test_download_reports_progress_without_terminal_bars(self):
        with patch("vntts.pocket_model_worker._event") as event:
            with _DownloadProgress(total=100, disable=True) as progress:
                progress.update(25)
                progress.update(75)
        self.assertIn("100%", event.call_args.kwargs["message"])
        self.assertEqual(event.call_args.args, ("progress",))

    def test_legacy_cached_asset_is_reused_without_authorization(self):
        config = self.config()
        previous = asset_path(Path(config["roots"][1]), config["assets"][0])
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"original")
        with (
            patch(
                "huggingface_hub.hf_hub_download",
                side_effect=AssertionError("downloaded"),
            ),
            patch("vntts.pocket_model_worker._event"),
        ):
            _download_originals(config)
        self.assertEqual(
            asset_path(Path(config["roots"][0]), config["assets"][0]).read_bytes(),
            b"original",
        )

    def test_login_then_genuine_terms_then_original_download(self):
        with (
            patch(
                "huggingface_hub.hf_hub_download",
                side_effect=[self.access_error(401), self.access_error(403), "ready"],
            ) as download,
            patch("huggingface_hub.get_token", return_value="credential"),
            patch("vntts.pocket_model_worker._authorize") as authorize,
            patch("vntts.pocket_model_worker._event") as event,
            patch("sys.stdin", io.StringIO("continue\n")),
        ):
            _download_originals(self.config())
        authorize.assert_called_once_with()
        self.assertIn(
            unittest.mock.call("terms", url=self.config()["model_url"]),
            event.call_args_list,
        )
        self.assertEqual(download.call_count, 3)
        self.assertEqual(download.call_args.kwargs["repo_id"], "kyutai/pocket-tts")

    def test_rejected_terms_do_not_loop_or_claim_readiness(self):
        with (
            patch(
                "huggingface_hub.hf_hub_download", side_effect=self.access_error(403)
            ) as download,
            patch("huggingface_hub.get_token", return_value="credential"),
            patch("vntts.pocket_model_worker._event"),
            patch("sys.stdin", io.StringIO("continue\n")),
            self.assertRaises(GatedRepoError),
        ):
            _download_originals(self.config())
        self.assertEqual(download.call_count, 2)

    def test_authorization_persists_sdk_token_without_exporting_it(self):
        with (
            patch(
                "huggingface_hub.utils._oauth_device.request_device_code",
                return_value={
                    "verification_uri_complete": "https://huggingface.co/oauth/device?user_code=code",
                    "user_code": "code",
                },
            ),
            patch(
                "huggingface_hub.utils._oauth_device.poll_device_token",
                return_value={"access_token": "private-token"},
            ),
            patch("huggingface_hub._login._save_oauth_token") as save,
            patch("vntts.pocket_model_worker._event") as event,
            patch.dict(
                os.environ, {"HF_TOKEN": "expired", "HUGGING_FACE_HUB_TOKEN": "old"}
            ),
        ):
            _authorize()
            self.assertNotIn("HF_TOKEN", os.environ)
            self.assertNotIn("HUGGING_FACE_HUB_TOKEN", os.environ)
        save.assert_called_once_with({"access_token": "private-token"})
        self.assertNotIn("private-token", repr(event.call_args_list))

    def test_declined_and_expired_authorization_have_distinct_retry_guidance(self):
        from huggingface_hub.errors import DeviceCodeError

        for code, message in (
            ("access_denied", "declined"),
            ("expired_token", "expired"),
        ):
            with self.subTest(code=code):
                output = io.StringIO()
                with (
                    patch("sys.stdin", io.StringIO(json.dumps(self.config()) + "\n")),
                    patch("sys.stdout", output),
                    patch(
                        "vntts.pocket_model_worker._download_originals",
                        side_effect=DeviceCodeError(
                            "private response", error_code=code
                        ),
                    ),
                    self.assertRaises(SystemExit),
                ):
                    main()
                result = json.loads(output.getvalue())
                self.assertEqual(result["kind"], "error")
                self.assertIn(message, result["message"])
                self.assertNotIn("private response", result["message"])

    def test_protocol_failure_hides_credentials_and_reports_no_ready(self):
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.config()) + "\n")),
            patch("sys.stdout", output),
            patch(
                "vntts.pocket_model_worker._download_originals",
                side_effect=ValueError("private-token"),
            ),
            self.assertRaises(SystemExit),
        ):
            main()
        self.assertNotIn("private-token", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["kind"], "error")


class PocketSetupProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(
            patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
        )
        self.cancel = Event()
        self.continue_event = Event()
        self.enterContext(
            patch(
                "vntts.runtime_installation.ensure_speech_runtime",
                return_value=(self.root, self.root / "python", self.root / "site"),
            )
        )
        self.enterContext(
            patch("vntts.runtime_ownership.claim_runtime", return_value=None)
        )
        manager = self.enterContext(patch("vntts.model_assets.ModelAssetManager"))
        manager.return_value.huggingface_environment.return_value = {
            "HF_HOME": str(self.root)
        }
        self.enterContext(
            patch("vntts.pocket_model.cache_roots", return_value=(self.root,))
        )
        self.terminate = self.enterContext(
            patch("vntts.pocket_model.terminate_running_process")
        )

    def process(self, events):
        return SimpleNamespace(
            stdin=Mock(),
            stdout=io.StringIO("".join(json.dumps(event) + "\n" for event in events)),
            wait=Mock(),
            returncode=0,
        )

    def test_parent_delivers_auth_and_terms_then_publishes_local_ready(self):
        process = self.process(
            [
                {
                    "kind": "authorize",
                    "url": "https://huggingface.co/oauth/device",
                    "code": "code",
                },
                {"kind": "terms"},
                {"kind": "ready"},
            ]
        )
        observed = []

        def authorize(url, code):
            observed.append((url, code))
            if not code:
                self.continue_event.set()

        with (
            patch("vntts.pocket_model.pocket_model_ready", side_effect=[False, True]),
            patch(
                "vntts.pocket_model.subprocess.Popen", return_value=process
            ) as launched,
        ):
            prepare_pocket_model(
                cancel_event=self.cancel,
                continue_event=self.continue_event,
                progress=Mock(),
                authorize=authorize,
            )
        self.assertEqual(observed[-1], ("https://huggingface.co/kyutai/pocket-tts", ""))
        self.assertEqual(process.stdin.write.call_args.args, ("continue\n",))
        self.assertEqual(
            launched.call_args.kwargs["env"]["HF_ENDPOINT"], "https://huggingface.co"
        )
        self.terminate.assert_called_once_with(process)

    def test_cancel_while_waiting_for_model_terms_stops_child(self):
        from vntts.runtime_preparation import RuntimePreparationCancelled

        process = self.process([{"kind": "terms"}])
        with (
            patch("vntts.pocket_model.pocket_model_ready", return_value=False),
            patch("vntts.pocket_model.subprocess.Popen", return_value=process),
            self.assertRaises(RuntimePreparationCancelled),
        ):
            prepare_pocket_model(
                cancel_event=self.cancel,
                continue_event=self.continue_event,
                progress=Mock(),
                authorize=lambda *_: self.cancel.set(),
            )
        self.terminate.assert_called_once_with(process)
        process.stdin.close.assert_called_once_with()

    def test_existing_local_model_does_not_start_setup_process(self):
        with (
            patch("vntts.pocket_model.pocket_model_ready", return_value=True),
            patch("vntts.pocket_model.subprocess.Popen") as launched,
        ):
            prepare_pocket_model(
                cancel_event=self.cancel,
                continue_event=self.continue_event,
                progress=Mock(),
                authorize=Mock(),
            )
        launched.assert_not_called()

    def test_old_login_is_reused_without_overwriting_shared_token_path(self):
        token = self.root / "cache" / "huggingface" / "token"
        token.parent.mkdir(parents=True)
        token.write_text("previous-login")
        process = self.process([{"kind": "ready"}])
        with (
            patch("vntts.pocket_model.pocket_model_ready", side_effect=[False, True]),
            patch(
                "vntts.pocket_model.subprocess.Popen", return_value=process
            ) as launched,
        ):
            prepare_pocket_model(
                cancel_event=self.cancel,
                continue_event=self.continue_event,
                progress=Mock(),
                authorize=Mock(),
            )
        self.assertEqual(launched.call_args.kwargs["env"]["HF_TOKEN"], "previous-login")
        self.assertNotIn("HF_TOKEN_PATH", launched.call_args.kwargs["env"])
        self.assertEqual(token.read_text(), "previous-login")
