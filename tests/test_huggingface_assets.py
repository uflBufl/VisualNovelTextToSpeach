import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.huggingface_assets import download_snapshot, inspect_snapshot


class HuggingFaceAssetsTest(unittest.TestCase):
    revision = "a" * 40

    def test_metadata_counts_only_complete_files_at_the_requested_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            config = cache / "config.json"
            config.write_bytes(b"1234")
            partial = cache / "weights.safetensors.incomplete"
            partial.write_bytes(b"123")
            info = SimpleNamespace(
                sha=self.revision,
                siblings=[
                    SimpleNamespace(rfilename="config.json", size=4),
                    SimpleNamespace(rfilename="weights.safetensors", size=100),
                    SimpleNamespace(rfilename="unused.bin", size=200),
                ],
            )
            api = Mock()
            api.repo_info.return_value = info
            lookup = Mock(side_effect=[str(config), str(partial)])
            with (
                patch("huggingface_hub.HfApi", return_value=api),
                patch("huggingface_hub.try_to_load_from_cache", lookup),
                patch("huggingface_hub.snapshot_download") as download,
            ):
                size = inspect_snapshot(
                    "owner/model",
                    revision=self.revision,
                    allow_patterns=["*.json", "*.safetensors"],
                    cache_dir=cache,
                    token=False,
                )
            self.assertEqual(size.total_bytes, 104)
            self.assertEqual(size.cached_bytes, 4)
            self.assertEqual(size.remaining_bytes, 100)
            self.assertEqual(size.cached_files, 1)
            self.assertEqual(size.total_files, 2)
            self.assertTrue(partial.exists())
            self.assertEqual(partial.read_bytes(), b"123")
            download.assert_not_called()
            for call in lookup.call_args_list:
                self.assertEqual(call.kwargs["revision"], self.revision)
            api.repo_info.assert_called_once_with(
                "owner/model",
                revision=self.revision,
                files_metadata=True,
                timeout=5.0,
                token=False,
            )

    def test_pinned_metadata_is_reused_offline_and_cache_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            weights = cache / "weights"
            api = Mock()
            api.repo_info.return_value = SimpleNamespace(
                sha=self.revision,
                siblings=[SimpleNamespace(rfilename="weights", size=8)],
            )
            lookup = Mock(side_effect=lambda *_args, **_kwargs: str(weights))
            with (
                patch("huggingface_hub.HfApi", return_value=api),
                patch("huggingface_hub.try_to_load_from_cache", lookup),
            ):
                first = inspect_snapshot(
                    "owner/model", revision=self.revision, cache_dir=cache
                )
                weights.write_bytes(b"complete")
                api.repo_info.side_effect = OSError("offline")
                second = inspect_snapshot(
                    "owner/model", revision=self.revision, cache_dir=cache
                )
            self.assertEqual(first.remaining_bytes, 8)
            self.assertEqual(second.remaining_bytes, 0)
            self.assertEqual(second.cached_bytes, 8)
            self.assertEqual(api.repo_info.call_count, 1)

    def test_unknown_remote_sizes_are_not_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("huggingface_hub.HfApi") as api,
                patch("huggingface_hub.try_to_load_from_cache", return_value=None),
            ):
                api.return_value.repo_info.return_value = SimpleNamespace(
                    sha=self.revision,
                    siblings=[SimpleNamespace(rfilename="weights", size=None)],
                )
                size = inspect_snapshot(
                    "owner/model", revision=self.revision, cache_dir=directory
                )
            self.assertIsNone(size.total_bytes)
            self.assertIsNone(size.remaining_bytes)
            self.assertEqual(size.cached_bytes, 0)

    def test_metadata_for_another_commit_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("huggingface_hub.HfApi") as api:
                api.return_value.repo_info.return_value = SimpleNamespace(
                    sha="b" * 40, siblings=[]
                )
                with self.assertRaisesRegex(ValueError, "pinned model"):
                    inspect_snapshot(
                        "owner/model", revision=self.revision, cache_dir=directory
                    )

    def test_invalid_cached_metadata_is_refetched_before_cache_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            with (
                patch("huggingface_hub.HfApi") as api,
                patch(
                    "huggingface_hub.try_to_load_from_cache", return_value=None
                ) as lookup,
            ):
                api.return_value.repo_info.return_value = SimpleNamespace(
                    sha=self.revision,
                    siblings=[SimpleNamespace(rfilename="weights", size=8)],
                )
                inspect_snapshot("owner/model", revision=self.revision, cache_dir=cache)
                manifest = next((cache / ".vntts-metadata").glob("*.json"))
                metadata = json.loads(manifest.read_text())
                metadata["files"] = [["../outside-cache", 8]]
                manifest.write_text(json.dumps(metadata))
                lookup.reset_mock()
                size = inspect_snapshot(
                    "owner/model", revision=self.revision, cache_dir=cache
                )
            self.assertEqual(api.return_value.repo_info.call_count, 2)
            self.assertEqual(size.total_bytes, 8)
            self.assertEqual(lookup.call_args.args[1], "weights")

    def test_real_disabled_hub_bars_still_report_desktop_progress(self):
        events = []

        def snapshot(*_args, tqdm_class, **_kwargs):
            with tqdm_class(unit="files", total=2, disable=True) as files:
                files.update(1)
                files.update(1)
            with tqdm_class(unit="B", total=100, disable=True) as transfer:
                transfer.update(20)
            return "snapshot"

        with patch("huggingface_hub.snapshot_download", side_effect=snapshot):
            download_snapshot("owner/model", asset="Model", progress=events.append)
        self.assertTrue(any(event.completed_files == 2 for event in events))
        self.assertEqual(events[-1].completed_bytes, 20)
        self.assertEqual(events[-1].total_bytes, 100)

    def test_transfer_reports_measured_bytes_and_preserves_unknown_file_only_bytes(
        self,
    ):
        class Bar:
            def __init__(self, *, total, unit, initial=0, disable=False, **_kwargs):
                self.n, self.total, self.disable = initial, total, disable

            def refresh(self, *_args, **_kwargs):
                pass

            def update(self, n=1):
                if not self.disable:
                    self.n += n
                    self.refresh()

            def close(self):
                pass

        for byte_progress in (False, True):
            with self.subTest(byte_progress=byte_progress):
                events, statuses = [], []

                def snapshot(*_args, tqdm_class, **_options):
                    files = tqdm_class(unit="files", total=3, disable=True)
                    files.update(1)
                    logger = logging.getLogger("huggingface_hub.file_download")
                    logger.info("Downloading 'weights' to '/cache/weights.incomplete'")
                    if byte_progress:
                        transfer = tqdm_class(
                            unit="B",
                            total=100,
                            disable=True,
                            name="huggingface_hub.snapshot_download.transfer",
                        )
                        transfer.update(25)
                        transfer.close()
                        # Closing a terminal bar twice must not send stale events.
                        transfer.close()
                        reconstruction = tqdm_class(
                            unit="B",
                            total=100,
                            disable=True,
                            name="huggingface_hub.snapshot_download",
                        )
                        reconstruction.update(60)
                        reconstruction.close()
                    files.update(2)
                    files.close()
                    return "snapshot"

                with patch.dict(
                    "sys.modules",
                    {
                        "huggingface_hub": SimpleNamespace(snapshot_download=snapshot),
                        "huggingface_hub.utils.tqdm": SimpleNamespace(tqdm=Bar),
                    },
                ):
                    self.assertEqual(
                        download_snapshot(
                            "owner/model",
                            asset="Model",
                            report=statuses.append,
                            progress=events.append,
                        ),
                        "snapshot",
                    )
                self.assertEqual(
                    statuses, ["Resolving Model...", "Downloading Model..."]
                )
                self.assertEqual(events[0].phase, "resolving")
                self.assertEqual(events[-1].completed_files, 3)
                bytes_events = [
                    event for event in events if event.completed_bytes is not None
                ]
                if byte_progress:
                    self.assertEqual(bytes_events[-1].completed_bytes, 25)
                    self.assertIsNone(bytes_events[-1].total_bytes)
                    self.assertEqual(events[-1].completed_bytes, 25)
                    self.assertEqual(events[-1].reconstructed_bytes, 60)
                    self.assertEqual(bytes_events[-1].completed_files, 3)
                else:
                    self.assertEqual(bytes_events, [])

    def test_cached_file_counts_never_claim_a_download(self):
        events, statuses = [], []

        class Bar:
            def __init__(self, **kwargs):
                self.total = kwargs["total"]
                self.n = self.total

            def refresh(self):
                pass

        def snapshot(*_args, tqdm_class, **_kwargs):
            tqdm_class(unit="files", total=4).refresh()
            return "cached"

        with patch.dict(
            "sys.modules",
            {
                "huggingface_hub": SimpleNamespace(snapshot_download=snapshot),
                "huggingface_hub.utils.tqdm": SimpleNamespace(tqdm=Bar),
            },
        ):
            download_snapshot(
                "owner/model",
                asset="Model",
                report=statuses.append,
                progress=events.append,
            )
        self.assertEqual(statuses, ["Resolving Model..."])
        self.assertTrue(all(event.phase == "resolving" for event in events))
        self.assertTrue(all(event.completed_bytes is None for event in events))


if __name__ == "__main__":
    unittest.main()
