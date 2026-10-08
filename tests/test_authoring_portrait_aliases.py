import itertools
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.source_reference_fixtures import (
    publish_source_reference_quality_fixture,
    write_test_png,
)
from vntts.authoring import portrait_aliases as aliases
from vntts.authoring.source_reference_quality import (
    load_source_reference_quality_review,
    record_source_reference_quality_decision,
)


class PortraitAliasesTest(unittest.TestCase):
    def test_optional_source_portrait_has_typed_alias_round_trip(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            portraits = root / "portraits"
            portraits.mkdir()
            for name in ("adult", "young"):
                write_test_png(portraits / f"{name}.png", red=120)
            _plan, _evaluation, _generation, quality = (
                publish_source_reference_quality_fixture(
                    root, portrait_directory=portraits, shared_portrait_bank=True
                )
            )
            for card in load_source_reference_quality_review(quality.session)[
                "variants"
            ]:
                record_source_reference_quality_decision(
                    quality.session, card["variant_id"], "accept"
                )
            original = json.loads(quality.session.read_text())
            for missing in (False, True):
                with self.subTest(missing=missing):
                    changed = json.loads(json.dumps(original))
                    changed["variants"][0]["portrait"] = None
                    if missing:
                        changed["variants"][0].pop("portrait")
                    quality.session.write_text(json.dumps(changed))
                    load_source_reference_quality_review(quality.session)
                    plan = aliases.build_portrait_alias_plan(quality.session)
                    suggestion = plan.document["suggestions"][0]
                    self.assertIn(
                        None, [value["portrait"] for value in suggestion["variants"]]
                    )
                    path = aliases.write_portrait_alias_plan(
                        plan, root / f"plan-{missing}.json"
                    )
                    loaded = aliases.load_portrait_alias_plan(path)
                    self.assertEqual(loaded.document, plan.document)
                    decision = aliases.build_portrait_alias_decision(
                        loaded, [suggestion["suggestion_id"]]
                    )
                    path = aliases.write_portrait_alias_decision(
                        decision, root / f"decision-{missing}.json"
                    )
                    self.assertEqual(
                        aliases.load_portrait_alias_decision(path, loaded).document,
                        decision.document,
                    )

    def test_planner_does_not_bind_other_review_decisions_to_captured_hash(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            portraits = root / "portraits"
            portraits.mkdir()
            for name in ("adult", "young"):
                write_test_png(portraits / f"{name}.png", red=120)
            _plan, _evaluation, _generation, quality = (
                publish_source_reference_quality_fixture(
                    root, portrait_directory=portraits, shared_portrait_bank=True
                )
            )
            rejected = quality.session.read_bytes()
            for card in load_source_reference_quality_review(quality.session)[
                "variants"
            ]:
                record_source_reference_quality_decision(
                    quality.session, card["variant_id"], "accept"
                )
            accepted = quality.session.read_bytes()
            quality.session.write_bytes(rejected)
            read_bytes = Path.read_bytes
            reads = 0

            def read_other_review(path):
                nonlocal reads
                if path != quality.session:
                    return read_bytes(path)
                reads += 1
                if reads != 2:
                    return read_bytes(path)
                path.write_bytes(accepted)
                try:
                    return read_bytes(path)
                finally:
                    path.write_bytes(rejected)

            with patch.object(Path, "read_bytes", read_other_review):
                with self.assertRaisesRegex(
                    aliases.PortraitAliasError, "changed while"
                ):
                    aliases.build_portrait_alias_plan(quality.session)
            self.assertEqual(quality.session.read_bytes(), rejected)

    def test_pair_search_only_visits_same_voice_families(self):
        variants = []
        for family in range(20):
            for member in range(2):
                variants.append(
                    {
                        "variant_id": f"{family}-{member}",
                        "character": f"Hero-{family}",
                        "portrait": f"{member}.png",
                        "source_bank": f"{family}.bnk",
                        "portrait_image_sha256": "a" * 64,
                        "dhash": "0" * 16,
                    }
                )
        pairs = []

        def counted_pairs(values, size):
            for pair in itertools.combinations(values, size):
                pairs.append(pair)
                yield pair

        with patch.object(aliases, "combinations", side_effect=counted_pairs):
            suggestions = aliases._portrait_alias_suggestions(variants, 6)
        self.assertEqual(len(pairs), 20)
        self.assertEqual(len(suggestions), 20)
        self.assertEqual(
            [value["suggestion_id"] for value in suggestions],
            sorted(value["suggestion_id"] for value in suggestions),
        )
        self.assertTrue(
            all(
                first["source_bank"] == second["source_bank"] for first, second in pairs
            )
        )


if __name__ == "__main__":
    unittest.main()
