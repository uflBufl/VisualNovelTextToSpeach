import unittest

from vntts_artifacts.story_index import StoryIndexRecord

from vntts.person_link_suggestions import suggest_person_links


def record(
    role: str,
    portrait: str | int | None,
    source_bank: str | None,
    source_voice_id: str | None,
    *,
    speaker: str | None = None,
) -> StoryIndexRecord:
    return StoryIndexRecord(
        line_id=f"line:{role}:{portrait}:{source_bank}:{source_voice_id}",
        chapter="1",
        sequence=1,
        speaker=speaker or role,
        voice_character=role,
        text="Line.",
        kind="dialogue",
        text_sha256="a" * 64,
        source_audio_id=source_voice_id,
        producer_fields={
            key: value
            for key, value in {
                "portrait": portrait,
                "source_bank": source_bank,
            }.items()
            if value is not None
        },
    )


def matched_roles(left: str, right: str) -> list[StoryIndexRecord]:
    return [
        record(left, portrait, "shared.bnk", f"{left}-{portrait}")
        for portrait in ("portrait-1", "portrait-2")
    ] + [
        record(right, portrait, "shared.bnk", f"{right}-{portrait}")
        for portrait in ("portrait-1", "portrait-2")
    ]


class PersonLinkSuggestionsTests(unittest.TestCase):
    def test_suggests_aderyn_and_rhiannon_with_concrete_evidence(self):
        suggestions = suggest_person_links(matched_roles("Aderyn", "Rhiannon"))

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(
            suggestions[0].to_document(),
            {
                "left_role": "Aderyn",
                "right_role": "Rhiannon",
                "shared_portraits": ["portrait-1", "portrait-2"],
                "shared_source_banks": ["shared.bnk"],
            },
        )

    def test_rejects_shared_bank_without_two_shared_portraits(self):
        records = [
            record("Aderyn", "portrait-1", "shared.bnk", "a-1"),
            record("Aderyn", "portrait-2", "shared.bnk", "a-2"),
            record("Rhiannon", "portrait-3", "shared.bnk", "r-1"),
            record("Rhiannon", "portrait-4", "shared.bnk", "r-2"),
        ]
        self.assertEqual(suggest_person_links(records), ())

    def test_rejects_a_single_shared_portrait(self):
        records = [
            record("Aderyn", "portrait-1", "shared.bnk", "a-1"),
            record("Aderyn", "portrait-2", "shared.bnk", "a-2"),
            record("Rhiannon", "portrait-1", "shared.bnk", "r-1"),
            record("Rhiannon", "portrait-3", "shared.bnk", "r-3"),
        ]
        self.assertEqual(suggest_person_links(records), ())

    def test_rejects_unvoiced_rows_even_with_matching_portraits_and_bank(self):
        records = [
            record(role, portrait, "shared.bnk", None)
            for role in ("Aderyn", "Rhiannon")
            for portrait in ("portrait-1", "portrait-2")
        ]
        self.assertEqual(suggest_person_links(records), ())

    def test_rejects_reused_voice_ids_without_portrait_matches(self):
        records = [
            record("Aderyn", "portrait-1", "shared.bnk", "voice-1"),
            record("Aderyn", "portrait-2", "shared.bnk", "voice-2"),
            record("Rhiannon", "portrait-3", "shared.bnk", "voice-1"),
            record("Rhiannon", "portrait-4", "shared.bnk", "voice-2"),
        ]
        self.assertEqual(suggest_person_links(records), ())

    def test_excludes_existing_aliases_narrator_unknown_and_normalized_duplicates(self):
        records = matched_roles("Aderyn", "Rhiannon") + matched_roles(
            "Narrator", "Aderyn"
        )
        records.extend(
            record(
                "Aderyn", portrait, "shared.bnk", f"unknown-{portrait}", speaker="???"
            )
            for portrait in ("portrait-1", "portrait-2")
        )
        records.extend(
            record('"aderyn"', portrait, "shared.bnk", f"duplicate-{portrait}")
            for portrait in ("portrait-1", "portrait-2")
        )

        self.assertEqual(suggest_person_links(records, {"aderyn": "Rhiannon"}), ())

    def test_does_not_suggest_two_names_already_linked_to_one_person(self):
        self.assertEqual(
            suggest_person_links(
                matched_roles("Aderyn", "Gwyndolyn"),
                {"Aderyn": "Rhiannon", "Gwyndolyn": "Rhiannon"},
            ),
            (),
        )

    def test_reports_every_ambiguous_target_for_the_caller_to_choose(self):
        suggestions = suggest_person_links(
            [
                record("Aderyn", portrait, "rhiannon.bnk", f"a-r-{portrait}")
                for portrait in ("portrait-1", "portrait-2")
            ]
            + [
                record("Aderyn", portrait, "gwyndolyn.bnk", f"a-g-{portrait}")
                for portrait in ("portrait-1", "portrait-2")
            ]
            + [
                record("Rhiannon", portrait, "rhiannon.bnk", f"r-{portrait}")
                for portrait in ("portrait-1", "portrait-2")
            ]
            + [
                record("Gwyndolyn", portrait, "gwyndolyn.bnk", f"g-{portrait}")
                for portrait in ("portrait-1", "portrait-2")
            ]
        )

        self.assertEqual(
            [(value.left_role, value.right_role) for value in suggestions],
            [("Aderyn", "Gwyndolyn"), ("Aderyn", "Rhiannon")],
        )


if __name__ == "__main__":
    unittest.main()
