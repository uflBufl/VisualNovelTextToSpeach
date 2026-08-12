import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_aliases import canonical_voice_name
from vntts.reverse1999_audition import default_mapping_path
from vntts.reverse1999_catalog import (
    Reverse1999CatalogError,
    Reverse1999NpcCatalog,
    default_catalog_path,
    normalize_name,
    project_root,
)
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
default_auto_review_path = (
    get_local_data_directory() / "reverse1999" / "auto-review-queue.json"
)


class Reverse1999BatchError(RuntimeError):
    pass


def new_state():
    return {
        "version": state_version,
        "game_audio_directory": None,
        "dialogue_index": str(default_dialogue_index),
        "bank_index": str(default_bank_index),
        "mappings": [],
        "mapping_review_queue": [],
        "alias_resolutions": [],
        "unidentified_npc_ids": [],
        "unresolved_npc_ids": [],
        "clips": [],
        "auto_selections": [],
        "imports": [],
        "catalog_updates": [],
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
    bank_index, bank_index_path = build_bank_index(audio_root, output=bank_index_path)
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


def _speaker_evidence(dialogue_index):
    names_by_id = defaultdict(Counter)
    chapters_by_id = defaultdict(Counter)
    ids_by_name = defaultdict(set)
    for row in dialogue_index.get("dialogue", []):
        if not isinstance(row, dict):
            continue
        npc_id = str(row.get("speaker_id", "")).strip()
        display_name = str(row.get("speaker_name") or "").strip()
        if not npc_id or not display_name:
            continue
        normalized = normalize_name(display_name)
        if not normalized:
            continue
        names_by_id[npc_id][display_name] += 1
        chapters_by_id[npc_id][str(row.get("chapter", ""))] += 1
        ids_by_name[normalized].add(npc_id)
    return names_by_id, chapters_by_id, ids_by_name


def _bank_media_count(entry):
    return len(
        {
            media_id
            for event in entry.get("events", [])
            if isinstance(event, dict)
            for media_id in event.get("media_ids", [])
            if isinstance(media_id, int)
        }
    )


def _rank_auto_banks(bank_index, npc_id, *, maximum_banks=2):
    entries = _bank_entries(bank_index)
    ranked = []
    for bank in bank_index.get("npc_banks", {}).get(npc_id, []):
        entry = entries.get(str(bank).casefold())
        if entry is None or entry.get("error"):
            continue
        bank_npc_ids = {str(value) for value in entry.get("npc_ids", [])}
        if bank_npc_ids != {npc_id}:
            continue
        media_count = _bank_media_count(entry)
        if media_count < 3:
            continue
        folded = str(entry.get("filename", bank)).casefold()
        score = 0
        if "story" in folded:
            score += 40
        if "activityvoc" in folded:
            score += 30
        if "plotvoc" in folded:
            score += 10
        if 3 <= media_count <= 30:
            score += 20
        elif media_count <= 80:
            score += 5
        score -= max(0, media_count - 30) // 10
        ranked.append((score, -media_count, str(entry.get("filename", bank))))
    ranked.sort(reverse=True)
    return [item[2] for item in ranked[:maximum_banks]]


def discover_auto_mappings(
    state,
    *,
    catalog_path=default_catalog_path,
    minimum_dialogue_lines=2,
    maximum_banks=2,
):
    """Map only unambiguous, named NPC IDs and queue everything else."""
    if minimum_dialogue_lines <= 0:
        raise Reverse1999BatchError("Minimum dialogue lines must be positive")
    if maximum_banks <= 0:
        raise Reverse1999BatchError("Maximum auto banks must be positive")

    bank_index = _load_json(state["bank_index"], "Bank index")
    dialogue_index = _load_json(state["dialogue_index"], "Dialogue index")
    catalog = Reverse1999NpcCatalog.load(catalog_path)
    cataloged_ids = {npc.npc_id for npc in catalog.npcs}
    installed_ids = set(bank_index.get("npc_banks", {}))
    names_by_id, chapters_by_id, ids_by_name = _speaker_evidence(dialogue_index)
    mappings = []
    review_queue = []
    unidentified = []
    alias_resolutions = []

    for npc_id in sorted(installed_ids - cataloged_ids):
        names = names_by_id.get(npc_id, Counter())
        normalized_names = {normalize_name(name) for name in names}
        reasons = []
        if not names:
            unidentified.append(npc_id)
            continue
        else:
            display_name = names.most_common(1)[0][0]
            if len(normalized_names) != 1:
                reasons.append("conflicting-speaker-names")
            normalized = normalize_name(display_name)
            if len(ids_by_name.get(normalized, ())) != 1:
                reasons.append("speaker-name-shared-by-multiple-ids")
            if sum(names.values()) < minimum_dialogue_lines:
                reasons.append("insufficient-dialogue-evidence")

        canonical_name = canonical_voice_name(display_name)
        if (
            not reasons
            and canonical_name is not None
            and normalize_name(canonical_name) != normalize_name(display_name)
        ):
            alias_resolutions.append(
                {
                    "npc_id": npc_id,
                    "observed_name": display_name,
                    "canonical_name": canonical_name,
                    "dialogue_lines": sum(names.values()),
                }
            )
            continue

        banks = _rank_auto_banks(bank_index, npc_id, maximum_banks=maximum_banks)
        if not banks:
            reasons.append("no-bank-with-enough-clips")
        chapters = [chapter for chapter, _ in chapters_by_id[npc_id].most_common()]
        evidence = {
            "npc_id": npc_id,
            "speaker_names": dict(names),
            "chapters": chapters,
            "banks": banks,
        }
        if reasons:
            evidence["reasons"] = reasons
            review_queue.append(evidence)
            continue
        mappings.append(
            {
                "speaker_name": display_name,
                "npc_id": npc_id,
                "banks": banks,
                "chapter": chapters[0] if chapters else "",
                "source": "auto",
                "confidence": {
                    "dialogue_lines": sum(names.values()),
                    "stable_name": True,
                    "unique_name": True,
                    "dedicated_banks": True,
                },
            }
        )

    state["mappings"] = sorted(
        mappings, key=lambda item: (item["speaker_name"].casefold(), item["npc_id"])
    )
    state["mapping_review_queue"] = review_queue
    state["alias_resolutions"] = alias_resolutions
    state["unidentified_npc_ids"] = unidentified
    covered_ids = {item["npc_id"] for item in mappings} | {
        item["npc_id"] for item in alias_resolutions
    }
    state["unresolved_npc_ids"] = sorted(installed_ids - cataloged_ids - covered_ids)
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


def create_local_whisper_transcriber(model_path):
    """Create an offline-only Whisper transcriber from an existing local model."""
    model_path = Path(model_path).expanduser().resolve()
    if not model_path.is_dir():
        raise Reverse1999BatchError(
            f"Local Whisper model directory does not exist: {model_path}"
        )
    try:
        from transformers import (
            WhisperForConditionalGeneration,
            WhisperProcessor,
            pipeline,
        )

        model = WhisperForConditionalGeneration.from_pretrained(
            model_path, local_files_only=True
        )
        processor = WhisperProcessor.from_pretrained(model_path, local_files_only=True)
        recognizer = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
        )
    except Exception as error:
        raise Reverse1999BatchError(
            f"Unable to load local Whisper model {model_path}: {error}"
        ) from error

    def transcribe(path):
        result = recognizer(str(path))
        if not isinstance(result, dict):
            return ""
        return str(result.get("text", "")).strip()

    return transcribe


