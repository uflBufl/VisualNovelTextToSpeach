import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.story_fixtures import write_synthetic_game_pack
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
    validate_source_audio_semantic_evidence,
)


class SourceAudioSemanticEvidenceTest(unittest.TestCase):
    def test_invalid_extra_values_use_domain_error_for_objects_and_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_game_pack(root, include_semantics=True)
            path = root / "source-audio-semantic-evidence.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            for location in ("document", "entry"):
                for value in (math.nan, math.inf, -math.inf, {"not JSON"}):
                    with self.subTest(location=location, value=value):
                        document = json.loads(json.dumps(original))
                        target = (
                            document
                            if location == "document"
                            else document["entries"][0]
                        )
                        target["unexpected"] = value
                        with self.subTest(boundary="object"):
                            with self.assertRaises(SourceAudioSemanticEvidenceError):
                                validate_source_audio_semantic_evidence(document)
                        if isinstance(value, float):
                            path.write_text(json.dumps(document), encoding="utf-8")
                            with self.subTest(boundary="file"):
                                with self.assertRaises(
                                    SourceAudioSemanticEvidenceError
                                ):
                                    load_source_audio_semantic_evidence(path)

    def test_valid_extra_values_remain_lossless(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_game_pack(root, include_semantics=True)
            path = root / "source-audio-semantic-evidence.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            entry = document["entries"][0]
            entry["extra"] = {"duration": 2.5, "labels": [None, True, "valid"]}
            entry["entry_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in entry.items()
                    if key not in {"entry_id", "source_line_ids"}
                }
            )
            document["extra"] = [1, 2.5, False, None]
            document["evidence_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in document.items()
                    if key not in {"evidence_id", "generated_at"}
                }
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(load_source_audio_semantic_evidence(path), document)
