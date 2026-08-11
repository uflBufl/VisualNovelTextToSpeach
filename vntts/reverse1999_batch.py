import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_audition import default_mapping_path
from vntts.reverse1999_catalog import Reverse1999NpcCatalog, default_catalog_path
from vntts.reverse1999_config import (
    build_dialogue_index,
    find_game_config_directory,
    write_dialogue_index,
)
from vntts.reverse1999_config import default_output as default_dialogue_index
from vntts.reverse1999_index import build_bank_index
from vntts.reverse1999_index import default_output as default_bank_index
from vntts.reverse1999_voice_import import (
    ImportedReference,
    find_game_audio_directory,
    update_manifest,
)
from vntts.reverse1999_voice_import import default_output as default_voice_output
from vntts.settings import get_local_data_directory
from vntts.voice_reference_quality import (
    analyze_voice_reference,
    default_review_path,
    select_reference_set,
    trim_and_normalize_voice_reference,
)
from vntts.wwise import convert_audio, read_embedded_media, resolve_decoder

state_version = 1
default_state_path = get_local_data_directory() / "reverse1999" / "batch-state.json"
default_batch_cache = get_local_data_directory() / "reverse1999" / "batch"


class Reverse1999BatchError(RuntimeError):
    pass


def new_state():
    return {
        "version": state_version,
        "game_audio_directory": None,
        "dialogue_index": str(default_dialogue_index),
        "bank_index": str(default_bank_index),
        "mappings": [],
        "unresolved_npc_ids": [],
        "clips": [],
        "imports": [],
        "errors": [],
    }


def load_state(path=default_state_path):
    path = Path(path).expanduser().resolve()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return new_state()
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999BatchError(f"Unable to read batch state: {error}") from error
    if not isinstance(state, dict) or state.get("version") != state_version:
        raise Reverse1999BatchError("Batch state has an unsupported format")
    return state


