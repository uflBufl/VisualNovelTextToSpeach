import unittest

from vntts.authoring.failure_reference_binding_records import (
    FailureReferenceBindingError,
)
from vntts.authoring.failure_reference_binding_records import (
    _safe_relative as binding_relative,
)
from vntts.authoring.game_pack import (
    FinalGamePackError,
)
from vntts.authoring.game_pack import (
    _safe_relative as authoring_pack_relative,
)
from vntts.authoring.legacy_import import (
    LegacyAuthoringImportError,
)
from vntts.authoring.legacy_import import (
    _safe_relative as legacy_relative,
)
from vntts.authoring.listening_import import (
    ListeningImportError,
)
from vntts.authoring.listening_import import (
    _safe_relative as listening_relative,
)
from vntts.pregeneration_pack import OfflinePackError
from vntts.pregeneration_pack import _safe_relative as offline_pack_relative


class RelativePathContractTest(unittest.TestCase):
    def test_pack_and_import_paths_reject_collapsed_aliases(self):
        validators = (
            (offline_pack_relative, OfflinePackError),
            (authoring_pack_relative, FinalGamePackError),
            (binding_relative, FailureReferenceBindingError),
            (listening_relative, ListeningImportError),
            (legacy_relative, LegacyAuthoringImportError),
        )
        for validate, error_type in validators:
            for value in ("audio//voice.wav", "audio/./voice.wav"):
                with self.subTest(validate=validate.__module__, value=value):
                    with self.assertRaises(error_type):
                        validate(value, "Audio")
            self.assertEqual(
                validate("audio/voice.wav", "Audio").as_posix(), "audio/voice.wav"
            )


if __name__ == "__main__":
    unittest.main()
