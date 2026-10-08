import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
