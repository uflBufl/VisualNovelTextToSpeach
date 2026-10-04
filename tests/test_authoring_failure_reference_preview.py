import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_authoring_failure_reference_audit import (
    _PreviewBackendFactory,
    create_failed_reference_workspace,
)
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAuditError,
    prepare_failure_reference_audio,
    publish_failure_reference_audit,
)
from vntts.authoring.failure_reference_preview import (
    FailureReferencePreviewError,
    FailureReferencePreviewService,
)


class FailureReferencePreviewTest(unittest.TestCase):
    def create_audit(self, root):
        workspace, _queue_id = create_failed_reference_workspace(root)
        audit = (root / "audit").resolve()
        publish_failure_reference_audit(workspace, audit)
        document = json.loads((audit / "audit.json").read_text())
        return audit, document["groups"][0]

    def swapped_read(self, path, replacement):
        original = path.read_bytes()
        read_text = Path.read_text
        reads = 0

        def read(candidate, *args, **kwargs):
            nonlocal reads
            if candidate != path:
                return read_text(candidate, *args, **kwargs)
            reads += 1
            if reads != 2:
                return read_text(candidate, *args, **kwargs)
            path.write_text(json.dumps(replacement))
            try:
                return read_text(candidate, *args, **kwargs)
            finally:
                path.write_bytes(original)

        return patch.object(Path, "read_text", read)

    def test_preview_rejects_text_from_reopened_unvalidated_audit(self):
        with TemporaryDirectory() as directory:
            audit, group = self.create_audit(Path(directory))
            path = audit / "audit.json"
            altered = json.loads(path.read_text())
            altered["groups"][0]["cases"][0]["text"] = "This line was never audited."
            factory = _PreviewBackendFactory()
            service = FailureReferencePreviewService(audit, backend_factory=factory)
            try:
                with self.swapped_read(path, altered):
                    with self.assertRaises(FailureReferencePreviewError):
                        service.generate(
                            group["group_id"],
                            group["candidates"][0]["candidate_id"],
                            "This line was never audited.",
                        )
                self.assertEqual(factory.backends, [])
            finally:
                service.close()

    def test_audio_preparation_uses_the_validated_candidate_document(self):
        with TemporaryDirectory() as directory:
            audit, group = self.create_audit(Path(directory))
            candidate = group["candidates"][0]
            path = audit / "audit.json"
            altered = json.loads(path.read_text())
            original = (audit / candidate["audio"]).read_bytes()
            alternate = original + b"not an audited candidate"
            (audit / "alternate.wav").write_bytes(alternate)
            changed = altered["groups"][0]["candidates"][0]
            changed["audio"] = "alternate.wav"
            changed["sha256"] = hashlib.sha256(alternate).hexdigest()
            with self.swapped_read(path, altered):
                audio = prepare_failure_reference_audio(
                    audit, group["group_id"], candidate["candidate_id"]
                )
            self.assertEqual(audio.payload, original)
            self.assertEqual(audio.sha256, candidate["sha256"])
            self.assertEqual(audio.path, audit / candidate["audio"])

    def test_preview_rechecks_audit_after_final_source_capture(self):
        with TemporaryDirectory() as directory:
            audit, group = self.create_audit(Path(directory))
            candidate = group["candidates"][0]
            audio = audit / candidate["audio"]
            path = audit / "audit.json"
            original = path.read_bytes()
            changed = json.loads(original)
            changed["groups"][0]["cases"][0]["text"] = (
                "Changed after the final media read."
            )
            rendered = False
            read_bytes = Path.read_bytes

            def on_render(_backend, _request):
                nonlocal rendered
                rendered = True

            def change_after_capture(candidate_path):
                payload = read_bytes(candidate_path)
                if candidate_path == audio and rendered:
                    path.write_text(json.dumps(changed))
                return payload

            factory = _PreviewBackendFactory(on_render=on_render)
            service = FailureReferencePreviewService(audit, backend_factory=factory)
            try:
                with patch.object(Path, "read_bytes", change_after_capture):
                    with self.assertRaisesRegex(
                        FailureReferenceAuditError, "identity changed"
                    ):
                        service.generate(
                            group["group_id"],
                            candidate["candidate_id"],
                            group["cases"][0]["text"],
                        )
                self.assertEqual(len(factory.backends[0].requests), 1)
                path.write_bytes(original)
                self.assertEqual(
                    service.generate(
                        group["group_id"],
                        candidate["candidate_id"],
                        group["cases"][0]["text"],
                    ).text,
                    group["cases"][0]["text"],
                )
                self.assertEqual(len(factory.backends[0].requests), 2)
            finally:
                path.write_bytes(original)
                service.close()