def save_state(state, path=default_state_path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _load_json(path, label, *, missing=None):
    try:
        return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing is not None:
            return missing
        raise Reverse1999BatchError(f"{label} does not exist: {path}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999BatchError(f"Unable to read {label}: {error}") from error


def scan_installed_game(
    state,
    *,
    bank_index_path=default_bank_index,
    dialogue_index_path=default_dialogue_index,
    game_audio_directory=None,
    game_config_directory=None,
):
    audio_root = game_audio_directory or find_game_audio_directory()
    config_root = game_config_directory or find_game_config_directory()
    if audio_root is None or config_root is None:
        raise Reverse1999BatchError(
            "Unable to find installed game audio and configs; pass both directories"
        )
    bank_index, bank_index_path = build_bank_index(
        audio_root, output=bank_index_path
    )
    dialogue_index = build_dialogue_index(config_root)
    dialogue_index_path = write_dialogue_index(dialogue_index, dialogue_index_path)
    state["game_audio_directory"] = str(Path(audio_root).resolve())
    state["bank_index"] = str(bank_index_path)
    state["dialogue_index"] = str(dialogue_index_path)
    state["scan"] = {
        "banks": bank_index["bank_count"],
        "bank_errors": bank_index["error_count"],
        "dialogue_rows": len(dialogue_index["dialogue"]),
    }
    return state


def map_speakers(
    state,
    *,
    catalog_path=default_catalog_path,
    mapping_path=default_mapping_path,
):
    bank_index = _load_json(state["bank_index"], "Bank index")
    catalog = Reverse1999NpcCatalog.load(catalog_path)
    mappings = {
        npc.npc_id: {
            "speaker_name": npc.display_name,
            "npc_id": npc.npc_id,
            "banks": list(npc.banks),
            "chapter": "",
            "source": "catalog",
        }
        for npc in catalog.npcs
    }
    local = _load_json(mapping_path, "Local speaker mappings", missing={"mappings": []})
    for item in local.get("mappings", []):
        npc_id = str(item.get("npc_id", "")).strip()
        display_name = str(item.get("display_name", "")).strip()
        bank = str(item.get("bank", "")).strip()
        if not npc_id or not display_name or not bank:
            continue
        mappings[npc_id] = {
            "speaker_name": display_name,
            "npc_id": npc_id,
            "banks": [bank],
            "chapter": str(item.get("chapter", "")),
            "source": "assisted",
        }
    installed_ids = set(bank_index.get("npc_banks", {}))
    state["mappings"] = sorted(
        mappings.values(), key=lambda item: item["speaker_name"].casefold()
    )
    state["unresolved_npc_ids"] = sorted(installed_ids - set(mappings))
    return state


def _bank_entries(bank_index):
    entries = {}
    for entry in bank_index.get("banks", []):
        if not isinstance(entry, dict):
            continue
        for key in (entry.get("filename"), entry.get("path")):
            if key:
                entries[str(key).casefold()] = entry
    return entries


def extract_mapped_clips(
    state,
    *,
    decoder="vgmstream-cli",
    cache_directory=default_batch_cache,
    checkpoint=None,
):
    bank_index = _load_json(state["bank_index"], "Bank index")
    root = Path(bank_index["game_audio_directory"])
    entries = _bank_entries(bank_index)
    decoder = resolve_decoder(decoder)
    cache_directory = Path(cache_directory).expanduser().resolve()
    existing = {
        (clip["bank"], clip["media_id"]): clip for clip in state.get("clips", [])
    }
    checkpoint = checkpoint or (lambda: None)
    for mapping in state.get("mappings", []):
        for configured_bank in mapping["banks"]:
            entry = entries.get(configured_bank.casefold())
            if entry is None:
                continue
            bank = root / entry["path"]
            try:
                media = read_embedded_media(bank)
                with TemporaryDirectory(prefix="vntts-batch-") as temporary_directory:
                    temporary_directory = Path(temporary_directory)
                    for item in media:
                        key = (entry["filename"], item.media_id)
                        if key in existing and Path(existing[key]["wav"]).is_file():
                            continue
                        wem = temporary_directory / f"{item.media_id}.wem"
                        wav = (
                            cache_directory
                            / mapping["npc_id"]
                            / entry["filename"]
                            / f"{item.media_id}.wav"
                        )
                        wav.parent.mkdir(parents=True, exist_ok=True)
                        wem.write_bytes(item.data)
                        convert_audio(wem, wav, decoder=decoder, overwrite=True)
                        clip = {
                            "speaker_name": mapping["speaker_name"],
                            "npc_id": mapping["npc_id"],
                            "chapter": mapping["chapter"],
                            "bank": entry["filename"],
                            "media_id": item.media_id,
                            "source_sha256": hashlib.sha256(item.data).hexdigest(),
                            "wav": str(wav),
                            "status": "extracted",
                        }
                        existing[key] = clip
                        state["clips"] = list(existing.values())
            except Exception as error:
                state["errors"].append(
                    {"stage": "extract", "bank": str(bank), "error": str(error)}
                )
            checkpoint()
    state["clips"] = sorted(
        existing.values(), key=lambda clip: (clip["bank"], clip["media_id"])
    )
    return state


def score_extracted_clips(state, *, checkpoint=None):
    checkpoint = checkpoint or (lambda: None)
    for clip in state.get("clips", []):
        if clip.get("status") not in {"extracted", "score-error"}:
            continue
        try:
            clip["metrics"] = asdict(analyze_voice_reference(clip["wav"]))
            clip["status"] = "scored"
            clip.pop("error", None)
        except Exception as error:
            clip["status"] = "score-error"
            clip["error"] = str(error)
        checkpoint()
    return state


def merge_clip_reviews(state, *, review_path=default_review_path):
    document = _load_json(review_path, "Clip reviews", missing={"clips": []})
    reviews = {
        (item.get("bank"), item.get("media_id")): item
        for item in document.get("clips", [])
    }
    for clip in state.get("clips", []):
        review = reviews.get((clip["bank"], clip["media_id"]))
        if review is None:
            continue
        clip["review"] = review
        clip["status"] = "approved" if review.get("approved") else "rejected"
    return state


def import_approved_references(
    state,
    *,
    output_directory=default_voice_output,
):
    output_directory = Path(output_directory).expanduser().resolve()
    by_speaker = defaultdict(list)
    for clip in state.get("clips", []):
        if clip.get("status") == "approved":
            by_speaker[clip["speaker_name"]].append(clip)
    imports = []
    for speaker_name, clips in by_speaker.items():
        selection_input = []
        for clip in clips:
            review = dict(clip["review"])
            review["speaker_name"] = speaker_name
            selection_input.append(review)
        selected_reviews = select_reference_set(selection_input, speaker_name)
        selected_keys = {
            (item["bank"], item["media_id"]) for item in selected_reviews
        }
        selected = [
            clip
            for clip in clips
            if (clip["bank"], clip["media_id"]) in selected_keys
        ]
        if not selected:
            continue
        imported = []
        for index, clip in enumerate(selected, start=1):
            destination = (
                output_directory
                / "references"
                / f"{clip['npc_id']}-{index:02d}.wav"
            )
            trim_and_normalize_voice_reference(clip["wav"], destination)
            imported.append(
                ImportedReference(
                    path=destination,
                    media_id=clip["media_id"],
                    source_sha256=clip["source_sha256"],
                    reference_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                    bank=clip["bank"],
                )
            )
        bank_names = sorted({clip["bank"] for clip in selected})
        manifest = update_manifest(
            output_directory,
            speaker_name,
            imported,
            Path(bank_names[0]),
        )
        imports.append(
            {
                "speaker_name": speaker_name,
                "npc_id": selected[0]["npc_id"],
                "references": len(imported),
                "manifest": str(manifest),
                "banks": bank_names,
            }
        )
        for clip in selected:
            clip["status"] = "imported"
    state["imports"] = imports
    return state


def stage_counts(state):
    statuses = defaultdict(int)
    for clip in state.get("clips", []):
        statuses[clip.get("status", "unknown")] += 1
    return {
        "mapped": len(state.get("mappings", [])),
        "unresolved": len(state.get("unresolved_npc_ids", [])),
        "extracted": len(state.get("clips", [])),
        "scored": sum(
            statuses[value]
            for value in ("scored", "approved", "rejected", "imported")
        ),
        "pending_review": statuses["scored"],
        "rejected": statuses["rejected"],
        "approved": statuses["approved"],
        "imported": statuses["imported"],
        "errors": len(state.get("errors", [])) + statuses["score-error"],
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Resume Reverse: 1999 NPC voice scan, mapping, extraction, scoring, "
            "review, and import."
        )
    )
    parser.add_argument(
        "stage", choices=("scan", "map", "extract", "score", "review", "import", "run")
    )
    parser.add_argument("--state", type=Path, default=default_state_path)
    parser.add_argument("--game-audio-directory", type=Path)
    parser.add_argument("--game-config-directory", type=Path)
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument("--catalog", type=Path, default=default_catalog_path)
    parser.add_argument("--mappings", type=Path, default=default_mapping_path)
    parser.add_argument("--reviews", type=Path, default=default_review_path)
    parser.add_argument("--output", type=Path, default=default_voice_output)
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        state = load_state(arguments.state)

        def checkpoint():
            save_state(state, arguments.state)

        stages = (
            ("scan", lambda: scan_installed_game(
                state,
                game_audio_directory=arguments.game_audio_directory,
                game_config_directory=arguments.game_config_directory,
            )),
            ("map", lambda: map_speakers(
                state, catalog_path=arguments.catalog, mapping_path=arguments.mappings
            )),
            ("extract", lambda: extract_mapped_clips(
                state, decoder=arguments.decoder, checkpoint=checkpoint
            )),
            ("score", lambda: score_extracted_clips(state, checkpoint=checkpoint)),
            ("review", lambda: merge_clip_reviews(state, review_path=arguments.reviews)),
            ("import", lambda: import_approved_references(
                state, output_directory=arguments.output
            )),
        )
        for name, action in stages:
            if arguments.stage in {name, "run"}:
                action()
                checkpoint()
                if arguments.stage == "run" and name == "review" and stage_counts(
                    state
                )["pending_review"]:
                    break
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    counts = stage_counts(state)
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"State: {Path(arguments.state).expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