def _transcript_flags(transcript):
    transcript = str(transcript or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", transcript)
    flags = []
    if len(words) < 3:
        flags.append("transcript-too-short")
    folded = transcript.casefold()
    if any(
        marker in folded
        for marker in ("[music]", "[noise]", "[applause]", "[laughter]")
    ):
        flags.append("transcript-non-speech-marker")
    if len(words) >= 6 and len({word.casefold() for word in words}) <= 2:
        flags.append("transcript-repetitive")
    return flags


def _write_auto_review_queue(selections, output):
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_document = json.loads(output.read_text(encoding="utf-8"))
    except FileNotFoundError:
        existing_document = {"clips": []}
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999BatchError(
            f"Unable to read existing auto-review queue: {error}"
        ) from error
    existing = {
        (item.get("bank"), item.get("media_id")): item
        for item in existing_document.get("clips", [])
        if isinstance(item, dict)
    }
    clips = []
    for selection in selections:
        for clip in selection.get("clips", []):
            item = {
                "speaker_name": selection["speaker_name"],
                "npc_id": selection["npc_id"],
                "bank": clip["bank"],
                "media_id": clip["media_id"],
                "chapter": selection.get("chapter", ""),
                "wav": clip["wav"],
                "transcript": clip.get("transcript"),
                "music_or_sfx": None,
                "multiple_speakers": None,
                "approved": None,
                "metrics": clip["metrics"],
            }
            previous = existing.get((clip["bank"], clip["media_id"]), {})
            for key in ("music_or_sfx", "multiple_speakers", "approved"):
                if isinstance(previous.get(key), bool):
                    item[key] = previous[key]
            clips.append(item)
    document = {
        "version": 1,
        "clips": clips,
        "review_note": (
            "Listen to every clip, then set music_or_sfx and multiple_speakers "
            "to true or false. Pending values are never imported."
        ),
    }
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return output


def preselect_auto_references(
    state,
    *,
    review_queue_path=default_auto_review_path,
    transcriber=None,
    candidate_limit=8,
):
    """Preselect technically clean clips without approving their content."""
    if candidate_limit < 3:
        raise Reverse1999BatchError("Auto candidate limit must be at least three")
    mappings = {item["npc_id"]: item for item in state.get("mappings", [])}
    by_npc = defaultdict(list)
    for clip in state.get("clips", []):
        if clip.get("status") != "scored":
            continue
        metrics = clip.get("metrics", {})
        if metrics.get("technical_flags"):
            continue
        by_npc[clip["npc_id"]].append(clip)

    selections = []
    failures = []
    for npc_id, mapping in mappings.items():
        candidates = sorted(
            by_npc.get(npc_id, []),
            key=lambda clip: (
                -int(clip["metrics"].get("quality_score", 0)),
                abs(float(clip["metrics"].get("duration_seconds", 0)) - 6.0),
                clip["bank"].casefold(),
                clip["media_id"],
            ),
        )[:candidate_limit]
        eligible = []
        for clip in candidates:
            if transcriber is not None and "transcript" not in clip:
                try:
                    clip["transcript"] = transcriber(clip["wav"])
                    clip["transcript_flags"] = _transcript_flags(clip["transcript"])
                except Exception as error:
                    clip["transcript"] = ""
                    clip["transcript_flags"] = ["transcription-error"]
                    clip["transcription_error"] = str(error)
            if clip.get("transcript_flags"):
                continue
            review = {
                "speaker_name": mapping["speaker_name"],
                "approved": True,
                "bank": clip["bank"],
                "media_id": clip["media_id"],
                "metrics": clip["metrics"],
            }
            eligible.append((clip, review))

        selected_reviews = select_reference_set(
            [review for _, review in eligible],
            mapping["speaker_name"],
            maximum_clips=3,
        )
        selected_keys = {
            (review["bank"], review["media_id"]) for review in selected_reviews
        }
        selected = [
            clip
            for clip, _ in eligible
            if (clip["bank"], clip["media_id"]) in selected_keys
        ]
        if not selected:
            failures.append(
                {
                    "npc_id": npc_id,
                    "speaker_name": mapping["speaker_name"],
                    "reason": "insufficient-clean-reference-duration",
                    "eligible_clips": len(eligible),
                }
            )
            continue
        for clip in selected:
            clip["auto_selected"] = True
        selections.append(
            {
                "speaker_name": mapping["speaker_name"],
                "npc_id": npc_id,
                "chapter": mapping.get("chapter", ""),
                "banks": sorted({clip["bank"] for clip in selected}),
                "clips": [
                    {key: clip[key] for key in ("bank", "media_id", "wav", "metrics")}
                    | (
                        {"transcript": clip["transcript"]}
                        if "transcript" in clip
                        else {}
                    )
                    for clip in selected
                ],
            }
        )
    state["auto_selections"] = selections
    state["auto_selection_failures"] = failures
    state["auto_review_queue"] = str(
        _write_auto_review_queue(selections, review_queue_path)
    )
    return state


def _review_decision(review):
    approved = review.get("approved")
    if isinstance(approved, bool):
        return approved
    music_or_sfx = review.get("music_or_sfx")
    multiple_speakers = review.get("multiple_speakers")
    if not isinstance(music_or_sfx, bool) or not isinstance(multiple_speakers, bool):
        return None
    technical_flags = review.get("metrics", {}).get("technical_flags", [])
    return not technical_flags and not music_or_sfx and not multiple_speakers


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
        decision = _review_decision(review)
        if decision is None:
            continue
        clip["review"] = review
        clip["review"]["approved"] = decision
        clip["status"] = "approved" if decision else "rejected"
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
        selected_keys = {(item["bank"], item["media_id"]) for item in selected_reviews}
        selected = [
            clip for clip in clips if (clip["bank"], clip["media_id"]) in selected_keys
        ]
        if not selected:
            continue
        imported = []
        for index, clip in enumerate(selected, start=1):
            destination = (
                output_directory / "references" / f"{clip['npc_id']}-{index:02d}.wav"
            )
            trim_and_normalize_voice_reference(clip["wav"], destination)
            imported.append(
                ImportedReference(
                    path=destination,
                    media_id=clip["media_id"],
                    source_sha256=clip["source_sha256"],
                    reference_sha256=hashlib.sha256(
                        destination.read_bytes()
                    ).hexdigest(),
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
                "references": [
                    {
                        "path": str(reference.path),
                        "bank": reference.bank,
                        "media_id": reference.media_id,
                        "source_sha256": reference.source_sha256,
                        "reference_sha256": reference.reference_sha256,
                    }
                    for reference in imported
                ],
                "manifest": str(manifest),
                "banks": bank_names,
            }
        )
        for clip in selected:
            clip["status"] = "imported"
    state["imports"] = imports
    return state


def update_catalog_from_imports(
    state,
    *,
    catalog_path=default_catalog_path,
    reference_root=project_root / "data",
    game_version="3.6.5",
):
    """Atomically append validated imports to the freshly read NPC catalog."""
    catalog_path = Path(catalog_path).expanduser().resolve()
    reference_root = Path(reference_root).expanduser().resolve()
    try:
        document = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999BatchError(f"Unable to read NPC catalog: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("npcs"), list):
        raise Reverse1999BatchError("NPC catalog requires an NPC list")

    existing_ids = {
        str(item.get("id")) for item in document["npcs"] if isinstance(item, dict)
    }
    updates = []
    for imported in state.get("imports", []):
        npc_id = str(imported.get("npc_id", "")).strip()
        if not npc_id or npc_id in existing_ids:
            continue
        references = []
        for reference in imported.get("references", []):
            path = Path(reference["path"]).expanduser().resolve()
            try:
                relative = path.relative_to(reference_root).as_posix()
            except ValueError as error:
                raise Reverse1999BatchError(
                    f"Imported reference is outside reference root: {path}"
                ) from error
            references.append(
                {
                    "bank": reference["bank"],
                    "media_id": int(reference["media_id"]),
                    "source_sha256": reference["source_sha256"],
                    "reference": relative,
                    "reference_sha256": reference["reference_sha256"],
                }
            )
        if not references:
            continue
        entry = {
            "id": npc_id,
            "display_name": str(imported["speaker_name"]).strip(),
            "aliases": [],
            "language": "en",
            "game_versions": [str(game_version)],
            "banks": list(imported["banks"]),
            "approved_references": references,
        }
        document["npcs"].append(entry)
        existing_ids.add(npc_id)
        updates.append(
            {
                "npc_id": npc_id,
                "speaker_name": entry["display_name"],
                "references": len(references),
            }
        )

    if updates:
        try:
            catalog = Reverse1999NpcCatalog.from_dict(document)
            catalog.validate_reference_files(reference_root)
        except Reverse1999CatalogError as error:
            raise Reverse1999BatchError(
                f"Generated NPC catalog is invalid: {error}"
            ) from error
        temporary = catalog_path.with_suffix(f"{catalog_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(catalog_path)
    state["catalog_updates"] = updates
    return state


def stage_counts(state):
    statuses = defaultdict(int)
    for clip in state.get("clips", []):
        statuses[clip.get("status", "unknown")] += 1
    return {
        "mapped": len(state.get("mappings", [])),
        "mapping_review": len(state.get("mapping_review_queue", [])),
        "alias_resolved": len(state.get("alias_resolutions", [])),
        "unidentified": len(state.get("unidentified_npc_ids", [])),
        "unresolved": len(state.get("unresolved_npc_ids", [])),
        "extracted": len(state.get("clips", [])),
        "scored": sum(
            statuses[value] for value in ("scored", "approved", "rejected", "imported")
        ),
        "pending_review": statuses["scored"],
        "rejected": statuses["rejected"],
        "approved": statuses["approved"],
        "imported": statuses["imported"],
        "auto_selected": sum(
            len(item.get("clips", [])) for item in state.get("auto_selections", [])
        ),
        "cataloged": len(state.get("catalog_updates", [])),
        "errors": len(state.get("errors", [])) + statuses["score-error"],
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Resume Reverse: 1999 NPC voice scan, mapping, extraction, scoring, "
            "review, import, and confidence-gated catalog updates."
        )
    )
    parser.add_argument(
        "stage",
        choices=(
            "scan",
            "map",
            "discover",
            "extract",
            "score",
            "preselect",
            "review",
            "import",
            "catalog",
            "run",
            "auto",
            "finish",
        ),
    )
    parser.add_argument("--state", type=Path, default=default_state_path)
    parser.add_argument("--game-audio-directory", type=Path)
    parser.add_argument("--game-config-directory", type=Path)
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument("--catalog", type=Path, default=default_catalog_path)
    parser.add_argument("--mappings", type=Path, default=default_mapping_path)
    parser.add_argument("--reviews", type=Path, default=default_review_path)
    parser.add_argument(
        "--auto-review-queue", type=Path, default=default_auto_review_path
    )
    parser.add_argument("--output", type=Path, default=default_voice_output)
    parser.add_argument("--reference-root", type=Path, default=project_root / "data")
    parser.add_argument("--game-version", default="3.6.5")
    parser.add_argument("--minimum-dialogue-lines", type=int, default=2)
    parser.add_argument("--maximum-auto-banks", type=int, default=2)
    parser.add_argument("--candidate-limit", type=int, default=8)
    parser.add_argument(
        "--whisper-model",
        type=Path,
        help=(
            "Existing local Whisper model directory. No model is downloaded; "
            "when omitted, transcription is left for manual review."
        ),
    )
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        state = load_state(arguments.state)
        transcriber = None

        def checkpoint():
            save_state(state, arguments.state)

        def preselect():
            nonlocal transcriber
            if arguments.whisper_model is not None and transcriber is None:
                transcriber = create_local_whisper_transcriber(arguments.whisper_model)
            return preselect_auto_references(
                state,
                review_queue_path=arguments.auto_review_queue,
                transcriber=transcriber,
                candidate_limit=arguments.candidate_limit,
            )

        actions = {
            "scan": lambda: scan_installed_game(
                state,
                game_audio_directory=arguments.game_audio_directory,
                game_config_directory=arguments.game_config_directory,
            ),
            "map": lambda: map_speakers(
                state,
                catalog_path=arguments.catalog,
                mapping_path=arguments.mappings,
            ),
            "discover": lambda: discover_auto_mappings(
                state,
                catalog_path=arguments.catalog,
                minimum_dialogue_lines=arguments.minimum_dialogue_lines,
                maximum_banks=arguments.maximum_auto_banks,
            ),
            "extract": lambda: extract_mapped_clips(
                state, decoder=arguments.decoder, checkpoint=checkpoint
            ),
            "score": lambda: score_extracted_clips(state, checkpoint=checkpoint),
            "preselect": preselect,
            "review": lambda: merge_clip_reviews(state, review_path=arguments.reviews),
            "auto-review": lambda: merge_clip_reviews(
                state, review_path=arguments.auto_review_queue
            ),
            "import": lambda: import_approved_references(
                state, output_directory=arguments.output
            ),
            "catalog": lambda: update_catalog_from_imports(
                state,
                catalog_path=arguments.catalog,
                reference_root=arguments.reference_root,
                game_version=arguments.game_version,
            ),
        }
        workflows = {
            "run": ("scan", "map", "extract", "score", "review", "import"),
            "auto": ("scan", "discover", "extract", "score", "preselect"),
            "finish": ("auto-review", "import", "catalog"),
        }
        stages = workflows.get(arguments.stage, (arguments.stage,))
        for name in stages:
            actions[name]()
            checkpoint()
            if (
                arguments.stage == "run"
                and name == "review"
                and stage_counts(state)["pending_review"]
            ):
                break
    except Exception as error:
        print(error, file=sys.stderr)
        return 1
    counts = stage_counts(state)
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"State: {Path(arguments.state).expanduser().resolve()}")
    if state.get("auto_review_queue"):
        print(f"Review queue: {state['auto_review_queue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
