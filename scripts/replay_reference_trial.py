"""Replay precomputed research selections through the real fresh MOSS planner.

python -m scripts.replay_reference_trial STORY_INDEX MANIFEST REPORT [REPORT ...]
    --output-root DIRECTORY [--story STORY_ID ...]

Compare each trial shortlist with the current application selector at the preview
qualification boundary. The application selector may load its encoder. Neither
arm synthesizes audio. Jobs and plans stay under the requested directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.cli import cli_error, print_json
from vntts.document_identity import is_sha256, sha256_file
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import ReferenceTrial, VoicePlan, VoicePlanStore
from vntts.settings import AppSettings
from vntts.voices import normalize_character_name


def replay(
    story_index: Path,
    manifest: Path,
    reports: list[dict[str, object]],
    output_root: Path,
    stories: list[str] | None = None,
) -> dict[str, object]:
    checksum = sha256_file(manifest)
    selections = {}
    typical_selections = {}
    characters = set()
    pending = []
    for report in reports:
        character = report.get("character")
        if (
            report.get("manifest_sha256") != checksum
            or not isinstance(character, str)
            or not character.strip()
        ):
            raise ValueError("Reference trial report does not match the voice catalog")
        normalized = normalize_character_name(character)
        if normalized in characters:
            raise ValueError(f"Duplicate reference trial character: {character}")
        characters.add(normalized)
        selected = report.get("selected", report.get("representative"))
        if selected is None:
            pending.append(character)
            continue
        if not isinstance(selected, dict) or not is_sha256(selected.get("sha256")):
            raise ValueError("Reference trial selection has no valid audio checksum")
        selections[character] = selected["sha256"]
        typical = report.get("representative")
        if typical is not None:
            if not isinstance(typical, dict) or not is_sha256(typical.get("sha256")):
                raise ValueError("Reference trial typical has no valid audio checksum")
            typical_selections[character] = typical["sha256"]
    content = inspect_story_index(story_index)
    jobs = PregenerationJobStore(output_root)
    job = jobs.create_or_resume(
        content, stories or [selection.selection_id for selection in content.selections]
    )
    store = VoicePlanStore(jobs)
    settings = AppSettings(speech_backend="moss-tts")

    def capture(plan: VoicePlan) -> dict[str, object]:
        return {
            group.group_id: {
                "character": group.character,
                "route": group.route,
                "resolution": group.resolution,
                "reference_sha256s": list(group.reference_sha256s),
                "preview_shortlist": [
                    candidate.to_document() for candidate in group.candidates
                ],
            }
            for group in plan.groups
            if normalize_character_name(group.character) in characters
        }

    def preview_entry(_plan: VoicePlan) -> dict[str, str]:
        # Replay stops exactly where the UI starts bounded preview qualification.
        return {}

    baseline = store.create(
        job, settings, manifest_path=manifest, reference_qualifier=preview_entry
    )
    trial = store.create(
        job,
        settings,
        manifest_path=manifest,
        reference_qualifier=preview_entry,
        reference_trial=ReferenceTrial(checksum, selections, typical_selections),
    )
    return {
        "mode": "preview-shortlist-replay",
        "manifest_sha256": checksum,
        "pending_characters": pending,
        "baseline": capture(baseline),
        "trial": capture(trial),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("story_index", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--story", action="append")
    args = parser.parse_args()
    try:
        reports = [json.loads(path.read_text()) for path in args.reports]
        if not all(isinstance(report, dict) for report in reports):
            raise ValueError("Reference trial reports must be JSON objects")
        result = replay(
            args.story_index, args.manifest, reports, args.output_root, args.story
        )
    except (OSError, ValueError, RuntimeError) as error:
        return cli_error(error)
    print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
