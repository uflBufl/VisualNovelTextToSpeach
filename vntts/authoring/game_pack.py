"""Atomic final game-pack publication from authoritative authoring state."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np
import soundfile as sf
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, load_game_pack, write_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
    validate_authoring_publication_authority,
)
from vntts.authoring.failure_reference_binding_records import (
    FailureReferenceBindingError,
    load_failure_reference_binding_document,
)
from vntts.authoring.generation_lease import process_started_at
from vntts.authoring.generation_manifest import approved_manifest_entries
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    reviewed_waveform_publication_queue_ids,
    validate_generation_state_document,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
)
from vntts.authoring.publication import (
    rename_directory_no_replace as _rename_directory_no_replace,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.voices import synthesis_character_for_line

_canonical_sha256 = canonical_document_sha256


class FinalGamePackError(RuntimeError):
    """Raised before an incomplete or unsafe final pack can be published."""


@dataclass(frozen=True)
class FinalGamePackResult:
    directory: Path
    manifest: Path
    game_id: str
    game_version: str
    approved_count: int
    rejected_count: int
    live_fallback_count: int
    omitted_count: int
    source_queue_sha256: str
    source_state_sha256: str

    def to_dict(self):
        payload = asdict(self)
        payload["directory"] = str(self.directory)
        payload["manifest"] = str(self.manifest)
        return payload


def publish_final_game_pack(
    destination,
    *,
    state_path,
    queue_path,
    story_index_path,
    voice_manifest_path,
    failure_reference_binding_path=None,
    game_id=None,
    game_version,
    producers,
    created_at=None,
):
    """Stage, verify and atomically publish one immutable game-pack directory."""
    destination = _new_destination(destination)
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    story_index_path = Path(story_index_path).expanduser().resolve()
    voice_manifest_path = Path(voice_manifest_path).expanduser().resolve()
    failure_reference_binding_path = (
        None
        if failure_reference_binding_path is None
        else Path(failure_reference_binding_path).expanduser().resolve()
    )
    if (
        failure_reference_binding_path is not None
        and failure_reference_binding_path.is_dir()
    ):
        failure_reference_binding_path = failure_reference_binding_path / "binding.json"
    game_version = _required_text(game_version, "game version")
    producers = _validate_producers(producers)
    created_at = created_at or _now()

    try:
        initial_state = load_generation_state(state_path)
    except BulkGenerationError as error:
        raise FinalGamePackError(str(error)) from error

    destination.parent.mkdir(parents=True, exist_ok=True)
    with _PublicationLease(destination) as publication_lease:
        with generation_publication_leases(
            ((state_path.parent, initial_state["queue_sha256"]),),
            process_checker=process_is_alive,
        ) as generation_leases:
            generation_lease = generation_leases[0]
            try:
                queue, queue_sha256 = load_stable_generation_queue(queue_path)
            except BulkGenerationError as error:
                raise FinalGamePackError(str(error)) from error
            state, state_sha256 = _load_stable_state(state_path, queue, queue_sha256)
            if queue_sha256 != state["queue_sha256"]:
                raise FinalGamePackError(
                    "Generation state does not match the exact queue bytes"
                )
            _require_final_review_state(state, queue)

            with TemporaryDirectory(
                dir=destination.parent,
                prefix=f".{destination.name}.staging-",
            ) as staging_directory:
                staging = Path(staging_directory)
                inventory = {state_path: state_sha256}
                story_copy = staging / "story" / "story-index.jsonl"
                voice_copy = staging / "voices" / "voice-manifest.json"
                story_sha256 = _copy_control(
                    story_index_path, story_copy, inventory, "story index"
                )
                voice_sha256 = _copy_control(
                    voice_manifest_path, voice_copy, inventory, "voice manifest"
                )
                captured_queue_sha256 = _capture_control(
                    queue_path, inventory, "generation queue"
                )
                if captured_queue_sha256 != queue_sha256:
                    raise FinalGamePackError(
                        "Generation queue changed while publication was staged"
                    )

                failure_reference_document = None
                if failure_reference_binding_path is not None:
                    try:
                        failure_reference_document = (
                            load_failure_reference_binding_document(
                                failure_reference_binding_path.parent
                            )
                        )
                    except FailureReferenceBindingError as error:
                        raise FinalGamePackError(str(error)) from error
                    authority = failure_reference_document["source_authority"]
                    if (
                        authority["queue_sha256"] != queue_sha256
                        or authority["voice_manifest_sha256"] != voice_sha256
                    ):
                        raise FinalGamePackError(
                            "Failure-reference binding belongs to different pack controls"
                        )
                    binding_copy = (
                        staging
                        / "voices"
                        / "failure-reference-binding"
                        / "binding.json"
                    )
                    _copy_control(
                        failure_reference_binding_path,
                        binding_copy,
                        inventory,
                        "failure-reference binding",
                    )
                    for group in failure_reference_document["groups"]:
                        relative = _safe_relative(
                            group["reference"], "Selected reference"
                        )
                        source = _contained_source(
                            failure_reference_binding_path.parent,
                            relative,
                            "selected reference",
                        )
                        _copy_control(
                            source,
                            binding_copy.parent / Path(*relative.parts),
                            inventory,
                            "selected reference",
                        )

                story = _load_story(story_copy)
                voice_document, voice_entries = _load_voices(voice_copy)
                narrator_selection = _verify_voice_control_provenance(
                    state,
                    queue,
                    voice_manifest_path,
                    voice_document,
                    voice_entries,
                    failure_reference_binding_path=failure_reference_binding_path,
                    failure_reference_document=failure_reference_document,
                )
                voice_override = _validate_source_bindings(
                    queue.metadata,
                    queue_path=queue_path,
                    story_index_path=story_index_path,
                    voice_manifest_path=voice_manifest_path,
                    story_sha256=story_sha256,
                    voice_manifest_sha256=voice_sha256,
                    reviewed_waveform_publication=state.get(
                        "reviewed_waveform_publication"
                    ),
                )
                _validate_story_identity(state, story)
                voice_projection = _copy_portable_voice_manifest_and_references(
                    voice_manifest_path,
                    voice_copy,
                    voice_document,
                    voice_entries,
                    inventory,
                )

                generated_manifest = staging / "generated" / "manifest.json"
                if not _reviewed_waveform_supersedes_legacy_authority(state):
                    try:
                        validate_authoring_publication_authority(
                            state_path,
                            state,
                        )
                    except BulkGenerationError as error:
                        raise FinalGamePackError(str(error)) from error
                generated_records = approved_manifest_entries(state, state_path.parent)
                live_fallback_records = _live_fallback_records(state, queue)
                omission_records = _audio_event_omission_records(state, queue)
                reviewed_waveform_records = _reviewed_waveform_publication_records(
                    state, queue
                )
                _validate_generated_story_records(generated_records, story)
                _validate_live_fallback_story_records(live_fallback_records, story)
                _validate_audio_event_omission_story_records(omission_records, story)
                _validate_reviewed_waveform_story_records(
                    reviewed_waveform_records, story
                )
                for record in generated_records:
                    relative = _safe_relative(
                        record["audio"], "Generated-audio state path"
                    )
                    source = _contained_source(
                        state_path.parent,
                        relative,
                        "generated WAV",
                    )
                    _copy_control(
                        source,
                        generated_manifest.parent / Path(*relative.parts),
                        inventory,
                        "generated WAV",
                    )
                try:
                    write_generated_audio_manifest(
                        generated_manifest,
                        {
                            "game": state.get("game"),
                            "language": state.get("language"),
                            "source_queue_sha256": state["queue_sha256"],
                            "generated_at": created_at,
                            "vntts.authoring.live_fallback": {
                                "schema_version": 1,
                                "mode": "explicit",
                                "entries": live_fallback_records,
                            },
                            "vntts.authoring.audio_event_omission": {
                                "schema_version": 1,
                                "mode": "explicit",
                                "entries": omission_records,
                            },
                            "vntts.authoring.reviewed_waveform_publication": {
                                "schema_version": 1,
                                "mode": "exact_reviewed_waveform",
                                "entries": reviewed_waveform_records,
                            },
                        },
                        generated_records,
                    )
                except GeneratedAudioManifestError as error:
                    raise FinalGamePackError(str(error)) from error

                counts = _review_counts(state)
                resolved_game_id = _required_text(
                    game_id if game_id is not None else state.get("game"),
                    "game id",
                )
                pack_manifest = staging / "game-pack.json"
                try:
                    write_game_pack(
                        pack_manifest,
                        {
                            "game": {
                                "id": resolved_game_id,
                                "version": game_version,
                            },
                            "producers": producers,
                            "created_at": created_at,
                            "vntts.authoring": {
                                "source_queue_sha256": queue_sha256,
                                "source_state_sha256": state_sha256,
                                "selected_voice_manifest_sha256": voice_sha256,
                                "queue_voice_manifest_sha256": queue.metadata.get(
                                    "source_voice_manifest_sha256"
                                ),
                                "voice_manifest_override": voice_override,
                                "narrator_selection": narrator_selection,
                                "failure_reference_binding": (
                                    None
                                    if failure_reference_document is None
                                    else {
                                        "path": "voices/failure-reference-binding/binding.json",
                                        "binding_id": failure_reference_document[
                                            "binding_id"
                                        ],
                                        "audit_id": failure_reference_document[
                                            "audit_id"
                                        ],
                                        "decision_set_id": failure_reference_document[
                                            "decision_set_id"
                                        ],
                                    }
                                ),
                                "reviewed_waveform_publication": (
                                    None
                                    if not reviewed_waveform_records
                                    else {
                                        "batch_id": state[
                                            "reviewed_waveform_publication"
                                        ]["batch_id"],
                                        "approved_count": len(
                                            reviewed_waveform_records
                                        ),
                                        "synthesis_reproducibility": False,
                                    }
                                ),
                                "voice_reference_projection": voice_projection,
                                **counts,
                            },
                        },
                        {
                            "story_index": story_copy,
                            "voice_manifest": voice_copy,
                            "generated_audio": generated_manifest,
                        },
                    )
                    load_game_pack(pack_manifest)
                except GamePackError as error:
                    raise FinalGamePackError(str(error)) from error

                generation_lease.assert_owned()
                publication_lease.assert_owned()
                _assert_controls_unchanged(inventory)
                if _path_exists(destination):
                    raise FinalGamePackError(
                        f"Final game-pack destination already exists: {destination}"
                    )
                try:
                    _rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    raise FinalGamePackError(
                        f"Unable to atomically publish final game pack: {error}"
                    ) from error
                publication_lease.mark_committed()
                generation_lease.mark_committed()

    return FinalGamePackResult(
        directory=destination,
        manifest=destination / "game-pack.json",
        game_id=resolved_game_id,
        game_version=game_version,
        approved_count=counts["approved_count"],
        rejected_count=counts["rejected_count"],
        live_fallback_count=counts["live_fallback_count"],
        omitted_count=counts["omitted_count"],
        source_queue_sha256=queue_sha256,
        source_state_sha256=state_sha256,
    )


class _PublicationLease:
    def __init__(self, destination):
        self.destination = destination
        self.path = destination.parent / f".{destination.name}.publication.json"
        self.guard_path = self.path.with_suffix(".guard")
        self.owner = uuid4().hex
        self.committed = False

    def __enter__(self):
        payload = {
            "schema": "vntts.game-pack-publication-lease",
            "schema_version": 1,
            "owner": self.owner,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_started_at": process_started_at(os.getpid()),
            "destination": str(self.destination),
            "created_at": _now(),
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        try:
            with exclusive_advisory_lock(self.guard_path):
                if _path_exists(self.destination):
                    raise FinalGamePackError(
                        "Final game-pack destination already exists: "
                        f"{self.destination}"
                    )
                if self.path.exists():
                    try:
                        existing_payload = self.path.read_bytes()
                        existing = json.loads(existing_payload.decode("utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise FinalGamePackError(
                            f"Unable to inspect publication lease: {error}"
                        ) from error
                    if self._existing_is_live(existing):
                        raise FinalGamePackError(
                            f"Another final game-pack publication owns {self.path}"
                        )
                    self._archive_stale(existing_payload)
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
        except FileExistsError as error:
            raise FinalGamePackError(
                "Another final game-pack publication acquired the destination"
            ) from error
        except AdvisoryLockBusyError as error:
            raise FinalGamePackError(
                "Another final game-pack publication is acquiring the destination"
            ) from error
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        ownership_lost = False
        try:
            with exclusive_advisory_lock(self.guard_path, blocking=True):
                try:
                    document = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ownership_lost = True
                else:
                    if document.get("owner") == self.owner:
                        self.path.unlink()
                    else:
                        ownership_lost = True
        except AdvisoryLockBusyError:
            ownership_lost = True
        if ownership_lost and exc_type is None and not self.committed:
            raise FinalGamePackError(
                "Final game-pack publication lease ownership was lost"
            )
        return False

    def assert_owned(self):
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FinalGamePackError(
                "Final game-pack publication lease became unreadable"
            ) from error
        if document.get("owner") != self.owner:
            raise FinalGamePackError(
                "Final game-pack publication lease ownership was lost"
            )

    def mark_committed(self):
        self.committed = True

    def _existing_is_live(self, document):
        if (
            not isinstance(document, dict)
            or document.get("schema") != "vntts.game-pack-publication-lease"
            or document.get("schema_version") != 1
            or not isinstance(document.get("owner"), str)
            or not document["owner"]
            or document.get("destination") != str(self.destination)
        ):
            return True
        if document.get("hostname") != socket.gethostname():
            return True
        pid = document.get("pid")
        if not process_is_alive(pid):
            return False
        expected_start = document.get("process_started_at")
        if not expected_start:
            return True
        return process_started_at(pid) == expected_start

    def _archive_stale(self, expected_payload):
        try:
            payload = self.path.read_bytes()
        except OSError as error:
            raise FinalGamePackError(
                f"Unable to inspect stale publication lease: {error}"
            ) from error
        if payload != expected_payload:
            raise FinalGamePackError(
                "Final game-pack publication lease changed during stale recovery"
            )
        digest = hashlib.sha256(payload).hexdigest()[:12]
        archive = self.path.with_name(f"{self.path.name}.interrupted-{digest}")
        if _path_exists(archive):
            raise FinalGamePackError(
                f"Stale publication lease archive already exists: {archive}"
            )
        if self.path.read_bytes() != expected_payload:
            raise FinalGamePackError(
                "Final game-pack publication lease changed during stale recovery"
            )
        os.replace(self.path, archive)


def _require_final_review_state(state, queue):
    if state.get("active") is not None:
        raise FinalGamePackError(
            "Generation state has an active or interrupted attempt; resume it first"
        )
    queue_ids = {item.queue_id for item in queue.items}
    state_ids = set(state["items"])
    if state_ids != queue_ids:
        missing = queue_ids.difference(state_ids)
        extra = state_ids.difference(queue_ids)
        if missing:
            raise FinalGamePackError(
                f"Generation state is missing {len(missing)} selected queue item(s)"
            )
        raise FinalGamePackError(
            f"Generation state contains {len(extra)} unknown queue item(s)"
        )
    pending = []
    failed = []
    for queue_id, item in state["items"].items():
        if item.get("status") == "failed":
            failed.append(queue_id)
        elif item.get("review_status") == "pending_review":
            pending.append(queue_id)
    if failed:
        raise FinalGamePackError(
            f"Generation state has {len(failed)} failed item(s); resolve them first"
        )
    if pending:
        raise FinalGamePackError(
            f"Generation state has {len(pending)} pending review item(s)"
        )


def _review_counts(state):
    return {
        "approved_count": sum(
            item.get("status") == "approved" for item in state["items"].values()
        ),
        "rejected_count": sum(
            item.get("review_status") == "rejected" for item in state["items"].values()
        ),
        "live_fallback_count": sum(
            isinstance(item.get("live_fallback"), dict)
            for item in state["items"].values()
        ),
        "omitted_count": sum(
            isinstance(item.get("audio_event_omission"), dict)
            for item in state["items"].values()
        ),
        "state_item_count": len(state["items"]),
    }


def _reviewed_waveform_supersedes_legacy_authority(state):
    migrated = reviewed_waveform_publication_queue_ids(state)
    approved = {
        queue_id
        for queue_id, item in state["items"].items()
        if item.get("status") == "approved" and item.get("review_status") == "approved"
    }
    return bool(approved) and migrated == approved


def _live_fallback_records(state, queue):
    queue_ids = {item.queue_id for item in queue.items}
    records = []
    for queue_id, item in state["items"].items():
        decision = item.get("live_fallback")
        if not isinstance(decision, dict):
            continue
        if queue_id not in queue_ids:
            raise FinalGamePackError(
                f"Live fallback item {queue_id!r} is missing from the queue"
            )
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _audio_event_omission_records(state, queue):
    queue_ids = {item.queue_id for item in queue.items}
    records = []
    for queue_id, item in state["items"].items():
        decision = item.get("audio_event_omission")
        if not isinstance(decision, dict):
            continue
        if queue_id not in queue_ids:
            raise FinalGamePackError(
                f"Audio-event omission {queue_id!r} is missing from the queue"
            )
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _reviewed_waveform_publication_records(state, queue):
    publication = state.get("reviewed_waveform_publication")
    if not isinstance(publication, dict):
        return []
    queue_ids = {item.queue_id for item in queue.items}
    records = []
    for ledger in publication["items"]:
        queue_id = ledger["queue_id"]
        if queue_id not in queue_ids:
            raise FinalGamePackError(
                f"Reviewed waveform {queue_id!r} is missing from the queue"
            )
        records.append(
            {
                "batch_id": publication["batch_id"],
                "queue_id": queue_id,
                "line_id": ledger["line_id"],
                "text_sha256": ledger["text_sha256"],
                "speaker": ledger["speaker"],
                "audio_sha256": ledger["file_sha256"],
                "base_result_sha256": ledger["base_result_sha256"],
                "route": copy.deepcopy(ledger["route"]),
                "synthesis_reproducibility": False,
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _copy_control(source, destination, inventory, label):
    source = Path(source).expanduser().resolve()
    destination = Path(destination)
    digest = _capture_control(source, inventory, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        copied_digest = sha256_file(destination)
    except OSError as error:
        raise FinalGamePackError(f"Unable to copy {label} {source}: {error}") from error
    if copied_digest != digest:
        raise FinalGamePackError(f"{label.capitalize()} changed while it was copied")
    return digest


def _capture_control(source, inventory, label):
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FinalGamePackError(f"{label.capitalize()} does not exist: {source}")
    try:
        digest = sha256_file(source)
    except OSError as error:
        raise FinalGamePackError(
            f"Unable to checksum {label} {source}: {error}"
        ) from error
    previous = inventory.get(source)
    if previous is not None and previous != digest:
        raise FinalGamePackError(f"{label.capitalize()} changed during staging")
    inventory[source] = digest
    return digest


def _assert_controls_unchanged(inventory):
    for source, expected in inventory.items():
        try:
            actual = sha256_file(source)
        except OSError as error:
            raise FinalGamePackError(
                f"Publication source became unreadable: {source}: {error}"
            ) from error
        if actual != expected:
            raise FinalGamePackError(
                f"Publication source changed during staging: {source}"
            )


def _copy_portable_voice_manifest_and_references(
    source_manifest,
    destination_manifest,
    document,
    entries,
    inventory,
):
    source_root = source_manifest.parent.resolve()
    destination_root = destination_manifest.parent
    copied = {}
    projections = []
    rewritten = copy.deepcopy(document)
    raw_voices = rewritten.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(entries):
        raise FinalGamePackError("Voice manifest entries changed during staging")
    for raw_entry, entry in zip(raw_voices, entries, strict=True):
        configured_references = raw_entry.get("references")
        if (
            not isinstance(configured_references, list)
            or tuple(configured_references) != entry.references
        ):
            raise FinalGamePackError("Voice manifest references changed during staging")
        portable_references = []
        for configured in entry.references:
            relative = _safe_relative(configured, "Voice reference")
            source = _contained_source(source_root, relative, "voice reference")
            source_sha256 = _capture_control(source, inventory, "voice reference")
            if relative.suffix.casefold() == ".wav":
                portable = relative
            else:
                portable = relative.with_name(f"{relative.stem}.vntts-pcm16.wav")
            destination = destination_root / Path(*portable.parts)
            if destination in copied:
                if copied[destination] != source_sha256:
                    raise FinalGamePackError(
                        f"Portable voice reference path collides: {portable.as_posix()}"
                    )
                portable_references.append(portable.as_posix())
                continue
            if portable == relative:
                _copy_control(source, destination, inventory, "voice reference")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    samples, sample_rate = sf.read(
                        source, dtype="float32", always_2d=True
                    )
                    if samples.size == 0 or sample_rate < 1:
                        raise ValueError("decoded reference is empty")
                    mono = np.mean(samples, axis=1, dtype=np.float32)
                    sf.write(
                        destination,
                        np.clip(mono, -1.0, 1.0),
                        sample_rate,
                        format="WAV",
                        subtype="PCM_16",
                    )
                    info = probe_pcm16_mono_wav(destination)
                except Exception as error:
                    raise FinalGamePackError(
                        f"Unable to project voice reference {source} to PCM16 WAV: {error}"
                    ) from error
                output_sha256 = sha256_file(destination)
                projections.append(
                    {
                        "character": entry.character,
                        "source_reference": relative.as_posix(),
                        "source_sha256": source_sha256,
                        "output_reference": portable.as_posix(),
                        "output_sha256": output_sha256,
                        "sample_rate": info.sample_rate,
                        "sample_count": info.sample_count,
                        "channels": 1,
                        "subtype": "PCM_16",
                    }
                )
            copied[destination] = source_sha256
            portable_references.append(portable.as_posix())
        raw_entry["references"] = portable_references
    if not projections:
        return None
    source_manifest_sha256 = sha256_file(source_manifest)
    atomic_write_json(destination_manifest, rewritten, sort_keys=True)
    try:
        _load_voices(destination_manifest)
    except FinalGamePackError as error:
        raise FinalGamePackError(
            f"Projected voice manifest is invalid: {error}"
        ) from error
    return {
        "schema": "vntts.authoring-voice-reference-projection",
        "schema_version": 1,
        "method": "decode_to_pcm16_mono_wav",
        "source_manifest_sha256": source_manifest_sha256,
        "output_manifest_sha256": sha256_file(destination_manifest),
        "entries": projections,
    }


def _contained_source(root, relative, label):
    root = Path(root).resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise FinalGamePackError(
            f"{label.capitalize()} leaves its source root"
        ) from error
    if not candidate.is_file():
        raise FinalGamePackError(f"{label.capitalize()} does not exist: {candidate}")
    return candidate


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise FinalGamePackError(f"{label} must be a safe POSIX-relative path")
    relative = PurePosixPath(value.strip())
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise FinalGamePackError(f"{label} must be a safe POSIX-relative path")
    return relative


def _load_story(path):
    try:
        return load_story_index_document(path)
    except StoryIndexError as error:
        raise FinalGamePackError(str(error)) from error


def _load_voices(path):
    try:
        document, entries = load_voice_manifest(path, allow_legacy=False)
    except VoiceManifestError as error:
        raise FinalGamePackError(str(error)) from error
    return document, entries


def _validate_source_bindings(
    queue_metadata,
    *,
    queue_path,
    story_index_path,
    voice_manifest_path,
    story_sha256,
    voice_manifest_sha256,
    reviewed_waveform_publication=None,
):
    def declared_binding(
        path_field, hash_field, label, migration_hash_field, selected_sha256
    ):
        declared_path = queue_metadata.get(path_field)
        declared_hash = queue_metadata.get(hash_field)
        if not isinstance(declared_path, str) or not declared_path.strip():
            if (
                isinstance(reviewed_waveform_publication, dict)
                and reviewed_waveform_publication.get(migration_hash_field)
                == selected_sha256
            ):
                return None, reviewed_waveform_publication[migration_hash_field]
            raise FinalGamePackError(
                f"Generation queue lacks required {label} source path binding; migrate it first"
            )
        if not isinstance(declared_hash, str) or len(declared_hash) != 64:
            raise FinalGamePackError(
                f"Generation queue lacks required {label} checksum binding; migrate it first"
            )
        bound_path = Path(declared_path).expanduser()
        if not bound_path.is_absolute():
            bound_path = queue_path.parent / bound_path
        return bound_path.resolve(), declared_hash

    story_path, declared_story_sha256 = declared_binding(
        "source_story_index",
        "source_story_index_sha256",
        "story index",
        "selected_story_index_sha256",
        story_sha256,
    )
    migrated_story_authorized = (
        isinstance(reviewed_waveform_publication, dict)
        and reviewed_waveform_publication.get("selected_story_index_sha256")
        == story_sha256
    )
    if declared_story_sha256 != story_sha256 and not migrated_story_authorized:
        raise FinalGamePackError(
            "Generation queue story index checksum does not match the selected source"
        )
    if story_path is not None and story_path != story_index_path.resolve():
        if not migrated_story_authorized:
            raise FinalGamePackError(
                "Generation queue story index path does not match the selected source"
            )

    voice_path, declared_voice_sha256 = declared_binding(
        "source_voice_manifest",
        "source_voice_manifest_sha256",
        "voice manifest",
        "selected_voice_manifest_sha256",
        voice_manifest_sha256,
    )
    if voice_path is None:
        if declared_voice_sha256 != voice_manifest_sha256:
            raise FinalGamePackError(
                "Reviewed-waveform voice manifest checksum does not match the selected source"
            )
        return False
    return (
        voice_path != voice_manifest_path.resolve()
        or declared_voice_sha256 != voice_manifest_sha256
    )


def _load_stable_state(state_path, queue, queue_sha256):
    try:
        payload = state_path.read_bytes()
        state = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise FinalGamePackError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if not isinstance(state, dict):
        raise FinalGamePackError("Generation state must be a JSON object")
    try:
        validate_generation_state_document(
            state, state_path.parent, queue, queue_sha256
        )
    except BulkGenerationError as error:
        raise FinalGamePackError(str(error)) from error
    return state, hashlib.sha256(payload).hexdigest()


def _validate_story_identity(state, story):
    for field in ("game", "language"):
        state_value = state.get(field)
        story_value = getattr(story, field)
        if (
            state_value is not None
            and story_value is not None
            and state_value != story_value
        ):
            raise FinalGamePackError(
                f"Generation state {field} does not match the story index"
            )


def _verify_voice_control_provenance(
    state,
    queue,
    voice_manifest_path,
    voice_document,
    voice_entries,
    *,
    failure_reference_binding_path=None,
    failure_reference_document=None,
):
    migrated = reviewed_waveform_publication_queue_ids(state)
    registry = state.get("synthesis_controls")
    if not isinstance(registry, dict):
        if any(
            result.get("status") == "approved" and queue_id not in migrated
            for queue_id, result in state["items"].items()
        ):
            raise FinalGamePackError(
                "Generation state lacks per-control synthesis provenance; regenerate or migrate it first"
            )
        registry = {}
    required_paths = {
        voice_manifest_path.resolve(): (
            _source_sha256(voice_manifest_path, "voice manifest"),
            lambda role: role == "voice_manifest",
        )
    }
    source_root = voice_manifest_path.parent.resolve()
    narrator_reference_bindings = {}
    for entry in voice_entries:
        names = (entry.character, *entry.aliases)
        for configured in entry.references:
            relative = _safe_relative(configured, "Voice reference")
            source = _contained_source(source_root, relative, "voice reference")
            digest = _source_sha256(source, "voice reference")
            required_paths[source] = (
                digest,
                lambda role: role.startswith("voice_reference:"),
            )
            for name in names:
                narrator_reference_bindings.setdefault(
                    normalize_character_name(name), set()
                ).add((source, digest))
    queue_by_id = {item.queue_id: item for item in queue.items}
    try:
        queue_voice_overrides = queue_voice_overrides_from_manifest(
            voice_document,
            queue_ids=queue_by_id,
            voices=voice_entries,
        )
    except SourceReferenceBindingError as error:
        raise FinalGamePackError(str(error)) from error
    queue_voice_overrides_digest = (
        queue_voice_overrides_sha256(queue_voice_overrides)
        if queue_voice_overrides
        else None
    )
    failure_reference_overrides = {}
    failure_reference_paths = {}
    combined_overrides = dict(queue_voice_overrides)
    combined_overrides_digest = queue_voice_overrides_digest
    if failure_reference_document is not None:
        if failure_reference_binding_path is None:
            raise FinalGamePackError(
                "Failure-reference binding document has no source path"
            )
        failure_reference_overrides = dict(
            failure_reference_document["queue_voice_overrides"]
        )
        combined_overrides.update(failure_reference_overrides)
        combined_overrides_digest = queue_voice_overrides_sha256(combined_overrides)
        binding_digest = _source_sha256(
            failure_reference_binding_path, "failure-reference binding"
        )
        failure_reference_paths[failure_reference_binding_path.resolve()] = (
            binding_digest,
            lambda role: role == "failure_reference_binding",
        )
        binding_root = failure_reference_binding_path.parent.resolve()
        for group in failure_reference_document["groups"]:
            relative = _safe_relative(group["reference"], "Selected reference")
            source = _contained_source(binding_root, relative, "selected reference")
            digest = _source_sha256(source, "selected reference")
            if digest != group["reference_sha256"]:
                raise FinalGamePackError("Failure-reference selected audio changed")
            failure_reference_paths[source] = (
                digest,
                lambda role: role.startswith("failure_reference_selected:"),
            )
    narrator_selections = set()
    migrated_narrator_selection = None
    publication = state.get("reviewed_waveform_publication")
    if isinstance(publication, dict):
        if publication["selected_voice_manifest_sha256"] != _source_sha256(
            voice_manifest_path, "voice manifest"
        ):
            raise FinalGamePackError(
                "Reviewed-waveform publication belongs to a different voice manifest"
            )
        narrator_character = publication["narrator_character"]
        bindings = narrator_reference_bindings.get(
            normalize_character_name(narrator_character), set()
        )
        configured_digests = sorted({digest for _path, digest in bindings})
        if configured_digests != publication["narrator_reference_sha256s"]:
            raise FinalGamePackError("Reviewed-waveform narrator binding changed")
        migrated_narrator_selection = {
            "character": narrator_character,
            "reference_sha256s": configured_digests,
        }
    for queue_id, result in state["items"].items():
        if result.get("status") in {"live_fallback", "omitted"} or (
            result.get("status") == "generated"
            and result.get("review_status") == "rejected"
        ):
            continue
        if queue_id in migrated:
            continue
        provenance = result.get("synthesis_provenance_sha256")
        controls = registry.get(provenance)
        if not isinstance(controls, list):
            raise FinalGamePackError(
                f"State item {queue_id!r} lacks its exact synthesis-control inventory"
            )
        provenance_document = {
            "provider": result.get("provider"),
            "model": result.get("model"),
            "generation_profile": result.get("generation_profile"),
            "text_transform": result.get("text_transform"),
            "controls": [
                {"role": control["role"], "sha256": control["sha256"]}
                for control in controls
            ],
        }
        configuration = result.get("synthesis_configuration")
        if configuration is not None:
            provenance_document.update(configuration)
        calculated = canonical_document_sha256(provenance_document)
        if calculated != provenance:
            raise FinalGamePackError(
                f"State item {queue_id!r} synthesis provenance is inconsistent"
            )
        controls_by_path = {
            Path(control["path"]).expanduser().resolve(): control
            for control in controls
            if control.get("kind") == "file"
        }
        for path, (digest, role_matches) in required_paths.items():
            control = controls_by_path.get(path)
            if (
                control is None
                or not role_matches(control.get("role", ""))
                or control.get("sha256") != digest
            ):
                raise FinalGamePackError(
                    f"Voice input {path} does not match synthesis controls for {queue_id!r}"
                )
        binding_controls_present = bool(
            failure_reference_binding_path is not None
            and failure_reference_binding_path.resolve() in controls_by_path
        )
        if binding_controls_present:
            for path, (digest, role_matches) in failure_reference_paths.items():
                control = controls_by_path.get(path)
                if (
                    control is None
                    or not role_matches(control.get("role", ""))
                    or control.get("sha256") != digest
                ):
                    raise FinalGamePackError(
                        "Failure-reference input does not match synthesis controls "
                        f"for {queue_id!r}"
                    )
        item = queue_by_id[queue_id]
        narrator_controls = [
            control
            for control in controls
            if str(control.get("role", "")).startswith("narrator_selection:")
        ]
        effective_character = result.get("voice_character") or (
            synthesis_character_for_line(item.speaker, item.voice_character)
        )
        expected_override = combined_overrides.get(queue_id)
        binding = result.get("source_reference_binding")
        expected_override_digest = (
            combined_overrides_digest
            if binding_controls_present
            else queue_voice_overrides_digest
        )
        if queue_id in failure_reference_overrides and not binding_controls_present:
            raise FinalGamePackError(
                f"Failure-reference controls are missing for {queue_id!r}"
            )
        if expected_override is not None:
            if (
                effective_character != expected_override
                or not isinstance(binding, dict)
                or binding.get("queue_voice_overrides_sha256")
                != expected_override_digest
            ):
                raise FinalGamePackError(
                    f"Source-reference voice binding is missing for {queue_id!r}"
                )
        elif binding is not None:
            raise FinalGamePackError(
                f"State item {queue_id!r} has an unselected source-reference binding"
            )
        if effective_character == "Narrator" and len(narrator_controls) != 1:
            raise FinalGamePackError(
                f"Narrator item {queue_id!r} lacks one role-bound narrator selection"
            )
        for control in narrator_controls:
            character = control["role"].removeprefix("narrator_selection:")
            configured_bindings = narrator_reference_bindings.get(
                normalize_character_name(character), set()
            )
            try:
                control_path = Path(control["path"]).expanduser().resolve()
            except (KeyError, TypeError, OSError):
                control_path = None
            if (
                control.get("kind") != "file"
                or (control_path, control.get("sha256")) not in configured_bindings
            ):
                raise FinalGamePackError(
                    f"Narrator selection for {queue_id!r} is not role-bound to "
                    f"the selected voice manifest character {character!r}"
                )
            narrator_selections.add(
                (
                    character,
                    control["sha256"],
                )
            )
    if len(narrator_selections) > 1:
        raise FinalGamePackError("Generation state mixes multiple narrator selections")
    if migrated_narrator_selection is not None:
        if narrator_selections:
            character, digest = next(iter(narrator_selections))
            if (
                normalize_character_name(character)
                != normalize_character_name(migrated_narrator_selection["character"])
                or digest not in migrated_narrator_selection["reference_sha256s"]
            ):
                raise FinalGamePackError(
                    "Reviewed and reproducible narrator selections conflict"
                )
        return migrated_narrator_selection
    if not narrator_selections:
        return None
    character, digest = next(iter(narrator_selections))
    return {"character": character, "reference_sha256": digest}


def _validate_generated_story_records(records, story):
    lines = {record.line_id: record for record in story.records}
    for generated in records:
        line = lines.get(generated["line_id"])
        if line is None or line.text_sha256 != generated["text_sha256"]:
            raise FinalGamePackError(
                f"Approved generated item {generated['line_id']!r} does not match the story index"
            )


def _validate_live_fallback_story_records(records, story):
    lines = {record.line_id: record for record in story.records}
    for fallback in records:
        line = lines.get(fallback["line_id"])
        if line is None or line.text_sha256 != fallback["text_sha256"]:
            raise FinalGamePackError(
                f"Live fallback item {fallback['line_id']!r} does not match the story index"
            )


def _validate_audio_event_omission_story_records(records, story):
    lines = {record.line_id: record for record in story.records}
    for omission in records:
        line = lines.get(omission["line_id"])
        if line is None or line.text_sha256 != omission["text_sha256"]:
            raise FinalGamePackError(
                f"Audio-event omission {omission['line_id']!r} does not match the story index"
            )


def _validate_reviewed_waveform_story_records(records, story):
    lines = {record.line_id: record for record in story.records}
    for migrated in records:
        line = lines.get(migrated["line_id"])
        if line is None or line.text_sha256 != migrated["text_sha256"]:
            raise FinalGamePackError(
                f"Reviewed waveform {migrated['line_id']!r} does not match the story index"
            )


def _validate_producers(producers):
    if not isinstance(producers, (list, tuple)) or not producers:
        raise FinalGamePackError("At least one producer name/version is required")
    validated = []
    for index, producer in enumerate(producers):
        if not isinstance(producer, dict) or set(producer) != {"name", "version"}:
            raise FinalGamePackError(
                f"Producer {index} must contain exactly name and version"
            )
        validated.append(
            {
                "name": _required_text(producer["name"], f"producer {index} name"),
                "version": _required_text(
                    producer["version"], f"producer {index} version"
                ),
            }
        )
    return validated


def _source_sha256(path, label):
    try:
        return sha256_file(path)
    except OSError as error:
        raise FinalGamePackError(
            f"Unable to checksum {label} {path}: {error}"
        ) from error


def _new_destination(value):
    try:
        candidate = Path(value).expanduser()
    except TypeError as error:
        raise FinalGamePackError("Final game-pack destination is invalid") from error
    if not candidate.name or candidate.name in {".", ".."}:
        raise FinalGamePackError(
            "Final game-pack destination requires a directory name"
        )
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.parent.resolve() / candidate.name


def _path_exists(path):
    return os.path.lexists(path)


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise FinalGamePackError(f"{label.capitalize()} must be non-empty text")
    return value.strip()


def _now():
    return datetime.now(timezone.utc).isoformat()
