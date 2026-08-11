import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from vntts.reverse1999_catalog import Reverse1999NpcCatalog
from vntts.reverse1999_config import (
    Reverse1999ConfigError,
    config_header_size,
    config_iv,
    config_key,
    decrypt_config_data,
    extract_dialogue_evidence,
    find_game_config_directory,
    load_config_directory,
    parse_data_document,
    parse_language_document,
)


def encrypt_config(document):
    plaintext = json.dumps(document).encode()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(config_key), modes.CBC(config_iv)).encryptor()
    return b"header".ljust(config_header_size, b"-") + (
        encryptor.update(padded) + encryptor.finalize()
    )


class Reverse1999ConfigTest(unittest.TestCase):
    def test_decrypts_config_after_authenticated_header(self):
        document = {"hello": "world"}
        encrypted = encrypt_config(document)

        decrypted = json.loads(decrypt_config_data(encrypted))

        self.assertEqual(decrypted, document)

    def test_rejects_truncated_and_misaligned_configs(self):
        with self.assertRaisesRegex(Reverse1999ConfigError, "missing its payload"):
            decrypt_config_data(b"short")
        with self.assertRaisesRegex(Reverse1999ConfigError, "not aligned"):
            decrypt_config_data(b"x" * (config_header_size + 1))

    def test_parses_language_and_nested_data_tables(self):
        language = parse_language_document(
            ["language_en", [["name", "Fatutu"], ["line", "Hello"]]]
        )
        tables = parse_data_document(
            {"json_tip_dialog": json.dumps(["json_tip_dialog", [[1, 2]]])}
        )

        self.assertEqual(language, {"name": "Fatutu", "line": "Hello"})
        self.assertEqual(tables, {"json_tip_dialog": [[1, 2]]})

    def test_loads_split_encrypted_config_directory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "language").mkdir()
            (root / "language" / "json_language_en.json.dat").write_bytes(
                encrypt_config(["language_en", [["line", "Hello"]]])
            )
            (root / "datacfg_1.dat").write_bytes(
                encrypt_config(
                    {"json_tip_dialog": json.dumps(["json_tip_dialog", []])}
                )
            )

            language, tables = load_config_directory(root)

        self.assertEqual(language, {"line": "Hello"})
        self.assertEqual(tables, {"json_tip_dialog": []})

    def test_extracts_chapter_dialogue_and_resolves_character_and_npc_ids(self):
        catalog = Reverse1999NpcCatalog.from_dict(
            {
                "version": 1,
                "game": "Reverse: 1999",
                "npcs": [
                    {
                        "id": "520301",
                        "display_name": "Kamuta",
                        "aliases": [],
                        "language": "en",
                        "game_versions": ["3.6.5"],
                        "banks": ["kamuta.bnk"],
                    }
                ],
            }
        )
        language = {
            "fatutu_name": "Fatutu",
            "fatutu_line": "Take this.",
            "kamuta_line": "Paddle out.",
            "unknown_line": "Selone!",
        }
        tables = {
            "json_character": [
                [3109, "fatutu_name", *([""] * 22), "Fatutu"],
            ],
            "json_tip_dialog": [
                [24006, 3, "talk", "300#236", "310918", "fatutu_line", 0],
                [24007, 3, "talk", "300#236", "520301", "kamuta_line", 0],
                [24008, 1, "talk", "300#236", "999999", "unknown_line", 0],
            ],
            "json_guide_step": [
                [24401, 6, "talk", 0, 0, "235#236", "520301", 0, "", "kamuta_line"]
            ],
        }

        identities, evidence = extract_dialogue_evidence(
            language, tables, catalog=catalog
        )

        self.assertEqual(identities["3109"].display_name, "Fatutu")
        self.assertEqual(
            [(item.speaker_id, item.speaker_name) for item in evidence],
            [
                ("310918", "Fatutu"),
                ("520301", "Kamuta"),
                ("999999", None),
                ("520301", "Kamuta"),
            ],
        )
        self.assertEqual(evidence[0].chapter, "24006")

    def test_finds_macos_config_directory(self):
        with TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            root = (
                home
                / "Library"
                / "Containers"
                / "game"
                / "Data"
                / "Documents"
                / "ResLib"
                / "iOS"
                / "configs"
            )
            (root / "language").mkdir(parents=True)
            (root / "datacfg_1.dat").touch()
            (root / "language" / "json_language_en.json.dat").touch()

            found = find_game_config_directory(home=home, environment={})

        self.assertEqual(found, root.resolve())


if __name__ == "__main__":
    unittest.main()
